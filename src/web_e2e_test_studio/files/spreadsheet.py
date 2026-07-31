"""Read-only extraction from Office Open XML spreadsheets."""

from __future__ import annotations

import re
from pathlib import Path, PurePosixPath
from xml.etree import ElementTree
from zipfile import BadZipFile, ZipFile

from web_e2e_test_studio.agent import ActionSpec
from web_e2e_test_studio.domain import (
    Action,
    ActionChannel,
    ActionResult,
    JsonValue,
)

_MAIN_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_PACKAGE_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
_CELL_REFERENCE = re.compile(r"^([A-Z]+)([1-9][0-9]*)$")
_QUERY_TOKEN = re.compile(r"[\w-]+", re.UNICODE)
_SUPPORTED_SUFFIXES = frozenset({".xlsx", ".xlsm"})
_MAX_FILE_BYTES = 50 * 1024 * 1024
_MAX_UNCOMPRESSED_BYTES = 200 * 1024 * 1024
_MAX_ENTRY_BYTES = 100 * 1024 * 1024
_MAX_COMPRESSION_RATIO = 200
_MAX_MATCHES = 100


def spreadsheet_action_spec() -> ActionSpec:
    return ActionSpec(
        name="extract_spreadsheet",
        channel=ActionChannel.EXTERNAL,
        description=(
            "Read a local .xlsx/.xlsm file and extract values related to a "
            "query. Pass the exact downloaded file path from action output or "
            "memory. Use sheet to restrict extraction to one worksheet."
        ),
        required_arguments=("file_path", "query"),
        argument_schema={
            "file_path": "string",
            "query": "string",
            "sheet": "string",
        },
    )


class SpreadsheetExtractor:
    """Extract query matches without launching Excel or executing macros."""

    def __init__(self, allowed_roots: tuple[Path, ...] | None = None) -> None:
        roots = allowed_roots or (Path.cwd(),)
        self._allowed_roots = tuple(root.resolve() for root in roots)

    def execute(self, action: Action) -> ActionResult:
        file_path = _required_text(action, "file_path")
        query = _required_text(action, "query")
        requested_sheet = _optional_text(action, "sheet")
        path = Path(file_path).expanduser().resolve()
        if not any(_is_relative_to(path, root) for root in self._allowed_roots):
            return ActionResult.failed(
                f"Spreadsheet is outside the allowed workspace: {path}"
            )
        if path.suffix.lower() not in _SUPPORTED_SUFFIXES:
            return ActionResult.failed(
                "extract_spreadsheet supports only .xlsx and .xlsm files"
            )
        if not path.is_file():
            return ActionResult.failed(f"Spreadsheet file not found: {path}")
        if path.stat().st_size > _MAX_FILE_BYTES:
            return ActionResult.failed("Spreadsheet exceeds the 50 MiB limit")
        try:
            matches = _extract_matches(path, query, requested_sheet)
        except (BadZipFile, ElementTree.ParseError, KeyError, OSError) as exc:
            return ActionResult.failed(f"Could not read spreadsheet: {exc}")
        return ActionResult.succeeded(
            {
                "file_path": str(path),
                "query": query,
                "matches": matches,
                "match_count": len(matches),
            }
        )


def _extract_matches(
    path: Path,
    query: str,
    requested_sheet: str,
) -> list[JsonValue]:
    with ZipFile(path) as archive:
        _validate_archive(archive)
        shared_strings = _shared_strings(archive)
        sheets = _worksheet_paths(archive)
        if requested_sheet:
            sheets = [
                item
                for item in sheets
                if item[0].casefold() == requested_sheet.casefold()
            ]
            if not sheets:
                raise KeyError(f"Worksheet not found: {requested_sheet}")
        matches: list[JsonValue] = []
        for sheet_name, worksheet_path in sheets:
            cells = _worksheet_cells(
                archive.read(worksheet_path), shared_strings
            )
            matches.extend(_query_matches(sheet_name, cells, query))
            if len(matches) >= _MAX_MATCHES:
                break
        return matches[:_MAX_MATCHES]


def _validate_archive(archive: ZipFile) -> None:
    total_uncompressed = 0
    for item in archive.infolist():
        if item.file_size > _MAX_ENTRY_BYTES:
            raise ValueError(
                f"Spreadsheet entry is too large: {item.filename}"
            )
        total_uncompressed += item.file_size
        if total_uncompressed > _MAX_UNCOMPRESSED_BYTES:
            raise ValueError("Spreadsheet uncompressed content is too large")
        if (
            item.compress_size > 0
            and item.file_size / item.compress_size > _MAX_COMPRESSION_RATIO
        ):
            raise ValueError(
                f"Spreadsheet entry has an unsafe compression ratio: "
                f"{item.filename}"
            )


