import _hermetic  # noqa: F401  (must precede lynx_memory imports)
import json
import os
import sqlite3
import subprocess
import tempfile
import time
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest import mock

FAKE_VEC = [0.1] * 16


def _memory(data_dir):
    with mock.patch("lynx_memory.storage._base.embed_one", return_value=FAKE_VEC):
        from lynx_memory.storage import Memory

        return Memory(data_dir=data_dir)


def _insert_turn(data_dir, tid, session, ts, user, asst, cwd="", summary=None):
    m = _memory(data_dir)
    try:
        m.db.execute(
            "INSERT INTO turns(id, session_id, ts, cwd, user_msg, assistant_msg, summary) "
            "VALUES(?,?,?,?,?,?,?)",
            (tid, session, ts, cwd, user, asst, summary),
        )
        m.db.commit()
    finally:
        m.close()


class TextCleanTest(unittest.TestCase):
    def test_strips_harness_noise(self):
        from lynx_memory.textclean import clean_user_text, is_noise_prompt

        raw = (
            "<system-reminder>CLAUDE.md dump\nlots</system-reminder>\n"
            "帮我修复同步锁的问题\n"
            "<task-notification><task-id>x</task-id></task-notification>"
        )
        self.assertEqual(clean_user_text(raw), "帮我修复同步锁的问题")
        self.assertTrue(is_noise_prompt("<task-notification>done</task-notification>"))
        self.assertFalse(is_noise_prompt("why is the lock busy?"))
        # truncated inside the envelope → nothing human left
        self.assertEqual(clean_user_text("<system-reminder>CLAUDE.md dump cut off mid"), "")

    def test_skill_and_command_wrappers(self):
        from lynx_memory.textclean import clean_user_text

        s = clean_user_text("<skill><name>review-doc</name>long body…</skill> check it")
        self.assertEqual(s, "[skill: review-doc] check it")
        c = clean_user_text("<command-name>/lynx-memory-status</command-name><command-args>x</command-args>")
        self.assertTrue(c.startswith("/lynx-memory-status x"))


class ProjectKeyTest(unittest.TestCase):
    def test_buckets_and_store_root(self):
        from lynx_memory import worklog

        home = str(Path.home())
        self.assertEqual(worklog.project_key_for(home), worklog.MISC_KEY)
        self.assertEqual(worklog.project_key_for(home + "/Desktop"), worklog.MISC_KEY)
        self.assertEqual(worklog.project_key_for("/private/var/folders/ab/T"), worklog.TEMP_KEY)
        self.assertEqual(worklog.project_key_for("/x/y", Path("/repo")), "/repo")

    def test_scratchpad_cwd_decodes_to_real_dir(self):
        from lynx_memory import worklog

        with tempfile.TemporaryDirectory() as td:
            real = Path(td).resolve() / "ios_tools" / "my-app"
            real.mkdir(parents=True)
            enc = "-" + str(real).strip("/").replace("/", "-").replace("_", "-")
            worklog._decode_cache.clear()
            with mock.patch.object(worklog, "_TEMP_PREFIXES", ("/nonexistent/",)):
                key = worklog.project_key_for(f"/private/tmp/claude-501/{enc}/abc/scratchpad")
            self.assertEqual(key, str(real))

    def test_dir_inside_project_store_maps_to_its_root(self):
        from lynx_memory import worklog

        with tempfile.TemporaryDirectory() as td:
            root = Path(td).resolve() / "proj"
            (root / ".lynx-memory").mkdir(parents=True)
            (root / "web" / "src").mkdir(parents=True)
            self.assertEqual(worklog.project_key_for(str(root / "web" / "src")), str(root))


