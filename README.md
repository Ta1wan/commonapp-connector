# commonapp-connector

Pulls Common App applicant data exports into your own systems: fetch export
files (from a local folder or an SFTP drop such as Common App's SDS delivery),
normalize each row into an `Application` record, and deliver it to JSONL,
CSV, or an HTTP webhook (your CRM/SIS ingest endpoint).

```
source (local | sftp)  ->  ExportParser (field map)  ->  sinks (jsonl | csv | webhook)
                                  ^
                     StateStore: skip files already ingested
```

## Install

```bash
pip install -e .            # core
pip install -e '.[sftp]'    # + SFTP support (paramiko)
```

## Use

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
PYTHONPATH=src python -m unittest discover -s tests
```
