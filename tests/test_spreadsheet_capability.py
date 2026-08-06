from __future__ import annotations

from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

import pytest

from web_e2e_test_studio.agent import (
    PlanCompiler,
    PlannedAction,
    PlanningResult,
)
from web_e2e_test_studio.agent.verification import DeterministicVerifier
from web_e2e_test_studio.capabilities import (
    CapabilityContext,
    default_capability_catalog,
)
from web_e2e_test_studio.domain import (
    Action,
    ActionChannel,
    ActionResult,
    ActionStatus,
    Observation,
    VerificationStatus,
)
from web_e2e_test_studio.tools import ExternalToolRegistry


class _NoBrowser:
    def action_specs(self) -> tuple[object, ...]:
        return ()


def test_registered_spreadsheet_action_extracts_matching_cells(
    tmp_path: Path,
) -> None:
    workbook = tmp_path / "Devices.xlsx"
    _write_workbook(
        workbook,
        (
            ("Device", "Bulk installation link"),
            ("MFC-L9570CDW", "https://install.example/device/289886"),
        ),
    )
    tools = ExternalToolRegistry(tmp_path, tmp_path / ".tools")
    registry = default_capability_catalog().build_action_registry(
        CapabilityContext(device=_NoBrowser(), tools=tools)
    )

    result = registry.execute(
        Action(
            name="extract_spreadsheet",
            channel=ActionChannel.EXTERNAL,
            arguments={
                "file_path": str(workbook),
                "query": "Extract the bulk installation link",
            },
        )
    )

    assert result.status is ActionStatus.SUCCESS
    assert result.output["matches"] == [
        {
            "sheet": "Devices",
            "cell": "B2",
            "value": "https://install.example/device/289886",
            "matched_by": "header:B1",
        }
    ]


def test_spreadsheet_variable_extraction_requires_one_value(
    tmp_path: Path,
) -> None:
    workbook = tmp_path / "Devices.xlsx"
    _write_workbook(
        workbook,
        (
            ("Device", "Bulk installation link"),
            ("MFC-L9570CDW", "https://install.example/device/289886"),
        ),
    )
    registry = default_capability_catalog().build_action_registry(
        CapabilityContext(
            device=_NoBrowser(),
            tools=ExternalToolRegistry(tmp_path, tmp_path / ".tools"),
        )
    )

    result = registry.execute(
        Action(
            "extract_spreadsheet",
            ActionChannel.EXTERNAL,
            {
                "file_path": str(workbook),
                "query": "Bulk installation link",
                "sheet": "Devices",
                "cell": "B2",
                "variable_name": "bulk_link",
            },
        )
    )

    assert result.status is ActionStatus.SUCCESS
    assert result.output["value"] == "https://install.example/device/289886"


def test_spreadsheet_plan_requires_file_path_and_query(tmp_path: Path) -> None:
    tools = ExternalToolRegistry(tmp_path, tmp_path / ".tools")
    registry = default_capability_catalog().build_action_registry(
        CapabilityContext(device=_NoBrowser(), tools=tools)
    )

    with pytest.raises(
        ValueError,
        match="missing arguments: file_path, query",
    ):
        PlanCompiler(registry).compile(
            PlanningResult.actions(
                (
                    PlannedAction(
                        Action(
                            name="extract_spreadsheet",
                            channel=ActionChannel.EXTERNAL,
                        ),
                        intent="Read the downloaded workbook",
                        expected_result="Return the installation link",
                    ),
                )
            )
        )


def test_empty_spreadsheet_extraction_is_not_verified() -> None:
    verification = DeterministicVerifier().verify(
        Action(
            name="extract_spreadsheet",
            channel=ActionChannel.EXTERNAL,
            arguments={"file_path": "Devices.xlsx", "query": "missing"},
        ),
        ActionResult.succeeded({"matches": [], "match_count": 0}),
        Observation(),
        Observation(),
        "Return the requested spreadsheet value",
    )

    assert verification.status is VerificationStatus.REJECTED
    assert verification.reason == "The spreadsheet query returned no matches"


def test_external_tool_empty_output_is_not_verified() -> None:
    verification = DeterministicVerifier().verify(
        Action(
            name="run_external_tool",
            channel=ActionChannel.EXTERNAL,
            tool_ref="echo_tool",
        ),
        ActionResult.succeeded({"stdout": "", "stderr": "", "returncode": 0}),
        Observation(),
        Observation(),
        "Return extracted workbook content",
    )

    assert verification.status is VerificationStatus.REJECTED
    assert verification.reason == "The external tool returned no output"


def test_spreadsheet_action_cannot_read_outside_workspace(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    workbook = tmp_path / "outside.xlsx"
    _write_workbook(workbook, (("Secret",),))
    registry = default_capability_catalog().build_action_registry(
        CapabilityContext(
            device=_NoBrowser(),
            tools=ExternalToolRegistry(workspace, workspace / ".tools"),
        )
    )

    result = registry.execute(
        Action(
            name="extract_spreadsheet",
            channel=ActionChannel.EXTERNAL,
            arguments={"file_path": str(workbook), "query": "Secret"},
        )
    )

    assert result.status is ActionStatus.FAILED
    assert "outside the allowed workspace" in result.error


def _write_workbook(
    path: Path,
    rows: tuple[tuple[str, ...], ...],
) -> None:
    cells = "".join(
        f'<c r="{chr(65 + column)}{row}" t="inlineStr"><is><t>{value}</t></is></c>'
        for row, values in enumerate(rows, start=1)
        for column, value in enumerate(values)
    )
    with ZipFile(path, "w", ZIP_DEFLATED) as archive:
        archive.writestr(
            "xl/workbook.xml",
            (
                '<?xml version="1.0" encoding="UTF-8"?>'
                '<workbook xmlns="http://schemas.openxmlformats.org/'
                'spreadsheetml/2006/main" '
                'xmlns:r="http://schemas.openxmlformats.org/'
                'officeDocument/2006/relationships">'
                '<sheets><sheet name="Devices" sheetId="1" '
                'r:id="rId1"/></sheets></workbook>'
            ),
        )
        archive.writestr(
            "xl/_rels/workbook.xml.rels",
            (
                '<?xml version="1.0" encoding="UTF-8"?>'
                '<Relationships xmlns="http://schemas.openxmlformats.org/'
                'package/2006/relationships">'
                '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/'
                'officeDocument/2006/relationships/worksheet" '
                'Target="worksheets/sheet1.xml"/></Relationships>'
            ),
        )
        archive.writestr(
            "xl/worksheets/sheet1.xml",
            (
                '<?xml version="1.0" encoding="UTF-8"?>'
                '<worksheet xmlns="http://schemas.openxmlformats.org/'
                'spreadsheetml/2006/main"><sheetData><row>'
                f"{cells}</row></sheetData></worksheet>"
            ),
        )
