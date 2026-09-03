"""Tests for dashboard.py - API endpoint and data retrieval."""

import json
import os
import sqlite3
import tempfile
import threading
import re
import unittest
import urllib.request
from datetime import datetime
from pathlib import Path

from scanner import get_db, init_db, upsert_sessions, insert_turns
from dashboard import (
    get_dashboard_data, get_session_detail, local_date,
    DashboardHandler, HTML_TEMPLATE,
)

try:
    from http.server import HTTPServer
except ImportError:
    HTTPServer = None


class TestGetDashboardData(unittest.TestCase):
    def setUp(self):
        self.tmpfile = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmpfile.close()
        self.db_path = Path(self.tmpfile.name)
        conn = get_db(self.db_path)
        init_db(conn)
        # Insert sample data
        sessions = [{
            "session_id": "sess-abc123", "project_name": "user/myproject",
            "first_timestamp": "2026-04-08T09:00:00Z",
            "last_timestamp": "2026-04-08T10:00:00Z",
            "git_branch": "main", "model": "claude-sonnet-4-6",
            "total_input_tokens": 5000, "total_output_tokens": 2000,
            "total_cache_read": 500, "total_cache_creation": 200,
            "turn_count": 10,
        }]
        upsert_sessions(conn, sessions)
        turns = [
            {
                "session_id": "sess-abc123", "timestamp": "2026-04-08T09:30:00Z",
                "model": "claude-sonnet-4-6", "input_tokens": 500,
                "output_tokens": 200, "cache_read_tokens": 50,
                "cache_creation_tokens": 20, "tool_name": None, "cwd": "/tmp",
            },
            {
                "session_id": "sess-abc123", "timestamp": "2026-04-08T14:15:00Z",
                "model": "claude-sonnet-4-6", "input_tokens": 300,
                "output_tokens": 150, "cache_read_tokens": 0,
                "cache_creation_tokens": 0, "tool_name": None, "cwd": "/tmp",
            },
        ]
        insert_turns(conn, turns)
        conn.commit()
        conn.close()

    def tearDown(self):
        os.unlink(self.db_path)

    def test_returns_valid_structure(self):
        data = get_dashboard_data(db_path=self.db_path)
        self.assertIn("all_models", data)
        self.assertIn("daily_by_model", data)
        self.assertIn("tool_by_model", data)
        self.assertIn("sessions_all", data)
        self.assertIn("generated_at", data)

    def test_models_populated(self):
        data = get_dashboard_data(db_path=self.db_path)
        self.assertIn("claude-sonnet-4-6", data["all_models"])

    def test_sessions_populated(self):
        data = get_dashboard_data(db_path=self.db_path)
        self.assertEqual(len(data["sessions_all"]), 1)
        session = data["sessions_all"][0]
        self.assertEqual(session["project"], "user/myproject")
        self.assertEqual(session["model"], "claude-sonnet-4-6")
        self.assertEqual(session["input"], 5000)
        self.assertEqual(session["cache_read"], 500)
        self.assertEqual(session["cache_creation"], 200)
        self.assertEqual(session["full_session_id"], "sess-abc123")
        self.assertEqual(session["tools"], ["<none>"])

    def test_daily_by_model_populated(self):
        data = get_dashboard_data(db_path=self.db_path)
        self.assertGreater(len(data["daily_by_model"]), 0)
        day = data["daily_by_model"][0]
        self.assertIn("day", day)
        self.assertIn("model", day)
        self.assertIn("input", day)

    def test_missing_db_returns_error(self):
        data = get_dashboard_data(db_path=Path("/nonexistent/path/usage.db"))
        self.assertIn("error", data)

    def test_session_id_sent_in_full(self):
        # The table displays an 8-char prefix (session_id), but the full value is
        # sent as full_session_id for the drilldown link and CSV export.
        data = get_dashboard_data(db_path=self.db_path)
        session = data["sessions_all"][0]
        self.assertEqual(session["full_session_id"], "sess-abc123")
        self.assertEqual(session["session_id"], "sess-abc123"[:8])

    def test_session_duration_calculated(self):
        data = get_dashboard_data(db_path=self.db_path)
        session = data["sessions_all"][0]
        # 1 hour = 60 minutes
        self.assertEqual(session["duration_min"], 60.0)

    def test_hourly_by_model_present(self):
        data = get_dashboard_data(db_path=self.db_path)
        self.assertIn("hourly_by_model", data)
        self.assertIsInstance(data["hourly_by_model"], list)

    def test_hourly_by_model_buckets_by_utc_hour(self):
        data = get_dashboard_data(db_path=self.db_path)
        rows = data["hourly_by_model"]
        # Two turns at UTC 09:30 and 14:15 → two hour buckets
        by_hour = {r["hour"]: r for r in rows}
        self.assertIn(9, by_hour)
        self.assertIn(14, by_hour)
        self.assertEqual(by_hour[9]["turns"], 1)
        self.assertEqual(by_hour[9]["output"], 200)
        self.assertEqual(by_hour[14]["turns"], 1)
        self.assertEqual(by_hour[14]["output"], 150)

    def test_hourly_by_model_carries_day_and_model(self):
        data = get_dashboard_data(db_path=self.db_path)
        rows = data["hourly_by_model"]
        self.assertTrue(all("day" in r and "model" in r for r in rows))
        self.assertTrue(all(r["model"] == "claude-sonnet-4-6" for r in rows))
        # Days are bucketed in local time, so the expected values are derived the
        # same way rather than hardcoded — a literal would assert the test
        # machine's timezone, not the code.
        expected = {local_date(t) for t in ("2026-04-08T09:30:00Z", "2026-04-08T14:15:00Z")}
        self.assertTrue(all(r["day"] in expected for r in rows))

    def test_tool_by_model_populated(self):
        data = get_dashboard_data(db_path=self.db_path)
        rows = data["tool_by_model"]
        self.assertGreater(len(rows), 0)
        self.assertIn("tool", rows[0])
        self.assertIn("cache_read", rows[0])

    def test_session_detail_returns_turns(self):
        data = get_session_detail("sess-abc123", db_path=self.db_path)
        self.assertEqual(data["session"]["session_id"], "sess-abc123")
        self.assertEqual(data["session"]["cache_read"], 500)
        self.assertEqual(len(data["turns"]), 2)
        self.assertEqual(data["turns"][0]["input"], 500)

    def test_session_detail_missing_session(self):
        data = get_session_detail("missing", db_path=self.db_path)
        self.assertIn("error", data)


