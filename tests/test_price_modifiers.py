"""test_price_modifiers.py - fast mode and US-only inference pricing.

Both are per-request multipliers on every token class, and they stack
(platform.claude.com/docs/en/about-claude/pricing, 2026-09-24):

  * usage.speed == "fast"        -> 2x   (Opus 5.5 $8/$40, Opus 5 / 4.8 $10/$50)
  * usage.inference_geo == "us"  -> 1.1x (Claude 4.6 and later)

Before this, both were billed at standard rates, so a session that used either
was under-reported. The premium travels as extra tokens, SUM(tokens * (mult-1)),
priced at the row's own rates; cost is linear in tokens, so that is exact.

The test_falsify_* cases prove the checks can fail: a standard turn must carry no
surcharge, and each modifier on its own must move the cost by its own factor.
"""

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cli
import dashboard
import scanner
from scanner import parse_jsonl_file

MODEL = "claude-opus-5-5"


def _record(mid, speed="standard", geo="global", advisor=False):
    usage = {
        "input_tokens": 1_000, "output_tokens": 2_000,
        "cache_read_input_tokens": 100_000, "cache_creation_input_tokens": 10_000,
        "cache_creation": {"ephemeral_5m_input_tokens": 6_000, "ephemeral_1h_input_tokens": 4_000},
        "speed": speed, "inference_geo": geo,
    }
    rec = {"type": "assistant", "sessionId": "sess-1", "timestamp": "2026-09-20T10:00:00Z",
           "cwd": "/home/u/proj", "message": {"id": mid, "model": MODEL, "content": [], "usage": usage}}
    if advisor:
        rec["advisorModel"] = "claude-fable-5-1"
        usage["iterations"] = [{"type": "message"},
                               {"type": "advisor_message", "input_tokens": 500, "output_tokens": 700}]
    return rec


def _standard_cost():
    """calc_cost of one _record's tokens at standard rates."""
    return cli.calc_cost(MODEL, 1_000, 2_000, 100_000, 10_000, 4_000)


class _ScannedDB(unittest.TestCase):
    """Scan a transcript of the given records into a temp DB."""
    records = []

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        proj = Path(self.tmp) / "projects" / "p"
        proj.mkdir(parents=True)
        (proj / "sess-1.jsonl").write_text("\n".join(json.dumps(r) for r in self.records) + "\n")
        self.db = Path(self.tmp) / "usage.db"
        scanner.scan(projects_dirs=[proj.parent], db_path=self.db, verbose=False)
        self.conn = sqlite3.connect(self.db)
        self.conn.row_factory = sqlite3.Row

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def cli_cost(self, where="1=1"):
        """Cost through the CLI's own aggregate shape + row_cost."""
        rows = self.conn.execute(f"""
            SELECT model, SUM(input_tokens) as inp, SUM(output_tokens) as out,
                   SUM(cache_read_tokens) as cr, SUM(cache_creation_tokens) as cc,
                   SUM(cache_creation_1h_tokens) as cc1h, {cli.SURCHARGE}
            FROM turns WHERE {where} GROUP BY model""").fetchall()
        return sum(cli.row_cost(r) for r in rows)


class TestScannerRecordsSpeed(_ScannedDB):
    records = [_record("m1", speed="fast", geo="us", advisor=True)]

    def test_parent_turn_keeps_speed_and_geo(self):
        r = self.conn.execute("SELECT speed, inference_geo FROM turns WHERE message_id='m1'").fetchone()
        self.assertEqual((r["speed"], r["inference_geo"]), ("fast", "us"))

    def test_advisor_turn_does_not_inherit_fast(self):
        r = self.conn.execute("SELECT speed FROM turns WHERE message_id LIKE 'advisor:%'").fetchone()
        self.assertEqual(r["speed"], "")


class TestCliCost(_ScannedDB):
    records = [_record("fast_us", "fast", "us"), _record("fast", "fast"),
               _record("us", geo="us"), _record("std")]

    def test_fast_and_us_stack(self):
        self.assertAlmostEqual(self.cli_cost("message_id='fast_us'"), _standard_cost() * 2.2, places=6)

    def test_falsify_fast_alone_is_2x(self):
        self.assertAlmostEqual(self.cli_cost("message_id='fast'"), _standard_cost() * 2.0, places=6)

    def test_falsify_us_alone_is_1_1x(self):
        self.assertAlmostEqual(self.cli_cost("message_id='us'"), _standard_cost() * 1.1, places=6)

    def test_falsify_standard_is_unchanged(self):
        self.assertAlmostEqual(self.cli_cost("message_id='std'"), _standard_cost(), places=6)


