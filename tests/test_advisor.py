"""test_advisor.py - advisor iterations and 1-hour cache-write pricing.

Two silent undercounts of the same session:

  1. An `advisor` tool call is a SEPARATE inference on a different (usually
     stronger) model, carried inside the parent assistant message as an extra
     `usage.iterations[]` entry of type 'advisor_message'. The model is named by
     the record's top-level `advisorModel`. The envelope's top-level
     `input_tokens`/`output_tokens` count ONLY the 'message' iterations, so those
     tokens were invisible and the entire advisor spend went unbilled.
  2. Cache writes with a 1-hour TTL bill at 2x input, not the 5-minute 1.25x.
     The 5m/1h split was already recorded but never used in pricing.

The `test_falsify_*` cases exist to prove the checks above can actually fail:
each breaks one rate and asserts the total moves. A cost test that cannot be made
to go red proves nothing.
"""

import json
import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cli
import scanner
from scanner import parse_jsonl_file

PARENT_MODEL = "claude-opus-5"
ADVISOR_MODEL = "claude-fable-5"


def _make_advisor_record(message_id="msg_adv_1", session_id="sess-1",
                         advisor_model=ADVISOR_MODEL, model=PARENT_MODEL,
                         include_advisor_model=True):
    """An assistant record whose usage carries message + advisor_message iterations.

    Top-level input/output deliberately equal the sum of the 'message' iterations
    only (2 + 2 in, 100 + 400 out) — that is exactly how Claude Code writes it,
    and the reason the advisor tokens were missed.
    """
    record = {
        "type": "assistant",
        "sessionId": session_id,
        "timestamp": "2026-08-17T10:00:00Z",
        "cwd": "/home/user/project",
        "isSidechain": False,
        "message": {
            "id": message_id,
            "model": model,
            "content": [],
            "usage": {
                "input_tokens": 4,
                "output_tokens": 500,
                "cache_read_input_tokens": 200_000,
                "cache_creation_input_tokens": 100_000,
                "cache_creation": {
                    "ephemeral_5m_input_tokens": 40_000,
                    "ephemeral_1h_input_tokens": 60_000,
                },
                "iterations": [
                    {"type": "message", "input_tokens": 2, "output_tokens": 100,
                     "cache_read_input_tokens": 100_000,
                     "cache_creation_input_tokens": 40_000},
                    {"type": "advisor_message", "input_tokens": 300_000,
                     "output_tokens": 20_000,
                     "cache_read_input_tokens": 0,
                     "cache_creation_input_tokens": 0},
                    {"type": "message", "input_tokens": 2, "output_tokens": 400,
                     "cache_read_input_tokens": 100_000,
                     "cache_creation_input_tokens": 60_000},
                ],
            },
        },
    }
    if include_advisor_model:
        record["advisorModel"] = advisor_model
    return json.dumps(record)


def _with_tool_call(record_json, name="Read"):
    """Same record, plus a tool_use block — the field the old duplicate dropped."""
    record = json.loads(record_json)
    record["message"]["content"] = [
        {"type": "tool_use", "id": "toolu_1", "name": name,
         "input": {"file_path": "/tmp/x"}},
    ]
    return json.dumps(record)


