"""The dashboard's UTC/local display toggle.

Every `day` field the client range-filters on is bucketed server-side, so the
toggle is a server round-trip (`/api/data?tz=`). This pins the UTC side
unconditionally, and the local-vs-UTC *difference* only where the machine has a
non-zero UTC offset — on a UTC machine the two calendars are identical and a
difference assertion would be vacuously green.
"""
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scanner import get_db, init_db, upsert_sessions, insert_turns
from dashboard import get_dashboard_data

# Straddles UTC midnight from both sides, so one of the two turns falls on a
# different calendar day whichever way the machine's offset points.
EARLY = "2026-09-02T00:30:00Z"
LATE  = "2026-09-02T23:30:00Z"

UTC_OFFSET = datetime.now().astimezone().utcoffset()


def _turn(ts, session_id):
    return {"session_id": session_id, "timestamp": ts, "model": "claude-opus-5",
            "input_tokens": 10, "output_tokens": 5, "cache_read_tokens": 0,
            "cache_creation_tokens": 0, "tool_name": None, "cwd": "/tmp"}


class TestDisplayTZ(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.db_path = Path(self.db_path)
        conn = get_db(self.db_path)
        init_db(conn)
        upsert_sessions(conn, [{
            "session_id": "s-early", "project_name": "p", "first_timestamp": EARLY,
            "last_timestamp": EARLY, "git_branch": "main", "model": "claude-opus-5",
            "total_input_tokens": 10, "total_output_tokens": 5,
            "total_cache_read": 0, "total_cache_creation": 0, "turn_count": 1,
        }, {
            "session_id": "s-late", "project_name": "p", "first_timestamp": LATE,
            "last_timestamp": LATE, "git_branch": "main", "model": "claude-opus-5",
            "total_input_tokens": 10, "total_output_tokens": 5,
            "total_cache_read": 0, "total_cache_creation": 0, "turn_count": 1,
        }])
        insert_turns(conn, [_turn(EARLY, "s-early"), _turn(LATE, "s-late")])
        conn.commit()
        conn.close()

    def tearDown(self):
        os.unlink(self.db_path)

    def _days(self, tz):
        d = get_dashboard_data(db_path=self.db_path, tz=tz)
        return ({r["day"] for r in d["daily_by_model"]},
                {s["last_date"] for s in d["sessions_all"]})

    def test_utc_mode_buckets_by_utc_day(self):
        """Both turns are on 2026-09-02 UTC, whatever the machine's timezone."""
        daily, sessions = self._days("utc")
        self.assertEqual(daily, {"2026-09-02"})
        self.assertEqual(sessions, {"2026-09-02"})

    def test_default_is_local(self):
        """No tz argument must keep the pre-toggle behaviour."""
        self.assertEqual(self._days(""), self._days("local"))

    @unittest.skipIf(UTC_OFFSET.total_seconds() == 0,
                     "machine runs at UTC+0: local and UTC calendars coincide")
    def test_local_mode_differs_from_utc(self):
        """One of the two turns straddles local midnight, so the local calendar
        must place it on a different day than UTC does — in BOTH the turn buckets
        and the session day, or a session lands on the far side of a range edge
        from its own turns."""
        daily, sessions = self._days("local")
        self.assertNotEqual(daily, {"2026-09-02"})
        self.assertEqual(daily, sessions, "session days must share the turn calendar")

if __name__ == "__main__":
    unittest.main()
