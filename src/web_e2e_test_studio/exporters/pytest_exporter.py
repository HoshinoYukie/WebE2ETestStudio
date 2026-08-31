"""Export recorded assets as standalone synchronous Playwright pytest files."""

from __future__ import annotations

import re
from pathlib import Path
from typing import TypeGuard

from web_e2e_test_studio.agent.actions import resolve_wait_milliseconds
from web_e2e_test_studio.api.response_assertions import parse_response_assertions
from web_e2e_test_studio.domain import (
    Action,
    ActionChannel,
    JsonValue,
    Locator,
    RecordedStep,
    TestAsset,
)

_ENV_REFERENCE = re.compile(r"\$\{ENV:([A-Za-z_][A-Za-z0-9_]*)\}")
_VARIABLE_REFERENCE = re.compile(r"\$\{VAR:([A-Za-z_][A-Za-z0-9_]*)\}")
_INPUT_REFERENCE = re.compile(r"\$\{INPUT:([A-Za-z_][A-Za-z0-9_.-]*)\}")

_INPUT_HELPER = '''def _input_value(path: str) -> object:
    current: object = INPUTS
    for segment in path.split("."):
        if isinstance(current, dict) and segment in current:
            current = current[segment]
        elif isinstance(current, list) and segment.isdigit() and int(segment) < len(current):
            current = current[int(segment)]
        else:
            raise KeyError(f"Required runtime input is missing: {path}")
    return current


with Path(os.environ["WEB_E2E_INPUT_PATH"]).open(encoding="utf-8") as _input_file:
    INPUTS = json.load(_input_file)
assert isinstance(INPUTS, dict), "Runtime input JSON must contain an object"'''

_HTTP_HELPER = """_JSON_MISSING = object()


def _json_path_value(payload: object, path: str) -> object:
    current = payload
    for segment in path.split("."):
        if isinstance(current, dict) and segment in current:
            current = current[segment]
        elif isinstance(current, list) and segment.isdigit() and int(segment) < len(current):
            current = current[int(segment)]
        else:
            return _JSON_MISSING
    return current


def _assert_json_response(payload: object, assertions: list[dict[str, object]]) -> None:
    for assertion in assertions:
        path = str(assertion["path"])
        operator = str(assertion["operator"])
        expected = assertion.get("expected")
        actual = _json_path_value(payload, path)
        equal = (
            actual is not _JSON_MISSING
            and type(actual) is type(expected)
            and actual == expected
        )
        if operator == "exists":
            matches = actual is not _JSON_MISSING
        elif operator == "not_exists":
            matches = actual is _JSON_MISSING
        elif operator == "equals":
            matches = equal
        elif operator == "not_equals":
            matches = actual is not _JSON_MISSING and not equal
        elif operator == "contains":
            matches = (
                isinstance(actual, str) and isinstance(expected, str) and expected in actual
            ) or (
                isinstance(actual, list)
                and any(type(item) is type(expected) and item == expected for item in actual)
            ) or (
                isinstance(actual, dict) and isinstance(expected, str) and expected in actual
            )
        else:
            raise AssertionError(f"Unsupported response assertion operator: {operator}")
        actual_text = "<missing>" if actual is _JSON_MISSING else repr(actual)
        assert matches, (
            f"JSON response assertion failed at {path!r}: "
            f"{operator} {expected!r}; actual={actual_text}"
        )


def _http_request(
    *,
    method: str,
    url: str,
    headers: dict[str, str],
    json_body: object,
    body: str | None,
    expected_status: int,
    timeout: float,
    response_assertions: list[dict[str, object]],
) -> bytes:
    request_headers = dict(headers)
    data = body.encode("utf-8") if body is not None else None
    if json_body is not None:
        request_headers.setdefault("Content-Type", "application/json")
        data = json.dumps(json_body, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url=url,
        data=data,
        headers=request_headers,
        method=method.upper(),
    )
    try:
        response_context = urllib.request.urlopen(request, timeout=timeout)
    except urllib.error.HTTPError as error:
        response_context = error
    with response_context as response:
        payload = response.read()
        assert response.status == expected_status, (
            f"Expected HTTP {expected_status}, received {response.status}: "
            f"{payload.decode('utf-8', errors='replace')}"
        )
        if response_assertions:
            try:
                json_payload = json.loads(payload)
            except (json.JSONDecodeError, UnicodeDecodeError) as error:
                raise AssertionError(
                    "HTTP response assertions require a valid JSON response"
                ) from error
            _assert_json_response(json_payload, response_assertions)
        return payload"""

_DOWNLOAD_HELPER = """def _download(
    page: Page,
    target: object,
    path: str,
    timeout: float,
) -> Path:
    with page.expect_download(timeout=timeout) as download_info:
        target.click()
    download = download_info.value
    destination = (
        Path(path)
        if path
        else Path("downloads") / download.suggested_filename
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    download.save_as(destination)
    return destination"""