class TestDashboardRows(_ScannedDB):
    records = [_record("fast_us", "fast", "us")]

    def test_daily_row_carries_surcharge(self):
        row = dashboard.get_dashboard_data(db_path=self.db)["daily_by_model"][0]
        self.assertAlmostEqual(row["x"]["output"], 2_000 * 1.2, places=6)
        self.assertAlmostEqual(row["x"]["cache_creation_1h"], 4_000 * 1.2, places=6)

    def test_session_breakdown_and_detail_carry_surcharge(self):
        data = dashboard.get_dashboard_data(db_path=self.db)
        self.assertAlmostEqual(data["sessions_all"][0]["by_model"][0]["x"]["input"], 1_200, places=6)
        turn = dashboard.get_session_detail("sess-1", db_path=self.db)["turns"][0]
        self.assertAlmostEqual(turn["x"]["cache_read"], 120_000, places=6)


class TestDashboardStandardRowsStayLean(_ScannedDB):
    records = [_record("std")]

    def test_falsify_no_surcharge_on_standard(self):
        data = dashboard.get_dashboard_data(db_path=self.db)
        self.assertIsNone(data["daily_by_model"][0]["x"])
        self.assertIsNone(dashboard.get_session_detail("sess-1", db_path=self.db)["turns"][0]["x"])


@unittest.skipIf(sqlite3.sqlite_version_info < (3, 35), "DROP COLUMN needs SQLite 3.35")
class TestDetailOnDbWithoutSpeedColumn(_ScannedDB):
    """A DB upgraded but not yet rescanned has inference_geo and no speed column;
    session detail must still apply the US multiplier."""
    records = [_record("us", geo="us")]

    def test_us_surcharge_survives_missing_speed_column(self):
        self.conn.execute("ALTER TABLE turns DROP COLUMN speed")
        self.conn.commit()
        turn = dashboard.get_session_detail("sess-1", db_path=self.db)["turns"][0]
        self.assertAlmostEqual(turn["x"]["output"], 2_000 * 0.1, places=6)


class TestSpeedBackfill(_ScannedDB):
    records = [_record("fast", "fast"), _record("std")]

    def test_backfill_restores_fast_on_old_rows(self):
        # Simulate rows ingested before the column existed.
        self.conn.execute("UPDATE turns SET speed = NULL")
        self.conn.commit()
        files = [str(p) for p in Path(self.tmp, "projects").rglob("*.jsonl")]
        self.assertEqual(scanner._backfill_speed(self.conn, files), 1)
        got = dict(self.conn.execute("SELECT message_id, speed FROM turns").fetchall())
        self.assertEqual(got, {"fast": "fast", "std": None})


_JS = dashboard.HTML_TEMPLATE
_JS = _JS[_JS.index("const PRICING_STORAGE_KEY"):_JS.index("// ── Formatting")]

HARNESS = _JS + """
const base = { model: 'claude-opus-5-5', input: 1000, output: 2000, cache_read: 100000,
               cache_creation: 10000, cache_creation_1h: 4000 };
const k = 1.2;
const x = { input: 1000*k, output: 2000*k, cache_read: 100000*k, cache_creation: 10000*k, cache_creation_1h: 4000*k };
const std = rowCost(base), prem = rowCost({ ...base, x });
if (!(std > 0)) throw new Error('standard cost is ' + std);
if (Math.abs(prem - std * 2.2) > 1e-9) throw new Error('fast+us ' + prem + ' != 2.2 x ' + std);
// Merging rows client-side must keep the surcharge (the byModel path).
const m = { ...base, x: null };
addSurcharge(m, { x }); addSurcharge(m, { x: null });
if (Math.abs(rowCost(m) - prem) > 1e-9) throw new Error('addSurcharge lost the premium');
// Sessions price through by_model rows.
const s = { model: base.model, by_model: [{ ...base, x }] };
if (Math.abs(sessionCost(s) - prem) > 1e-9) throw new Error('sessionCost ignores x');
console.log('ok');
"""


class TestByModelMergeKeepsSurcharge(unittest.TestCase):
    """byModel feeds the Est. Cost card and cost-by-model table; its merge lives
    inside a large JS function with no seam to call, so guard it at source level
    (same pattern as TestByModelCarriesThe1hSplit)."""

    def test_model_aggregator_merges_surcharge(self):
        tail = dashboard.HTML_TEMPLATE.partition("const modelMap = {};")[2]
        self.assertIn("addSurcharge(m, r)", tail[:tail.index('\n  }')])


@unittest.skipUnless(shutil.which("node"), "node not installed")
class TestDashboardJs(unittest.TestCase):
    def test_row_cost_prices_the_surcharge(self):
        r = subprocess.run(["node", "-e", HARNESS], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("ok", r.stdout)


if __name__ == "__main__":
    unittest.main()
