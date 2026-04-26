# Copyright 2026 Google LLC
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

import inspect
import math
import os
import tempfile
import time
import typing

from docutils import nodes
from docutils.parsers.rst import directives
from docutils.parsers.rst.directives.images import Figure
from playwright._impl._helper import ColorScheme
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright
from sphinx.util import logging as sphinx_logging

from ._common import (ContextBuilder, _hash_filename, _navigate,
                      _PlaywrightDirective, _prepare_context,
                      _run_interactions, parse_expected_status_codes)
from ._ffmpeg import _postprocess_video, _require_ffmpeg

logger = sphinx_logging.getLogger(__name__)


class screencast(nodes.General, nodes.Element):
  """Docutils node rendered as <figure><video></video></figure> in HTML."""


def visit_screencast_html(self, node: screencast) -> None:
  classes = list(node.get('classes', []))
  align = node.get('align')
  if align:
    classes.append(f'align-{align}')
  classes_attr = ''
  if classes:
    classes_attr = f' class="{self.attval(" ".join(classes))}"'
  self.body.append(f'<figure{classes_attr}>\n')

  video_attrs = f'src="{self.attval(node["src"])}"'
  for flag in ('controls', 'autoplay', 'loop', 'muted'):
    if node.get(flag):
      video_attrs += f' {flag}'
  if node.get('poster'):
    video_attrs += f' poster="{self.attval(node["poster"])}"'
  self.body.append(f'<video {video_attrs}></video>\n')

  caption = node.get('caption')
  if caption:
    self.body.append(f'<figcaption>{self.encode(caption)}</figcaption>\n')


def depart_screencast_html(self, node: screencast) -> None:
  self.body.append('</figure>\n')


def visit_screencast_skip(self, node: screencast) -> None:
  """Fallback for non-HTML builders: silently skip the node so cached
  doctrees (potentially containing a screencast node from a prior HTML build)
  don't crash a subsequent text/latex/etc. build."""
  raise nodes.SkipNode


_FIGURE_OPTIONS_REUSED: typing.Dict[str, typing.Callable[
    [str], typing.Any]] = {
        name: Figure.option_spec[name]
        for name in ('align', 'figclass', 'name', 'class')
        if Figure.option_spec and name in Figure.option_spec
    }


