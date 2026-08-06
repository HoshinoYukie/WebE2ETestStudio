"""PyInstaller entry point for Desktop Studio."""

from web_e2e_test_studio.desktop.modern_app import main  # noqa: I001


if __name__ == "__main__":
    raise SystemExit(main())
