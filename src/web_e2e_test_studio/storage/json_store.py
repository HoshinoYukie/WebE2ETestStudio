"""Atomic JSON persistence for the versioned test asset IR."""

from __future__ import annotations

import json
from pathlib import Path
from tempfile import NamedTemporaryFile

from web_e2e_test_studio.domain import TestAsset


class JsonAssetStore:
    """Load and save model-independent test assets."""

    def save(self, asset: TestAsset, path: str | Path) -> Path:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        content = json.dumps(
            asset.to_dict(),
            ensure_ascii=False,
            indent=2,
            sort_keys=False,
        )
        with NamedTemporaryFile(
            "w",
            encoding="utf-8",
            newline="\n",
            dir=target.parent,
            prefix=f".{target.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            handle.write(content)
            handle.write("\n")
            temporary = Path(handle.name)
        temporary.replace(target)
        return target

    def load(self, path: str | Path) -> TestAsset:
        target = Path(path)
        data = json.loads(target.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("Test asset must contain a JSON object")
        return TestAsset.from_dict(data)

