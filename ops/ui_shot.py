"""Capture a page with an installed headless Chrome.

Usage: ``python ops/ui_shot.py <url> <out.png> [--width 1440 --height 900 --wait-ms N]``.
The URL may be ``file://`` or an HTTP URL. No browser package is installed by
this helper; it uses the first Chrome or Chromium executable on the machine.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from pathlib import Path


def browser() -> str:
    candidates = [
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Chromium.app/Contents/MacOS/Chromium",
        "google-chrome",
        "chromium",
        "chromium-browser",
    ]
    for candidate in candidates:
        path = candidate if candidate.startswith("/") else shutil.which(candidate)
        if path and Path(path).is_file():
            return path
    raise SystemExit("no Chrome or Chromium executable found")


def capture(url: str, output: Path, width: int = 1440, height: int = 900, wait_ms: int = 500) -> None:
    if width < 1 or height < 1 or wait_ms < 0:
        raise ValueError("width and height must be positive; wait-ms cannot be negative")
    output.parent.mkdir(parents=True, exist_ok=True)
    parsed = urlsplit(url)
    if parsed.scheme in ("http", "https"):
        query = parse_qsl(parsed.query, keep_blank_values=True)
        query.append(("screenshot", "1"))
        url = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), parsed.fragment))
    command = [browser(), "--headless=new", "--disable-gpu", "--hide-scrollbars",
               f"--window-size={width},{height}", f"--virtual-time-budget={wait_ms}",
               f"--screenshot={output}", url]
    subprocess.run(command, check=True, timeout=30)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url")
    parser.add_argument("out", type=Path)
    parser.add_argument("--width", type=int, default=1440)
    parser.add_argument("--height", type=int, default=900)
    parser.add_argument("--wait-ms", type=int, default=500)
    args = parser.parse_args()
    capture(args.url, args.out, args.width, args.height, args.wait_ms)


if __name__ == "__main__":
    main()