class TestEmptyStringModelNormalization(unittest.TestCase):
    """Regression: turns with model='' (empty string) must group as 'unknown'.
    COALESCE(model, 'unknown') alone returns '' because empty string isn't NULL;
    NULLIF(model, '') is needed first."""

    def setUp(self):
        self.tmpfile = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmpfile.close()
        self.db_path = Path(self.tmpfile.name)
        conn = get_db(self.db_path)
        init_db(conn)
        upsert_sessions(conn, [{
            "session_id": "sess-empty", "project_name": "u/p",
            "first_timestamp": "2026-04-08T09:00:00Z",
            "last_timestamp": "2026-04-08T09:05:00Z",
            "git_branch": "", "model": "",
            "total_input_tokens": 100, "total_output_tokens": 50,
            "total_cache_read": 0, "total_cache_creation": 0,
            "turn_count": 1,
        }])
        insert_turns(conn, [{
            "session_id": "sess-empty", "timestamp": "2026-04-08T09:05:00Z",
            "model": "", "input_tokens": 100, "output_tokens": 50,
            "cache_read_tokens": 0, "cache_creation_tokens": 0,
            "tool_name": None, "cwd": "/tmp",
        }])
        conn.commit()
        conn.close()

    def tearDown(self):
        os.unlink(self.db_path)

    def test_all_models_contains_unknown_not_empty(self):
        data = get_dashboard_data(db_path=self.db_path)
        self.assertIn("unknown", data["all_models"])
        self.assertNotIn("", data["all_models"])

    def test_daily_by_model_contains_unknown_not_empty(self):
        data = get_dashboard_data(db_path=self.db_path)
        models = {r["model"] for r in data["daily_by_model"]}
        self.assertIn("unknown", models)
        self.assertNotIn("", models)

    def test_hourly_by_model_contains_unknown_not_empty(self):
        data = get_dashboard_data(db_path=self.db_path)
        models = {r["model"] for r in data["hourly_by_model"]}
        self.assertIn("unknown", models)
        self.assertNotIn("", models)


