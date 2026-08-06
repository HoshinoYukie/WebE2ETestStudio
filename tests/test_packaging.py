import ast
from pathlib import Path


def test_pyinstaller_distribution_files_reference_required_resources() -> None:
    root = Path(__file__).parents[1]
    entry = root / "packaging" / "desktop_entry.py"
    spec = root / "packaging" / "WebE2ETestStudio.spec"
    script = root / "scripts" / "build_desktop.ps1"

    ast.parse(entry.read_text(encoding="utf-8"))
    spec_source = spec.read_text(encoding="utf-8")
    assert "web_e2e_test_studio/desktop/web" in spec_source
    assert "runtime/playwright" in spec_source
    assert "collect_all(\"webview\")" in spec_source
    assert "collect_submodules(\"playwright\")" in spec_source
    assert "PyInstaller" in script.read_text(encoding="utf-8")
