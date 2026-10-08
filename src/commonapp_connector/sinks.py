"""Destinations for parsed applications."""

from __future__ import annotations

import csv
import json
import time
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Iterable

import requests

from .models import Application


class Sink(ABC):
    @abstractmethod
    def write(self, applications: list[Application], source_name: str) -> None:
        """Deliver one export file's worth of applications."""


class JsonlSink(Sink):
    """Appends one JSON object per application to a file."""

    def __init__(self, path: str | Path):
        self.path = Path(path)

    def write(self, applications, source_name):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as f:
            for app in applications:
                f.write(json.dumps({**app.to_dict(), "source_file": source_name}) + "\n")


class CsvSink(Sink):
    """Writes a flat CSV per export file into ``directory``."""

    def __init__(self, directory: str | Path):
        self.directory = Path(directory)

    def write(self, applications, source_name):
        self.directory.mkdir(parents=True, exist_ok=True)
        rows = [_flatten(a.to_dict()) for a in applications]
        columns = sorted({k for r in rows for k in r})
        out = self.directory / (Path(source_name).stem + ".csv")
        with out.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=columns)
            writer.writeheader()
            writer.writerows(rows)


class WebhookSink(Sink):
    """POSTs applications as JSON batches to an HTTP endpoint (e.g. a CRM/SIS ingest)."""

    def __init__(self, url: str, token: str | None = None, batch_size: int = 100,
                 max_retries: int = 4, timeout: float = 30, session: requests.Session | None = None):
        self.url = url
        self.batch_size = batch_size
        self.max_retries = max_retries
        self.timeout = timeout
        self.session = session or requests.Session()
        if token:
            self.session.headers["Authorization"] = f"Bearer {token}"

    def write(self, applications, source_name):
        for start in range(0, len(applications), self.batch_size):
            batch = applications[start:start + self.batch_size]
            self._post({"source_file": source_name, "applications": [a.to_dict() for a in batch]})

    def _post(self, payload: dict) -> None:
        for attempt in range(self.max_retries + 1):
            try:
                resp = self.session.post(self.url, json=payload, timeout=self.timeout)
            except requests.ConnectionError:
                if attempt == self.max_retries:
                    raise
            else:
                if resp.status_code < 500 and resp.status_code != 429:
                    resp.raise_for_status()
                    return
                if attempt == self.max_retries:
                    resp.raise_for_status()
            time.sleep(2 ** attempt)


def _flatten(d: dict, prefix: str = "") -> dict:
    out = {}
    for k, v in d.items():
        key = f"{prefix}{k}"
        if isinstance(v, dict):
            out.update(_flatten(v, key + "."))
        else:
            out[key] = v
    return out


def iter_unique(applications: Iterable[Application]) -> list[Application]:
    """Keep the last occurrence of each application key (later rows win)."""
    seen: dict[str, Application] = {}
    for app in applications:
        seen.pop(app.key, None)
        seen[app.key] = app
    return list(seen.values())
