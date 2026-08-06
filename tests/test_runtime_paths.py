from pathlib import Path
from types import SimpleNamespace

from web_e2e_test_studio.runtime_paths import RuntimePaths


def test_runtime_paths_use_local_app_data_and_create_writable_directories(
    tmp_path: Path,
) -> None:
    environment = {"LOCALAPPDATA": str(tmp_path / "Local")}

    paths = RuntimePaths.discover(environment=environment, platform="win32")
    paths.ensure()

    assert paths.data_dir == tmp_path / "Local" / "WebE2ETestStudio"
    assert paths.config_dir.is_dir()
    assert paths.browser_dir.is_dir()
    assert paths.session_dir.is_dir()
    assert paths.log_dir.is_dir()
    assert paths.cache_dir.is_dir()


def test_runtime_paths_allow_managed_data_directory_override(tmp_path: Path) -> None:
    managed = tmp_path / "managed-data"

    paths = RuntimePaths.discover(
        environment={"WEB_E2E_STUDIO_DATA_DIR": str(managed)},
        platform="win32",
    )

    assert paths.data_dir == managed.resolve()


def test_runtime_paths_resolve_pyinstaller_web_resources(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle"
    web = bundle / "web_e2e_test_studio" / "desktop" / "web"
    web.mkdir(parents=True)
    (web / "index.html").write_text("studio", encoding="utf-8")

    paths = RuntimePaths.discover(
        environment={"LOCALAPPDATA": str(tmp_path / "Local")},
        platform="win32",
        runtime=SimpleNamespace(_MEIPASS=str(bundle), frozen=True),
    )

    assert paths.web_dir == web.resolve()
    assert paths.web_entrypoint == (web / "index.html").resolve()