_APPLICATION_HELPER = """def _launch_application(
    command: list[str],
    **options: object,
) -> None:
    process = subprocess.Popen(command, **options)
    time.sleep(0.25)
    assert process.poll() is None, (
        f"Application exited during startup with code {process.returncode}"
    )"""

_SPREADSHEET_HELPER = """def _extract_spreadsheet(
    file_path: str,
    query: str,
    sheet: str = "",
) -> list[dict[str, str]]:
    main_ns = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    rel_ns = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    package_ns = "http://schemas.openxmlformats.org/package/2006/relationships"
    path = Path(file_path).expanduser().resolve()
    assert path.is_file(), f"Spreadsheet file not found: {path}"
    with ZipFile(path) as archive:
        try:
            shared_root = ElementTree.fromstring(
                archive.read("xl/sharedStrings.xml")
            )
            shared = [
                "".join(node.text or "" for node in item.iter(f"{{{main_ns}}}t"))
                for item in shared_root.findall(f"{{{main_ns}}}si")
            ]
        except KeyError:
            shared = []
        workbook = ElementTree.fromstring(archive.read("xl/workbook.xml"))
        relationships = ElementTree.fromstring(
            archive.read("xl/_rels/workbook.xml.rels")
        )
        targets = {
            item.attrib["Id"]: item.attrib["Target"]
            for item in relationships.findall(
                f"{{{package_ns}}}Relationship"
            )
        }
        matches: list[dict[str, str]] = []
        for worksheet in workbook.findall(f".//{{{main_ns}}}sheet"):
            sheet_name = worksheet.attrib["name"]
            if sheet and sheet_name.casefold() != sheet.casefold():
                continue
            relation_id = worksheet.attrib[f"{{{rel_ns}}}id"]
            target = targets[relation_id].lstrip("/")
            worksheet_path = (
                target if target.startswith("xl/") else f"xl/{target}"
            )
            root = ElementTree.fromstring(archive.read(worksheet_path))
            cells: dict[str, str] = {}
            for cell in root.iter(f"{{{main_ns}}}c"):
                reference = cell.attrib.get("r", "")
                if cell.attrib.get("t") == "inlineStr":
                    value = "".join(
                        node.text or ""
                        for node in cell.iter(f"{{{main_ns}}}t")
                    )
                else:
                    value_node = cell.find(f"{{{main_ns}}}v")
                    value = value_node.text if value_node is not None else ""
                    if value and cell.attrib.get("t") == "s":
                        value = shared[int(value)]
                if reference and value:
                    cells[reference] = value
            query_tokens = set(query.casefold().replace("-", " ").split())
            for reference, value in cells.items():
                value_tokens = set(value.casefold().replace("-", " ").split())
                if (
                    query.casefold() in value.casefold()
                    or value.casefold() in query.casefold()
                    or (value_tokens and value_tokens <= query_tokens)
                ):
                    column = "".join(filter(str.isalpha, reference))
                    row = int("".join(filter(str.isdigit, reference)))
                    below = [
                        (other_ref, other_value)
                        for other_ref, other_value in cells.items()
                        if "".join(filter(str.isalpha, other_ref)) == column
                        and int("".join(filter(str.isdigit, other_ref))) > row
                    ]
                    values = below or [(reference, value)]
                    matches.extend(
                        {
                            "sheet": sheet_name,
                            "cell": match_ref,
                            "value": match_value,
                        }
                        for match_ref, match_value in values
                    )
        assert matches, f"No spreadsheet values matched query: {query}"
        return matches"""

_STEP_REPORTER = """@contextmanager
def _validation_step(step_id: str, action: str) -> Iterator[None]:
    prefix = "__WEB_E2E_STEP__"
    print(f"{prefix}|running|{step_id}|{action}", flush=True)
    try:
        yield
    except BaseException:
        print(f"{prefix}|failed|{step_id}|{action}", flush=True)
        raise
    else:
        print(f"{prefix}|passed|{step_id}|{action}", flush=True)"""


