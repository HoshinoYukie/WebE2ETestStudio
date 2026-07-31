"""Tiny deterministic external-tool example used by the recording CLI."""

from __future__ import annotations

import json
import sys


def main() -> int:
    print(json.dumps({"arguments": sys.argv[1:]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