class TestMixedNullAndEmptyModel(unittest.TestCase):
    """Regression: a mix of model=NULL and model='' rows must collapse into a
    SINGLE 'unknown' group across all aggregations. Without `GROUP BY
    COALESCE(NULLIF(model, ''), 'unknown')` (matching the SELECT expression),
    SQLite groups by raw value and emits two distinct 'unknown' rows."""

    def setUp(self):
        self.tmpfile = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmpfile.close()
        self.db_path = Path(self.tmpfile.name)
        conn = get_db(self.db_path)
        init_db(conn)
        upsert_sessions(conn, [{
            "session_id": "sess-mix", "project_name": "u/p",
            "first_timestamp": "2026-04-08T09:00:00Z",
            "last_timestamp": "2026-04-08T10:00:00Z",
            "git_branch": "", "model": "",
            "total_input_tokens": 200, "total_output_tokens": 100,
            "total_cache_read": 0, "total_cache_creation": 0,
            "turn_count": 2,
        }])
        # Insert one turn with model='' and one with model=NULL on the same day.
        # Use raw INSERT for the NULL row because insert_turns() requires the
        # model key to exist (would error on missing key, not on None).
        insert_turns(conn, [{
            "session_id": "sess-mix", "timestamp": "2026-04-08T09:00:00Z",
            "model": "", "input_tokens": 100, "output_tokens": 50,
            "cache_read_tokens": 0, "cache_creation_tokens": 0,
            "tool_name": None, "cwd": "/tmp",
        }])
        conn.execute("""
            INSERT INTO turns (session_id, timestamp, model, input_tokens,
                output_tokens, cache_read_tokens, cache_creation_tokens,
                tool_name, cwd)
            VALUES ('sess-mix', '2026-04-08T09:30:00Z', NULL, 100, 50, 0, 0, NULL, '/tmp')
        """)
        conn.commit()
        conn.close()

    def tearDown(self):
        os.unlink(self.db_path)

    def test_all_models_collapses_to_single_unknown(self):
        data = get_dashboard_data(db_path=self.db_path)
        unknowns = [m for m in data["all_models"] if m == "unknown"]
        self.assertEqual(len(unknowns), 1, f"got duplicate 'unknown' rows: {data['all_models']}")

    def test_daily_collapses_to_single_unknown(self):
        data = get_dashboard_data(db_path=self.db_path)
        unknown_rows = [r for r in data["daily_by_model"] if r["model"] == "unknown"]
        # One day, one model bucket
        self.assertEqual(len(unknown_rows), 1, f"got {unknown_rows}")
        self.assertEqual(unknown_rows[0]["turns"], 2)
        self.assertEqual(unknown_rows[0]["input"], 200)

    def test_hourly_collapses_to_single_unknown(self):
        data = get_dashboard_data(db_path=self.db_path)
        # Both turns are in UTC hour 9 — must be one row, not two
        hour9 = [r for r in data["hourly_by_model"]
                 if r["hour"] == 9 and r["model"] == "unknown"]
        self.assertEqual(len(hour9), 1, f"got {hour9}")
        self.assertEqual(hour9[0]["turns"], 2)


class TestDefaultModelSelectionShowsAll(unittest.TestCase):
    """Regression: local-LLM / non-billable runs (qwen, gemma, glm — or turns
    with no model field) must NOT be hidden by default, even when billable
    Anthropic runs coexist in the same data. The model filter controls
    visibility, not billing, so the default selection is ALL models; cost
    columns still show N/A for non-billable models via isBillable()."""

    def test_default_model_selection_is_all_models(self):
        # The selection logic is JS; assert the source defaults to all models so
        # a future refactor doesn't silently re-introduce the billable-only
        # default that hid local-LLM runs.
        self.assertIn("if (!param) return new Set(allModels)", HTML_TEMPLATE)


