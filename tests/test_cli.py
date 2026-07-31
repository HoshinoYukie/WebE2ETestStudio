from pathlib import Path

from web_e2e_test_studio.cli import main
from web_e2e_test_studio.domain import TestAsset as Asset
from web_e2e_test_studio.storage import JsonAssetStore


def test_cli_exports_and_inspects_an_asset(
    tmp_path: Path, capsys
) -> None:  # noqa: ANN001
    asset_path = tmp_path / "flow.asset.json"
    output = tmp_path / "test_flow.py"
    JsonAssetStore().save(
        Asset.create(
            name="Recorded flow",
            goal="Demonstrate a model-free export",
            base_url="https://example.test",
        ),
        asset_path,
    )

    assert main(["export", str(asset_path), "--output", str(output)]) == 0
    assert output.is_file()
    assert "openai" not in output.read_text(encoding="utf-8").lower()

    assert main(["inspect", str(asset_path)]) == 0
    captured = capsys.readouterr().out
    assert "Recorded flow" in captured
    assert "verified steps: 0" in captured