class PytestExporter:
    """Render model-independent recorded assets as executable pytest source."""

    def render(self, asset: TestAsset) -> str:
        """Return a standalone pytest module for ``asset``."""
        has_browser_steps = any(
            step.action.channel == ActionChannel.BROWSER for step in asset.steps
        )
        has_api_steps = any(step.action.channel == ActionChannel.API for step in asset.steps)
        has_download_steps = any(
            step.action.channel == ActionChannel.BROWSER and step.action.name == "download"
            for step in asset.steps
        )
        has_external_steps = any(
            step.action.channel == ActionChannel.EXTERNAL and step.action.tool_ref is not None
            for step in asset.steps
        )
        has_application_steps = any(
            step.action.name == "launch_application" for step in asset.steps
        )
        has_spreadsheet_steps = any(
            step.action.name == "extract_spreadsheet" for step in asset.steps
        )
        has_output_steps = any(
            step.action.name in {"extract_value", "extract_spreadsheet"}
            and bool(step.action.arguments.get("variable_name"))
            for step in asset.steps
        )
        has_portable_tools = any(
            step.action.channel == ActionChannel.EXTERNAL
            and step.action.tool_ref is not None
            and "command" not in asset.tool_dependencies.get(step.action.tool_ref, {})
            for step in asset.steps
        )
        variable_definitions = _used_variable_definitions(asset)
        has_environment_references = any(
            _contains_environment_reference(step.action.arguments) for step in asset.steps
        )
        has_input_references = any(
            _contains_input_reference(step.action.arguments)
            or _locator_contains_input_reference(step.action.locator)
            for step in asset.steps
        )
        imports: list[str] = []
        if asset.steps:
            imports.append("from contextlib import contextmanager")
            imports.append("from collections.abc import Iterator")
        if has_api_steps or has_input_references or has_output_steps:
            imports.append("import json")
        if has_api_steps:
            imports.extend(["import urllib.error", "import urllib.request"])
        if has_input_references:
            imports.extend(["import os", "from pathlib import Path"])
        if has_external_steps:
            imports.extend(["import os", "import subprocess"])
        elif has_environment_references:
            imports.append("import os")
        if has_application_steps:
            imports.append("import time")
        if variable_definitions:
            imports.extend(
                [
                    "import random",
                    "import string",
                    "import uuid",
                    "from datetime import UTC, datetime",
                ]
            )
        if has_spreadsheet_steps:
            imports.extend(
                [
                    "from pathlib import Path",
                    "from xml.etree import ElementTree",
                    "from zipfile import ZipFile",
                ]
            )
        if has_portable_tools:
            imports.extend(["import sys", "from pathlib import Path"])
        elif has_download_steps:
            imports.append("from pathlib import Path")
        imports.extend(["", "import pytest"])
        if has_browser_steps:
            imports.append("from playwright.sync_api import Page, sync_playwright, expect")
        fixture = self._render_page_fixture(asset) if has_browser_steps else ""
        body = [self._render_step(asset, step) for step in asset.steps]
        if any(
            step.action.name in {"bind_page", "switch_page", "close_page", "open_popup"}
            for step in asset.steps
        ):
            body.insert(0, "pages: dict[str, Page] = {'main': page}")
        if not body:
            body.append("pass")
        test_lines = [
            (
                f"def {_test_name(asset.name, asset.id)}"
                f"({'page: Page' if has_browser_steps else ''}) -> None:"
            ),
            *[
                rendered_line
                for action_source in body
                for rendered_line in _indent_source(action_source)
            ],
        ]

        sections = [
            '"""Generated deterministic Playwright test."""',
            "\n".join(imports),
        ]
        if has_portable_tools:
            sections.append(
                "PROJECT_ROOT = Path(\n"
                '    os.environ.get("WEB_E2E_PROJECT_ROOT", Path.cwd())\n'
                ").resolve()"
            )
        if variable_definitions:
            sections.append(_render_generated_variables(variable_definitions))
        if has_input_references:
            sections.append(_INPUT_HELPER)
        if has_api_steps:
            sections.append(_HTTP_HELPER)
        if has_download_steps:
            sections.append(_DOWNLOAD_HELPER)
        if has_application_steps:
            sections.append(_APPLICATION_HELPER)
        if has_spreadsheet_steps:
            sections.append(_SPREADSHEET_HELPER)
        if asset.steps:
            sections.append(_STEP_REPORTER)
        if fixture:
            sections.append(fixture)
        sections.append("\n".join(test_lines))
        return "\n\n".join(sections) + "\n"

    def _render_step(self, asset: TestAsset, step: RecordedStep) -> str:
        action_source = self._render_action(asset, step.action)
        nested = "\n".join(f"    {line}" if line else "" for line in action_source.splitlines())
        return f"with _validation_step({step.id!r}, {step.action.name!r}):\n{nested}"

    def export(self, asset: TestAsset, path: str | Path) -> Path:
        """Write the rendered pytest module to ``path`` and return that path."""
        destination = Path(path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(self.render(asset), encoding="utf-8")
        return destination

    def _render_page_fixture(self, asset: TestAsset) -> str:
        context_arguments = f"base_url={asset.base_url!r}" if asset.base_url else ""
        return "\n".join(
            [
                "@pytest.fixture()",
                "def page() -> Iterator[Page]:",
                "    with sync_playwright() as playwright:",
                "        browser = playwright.chromium.launch()",
                f"        context = browser.new_context({context_arguments})",
                "        test_page = context.new_page()",
                "        yield test_page",
                "        context.close()",
                "        browser.close()",
            ]
        )

    def _render_action(self, asset: TestAsset, action: Action) -> str:
        if action.channel == ActionChannel.EXTERNAL:
            if action.name == "extract_spreadsheet":
                file_path = _string_argument(action, "file_path")
                query = _string_argument(action, "query")
                sheet = _string_argument(action, "sheet", default="")
                variable_name = _string_argument(action, "variable_name", default="")
                cell = _string_argument(action, "cell", default="")
                if variable_name:
                    result_name = f"_matches_{variable_name}"
                    selection = (
                        f"[item for item in {result_name} "
                        f"if item['cell'].upper() == {cell.upper()!r}]"
                        if cell
                        else result_name
                    )
                    return (
                        f"{result_name} = _extract_spreadsheet("
                        f"{file_path!r}, {query!r}, {sheet!r})\n"
                        f"{result_name} = {selection}\n"
                        f"assert len({result_name}) == 1, "
                        f"'Variable extraction requires exactly one value'\n"
                        f"VARIABLES[{variable_name!r}] = "
                        f"{result_name}[0]['value']\n"
                        f"{_output_statement(variable_name)}"
                    )
                return f"_extract_spreadsheet({file_path!r}, {query!r}, {sheet!r})"
            return self._render_external_action(asset, action)
        if action.channel == ActionChannel.API:
            return self._render_api_action(action)
        if action.channel != ActionChannel.BROWSER:
            raise ValueError(f"Unsupported action channel: {action.channel!r}")

        name = action.name
        if name == "navigate":
            url = _string_argument(action, "url", default=asset.base_url)
            if not url:
                raise ValueError("navigate requires a non-empty url or asset base_url")
            return f"page.goto({_string_expression(url)})"
        if name == "wait":
            return self._render_wait(action)
        if name == "screenshot":
            return self._render_screenshot(action)
        if name == "bind_page":
            page_ref = _string_argument(action, "page_ref")
            return f"pages[{page_ref!r}] = page"
        if name == "switch_page":
            page_ref = _string_argument(action, "page_ref")
            return f"page = pages[{page_ref!r}]\npage.bring_to_front()"
        if name == "close_page":
            page_ref = _string_argument(action, "page_ref")
            return f"pages[{page_ref!r}].close()\npages.pop({page_ref!r})"
        if name == "open_popup":
            page_ref = _string_argument(action, "page_ref")
            target = _target_expression(action)
            timeout = _optional_number_argument(action, "timeout")
            timeout_option = f"timeout={_number_literal(timeout)}" if timeout is not None else ""
            return (
                f"with page.expect_popup({timeout_option}) as popup_info:\n"
                f"    {target}.click({timeout_option})\n"
                "page = popup_info.value\n"
                f"pages[{page_ref!r}] = page\n"
                "page.wait_for_load_state()"
            )

        target = _target_expression(action)
        operation_options = _browser_timeout_options(action)
        if name == "extract_value":
            variable_name = _string_argument(action, "variable_name")
            read = _string_argument(action, "read", default="text")
            if read == "text":
                expression = f"{target}.inner_text({operation_options})"
            elif read == "value":
                expression = f"{target}.input_value({operation_options})"
            elif read == "attribute":
                attribute = _string_argument(action, "attribute")
                expression = f"{target}.get_attribute({attribute!r}, {operation_options})"
            else:
                raise ValueError(f"Unsupported extraction read mode: {read}")
            return (
                f"VARIABLES[{variable_name!r}] = {expression}\n"
                f"{_output_statement(variable_name)}"
            )
        if name == "click":
            return f"{target}.click({operation_options})"
        if name == "download":
            path = _string_argument(action, "path", default="")
            timeout = _optional_number_argument(action, "timeout")
            if timeout is None and action.timeout_seconds is not None:
                timeout = action.timeout_seconds * 1000
            return (
                f"_download(page, {target}, {path!r}, "
                f"{_number_literal(timeout) if timeout is not None else '30000'})"
            )
        if name == "fill":
            text = _string_argument(action, "text", aliases=("value",))
            suffix = f", {operation_options}" if operation_options else ""
            return f"{target}.fill({_string_expression(text)}{suffix})"
        if name == "press":
            key = _string_argument(action, "key")
            suffix = f", {operation_options}" if operation_options else ""
            return f"{target}.press({_string_expression(key)}{suffix})"
        if name == "select":
            option = _select_option_expression(action)
            suffix = f", {operation_options}" if operation_options else ""
            return f"{target}.select_option({option}{suffix})"
        if name == "check":
            checked = action.arguments.get("checked", True)
            if not isinstance(checked, bool):
                raise ValueError("check argument 'checked' must be a boolean")
            method = "check" if checked else "uncheck"
            return f"{target}.{method}({operation_options})"
        if name == "hover":
            return f"{target}.hover({operation_options})"
        if name == "assert_text":
            expected = _string_argument(
                action,
                "text",
                aliases=("expected", "value"),
            )
            timeout = _optional_number_argument(action, "timeout")
            if timeout is None and action.timeout_seconds is not None:
                timeout = action.timeout_seconds * 1000
            suffix = f", timeout={_number_literal(timeout)}" if timeout is not None else ""
            return (
                f"expect({target}).to_contain_text("
                f"{_string_expression(expected)}{suffix})"
            )
        if name == "assert_visible":
            timeout = _optional_number_argument(action, "timeout")
            if timeout is None and action.timeout_seconds is not None:
                timeout = action.timeout_seconds * 1000
            arguments = f"timeout={_number_literal(timeout)}" if timeout is not None else ""
            return f"expect({target}).to_be_visible({arguments})"
        raise ValueError(f"Unsupported browser action: {name!r}")

    def _render_api_action(self, action: Action) -> str:
        if action.name != "http_request":
            raise ValueError(f"Unsupported API action: {action.name!r}")
        method = _string_argument(action, "method")
        url = _string_argument(action, "url")
        headers = action.arguments.get("headers", {})
        if not isinstance(headers, dict):
            raise ValueError("http_request headers must be an object")
        body = action.arguments.get("body")
        if body is not None and not isinstance(body, str):
            raise ValueError("http_request body must be a string")
        expected_status = action.arguments.get("expected_status", 200)
        if isinstance(expected_status, bool) or not isinstance(expected_status, int):
            raise ValueError("http_request expected_status must be an integer")
        timeout = _optional_number_argument(action, "timeout")
        if timeout is None:
            timeout = action.timeout_seconds
        response_assertions = action.arguments.get("response_assertions", [])
        parse_response_assertions(response_assertions)
        return (
            "_http_request("
            f"method={_string_expression(method)}, "
            f"url={_string_expression(url)}, "
            f"headers={_value_expression(headers)}, "
            f"json_body={_value_expression(action.arguments.get('json'))}, "
            f"body={_value_expression(body)}, "
            f"expected_status={expected_status!r}, "
            f"timeout={_number_literal(timeout) if timeout is not None else '30'}"
            f", response_assertions={_value_expression(response_assertions)}"
            ")"
        )

    def _render_wait(self, action: Action) -> str:
        duration = resolve_wait_milliseconds(action)
        if action.locator is not None:
            target = _target_expression(action)
            keyword_arguments: list[str] = []
            state = action.arguments.get("state")
            if state is not None:
                if not isinstance(state, str):
                    raise ValueError("wait argument 'state' must be a string")
                keyword_arguments.append(f"state={state!r}")
            keyword_arguments.append(f"timeout={_number_literal(duration)}")
            return f"{target}.wait_for({', '.join(keyword_arguments)})"

        return f"page.wait_for_timeout({_number_literal(duration)})"

    def _render_screenshot(self, action: Action) -> str:
        arguments: list[str] = []
        path = action.arguments.get("path")
        if path is not None:
            if not isinstance(path, str):
                raise ValueError("screenshot argument 'path' must be a string")
            arguments.append(f"path={path!r}")
        full_page = action.arguments.get("full_page")
        if full_page is not None:
            if not isinstance(full_page, bool):
                raise ValueError("screenshot argument 'full_page' must be a boolean")
            if action.locator is not None:
                raise ValueError("full_page is only supported for page screenshots")
            arguments.append(f"full_page={full_page!r}")
        target = _target_expression(action) if action.locator is not None else "page"
        return f"{target}.screenshot({', '.join(arguments)})"

    def _render_external_action(
        self,
        asset: TestAsset,
        action: Action,
    ) -> str:
        if not action.tool_ref:
            raise ValueError("External actions require tool_ref")
        try:
            dependency = asset.tool_dependencies[action.tool_ref]
        except KeyError as error:
            raise ValueError(f"Unknown external tool dependency: {action.tool_ref!r}") from error

        command = dependency.get("command")
        if command is not None:
            command_parts = [
                _string_expression(part)
                for part in _string_list(
                    command,
                    f"Tool dependency {action.tool_ref!r} command",
                    non_empty=True,
                )
            ]
            default_environment = dependency.get("env", {})
            default_cwd = dependency.get("cwd")
        else:
            command_parts = _portable_command_parts(
                dependency,
                action.tool_ref,
            )
            default_environment = dependency.get("default_env", {})
            default_cwd = _portable_cwd_expression(dependency, action)

        command_parts.extend(
            _string_expression(part)
            for part in _string_list(
                action.arguments.get("args", []),
                "External action args",
            )
        )
        command_expression = "[" + ", ".join(command_parts) + "]"
        keyword_arguments = [command_expression]

        action_cwd = action.arguments.get("cwd")
        if command is not None and action_cwd is not None:
            default_cwd = action_cwd
        if default_cwd is not None:
            cwd_expression = (
                default_cwd
                if command is None
                else _string_expression(_required_string(default_cwd, "External tool cwd"))
            )
            keyword_arguments.append(f"cwd={cwd_expression}")

        environment = {
            **_string_mapping(default_environment, "External tool environment"),
            **_string_mapping(
                action.arguments.get("env", {}),
                "External action environment",
            ),
        }
        if environment:
            environment_items = ", ".join(
                f"{key!r}: {_string_expression(value)}" for key, value in environment.items()
            )
            environment_expression = (
                "{**os.environ" + (f", {environment_items}" if environment_items else "") + "}"
            )
            keyword_arguments.append(f"env={environment_expression}")

        if action.name == "launch_application":
            keyword_arguments.extend(
                [
                    "stdin=subprocess.DEVNULL",
                    "stdout=subprocess.DEVNULL",
                    "stderr=subprocess.DEVNULL",
                    "shell=False",
                ]
            )
            continuation = ",\n        ".join(keyword_arguments)
            return f"_launch_application(\n        {continuation},\n    )"

        timeout = action.arguments.get(
            "timeout",
            action.timeout_seconds
            if action.timeout_seconds is not None
            else dependency.get("timeout"),
        )
        if timeout is not None:
            if not _is_number(timeout):
                raise ValueError(f"Tool dependency {action.tool_ref!r} timeout must be numeric")
            keyword_arguments.append(f"timeout={_number_literal(timeout)}")
        keyword_arguments.append("check=True")

        continuation = ",\n        ".join(keyword_arguments)
        return f"subprocess.run(\n        {continuation},\n    )"


def _portable_command_parts(
    dependency: dict[str, JsonValue],
    tool_ref: str,
) -> list[str]:
    kind = _required_string(
        dependency.get("kind"),
        f"Tool dependency {tool_ref!r} kind",
    ).lower()
    root = _required_string(
        dependency.get("root"),
        f"Tool dependency {tool_ref!r} root",
    )
    entry = _required_string(
        dependency.get("entry"),
        f"Tool dependency {tool_ref!r} entry",
    )
    entry_expression = _joined_path_expression(root, entry)
    if kind == "python":
        parts = ["sys.executable", entry_expression]
    elif kind == "powershell":
        parts = [
            repr("powershell.exe"),
            repr("-NoProfile"),
            repr("-ExecutionPolicy"),
            repr("Bypass"),
            repr("-File"),
            entry_expression,
        ]
    elif kind == "node":
        parts = [repr("node"), entry_expression]
    elif kind == "exe":
        parts = [entry_expression]
    else:
        raise ValueError(f"Tool dependency {tool_ref!r} has unsupported kind {kind!r}")
    parts.extend(
        _string_expression(part)
        for part in _string_list(
            dependency.get("default_args", []),
            f"Tool dependency {tool_ref!r} default_args",
        )
    )
    return parts


def _indent_source(source: str) -> list[str]:
    return [f"    {line}" if line else "" for line in source.splitlines()]


def _browser_timeout_options(action: Action) -> str:
    timeout = _optional_number_argument(action, "timeout")
    if timeout is None and action.timeout_seconds is not None:
        timeout = action.timeout_seconds * 1000
    return f"timeout={_number_literal(timeout)}" if timeout is not None else ""


def _portable_cwd_expression(
    dependency: dict[str, JsonValue],
    action: Action,
) -> str:
    root = _required_string(dependency.get("root"), "External tool root")
    raw_cwd = action.arguments.get("cwd")
    cwd = _required_string(raw_cwd, "External action cwd") if raw_cwd is not None else ""
    return _joined_path_expression(root, cwd)


def _joined_path_expression(root: str, child: str) -> str:
    root_path = Path(root)
    child_path = Path(child)
    if child_path.is_absolute() or ".." in child_path.parts:
        raise ValueError("External tool path escapes its declared root")

    if root_path.is_absolute():
        expression = f"Path({root_path.as_posix()!r})"
    else:
        if ".." in root_path.parts:
            raise ValueError("External tool root escapes the project root")
        expression = "PROJECT_ROOT"
        for part in root_path.parts:
            if part not in {"", "."}:
                expression += f" / {part!r}"
    for part in child_path.parts:
        if part not in {"", "."}:
            expression += f" / {part!r}"
    return expression


def _string_list(
    value: JsonValue,
    label: str,
    *,
    non_empty: bool = False,
) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(part, str) for part in value):
        raise ValueError(f"{label} must be a list of strings")
    if non_empty and not value:
        raise ValueError(f"{label} must not be empty")
    return [str(part) for part in value]