class TestDashboardHTTP(unittest.TestCase):
    """Integration test: start server and make HTTP requests."""

    @classmethod
    def setUpClass(cls):
        # Redirect DB_PATH + projects dirs to a tempdir so /api/rescan
        # writes to a throwaway DB and scans a throwaway transcript dir
        # instead of the user's real ~/.claude/usage.db and transcripts.
        import dashboard as _d
        import scanner as _s
        cls._tmpdir = tempfile.TemporaryDirectory()
        tmp = Path(cls._tmpdir.name)
        tmp_projects = tmp / "projects"
        tmp_projects.mkdir()
        cls._patches = {
            (_d, "DB_PATH"):                (_d.DB_PATH,                tmp / "usage.db"),
            (_s, "DB_PATH"):                (_s.DB_PATH,                tmp / "usage.db"),
            (_s, "PROJECTS_DIR"):           (_s.PROJECTS_DIR,           tmp_projects),
            (_s, "DEFAULT_PROJECTS_DIRS"):  (_s.DEFAULT_PROJECTS_DIRS,  [tmp_projects]),
        }
        for (mod, name), (_orig, new) in cls._patches.items():
            setattr(mod, name, new)

        cls.server = HTTPServer(("127.0.0.1", 0), DashboardHandler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever)
        cls.thread.daemon = True
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        for (mod, name), (orig, _new) in cls._patches.items():
            setattr(mod, name, orig)
        cls._tmpdir.cleanup()

    def test_index_returns_html(self):
        url = f"http://127.0.0.1:{self.port}/"
        with urllib.request.urlopen(url) as resp:
            self.assertEqual(resp.status, 200)
            self.assertIn("text/html", resp.headers["Content-Type"])

    def test_index_with_query_string_returns_html(self):
        # Regression: ?range=... and ?models=... must not 404. The dashboard
        # itself rewrites the URL with these params via history.replaceState,
        # so anything that reloads or bookmarks the page hits this path.
        for qs in ("?range=all", "?range=30d&models=claude-opus-4-7"):
            with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/{qs}") as resp:
                self.assertEqual(resp.status, 200)
                self.assertIn(b"Claude Code Usage", resp.read())

    def test_api_data_with_query_string(self):
        # /api/data is fetched without query parameters today, but the route
        # should be tolerant if any are tacked on (e.g. cache-busting).
        with urllib.request.urlopen(
            f"http://127.0.0.1:{self.port}/api/data?_=cachebust"
        ) as resp:
            self.assertEqual(resp.status, 200)

    def test_api_data_returns_json(self):
        url = f"http://127.0.0.1:{self.port}/api/data"
        with urllib.request.urlopen(url) as resp:
            self.assertEqual(resp.status, 200)
            self.assertIn("application/json", resp.headers["Content-Type"])
            data = json.loads(resp.read())
            # Should have expected keys (or error if no DB)
            self.assertTrue("all_models" in data or "error" in data)

    def test_api_session_returns_json(self):
        url = f"http://127.0.0.1:{self.port}/api/session?session_id=missing"
        with urllib.request.urlopen(url) as resp:
            self.assertEqual(resp.status, 200)
            self.assertIn("application/json", resp.headers["Content-Type"])
            data = json.loads(resp.read())
            self.assertIn("error", data)

    def test_api_rescan_returns_json(self):
        url = f"http://127.0.0.1:{self.port}/api/rescan"
        req = urllib.request.Request(url, method="POST")
        with urllib.request.urlopen(req) as resp:
            self.assertEqual(resp.status, 200)
            self.assertIn("application/json", resp.headers["Content-Type"])
            data = json.loads(resp.read())
            self.assertIn("new", data)
            self.assertIn("updated", data)
            self.assertIn("skipped", data)

    def test_api_rescan_is_non_destructive(self):
        # Regression (#138): /api/rescan must NOT wipe the DB. usage.db is the
        # only durable store of history once Claude Code prunes old transcripts
        # (cleanupPeriodDays), so a rescan with nothing left on disk must keep
        # the existing rows. Seed history that has no corresponding JSONL file
        # (the projects dir is empty), rescan, and assert it survives.
        import dashboard as _d
        db_path = _d.DB_PATH
        conn = get_db(db_path)
        init_db(conn)
        upsert_sessions(conn, [{
            "session_id": "pruned-sess", "project_name": "user/oldproject",
            "first_timestamp": "2026-01-01T09:00:00Z",
            "last_timestamp": "2026-01-01T10:00:00Z",
            "git_branch": "main", "model": "claude-opus-4-8",
            "total_input_tokens": 1000, "total_output_tokens": 400,
            "total_cache_read": 0, "total_cache_creation": 0,
            "turn_count": 1,
        }])
        insert_turns(conn, [{
            "session_id": "pruned-sess", "timestamp": "2026-01-01T09:30:00Z",
            "model": "claude-opus-4-8", "input_tokens": 1000,
            "output_tokens": 400, "cache_read_tokens": 0,
            "cache_creation_tokens": 0, "tool_name": None, "cwd": "/tmp",
            "message_id": "msg-pruned-1",
        }])
        conn.commit()
        conn.close()

        url = f"http://127.0.0.1:{self.port}/api/rescan"
        req = urllib.request.Request(url, method="POST")
        with urllib.request.urlopen(req) as resp:
            self.assertEqual(resp.status, 200)

        conn = sqlite3.connect(db_path)
        try:
            turn_count = conn.execute(
                "SELECT COUNT(*) FROM turns WHERE session_id = 'pruned-sess'"
            ).fetchone()[0]
            sess_count = conn.execute(
                "SELECT COUNT(*) FROM sessions WHERE session_id = 'pruned-sess'"
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(turn_count, 1, "rescan must not delete existing turns")
        self.assertEqual(sess_count, 1, "rescan must not delete existing sessions")

    def test_404_for_unknown_path(self):
        url = f"http://127.0.0.1:{self.port}/nonexistent"
        try:
            urllib.request.urlopen(url)
            self.fail("Expected 404")
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 404)

    def test_index_injects_app_config(self):
        # do_GET must substitute the __APP_CONFIG_JSON__ placeholder with a real
        # JSON object (version). The raw placeholder must never reach the
        # browser, or window.APP_CONFIG would be a syntax error.
        from scanner import VERSION
        url = f"http://127.0.0.1:{self.port}/"
        with urllib.request.urlopen(url) as resp:
            body = resp.read().decode("utf-8")
        self.assertNotIn("__APP_CONFIG_JSON__", body)
        self.assertIn("window.APP_CONFIG =", body)
        self.assertIn(VERSION, body)
        self.assertIn(f'"version": "{VERSION}"', body)