class AutomationAndUnitsTest(unittest.TestCase):
    def _turn(self, tid, sid, ts, user, key="/p"):
        from lynx_memory.worklog import Turn

        return Turn(id=tid, session_id=sid, ts=ts, cwd=key, user=user, note="", assistant_tail="ok",
                    assistant_len=2, store="global", project_key=key)

    def test_templated_single_turn_sessions_are_automation(self):
        from lynx_memory import worklog

        base = time.time() - 3600
        turns = [self._turn(f"a{i}", f"s{i}", base + i, f"Classify feedback #{i}: text") for i in range(4)]
        turns += [self._turn("h1", "sx", base + 10, "real question"),
                  self._turn("h2", "sx", base + 11, "follow-up")]
        worklog.mark_automation(turns)
        self.assertTrue(all(t.automation for t in turns[:4]))
        self.assertFalse(any(t.automation for t in turns[4:]))
        units = worklog.build_units(turns)
        self.assertEqual(len(units), 1)
        self.assertEqual(len(units[0].turns), 2)
        self.assertEqual(units[0].automation_runs, 4)

    def test_input_hash_changes_only_with_input(self):
        from lynx_memory import worklog

        base = time.time() - 3600
        t1 = [self._turn("x1", "s", base, "do A")]
        h1 = worklog.build_units(t1)[0].input_hash()
        self.assertEqual(h1, worklog.build_units([self._turn("x1", "s", base, "do A")])[0].input_hash())
        h2 = worklog.build_units(t1 + [self._turn("x2", "s", base + 5, "do B")])[0].input_hash()
        self.assertNotEqual(h1, h2)

    def test_normalize_maps_evidence_and_drops_unknown_ids(self):
        from lynx_memory.worklog import normalize_unit_result

        out = normalize_unit_result(
            {"headline": "h", "work_items": [{"title": "T", "status": "done", "evidence": ["t1", "t9"]}],
             "facts": [{"type": "pitfall", "text": "x", "evidence": ["t2"]}, {"text": ""}]},
            {"t1": "real-1", "t2": "real-2"},
        )
        self.assertEqual(out["work_items"][0]["evidence"], ["real-1"])
        self.assertEqual(len(out["facts"]), 1)
        self.assertEqual(out["facts"][0]["evidence"], ["real-2"])

    def test_non_string_model_fields_are_coerced(self):
        from lynx_memory.worklog import normalize_unit_result

        out = normalize_unit_result(
            {"headline": ["a", "b"], "work_items": [{"title": "T", "outcome": ["x", "y"], "status": None,
                                                    "decisions": [{"what": {"k": 1}}], "rework": 3}],
             "facts": [{"type": "pitfall", "text": {"en": "boom"}, "anchors": [1, "f.py"]}]}, {})
        self.assertEqual(out["headline"], "a; b")
        self.assertEqual(out["work_items"][0]["outcome"], "x; y")
        self.assertEqual(out["work_items"][0]["status"], "discussed-only")
        self.assertIsInstance(out["work_items"][0]["decisions"][0]["what"], str)
        self.assertEqual(out["work_items"][0]["rework"], {"count": 0, "what": ""})
        self.assertIsInstance(out["facts"][0]["text"], str)
        self.assertEqual(out["facts"][0]["anchors"], ["1", "f.py"])

    def test_parse_json_object_handles_fences(self):
        from lynx_memory.worklog import parse_json_object

        self.assertEqual(parse_json_object('```json\n{"a": 1}\n```'), {"a": 1})
        self.assertIsNone(parse_json_object("no json here"))
        self.assertEqual(parse_json_object('{"a": "line1\nline2"}'), {"a": "line1\nline2"})
        self.assertEqual(parse_json_object('{"a": [1, 2,], }'), {"a": [1, 2]})

    def test_malformed_reply_is_retried_once(self):
        from lynx_memory import worklog

        replies = iter([("not json", "m"), ('{"headline": "ok"}', "m")])
        with mock.patch.object(worklog, "_llm", side_effect=lambda *a, **k: next(replies)), \
                mock.patch("lynx_memory.hooks._log.log"):
            self.assertEqual(worklog._llm_json("s", "u", want=dict), ({"headline": "ok"}, "m"))


