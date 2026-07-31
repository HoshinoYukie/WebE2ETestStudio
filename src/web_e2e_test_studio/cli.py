"""Command-line shell around the portable authoring core."""

from __future__ import annotations

import argparse
import ast
import sys
from collections.abc import Sequence
from pathlib import Path

from web_e2e_test_studio.exporters import PytestExporter
from web_e2e_test_studio.storage import JsonAssetStore


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="web-e2e-studio",
        description=(
            "Record Playwright actions with optional VLM assistance and export "
            "model-free pytest."
        ),
    )
    subcommands = parser.add_subparsers(dest="command", required=True)

    export = subcommands.add_parser(
        "export", help="Compile a saved test asset to plain pytest"
    )
    export.add_argument("asset", type=Path)
    export.add_argument("-o", "--output", required=True, type=Path)
    export.set_defaults(handler=_export_command)

    inspect = subcommands.add_parser(
        "inspect", help="Show the portable contents of a test asset"
    )
    inspect.add_argument("asset", type=Path)
    inspect.set_defaults(handler=_inspect_command)

    validate = subcommands.add_parser(
        "validate", help="Validate an asset and its generated Python syntax"
    )
    validate.add_argument("asset", type=Path)
    validate.set_defaults(handler=_validate_command)

    record = subcommands.add_parser(
        "record",
        help="Open a browser for manual and VLM-assisted interactive recording",
    )
    record.add_argument("--url", required=True)
    record.add_argument("--goal", required=True)
    record.add_argument("--name", default="recorded-flow")
    record.add_argument(
        "--asset",
        type=Path,
        default=Path("artifacts/recorded-flow.asset.json"),
    )
    record.add_argument(
        "--output",
        type=Path,
        default=Path("generated/test_recorded_flow.py"),
    )
    record.add_argument("--workspace", type=Path, default=Path.cwd())
    record.add_argument(
        "--tool",
        action="append",
        type=Path,
        default=[],
        help="Register an external executable or script before recording",
    )
    record.add_argument("--headless", action="store_true")
    record.set_defaults(handler=_record_command)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    arguments = parser.parse_args(argv)
    handler = arguments.handler
    try:
        return int(handler(arguments))
    except (FileNotFoundError, KeyError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


def _export_command(arguments: argparse.Namespace) -> int:
    asset = JsonAssetStore().load(arguments.asset)
    destination = PytestExporter().export(asset, arguments.output)
    print(f"Exported model-free pytest: {destination}")
    return 0


def _inspect_command(arguments: argparse.Namespace) -> int:
    asset = JsonAssetStore().load(arguments.asset)
    print(f"name: {asset.name}")
    print(f"id: {asset.id}")
    print(f"goal: {asset.goal}")
    print(f"base URL: {asset.base_url or '(none)'}")
    print(f"verified steps: {len(asset.steps)}")
    print(f"external tools: {len(asset.tool_dependencies)}")
    for index, step in enumerate(asset.steps, start=1):
        print(
            f"  {index}. [{step.action.channel.value}] "
            f"{step.action.name} — {step.intent}"
        )
    return 0


def _validate_command(arguments: argparse.Namespace) -> int:
    asset = JsonAssetStore().load(arguments.asset)
    generated = PytestExporter().render(asset)
    ast.parse(generated)
    print(
        f"Valid asset {asset.id}: {len(asset.steps)} verified step(s), "
        "generated Python syntax OK"
    )
    return 0


def _record_command(arguments: argparse.Namespace) -> int:
    from web_e2e_test_studio.application import run_interactive_recording

    return run_interactive_recording(
        url=str(arguments.url),
        goal=str(arguments.goal),
        name=str(arguments.name),
        asset_path=arguments.asset,
        output_path=arguments.output,
        workspace=arguments.workspace,
        tools=arguments.tool,
        headless=bool(arguments.headless),
    )


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["build_parser", "main"]