class TestHTMLTemplate(unittest.TestCase):
    def test_template_is_valid_html(self):
        self.assertIn("<!DOCTYPE html>", HTML_TEMPLATE)
        self.assertIn("</html>", HTML_TEMPLATE)

    def test_template_has_esc_function(self):
        """Verify XSS protection is present (PR #10)."""
        self.assertIn("function esc(", HTML_TEMPLATE)

    def test_template_has_chart_js(self):
        self.assertIn("chart.js", HTML_TEMPLATE.lower())

    def test_template_has_substring_matching(self):
        """Verify getPricing falls back to substring match for unknown models."""
        self.assertIn("m.includes('opus')", HTML_TEMPLATE)
        self.assertIn("m.includes('sonnet')", HTML_TEMPLATE)
        self.assertIn("m.includes('haiku')", HTML_TEMPLATE)

    def test_unknown_models_return_null(self):
        """Verify getPricing returns null for non-Anthropic models."""
        self.assertIn("return null;", HTML_TEMPLATE)

    def test_hourly_chart_canvas_present(self):
        """Hourly distribution chart has a canvas + TZ toggle."""
        self.assertIn('id="chart-hourly"', HTML_TEMPLATE)
        self.assertIn('data-tz="local"', HTML_TEMPLATE)
        self.assertIn('data-tz="utc"', HTML_TEMPLATE)

    def test_hourly_peak_hour_constants(self):
        """Peak-hour set covers UTC 12–17 (Mon–Fri 05:00–11:00 PT)."""
        self.assertIn('PEAK_HOURS_UTC', HTML_TEMPLATE)
        self.assertIn('[12, 13, 14, 15, 16, 17]', HTML_TEMPLATE)

    def test_recent_sessions_shows_cache_tokens(self):
        """Recent sessions table exposes session-level cache token usage."""
        self.assertIn("setSessionSort('cache_read')", HTML_TEMPLATE)
        self.assertIn("setSessionSort('cache_creation')", HTML_TEMPLATE)
        self.assertIn("${fmt(s.cache_read)}", HTML_TEMPLATE)
        self.assertIn("${fmt(s.cache_creation)}", HTML_TEMPLATE)

    def test_template_has_drilldown_and_metric_tables(self):
        self.assertIn('id="session-detail-view"', HTML_TEMPLATE)
        self.assertIn('id="chart-session-timeline"', HTML_TEMPLATE)
        self.assertIn('id="session-tool-breakdown-body"', HTML_TEMPLATE)
        self.assertIn('id="cost-breakdown-body"', HTML_TEMPLATE)
        self.assertIn('id="tool-cost-body"', HTML_TEMPLATE)
        self.assertIn('id="session-signals-body"', HTML_TEMPLATE)
        self.assertIn("function openSessionDetail(", HTML_TEMPLATE)
        self.assertIn("function closeSessionDetail(", HTML_TEMPLATE)
        self.assertIn("function updateURLForSession(", HTML_TEMPLATE)
        self.assertIn("function renderSessionToolBreakdown(", HTML_TEMPLATE)

    def test_template_has_session_filters(self):
        self.assertIn('id="session-project-filter"', HTML_TEMPLATE)
        self.assertIn('id="session-branch-filter"', HTML_TEMPLATE)
        self.assertIn('id="session-tool-filter"', HTML_TEMPLATE)
        self.assertIn('id="session-min-cost-filter"', HTML_TEMPLATE)
        self.assertIn('id="session-cache-filter"', HTML_TEMPLATE)
        self.assertIn("function applySessionFilters(", HTML_TEMPLATE)

    def test_template_has_editable_pricing(self):
        self.assertIn('id="pricing-body"', HTML_TEMPLATE)
        self.assertIn("PRICING_STORAGE_KEY", HTML_TEMPLATE)
        self.assertIn("function renderPricingEditor(", HTML_TEMPLATE)
        self.assertIn("function onPricingChange(", HTML_TEMPLATE)
        self.assertIn("function resetPricing(", HTML_TEMPLATE)

    def test_hourly_filter_uses_range_bounds(self):
        """Hourly filter should use the same date bounds as the rest of the UI."""
        self.assertNotIn("cutoff", HTML_TEMPLATE)
        self.assertIn("(!start || r.day >= start) && (!end || r.day <= end)", HTML_TEMPLATE)

    def test_today_range_option_present(self):
        """The 'Today' range is wired into RANGE_LABELS, RANGE_TICKS,
        getRangeBounds, and the filter-bar range dropdown."""
        self.assertIn("<option value=\"today\">", HTML_TEMPLATE)
        self.assertIn("'today': 'Today'", HTML_TEMPLATE)
        self.assertIn("'today': 1", HTML_TEMPLATE)
        # Bounds case: today returns start === end === today's ISO date
        self.assertIn("range === 'today'", HTML_TEMPLATE)

    def test_app_config_placeholder_present(self):
        """The head carries the server-substituted config placeholder and the
        footer carries the element + JS the version/update feature drives."""
        self.assertIn("__APP_CONFIG_JSON__", HTML_TEMPLATE)
        self.assertIn("window.APP_CONFIG", HTML_TEMPLATE)
        self.assertIn('id="footer-meta"', HTML_TEMPLATE)
        self.assertIn("function initFooterMeta(", HTML_TEMPLATE)
        self.assertIn("function checkForUpdate(", HTML_TEMPLATE)

    def test_update_check_points_at_fork(self):
        """The GitHub update check hits the fork's public releases API (not
        upstream's), and no VS Code marketplace promo survives in the footer."""
        self.assertIn("api.github.com/repos/ollo12-prog/claude-usage/releases/latest", HTML_TEMPLATE)
        self.assertNotIn("marketplace.visualstudio.com", HTML_TEMPLATE)
        self.assertNotIn("Get the VS Code extension", HTML_TEMPLATE)


