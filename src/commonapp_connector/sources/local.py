from __future__ import annotations

import fnmatch
from pathlib import Path
from typing import Iterator

from .base import ExportFile, Source


class LocalDirectorySource(Source):
    """Reads exports already on disk (e.g. dropped by another transfer job)."""

    def __init__(self, directory: str | Path, pattern: str = "*.txt"):
        self.directory = Path(directory)
        self.pattern = pattern

    def list_files(self) -> Iterator[ExportFile]:
        paths = [p for p in self.directory.iterdir() if p.is_file() and fnmatch.fnmatch(p.name, self.pattern)]
        for p in sorted(paths, key=lambda p: (p.stat().st_mtime, p.name)):
            st = p.stat()
            yield ExportFile(p.name, f"{st.st_size}:{int(st.st_mtime)}")

    def fetch(self, file: ExportFile, dest_dir: Path) -> Path:
        return self.directory / file.name
