import shutil
import tempfile
import unittest
from pathlib import Path

import anyio
from mcp import Client

from commonapp_connector.config import Settings
from commonapp_connector.server import create_server

FIXTURE = Path(__file__).parent / "fixtures" / "sample_export.txt"


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        (root / "inbox").mkdir()
        shutil.copy(FIXTURE, root / "inbox" / "export_1.txt")
        self.settings = Settings(local_dir=str(root / "inbox"), state_path=str(root / "state.json"),
                                 db_path=str(root / "apps.db"))

    def tearDown(self):
        self.tmp.cleanup()

    def call(self, *calls):
        """Run tool calls in order over a real MCP session; return their results."""
        async def go():
            async with Client(create_server(self.settings)) as client:
                return [await client.call_tool(name, args) for name, args in calls]
        return anyio.run(go)

    def test_lists_tools_with_read_only_hints(self):
        async def go():
            async with Client(create_server(self.settings)) as client:
                return (await client.list_tools()).tools
        tools = {t.name: t for t in anyio.run(go)}
        self.assertEqual(set(tools), {"search_applicants", "get_applicant", "application_counts",
                                      "list_export_files", "sync_exports"})
        self.assertTrue(tools["search_applicants"].annotations.read_only_hint)
        self.assertFalse(tools["sync_exports"].annotations.read_only_hint)

    def test_sync_then_query(self):
        sync, again, search, person, counts, files = self.call(
            ("sync_exports", {}),
            ("sync_exports", {}),
            ("search_applicants", {"query": "lovelace"}),
            ("get_applicant", {"common_app_id": "12345678"}),
            ("application_counts", {"group_by": "decision_plan", "term": "fall 2027"}),
            ("list_export_files", {}),
        )
        self.assertEqual(sync.structured_content["total_applications_loaded"], 2)
        self.assertEqual(again.structured_content["already_loaded"], 1)
        self.assertEqual(search.structured_content["total"], 1)
        self.assertEqual(search.structured_content["results"][0]["email"], "ada.new@example.com")
        app = person.structured_content["applications"][0]
        self.assertEqual(app["extra"], {"Favorite Color": "blue"})
        self.assertEqual({g["value"]: g["count"] for g in counts.structured_content["groups"]},
                         {"Early Decision": 1, "Regular Decision": 1})
        self.assertEqual(files.structured_content["files"],
                         [{"source_file": "export_1.txt", "applications": 2}])

    def test_errors_reach_the_model(self):
        missing, bad_group = self.call(
            ("get_applicant", {"common_app_id": "000"}),
            ("application_counts", {"group_by": "email"}),
        )
        self.assertTrue(missing.is_error)
        self.assertIn("No applicant", missing.content[0].text)
        self.assertTrue(bad_group.is_error)

    def test_misconfigured_source_is_reported(self):
        self.settings.source = "ftp"
        (result,) = self.call(("sync_exports", {}))
        self.assertTrue(result.is_error)
        self.assertIn("misconfigured", result.content[0].text)

    def test_search_pagination(self):
        _, page = self.call(("sync_exports", {}), ("search_applicants", {"limit": 1}))
        self.assertEqual(page.structured_content["total"], 2)
        self.assertTrue(page.structured_content["has_more"])


if __name__ == "__main__":
    unittest.main()