class TestPricingParity(unittest.TestCase):
    """Verify CLI and dashboard pricing tables stay in sync."""

    def _extract_js_pricing(self):
        """Extract pricing values from the dashboard JS PRICING object."""
        import re
        prices = {}
        for match in re.finditer(
            r"'(claude-[^']+)':\s*\{\s*input:\s*([\d.]+),\s*output:\s*([\d.]+)",
            HTML_TEMPLATE
        ):
            model, inp, out = match.group(1), float(match.group(2)), float(match.group(3))
            prices[model] = {"input": inp, "output": out}
        return prices

    def _expected_prices(self):
        """CLI prices as they should appear in the JS literal."""
        import cli
        return dict(cli.PRICING)

    def test_fable_51_cache_read_is_a_quarter_of_fable_5(self):
        """The 0.025x cache-hit rate, in both tables.

        The parity check above compares only input/output, so a cache_read that
        drifted between cli.py and the dashboard JS — or a 5.1 id silently priced
        as 5.0 — would pass it. That is a 4x overcharge on the token class that
        dominates an agentic session.
        """
        import cli
        self.assertEqual(cli.get_pricing("claude-fable-5-1")["cache_read"], 0.25)
        self.assertEqual(cli.get_pricing("claude-fable-5")["cache_read"], 1.00)
        # A dated variant must not fall through to Fable 5's prefix.
        self.assertEqual(
            cli.get_pricing("claude-fable-5-1-20260901")["cache_read"], 0.25)
        self.assertEqual(cli.get_pricing("claude-mythos-5-1")["cache_read"], 0.25)

        js = dict(re.findall(
            r"'(claude-fable-5-1|claude-fable-5)':\s*\{[^}]*cache_read:\s*([\d.]+)",
            HTML_TEMPLATE))
        self.assertEqual(float(js["claude-fable-5-1"]), 0.25)
        self.assertEqual(float(js["claude-fable-5"]), 1.00)
        self.assertLess(HTML_TEMPLATE.index("'claude-fable-5-1'"),
                        HTML_TEMPLATE.index("'claude-fable-5'"))

    def test_all_cli_models_in_dashboard(self):
        js_prices = self._extract_js_pricing()
        for model in self._expected_prices():
            self.assertIn(model, js_prices, f"{model} missing from dashboard JS")

    def test_prices_match(self):
        js_prices = self._extract_js_pricing()
        for model, expected in self._expected_prices().items():
            self.assertAlmostEqual(
                expected["input"], js_prices[model]["input"],
                msg=f"{model} input price mismatch"
            )
            self.assertAlmostEqual(
                expected["output"], js_prices[model]["output"],
                msg=f"{model} output price mismatch"
            )

    def test_sonnet_5_cache_rates_match(self):
        """Sonnet 5's full rate row must agree across the two hand-maintained
        tables. The parity check above compares only input/output, so a drifted
        cache_read would pass it."""
        import cli
        m = re.search(r"'claude-sonnet-5':\s*\{([^}]*)\}", HTML_TEMPLATE)
        self.assertIsNotNone(m, "claude-sonnet-5 missing from dashboard JS")
        js_rates = {k: float(v) for k, v in re.findall(r"(\w+): *([\d.]+)", m.group(1))}
        py_rates = cli.PRICING["claude-sonnet-5"]
        self.assertEqual(set(js_rates), set(py_rates),
                         "claude-sonnet-5 has different rate KEYS in cli.py vs dashboard.py")
        for field, py_value in py_rates.items():
            self.assertAlmostEqual(py_value, js_rates[field],
                                   msg=f"claude-sonnet-5 {field} differs between cli.py and dashboard.py")