class WorklogRunTest(unittest.TestCase):
    """End-to-end over temp stores with the LLM mocked."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name).resolve()
        self.global_dir = root / "home" / ".openlynx"
        self.proj_root = root / "work" / "app"
        self.proj_store = self.proj_root / ".lynx-memory"
        self.global_dir.mkdir(parents=True)
        self.proj_store.mkdir(parents=True)
        self.patches = [
            mock.patch("lynx_memory.config.GLOBAL_DATA_DIR", self.global_dir),
            mock.patch("lynx_memory.config.load_env", lambda *a, **k: None),
            mock.patch("lynx_memory.worklog.discover_sources",
                       lambda: [self.global_dir, self.proj_store]),
            mock.patch.dict(os.environ, {"WORKLOG_LANG": "en"}),
            mock.patch("lynx_memory.worklog._TEMP_PREFIXES", ("/nonexistent/",)),
            mock.patch("lynx_memory.worklog.CARD_MIN_TURNS", 1),
        ]
        for p in self.patches:
            p.start()
        self.calls = []

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self._tmp.cleanup()

    def _fake_llm(self, system, user, fast=False):
        from lynx_memory import worklog

        self.calls.append(system[:20])
        if system == worklog.UNIT_SYSTEM:
            return json.dumps({
                "headline": "shipped the thing",
                "work_items": [{"title": "Fix lock", "type": "bugfix", "outcome": "lock released before LLM",
                                "status": "done", "evidence": ["t1"],
                                "open_loops": [{"text": "verify on Turso", "owner": "user"}]}],
                "facts": [{"type": "pitfall", "text": "holding the replica lock starves hooks",
                           "anchors": ["summarizer._run_one"], "evidence": ["t1"]}],
            }), "sonnet-test"
        return json.dumps({"summary": "A test app", "state": ["v1 in progress"],
                           "open_loops": [{"text": "verify on Turso", "since": "2026-01-01"}]}), "sonnet-test"

    def _seed(self):
        now = time.time()
        old = now - 2 * 86400
        _insert_turn(self.proj_store, "p1", "s1", old, "fix the lock", "done: released lock", cwd=str(self.proj_root))
        _insert_turn(self.proj_store, "p2", "s1", old + 60, "add a test", "test added", cwd=str(self.proj_root))
        _insert_turn(self.global_dir, "g1", "s2", old + 120, "and in the web dir", "ok",
                     cwd=str(self.proj_root / "web"))
        return old

    def test_build_is_idempotent_and_flushes_once(self):
        from lynx_memory import worklog

        self._seed()
        with mock.patch("lynx_memory.worklog._llm", side_effect=self._fake_llm):
            st = worklog.run(days=7, jobs=2, reconcile=False, log=lambda m: None)
        self.assertEqual(st.units_done, 1)          # project-store + global turns → one unit
        self.assertEqual(st.cards_done, 1)
        unit_calls = len(self.calls)
        self.assertEqual(unit_calls, 2)             # 1 unit + 1 card

        out = worklog.worklog_dir()
        days = list((out / "days").glob("*.md"))
        self.assertEqual(len(days), 1)
        text = days[0].read_text()
        self.assertIn("Fix lock", text)
        self.assertIn("holding the replica lock", text)
        self.assertTrue((out / "projects" / "app.md").exists())
        self.assertIn("verify on Turso", (out / "open-loops.md").read_text())
        idx = json.loads((out / "index.json").read_text())
        self.assertIn(str(self.proj_root), idx["projects"])

        # second run: nothing changed → zero LLM calls
        with mock.patch("lynx_memory.worklog._llm", side_effect=self._fake_llm):
            st2 = worklog.run(days=7, jobs=2, reconcile=False, log=lambda m: None)
        self.assertEqual(len(self.calls), unit_calls)
        self.assertEqual(st2.units_todo, 0)

        # a new turn that day → only that unit (and its card) rebuilt
        old = time.time() - 2 * 86400
        _insert_turn(self.proj_store, "p3", "s1", old + 300, "one more", "done", cwd=str(self.proj_root))
        with mock.patch("lynx_memory.worklog._llm", side_effect=self._fake_llm):
            st3 = worklog.run(days=7, jobs=2, reconcile=False, log=lambda m: None)
        self.assertEqual(st3.units_todo, 1)
        self.assertEqual(len(self.calls), unit_calls + 2)

    def test_flush_is_a_single_commit(self):
        from lynx_memory import worklog
        from lynx_memory.storage import Memory

        self._seed()
        commits = []
        real_init = Memory.__init__

        def counting_init(self_, *a, **k):
            real_init(self_, *a, **k)
            inner = self_.db

            class Proxy:
                def __getattr__(_s, name):
                    return getattr(inner, name)

                def commit(_s):
                    commits.append(1)
                    return inner.commit()

            self_.db = Proxy()

        with mock.patch("lynx_memory.worklog._llm", side_effect=self._fake_llm):
            rows = [worklog.run_unit(u) for u in worklog.build_units(
                worklog.snapshot_store(self.proj_store, 0, time.time() + 10))]
        with mock.patch.object(Memory, "__init__", counting_init):
            worklog.flush(rows, [], [])
        self.assertEqual(len(commits), 1)

    def test_partial_day_is_finalized_without_llm(self):
        from lynx_memory import worklog

        now = time.time()
        _insert_turn(self.proj_store, "t1", "s1", now - 60, "today work", "ok", cwd=str(self.proj_root))
        with mock.patch("lynx_memory.worklog._llm", side_effect=self._fake_llm):
            worklog.run(days=2, reconcile=False, cards=False, log=lambda m: None)
        n = len(self.calls)
        tomorrow = date.today() + timedelta(days=1)
        with mock.patch("lynx_memory.worklog._llm", side_effect=self._fake_llm), \
                mock.patch("lynx_memory.worklog._today", return_value=tomorrow):
            st = worklog.run(days=3, reconcile=False, cards=False, log=lambda m: None)
        self.assertEqual(len(self.calls), n)
        self.assertEqual(st.units_finalized, 1)
        m = _memory(self.global_dir)
        try:
            self.assertEqual(m.db.execute("SELECT status FROM worklog_units").fetchone()[0], "final")
        finally:
            m.close()

    def test_failed_units_are_retried_next_run(self):
        from lynx_memory import worklog

        self._seed()
        with mock.patch("lynx_memory.worklog._llm", return_value=None):
            st = worklog.run(days=7, reconcile=False, log=lambda m: None)
        self.assertEqual(st.units_failed, 1)
        with mock.patch("lynx_memory.worklog._llm", side_effect=self._fake_llm):
            st2 = worklog.run(days=7, reconcile=False, log=lambda m: None)
        self.assertEqual(st2.units_done, 1)

    def test_bad_staged_row_does_not_wedge_the_journal(self):
        from lynx_memory import worklog

        self._seed()
        units = worklog.build_units(worklog.snapshot_store(self.proj_store, 0, time.time() + 10))
        with mock.patch("lynx_memory.worklog._llm", side_effect=self._fake_llm):
            row = worklog.run_unit(units[0])
        row["items"][0]["text"] = ["a", "list"]   # what older code could stage
        row["headline"] = {"oops": 1}
        worklog._stage(units[0].id, units[0].input_hash(), row)
        with mock.patch("lynx_memory.worklog._llm", side_effect=self._fake_llm):
            st = worklog.run(days=7, reconcile=False, cards=False, log=lambda m: None)
        self.assertEqual(st.units_done, 1)
        self.assertFalse(worklog._staging_path().exists())

    def test_abort_after_repeated_failures_still_flushes(self):
        from lynx_memory import worklog

        now = time.time()
        for i in range(8):  # 8 separate days → 8 units
            _insert_turn(self.proj_store, f"d{i}", f"s{i}", now - (i + 1) * 86400,
                         f"{'abcdefgh'[i]} distinct task", "ok", cwd=str(self.proj_root))
        calls = {"n": 0}

        def flaky(system, user, fast=False):
            calls["n"] += 1
            return self._fake_llm(system, user) if calls["n"] == 1 else None

        with mock.patch("lynx_memory.worklog._llm", side_effect=flaky), \
                mock.patch("lynx_memory.worklog.MAX_CONSECUTIVE_FAILURES", 2):
            st = worklog.run(days=10, jobs=1, reconcile=False, log=lambda m: None)
        self.assertTrue(st.aborted)
        self.assertEqual(st.units_done, 1)
        m = _memory(self.global_dir)
        try:
            self.assertEqual(m.db.execute("SELECT COUNT(*) FROM worklog_units").fetchone()[0], 1)
        finally:
            m.close()

    def test_narrow_run_keeps_unrelated_staged_results(self):
        from lynx_memory import worklog

        worklog._stage("day:2020-01-01:/elsewhere", "h", {"id": "x"})
        self._seed()
        with mock.patch("lynx_memory.worklog._llm", side_effect=self._fake_llm):
            worklog.run(days=7, reconcile=False, log=lambda m: None)
        self.assertIn("day:2020-01-01:/elsewhere", worklog._load_staging())

    def test_brief_for_cwd_reads_exported_card(self):
        from lynx_memory import worklog

        self._seed()
        with mock.patch("lynx_memory.worklog._llm", side_effect=self._fake_llm):
            worklog.run(days=7, reconcile=False, log=lambda m: None)
        brief = worklog.brief_for_cwd(str(self.proj_root / "web"))
        self.assertIsNotNone(brief)
        self.assertIn("A test app", brief)
        self.assertIn("verify on Turso", brief)
        self.assertIsNone(worklog.brief_for_cwd("/somewhere/else"))


class ReconcileTest(unittest.TestCase):
    SCHEMA = (
        "CREATE TABLE turns(id TEXT PRIMARY KEY, session_id TEXT, ts REAL, cwd TEXT, user_msg TEXT, "
        "assistant_msg TEXT, user_uuid TEXT, summary TEXT, deleted_at REAL);"
        "CREATE UNIQUE INDEX u ON turns(session_id, user_uuid) WHERE user_uuid IS NOT NULL;"
        "CREATE TABLE sessions(id TEXT PRIMARY KEY, started_at REAL, ended_at REAL, cwd TEXT);"
        "CREATE TABLE summaries(id TEXT PRIMARY KEY, session_id TEXT, ts REAL, summary TEXT, "
        "turn_count INTEGER, deleted_at REAL);"
    )

    def _db(self):
        c = sqlite3.connect(":memory:")
        c.row_factory = sqlite3.Row
        c.executescript(self.SCHEMA)
        return c

    def test_merges_only_post_watermark_rows(self):
        from lynx_memory.reconcile import reconcile_rows

        local, target = self._db(), self._db()
        ins = "INSERT INTO turns(id, session_id, ts, user_msg, assistant_msg, user_uuid) VALUES(?,?,?,?,?,?)"
        # migrated at sync-init (in both)
        for c in (local, target):
            c.execute(ins, ("m1", "s0", 100.0, "u", "a", "uu0"))
        # deleted from the replica after sync started (older than watermark) → must stay deleted
        local.execute(ins, ("old-deleted", "s0", 50.0, "u", "a", "uu-old"))
        # stranded fallback writes
        local.execute(ins, ("f1", "s1", 200.0, "u1", "a1", "uu1"))
        local.execute("INSERT INTO sessions VALUES('s1', 199.0, NULL, '/x')")
        # same turn written twice: replica has an older/shorter copy → update in place
        target.execute(ins, ("r2", "s2", 150.0, "u2", "short", "uu2"))
        local.execute(ins, ("f2", "s2", 210.0, "u2", "short + final answer", "uu2"))
        # identical duplicate → skipped
        target.execute(ins, ("r3", "s3", 160.0, "u3", "same", "uu3"))
        local.execute(ins, ("f3", "s3", 220.0, "u3", "same", "uu3"))
        local.execute("INSERT INTO summaries VALUES('sum1','s1',230.0,'sess summary',2,NULL)")

        res = reconcile_rows(local, target)
        target.commit()
        self.assertEqual((res["inserted"], res["updated"], res["summaries"]), (1, 1, 1))
        ids = {r[0] for r in target.execute("SELECT id FROM turns")}
        self.assertIn("f1", ids)
        self.assertNotIn("old-deleted", ids)
        self.assertNotIn("f2", ids)
        self.assertEqual(target.execute("SELECT assistant_msg FROM turns WHERE id='r2'").fetchone()[0],
                         "short + final answer")
        self.assertEqual(target.execute("SELECT COUNT(*) FROM sessions WHERE id='s1'").fetchone()[0], 1)
        # idempotent
        res2 = reconcile_rows(local, target)
        self.assertEqual((res2["inserted"], res2["updated"]), (0, 0))

    def test_update_keeps_replica_note_and_never_revives_deleted(self):
        from lynx_memory.reconcile import reconcile_rows

        local, target = self._db(), self._db()
        ins = ("INSERT INTO turns(id, session_id, ts, user_msg, assistant_msg, user_uuid, summary, deleted_at) "
               "VALUES(?,?,?,?,?,?,?,?)")
        for c in (local, target):
            c.execute(ins, ("m1", "s0", 100.0, "u", "a", "uu0", None, None))
        target.execute(ins, ("r1", "s1", 150.0, "u", "short", "uu1", "the note", None))
        local.execute(ins, ("f1", "s1", 200.0, "u", "short + final", "uu1", None, None))
        target.execute(ins, ("r2", "s2", 150.0, "u", "x", "uu2", "note2", 160.0))  # deleted in replica
        local.execute(ins, ("f2", "s2", 210.0, "u", "x + more", "uu2", None, None))
        res = reconcile_rows(local, target)
        self.assertEqual(res["updated"], 1)
        r1 = target.execute("SELECT assistant_msg, summary, deleted_at FROM turns WHERE id='r1'").fetchone()
        self.assertEqual(tuple(r1), ("short + final", "the note", None))
        r2 = target.execute("SELECT assistant_msg, deleted_at FROM turns WHERE id='r2'").fetchone()
        self.assertEqual(tuple(r2), ("x", 160.0))

    def test_no_overlap_uses_replica_creation_time(self):
        from lynx_memory.reconcile import reconcile_rows

        local, target = self._db(), self._db()
        ins = "INSERT INTO turns(id, session_id, ts, user_msg, assistant_msg) VALUES(?,?,?,?,?)"
        target.execute(ins, ("r1", "s0", 100.0, "u", "a"))
        local.execute(ins, ("before", "s1", 40.0, "u", "a"))
        local.execute(ins, ("after", "s2", 300.0, "u", "a"))
        self.assertIn("skipped", reconcile_rows(local, target, dry_run=True))
        res = reconcile_rows(local, target, replica_created=50.0)
        self.assertEqual(res["inserted"], 1)
        ids = {r[0] for r in target.execute("SELECT id FROM turns")}
        self.assertEqual(ids, {"r1", "after"})

    def test_dry_run_writes_nothing(self):
        from lynx_memory.reconcile import reconcile_rows

        local, target = self._db(), self._db()
        ins = "INSERT INTO turns(id, session_id, ts, user_msg, assistant_msg) VALUES(?,?,?,?,?)"
        for c in (local, target):
            c.execute(ins, ("m1", "s0", 100.0, "u", "a"))
        local.execute(ins, ("f1", "s1", 200.0, "u", "a"))
        res = reconcile_rows(local, target, dry_run=True)
        self.assertEqual(res["inserted"], 1)
        self.assertEqual(target.execute("SELECT COUNT(*) FROM turns").fetchone()[0], 1)


class ClaudeCliTest(unittest.TestCase):
    def test_command_isolates_the_child(self):
        from lynx_memory import claude_cli

        with mock.patch.object(claude_cli, "claude_bin", return_value="/bin/claude"):
            cmd = claude_cli.build_command("SYS", "haiku")
        self.assertEqual(cmd[:2], ["/bin/claude", "-p"])
        self.assertIn("--no-session-persistence", cmd)
        self.assertIn("--strict-mcp-config", cmd)
        self.assertEqual(cmd[cmd.index("--tools") + 1], "")
        self.assertEqual(json.loads(cmd[cmd.index("--settings") + 1]), {"disableAllHooks": True})
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "x", "CLAUDE_CODE_OAUTH_TOKEN": "t"}):
            env = claude_cli.child_env()
        self.assertEqual(env["LYNX_MEMORY_NO_HOOK"], "1")
        self.assertNotIn("CLAUDE_CODE_SESSION_ID", env)
        self.assertEqual(env["CLAUDE_CODE_OAUTH_TOKEN"], "t")

    def _proc(self, rc, payload):
        return subprocess.CompletedProcess([], rc, stdout=json.dumps(payload), stderr="")

    def setUp(self):
        p = mock.patch("lynx_memory.hooks._log.log")
        p.start()
        self.addCleanup(p.stop)

    def test_run_parses_result_and_errors(self):
        from lynx_memory import claude_cli

        ok = {"result": " note ", "is_error": False, "modelUsage": {"claude-haiku-x": {}}}
        with mock.patch.object(claude_cli, "claude_bin", return_value="/bin/claude"), \
                mock.patch("subprocess.run", return_value=self._proc(0, ok)) as run:
            self.assertEqual(claude_cli.run("s", "u", fast=True), ("note", "claude-haiku-x"))
            self.assertEqual(run.call_args.kwargs["input"], "u")
        bad = {"result": "rate limited", "is_error": True}
        with mock.patch.object(claude_cli, "claude_bin", return_value="/bin/claude"), \
                mock.patch("subprocess.run", return_value=self._proc(1, bad)):
            self.assertIsNone(claude_cli.run("s", "u"))
        with mock.patch.object(claude_cli, "claude_bin", return_value="/bin/claude"), \
                mock.patch("subprocess.run", side_effect=subprocess.TimeoutExpired("c", 1)):
            self.assertIsNone(claude_cli.run("s", "u"))


class ClaudeBackendTest(unittest.TestCase):
    def test_forced_claude_backend_never_falls_back(self):
        from lynx_memory import summarizer

        env = {"SUMMARY_BACKEND": "claude", "DEEPSEEK_API_KEY": "sk"}
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch("lynx_memory.claude_cli.available", return_value=True), \
                mock.patch("lynx_memory.claude_cli.run", return_value=None) as run, \
                mock.patch.object(summarizer, "_summarize_via_deepseek") as ds:
            self.assertEqual(summarizer.active_provider(), "claude")
            self.assertIsNone(summarizer.summarize_with_source("question?", "answer"))
            run.assert_called_once()
            ds.assert_not_called()

    def test_goal_judge_is_deferred_for_claude(self):
        from lynx_memory import goals

        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            m = _memory(d)
            m.set_goal("ship v2")
            m.close()
            with mock.patch.dict(os.environ, {"SUMMARY_BACKEND": "claude"}, clear=True), \
                    mock.patch("lynx_memory.claude_cli.available", return_value=True), \
                    mock.patch("lynx_memory.config.load_env", lambda *a, **k: None), \
                    mock.patch("lynx_memory.summarizer.judge_relevance") as judge:
                self.assertEqual(goals.evaluate_turn_relevance(d, "u", "a"), "defer")
                judge.assert_not_called()

    def test_worker_does_not_hold_store_open_during_llm(self):
        from lynx_memory import summarizer
        from lynx_memory.storage import Memory

        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            _insert_turn(d, "t1", "s", time.time(), "q", "a")
            open_count = {"n": 0}
            real_init, real_close = Memory.__init__, Memory.close

            def init(self_, *a, **k):
                real_init(self_, *a, **k)
                open_count["n"] += 1

            def close(self_):
                open_count["n"] -= 1
                return real_close(self_)

            def fake_summarize(*a, **k):
                self.assertEqual(open_count["n"], 0, "store held open across the LLM call")
                return ("note", "claude", "haiku")

            with mock.patch.object(Memory, "__init__", init), mock.patch.object(Memory, "close", close), \
                    mock.patch.object(summarizer, "summarize_with_source", side_effect=fake_summarize), \
                    mock.patch("lynx_memory.config.load_env", lambda *a, **k: None), \
                    mock.patch.dict(os.environ, {"SUMMARY_ENABLED": "1"}):
                self.assertEqual(summarizer._run_one(str(d), "t1"), 0)
            m = _memory(d)
            try:
                self.assertEqual(m.get_turn("t1")["summary"], "note")
            finally:
                m.close()

    def test_deferred_judge_keeps_row_reingested_meanwhile(self):
        from lynx_memory import summarizer

        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            _insert_turn(d, "t1", "s", 1000.0, "q", "early partial reply")
            m = _memory(d)
            m.set_goal("ship v2")
            m.close()

            def judge_then_reingest(*a, **k):
                mm = _memory(d)
                mm.db.execute("UPDATE turns SET assistant_msg='final reply', ts=2000.0 WHERE id='t1'")
                mm.db.commit()
                mm.close()
                return False

            with mock.patch.object(summarizer, "judge_relevance", side_effect=judge_then_reingest), \
                    mock.patch("lynx_memory.config.load_env", lambda *a, **k: None), \
                    mock.patch("lynx_memory.storage._base.embed_one", return_value=FAKE_VEC):
                summarizer._run_one(str(d), "t1", judge=True)
            m = _memory(d)
            try:
                self.assertEqual(m.get_turn("t1")["assistant_msg"], "final reply")
            finally:
                m.close()

    def test_deferred_judge_soft_deletes_irrelevant_turn(self):
        from lynx_memory import summarizer

        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            _insert_turn(d, "t1", "s", time.time(), "chit chat", "hi")
            m = _memory(d)
            m.set_goal("ship v2")
            m.close()
            with mock.patch.object(summarizer, "judge_relevance", return_value=False), \
                    mock.patch.object(summarizer, "summarize_with_source") as summ, \
                    mock.patch("lynx_memory.config.load_env", lambda *a, **k: None), \
                    mock.patch("lynx_memory.storage._base.embed_one", return_value=FAKE_VEC):
                self.assertEqual(summarizer._run_one(str(d), "t1", judge=True), 0)
                summ.assert_not_called()
            m = _memory(d)
            try:
                self.assertIsNone(m.get_turn("t1"))
            finally:
                m.close()


class ScheduleTest(unittest.TestCase):
    def test_plist_and_times(self):
        import plistlib

        from lynx_memory import schedule

        self.assertEqual(schedule.parse_times(["8:30", "20:30", "20:30"]), [(8, 30), (20, 30)])
        with self.assertRaises(ValueError):
            schedule.parse_times(["25:00"])
        with mock.patch("lynx_memory.claude_cli.claude_bin", return_value="/u/bin/claude"):
            d = plistlib.loads(schedule.build_plist([(20, 30), (8, 30)]))
        self.assertEqual(d["Label"], schedule.LABEL)
        self.assertEqual(d["ProgramArguments"][:2], ["/usr/bin/caffeinate", "-i"])
        self.assertEqual(d["ProgramArguments"][-3:], ["lynx_memory.cli", "worklog", "run"])
        self.assertEqual(d["StartCalendarInterval"], [{"Hour": 20, "Minute": 30}, {"Hour": 8, "Minute": 30}])
        self.assertEqual(d["EnvironmentVariables"]["CLAUDE_BIN"], "/u/bin/claude")
        self.assertIn("/u/bin", d["EnvironmentVariables"]["PATH"])


class DiscoverStoresTest(unittest.TestCase):
    def test_replica_only_store_is_found(self):
        from lynx_memory import daily

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            db = root / "synced" / ".lynx-memory" / "db"
            db.mkdir(parents=True)
            (db / "sync-memory.db").write_text("x")
            with mock.patch("lynx_memory.config.GLOBAL_DATA_DIR", root / "no-global"):
                stores = daily.discover_stores(roots=[root], max_depth=4)
        self.assertEqual([s.parent.name for s in stores], ["synced"])


if __name__ == "__main__":
    unittest.main()