class _FixtureMixin(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def _parse(self, *lines):
        path = os.path.join(self.tmpdir, "sess.jsonl")
        with open(path, "w") as f:
            for line in lines:
                f.write(line + "\n")
        result = parse_jsonl_file(path)
        turns = result[1]
        return {t["message_id"]: t for t in turns}


class TestAdvisorTurnExtraction(_FixtureMixin):
    def test_advisor_iteration_becomes_its_own_turn(self):
        turns = self._parse(_make_advisor_record())
        self.assertIn("msg_adv_1", turns)
        self.assertIn("advisor:msg_adv_1:1", turns)

    def test_advisor_turn_uses_the_advisor_model(self):
        turns = self._parse(_make_advisor_record())
        adv = turns["advisor:msg_adv_1:1"]
        self.assertEqual(adv["model"], ADVISOR_MODEL)
        self.assertEqual(adv["input_tokens"], 300_000)
        self.assertEqual(adv["output_tokens"], 20_000)
        self.assertEqual(adv["tool_name"], "advisor")

    def test_parent_turn_keeps_only_its_own_tokens(self):
        """No double-count: the parent keeps the envelope's top-level numbers,
        which already exclude the advisor iteration."""
        p = self._parse(_make_advisor_record())["msg_adv_1"]
        self.assertEqual(p["input_tokens"], 4)
        self.assertEqual(p["output_tokens"], 500)
        self.assertEqual(p["cache_creation_1h_tokens"], 60_000)

    def test_falls_back_to_parent_model_without_advisorModel(self):
        """Older Claude Code builds emit iterations with no advisorModel; those
        tokens must still be priced, not silently free."""
        turns = self._parse(_make_advisor_record(include_advisor_model=False))
        self.assertEqual(turns["advisor:msg_adv_1:1"]["model"], PARENT_MODEL)

    def test_message_iterations_are_not_turned_into_turns(self):
        turns = self._parse(_make_advisor_record())
        self.assertEqual(sorted(turns), ["advisor:msg_adv_1:1", "msg_adv_1"])


class TestAdvisorDoesNotRelabelSession(unittest.TestCase):
    """fable-5 outranks opus, so an advisor call must not win the primary-model
    vote — otherwise one advisor call relabels a whole Opus session."""

    def test_is_advisor_turn(self):
        self.assertTrue(scanner.is_advisor_turn({"message_id": "advisor:m:0"}))
        self.assertFalse(scanner.is_advisor_turn({"message_id": "m"}))
        self.assertFalse(scanner.is_advisor_turn({}))

    def test_advisor_turns_do_not_win_the_vote(self):
        """Three advisor turns against one parent: the advisor must OUTNUMBER the
        parent, or Counter.most_common breaks the tie by insertion order and this
        passes with the guard removed."""
        metas = [{"session_id": "s1", "project_name": "p",
                  "first_timestamp": "t", "last_timestamp": "t",
                  "git_branch": ""}]
        base = {"session_id": "s1", "input_tokens": 1, "output_tokens": 1,
                "cache_read_tokens": 0, "cache_creation_tokens": 0}
        turns = [dict(base, model=PARENT_MODEL, message_id="m")]
        turns += [dict(base, model=ADVISOR_MODEL, message_id="advisor:m:%d" % i)
                  for i in range(3)]
        out = scanner.aggregate_sessions(metas, turns)
        self.assertEqual(out[0]["model"], PARENT_MODEL)


class TestCostMatchesHandArithmetic(_FixtureMixin):
    # parent  in  4       x  $5.00/MTok = $0.00002
    #         out 500     x $25.00/MTok = $0.0125
    #         cr  200,000 x  $0.50/MTok = $0.10
    #         cc   40,000 x  $6.25/MTok = $0.25    (5m TTL, 1.25x input)
    #         cc   60,000 x $10.00/MTok = $0.60    (1h TTL, 2x input)
    # advisor in  300,000 x $10.00/MTok = $3.00    (fable-5, not opus-5)
    #         out  20,000 x $50.00/MTok = $1.00
    EXPECTED_PARENT = 0.00002 + 0.0125 + 0.10 + 0.25 + 0.60
    EXPECTED_ADVISOR = 3.00 + 1.00
    EXPECTED_TOTAL = EXPECTED_PARENT + EXPECTED_ADVISOR  # $4.96252

    def _total(self):
        return sum(
            cli.calc_cost(t["model"], t["input_tokens"], t["output_tokens"],
                          t["cache_read_tokens"], t["cache_creation_tokens"],
                          t["cache_creation_1h_tokens"])
            for t in self._parse(_make_advisor_record()).values())

    def test_total_matches_hand_arithmetic(self):
        self.assertAlmostEqual(self._total(), self.EXPECTED_TOTAL, places=6)

    def test_1h_costs_more_than_5m(self):
        as_5m = cli.calc_cost(PARENT_MODEL, 0, 0, 0, 100_000, 0)
        as_1h = cli.calc_cost(PARENT_MODEL, 0, 0, 0, 100_000, 100_000)
        self.assertGreater(as_1h, as_5m)

    def test_1h_portion_clamped_to_total(self):
        self.assertAlmostEqual(cli.calc_cost(PARENT_MODEL, 0, 0, 0, 1000, 9999),
                               cli.calc_cost(PARENT_MODEL, 0, 0, 0, 1000, 1000),
                               places=9)

    def test_default_prices_at_5m_rate(self):
        """Rows recorded before the split have cc but no 1h value."""
        rates = cli.get_pricing(PARENT_MODEL)
        self.assertAlmostEqual(cli.calc_cost(PARENT_MODEL, 0, 0, 0, 1_000_000),
                               rates["cache_write"], places=6)


class TestFalsification(_FixtureMixin):
    """Break one rate at a time; the total must move each time."""

    def setUp(self):
        super().setUp()
        self._saved = {k: dict(v) for k, v in cli.PRICING.items()
                       if isinstance(v, dict)}

    def tearDown(self):
        for k, v in self._saved.items():
            cli.PRICING[k] = v

    def _total(self):
        return sum(
            cli.calc_cost(t["model"], t["input_tokens"], t["output_tokens"],
                          t["cache_read_tokens"], t["cache_creation_tokens"],
                          t["cache_creation_1h_tokens"])
            for t in self._parse(_make_advisor_record()).values())

    def _assert_moved(self, msg):
        self.assertNotAlmostEqual(
            self._total(), TestCostMatchesHandArithmetic.EXPECTED_TOTAL,
            places=6, msg=msg)

    def test_falsify_advisor_input_rate(self):
        cli.PRICING[ADVISOR_MODEL]["input"] = 1.00
        self._assert_moved("advisor input rate never reaches the total")

    def test_falsify_advisor_output_rate(self):
        cli.PRICING[ADVISOR_MODEL]["output"] = 1.00
        self._assert_moved("advisor output rate never reaches the total")

    def test_falsify_1h_rate(self):
        cli.PRICING[PARENT_MODEL]["cache_write_1h"] = 6.25
        self._assert_moved("1h cache-write rate never reaches the total")

    def test_falsify_5m_rate(self):
        cli.PRICING[PARENT_MODEL]["cache_write"] = 99.0
        self._assert_moved("5m cache-write rate never reaches the total")

    def test_falsify_dropping_the_advisor_turn(self):
        """The original bug, reproduced: the parent alone must fall short by
        exactly the advisor's cost."""
        parent = self._parse(_make_advisor_record())["msg_adv_1"]
        parent_only = cli.calc_cost(
            parent["model"], parent["input_tokens"], parent["output_tokens"],
            parent["cache_read_tokens"], parent["cache_creation_tokens"],
            parent["cache_creation_1h_tokens"])
        self.assertAlmostEqual(
            TestCostMatchesHandArithmetic.EXPECTED_TOTAL - parent_only,
            TestCostMatchesHandArithmetic.EXPECTED_ADVISOR, places=6)


class TestUpgradeBackfill(unittest.TestCase):
    """An existing DB must gain its advisor turns on the next scan: the walk skips
    unchanged files and turn inserts are INSERT OR IGNORE, so without the one-time
    re-parse an upgraded DB would keep the undercount forever."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.projects = os.path.join(self.tmpdir, "projects", "proj")
        os.makedirs(self.projects)
        with open(os.path.join(self.projects, "sess.jsonl"), "w") as f:
            f.write(_make_advisor_record() + "\n")
        self.db = os.path.join(self.tmpdir, "usage.db")

    def _advisor_rows(self):
        conn = sqlite3.connect(self.db)
        n = conn.execute("SELECT COUNT(*) FROM turns "
                         "WHERE message_id LIKE 'advisor:%'").fetchone()[0]
        conn.close()
        return n

    def _scan(self):
        scanner.scan(projects_dir=os.path.join(self.tmpdir, "projects"),
                     db_path=self.db, verbose=False)

    def test_pre_fix_db_gains_advisor_turns(self):
        self._scan()
        self.assertGreater(self._advisor_rows(), 0)
        # Simulate a pre-fix DB: advisor rows absent, file still marked processed,
        # marker cleared so the one-time re-parse is pending again.
        conn = sqlite3.connect(self.db)
        conn.execute("DELETE FROM turns WHERE message_id LIKE 'advisor:%'")
        conn.execute("DELETE FROM schema_meta WHERE key='advisor_reparse_done'")
        conn.commit()
        conn.close()
        self.assertEqual(self._advisor_rows(), 0)
        self._scan()
        self.assertGreater(self._advisor_rows(), 0,
                           "upgrade re-parse did not restore advisor turns")

    def test_repeat_scans_do_not_duplicate(self):
        self._scan()
        first = self._advisor_rows()
        conn = sqlite3.connect(self.db)
        conn.execute("DELETE FROM processed_files")  # force a full re-parse
        conn.commit()
        conn.close()
        self._scan()
        self.assertEqual(self._advisor_rows(), first)


class TestIncrementalScanKeepsAdvisorTurns(unittest.TestCase):
    """Advisor turns must survive the *incremental* path, not just a full parse.

    A live session is rescanned every time the dashboard runs, so most advisor
    calls arrive as lines appended to an already-processed file. That path used
    to be a second, hand-maintained copy of the parse loop with no advisor block,
    so every advisor call after a file's first scan was silently billed at $0 —
    and the one-time re-parse could not save it, because the next append put the
    file straight back on the incremental path.
    """

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.projects = os.path.join(self.tmpdir, "projects", "proj")
        os.makedirs(self.projects)
        self.jsonl = os.path.join(self.projects, "sess.jsonl")
        self.db = os.path.join(self.tmpdir, "usage.db")
        # First scan sees a session with no advisor call yet.
        self._append(json.dumps({
            "type": "assistant", "sessionId": "sess-1",
            "timestamp": "2026-08-17T09:00:00Z", "cwd": "/home/user/project",
            "message": {"id": "msg_plain", "model": PARENT_MODEL, "content": [],
                        "usage": {"input_tokens": 10, "output_tokens": 20}},
        }))

    def _append(self, line):
        with open(self.jsonl, "a") as f:
            f.write(line + "\n")
        # Bump the mtime past the recorded one: a same-tick write would be seen as
        # unchanged and skipped, which would fail the test for the wrong reason.
        stamp = os.path.getmtime(self.jsonl) + 10
        os.utime(self.jsonl, (stamp, stamp))

    def _scan(self):
        scanner.scan(projects_dir=os.path.join(self.tmpdir, "projects"),
                     db_path=self.db, verbose=False)

    def _advisor_rows(self):
        conn = sqlite3.connect(self.db)
        rows = conn.execute("SELECT message_id, model, input_tokens, output_tokens "
                            "FROM turns WHERE message_id LIKE 'advisor:%'").fetchall()
        conn.close()
        return rows

    def test_advisor_turn_appended_after_first_scan_is_recorded(self):
        self._scan()
        self.assertEqual(self._advisor_rows(), [])
        self._append(_make_advisor_record(message_id="msg_adv_late"))
        self._scan()
        rows = self._advisor_rows()
        self.assertEqual(len(rows), 1, "incremental scan dropped the advisor turn")
        self.assertEqual(rows[0][0], "advisor:msg_adv_late:1")
        self.assertEqual(rows[0][1], ADVISOR_MODEL)
        self.assertEqual((rows[0][2], rows[0][3]), (300_000, 20_000))

    def test_incremental_turn_carries_the_same_fields_as_a_full_parse(self):
        """Parity guard: both paths must produce the same turn shape.

        Passes against the old duplicate too — on the fields it happened to have
        in sync. It is here to catch the *next* divergence (tool_calls already
        went missing once this way), not to reproduce this bug.
        """
        record = _with_tool_call(_make_advisor_record(message_id="msg_adv_late"))
        self._scan()
        self._append(record)
        self._scan()
        conn = sqlite3.connect(self.db)
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM turns "
                           "WHERE message_id = 'msg_adv_late'").fetchone()
        conn.close()
        self.assertIsNotNone(row, "incremental scan dropped the parent turn")
        reference = os.path.join(self.tmpdir, "full.jsonl")
        with open(reference, "w") as f:
            f.write(record + "\n")
        expected = {t["message_id"]: t
                    for t in parse_jsonl_file(reference)[1]}["msg_adv_late"]
        for field, want in expected.items():
            if field in row.keys():
                self.assertEqual(row[field], want, "incremental %s differs" % field)


if __name__ == "__main__":
    unittest.main()
