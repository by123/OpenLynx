import tempfile
import unittest
from pathlib import Path
from unittest import mock


class WebOpenLynxHomeTest(unittest.TestCase):
    def test_settings_api_reads_and_writes_global_openlynx_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / ".openlynx"
            home.mkdir()
            (home / ".env").write_text("TOP_K=3\nMIN_SCORE=0.5\n")

            from fastapi.testclient import TestClient
            from lynx_memory import web

            with mock.patch.object(web, "GLOBAL_DATA_DIR", home):
                app = web.create_app()
                client = TestClient(app)

                settings = client.get("/api/settings").json()
                self.assertEqual(settings["top_k"], 3)
                self.assertEqual(settings["min_score"], 0.5)

                payload = settings | {
                    "top_k": 8,
                    "voyage_api_key": "pa-test",
                    "openai_api_key": "",
                }
                response = client.put("/api/settings", json=payload)

            self.assertEqual(response.status_code, 200)
            text = (home / ".env").read_text()
            self.assertIn("TOP_K='8'", text)
            self.assertIn("VOYAGE_API_KEY='pa-test'", text)

    def test_scopes_api_reports_global_openlynx_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / ".openlynx"
            home.mkdir()

            from fastapi.testclient import TestClient
            from lynx_memory import projects, web

            # Isolate the registry to the temp home and stub the $HOME scan so
            # the test never walks the real machine.
            with mock.patch.object(web, "GLOBAL_DATA_DIR", home), mock.patch.object(
                projects, "GLOBAL_DATA_DIR", home
            ), mock.patch.object(
                projects, "find_project_root", return_value=None
            ), mock.patch.object(
                projects, "scan", return_value=[]
            ):
                app = web.create_app()
                client = TestClient(app)
                scopes = client.get("/api/scopes").json()

            self.assertEqual(scopes["global_dir"], str(home))
            self.assertEqual(scopes["current_id"], "global")
            # With no project markers, the only tab is the global store.
            self.assertEqual([s["kind"] for s in scopes["scopes"]], ["global"])


if __name__ == "__main__":
    unittest.main()
