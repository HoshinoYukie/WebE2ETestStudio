from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def isolate_studio_application_data(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keep per-user runtime data created by tests inside their temp directory."""
    monkeypatch.setenv("WEB_E2E_STUDIO_DATA_DIR", str(tmp_path / "application-data"))