class ScreencastDirective(_PlaywrightDirective):
  """Sphinx Screencast Directive.

  Records a WebM video of a webpage using Playwright and embeds it as an
  HTML5 ``<video>`` tag.

  ```rst
  .. screencast:: http://www.example.com

      document.querySelector('button').click();
      await new Promise(r => setTimeout(r, 1000));
  ```

  All options of :rst:dir:`screenshot` related to the page setup are
  supported (browser, viewport, locale, timezone, headers, context, etc.)
  except those tied to still images (``pdf``, ``full-page``, ``locator``,
  ``color-scheme``).

  Video-specific options:

  - ``loop`` / ``autoplay`` / ``muted`` / ``controls``: HTML5 ``<video>``
    boolean attributes. ``autoplay`` without ``muted`` is rejected by most
    browsers, so ``muted`` is forced (with a warning) when ``autoplay`` is
    set.
  - ``poster``: URL of the still image displayed before playback. Also
    used as fallback for non-HTML builders.
  - ``caption``: optional ``<figcaption>`` text.
  """

  required_arguments = 1
  has_content = True
  option_spec = {
      **_FIGURE_OPTIONS_REUSED,
      **_PlaywrightDirective.common_option_spec,
      'loop': directives.flag,
      'autoplay': directives.flag,
      'muted': directives.flag,
      'controls': directives.flag,
      'poster': directives.unchanged,
      'caption': directives.unchanged,
      'trim-start': directives.unchanged,
      'locator': str,
  }

  @staticmethod
  def take_screencast(url: str,
                      browser_name: str,
                      viewport_width: int,
                      viewport_height: int,
                      filepath: str,
                      init_script: str,
                      interactions: str,
                      context_builder: ContextBuilder,
                      headers: dict,
                      device_scale_factor: int,
                      locale: typing.Optional[str],
                      timezone: typing.Optional[str],
                      generate_poster: bool = False,
                      trim_start: typing.Optional[float] = None,
                      trim_auto: bool = False,
                      locator: typing.Optional[str] = None,
                      expected_status_codes: typing.Optional[str] = None,
                      location: typing.Optional[str] = None,
                      timeout: int = 10000):
    """Records a WebM screencast of a webpage with Playwright.

    The recording covers `goto -> networkidle -> interactions -> networkidle`.
    To capture animations after a click, the user must keep the page busy in
    the interactions JS (e.g. `await new Promise(r => setTimeout(r, 1000))`).

    When ``generate_poster`` is True, a PNG screenshot of the loaded page is
    saved next to ``filepath`` (with a .png extension), captured before any
    interaction so it matches frame 0 of the video.

    ``trim_start`` (in seconds) trims the beginning of the recording.
    ``trim_auto=True`` measures the time between context creation and the
    end of the page load and uses it as the trim offset, eliminating the
    initial about:blank flash. Mutually exclusive with ``trim_start``.

    ``locator`` is a Playwright selector. When set, the video is cropped to
    the bounding box of the matched element via ffmpeg post-processing.
    """
    if expected_status_codes is None:
      expected_status_codes = "200,302"

    valid_codes = parse_expected_status_codes(expected_status_codes)
    poster_path = os.path.splitext(filepath)[0] + '.png'
    success = False
    try:
      with (tempfile.TemporaryDirectory() as tmp_dir, sync_playwright() as
            playwright):
        browser, context = _prepare_context(
            playwright,
            browser_name,
            url,
            typing.cast(ColorScheme, 'null'),
            locale,
            timezone,
            device_scale_factor,
            context_builder,
            record_video_dir=tmp_dir)
        # Capture timer right after context creation so auto-trim brackets
        # the actual video — placing it earlier would also count the browser
        # launch overhead (100–500 ms) and trim into real content.
        t_context_start = time.monotonic()

        page = context.new_page()
        page.set_default_timeout(timeout)
        page.set_viewport_size({
            'width': viewport_width,
            'height': viewport_height
        })

        crop_box: typing.Optional[typing.Tuple[int, int, int, int]] = None
        auto_trim_offset: typing.Optional[float] = None

        try:
          if init_script:
            page.add_init_script(init_script)
          page.set_extra_http_headers(headers)
          _navigate(page, url, valid_codes, expected_status_codes, location)

          if trim_auto:
            auto_trim_offset = time.monotonic() - t_context_start

          if generate_poster:
            page.screenshot(path=poster_path)

          _run_interactions(page, interactions)

          if locator:
            bbox = page.locator(locator).bounding_box(timeout=timeout)
            if bbox is None:
              raise RuntimeError(
                  f'Locator {locator!r} did not match a visible element on '
                  f'{url}.')

            # Floor x/y and ceil w/h so the bounding box always encloses the
            # element (the opposite would shave off sub-pixel edges). Clamp
            # to the viewport since ffmpeg crop coordinates outside the
            # frame fail with a cryptic message.
            x = max(0, math.floor(bbox['x']))
            y = max(0, math.floor(bbox['y']))
            w = min(
                math.ceil(bbox['x'] + bbox['width']) - x, viewport_width - x)
            h = min(
                math.ceil(bbox['y'] + bbox['height']) - y, viewport_height - y)
            if w <= 0 or h <= 0:
              raise RuntimeError(
                  f'Locator {locator!r} bounding box ({bbox}) is outside '
                  f'the viewport ({viewport_width}x{viewport_height}).')

            crop_box = (x, y, w, h)
        except PlaywrightTimeoutError as e:
          raise RuntimeError('Timeout error occurred at %s in executing\n%s' %
                             (url, interactions)) from e

        # Keep a reference to the video before closing — page.close() and
        # context.close() are required to flush the .webm to disk before
        # save_as can read it.
        video = page.video
        page.close()
        context.close()
        if video is None:
          raise RuntimeError(
              'Playwright did not record a video. The custom context '
              'builder likely did not pass record_video_dir to '
              'browser.new_context().')

        video.save_as(filepath)
        browser.close()

        # Post-process: trim and/or crop via ffmpeg.
        effective_trim = auto_trim_offset if trim_auto else trim_start
        if (effective_trim and effective_trim > 0) or crop_box:
          ffmpeg = _require_ffmpeg(
              'screencast trim-start/locator post-processing')
          intermediate = os.path.join(tmp_dir, 'postprocessed.webm')
          _postprocess_video(
              ffmpeg,
              filepath,
              intermediate,
              trim_start=effective_trim
              if effective_trim and effective_trim > 0 else None,
              crop=crop_box)
          os.replace(intermediate, filepath)

      success = True
    finally:
      if not success:
        # Remove any partial output left behind so the next build retries
        # from scratch instead of returning a stale or half-written file.
        for path in (filepath, poster_path):
          if os.path.exists(path):
            try:
              os.remove(path)
            except OSError:
              pass

  def run(self) -> typing.Sequence[nodes.Node]:
    """Process the screencast directive and return a screencast node.

    For non-HTML builders, falls back to the poster image if provided,
    otherwise emits a warning and skips the directive.
    """
    # Three modes for :poster:
    #   - absent       → poster_mode = 'none'
    #   - flag (empty) → poster_mode = 'auto'   (auto-screenshot)
    #   - URL          → poster_mode = 'explicit'
    if 'poster' not in self.options:
      poster_mode = 'none'
      poster_value = ''
    elif not self.options['poster']:
      poster_mode = 'auto'
      poster_value = ''
    else:
      poster_mode = 'explicit'
      poster_value = self.options['poster']

    # Three modes for :trim-start: (same shape as :poster:)
    #   - absent       → trim_mode = 'none'
    #   - flag (empty) → trim_mode = 'auto'     (timer-based)
    #   - seconds      → trim_mode = 'explicit'
    if 'trim-start' not in self.options:
      trim_mode = 'none'
      trim_value: typing.Optional[float] = None
    elif not self.options['trim-start']:
      trim_mode = 'auto'
      trim_value = None
    else:
      trim_mode = 'explicit'
      try:
        trim_value = float(self.options['trim-start'])
      except ValueError:
        raise self.error(f':trim-start: must be a number of seconds, got '
                         f'{self.options["trim-start"]!r}.')

    locator_value: str = self.options.get('locator', '') or ''

    builder_format = self.env.app.builder.format
    if builder_format != 'html':
      if poster_mode == 'explicit':
        image_node = nodes.image(
            uri=poster_value, alt=self.options.get('caption', ''))
        return [image_node]
      logger.warning(
          'screencast directive skipped: builder %r is not HTML and no '
          'explicit :poster: URL was provided.' % builder_format,
          location=self.env.docname,
          type='screencast')
      return []

    if 'autoplay' in self.options and 'muted' not in self.options:
      logger.warning(
          'screencast: :autoplay: requires :muted: due to browser autoplay '
          'policies. Forcing muted.',
          location=self.env.docname,
          type='screencast')
      self.options['muted'] = True

    screencast_init_script: str = self.env.config.screenshot_init_script or ''

    sc_dirpath = os.path.join(self.env.app.outdir, '_static', 'screencasts')
    os.makedirs(sc_dirpath, exist_ok=True)

    raw_path = self.arguments[0]
    url_or_filepath = self._resolve_url(raw_path)

    interactions = '\n'.join(self.content) or self.options.get(
        'interactions', '')
    browser = self.options.get('browser',
                               self.env.config.screenshot_default_browser)
    viewport_height = self.options.get(
        'viewport-height', self.env.config.screenshot_default_viewport_height)
    viewport_width = self.options.get(
        'viewport-width', self.env.config.screenshot_default_viewport_width)
    locale = self.options.get('locale',
                              self.env.config.screenshot_default_locale)
    timezone = self.options.get('timezone',
                                self.env.config.screenshot_default_timezone)
    context = self.options.get('context', '')
    headers = self.options.get('headers', '')
    device_scale_factor = self.options.get(
        'device-scale-factor',
        self.env.config.screenshot_default_device_scale_factor)
    status_code = self.options.get('status-code', None)
    timeout = self.options.get('timeout',
                               self.env.config.screenshot_default_timeout)
    request_headers = {**self.env.config.screenshot_default_headers}
    if headers:
      for header in headers.strip().split("\n"):
        name, value = header.split(" ", 1)
        request_headers[name] = value

    loop_flag = 'loop' in self.options
    autoplay_flag = 'autoplay' in self.options
    muted_flag = 'muted' in self.options
    controls_flag = 'controls' in self.options
    caption = self.options.get('caption', '')

    filename = _hash_filename([
        raw_path,
        browser,
        viewport_height,
        viewport_width,
        context,
        interactions,
        device_scale_factor,
        status_code,
        loop_flag,
        autoplay_flag,
        muted_flag,
        controls_flag,
        poster_mode,
        poster_value,
        trim_mode,
        trim_value,
        locator_value,
        screencast_init_script,
        locale,
        timezone,
        request_headers,
    ], '.webm')
    filepath = os.path.join(sc_dirpath, filename)

    context_builder = self._resolve_context_builder(context)

    # Detect a 3-args context builder early — recording video requires the
    # builder to accept record_video_dir. Emit an error and skip the directive
    # rather than crashing the whole Sphinx build.
    if context_builder and 'record_video_dir' not in inspect.signature(
        context_builder).parameters:
      logger.error(
          f'screencast: context builder '
          f'{context_builder.__module__}.{context_builder.__name__} must '
          f'accept a record_video_dir parameter. Update its signature to '
          f'(browser, url, color_scheme, record_video_dir). Skipping '
          f'directive.',
          location=self.env.docname,
          type='screencast')
      return []

    poster_filepath = os.path.splitext(filepath)[0] + '.png'
    needs_recording = not os.path.exists(filepath)
    if poster_mode == 'auto' and not os.path.exists(poster_filepath):
      needs_recording = True

    if needs_recording:
      fut = self.pool.submit(
          ScreencastDirective.take_screencast,
          url_or_filepath,
          browser,
          viewport_width,
          viewport_height,
          filepath,
          screencast_init_script,
          interactions,
          context_builder,
          request_headers,
          device_scale_factor,
          locale,
          timezone,
          generate_poster=(poster_mode == 'auto'),
          trim_start=trim_value,
          trim_auto=(trim_mode == 'auto'),
          locator=locator_value or None,
          expected_status_codes=status_code,
          location=self.env.docname,
          timeout=timeout)
      fut.result()

    # Compute src relative to the HTML output of the current doc, since the
    # screencast node has no Sphinx-side image-collection machinery to rewrite
    # the URI for us (unlike nodes.image used by ScreenshotDirective).
    target_uri = self.env.app.builder.get_target_uri(self.env.docname)
    out_doc_dir = os.path.dirname(
        os.path.join(self.env.app.outdir, target_uri))
    rel_filepath = os.path.relpath(
        filepath, start=out_doc_dir).replace(os.sep, '/')

    node = screencast()
    node['src'] = rel_filepath
    node['loop'] = loop_flag
    node['autoplay'] = autoplay_flag
    node['muted'] = muted_flag
    node['controls'] = controls_flag
    if poster_mode == 'auto':
      node['poster'] = os.path.relpath(
          poster_filepath, start=out_doc_dir).replace(os.sep, '/')
    elif poster_mode == 'explicit':
      node['poster'] = poster_value
    if caption:
      node['caption'] = caption
    node['classes'] = list(self.options.get('class', []))
    figclass = self.options.get('figclass')
    if figclass:
      node['classes'].extend(figclass)
    align = self.options.get('align')
    if align:
      node['align'] = align
    self.add_name(node)
    return [node]
