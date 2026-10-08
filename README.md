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

Every tool call is written to the `commonapp_connector.audit` log with the signed-in user's email.

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

**claude.ai (custom connector, OAuth sign-in)** — see [Remote setup with OAuth](#remote-setup-with-oauth) below.

### Data protection

Applicant records are FERPA-protected. The local stdio setup keeps all data on
the machine running Claude Desktop/Code; data only leaves it as tool results
in your Claude conversation. Keep `.env`, the database, and state files out of
version control (they're in `.gitignore`), and restrict who holds the token for
a remote deployment.

## Remote setup with OAuth

Deployed on a server, the connector uses OAuth so each person signs in with
their institution account. Claude registers itself automatically (dynamic
client registration); the sign-in itself happens at your identity provider,
and only allowlisted emails or domains get access:

```
Claude ── /authorize ──▶ connector ── redirect ──▶ your IdP login (Google / Entra / Okta …)
Claude ◀── code ─────── /oauth/callback ◀── code ── your IdP
```

1. **Create an OAuth app at your identity provider** (a "Web application"
   OIDC client) with redirect URI `https://<your-host>/oauth/callback` and
   scopes `openid email profile`. Note its client ID, client secret, and issuer:

   | Provider | `COMMONAPP_OIDC_ISSUER` |
   | --- | --- |
   | Google Workspace | `https://accounts.google.com` |
   | Microsoft Entra ID | `https://login.microsoftonline.com/<tenant-id>/v2.0` (use your tenant, not `common`) |
   | Okta | `https://<org>.okta.com` (or your custom authorization server URL) |

2. **Run the connector** behind HTTPS (e.g. a reverse proxy forwarding to port 8000):

   ```bash
   export COMMONAPP_PUBLIC_URL=https://commonapp.example.edu
   export COMMONAPP_OIDC_ISSUER=https://accounts.google.com
   export COMMONAPP_OIDC_CLIENT_ID=... COMMONAPP_OIDC_CLIENT_SECRET=...
   export COMMONAPP_ALLOWED_DOMAINS=example.edu            # and/or
   export COMMONAPP_ALLOWED_EMAILS=dean@example.edu,reader@example.edu
   commonapp-mcp --transport http --host 0.0.0.0 --port 8000
   ```

3. **Add it in Claude**: claude.ai → Settings → Connectors → *Add custom
   connector* → URL `https://commonapp.example.edu/mcp`. Claude sends you to your
   institution's login; after signing in, the tools appear. From Claude Code:
   `claude mcp add --transport http commonapp https://commonapp.example.edu/mcp`,
   then `/mcp` to sign in.

Security details:

- The server refuses to listen on a non-localhost address unless OAuth is
  configured, and refuses to start without an email/domain allowlist or with a
  non-HTTPS public URL.
- Sign-in requires the IdP to report an email that isn't marked unverified.
  Entra ID doesn't verify the `email` claim, so with Entra use your
  tenant-specific issuer (as above) so only your tenant's accounts can sign in.
- PKCE is used on both legs. Access tokens last 1 hour; refresh tokens 30 days
  and rotate on every use (the old pair stops working). Tokens and codes are
  stored only as SHA-256 hashes in `COMMONAPP_AUTH_DB_PATH`.
- Clients may only register redirect URIs on `claude.ai`, `claude.com`, or
  localhost (`COMMONAPP_ALLOWED_REDIRECT_HOSTS` to change), so a stranger can't
  register a client that sends a staff member's token to their own site.

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
