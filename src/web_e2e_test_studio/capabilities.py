"""Extensible capability adapters shared by agents and Studio shells."""

from __future__ import annotations

from dataclasses import dataclass
from importlib.metadata import entry_points
from typing import Any, Protocol, cast

from web_e2e_test_studio.agent import ActionRegistry, ActionSpec
from web_e2e_test_studio.api import HttpActionExecutor, http_action_spec
from web_e2e_test_studio.domain import ActionChannel, JsonValue
from web_e2e_test_studio.files import (
    SpreadsheetExtractor,
    spreadsheet_action_spec,
)
from web_e2e_test_studio.tools import ExternalToolExecutor, ExternalToolRegistry


@dataclass(frozen=True)
class Capability:
    """One discoverable adapter and the actions it contributes."""

    id: str
    label: str
    channel: ActionChannel
    actions: tuple[ActionSpec, ...]

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "id": self.id,
            "label": self.label,
            "channel": self.channel.value,
            "actions": [action.to_dict() for action in self.actions],
        }


@dataclass(frozen=True)
class CapabilityContext:
    """Runtime resources supplied to adapters by a composition root."""

    device: Any
    tools: ExternalToolRegistry


class CapabilityAdapter(Protocol):
    """Plugin contract for adding actions without editing the Agent loop or UI."""

    def describe(self, context: CapabilityContext | None = None) -> Capability: ...

    def register(
        self,
        registry: ActionRegistry,
        context: CapabilityContext,
    ) -> None: ...


class BrowserCapabilityAdapter:
    def describe(self, context: CapabilityContext | None = None) -> Capability:
        if context is None:
            from web_e2e_test_studio.browser import PlaywrightDevice

            actions = PlaywrightDevice.action_specs()
        else:
            actions = tuple(context.device.action_specs())
        return Capability(
            id="playwright",
            label="Browser",
            channel=ActionChannel.BROWSER,
            actions=actions,
        )

    def register(
        self, registry: ActionRegistry, context: CapabilityContext
    ) -> None:
        for spec in self.describe(context).actions:
            registry.register(spec, context.device)


class HttpCapabilityAdapter:
    def describe(self, context: CapabilityContext | None = None) -> Capability:
        return Capability(
            id="http",
            label="HTTP API",
            channel=ActionChannel.API,
            actions=(http_action_spec(),),
        )

    def register(
        self, registry: ActionRegistry, context: CapabilityContext
    ) -> None:
        registry.register(http_action_spec(), HttpActionExecutor())


class ExternalToolCapabilityAdapter:
    def describe(self, context: CapabilityContext | None = None) -> Capability:
        available = (
            ", ".join(
                f"{tool.id} ({tool.name})"
                for tool in context.tools.list_tools()
            )
            if context is not None
            else ""
        )
        specs = (
            ActionSpec(
                name="run_external_tool",
                channel=ActionChannel.EXTERNAL,
                description=(
                    "Run a project-registered external tool to completion using "
                    f"tool_ref. Available: {available or 'none'}"
                ),
            ),
            ActionSpec(
                name="launch_application",
                channel=ActionChannel.EXTERNAL,
                description=(
                    "Start a user-registered executable without waiting for it "
                    f"to close, using tool_ref. Available: {available or 'none'}"
                ),
                user_initiated_only=True,
            ),
        )
        return Capability(
            id="external-tools",
            label="External Tools",
            channel=ActionChannel.EXTERNAL,
            actions=specs,
        )

    def register(
        self, registry: ActionRegistry, context: CapabilityContext
    ) -> None:
        executor = ExternalToolExecutor(context.tools)
        for spec in self.describe(context).actions:
            registry.register(spec, executor)


class SpreadsheetCapabilityAdapter:
    def describe(self, context: CapabilityContext | None = None) -> Capability:
        return Capability(
            id="spreadsheet",
            label="Spreadsheet",
            channel=ActionChannel.EXTERNAL,
            actions=(spreadsheet_action_spec(),),
        )

    def register(
        self, registry: ActionRegistry, context: CapabilityContext
    ) -> None:
        registry.register(
            spreadsheet_action_spec(),
            SpreadsheetExtractor((context.tools.workspace_root,)),
        )


class CapabilityCatalog:
    """Ordered adapter collection with optional package entry-point plugins."""

    def __init__(self, adapters: tuple[CapabilityAdapter, ...] = ()) -> None:
        self._adapters = list(adapters)

    def add(self, adapter: CapabilityAdapter) -> None:
        self._adapters.append(adapter)

    def describe(
        self, context: CapabilityContext | None = None
    ) -> tuple[Capability, ...]:
        return tuple(adapter.describe(context) for adapter in self._adapters)

    def build_action_registry(
        self, context: CapabilityContext
    ) -> ActionRegistry:
        registry = ActionRegistry()
        for adapter in self._adapters:
            adapter.register(registry, context)
        return registry


def default_capability_catalog() -> CapabilityCatalog:
    catalog = CapabilityCatalog(
        (
            BrowserCapabilityAdapter(),
            HttpCapabilityAdapter(),
            SpreadsheetCapabilityAdapter(),
            ExternalToolCapabilityAdapter(),
        )
    )
    for plugin in entry_points(
        group="web_e2e_test_studio.capabilities"
    ):
        loaded = plugin.load()
        adapter = loaded() if isinstance(loaded, type) else loaded
        catalog.add(cast(CapabilityAdapter, adapter))
    return catalog


def default_capabilities() -> tuple[Capability, ...]:
    """Discover built-in and installed plugin adapters without starting them."""
    return default_capability_catalog().describe()


__all__ = [
    "Capability",
    "CapabilityAdapter",
    "CapabilityCatalog",
    "CapabilityContext",
    "default_capabilities",
    "default_capability_catalog",
]
