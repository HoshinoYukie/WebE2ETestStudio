from __future__ import annotations

import re
from typing import Any

import pytest

from web_e2e_test_studio.agent.planner import PlannerProtocolError
from web_e2e_test_studio.variables import (
    VariableDefinition,
    VariableRegistry,
    VLMVariableDefinitionCompiler,
    VLMVariableExtractionCompiler,
    generate_variable,
    variable_definition_from_rule,
)


class _RuleBackend:
    def __init__(self, rule: dict[str, Any]) -> None:
        self.rule = rule
        self.calls: list[tuple[str, str, str]] = []

    def complete_json(
        self, system_prompt: str, user_prompt: str, screenshot_path: str
    ) -> dict[str, Any]:
        self.calls.append((system_prompt, user_prompt, screenshot_path))
        return self.rule


class _MalformedThenValidBackend(_RuleBackend):
    def complete_json(
        self, system_prompt: str, user_prompt: str, screenshot_path: str
    ) -> dict[str, Any]:
        self.calls.append((system_prompt, user_prompt, screenshot_path))
        if len(self.calls) == 1:
            raise PlannerProtocolError("VLM did not return valid JSON")
        return self.rule


@pytest.mark.parametrize(
    "description",
    [
        "以 ORD- 开头的 8 位数字",
        "ORD- で始まる8桁の数字",
        "8 digits prefixed with ORD-",
    ],
)
def test_vlm_compiler_accepts_descriptions_without_language_specific_parsing(
    description: str,
) -> None:
    backend = _RuleBackend({"kind": "digits", "options": {"length": 8, "prefix": "ORD-"}})

    definition = VLMVariableDefinitionCompiler(backend).compile("order_id", description)

    assert definition.description == description
    assert definition.kind == "digits"
    assert definition.options == {"length": 8, "prefix": "ORD-"}
    assert description in backend.calls[0][1]
    assert re.fullmatch(r"ORD-\d{8}", generate_variable(definition))


def test_variable_compiler_repairs_one_malformed_model_response() -> None:
    backend = _MalformedThenValidBackend(
        {"kind": "digits", "options": {"length": 6, "prefix": "INV-"}}
    )

    definition = VLMVariableDefinitionCompiler(backend).compile(
        "invoice_id", "six digits prefixed with INV-"
    )

    assert definition.kind == "digits"
    assert len(backend.calls) == 2
    assert "valid JSON" in backend.calls[1][1]


@pytest.mark.parametrize(
    "description",
    ["获取订单编号", "注文番号を取得", "Get the order number"],
)
def test_page_extraction_compiler_preserves_multilingual_request(
    description: str,
) -> None:
    backend = _RuleBackend(
        {
            "locator": {"strategy": "label", "value": "Order ID", "exact": True},
            "read": "value",
        }
    )

    definition = VLMVariableExtractionCompiler(backend).compile(
        "order_id", description, source="page", context="Order ID: ORD-42"
    )

    assert definition.kind == "page_value"
    assert definition.options["read"] == "value"
    assert description in backend.calls[0][1]


def test_spreadsheet_extraction_compiler_records_portable_rule() -> None:
    backend = _RuleBackend({"query": "Bulk installation link", "sheet": "Devices"})

    definition = VLMVariableExtractionCompiler(backend).compile(
        "bulk_link",
        "Find the bulk installation link",
        source="spreadsheet",
        context="Devices!F2=https://example.test/install",
        file_path="recordings/downloads/devices.xlsx",
    )

    assert definition.kind == "spreadsheet_value"
    assert definition.options["file_path"] == "recordings/downloads/devices.xlsx"
    assert definition.options["query"] == "Bulk installation link"


@pytest.mark.parametrize(
    ("rule", "message"),
    [
        ({"kind": "script", "options": {}}, "Unsupported"),
        ({"kind": "digits", "options": {"length": 0}}, "between 1 and 256"),
        ({"kind": "integer", "options": {"minimum": 9, "maximum": 1}}, "minimum"),
        ({"kind": "email", "options": {"domain": "bad domain"}}, "domain"),
    ],
)
def test_model_rules_are_validated_before_execution(rule: dict[str, Any], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        variable_definition_from_rule("value", "any language", rule)


def test_variable_registry_resolves_nested_references_and_restores_values() -> None:
    registry = VariableRegistry(
        definition_compiler=lambda name, description: VariableDefinition(
            name, description, "digits", {"length": 8}
        )
    )
    _definition, generated = registry.define("order_id", "portable rule")
    template = {
        "url": "https://api.example.test/orders/${VAR:order_id}",
        "json": {"id": "${VAR:order_id}"},
    }

    resolved = registry.resolve(template)

    assert resolved == {
        "url": f"https://api.example.test/orders/{generated}",
        "json": {"id": generated},
    }
    assert registry.restore_references(resolved) == template
    assert registry.referenced_names(template) == {"order_id"}


def test_variable_registry_rejects_undefined_references() -> None:
    with pytest.raises(ValueError, match="Undefined generated variable"):
        VariableRegistry().resolve("${VAR:missing}")


def test_variable_definition_rejects_unsafe_names() -> None:
    with pytest.raises(ValueError, match="Variable name"):
        VariableDefinition("bad-name", "UUID", "uuid")
