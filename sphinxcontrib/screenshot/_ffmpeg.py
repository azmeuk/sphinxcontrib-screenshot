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
"""ffmpeg location and post-processing helpers used by screencast trim/crop."""

import os
import re
import shutil
import subprocess
import typing

# Playwright bundles ffmpeg at <browsers-path>/ffmpeg-<rev>/ffmpeg-<platform>.
# Example: ~/.cache/ms-playwright/ffmpeg-1011/ffmpeg-linux
_FFMPEG_DIR_RE = re.compile(r'^ffmpeg-\d+$')
_FFMPEG_BIN_RE = re.compile(r'^ffmpeg-[a-z]+$')


def _find_ffmpeg() -> typing.Optional[str]:
  """Locate an ffmpeg binary.

  Prefers the one bundled by Playwright at ``$PLAYWRIGHT_BROWSERS_PATH`` (or
  the default ``~/.cache/ms-playwright``). Falls back to a system ffmpeg on
  PATH. Returns None if nothing is found.
  """
  base = os.environ.get('PLAYWRIGHT_BROWSERS_PATH') or os.path.expanduser(
      '~/.cache/ms-playwright')
  candidates = []
  if os.path.isdir(base):
    for entry in os.listdir(base):
      if not _FFMPEG_DIR_RE.match(entry):
        continue
      ffmpeg_dir = os.path.join(base, entry)
      for bin_entry in os.listdir(ffmpeg_dir):
        if _FFMPEG_BIN_RE.match(bin_entry):
          candidates.append(os.path.join(ffmpeg_dir, bin_entry))
  candidates.sort(reverse=True)  # newest revision first
  bundled = next(
      (c for c in candidates if os.path.isfile(c) and os.access(c, os.X_OK)),
      None)
  return bundled or shutil.which('ffmpeg')


def _require_ffmpeg(reason: str) -> str:
  """Resolve ffmpeg or raise a clear error explaining what needs it."""
  ffmpeg = _find_ffmpeg()
  if not ffmpeg:
    raise RuntimeError(
        f'{reason} requires ffmpeg, but none was found. Install Playwright '
        f"browsers (`playwright install`) which bundles ffmpeg, or install "
        f'a system ffmpeg available on PATH.')
  return ffmpeg


def _postprocess_video(
    ffmpeg: str,
    src: str,
    dst: str,
    trim_start: typing.Optional[float] = None,
    crop: typing.Optional[typing.Tuple[int, int, int, int]] = None,
) -> None:
  """Apply optional trim and/or crop to ``src`` and write to ``dst``.

  Combines both into a single libvpx re-encode pass when both are requested.
  ``crop`` is ``(x, y, w, h)`` in video pixels.
  """
  cmd = [
      ffmpeg,
      '-y',  # overwrite output without prompting
      '-i',
      src,  # input file
  ]
  if trim_start:
    cmd += ['-ss', f'{trim_start:.3f}']  # seek to start (seconds)
  if crop:
    x, y, w, h = crop
    cmd += ['-vf', f'crop={w}:{h}:{x}:{y}']  # video filter
  cmd += [
      '-an',  # drop audio
      '-c:v',
      'libvpx',  # encode video as VP8 (WebM)
      '-b:v',
      '1M',  # target bitrate; default of 200kbps is too low for screencasts
      '-crf',
      '4',  # quality (0-63, lower is better) — high quality for doc
      dst,
  ]
  try:
    subprocess.run(
        cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
  except subprocess.CalledProcessError as e:
    stderr = (e.stderr or b'').decode('utf-8', errors='replace').strip()
    raise RuntimeError(
        f'ffmpeg failed (exit {e.returncode}) for {src!r}:\n{stderr}') from e