def _shared_strings(archive: ZipFile) -> list[str]:
    try:
        root = ElementTree.fromstring(archive.read("xl/sharedStrings.xml"))
    except KeyError:
        return []
    return [
        "".join(node.text or "" for node in item.iter(f"{{{_MAIN_NS}}}t"))
        for item in root.findall(f"{{{_MAIN_NS}}}si")
    ]


def _worksheet_paths(archive: ZipFile) -> list[tuple[str, str]]:
    workbook = ElementTree.fromstring(archive.read("xl/workbook.xml"))
    relationships = ElementTree.fromstring(
        archive.read("xl/_rels/workbook.xml.rels")
    )
    targets = {
        item.attrib["Id"]: item.attrib["Target"]
        for item in relationships.findall(f"{{{_PACKAGE_REL_NS}}}Relationship")
    }
    result: list[tuple[str, str]] = []
    sheets = workbook.find(f"{{{_MAIN_NS}}}sheets")
    if sheets is None:
        return result
    for sheet in sheets:
        relationship_id = sheet.attrib[f"{{{_REL_NS}}}id"]
        target = PurePosixPath(targets[relationship_id])
        worksheet_path = (
            target.as_posix()
            if target.is_absolute()
            else (PurePosixPath("xl") / target).as_posix()
        ).lstrip("/")
        result.append((sheet.attrib["name"], worksheet_path))
    return result


def _worksheet_cells(
    content: bytes,
    shared_strings: list[str],
) -> dict[str, str]:
    root = ElementTree.fromstring(content)
    cells: dict[str, str] = {}
    for cell in root.iter(f"{{{_MAIN_NS}}}c"):
        reference = cell.attrib.get("r", "")
        value = ""
        if cell.attrib.get("t") == "inlineStr":
            value = "".join(
                node.text or "" for node in cell.iter(f"{{{_MAIN_NS}}}t")
            )
        else:
            value_node = cell.find(f"{{{_MAIN_NS}}}v")
            if value_node is not None:
                value = value_node.text or ""
                if cell.attrib.get("t") == "s":
                    value = shared_strings[int(value)]
        if reference and value:
            cells[reference] = value
    return cells


def _query_matches(
    sheet: str,
    cells: dict[str, str],
    query: str,
) -> list[JsonValue]:
    folded_query = query.casefold()
    direct = [
        (reference, value)
        for reference, value in cells.items()
        if _matches_query(value, folded_query)
    ]
    matches: list[JsonValue] = []
    for reference, value in direct:
        column, row = _split_reference(reference)
        values_below = [
            (other_reference, other_value)
            for other_reference, other_value in cells.items()
            if _split_reference(other_reference)[0] == column
            and _split_reference(other_reference)[1] > row
        ]
        if values_below:
            for other_reference, other_value in values_below:
                matches.append(
                    {
                        "sheet": sheet,
                        "cell": other_reference,
                        "value": other_value,
                        "matched_by": f"header:{reference}",
                    }
                )
        else:
            matches.append(
                {"sheet": sheet, "cell": reference, "value": value}
            )
    return matches


def _matches_query(value: str, folded_query: str) -> bool:
    folded_value = value.casefold()
    if folded_query in folded_value or folded_value in folded_query:
        return True
    value_tokens = {
        token
        for token in _QUERY_TOKEN.findall(folded_value)
        if len(token) >= 2
    }
    query_tokens = {
        token
        for token in _QUERY_TOKEN.findall(folded_query)
        if len(token) >= 2
    }
    return bool(value_tokens) and value_tokens <= query_tokens


def _split_reference(reference: str) -> tuple[str, int]:
    match = _CELL_REFERENCE.fullmatch(reference)
    if match is None:
        raise ValueError(f"Invalid spreadsheet cell reference: {reference}")
    return match.group(1), int(match.group(2))


def _required_text(action: Action, key: str) -> str:
    value = action.arguments.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"extract_spreadsheet requires non-empty {key}")
    return value.strip()


def _optional_text(action: Action, key: str) -> str:
    value = action.arguments.get(key, "")
    if not isinstance(value, str):
        raise ValueError(f"extract_spreadsheet {key} must be a string")
    return value.strip()


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


__all__ = ["SpreadsheetExtractor", "spreadsheet_action_spec"]
