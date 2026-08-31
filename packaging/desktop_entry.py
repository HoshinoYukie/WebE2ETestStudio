"""PyInstaller entry point for Desktop Studio and its environment tool."""

import sys


def _main() -> int:
    if sys.argv[1:2] == ["--environment-tool"]:
        from web_e2e_test_studio.environment_cli import main

        return main(sys.argv[2:])

    from web_e2e_test_studio.desktop.modern_app import main

    return main()



if __name__ == "__main__":
    raise SystemExit(_main())
