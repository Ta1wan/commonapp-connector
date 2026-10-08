# commonapp-connector

A Claude connector (MCP server) for Common App applicant data. It loads your
institution's Common App export files (from a local folder or an SFTP drop such
as Common App's SDS delivery) into a local database and lets Claude search and
summarize them:

> *"How many Early Decision applicants do we have for Fall 2027, by intended major?"*
> *"Pull up Ada Lovelace's application."* · *"Load any new export files."*

| Tool | What it does |
| --- | --- |
| `search_applicants` | Search by name / email / Common App ID, filter by term, plan, major, state, school; paginated |
| `get_applicant` | Full record(s) for one Common App ID, including every unmapped export column |
| `application_counts` | Counts grouped by term, decision plan, major, state, country, school, fee status, or file |
| `list_export_files` | Which export files are loaded and how many applications each holds |
| `sync_exports` | Fetch new/changed export files from the configured source and load them |

All tools but `sync_exports` are marked read-only. Nothing is ever written back to Common App.

## Connect it to Claude

Install (Python 3.10+):

```bash
pip install -e '.[sftp]'      # drop [sftp] if you only read a local folder
```

Configure with environment variables (full list in `.env.example`); at minimum
a source, e.g. `COMMONAPP_SOURCE=local COMMONAPP_LOCAL_DIR=/path/to/exports`.

**Claude Desktop** — add to `claude_desktop_config.json`, then restart:

```json
{
  "mcpServers": {
    "commonapp": {
      "command": "/path/to/.venv/bin/commonapp-mcp",
      "env": {
        "COMMONAPP_SOURCE": "sftp",
        "COMMONAPP_SFTP_HOST": "sftp.example.org",
        "COMMONAPP_SFTP_USER": "youruser",
        "COMMONAPP_SFTP_KEY_PATH": "/Users/you/.ssh/commonapp",
        "COMMONAPP_DB_PATH": "/Users/you/commonapp/applications.db",
        "COMMONAPP_STATE_PATH": "/Users/you/commonapp/state.json"
      }
    }
  }
}
```

**Claude Code**:

```bash
claude mcp add commonapp -e COMMONAPP_LOCAL_DIR=/path/to/exports -- /path/to/.venv/bin/commonapp-mcp
```

**Remote (shared server)** — serve over streamable HTTP behind a bearer token:

```bash
COMMONAPP_MCP_TOKEN=$(openssl rand -hex 32) commonapp-mcp --transport http --host 0.0.0.0 --port 8000
# endpoint: https://<your-host>/mcp  (put it behind TLS, e.g. a reverse proxy)
claude mcp add --transport http commonapp https://<your-host>/mcp --header "Authorization: Bearer $TOKEN"
```

The server refuses to bind to a non-localhost address without a token. The
static token works for Claude Code and the Claude API's MCP connector
(`authorization_token`). Adding it as a custom connector on claude.ai requires
OAuth, which this server doesn't implement yet.

### Data protection

Applicant records are FERPA-protected. The local stdio setup keeps all data on
the machine running Claude Desktop/Code; data only leaves it as tool results
in your Claude conversation. Keep `.env`, the database, and state files out of
version control (they're in `.gitignore`), and restrict who holds the token for
a remote deployment.

## Batch CLI (no Claude)

The same pipeline runs standalone, for scheduled loads into other systems:

```bash
# Inspect one export file
commonapp-connector parse path/to/export.txt --field-map examples/field_map.json

# Ingest everything new (configure via env; see .env.example)
export COMMONAPP_SOURCE=sftp COMMONAPP_SFTP_HOST=... COMMONAPP_SFTP_USER=... \
       COMMONAPP_SFTP_KEY_PATH=~/.ssh/commonapp COMMONAPP_JSONL_PATH=./output/apps.jsonl
commonapp-connector sync            # or: sync --dry-run
```

`sync` prints a JSON summary and exits non-zero if any file failed. It's safe
to run on a schedule (cron, Airflow, etc.): each file is recorded by name and
size/mtime fingerprint after delivery, so it's ingested once, re-ingested only
if it changes, and retried on the next run if it failed.

## How parsing works

- Tab, comma, or pipe delimited, header row required; UTF-8 (with/without BOM)
  or Windows-1252.
- Headers are matched case/space/underscore-insensitively against
  `DEFAULT_FIELD_MAP` in `mapping.py`. Export templates are configured per
  institution, so **check the mapping against your own template** and supply
  overrides with `COMMONAPP_FIELD_MAP` (see `examples/field_map.json`).
- Columns the map doesn't claim are kept in `application.extra`.
- A file without a Common App ID column fails; individual bad rows (missing ID,
  unparseable date) are skipped and reported, the rest of the file still loads.
- Duplicate rows for the same applicant + term collapse to the last occurrence.

## Webhook payload

```json
{"source_file": "export_1.txt",
 "applications": [{"applicant": {"common_app_id": "12345678", "first_name": "Ada", ...},
                   "term": "Fall 2027", "decision_plan": "Early Decision", "extra": {...}}]}
```

Sent in batches of 100 with `Authorization: Bearer $COMMONAPP_WEBHOOK_TOKEN`;
429/5xx/connection errors retry with exponential backoff.

## SFTP security

Host keys are verified (`RejectPolicy`) against `~/.ssh/known_hosts` or
`COMMONAPP_SFTP_KNOWN_HOSTS`; add the server's key before the first run.
Applicant data is sensitive (FERPA) — keep output paths, state, and `.env`
out of version control (they're in `.gitignore`).

## Tests

```bash
pip install -e '.[sftp]'
python -m unittest discover -s tests
```
