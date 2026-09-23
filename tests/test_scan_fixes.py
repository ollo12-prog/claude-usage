"""Fixes found by the claude-profiler consistency check (2026-09-22)."""
import os
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path

import cli
from dashboard import HTML_TEMPLATE
from scanner import scan
from tests.test_scanner import _make_assistant_record, _make_custom_title_record


class ScanCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.projects = self.tmp / "projects"
        self.projects.mkdir()
        self.fp = self.projects / "s.jsonl"
        self.db = self.tmp / "u.db"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def scan(self):
        scan(projects_dir=self.projects, db_path=self.db, verbose=False)

    def query(self, sql):
        conn = sqlite3.connect(self.db)
        try:
            return conn.execute(sql).fetchall()
        finally:
            conn.close()


class TestMidStreamTallies(ScanCase):
    def test_next_scan_corrects_a_turn_stored_mid_stream(self):
        # Claude Code writes a response's usage twice while streaming; a scan
        # between the writes must not freeze the partial record.
        self.fp.write_text(_make_assistant_record(message_id="msg-1", output_tokens=3) + "\n",
                           encoding="utf-8")
        os.utime(self.fp, (1_000_000, 1_000_000))
        self.scan()
        with open(self.fp, "a", encoding="utf-8") as f:
            f.write(_make_assistant_record(message_id="msg-1", output_tokens=342) + "\n")
        os.utime(self.fp, (1_000_100, 1_000_100))
        self.scan()
        self.assertEqual(self.query("SELECT output_tokens FROM turns"), [(342,)])
        self.assertEqual(self.query("SELECT total_output_tokens FROM sessions"), [(342,)])


class TestTitleRecordFirst(ScanCase):
    def test_session_opening_with_a_title_still_gets_its_project(self):
        self.fp.write_text(_make_custom_title_record() + "\n"
                           + _make_assistant_record(message_id="msg-1", cwd="/home/user/project") + "\n",
                           encoding="utf-8")
        self.scan()
        self.assertEqual(self.query("SELECT project_name FROM sessions"), [("user/project",)])


class TestLocalOpusBuildIsNotBilled(unittest.TestCase):
    def test_python_and_js_agree(self):
        self.assertIsNone(cli.get_pricing("qwen3.6-40b-claude-46-opus"))
        self.assertEqual(cli.get_pricing("new-opus-5-model")["input"], 5.00)
        self.assertNotIn("m.includes('opus')", HTML_TEMPLATE)


if __name__ == "__main__":
    unittest.main()
