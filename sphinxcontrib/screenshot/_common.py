# Copyright 2023 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Internal helpers and base directive shared by screenshot and screencast."""

import hashlib
import importlib
import json
import os
import typing
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse

from docutils.parsers.rst import directives
from playwright._impl._helper import ColorScheme
from playwright.sync_api import Browser, BrowserContext
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from sphinx.util import logging as sphinx_logging
from sphinx.util.docutils import SphinxDirective

logger = sphinx_logging.getLogger(__name__)

ContextBuilder = typing.Optional[typing.Callable[[Browser, str, str],
                                                 BrowserContext]]


def parse_expected_status_codes(codes_str: str) -> typing.List[int]:
  """Parse a comma-separated string of HTTP status codes into a list.

  Args:
    codes_str: Comma-separated status codes like "200, 201, 302".

  Returns:
    List of integer status codes.
  """
  return [int(code.strip()) for code in codes_str.split(',')]


def resolve_python_method(import_path: str):
  module_path, method_name = import_path.split(":")
  module = importlib.import_module(module_path)
  method = getattr(module, method_name)
  return method


def _hash_filename(parts: typing.Iterable[typing.Any], extension: str) -> str:
  """Build a deterministic ``<md5><extension>`` filename from arbitrary parts.

  Each part is stringified; dicts/lists are JSON-serialized with sorted keys
  so config dicts (e.g. headers) hash identically across runs.
  """

  def _normalize(value):
    if isinstance(value, (dict, list, tuple)):
      return json.dumps(value, sort_keys=True, default=str)
    return '' if value is None else str(value)

  payload = '_'.join(_normalize(p) for p in parts)
  return hashlib.md5(payload.encode()).hexdigest() + extension


def _invoke_context_builder(
    context_builder: typing.Callable, browser: Browser, url: str,
    color_scheme: ColorScheme,
    record_video_dir: typing.Optional[str]) -> BrowserContext:
  """Call a user-provided context builder with the right signature.

  For screenshot (record_video_dir is None), invoke the builder in 3-args
  mode for full backwards compatibility. For screencast, pass
  record_video_dir as a kwarg — the builder must accept it. The screencast
  directive validates this in its run() before reaching here, so the kwarg
  call is expected to succeed.
  """
  if record_video_dir is None:
    return context_builder(browser, url, color_scheme)
  return context_builder(
      browser, url, color_scheme, record_video_dir=record_video_dir)


def _prepare_context(
    playwright,
    browser_name: str,
    url: str,
    color_scheme: ColorScheme,
    locale: typing.Optional[str],
    timezone: typing.Optional[str],
    device_scale_factor: int,
    context_builder: ContextBuilder,
    record_video_dir: typing.Optional[str] = None
) -> typing.Tuple[Browser, BrowserContext]:
  """Launch a browser and create a context, optionally via a custom builder."""
  browser: Browser = getattr(playwright, browser_name).launch()

  if context_builder:
    try:
      context = _invoke_context_builder(context_builder, browser, url,
                                        color_scheme, record_video_dir)
    except PlaywrightTimeoutError:
      raise RuntimeError(
          'Timeout error occurred at %s in executing py init script %s' %
          (url, context_builder.__name__))
  else:
    new_context_kwargs: typing.Dict[str, typing.Any] = dict(
        color_scheme=color_scheme,
        locale=locale,
        timezone_id=timezone,
        device_scale_factor=device_scale_factor)
    if record_video_dir is not None:
      new_context_kwargs['record_video_dir'] = record_video_dir
    context = browser.new_context(**new_context_kwargs)

  return browser, context


def _navigate(page, url: str, valid_codes: typing.List[int],
              expected_status_codes: str,
              location: typing.Optional[str]) -> None:
  """Navigate to URL, warn on unexpected status, wait for networkidle."""
  response = page.goto(url)

  if response and response.status not in valid_codes:
    logger.warning(
        f'Page {url} returned status code {response.status}, '
        f'expected one of: {expected_status_codes}',
        type='screenshot',
        subtype='status_code',
        location=location)

  page.wait_for_load_state('networkidle')


def _run_interactions(page, interactions: str) -> None:
  """Run JS interactions and wait for networkidle.

  Interactions are wrapped in an async IIFE so users can use ``await`` at the
  top level (e.g. ``await new Promise(r => setTimeout(r, 500))``). Playwright
  awaits the returned Promise, so synchronous code keeps working unchanged.
  """
  if interactions:
    page.evaluate(f'(async () => {{ {interactions} }})()')
    page.wait_for_load_state('networkidle')


class _PlaywrightDirective(SphinxDirective):
  """Base class shared by Playwright-driven directives.

  Holds the option spec common to all directives that drive Playwright,
  the worker pool, and the helpers that resolve URLs and custom context
  builders.
  """

  common_option_spec: typing.Dict[str, typing.Callable[[str], typing.Any]] = {
      'browser': str,
      'viewport-height': directives.positive_int,
      'viewport-width': directives.positive_int,
      'interactions': str,
      'context': str,
      'headers': directives.unchanged,
      'locale': str,
      'timezone': str,
      'device-scale-factor': directives.positive_int,
      'status-code': str,
      'timeout': directives.positive_int,
  }
  pool = ThreadPoolExecutor()

  def _evaluate_substitutions(self, text: str) -> str:
    substitutions = self.state.document.substitution_defs
    for key, value in substitutions.items():
      text = text.replace(f"|{key}|", value.astext())
    return text

  def _resolve_url(self, raw_path: str) -> str:
    """Resolve a raw URL/path argument to an absolute URL.

    Substitutions are evaluated. Root-relative and document-relative
    file paths are converted to ``file://`` URLs. Only http/https/file
    schemes are accepted.
    """
    docdir = os.path.dirname(self.env.doc2path(self.env.docname))
    url_or_filepath = self._evaluate_substitutions(raw_path)
    scheme = urlparse(url_or_filepath).scheme

    if scheme == '':
      if url_or_filepath.startswith('/'):
        url_or_filepath = os.path.join(self.env.srcdir,
                                       url_or_filepath.lstrip('/'))
      else:
        url_or_filepath = os.path.join(docdir, url_or_filepath)
      url_or_filepath = "file://" + os.path.normpath(url_or_filepath)
      scheme = 'file'

    if scheme not in {'http', 'https', 'file'}:
      raise RuntimeError(
          f'Invalid URL: {url_or_filepath}. ' +
          'Only HTTP/HTTPS/FILE URLs or root/document-relative file paths ' +
          'are supported.')

    return url_or_filepath

  def _resolve_context_builder(self, context_name: str) -> ContextBuilder:
    """Resolve a context name to a callable, or None if unset."""
    if not context_name:
      return None
    context_builder_path = self.config.screenshot_contexts[context_name]
    return resolve_python_method(context_builder_path)
