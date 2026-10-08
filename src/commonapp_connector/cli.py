"""Command-line entry point: ``commonapp-connector``."""

from __future__ import annotations

import argparse
import json
import logging
import sys

from .config import Settings
from .mapping import load_field_map
from .parser import ExportParser, ParseReport
from .state import StateStore
from .sync import SyncRunner


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="commonapp-connector", description=__doc__)
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="command", required=True)

    sync = sub.add_parser("sync", help="Ingest new export files into the configured sinks")
    sync.add_argument("--dry-run", action="store_true", help="Parse but don't write or record state")

    parse = sub.add_parser("parse", help="Parse one export file and print JSON lines")
    parse.add_argument("file")
    parse.add_argument("--field-map", help="JSON field map overrides")

    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    if args.command == "parse":
        report = ParseReport()
        parser = ExportParser(load_field_map(args.field_map))
        for app in parser.parse_file(args.file, report):
            print(json.dumps(app.to_dict()))
        for err in report.errors:
            print(f"line {err.line}: {err.message}", file=sys.stderr)
        return 1 if report.errors else 0

    settings = Settings.from_env()
    sinks = settings.build_sinks()
    parser = ExportParser(load_field_map(settings.field_map_path))
    with settings.build_source() as source:
        result = SyncRunner(source, sinks, StateStore(settings.state_path), parser,
                            dry_run=args.dry_run).run()
    print(json.dumps({
        "files": [f.__dict__ for f in result.files],
        "skipped": result.skipped,
        "failed": result.failed,
        "total_applications": result.total_applications,
    }, indent=2))
    return 1 if result.failed else 0


if __name__ == "__main__":
    sys.exit(main())
