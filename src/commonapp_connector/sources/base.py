from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator


@dataclass(frozen=True)
class ExportFile:
    name: str
    # Changes whenever the file's contents change (size + mtime); used to skip re-ingest.
    fingerprint: str


class Source(ABC):
    @abstractmethod
    def list_files(self) -> Iterator[ExportFile]:
        """List candidate export files, oldest first."""

    @abstractmethod
    def fetch(self, file: ExportFile, dest_dir: Path) -> Path:
        """Make ``file`` available locally and return its path."""

    def close(self) -> None:
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
