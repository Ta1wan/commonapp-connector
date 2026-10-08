"""SQLite store of ingested applications, queried by the MCP server."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from .models import Application
from .sinks import Sink

_SCHEMA = """
CREATE TABLE IF NOT EXISTS applications (
    key TEXT PRIMARY KEY,
    common_app_id TEXT NOT NULL,
    first_name TEXT, last_name TEXT, preferred_name TEXT, email TEXT,
    city TEXT, state TEXT, country TEXT, high_school_name TEXT, high_school_ceeb TEXT,
    term TEXT, decision_plan TEXT, intended_major TEXT, submitted_at TEXT, fee_status TEXT,
    source_file TEXT NOT NULL,
    data TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_app_caid ON applications(common_app_id);
"""

# Columns a caller may group or filter by; anything else is rejected (they're
# interpolated into SQL as identifiers).
GROUPABLE = ("term", "decision_plan", "intended_major", "state", "country",
             "high_school_name", "fee_status", "source_file")

_SUMMARY_COLS = ("common_app_id", "first_name", "last_name", "email", "term",
                 "decision_plan", "intended_major", "state", "high_school_name", "submitted_at")


class ApplicationStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)

    def upsert(self, applications: list[Application], source_file: str) -> None:
        rows = []
        for app in applications:
            a = app.applicant
            rows.append((
                app.key, a.common_app_id, a.first_name, a.last_name, a.preferred_name, a.email,
                a.city, a.state, a.country, a.high_school_name, a.high_school_ceeb,
                app.term, app.decision_plan, app.intended_major, app.submitted_at, app.fee_status,
                source_file, json.dumps(app.to_dict()),
            ))
        with self._conn:
            self._conn.executemany(
                f"INSERT OR REPLACE INTO applications VALUES ({','.join('?' * 18)})", rows)

    def search(self, query: str = "", filters: dict[str, str] | None = None,
               limit: int = 25, offset: int = 0) -> tuple[int, list[dict[str, Any]]]:
        where, params = self._where(query, filters)
        total = self._conn.execute(f"SELECT COUNT(*) FROM applications{where}", params).fetchone()[0]
        rows = self._conn.execute(
            f"SELECT {','.join(_SUMMARY_COLS)} FROM applications{where} "
            "ORDER BY last_name, first_name, common_app_id LIMIT ? OFFSET ?",
            [*params, limit, offset],
        ).fetchall()
        return total, [dict(r) for r in rows]

    def get(self, common_app_id: str) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT data, source_file FROM applications WHERE common_app_id = ? ORDER BY term",
            (common_app_id,),
        ).fetchall()
        return [{**json.loads(r["data"]), "source_file": r["source_file"]} for r in rows]

    def counts(self, group_by: str, filters: dict[str, str] | None = None,
               limit: int = 50) -> list[dict[str, Any]]:
        if group_by not in GROUPABLE:
            raise ValueError(f"group_by must be one of {GROUPABLE}")
        where, params = self._where("", filters)
        rows = self._conn.execute(
            f"SELECT COALESCE(NULLIF({group_by}, ''), '(blank)') AS value, COUNT(*) AS count "
            f"FROM applications{where} GROUP BY value ORDER BY count DESC, value LIMIT ?",
            [*params, limit],
        ).fetchall()
        return [dict(r) for r in rows]

    def files(self) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT source_file, COUNT(*) AS applications FROM applications "
            "GROUP BY source_file ORDER BY source_file").fetchall()
        return [dict(r) for r in rows]

    def _where(self, query: str, filters: dict[str, str] | None) -> tuple[str, list[str]]:
        clauses, params = [], []
        for col, value in (filters or {}).items():
            if value is None or value == "":
                continue
            if col not in GROUPABLE:
                raise ValueError(f"Cannot filter on {col!r}; allowed: {GROUPABLE}")
            clauses.append(f"{col} = ? COLLATE NOCASE")
            params.append(value)
        if query:
            like = f"%{query.strip()}%"
            clauses.append(
                "(common_app_id = ? OR email LIKE ? OR first_name || ' ' || last_name LIKE ? "
                "OR preferred_name LIKE ? OR last_name LIKE ?)")
            params += [query.strip(), like, like, like, like]
        return (" WHERE " + " AND ".join(clauses) if clauses else ""), params

    def close(self) -> None:
        self._conn.close()


class SqliteSink(Sink):
    def __init__(self, store: ApplicationStore):
        self.store = store

    def write(self, applications, source_name):
        self.store.upsert(applications, source_name)
