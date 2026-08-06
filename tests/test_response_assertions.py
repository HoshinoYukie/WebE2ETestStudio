import pytest

from web_e2e_test_studio.api.response_assertions import assert_json_response


def test_assert_json_response_supports_nested_objects_and_arrays() -> None:
    assertions = [
        {"path": "data.items.0.id", "operator": "equals", "expected": 42},
        {"path": "data.tags", "operator": "contains", "expected": "ready"},
        {"path": "data.optional", "operator": "exists"},
        {"path": "data.removed", "operator": "not_exists"},
    ]

    assert_json_response(
        {"data": {"items": [{"id": 42}], "tags": ["ready"], "optional": None}},
        assertions,
    )


def test_assert_json_response_reports_path_expected_and_actual() -> None:
    with pytest.raises(AssertionError, match="data.status.*active.*pending"):
        assert_json_response(
            {"data": {"status": "pending"}},
            [{"path": "data.status", "operator": "equals", "expected": "active"}],
        )


def test_assert_json_response_uses_type_sensitive_equality() -> None:
    with pytest.raises(AssertionError):
        assert_json_response(
            {"enabled": True},
            [{"path": "enabled", "operator": "equals", "expected": 1}],
        )
