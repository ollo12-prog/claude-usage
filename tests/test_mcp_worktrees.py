"""MCP/plugin attribution and worktree project folding (ideas from upstream PR #179)."""
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import scanner
from scanner import get_db, init_db, project_name_from_cwd, scan
from tests.test_scanner import _make_assistant_record


class TestMcpAttribution(unittest.TestCase):
    ATTR = {"attributionMcpServer": "plane", "attributionMcpTool": "workitem",
            "attributionPlugin": "bc-integration"}

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.projects = self.tmp / "projects"
        self.projects.mkdir()
        rec = json.loads(_make_assistant_record(message_id="msg-mcp"))
        rec.update(self.ATTR)
        plain = _make_assistant_record(message_id="msg-plain")
        self.transcript = self.projects / "t.jsonl"
        self.transcript.write_text(json.dumps(rec) + "\n" + plain + "\n", encoding="utf-8")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _row(self, db, mid):
        conn = get_db(db)
        row = conn.execute("SELECT mcp_server, mcp_tool, plugin FROM turns "
                           "WHERE message_id = ?", (mid,)).fetchone()
        conn.close()
        return tuple(row)

    def test_fresh_scan_captures_inline(self):
        db = self.tmp / "fresh.db"
        scan(projects_dir=self.projects, db_path=db, verbose=False)
        self.assertEqual(self._row(db, "msg-mcp"), ("plane", "workitem", "bc-integration"))
        self.assertEqual(self._row(db, "msg-plain"), ("", "", ""))

    def test_backfill_fills_an_already_processed_file(self):
        db = self.tmp / "old.db"
        conn = get_db(db)
        init_db(conn)
        conn.execute("""
            INSERT INTO turns (session_id, timestamp, model, input_tokens,
                output_tokens, cache_read_tokens, cache_creation_tokens,
                tool_name, cwd, message_id)
            VALUES ('sess-1', '2026-04-08T10:00:00Z', 'claude-sonnet-4-6',
                    100, 50, 10, 5, NULL, '/tmp', 'msg-mcp')
        """)
        # A pre-column advisor row: its synthetic id matches no transcript record.
        conn.execute("""
            INSERT INTO turns (session_id, timestamp, model, input_tokens,
                output_tokens, cache_read_tokens, cache_creation_tokens,
                tool_name, cwd, message_id)
            VALUES ('sess-1', '2026-04-08T10:00:00Z', 'claude-fable-5',
                    9, 9, 0, 0, NULL, '/tmp', 'advisor:msg-mcp:1')
        """)
        conn.execute("INSERT INTO processed_files (path, mtime, lines) VALUES (?, ?, ?)",
                     (str(self.transcript), os.path.getmtime(self.transcript), 2))
        for key in ("advisor_reparse_done", "topic_backfill_done", "agent_type_backfill_done"):
            conn.execute("INSERT OR REPLACE INTO schema_meta (key, value) VALUES (?, '1')", (key,))
        conn.commit()
        conn.close()
        scan(projects_dir=self.projects, db_path=db, verbose=False)
        self.assertEqual(self._row(db, "msg-mcp"), ("plane", "workitem", "bc-integration"))
        self.assertEqual(self._row(db, "advisor:msg-mcp:1"), ("plane", "workitem", "bc-integration"))

    def test_dashboard_groups_cost_by_server(self):
        import dashboard
        db = self.tmp / "fresh.db"
        scan(projects_dir=self.projects, db_path=db, verbose=False)
        rows = dashboard.get_dashboard_data(db_path=db)["mcp_by_model"]
        self.assertEqual([(r["tool"], r["turns"]) for r in rows], [("plane", 1)])
        self.assertIn('mcp-cost-body', dashboard.HTML_TEMPLATE)


def _git(*args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@unittest.skipUnless(shutil.which("git"), "git not installed")
class TestWorktreeProjectName(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.main = self.tmp / "work" / "myrepo"
        self.main.mkdir(parents=True)
        _git("init", "-q", cwd=self.main)
        _git("-c", "user.email=t@t", "-c", "user.name=t",
             "commit", "-q", "--allow-empty", "-m", "init", cwd=self.main)
        self.wt = self.tmp / "work" / "myrepo-lane2"
        _git("worktree", "add", "-q", str(self.wt), cwd=self.main)
        (self.main / "sub").mkdir()
        scanner.worktree_main_root.cache_clear()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_linked_worktree_folds_into_main_repo(self):
        self.assertEqual(project_name_from_cwd(str(self.wt)), "work/myrepo")

    def test_main_checkout_and_its_subfolders_keep_path_names(self):
        self.assertEqual(project_name_from_cwd(str(self.main)), "work/myrepo")
        self.assertEqual(project_name_from_cwd(str(self.main / "sub")), "myrepo/sub")

    def test_claude_worktree_path_folds_even_when_deleted(self):
        self.assertEqual(
            project_name_from_cwd(r"C:\gone\proj\.claude\worktrees\agent-abc"), "gone/proj")

    def test_backfill_renames_only_worktree_sessions(self):
        conn = get_db(self.tmp / "u.db")
        init_db(conn)
        for sid, cwd, name in (("s-wt", str(self.wt), "work/myrepo-lane2"),
                               ("s-sub", str(self.main / "sub"), "myrepo/sub")):
            conn.execute("INSERT INTO sessions (session_id, project_name) VALUES (?, ?)", (sid, name))
            conn.execute("""INSERT INTO turns (session_id, timestamp, model, input_tokens,
                output_tokens, cache_read_tokens, cache_creation_tokens, tool_name, cwd, message_id)
                VALUES (?, '2026-09-22T10:00:00Z', 'claude-opus-5-5', 1, 1, 0, 0, NULL, ?, ?)""",
                         (sid, cwd, "m-" + sid))
        conn.commit()
        self.assertEqual(scanner._backfill_worktree_projects(conn), 1)
        names = dict(conn.execute("SELECT session_id, project_name FROM sessions").fetchall())
        self.assertEqual(names, {"s-wt": "work/myrepo", "s-sub": "myrepo/sub"})
        conn.close()


if __name__ == "__main__":
    unittest.main()
