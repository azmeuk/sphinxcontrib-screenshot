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

import importlib.metadata
import typing
from pathlib import Path

from sphinx.application import Sphinx
from sphinx.util.fileutil import copy_asset

from ._screencast import (ScreencastDirective, depart_screencast_html,
                          screencast, visit_screencast_html,
                          visit_screencast_skip)
from ._screenshot import ScreenshotDirective
from ._wsgi import setup_apps, teardown_apps

__all__ = ['ScreenshotDirective', 'ScreencastDirective', 'setup']

Meta = typing.TypedDict('Meta', {
    'version': str,
    'parallel_read_safe': bool,
    'parallel_write_safe': bool
})


def copy_static_files(app: Sphinx, exception: typing.Optional[Exception]):
  """Copy static CSS files from the extension to the build output directory."""
  if exception is None and app.builder.format == 'html':
    static_source_dir = Path(__file__).parent / 'static'
    static_dest_dir = Path(app.outdir) / '_static' / 'sphinxcontrib-screenshot'
    static_dest_dir.mkdir(parents=True, exist_ok=True)

    css_source = str(static_source_dir / 'screenshot-theme.css')
    css_dest = str(static_dest_dir)
    copy_asset(css_source, css_dest)


def setup(app: Sphinx) -> Meta:
  app.add_directive('screenshot', ScreenshotDirective)
  app.add_directive('screencast', ScreencastDirective)
  app.add_node(
      screencast,
      html=(visit_screencast_html, depart_screencast_html),
      text=(visit_screencast_skip, lambda s, n: None),
      latex=(visit_screencast_skip, lambda s, n: None),
      man=(visit_screencast_skip, lambda s, n: None),
      texinfo=(visit_screencast_skip, lambda s, n: None),
  )
  app.add_config_value('screenshot_init_script', '', 'env')
  app.add_config_value(
      'screenshot_default_viewport_width',
      1280,
      'env',
      description="The default width for screenshots")
  app.add_config_value(
      'screenshot_default_viewport_height',
      960,
      'env',
      description="The default height for screenshots")
  app.add_config_value(
      'screenshot_default_browser',
      'chromium',
      'env',
      description="The default browser for screenshots")
  app.add_config_value(
      'screenshot_default_full_page',
      False,
      'env',
      description="Whether to take full page screenshots")
  app.add_config_value(
      'screenshot_default_color_scheme',
      'null',
      'env',
      description="The default color scheme for screenshots. " +
      "Use 'auto' to generate both light and dark mode screenshots")
  app.add_config_value(
      'screenshot_contexts', {},
      'env',
      types=[dict[str, str]],
      description="A dict of paths to Playwright context build methods")
  app.add_config_value(
      'screenshot_default_headers', {},
      'env',
      description="The default headers to pass in requests")
  app.add_config_value(
      'screenshot_default_device_scale_factor',
      1,
      'env',
      description="The default device scale factor " +
      "a.k.a. DPR (device pixel ratio)")
  app.add_config_value(
      'screenshot_default_locale',
      None,
      'env',
      description="The default locale in requests")
  app.add_config_value(
      'screenshot_default_timezone',
      None,
      'env',
      description="The default timezone in requests")
  app.add_config_value(
      'screenshot_apps', {},
      'env',
      types=[dict[str, str]],
      description="A dict of WSGI apps")
  app.add_config_value(
      'screenshot_default_timeout',
      10000,
      'env',
      description="The default timeout in milliseconds for page operations")
  app.connect('config-inited', setup_apps)
  app.connect('build-finished', teardown_apps)
  app.connect('build-finished', copy_static_files)
  app.add_css_file('sphinxcontrib-screenshot/screenshot-theme.css')
  return {
      'version': importlib.metadata.version('sphinxcontrib-screenshot'),
      'parallel_read_safe': True,
      'parallel_write_safe': True,
  }
