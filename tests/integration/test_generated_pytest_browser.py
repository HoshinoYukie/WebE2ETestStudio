from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

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
from web_e2e_test_studio.recording.manual import BrowserEventRecorder

pytestmark = pytest.mark.browser


@pytest.mark.skipif(
    os.getenv("RUN_BROWSER_TESTS") != "1",
    reason="set RUN_BROWSER_TESTS=1 after installing Playwright Chromium",
)
def test_context_capture_script_is_valid_in_chromium(tmp_path: Path) -> None:
    device = PlaywrightDevice(
        headless=True,
        artifact_dir=tmp_path / "contexts",
    )
    try:
        device.start()
        result = device.execute(
            Action(
                "navigate",
                ActionChannel.BROWSER,
                {"url": "data:text/html,<button>Save</button>"},
            )
        )

        observation = device.observe()

        assert result.success
        assert observation.interactive_elements[0]["text"] == "Save"
    finally:
        device.close()


def _step(
    step_id: str,
    action: Action,
) -> RecordedStep:
    return RecordedStep(
        id=step_id,
        action=action,
        intent=action.name,
        expected_result=f"{action.name} succeeds",
        source=StepSource.MANUAL,
        verification=VerificationResult(
            VerificationStatus.VERIFIED,
            "Verified during authoring",
        ),
    )


@pytest.mark.skipif(
    os.getenv("RUN_BROWSER_TESTS") != "1",
    reason="set RUN_BROWSER_TESTS=1 after installing Playwright Chromium",
)
def test_generated_pytest_runs_in_chromium_without_the_authoring_package(
    tmp_path: Path,
) -> None:
    page = tmp_path / "form.html"
    page.write_text(
        """<!doctype html>
<html>
<body>
  <label>Name <input id="name"></label>
  <button type="button">Submit</button>
  <div role="status" hidden></div>
  <script>
    document.querySelector("button").addEventListener("click", () => {
      const status = document.querySelector('[role="status"]');
      status.hidden = false;
      status.textContent = `Saved ${document.querySelector("#name").value}`;
    });
  </script>
</body>
</html>
""",
        encoding="utf-8",
    )
    asset = Asset(
        id="asset_browser_smoke",
        name="Generated browser smoke",
        goal="Submit a local form",
        steps=[
            _step(
                "s1",
                Action(
                    "navigate",
                    ActionChannel.BROWSER,
                    {"url": page.as_uri()},
                ),
            ),
            _step(
                "s2",
                Action(
                    "fill",
                    ActionChannel.BROWSER,
                    {"text": "Alice"},
                    Locator("label", "Name"),
                ),
            ),
            _step(
                "s3",
                Action(
                    "click",
                    ActionChannel.BROWSER,
                    locator=Locator("role", "button", name="Submit"),
                ),
            ),
            _step(
                "s4",
                Action(
                    "assert_text",
                    ActionChannel.BROWSER,
                    {"text": "Saved Alice"},
                    Locator("role", "status"),
                ),
            ),
        ],
    )
    generated = PytestExporter().export(asset, tmp_path / "test_generated.py")
    source = generated.read_text(encoding="utf-8")
    assert "web_e2e_test_studio" not in source
    assert "openai" not in source.lower()

    environment = os.environ.copy()
    environment.pop("VLM_API_KEY", None)
    environment.pop("OPENAI_API_KEY", None)
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            str(generated),
            "-q",
            "-p",
            "no:cacheprovider",
            "--basetemp",
            str(tmp_path / "generated-test-temp"),
        ],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "1 passed" in completed.stdout


@pytest.mark.skipif(
    os.getenv("RUN_BROWSER_TESTS") != "1",
    reason="set RUN_BROWSER_TESTS=1 after installing Playwright Chromium",
)
def test_manual_recorder_keeps_actions_when_click_navigates(
    tmp_path: Path,
) -> None:
    target = tmp_path / "target.html"
    target.write_text("<title>Target</title><h1>Arrived</h1>", encoding="utf-8")
    source = tmp_path / "source.html"
    source.write_text(
        f"""<!doctype html>
<label>Name <input></label>
<a href="{target.as_uri()}">Continue</a>
""",
        encoding="utf-8",
    )
    device = PlaywrightDevice(headless=True)
    recorder = BrowserEventRecorder()
    try:
        device.start()
        assert device.page is not None
        recorder.install(device.page)
        device.page.goto(source.as_uri())
        device.page.get_by_label("Name").fill("Alice")
        device.page.get_by_role("link", name="Continue").click()
        device.page.wait_for_load_state("load")

        actions = recorder.drain(device.page)
    finally:
        device.close()

    assert [action.name for action in actions] == ["fill", "click"]
    assert actions[0].arguments == {"text": "Alice"}