class TestMixedModelSessionCost(unittest.TestCase):
    """A session's tokens must be priced by the model that produced them.

    The `sessions` table stores one primary-model label (opus > sonnet > haiku),
    so pricing summed session tokens by that label overcharges any session that
    also ran cheaper models (subagents, or a mid-session /model switch).
    """

    def setUp(self):
        self.tmpfile = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmpfile.close()
        self.db_path = Path(self.tmpfile.name)
        conn = get_db(self.db_path)
        init_db(conn)
        upsert_sessions(conn, [{
            "session_id": "sess-mixed", "project_name": "user/proj",
            "first_timestamp": "2026-04-08T09:00:00Z",
            "last_timestamp": "2026-04-08T10:00:00Z",
            "git_branch": "main", "model": "claude-opus-4-8",  # primary label
            "total_input_tokens": 300, "total_output_tokens": 3000,
            "total_cache_read": 30000, "total_cache_creation": 3000,
            "turn_count": 2,
        }])
        insert_turns(conn, [
            {"session_id": "sess-mixed", "timestamp": "2026-04-08T09:10:00Z",
             "model": "claude-opus-4-8", "input_tokens": 100, "output_tokens": 1000,
             "cache_read_tokens": 10000, "cache_creation_tokens": 1000,
             "tool_name": None, "cwd": "/tmp"},
            {"session_id": "sess-mixed", "timestamp": "2026-04-08T09:20:00Z",
             "model": "claude-haiku-4-5", "input_tokens": 200, "output_tokens": 2000,
             "cache_read_tokens": 20000, "cache_creation_tokens": 2000,
             "tool_name": None, "cwd": "/tmp"},
        ])
        conn.commit()
        conn.close()

    def tearDown(self):
        os.unlink(self.db_path)

    def test_session_carries_per_model_breakdown(self):
        data = get_dashboard_data(self.db_path)
        session = next(s for s in data["sessions_all"] if s["full_session_id"] == "sess-mixed")
        by_model = {m["model"]: m for m in session["by_model"]}
        self.assertEqual(set(by_model), {"claude-opus-4-8", "claude-haiku-4-5"},
                         "by_model must split the session's tokens by producing model")
        self.assertEqual(by_model["claude-haiku-4-5"]["output"], 2000)
        self.assertEqual(by_model["claude-opus-4-8"]["cache_read"], 10000)
        # The split must be lossless against the session rollup.
        for field in ("input", "output", "cache_read", "cache_creation"):
            self.assertEqual(sum(m[field] for m in session["by_model"]), session[field],
                             f"by_model {field} must sum to the session total")

    def test_per_model_pricing_is_cheaper_than_the_primary_label(self):
        """The bug this guards: haiku turns billed at opus rates."""
        import cli
        data = get_dashboard_data(self.db_path)
        session = next(s for s in data["sessions_all"] if s["full_session_id"] == "sess-mixed")
        per_model = sum(cli.calc_cost(m["model"], m["input"], m["output"],
                                      m["cache_read"], m["cache_creation"])
                        for m in session["by_model"])
        single_label = cli.calc_cost(session["model"], session["input"], session["output"],
                                     session["cache_read"], session["cache_creation"])
        self.assertLess(per_model, single_label)

    def test_js_prices_sessions_per_model(self):
        """The cost math runs in JS; keep the session sites off the single-label path."""
        # assertTrue, not assertIn: assertIn dumps the whole 130KB template on failure.
        fallback = ("  return calcCostBreakdown(s.model, s.input, s.output, "
                    "s.cache_read, s.cache_creation, s.cache_creation_1h);")
        self.assertTrue("function sessionCostBreakdown(s)" in HTML_TEMPLATE,
                        "sessionCostBreakdown missing from dashboard JS")
        self.assertFalse("calcCost(s.model," in HTML_TEMPLATE,
                         "a session is still priced by its single primary-model label")
        self.assertFalse("calcCostBreakdown(s.model," in HTML_TEMPLATE.replace(fallback, ""),
                         "a session breakdown is still priced by its single primary-model label")


class TestByModelCarriesThe1hSplit(unittest.TestCase):
    """Every consumer of byModel prices with `m.cache_creation_1h` — the overview
    Est. Cost card, the cost-by-model table, the CSV export. If the aggregator
    never accumulates that field it arrives `undefined`, calcCostBreakdown's
    default silently prices the whole thing at the 5m rate, and the dashboard
    reports less than it costs (measured: $2,892.66 against $3,037.05 over one
    7-day window). Source-level guard — the aggregation lives inside a large JS
    function with no seam to call.
    """

    def test_model_aggregator_accumulates_the_1h_split(self):
        head, _, tail = HTML_TEMPLATE.partition("const modelMap = {};")
        self.assertTrue(tail, "modelMap aggregation not found in dashboard JS")
        block = tail[:tail.index("\n  }")]
        self.assertIn("cache_creation_1h: 0", block,
                      "modelMap rows start without a cache_creation_1h field")
        self.assertIn("m.cache_creation_1h +=", block,
                      "modelMap never accumulates cache_creation_1h — every byModel "
                      "cost falls back to the 5m cache-write rate")


