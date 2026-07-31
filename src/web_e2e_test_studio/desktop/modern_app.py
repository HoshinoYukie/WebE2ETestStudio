"""Desktop launcher for the Code-Agent-style WebView workbench."""

from __future__ import annotations

import argparse
from pathlib import Path

from web_e2e_test_studio.desktop.bridge import StudioBridge


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="web-e2e-desktop",
        description="Launch the test Agent Desktop workbench.",
    )
    parser.add_argument(
        "--workspace",
        type=Path,
        default=Path.cwd(),
        help="Project workspace (default: current directory)",
    )
    args = parser.parse_args(argv)
    try:
        import webview
    except ImportError as exc:
        raise SystemExit(
            "Desktop dependency missing. Run: "
            'python -m pip install -e ".[desktop]"'
        ) from exc

    bridge = StudioBridge(args.workspace)
    static_dir = Path(__file__).with_name("web")
    window = webview.create_window(
        "Test Agent Studio",
        url=str(static_dir / "index.html"),
        js_api=bridge,
        width=1480,
        height=920,
        min_size=(1100, 700),
        background_color="#0b0d10",
    )
    if window is None:
        raise RuntimeError("The Desktop WebView window could not be created")
    bridge.attach_window(window)
    window.events.closed += bridge.close
    webview.start(debug=False)
    return 0


__all__ = ["main"]
