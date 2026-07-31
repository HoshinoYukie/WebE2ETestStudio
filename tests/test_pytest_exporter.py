import ast
from pathlib import Path

from web_e2e_test_studio.domain import (
    Action,
    ActionChannel,
    JsonValue,
    Locator,
    RecordedStep,
    StepSource,
    VerificationResult,
    VerificationStatus,
)
from web_e2e_test_studio.domain import TestAsset as RecordedTestAsset
from web_e2e_test_studio.exporters import PytestExporter


def _recorded_step(
    step_id: str,
    name: str,
    *,
    arguments: dict[str, JsonValue] | None = None,
    locator: Locator | None = None,
    channel: ActionChannel = ActionChannel.BROWSER,
    tool_ref: str | None = None,
) -> RecordedStep:
    return RecordedStep(
        id=step_id,
        action=Action(
            name=name,
            channel=channel,
            arguments=arguments or {},
            locator=locator,
            tool_ref=tool_ref,
        ),
        intent=f"Perform {name}",
        expected_result=f"{name} completes",
        source=StepSource.VLM,
        verification=VerificationResult(
            status=VerificationStatus.VERIFIED,
            reason="Observed after recording",
        ),
    )


def _asset_with_browser_and_external_steps() -> RecordedTestAsset:
    return RecordedTestAsset(
        id="asset_checkout",
        name="Checkout happy path",
        goal="Complete a checkout without consulting a model during replay",
        base_url="https://shop.example.test",
        steps=[
            _recorded_step(
                "s01",
                "navigate",
                arguments={"url": "https://shop.example.test/cart"},
            ),
            _recorded_step(
                "s02",
                "click",
                locator=Locator(
                    "role",
                    "button",
                    name="Buy now",
                    nth=1,
                ),
            ),
            _recorded_step(
                "s03",
                "fill",
                arguments={"text": "buyer@example.test"},
                locator=Locator("label", "Email"),
            ),
            _recorded_step(
                "s04",
                "press",
                arguments={"key": "Enter"},
                locator=Locator("placeholder", "Promo code"),
            ),
            _recorded_step(
                "s05",
                "select",
                arguments={"value": "express"},
                locator=Locator("css", "#shipping-speed"),
            ),
            _recorded_step(
                "s06",
                "check",
                locator=Locator("test_id", "accept-terms"),
            ),
            _recorded_step(
                "s07",
                "hover",
                locator=Locator("text", "Delivery details", exact=False),
            ),
            _recorded_step(
                "s08",
                "wait",
                arguments={"milliseconds": 750},
            ),
            _recorded_step(
                "s09",
                "assert_text",
                arguments={"text": "Order confirmed"},
                locator=Locator("role", "status"),
            ),
            _recorded_step(
                "s10",
                "assert_visible",
                locator=Locator("css", "[data-receipt]"),
            ),
            _recorded_step(
                "s11",
                "screenshot",
                arguments={"path": "artifacts/receipt.png", "full_page": True},
            ),
            _recorded_step(
                "s12",
                "run_receipt_helper",
                channel=ActionChannel.EXTERNAL,
                tool_ref="receipt_helper",
            ),
        ],
        tool_dependencies={
            "receipt_helper": {
                "command": [
                    "python",
                    "-m",
                    "receipt_helper",
                    "${ENV:ORDER_ID}",
                ],
                "cwd": "${ENV:HELPER_ROOT}",
                "env": {
                    "API_TOKEN": "${ENV:RECEIPT_TOKEN}",
                    "MODE": "test",
                },
                "timeout": 12.5,
            }
        },
    )


def test_exporter_supports_builtin_spreadsheet_action_without_tool_ref() -> None:
    asset = RecordedTestAsset(
        id="asset_spreadsheet",
        name="Spreadsheet extraction",
        goal="Extract a bulk installation link",
        steps=[
            _recorded_step(
                "s01",
                "extract_spreadsheet",
                channel=ActionChannel.EXTERNAL,
                arguments={
                    "file_path": "downloads/Devices.xlsx",
                    "query": "bulk installation link",
                },
            )
        ],
    )

    source = PytestExporter().render(asset)

    assert "_extract_spreadsheet(" in source
    assert "subprocess.run" not in source
    ast.parse(source)


def test_exporter_records_application_launch_without_waiting() -> None:
    asset = RecordedTestAsset(
        id="asset_app",
        name="Launch desktop app",
        goal="Open the imported simulator",
        steps=[
            _recorded_step(
                "s01",
                "launch_application",
                channel=ActionChannel.EXTERNAL,
                tool_ref="tool_simulator",
                arguments={"args": ["--recording"]},
            )
        ],
        tool_dependencies={
            "tool_simulator": {
                "id": "tool_simulator",
                "name": "Simulator",
                "kind": "exe",
                "root": "tools/simulator",
                "entry": "Simulator.exe",
                "default_args": [],
                "default_env": {},
                "timeout": 30,
            }
        },
    )

    source = PytestExporter().render(asset)

    ast.parse(source)
    assert "subprocess.Popen(" in source
    assert "subprocess.run(" not in source
    assert "'Simulator.exe'" in source
    assert "'--recording'" in source