def _string_mapping(value: JsonValue, label: str) -> dict[str, str]:
    if not isinstance(value, dict) or not all(
        isinstance(key, str) and isinstance(item, str) for key, item in value.items()
    ):
        raise ValueError(f"{label} must map strings to strings")
    return {str(key): str(item) for key, item in value.items()}


def _required_string(value: JsonValue, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} must be a non-empty string")
    return value


def _test_name(name: str, asset_id: str) -> str:
    stem = re.sub(r"\W+", "_", name, flags=re.UNICODE).strip("_").casefold()
    if not stem:
        stem = (
            re.sub(
                r"\W+",
                "_",
                asset_id,
                flags=re.UNICODE,
            )
            .strip("_")
            .casefold()
        )
    return f"test_{stem or 'recorded_flow'}"


def _output_statement(variable_name: str) -> str:
    prefix = f"__WEB_E2E_OUTPUT__|{variable_name}|"
    return (
        f"print({prefix!r} + json.dumps(VARIABLES[{variable_name!r}], "
        "ensure_ascii=False), flush=True)"
    )


def _required_locator(action: Action) -> Locator:
    if action.locator is None:
        raise ValueError(f"Browser action {action.name!r} requires a locator")
    return action.locator


def _target_expression(action: Action) -> str:
    locator = _required_locator(action)
    base = "page"
    if action.target is not None:
        for frame in action.target.frame_path:
            if frame.selector:
                base = f"{base}.frame_locator({frame.selector!r})"
                if frame.index is not None:
                    base = f"{base}.nth({frame.index})"
            elif frame.name:
                frame_selector = f"iframe[name={frame.name!r}],frame[name={frame.name!r}]"
                base = f"{base}.frame_locator({frame_selector!r})"
            elif frame.index is not None:
                base = f"{base}.frame_locator('iframe,frame').nth({frame.index})"
            else:
                raise ValueError("Frame target requires selector, name, or index")
    return _locator_expression(locator, base=base)


