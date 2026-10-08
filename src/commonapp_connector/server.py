"""MCP server exposing Common App applicant data to Claude.

Run locally over stdio (Claude Desktop / Claude Code) or over streamable HTTP
for remote use. Data is read from a local SQLite store that ``sync_exports``
fills from the configured export source.
"""

from __future__ import annotations

import argparse
import hmac
import logging
import os
import threading
from typing import Annotated, Any, Literal

from pydantic import Field

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from .config import Settings
from .mapping import load_field_map
from .parser import ExportParser
from .state import StateStore
from .store import ApplicationStore, SqliteSink
from .sync import SyncRunner

log = logging.getLogger(__name__)

INSTRUCTIONS = """\
Access to Common App applicant records ingested from this institution's export files.
Records contain student PII protected by FERPA: share only what the user's question needs.
Use search_applicants to find people or slice the pool, get_applicant for one person's full
record, application_counts for totals and breakdowns (prefer it over paging through search
results to count), and list_export_files to see what has been loaded. If results look stale
or empty, call sync_exports to pull new export files."""

READ_ONLY = ToolAnnotations(read_only_hint=True, open_world_hint=False)

# Keep in sync with store.GROUPABLE.
GroupField = Literal["term", "decision_plan", "intended_major", "state", "country",
                     "high_school_name", "fee_status", "source_file"]


def create_server(settings: Settings | None = None) -> MCPServer:
    settings = settings or Settings.from_env()
    store = ApplicationStore(settings.db_path)
    sync_lock = threading.Lock()
    mcp = MCPServer("Common App", instructions=INSTRUCTIONS, version="0.1.0")

    @mcp.tool(annotations=READ_ONLY)
    def search_applicants(
        query: Annotated[str, Field(description="Name, email, or exact Common App ID. Empty for all.")] = "",
        term: Annotated[str, Field(description="Entry term, e.g. 'Fall 2027'")] = "",
        decision_plan: Annotated[str, Field(description="e.g. 'Early Decision'")] = "",
        intended_major: str = "",
        state: Annotated[str, Field(description="Applicant's home state/province")] = "",
        high_school_name: str = "",
        limit: Annotated[int, Field(ge=1, le=100)] = 25,
        offset: Annotated[int, Field(ge=0)] = 0,
    ) -> dict[str, Any]:
        """Search ingested applications. Returns summary rows and the total match count.
        Filters are exact, case-insensitive matches; query is a partial match."""
        filters = {"term": term, "decision_plan": decision_plan, "intended_major": intended_major,
                   "state": state, "high_school_name": high_school_name}
        total, rows = store.search(query, filters, limit, offset)
        return {"total": total, "offset": offset, "results": rows,
                "has_more": offset + len(rows) < total}

    @mcp.tool(annotations=READ_ONLY)
    def get_applicant(
        common_app_id: Annotated[str, Field(description="The applicant's Common App ID")],
    ) -> dict[str, Any]:
        """Full record(s) for one applicant, including unmapped export columns under 'extra'.
        An applicant can have one application per entry term."""
        apps = store.get(common_app_id.strip())
        if not apps:
            raise ToolError(f"No applicant with Common App ID {common_app_id!r}")
        return {"common_app_id": common_app_id, "applications": apps}

    @mcp.tool(annotations=READ_ONLY)
    def application_counts(
        group_by: GroupField,
        term: str = "",
        decision_plan: str = "",
        intended_major: str = "",
        state: str = "",
        limit: Annotated[int, Field(ge=1, le=500)] = 50,
    ) -> dict[str, Any]:
        """Count applications grouped by a field, optionally filtered — e.g. applications
        by decision plan for Fall 2027, or top intended majors."""
        filters = {"term": term, "decision_plan": decision_plan,
                   "intended_major": intended_major, "state": state}
        groups = store.counts(group_by, {k: v for k, v in filters.items() if k != group_by}, limit)
        return {"group_by": group_by, "groups": groups, "total": sum(g["count"] for g in groups)}

    @mcp.tool(annotations=READ_ONLY)
    def list_export_files() -> dict[str, Any]:
        """Export files loaded so far, with the number of applications from each."""
        return {"source": settings.source, "files": store.files()}

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False,
                                          idempotent_hint=True, open_world_hint=True))
    def sync_exports() -> dict[str, Any]:
        """Fetch new or changed export files from the configured source (local folder or
        SFTP) and load them. Files already loaded are skipped."""
        if not sync_lock.acquire(blocking=False):
            raise ToolError("A sync is already running; try again shortly.")
        try:
            try:
                sinks = [SqliteSink(store), *settings.build_sinks(required=False)]
                parser = ExportParser(load_field_map(settings.field_map_path))
                source = settings.build_source()
            except Exception as exc:  # bad settings, missing paramiko, SFTP login failure
                raise ToolError(f"Connector is misconfigured: {exc}") from exc
            with source:
                result = SyncRunner(source, sinks, StateStore(settings.state_path), parser).run()
        finally:
            sync_lock.release()
        return {
            "loaded": [{"file": f.name, "applications": f.applications, "row_errors": f.errors,
                        "unmapped_columns": f.unmapped_columns} for f in result.files],
            "already_loaded": len(result.skipped),
            "failed": result.failed,
            "total_applications_loaded": result.total_applications,
        }

    return mcp


class BearerAuthMiddleware:
    """Rejects HTTP requests without ``Authorization: Bearer <token>``."""

    def __init__(self, app, token: str):
        self.app = app
        self.expected = f"Bearer {token}".encode()

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            got = dict(scope["headers"]).get(b"authorization", b"")
            if not hmac.compare_digest(got, self.expected):
                await send({"type": "http.response.start", "status": 401,
                            "headers": [(b"content-type", b"text/plain"),
                                        (b"www-authenticate", b"Bearer")]})
                await send({"type": "http.response.body", "body": b"Unauthorized"})
                return
        await self.app(scope, receive, send)


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="commonapp-mcp", description="Common App MCP connector")
    ap.add_argument("--transport", choices=["stdio", "http"], default="stdio")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    args = ap.parse_args(argv)
    # stdout is the protocol channel under stdio, so logs go to stderr.
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    mcp = create_server()
    if args.transport == "stdio":
        mcp.run("stdio")
        return

    import uvicorn
    from mcp.server.transport_security import TransportSecuritySettings

    token = os.environ.get("COMMONAPP_MCP_TOKEN")
    if not token and args.host not in ("127.0.0.1", "localhost"):
        raise SystemExit("Refusing to serve applicant data on a public interface without "
                         "COMMONAPP_MCP_TOKEN set.")
    # Host-header checks guard local servers against DNS rebinding; behind a
    # token on a public host, the reverse proxy decides which hosts reach us.
    security = (TransportSecuritySettings(enable_dns_rebinding_protection=False) if token else None)
    app = mcp.streamable_http_app(stateless_http=True, transport_security=security, host=args.host)
    if token:
        app = BearerAuthMiddleware(app, token)
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
