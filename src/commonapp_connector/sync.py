"""Orchestrates source -> parser -> sinks with idempotent file tracking."""

from __future__ import annotations

import logging
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from .parser import ExportParser, ParseReport
from .sinks import Sink, iter_unique
from .sources.base import Source
from .state import StateStore

log = logging.getLogger(__name__)


@dataclass
class FileResult:
    name: str
    applications: int
    errors: int
    unmapped_columns: list[str] = field(default_factory=list)


@dataclass
class SyncResult:
    files: list[FileResult] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    failed: dict[str, str] = field(default_factory=dict)

    @property
    def total_applications(self) -> int:
        return sum(f.applications for f in self.files)


class SyncRunner:
    def __init__(self, source: Source, sinks: list[Sink], state: StateStore,
                 parser: ExportParser | None = None, staging_dir: str | Path | None = None,
                 dry_run: bool = False):
        self.source = source
        self.sinks = sinks
        self.state = state
        self.parser = parser or ExportParser()
        self.staging_dir = Path(staging_dir) if staging_dir else None
        self.dry_run = dry_run

    def run(self) -> SyncResult:
        result = SyncResult()
        with tempfile.TemporaryDirectory() as tmp:
            staging = self.staging_dir or Path(tmp)
            for export in self.source.list_files():
                if self.state.is_processed(export.name, export.fingerprint):
                    result.skipped.append(export.name)
                    continue
                try:
                    result.files.append(self._process(export, staging))
                except Exception as exc:  # keep going; one bad file shouldn't block the rest
                    log.exception("failed to process %s", export.name)
                    result.failed[export.name] = str(exc)
                    continue
                if not self.dry_run:
                    self.state.mark_processed(export.name, export.fingerprint)
                    self.state.save()
        return result

    def _process(self, export, staging: Path) -> FileResult:
        local = self.source.fetch(export, staging)
        report = ParseReport()
        apps = iter_unique(self.parser.parse_file(local, report))
        if report.unmapped_columns:
            log.info("%s: unmapped columns kept in 'extra': %s", export.name, report.unmapped_columns)
        if not self.dry_run:
            for sink in self.sinks:
                sink.write(apps, export.name)
        log.info("%s: %d applications, %d row errors", export.name, len(apps), len(report.errors))
        return FileResult(export.name, len(apps), len(report.errors), report.unmapped_columns)