def _locator_expression(locator: Locator, *, base: str = "page") -> str:
    strategy = locator.strategy.casefold().replace("-", "_")
    if strategy == "role":
        arguments = [_string_expression(locator.value)]
        if locator.name is not None:
            arguments.append(f"name={_string_expression(locator.name)}")
        arguments.append(f"exact={locator.exact!r}")
        expression = f"{base}.get_by_role({', '.join(arguments)})"
    elif strategy == "label":
        expression = (
            f"{base}.get_by_label({_string_expression(locator.value)}, "
            f"exact={locator.exact!r})"
        )
    elif strategy == "text":
        expression = (
            f"{base}.get_by_text({_string_expression(locator.value)}, "
            f"exact={locator.exact!r})"
        )
    elif strategy == "placeholder":
        expression = (
            f"{base}.get_by_placeholder({_string_expression(locator.value)}, "
            f"exact={locator.exact!r})"
        )
    elif strategy in {"test_id", "testid"}:
        expression = f"{base}.get_by_test_id({_string_expression(locator.value)})"
    elif strategy in {"css", "xpath", "selector"}:
        expression = f"{base}.locator({_string_expression(locator.value)})"
    elif strategy in {"alt_text", "alt"}:
        expression = (
            f"{base}.get_by_alt_text({_string_expression(locator.value)}, "
            f"exact={locator.exact!r})"
        )
    elif strategy == "title":
        expression = (
            f"{base}.get_by_title({_string_expression(locator.value)}, "
            f"exact={locator.exact!r})"
        )
    else:
        raise ValueError(f"Unsupported locator strategy: {locator.strategy!r}")

    if locator.fallbacks:
        alternatives = [
            _locator_expression(fallback, base=base) for fallback in locator.fallbacks
        ]
        expression = (
            "("
            + expression
            + "".join(f".or_({alternative})" for alternative in alternatives)
            + ").first"
        )
    if locator.nth is not None:
        expression = f"{expression}.nth({locator.nth})"
    return expression


