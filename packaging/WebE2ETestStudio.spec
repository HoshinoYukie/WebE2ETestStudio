# -*- mode: python ; coding: utf-8 -*-

import os
from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_submodules

project_root = Path(SPECPATH).resolve().parent
source_root = project_root / "src"
web_root = source_root / "web_e2e_test_studio" / "desktop" / "web"

webview_data, webview_binaries, webview_hidden = collect_all("webview")
playwright_data, playwright_binaries, playwright_hidden = collect_all("playwright")
datas = [
    *webview_data,
    *playwright_data,
    (str(web_root), "web_e2e_test_studio/desktop/web"),
]
binaries = [*webview_binaries, *playwright_binaries]
hiddenimports = [
    *webview_hidden,
    *playwright_hidden,
    *collect_submodules("playwright"),
]

browser_root_value = os.environ.get("WEB_E2E_BUNDLED_BROWSERS", "").strip()
if browser_root_value:
    browser_root = Path(browser_root_value).expanduser().resolve()
    if not browser_root.is_dir():
        raise ValueError(f"Bundled browser directory does not exist: {browser_root}")
    datas.append((str(browser_root), "runtime/playwright"))

analysis = Analysis(
    [str(project_root / "packaging" / "desktop_entry.py")],
    pathex=[str(source_root)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(analysis.pure)
exe = EXE(
    pyz,
    analysis.scripts,
    [],
    exclude_binaries=True,
    name="WebE2ETestStudio",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
)
collection = COLLECT(
    exe,
    analysis.binaries,
    analysis.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="WebE2ETestStudio",
)
