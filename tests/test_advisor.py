"""test_advisor.py - advisor iterations are separate, billable inferences.

An `advisor` tool call runs a SEPARATE inference on a different (usually
stronger) model, carried inside the parent assistant message as an extra
`usage.iterations[]` entry of type 'advisor_message'. The model it ran on is
named by the record's top-level `advisorModel`.

The envelope's top-level `input_tokens` / `output_tokens` count ONLY the
'message' iterations, so those tokens were invisible to the scanner and the
entire advisor spend went unbilled.

The `test_falsify_*` cases exist to prove these checks can actually fail: a cost
test that cannot be made to go red proves nothing.
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
                         model=PARENT_MODEL, advisor_model=ADVISOR_MODEL,
                         include_advisor_model=True):
    """An assistant record carrying message + advisor_message iterations.

    Top-level input/output deliberately equal the sum of the 'message' iterations
    ONLY (2 + 2 in, 100 + 400 out) — that is how Claude Code writes it, and the
    reason the advisor tokens were missed.
    """
    record = {
        "type": "assistant",
        "sessionId": session_id,
        "timestamp": "2026-08-17T10:00:00Z",
        "cwd": "/home/user/project",
        "message": {
            "id": message_id,
            "model": model,
            "content": [],
            "usage": {
                "input_tokens": 4,
                "output_tokens": 500,
                "cache_read_input_tokens": 200_000,
                "cache_creation_input_tokens": 100_000,
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


class _FixtureMixin(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def _parse(self, *lines):
        path = os.path.join(self.tmpdir, "sess.jsonl")
        with open(path, "w") as f:
            for line in lines:
                f.write(line + "\n")
        turns = parse_jsonl_file(path)[1]
        return {t["message_id"]: t for t in turns}


class TestAdvisorTurnExtraction(_FixtureMixin):
    def test_advisor_iteration_becomes_its_own_turn(self):
        turns = self._parse(_make_advisor_record())
        self.assertEqual(sorted(turns), ["advisor:msg_adv_1:1", "msg_adv_1"])

    def test_advisor_turn_uses_the_advisor_model(self):
        adv = self._parse(_make_advisor_record())["advisor:msg_adv_1:1"]
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
        self.assertEqual(p["model"], PARENT_MODEL)

    def test_falls_back_to_parent_model_without_advisorModel(self):
        """Transcripts from builds that don't emit advisorModel must still be
        priced, not silently free."""
        turns = self._parse(_make_advisor_record(include_advisor_model=False))
        self.assertEqual(turns["advisor:msg_adv_1:1"]["model"], PARENT_MODEL)

    def test_reparse_is_idempotent(self):
        """Synthetic ids must be stable, or a rescan double-bills the advisor."""
        self.assertEqual(sorted(self._parse(_make_advisor_record())),
                         sorted(self._parse(_make_advisor_record())))


class TestAdvisorDoesNotRelabelSession(unittest.TestCase):
    def test_is_advisor_turn(self):
        self.assertTrue(scanner.is_advisor_turn({"message_id": "advisor:m:0"}))
        self.assertFalse(scanner.is_advisor_turn({"message_id": "m"}))
        self.assertFalse(scanner.is_advisor_turn({}))

    def test_advisor_turns_do_not_win_the_vote(self):
        """Three advisor turns against one parent: the advisor turns must
        OUTNUMBER the parent, or Counter.most_common breaks the tie by insertion
        order and this passes even with the guard removed."""
        metas = [{"session_id": "s1", "project_name": "p",
                  "first_timestamp": "t", "last_timestamp": "t",
                  "git_branch": ""}]
        base = {"session_id": "s1", "input_tokens": 1, "output_tokens": 1,
                "cache_read_tokens": 0, "cache_creation_tokens": 0}
        turns = [dict(base, model=PARENT_MODEL, message_id="m")]
        turns += [dict(base, model=ADVISOR_MODEL, message_id="advisor:m:%d" % i)
                  for i in range(3)]
        self.assertEqual(scanner.aggregate_sessions(metas, turns)[0]["model"],
                         PARENT_MODEL)


class TestAdvisorCost(_FixtureMixin):
    # parent  in  4       x  $5.00/MTok = $0.00002
    #         out 500     x $25.00/MTok = $0.0125
    #         cr  200,000 x  $0.50/MTok = $0.10
    #         cc  100,000 x  $6.25/MTok = $0.625
    # advisor in  300,000 x $10.00/MTok = $3.00   (fable-5, not opus-5)
    #         out  20,000 x $50.00/MTok = $1.00
    EXPECTED_PARENT = 0.00002 + 0.0125 + 0.10 + 0.625
    EXPECTED_ADVISOR = 3.00 + 1.00
    EXPECTED_TOTAL = EXPECTED_PARENT + EXPECTED_ADVISOR

    def _total(self):
        return sum(
            cli.calc_cost(t["model"], t["input_tokens"], t["output_tokens"],
                          t["cache_read_tokens"], t["cache_creation_tokens"])
            for t in self._parse(_make_advisor_record()).values())

    def test_total_matches_hand_arithmetic(self):
        self.assertAlmostEqual(self._total(), self.EXPECTED_TOTAL, places=6)

    def test_advisor_is_priced_at_its_own_model(self):
        """Pricing the advisor at the parent's model would halve its cost."""
        as_parent = cli.calc_cost(PARENT_MODEL, 300_000, 20_000, 0, 0)
        as_advisor = cli.calc_cost(ADVISOR_MODEL, 300_000, 20_000, 0, 0)
        self.assertNotAlmostEqual(as_parent, as_advisor, places=6)
        self.assertAlmostEqual(as_advisor, self.EXPECTED_ADVISOR, places=6)


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
                          t["cache_read_tokens"], t["cache_creation_tokens"])
            for t in self._parse(_make_advisor_record()).values())

    def _assert_moved(self, msg):
        self.assertNotAlmostEqual(self._total(), TestAdvisorCost.EXPECTED_TOTAL,
                                  places=6, msg=msg)

    def test_falsify_advisor_input_rate(self):
        # Mutate via get_pricing, not PRICING[...]: not every model id is a
        # literal key (claude-opus-5 resolves through the prefix fallback), and
        # get_pricing hands back the live rate dict.
        cli.get_pricing(ADVISOR_MODEL)["input"] = 1.00
        self._assert_moved("advisor input rate never reaches the total")

    def test_falsify_advisor_output_rate(self):
        cli.get_pricing(ADVISOR_MODEL)["output"] = 1.00
        self._assert_moved("advisor output rate never reaches the total")

    def test_falsify_parent_output_rate(self):
        cli.get_pricing(PARENT_MODEL)["output"] = 1.00
        self._assert_moved("parent output rate never reaches the total")

    def test_falsify_dropping_the_advisor_turn(self):
        """The original bug, reproduced: the parent alone falls short by exactly
        the advisor's cost."""
        p = self._parse(_make_advisor_record())["msg_adv_1"]
        parent_only = cli.calc_cost(p["model"], p["input_tokens"],
                                    p["output_tokens"], p["cache_read_tokens"],
                                    p["cache_creation_tokens"])
        self.assertAlmostEqual(TestAdvisorCost.EXPECTED_TOTAL - parent_only,
                               TestAdvisorCost.EXPECTED_ADVISOR, places=6)


class TestUpgradeBackfill(unittest.TestCase):
    """An existing database must gain its advisor turns on the next scan: the
    walk skips unchanged files and turn inserts are INSERT OR IGNORE, so without
    the one-time re-parse an upgraded install keeps the undercount forever."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        projects = os.path.join(self.tmpdir, "projects", "proj")
        os.makedirs(projects)
        with open(os.path.join(projects, "sess.jsonl"), "w") as f:
            f.write(_make_advisor_record() + "\n")
        self.db = os.path.join(self.tmpdir, "usage.db")

    def _scan(self):
        scanner.scan(projects_dir=os.path.join(self.tmpdir, "projects"),
                     db_path=self.db, verbose=False)

    def _advisor_rows(self):
        conn = sqlite3.connect(self.db)
        n = conn.execute("SELECT COUNT(*) FROM turns "
                         "WHERE message_id LIKE 'advisor:%'").fetchone()[0]
        conn.close()
        return n

    def test_pre_fix_db_gains_advisor_turns(self):
        self._scan()
        self.assertGreater(self._advisor_rows(), 0)
        # Simulate a pre-fix database: advisor rows absent, file still marked
        # processed, marker cleared so the one-time re-parse is pending again.
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


if __name__ == "__main__":
    unittest.main()