def _string_argument(
    action: Action,
    key: str,
    *,
    aliases: tuple[str, ...] = (),
    default: str | None = None,
) -> str:
    for candidate in (key, *aliases):
        value = action.arguments.get(candidate)
        if value is not None:
            if not isinstance(value, str):
                raise ValueError(f"{action.name} argument {candidate!r} must be a string")
            return value
    if default is not None:
        return default
    raise ValueError(f"{action.name} requires string argument {key!r}")


def _select_option_expression(action: Action) -> str:
    if "values" in action.arguments:
        values = action.arguments["values"]
        if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
            raise ValueError("select argument 'values' must be a list of strings")
        return "[" + ", ".join(_string_expression(value) for value in values) + "]"
    for key in ("value", "label", "index"):
        if key not in action.arguments:
            continue
        value = action.arguments[key]
        if key == "index":
            if not isinstance(value, int) or isinstance(value, bool):
                raise ValueError("select argument 'index' must be an integer")
        elif not isinstance(value, str):
            raise ValueError(f"select argument {key!r} must be a string")
        if key == "value":
            return _string_expression(value)
        if key == "label":
            return f"label={_string_expression(value)}"
        return f"index={value!r}"
    raise ValueError("select requires 'value', 'values', 'label', or 'index'")


def _optional_number_argument(action: Action, key: str) -> int | float | None:
    value = action.arguments.get(key)
    if value is None:
        return None
    if not _is_number(value):
        raise ValueError(f"{action.name} argument {key!r} must be numeric")
    return value


