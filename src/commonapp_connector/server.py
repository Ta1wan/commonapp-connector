"""MCP server exposing Common App applicant data to Claude.

Run locally over stdio (Claude Desktop / Claude Code) or over streamable HTTP
with OAuth sign-in for remote use (claude.ai custom connectors). Data is read from a local SQLite store that ``sync_exports``
fills from the configured export source.
"""

from __future__ import annotations

import argparse
import logging
import os
import threading
from typing import Annotated, Any, Literal
from urllib.parse import urlparse

from pydantic import Field

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from .auth import CALLBACK_PATH, OAuthSettings, OIDCAuthProvider
from .config import Settings
from .mapping import load_field_map
from .parser import ExportParser
from .state import StateStore
from .store import ApplicationStore, SqliteSink
from .sync import SyncRunner

log = logging.getLogger(__name__)
audit = logging.getLogger("commonapp_connector.audit")

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


def _audit(tool: str, **args: Any) -> None:
    """Record who looked at what (FERPA requires knowing who accessed records)."""
    token = get_access_token()
    who = token.subject if token and token.subject else "local"
    audit.info("%s called %s %s", who, tool, {k: v for k, v in args.items() if v not in ("", None)})


def create_server(settings: Settings | None = None, oauth: OAuthSettings | None = None,
                  provider: OIDCAuthProvider | None = None) -> MCPServer:
    """Build the server. With ``oauth``, HTTP requests need a token from OAuth sign-in."""
    settings = settings or Settings.from_env()
    store = ApplicationStore(settings.db_path)
    sync_lock = threading.Lock()
    if oauth:
        provider = provider or OIDCAuthProvider(oauth)
        mcp = MCPServer("Common App", instructions=INSTRUCTIONS, version="0.1.0",
                        auth=oauth.auth_settings(), auth_server_provider=provider)
        mcp.custom_route(CALLBACK_PATH, methods=["GET"])(provider.handle_callback)
    else:
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
        _audit("search_applicants", query=query, offset=offset, **filters)
        total, rows = store.search(query, filters, limit, offset)
        return {"total": total, "offset": offset, "results": rows,
                "has_more": offset + len(rows) < total}

    @mcp.tool(annotations=READ_ONLY)
    def get_applicant(
        common_app_id: Annotated[str, Field(description="The applicant's Common App ID")],
    ) -> dict[str, Any]:
        """Full record(s) for one applicant, including unmapped export columns under 'extra'.
        An applicant can have one application per entry term."""
        _audit("get_applicant", common_app_id=common_app_id)
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
        _audit("application_counts", group_by=group_by, **filters)
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
        _audit("sync_exports")
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


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="commonapp-mcp", description="Common App MCP connector")
    ap.add_argument("--transport", choices=["stdio", "http"], default="stdio")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    args = ap.parse_args(argv)
    # stdout is the protocol channel under stdio, so logs go to stderr.
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    if args.transport == "stdio":
        create_server().run("stdio")
        return

    import uvicorn
    from mcp.server.transport_security import TransportSecuritySettings

    oauth = OAuthSettings.from_env(os.environ)
    local_only = args.host in ("127.0.0.1", "localhost")
    if oauth is None and not local_only:
        raise SystemExit("Refusing to serve applicant data on a public interface without OAuth "
                         "sign-in. Set COMMONAPP_OIDC_ISSUER and friends (see README).")
    if oauth is None:
        log.warning("OAuth is not configured: HTTP server is open to anyone on this machine.")
        security = None  # SDK default: localhost-only host checks
    else:
        public = urlparse(oauth.public_url)
        security = TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=[public.netloc, "127.0.0.1:*", "localhost:*"],
            allowed_origins=[f"{public.scheme}://{public.netloc}", "https://claude.ai", "https://claude.com"],
        )
    mcp = create_server(oauth=oauth)
    app = mcp.streamable_http_app(stateless_http=True, transport_security=security, host=args.host)
    uvicorn.run(app, host=args.host, port=args.port)

if __name__ == "__main__":
    main()
