"""cost-band session.measure sidecar -> measures table -> dashboard quota view."""

import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import dashboard
import scanner


def line(sid, ts, five=None, seven=None, five_reset="2099-01-01T00:00:00.000Z", cost=1.0):
    limits = []
    if five is not None:
        limits.append({"kind": "five_hour", "percentUsed": five, "resetsAt": five_reset})
    if seven is not None:
        limits.append({"kind": "seven_day", "percentUsed": seven, "resetsAt": "2099-01-01T00:00:00.000Z"})
    return json.dumps({"ts": ts, "sessionId": sid, "cwd": "C:\\x", "rateLimits": limits,
                       "cost_usd": cost, "context_tokens": 1000, "changed": ["rateLimits"]})


class MeasuresTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.mdir = self.tmp / "usage-measure"
        self.mdir.mkdir()
        self.conn = scanner.get_db(self.tmp / "m.db")
        scanner.init_db(self.conn)

    def tearDown(self):
        self.conn.close()
        self._tmp.cleanup()

    def write(self, sid, lines, mtime=None):
        p = self.mdir / f"{sid}.jsonl"
        p.write_text("\n".join(lines) + "\n", encoding="utf-8")
        if mtime is not None:
            os.utime(p, (mtime, mtime))
        return p

    def count(self):
        return self.conn.execute("SELECT COUNT(*) FROM measures").fetchone()[0]

    def test_ingest_is_idempotent_and_skips_torn_lines(self):
        self.write("a", [line("a", "2026-10-06T01:00:00Z", 10, 40), '{"ts": "2026-10-06T01:01',
                         '{"ts": "2026-10-06T01:02:00Z"}', '{"sessionId": "a"}', "[]", ""])
        self.assertEqual(scanner.ingest_measures(self.conn, self.mdir), 1)
        row = self.conn.execute("SELECT five_hour_pct, seven_day_pct, cost_usd FROM measures").fetchone()
        self.assertEqual(tuple(row), (10, 40, 1.0))
        self.assertEqual(scanner.ingest_measures(self.conn, self.mdir), 0)
        os.utime(self.mdir / "a.jsonl", (1, 1))  # changed mtime forces a full re-parse: still no dupes
        self.assertEqual(scanner.ingest_measures(self.conn, self.mdir), 0)
        self.assertEqual(self.count(), 1)

    def test_rewritten_file_is_reread(self):
        self.write("a", [line("a", "2026-10-06T01:00:00Z", 10)], mtime=1000)
        scanner.ingest_measures(self.conn, self.mdir)
        self.write("a", [line("a", "2026-10-06T01:00:00Z", 10), line("a", "2026-10-06T01:05:00Z", 12)], mtime=2000)
        self.assertEqual(scanner.ingest_measures(self.conn, self.mdir), 1)
        self.assertEqual(self.count(), 2)

    def test_unchanged_mtime_is_skipped(self):
        self.write("a", [line("a", "2026-10-06T01:00:00Z", 10)], mtime=1000)
        scanner.ingest_measures(self.conn, self.mdir)
        self.write("a", [line("a", "2026-10-06T01:05:00Z", 12)], mtime=1000)
        self.assertEqual(scanner.ingest_measures(self.conn, self.mdir), 0)

    def test_missing_dir_is_a_noop(self):
        self.assertEqual(scanner.ingest_measures(self.conn, self.tmp / "nope"), 0)

    def test_only_default_scan_reads_measure_dir(self):
        self.write("a", [line("a", "2026-10-06T01:00:00Z", 10)])
        db = self.tmp / "u.db"
        projects = self.tmp / "projects"
        projects.mkdir()
        with mock.patch.object(scanner, "MEASURE_DIR", self.mdir):
            self.assertEqual(scanner.scan(projects_dir=projects, db_path=db, verbose=False)["measures"], 0)
            self.assertEqual(scanner.scan(projects_dir=projects, db_path=db, verbose=False,
                                          measure_dir=self.mdir)["measures"], 1)
            with mock.patch.object(scanner, "DEFAULT_PROJECTS_DIRS", [projects]):
                db2 = self.tmp / "u2.db"
                self.assertEqual(scanner.scan(db_path=db2, verbose=False)["measures"], 1)

    def test_burn_across_interleaved_sessions_and_reset(self):
        self.write("aaaaaaaa-1", [
            line("aaaaaaaa-1", "2026-10-06T01:00:00Z", 10, 40),  # first reading: no baseline
            line("aaaaaaaa-1", "2026-10-06T01:10:00Z", 14, 41),  # +4 / +1 to A
            line("aaaaaaaa-1", "2026-10-06T01:30:00Z", 20, 43),  # +3 / +1 to A
        ])
        self.write("bbbbbbbb-2", [
            line("bbbbbbbb-2", "2026-10-06T01:20:00Z", 17, 42),  # +3 / +1 to B
            line("bbbbbbbb-2", "2026-10-06T01:40:00Z", 5, 44),   # drop = reset: +5 to B
        ])
        scanner.ingest_measures(self.conn, self.mdir)
        q = dashboard.quota_summary(self.conn, tz="utc")
        burn = {s["full_session_id"]: (s["five_hour"], s["seven_day"]) for s in q["sessions"]}
        self.assertEqual(burn["aaaaaaaa-1"], (7, 2))
        self.assertEqual(burn["bbbbbbbb-2"], (8, 2))
        self.assertEqual(q["latest"]["five_hour"]["pct"], 5)
        self.assertEqual(q["latest"]["seven_day"]["pct"], 44)
        self.assertEqual(q["sessions"][0]["full_session_id"], "bbbbbbbb-2")  # most recent first

    def test_reset_detected_by_resets_at_even_when_pct_rises(self):
        self.write("a", [
            line("a", "2026-10-06T01:00:00Z", 10, five_reset="2026-10-06T02:00:00Z"),
            line("a", "2026-10-06T03:00:00Z", 15, five_reset="2026-10-06T07:00:00Z"),
        ])
        scanner.ingest_measures(self.conn, self.mdir)
        q = dashboard.quota_summary(self.conn, tz="utc")
        self.assertEqual(q["sessions"][0]["five_hour"], 15)

    def test_latest_marked_stale_after_reset_time(self):
        self.write("a", [line("a", "2026-10-06T01:00:00Z", 30, five_reset="2000-01-01T00:00:00Z")])
        scanner.ingest_measures(self.conn, self.mdir)
        self.assertTrue(dashboard.quota_summary(self.conn, tz="utc")["latest"]["five_hour"]["stale"])

    def test_malformed_fields_do_not_abort_or_poison(self):
        bad = [
            json.dumps({"ts": "2026-10-06T01:00:00Z", "sessionId": "a", "rateLimits": 1, "cost_usd": "x"}),
            json.dumps({"ts": "2026-10-06T01:01:00Z", "sessionId": "a",
                        "rateLimits": [{"kind": "five_hour", "percentUsed": "31"}]}),
            json.dumps({"ts": "2026-10-06T01:02:00Z", "sessionId": 5}),
        ]
        self.write("a", bad + [line("a", "2026-10-06T01:03:00Z", 10)])
        self.assertEqual(scanner.ingest_measures(self.conn, self.mdir), 3)
        rows = self.conn.execute("SELECT five_hour_pct, cost_usd FROM measures ORDER BY ts").fetchall()
        self.assertEqual([tuple(r) for r in rows], [(None, None), (None, None), (10, 1.0)])
        q = dashboard.quota_summary(self.conn, tz="utc")
        self.assertEqual(q["latest"]["five_hour"]["pct"], 10)

    def test_mixed_timestamp_precision_orders_and_resets_correctly(self):
        self.write("a", [
            line("a", "2026-10-06T01:00:00Z", 10, five_reset="2026-10-06T02:00:00Z"),
            line("a", "2026-10-06T02:00:00.100Z", 15, five_reset="2026-10-06T07:00:00Z"),
            line("a", "2026-10-06T02:00:01", 16, five_reset="2026-10-06T07:00:00Z"),  # naive = UTC
            # As text "...:05.500Z" sorts before "...:05Z"; parsed, it is later.
            line("a", "2026-10-06T02:00:05Z", 17, five_reset="2026-10-06T07:00:00Z"),
            line("a", "2026-10-06T02:00:05.500Z", 18, five_reset="2026-10-06T07:00:00Z"),
        ])
        scanner.ingest_measures(self.conn, self.mdir)
        q = dashboard.quota_summary(self.conn, tz="utc")
        self.assertEqual(q["sessions"][0]["five_hour"], 18)  # reset: 15 from zero, then +1 +1 +1
        self.assertEqual(q["latest"]["five_hour"]["pct"], 18)

    def test_no_table_or_rows_gives_none(self):
        self.assertIsNone(dashboard.quota_summary(sqlite3.connect(":memory:")))
        self.assertIsNone(dashboard.quota_summary(self.conn))


if __name__ == "__main__":
    unittest.main()
