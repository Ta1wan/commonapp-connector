"""Parses Common App delimited export files into Application records."""

from __future__ import annotations

import csv
import io
import logging
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Iterator

from .mapping import load_field_map, resolve_columns
from .models import Applicant, Application

log = logging.getLogger(__name__)

_DATE_FORMATS = ("%m/%d/%Y", "%Y-%m-%d", "%m-%d-%Y", "%m/%d/%y", "%Y%m%d", "%m/%Y")
_DATE_FIELDS = {"birth_date", "graduation_date"}


def parse_date(value: str) -> date | None:
    value = value.strip()
    if not value:
        return None
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"Unrecognized date: {value!r}")


@dataclass
class ParseError:
    line: int
    message: str


@dataclass
class ParseReport:
    errors: list[ParseError] = field(default_factory=list)
    unmapped_columns: list[str] = field(default_factory=list)


class ExportParser:
    """Reads tab-, comma- or pipe-delimited exports with a header row."""

    def __init__(self, field_map: dict[str, list[str]] | None = None):
        self.field_map = field_map or load_field_map()

    def parse_file(self, path: str | Path, report: ParseReport | None = None) -> Iterator[Application]:
        raw = Path(path).read_bytes()
        yield from self.parse_text(_decode(raw), report)

    def parse_text(self, text: str, report: ParseReport | None = None) -> Iterator[Application]:
        report = report if report is not None else ParseReport()
        if not text.strip():
            return
        reader = csv.reader(io.StringIO(text), delimiter=_sniff_delimiter(text))
        headers = next(reader)
        columns = resolve_columns(headers, self.field_map)
        if "applicant.common_app_id" not in columns:
            raise ValueError(
                "Export has no Common App ID column; add its header to the field map. "
                f"Headers seen: {headers}"
            )
        claimed = set(columns.values())
        extra_cols = [(i, h.strip()) for i, h in enumerate(headers) if i not in claimed]
        report.unmapped_columns = [h for _, h in extra_cols]

        for line_no, row in enumerate(reader, start=2):
            if not any(cell.strip() for cell in row):
                continue
            try:
                yield self._build(row, columns, extra_cols)
            except ValueError as exc:
                log.warning("line %d: %s", line_no, exc)
                report.errors.append(ParseError(line_no, str(exc)))

    def _build(self, row: list[str], columns: dict[str, int], extra_cols) -> Application:
        def cell(i: int) -> str:
            return row[i].strip() if i < len(row) else ""

        applicant_kwargs: dict = {}
        app_kwargs: dict = {}
        for path, i in columns.items():
            value = cell(i)
            if path.startswith("applicant."):
                name = path.split(".", 1)[1]
                applicant_kwargs[name] = parse_date(value) if name in _DATE_FIELDS else value
            else:
                app_kwargs[path] = value

        if not applicant_kwargs.get("common_app_id"):
            raise ValueError("missing Common App ID")
        extra = {h: cell(i) for i, h in extra_cols if cell(i)}
        return Application(applicant=Applicant(**applicant_kwargs), extra=extra, **app_kwargs)


def _decode(raw: bytes) -> str:
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1")


def _sniff_delimiter(text: str) -> str:
    header = text.splitlines()[0]
    return max(("\t", ",", "|"), key=header.count)