def _is_number(value: object) -> TypeGuard[int | float]:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _number_literal(value: int | float) -> str:
    return repr(value)


def _string_expression(value: JsonValue) -> str:
    if not isinstance(value, str):
        raise TypeError("Environment-aware values must be strings")
    matches = sorted(
        [
            *_ENV_REFERENCE.finditer(value),
            *_VARIABLE_REFERENCE.finditer(value),
            *_INPUT_REFERENCE.finditer(value),
        ],
        key=lambda match: match.start(),
    )
    if not matches:
        return repr(value)
    parts: list[str] = []
    cursor = 0
    for match in matches:
        if match.start() > cursor:
            parts.append(repr(value[cursor : match.start()]))
        if match.re is _ENV_REFERENCE:
            parts.append(f"os.environ[{match.group(1)!r}]")
        elif match.re is _INPUT_REFERENCE:
            parts.append(f"str(_input_value({match.group(1)!r}))")
        else:
            parts.append(f"VARIABLES[{match.group(1)!r}]")
        cursor = match.end()
    if cursor < len(value):
        parts.append(repr(value[cursor:]))
    return " + ".join(parts)


def _value_expression(value: JsonValue) -> str:
    if isinstance(value, str):
        input_match = _INPUT_REFERENCE.fullmatch(value)
        if input_match is not None:
            return f"_input_value({input_match.group(1)!r})"
        return _string_expression(value)
    if isinstance(value, list):
        return "[" + ", ".join(_value_expression(item) for item in value) + "]"
    if isinstance(value, dict):
        return (
            "{"
            + ", ".join(f"{key!r}: {_value_expression(item)}" for key, item in value.items())
            + "}"
        )
    return repr(value)


