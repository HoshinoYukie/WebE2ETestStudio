from __future__ import annotations

from pathlib import Path

from web_e2e_test_studio.browser import PlaywrightDevice
from web_e2e_test_studio.domain import (
    Action,
    ActionChannel,
    Locator,
    RecordedStep,
    StepSource,
    VerificationResult,
    VerificationStatus,
)
from web_e2e_test_studio.domain import TestAsset as Asset
from web_e2e_test_studio.exporters import PytestExporter


class FakeLocator:
    def count(self) -> int:
        return 1

    def click(self) -> None:
        return None


class FakeDownload:
    suggested_filename = "devices.xlsx"
    url = "https://example.test/export"

    def save_as(self, path: str) -> None:
        Path(path).write_bytes(b"xlsx")


class FakeDownloadInfo:
    value = FakeDownload()

    def __enter__(self) -> FakeDownloadInfo:
        return self

    def __exit__(self, *args) -> None:  # noqa: ANN002
        return None


class FakePage:
    def get_by_role(self, role: str, **options) -> FakeLocator:  # noqa: ANN003
        assert role == "button"
        assert options["name"] == "Export All"
        return FakeLocator()

    def expect_download(self, *, timeout: float) -> FakeDownloadInfo:
        assert timeout == 30_000
        return FakeDownloadInfo()


def test_download_action_saves_file_and_returns_evidence(tmp_path: Path) -> None:
    device = PlaywrightDevice(
        page=FakePage(),
        artifact_dir=tmp_path / "observations",
    )
    action = Action(
        name="download",
        channel=ActionChannel.BROWSER,
        locator=Locator("role", "button", name="Export All"),
    )

    result = device.execute(action)

    assert result.success
    assert result.output["filename"] == "devices.xlsx"
    assert Path(str(result.output["path"])).read_bytes() == b"xlsx"


def test_download_action_exports_model_free_pytest() -> None:
    action = Action(
        name="download",
        channel=ActionChannel.BROWSER,
        locator=Locator("role", "button", name="Export All"),
    )
    asset = Asset.create(name="download_devices", goal="Export devices")
    asset.steps.append(
        RecordedStep(
            id="step_download",
            action=action,
            intent="Download the XLSX export",
            expected_result="The export is saved",
            source=StepSource.VLM,
            verification=VerificationResult(
                VerificationStatus.VERIFIED,
                "Download completed",
            ),
        )
    )

    source = PytestExporter().render(asset)

    assert "page.expect_download" in source
    assert "download.suggested_filename" in source
    assert "openai" not in source.lower()
    compile(source, "<download-test>", "exec")
