import os
from pathlib import Path

from web_e2e_test_studio.config import load_dotenv, update_dotenv


def test_update_dotenv_preserves_unmanaged_entries_and_removes_values(
    tmp_path: Path,
    monkeypatch,
) -> None:  # noqa: ANN001
    path = tmp_path / "config" / ".env"
    path.parent.mkdir()
    path.write_text(
        "# managed locally\nCUSTOM_ENDPOINT=https://example.test\nVLM_MODEL=old\n",
        encoding="utf-8",
    )

    update_dotenv(
        path,
        {
            "VLM_MODEL": "vision-model",
            "VLM_API_KEY": "secret with spaces",
            "VLM_BASE_URL": None,
        },
    )

    content = path.read_text(encoding="utf-8")
    assert "# managed locally" in content
    assert "CUSTOM_ENDPOINT=https://example.test" in content
    assert content.count("VLM_MODEL=") == 1
    assert 'VLM_MODEL="vision-model"' in content
    assert 'VLM_API_KEY="secret with spaces"' in content
    assert "VLM_BASE_URL" not in content
    monkeypatch.delenv("VLM_MODEL", raising=False)
    monkeypatch.delenv("VLM_API_KEY", raising=False)
    assert load_dotenv(path) == {"CUSTOM_ENDPOINT", "VLM_MODEL", "VLM_API_KEY"}
    assert os.environ["VLM_API_KEY"] == "secret with spaces"