def _used_variable_definitions(
    asset: TestAsset,
) -> dict[str, dict[str, JsonValue]]:
    used = (
        set().union(*(_referenced_variable_names(step.action.arguments) for step in asset.steps))
        if asset.steps
        else set()
    )
    used.update(
        str(step.action.arguments.get("variable_name", ""))
        for step in asset.steps
        if step.action.name in {"extract_value", "extract_spreadsheet"}
        and step.action.arguments.get("variable_name")
    )
    if not used:
        return {}
    raw_definitions = asset.metadata.get("variable_definitions", [])
    if not isinstance(raw_definitions, list):
        raise ValueError("Asset variable_definitions must be a list")
    definitions = {
        str(item.get("name", "")): dict(item) for item in raw_definitions if isinstance(item, dict)
    }
    missing = used - definitions.keys()
    if missing:
        raise ValueError("Missing generated variable definitions: " + ", ".join(sorted(missing)))
    selected = {name: definitions[name] for name in sorted(used)}
    for definition in selected.values():
        options = definition.get("options")
        if isinstance(options, dict):
            definition["options"] = {
                key: value for key, value in options.items() if key != "captured_value"
            }
    return selected


def _referenced_variable_names(value: JsonValue) -> set[str]:
    if isinstance(value, str):
        return set(_VARIABLE_REFERENCE.findall(value))
    if isinstance(value, list):
        return (
            set().union(*(_referenced_variable_names(item) for item in value)) if value else set()
        )
    if isinstance(value, dict):
        return (
            set().union(*(_referenced_variable_names(item) for item in value.values()))
            if value
            else set()
        )
    return set()


def _contains_environment_reference(value: JsonValue) -> bool:
    if isinstance(value, str):
        return bool(_ENV_REFERENCE.search(value))
    if isinstance(value, list):
        return any(_contains_environment_reference(item) for item in value)
    if isinstance(value, dict):
        return any(_contains_environment_reference(item) for item in value.values())
    return False


def _contains_input_reference(value: JsonValue) -> bool:
    if isinstance(value, str):
        return bool(_INPUT_REFERENCE.search(value))
    if isinstance(value, list):
        return any(_contains_input_reference(item) for item in value)
    if isinstance(value, dict):
        return any(_contains_input_reference(item) for item in value.values())
    return False


def _locator_contains_input_reference(locator: Locator | None) -> bool:
    if locator is None:
        return False
    return (
        bool(_INPUT_REFERENCE.search(locator.value))
        or bool(locator.name and _INPUT_REFERENCE.search(locator.name))
        or any(
            _locator_contains_input_reference(fallback)
            for fallback in locator.fallbacks
        )
    )


def _render_generated_variables(
    definitions: dict[str, dict[str, JsonValue]],
) -> str:
    return f"""_VARIABLE_DEFINITIONS = {definitions!r}


def _generate_variable(definition: dict[str, object]) -> str:
    kind = str(definition["kind"])
    options = dict(definition.get("options", {{}}))
    generator = random.SystemRandom()
    if kind == "uuid":
        return str(uuid.uuid4())
    if kind == "timestamp":
        value = datetime.now(UTC).timestamp()
        return str(int(value * (1000 if options.get("unit") == "milliseconds" else 1)))
    if kind == "datetime":
        return datetime.now(UTC).strftime(str(options.get("format", "%Y-%m-%d")))
    if kind == "integer":
        return str(generator.randint(int(options["minimum"]), int(options["maximum"])))
    if kind in {{"digits", "alphanumeric"}}:
        alphabet = string.digits if kind == "digits" else string.ascii_letters + string.digits
        return str(options.get("prefix", "")) + "".join(
            generator.choice(alphabet) for _ in range(int(options.get("length", 12)))
        )
    if kind == "email":
        local = "".join(
            generator.choice(string.ascii_lowercase + string.digits)
            for _ in range(int(options.get("length", 12)))
        )
        return f"{{local}}@{{options.get('domain', 'example.test')}}"
    raise ValueError(f"Unsupported variable generator kind: {{kind}}")


VARIABLES = {{
    name: _generate_variable(definition)
    for name, definition in _VARIABLE_DEFINITIONS.items()
    if definition["kind"] not in {{"page_value", "spreadsheet_value"}}
}}"""


__all__ = ["PytestExporter"]
