import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock


def _make_turns_db(path: Path, *, total: int, deleted: int = 0, with_deleted_col: bool = True) -> None:
    """Create a minimal `turns` table with `total` rows (`deleted` soft-deleted)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(path))
    try:
        col = ", deleted_at REAL" if with_deleted_col else ""
        con.execute(f"CREATE TABLE turns (id TEXT PRIMARY KEY{col})")
        for i in range(total):
            if with_deleted_col:
                con.execute(
                    "INSERT INTO turns (id, deleted_at) VALUES (?, ?)",
                    (f"t{i}", 1.0 if i < deleted else None),
                )
            else:
                con.execute("INSERT INTO turns (id) VALUES (?)", (f"t{i}",))
        con.commit()
    finally:
        con.close()


class SqliteTurnCountTest(unittest.TestCase):
    def test_prefers_synced_replica_over_frozen_memory_db(self):
        from lynx_memory import web

        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            db = data_dir / "db"
            # Frozen pre-sync DB is empty; live data lives in the replica.
            _make_turns_db(db / "memory.db", total=0, with_deleted_col=False)
            _make_turns_db(db / "sync-memory.db", total=10, deleted=4)

            # 10 rows, 4 soft-deleted -> 6 active.
            self.assertEqual(web._sqlite_turn_count(data_dir), 6)

    def test_falls_back_to_memory_db_when_no_replica(self):
        from lynx_memory import web

        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            _make_turns_db(data_dir / "db" / "memory.db", total=3, deleted=1)
            self.assertEqual(web._sqlite_turn_count(data_dir), 2)

    def test_zero_when_no_db_at_all(self):
        from lynx_memory import web

        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(web._sqlite_turn_count(Path(tmp)), 0)


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
