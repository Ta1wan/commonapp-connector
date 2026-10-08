"""Tracks which export files have already been ingested."""

from __future__ import annotations

import json
import os
from pathlib import Path


class StateStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._data: dict = {"processed": {}}
        if self.path.exists():
            self._data = json.loads(self.path.read_text())

    def is_processed(self, name: str, fingerprint: str) -> bool:
        return self._data["processed"].get(name) == fingerprint

    def mark_processed(self, name: str, fingerprint: str) -> None:
        self._data["processed"][name] = fingerprint

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(self._data, indent=2, sort_keys=True))
        os.replace(tmp, self.path)