class TestLocalDayBucketing(unittest.TestCase):
    """Every range-filtered aggregate must bucket days in LOCAL time.

    The frontend derives its range bounds from local calendar components
    (`localISODate`, PR #151's "This Month" fix), and the CLI compares against
    `date.today()`. A UTC `substr(timestamp, 1, 10)` bucket compared against those
    bounds is off by up to a day at each edge — measured on a real UTC-4 database:
    22,722 turns / 65.86M input tokens under UTC bucketing against 22,055 / 64.14M
    under local, an $85 swing on a 7-day window.

    Falsification note: the discriminating assertions are skipped when the test
    machine runs at UTC, where the two bucketings are identical by definition and
    nothing here can tell them apart. Verified red by monkeypatching
    `dashboard.LOCAL_DAY` back to `substr(timestamp, 1, 10)`.
    """

    # NOON never crosses a local midnight for any real offset; SHIFTED always does
    # unless the machine is at UTC — picked by the sign of the local offset, since
    # no single fixed hour shifts in both eastern and western zones. Both fall on
    # the same UTC calendar day, so UTC bucketing collapses them into one bucket
    # and local bucketing splits them into two — that is the discriminator.
    NOON_UTC = "2026-04-08T12:00:00Z"
    SHIFTED_UTC = ("2026-04-08T23:30:00Z"
                   if datetime.now().astimezone().utcoffset().total_seconds() > 0
                   else "2026-04-08T00:30:00Z")

    def setUp(self):
        self.tmpfile = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmpfile.close()
        self.db_path = Path(self.tmpfile.name)
        conn = get_db(self.db_path)
        init_db(conn)
        upsert_sessions(conn, [{
            "session_id": "sess-tz", "project_name": "user/proj",
            "first_timestamp": self.NOON_UTC, "last_timestamp": self.SHIFTED_UTC,
            "git_branch": "main", "model": "claude-sonnet-4-6",
            "total_input_tokens": 200, "total_output_tokens": 100,
            "total_cache_read": 0, "total_cache_creation": 0, "turn_count": 2,
        }])
        insert_turns(conn, [{
            "session_id": "sess-tz", "timestamp": ts,
            "model": "claude-sonnet-4-6", "input_tokens": 100, "output_tokens": 50,
            "cache_read_tokens": 0, "cache_creation_tokens": 0,
            "tool_name": "Read", "cwd": "/tmp",
        } for ts in (self.NOON_UTC, self.SHIFTED_UTC)])
        conn.commit()
        conn.close()

    def tearDown(self):
        os.unlink(self.db_path)

    def _discriminates(self):
        """True when this machine's timezone actually moves SHIFTED_UTC onto another
        calendar day — i.e. when the assertions below can tell the bucketings apart."""
        return local_date(self.SHIFTED_UTC) != self.SHIFTED_UTC[:10]

    def test_daily_hourly_and_tool_buckets_are_local_days(self):
        data = get_dashboard_data(db_path=self.db_path)
        expected = {local_date(self.NOON_UTC), local_date(self.SHIFTED_UTC)}
        for key in ("daily_by_model", "hourly_by_model", "tool_by_model"):
            days = {r["day"] for r in data[key]}
            self.assertEqual(days, expected, f"{key} is not bucketed by local day")

    def test_hourly_keeps_utc_hours(self):
        """`day` goes local but `hour` stays UTC — the client shifts hours itself
        (utcHourToDisplay), so shifting them here too would double-shift."""
        data = get_dashboard_data(db_path=self.db_path)
        self.assertEqual({r["hour"] for r in data["hourly_by_model"]},
                         {12, int(self.SHIFTED_UTC[11:13])})

    def test_session_last_date_is_a_local_day(self):
        """The sessions list is range-filtered on `last_date`, so a UTC slice there
        classifies sessions into a different day than the token buckets above."""
        data = get_dashboard_data(db_path=self.db_path)
        row = next(s for s in data["sessions_all"] if s["full_session_id"] == "sess-tz")
        self.assertEqual(row["last_date"], local_date(self.SHIFTED_UTC))
        if not self._discriminates():
            self.skipTest("machine is at UTC — local and UTC days coincide")
        self.assertNotEqual(row["last_date"], self.SHIFTED_UTC[:10])

    def test_utc_bucketing_would_disagree(self):
        """Guards the guard: both fixtures share one UTC day, so a UTC bucket would
        yield a single day. Two distinct days proves the assertions above are not
        passing vacuously."""
        if not self._discriminates():
            self.skipTest("machine is at UTC — local and UTC days coincide")
        data = get_dashboard_data(db_path=self.db_path)
        self.assertEqual(len({r["day"] for r in data["daily_by_model"]}), 2)


if __name__ == "__main__":
    unittest.main()
