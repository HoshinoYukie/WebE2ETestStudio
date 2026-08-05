"""Export recorded assets as standalone synchronous Playwright pytest files."""

from __future__ import annotations

import re
from pathlib import Path
from typing import TypeGuard

from web_e2e_test_studio.agent.actions import resolve_wait_milliseconds
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

_HTTP_HELPER = """def _http_request(
    *,
    method: str,
    url: str,
    headers: dict[str, str],
    json_body: object,
    body: str | None,
    expected_status: int,
    timeout: float,
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

_STEP_REPORTER = '''@contextmanager
def _validation_step(step_id: str, action: str) -> Iterator[None]:
    prefix = "__WEB_E2E_STEP__"
    print(f"{prefix}|running|{step_id}|{action}", flush=True)
    try:
        yield
    except BaseException:
        print(f"{prefix}|failed|{step_id}|{action}", flush=True)
        raise
    else:
        print(f"{prefix}|passed|{step_id}|{action}", flush=True)'''


class PytestExporter:
    """Render model-independent recorded assets as executable pytest source."""

    def render(self, asset: TestAsset) -> str:
        """Return a standalone pytest module for ``asset``."""
        has_browser_steps = any(
            step.action.channel == ActionChannel.BROWSER
            for step in asset.steps
        )
        has_api_steps = any(
            step.action.channel == ActionChannel.API for step in asset.steps
        )
        has_download_steps = any(
            step.action.channel == ActionChannel.BROWSER
            and step.action.name == "download"
            for step in asset.steps
        )
        has_external_steps = any(
            step.action.channel == ActionChannel.EXTERNAL
            and step.action.tool_ref is not None
            for step in asset.steps
        )
        has_application_steps = any(
            step.action.name == "launch_application"
            for step in asset.steps
        )
        has_spreadsheet_steps = any(
            step.action.name == "extract_spreadsheet"
            for step in asset.steps
        )
        has_portable_tools = any(
            step.action.channel == ActionChannel.EXTERNAL
            and step.action.tool_ref is not None
            and "command"
            not in asset.tool_dependencies.get(step.action.tool_ref, {})
            for step in asset.steps
        )
        variable_definitions = _used_variable_definitions(asset)
        has_environment_references = any(
            _contains_environment_reference(step.action.arguments)
            for step in asset.steps
        )
        imports: list[str] = []
        if asset.steps:
            imports.append("from contextlib import contextmanager")
            imports.append("from collections.abc import Iterator")
        if has_api_steps:
            imports.extend(
                ["import json", "import urllib.error", "import urllib.request"]
            )
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
            imports.append(
                "from playwright.sync_api import Page, sync_playwright, expect"
            )
        fixture = (
            self._render_page_fixture(asset) if has_browser_steps else ""
        )
        body = [self._render_step(asset, step) for step in asset.steps]
        if any(
            step.action.name
            in {"bind_page", "switch_page", "close_page", "open_popup"}
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
                "    os.environ.get(\"WEB_E2E_PROJECT_ROOT\", Path.cwd())\n"
                ").resolve()"
            )
        if variable_definitions:
            sections.append(
                _render_generated_variables(variable_definitions)
            )
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
        nested = "\n".join(
            f"    {line}" if line else ""
            for line in action_source.splitlines()
        )
        return (
            f"with _validation_step({step.id!r}, {step.action.name!r}):\n"
            f"{nested}"
        )

    def export(self, asset: TestAsset, path: str | Path) -> Path:
        """Write the rendered pytest module to ``path`` and return that path."""
        destination = Path(path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(self.render(asset), encoding="utf-8")
        return destination

    def _render_page_fixture(self, asset: TestAsset) -> str:
        context_arguments = (
            f"base_url={asset.base_url!r}" if asset.base_url else ""
        )
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
                return (
                    f"_extract_spreadsheet({file_path!r}, "
                    f"{query!r}, {sheet!r})"
                )
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
            target = _locator_expression(_required_locator(action))
            timeout = _optional_number_argument(action, "timeout")
            timeout_option = (
                f"timeout={_number_literal(timeout)}"
                if timeout is not None
                else ""
            )
            return (
                f"with page.expect_popup({timeout_option}) as popup_info:\n"
                f"    {target}.click({timeout_option})\n"
                "page = popup_info.value\n"
                f"pages[{page_ref!r}] = page\n"
                "page.wait_for_load_state()"
            )

        target = _locator_expression(_required_locator(action))
        operation_options = _browser_timeout_options(action)
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
            suffix = (
                f", timeout={_number_literal(timeout)}"
                if timeout is not None
                else ""
            )
            return f"expect({target}).to_contain_text({expected!r}{suffix})"
        if name == "assert_visible":
            timeout = _optional_number_argument(action, "timeout")
            if timeout is None and action.timeout_seconds is not None:
                timeout = action.timeout_seconds * 1000
            arguments = (
                f"timeout={_number_literal(timeout)}"
                if timeout is not None
                else ""
            )
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
        if (
            isinstance(expected_status, bool)
            or not isinstance(expected_status, int)
        ):
            raise ValueError("http_request expected_status must be an integer")
        timeout = _optional_number_argument(action, "timeout")
        if timeout is None:
            timeout = action.timeout_seconds
        return (
            "_http_request("
            f"method={_string_expression(method)}, "
            f"url={_string_expression(url)}, "
            f"headers={_value_expression(headers)}, "
            f"json_body={_value_expression(action.arguments.get('json'))}, "
            f"body={_value_expression(body)}, "
            f"expected_status={expected_status!r}, "
            f"timeout={_number_literal(timeout) if timeout is not None else '30'}"
            ")"
        )

    def _render_wait(self, action: Action) -> str:
        duration = resolve_wait_milliseconds(action)
        if action.locator is not None:
            target = _locator_expression(action.locator)
            keyword_arguments: list[str] = []
            state = action.arguments.get("state")
            if state is not None:
                if not isinstance(state, str):
                    raise ValueError("wait argument 'state' must be a string")
                keyword_arguments.append(f"state={state!r}")
            keyword_arguments.append(
                f"timeout={_number_literal(duration)}"
            )
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
                raise ValueError(
                    "screenshot argument 'full_page' must be a boolean"
                )
            if action.locator is not None:
                raise ValueError(
                    "full_page is only supported for page screenshots"
                )
            arguments.append(f"full_page={full_page!r}")
        target = (
            _locator_expression(action.locator)
            if action.locator is not None
            else "page"
        )
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
            raise ValueError(
                f"Unknown external tool dependency: {action.tool_ref!r}"
            ) from error

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
                else _string_expression(
                    _required_string(default_cwd, "External tool cwd")
                )
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
                f"{key!r}: {_string_expression(value)}"
                for key, value in environment.items()
            )
            environment_expression = (
                "{**os.environ"
                + (f", {environment_items}" if environment_items else "")
                + "}"
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
                raise ValueError(
                    f"Tool dependency {action.tool_ref!r} timeout "
                    "must be numeric"
                )
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
        raise ValueError(
            f"Tool dependency {tool_ref!r} has unsupported kind {kind!r}"
        )
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
    return (
        f"timeout={_number_literal(timeout)}"
        if timeout is not None
        else ""
    )


def _portable_cwd_expression(
    dependency: dict[str, JsonValue],
    action: Action,
) -> str:
    root = _required_string(dependency.get("root"), "External tool root")
    raw_cwd = action.arguments.get("cwd")
    cwd = (
        _required_string(raw_cwd, "External action cwd")
        if raw_cwd is not None
        else ""
    )
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
    if not isinstance(value, list) or not all(
        isinstance(part, str) for part in value
    ):
        raise ValueError(f"{label} must be a list of strings")
    if non_empty and not value:
        raise ValueError(f"{label} must not be empty")
    return [str(part) for part in value]


def _string_mapping(value: JsonValue, label: str) -> dict[str, str]:
    if not isinstance(value, dict) or not all(
        isinstance(key, str) and isinstance(item, str)
        for key, item in value.items()
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
        stem = re.sub(
            r"\W+",
            "_",
            asset_id,
            flags=re.UNICODE,
        ).strip("_").casefold()
    return f"test_{stem or 'recorded_flow'}"


def _required_locator(action: Action) -> Locator:
    if action.locator is None:
        raise ValueError(f"Browser action {action.name!r} requires a locator")
    return action.locator


def _locator_expression(locator: Locator) -> str:
    strategy = locator.strategy.casefold().replace("-", "_")
    if strategy == "role":
        arguments = [repr(locator.value)]
        if locator.name is not None:
            arguments.append(f"name={locator.name!r}")
        arguments.append(f"exact={locator.exact!r}")
        expression = f"page.get_by_role({', '.join(arguments)})"
    elif strategy == "label":
        expression = (
            f"page.get_by_label({locator.value!r}, exact={locator.exact!r})"
        )
    elif strategy == "text":
        expression = (
            f"page.get_by_text({locator.value!r}, exact={locator.exact!r})"
        )
    elif strategy == "placeholder":
        expression = (
            "page.get_by_placeholder("
            f"{locator.value!r}, exact={locator.exact!r})"
        )
    elif strategy in {"test_id", "testid"}:
        expression = f"page.get_by_test_id({locator.value!r})"
    elif strategy in {"css", "xpath", "selector"}:
        expression = f"page.locator({locator.value!r})"
    elif strategy in {"alt_text", "alt"}:
        expression = (
            f"page.get_by_alt_text({locator.value!r}, exact={locator.exact!r})"
        )
    elif strategy == "title":
        expression = (
            f"page.get_by_title({locator.value!r}, exact={locator.exact!r})"
        )
    else:
        raise ValueError(f"Unsupported locator strategy: {locator.strategy!r}")

    if locator.fallbacks:
        alternatives = [
            _locator_expression(fallback) for fallback in locator.fallbacks
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
                raise ValueError(
                    f"{action.name} argument {candidate!r} must be a string"
                )
            return value
    if default is not None:
        return default
    raise ValueError(f"{action.name} requires string argument {key!r}")


def _select_option_expression(action: Action) -> str:
    if "values" in action.arguments:
        values = action.arguments["values"]
        if not isinstance(values, list) or not all(
            isinstance(value, str) for value in values
        ):
            raise ValueError("select argument 'values' must be a list of strings")
        return repr(values)
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
            return repr(value)
        return f"{key}={value!r}"
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
        [*_ENV_REFERENCE.finditer(value), *_VARIABLE_REFERENCE.finditer(value)],
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
        else:
            parts.append(f"VARIABLES[{match.group(1)!r}]")
        cursor = match.end()
    if cursor < len(value):
        parts.append(repr(value[cursor:]))
    return " + ".join(parts)


def _value_expression(value: JsonValue) -> str:
    if isinstance(value, str):
        return _string_expression(value)
    if isinstance(value, list):
        return "[" + ", ".join(_value_expression(item) for item in value) + "]"
    if isinstance(value, dict):
        return "{" + ", ".join(
            f"{key!r}: {_value_expression(item)}"
            for key, item in value.items()
        ) + "}"
    return repr(value)


def _used_variable_definitions(
    asset: TestAsset,
) -> dict[str, dict[str, JsonValue]]:
    used = set().union(
        *(
            _referenced_variable_names(step.action.arguments)
            for step in asset.steps
        )
    ) if asset.steps else set()
    if not used:
        return {}
    raw_definitions = asset.metadata.get("variable_definitions", [])
    if not isinstance(raw_definitions, list):
        raise ValueError("Asset variable_definitions must be a list")
    definitions = {
        str(item.get("name", "")): dict(item)
        for item in raw_definitions
        if isinstance(item, dict)
    }
    missing = used - definitions.keys()
    if missing:
        raise ValueError(
            "Missing generated variable definitions: "
            + ", ".join(sorted(missing))
        )
    return {name: definitions[name] for name in sorted(used)}


def _referenced_variable_names(value: JsonValue) -> set[str]:
    if isinstance(value, str):
        return set(_VARIABLE_REFERENCE.findall(value))
    if isinstance(value, list):
        return (
            set().union(*(_referenced_variable_names(item) for item in value))
            if value
            else set()
        )
    if isinstance(value, dict):
        return (
            set().union(
                *(_referenced_variable_names(item) for item in value.values())
            )
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


def _render_generated_variables(
    definitions: dict[str, dict[str, JsonValue]],
) -> str:
    return f'''_VARIABLE_DEFINITIONS = {definitions!r}


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
}}'''


__all__ = ["PytestExporter"]
