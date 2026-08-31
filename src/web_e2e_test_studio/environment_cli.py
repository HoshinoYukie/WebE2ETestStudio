"""Hidden command mode used by source and packaged runtime repair."""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Sequence


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="WebE2ETestStudio environment tool")
    subcommands = parser.add_subparsers(dest="command", required=True)
    install = subcommands.add_parser("install-chromium")
    install.add_argument("--browser-path", required=True)
    arguments = parser.parse_args(argv)
    if arguments.command != "install-chromium":
        return 2

    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = arguments.browser_path
    from playwright.__main__ import main as playwright_main

    previous_arguments = sys.argv
    sys.argv = ["playwright", "install", "chromium"]
    try:
        playwright_main()
    except SystemExit as exc:
        return int(exc.code or 0)
    finally:
        sys.argv = previous_arguments
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
