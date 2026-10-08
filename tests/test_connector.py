import json
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

from commonapp_connector.mapping import load_field_map
from commonapp_connector.parser import ExportParser, ParseReport, parse_date
from commonapp_connector.sinks import CsvSink, JsonlSink, WebhookSink
from commonapp_connector.sources import LocalDirectorySource
from commonapp_connector.state import StateStore
from commonapp_connector.sync import SyncRunner

FIXTURE = Path(__file__).parent / "fixtures" / "sample_export.txt"


class ParserTests(unittest.TestCase):
    def test_parses_rows_and_reports_errors(self):
        report = ParseReport()
        apps = list(ExportParser().parse_file(FIXTURE, report))
        self.assertEqual([a.applicant.first_name for a in apps], ["Ada", "Alan", "Ada"])
        ada = apps[0]
        self.assertEqual(ada.applicant.common_app_id, "12345678")
        self.assertEqual(ada.applicant.birth_date, date(2008, 12, 10))
        self.assertEqual(ada.decision_plan, "Early Decision")
        self.assertEqual(ada.extra, {"Favorite Color": "blue"})
        self.assertEqual(apps[1].applicant.birth_date, date(2008, 6, 23))
        self.assertEqual([e.line for e in report.errors], [4, 5])
        self.assertEqual(report.unmapped_columns, ["Favorite Color"])

    def test_comma_delimited_and_custom_map(self):
        with tempfile.TemporaryDirectory() as d:
            mp = Path(d) / "map.json"
            mp.write_text(json.dumps({"applicant.common_app_id": ["Student Ref"]}))
            text = 'Student Ref,First Name,Term\n"999",Kim,"Fall 2027"\n'
            apps = list(ExportParser(load_field_map(mp)).parse_text(text))
        self.assertEqual(apps[0].applicant.common_app_id, "999")
        self.assertEqual(apps[0].term, "Fall 2027")

    def test_missing_id_column_raises(self):
        with self.assertRaises(ValueError):
            list(ExportParser().parse_text("First Name\nKim\n"))

    def test_unknown_field_map_key_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            mp = Path(d) / "map.json"
            mp.write_text(json.dumps({"applicant.shoe_size": "Shoe"}))
            with self.assertRaises(ValueError):
                load_field_map(mp)

    def test_parse_date(self):
        self.assertIsNone(parse_date(""))
        self.assertEqual(parse_date("20270501"), date(2027, 5, 1))


class SyncTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.inbox = self.root / "inbox"
        self.inbox.mkdir()
        (self.inbox / "export_1.txt").write_bytes(FIXTURE.read_bytes())

    def tearDown(self):
        self.tmp.cleanup()

    def runner(self, **kw):
        return SyncRunner(
            LocalDirectorySource(self.inbox),
            [JsonlSink(self.root / "out.jsonl"), CsvSink(self.root / "csv")],
            StateStore(self.root / "state.json"),
            **kw,
        )

    def test_sync_dedupes_writes_and_is_idempotent(self):
        result = self.runner().run()
        self.assertEqual(result.total_applications, 2)  # duplicate Ada collapsed, latest wins
        lines = [json.loads(l) for l in (self.root / "out.jsonl").read_text().splitlines()]
        emails = {l["applicant"]["email"] for l in lines}
        self.assertEqual(emails, {"alan@example.com", "ada.new@example.com"})
        self.assertTrue((self.root / "csv" / "export_1.csv").exists())

        again = self.runner().run()
        self.assertEqual(again.skipped, ["export_1.txt"])
        self.assertEqual(len((self.root / "out.jsonl").read_text().splitlines()), 2)

    def test_dry_run_writes_nothing(self):
        result = self.runner(dry_run=True).run()
        self.assertEqual(result.total_applications, 2)
        self.assertFalse((self.root / "out.jsonl").exists())
        self.assertFalse((self.root / "state.json").exists())

    def test_bad_file_recorded_as_failed_and_retried(self):
        (self.inbox / "bad.txt").write_text("No Id Here\nx\n")
        result = self.runner().run()
        self.assertIn("bad.txt", result.failed)
        self.assertIn("bad.txt", self.runner().run().failed)


class WebhookTests(unittest.TestCase):
    def test_batches_and_retries_5xx(self):
        apps = list(ExportParser().parse_file(FIXTURE))
        session = mock.Mock()
        session.headers = {}
        ok = mock.Mock(status_code=200)
        session.post.side_effect = [mock.Mock(status_code=503), ok, ok]
        sink = WebhookSink("https://example.test/ingest", token="t", batch_size=2, session=session)
        with mock.patch("commonapp_connector.sinks.time.sleep"):
            sink.write(apps, "f.txt")
        self.assertEqual(session.post.call_count, 3)
        self.assertEqual(session.headers["Authorization"], "Bearer t")
        sizes = [len(c.kwargs["json"]["applications"]) for c in session.post.call_args_list]
        self.assertEqual(sizes, [2, 2, 1])


if __name__ == "__main__":
    unittest.main()