def test_render_produces_standalone_model_free_pytest_for_recorded_steps() -> None:
    source = PytestExporter().render(_asset_with_browser_and_external_steps())

    ast.parse(source)
    assert "import pytest" in source
    assert "from playwright.sync_api import Page, sync_playwright, expect" in source
    assert "@pytest.fixture()" in source
    assert "def page() -> Iterator[Page]:" in source
    assert "def test_checkout_happy_path(page: Page) -> None:" in source

    assert "page.goto('https://shop.example.test/cart')" in source
    assert (
        "page.get_by_role('button', name='Buy now', exact=True).nth(1).click()"
        in source
    )
    assert "page.get_by_label('Email', exact=True).fill('buyer@example.test')" in source
    assert "page.get_by_placeholder('Promo code', exact=True).press('Enter')" in source
    assert "page.locator('#shipping-speed').select_option('express')" in source
    assert "page.get_by_test_id('accept-terms').check()" in source
    assert "page.get_by_text('Delivery details', exact=False).hover()" in source
    assert "page.wait_for_timeout(750)" in source
    assert "expect(page.get_by_role('status', exact=True)).to_contain_text(" in source
    assert "'Order confirmed'" in source
    assert "expect(page.locator('[data-receipt]')).to_be_visible()" in source
    assert (
        "page.screenshot(path='artifacts/receipt.png', full_page=True)"
        in source
    )

    assert "subprocess.run(" in source
    assert (
        "['python', '-m', 'receipt_helper', os.environ['ORDER_ID']]"
        in source
    )
    assert "cwd=os.environ['HELPER_ROOT']" in source
    assert (
        "env={**os.environ, 'API_TOKEN': os.environ['RECEIPT_TOKEN'], "
        "'MODE': 'test'}"
        in source
    )
    assert "timeout=12.5" in source
    assert "check=True" in source

    assert "web_e2e_test_studio" not in source
    assert "openai" not in source.lower()
    assert "vlm" not in source.lower()


def test_exporter_uses_default_duration_for_wait_without_arguments() -> None:
    asset = RecordedTestAsset(
        id="asset_wait",
        name="Default wait",
        goal="Wait briefly",
        steps=[_recorded_step("s01", "wait")],
    )

    source = PytestExporter().render(asset)

    assert "page.wait_for_timeout(1000)" in source


def test_export_writes_the_rendered_pytest_file(tmp_path: Path) -> None:
    asset = _asset_with_browser_and_external_steps()
    destination = tmp_path / "nested" / "test_checkout.py"

    exported = PytestExporter().export(asset, destination)

    assert exported == destination
    assert destination.read_text(encoding="utf-8") == PytestExporter().render(asset)
    ast.parse(destination.read_text(encoding="utf-8"))


def test_export_renders_http_api_action_without_agent_runtime() -> None:
    asset = RecordedTestAsset(
        id="asset_api",
        name="API flow",
        goal="Create an item",
        steps=[
            _recorded_step(
                "s01",
                "http_request",
                channel=ActionChannel.API,
                arguments={
                    "method": "POST",
                    "url": "https://api.example.test/items",
                    "headers": {"X-Test": "true"},
                    "json": {"name": "book"},
                    "expected_status": 201,
                },
            )
        ],
    )

    source = PytestExporter().render(asset)

    ast.parse(source)
    assert "urllib.request" in source
    assert "https://api.example.test/items" in source
    assert "expected_status=201" in source
    assert "openai" not in source.lower()


def test_render_accepts_registry_portable_manifest_and_action_overrides() -> None:
    asset = RecordedTestAsset(
        id="asset_tools",
        name="External tool flow",
        goal="Run a project helper",
        steps=[
            _recorded_step(
                "s01",
                "run_external_tool",
                channel=ActionChannel.EXTERNAL,
                tool_ref="tool_receipt",
                arguments={
                    "args": ["invoice 123"],
                    "env": {"ACTION_TOKEN": "${ENV:ACTION_TOKEN}"},
                    "cwd": "runtime",
                    "timeout": 5,
                },
            )
        ],
        tool_dependencies={
            "tool_receipt": {
                "id": "tool_receipt",
                "name": "Receipt helper",
                "kind": "python",
                "root": "tools/receipt",
                "entry": "helper.py",
                "default_args": ["--json"],
                "default_env": {"MODE": "recorded"},
                "timeout": 30,
            }
        },
    )

    source = PytestExporter().render(asset)

    ast.parse(source)
    assert "PROJECT_ROOT" in source
    assert "sys.executable" in source
    assert "PROJECT_ROOT / 'tools' / 'receipt' / 'helper.py'" in source
    assert "'--json', 'invoice 123'" in source
    assert "cwd=PROJECT_ROOT / 'tools' / 'receipt' / 'runtime'" in source
    assert (
        "env={**os.environ, 'MODE': 'recorded', "
        "'ACTION_TOKEN': os.environ['ACTION_TOKEN']}"
    ) in source
    assert "timeout=5" in source
