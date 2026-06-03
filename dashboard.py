"""
dashboard.py - Local web dashboard served on localhost:8080.
"""

import json
import os
import sqlite3
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from datetime import datetime
from urllib.parse import parse_qs, urlparse

DB_PATH = Path.home() / ".claude" / "usage.db"


def get_dashboard_data(db_path=DB_PATH):
    if not db_path.exists():
        return {"error": "Database not found. Run: python cli.py scan"}

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    # ── All models (for filter UI) ────────────────────────────────────────────
    # GROUP BY uses the normalised expression too so NULL and '' don't end up
    # as two separate "unknown" rows.
    model_rows = conn.execute("""
        SELECT COALESCE(NULLIF(model, ''), 'unknown') as model
        FROM turns
        GROUP BY COALESCE(NULLIF(model, ''), 'unknown')
        ORDER BY SUM(input_tokens + output_tokens) DESC
    """).fetchall()
    all_models = [r["model"] for r in model_rows]

    # ── Daily per-model, ALL history (client filters by range) ────────────────
    daily_rows = conn.execute("""
        SELECT
            substr(timestamp, 1, 10)   as day,
            COALESCE(NULLIF(model, ''), 'unknown') as model,
            SUM(input_tokens)          as input,
            SUM(output_tokens)         as output,
            SUM(cache_read_tokens)     as cache_read,
            SUM(cache_creation_tokens) as cache_creation,
            COUNT(*)                   as turns
        FROM turns
        GROUP BY day, COALESCE(NULLIF(model, ''), 'unknown')
        ORDER BY day, model
    """).fetchall()

    daily_by_model = [{
        "day":            r["day"],
        "model":          r["model"],
        "input":          r["input"] or 0,
        "output":         r["output"] or 0,
        "cache_read":     r["cache_read"] or 0,
        "cache_creation": r["cache_creation"] or 0,
        "turns":          r["turns"] or 0,
    } for r in daily_rows]

    # ── Hourly per-day per-model (client filters by range + TZ-shifts) ────────
    # Timestamps are ISO8601 UTC (e.g. "2026-04-08T09:30:00Z"); chars 12-13 = hour.
    hourly_rows = conn.execute("""
        SELECT
            substr(timestamp, 1, 10)                  as day,
            CAST(substr(timestamp, 12, 2) AS INTEGER) as hour,
            COALESCE(NULLIF(model, ''), 'unknown')    as model,
            SUM(output_tokens)                        as output,
            COUNT(*)                                  as turns
        FROM turns
        WHERE timestamp IS NOT NULL AND length(timestamp) >= 13
        GROUP BY day, hour, COALESCE(NULLIF(model, ''), 'unknown')
        ORDER BY day, hour, model
    """).fetchall()

    hourly_by_model = [{
        "day":    r["day"],
        "hour":   r["hour"] if r["hour"] is not None else 0,
        "model":  r["model"],
        "output": r["output"] or 0,
        "turns":  r["turns"] or 0,
    } for r in hourly_rows]

    # ── Tool usage per-day per-model (client filters by range) ────────────────
    tool_rows = conn.execute("""
        SELECT
            substr(timestamp, 1, 10)        as day,
            COALESCE(model, 'unknown')      as model,
            COALESCE(tool_name, '<none>')   as tool,
            SUM(input_tokens)               as input,
            SUM(output_tokens)              as output,
            SUM(cache_read_tokens)          as cache_read,
            SUM(cache_creation_tokens)      as cache_creation,
            COUNT(*)                        as turns
        FROM turns
        GROUP BY day, model, tool
        ORDER BY day, model, tool
    """).fetchall()

    tool_by_model = [{
        "day":            r["day"],
        "model":          r["model"],
        "tool":           r["tool"],
        "input":          r["input"] or 0,
        "output":         r["output"] or 0,
        "cache_read":     r["cache_read"] or 0,
        "cache_creation": r["cache_creation"] or 0,
        "turns":          r["turns"] or 0,
    } for r in tool_rows]

    # ── All sessions (client filters by range and model) ──────────────────────
    session_rows = conn.execute("""
        SELECT
            s.session_id, s.project_name, s.first_timestamp, s.last_timestamp,
            s.total_input_tokens, s.total_output_tokens,
            s.total_cache_read, s.total_cache_creation, s.model, s.turn_count,
            s.git_branch,
            COALESCE(t.tools, '') as tools
        FROM sessions s
        LEFT JOIN (
            SELECT
                session_id,
                GROUP_CONCAT(DISTINCT COALESCE(tool_name, '<none>')) as tools
            FROM turns
            GROUP BY session_id
        ) t ON t.session_id = s.session_id
        ORDER BY s.last_timestamp DESC
    """).fetchall()

    sessions_all = []
    for r in session_rows:
        try:
            t1 = datetime.fromisoformat(r["first_timestamp"].replace("Z", "+00:00"))
            t2 = datetime.fromisoformat(r["last_timestamp"].replace("Z", "+00:00"))
            duration_min = round((t2 - t1).total_seconds() / 60, 1)
        except Exception:
            duration_min = 0
        sessions_all.append({
            "session_id":    r["session_id"][:8],
            "full_session_id": r["session_id"],
            "project":       r["project_name"] or "unknown",
            "branch":        r["git_branch"] or "",
            "last":          (r["last_timestamp"] or "")[:16].replace("T", " "),
            "last_date":     (r["last_timestamp"] or "")[:10],
            "duration_min":  duration_min,
            "model":         r["model"] or "unknown",
            "tools":         [x for x in (r["tools"] or "").split(",") if x],
            "turns":         r["turn_count"] or 0,
            "input":         r["total_input_tokens"] or 0,
            "output":        r["total_output_tokens"] or 0,
            "cache_read":    r["total_cache_read"] or 0,
            "cache_creation": r["total_cache_creation"] or 0,
        })

    conn.close()

    return {
        "all_models":      all_models,
        "daily_by_model":  daily_by_model,
        "hourly_by_model": hourly_by_model,
        "tool_by_model":   tool_by_model,
        "sessions_all":    sessions_all,
        "generated_at":    datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


def get_session_detail(session_id, db_path=DB_PATH):
    if not db_path.exists():
        return {"error": "Database not found. Run: python cli.py scan"}
    if not session_id:
        return {"error": "Missing session_id"}

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    session = conn.execute("""
        SELECT
            session_id, project_name, first_timestamp, last_timestamp,
            total_input_tokens, total_output_tokens,
            total_cache_read, total_cache_creation, model, turn_count,
            git_branch
        FROM sessions
        WHERE session_id = ?
    """, (session_id,)).fetchone()

    if session is None:
        conn.close()
        return {"error": "Session not found"}

    turn_cols = {
        row["name"] for row in conn.execute("PRAGMA table_info(turns)").fetchall()
    }

    def turn_expr(col, default):
        return col if col in turn_cols else f"{default} as {col}"

    turns = conn.execute(f"""
        SELECT
            timestamp, COALESCE(model, 'unknown') as model,
            input_tokens, output_tokens,
            cache_read_tokens, cache_creation_tokens,
            {turn_expr('cache_creation_5m_tokens', '0')},
            {turn_expr('cache_creation_1h_tokens', '0')},
            {turn_expr('duration_ms', '0')},
            {turn_expr('stop_reason', "''")},
            {turn_expr('service_tier', "''")},
            {turn_expr('inference_geo', "''")},
            {turn_expr('is_sidechain', '0')},
            {turn_expr('is_compact_summary', '0')},
            {turn_expr('tool_calls', 'NULL')},
            COALESCE(tool_name, '<none>') as tool_name,
            cwd
        FROM turns
        WHERE session_id = ?
        ORDER BY timestamp, id
    """, (session_id,)).fetchall()

    try:
        t1 = datetime.fromisoformat(session["first_timestamp"].replace("Z", "+00:00"))
        t2 = datetime.fromisoformat(session["last_timestamp"].replace("Z", "+00:00"))
        duration_min = round((t2 - t1).total_seconds() / 60, 1)
    except Exception:
        duration_min = 0

    result = {
        "session": {
            "session_id":       session["session_id"],
            "display_id":       session["session_id"][:8],
            "project":          session["project_name"] or "unknown",
            "branch":           session["git_branch"] or "",
            "first":            (session["first_timestamp"] or "")[:16].replace("T", " "),
            "last":             (session["last_timestamp"] or "")[:16].replace("T", " "),
            "duration_min":     duration_min,
            "model":            session["model"] or "unknown",
            "turns":            session["turn_count"] or 0,
            "input":            session["total_input_tokens"] or 0,
            "output":           session["total_output_tokens"] or 0,
            "cache_read":       session["total_cache_read"] or 0,
            "cache_creation":   session["total_cache_creation"] or 0,
        },
        "turns": [{
            "timestamp":      (r["timestamp"] or "")[:16].replace("T", " "),
            "model":          r["model"],
            "input":          r["input_tokens"] or 0,
            "output":         r["output_tokens"] or 0,
            "cache_read":     r["cache_read_tokens"] or 0,
            "cache_creation": r["cache_creation_tokens"] or 0,
            "cache_creation_5m": r["cache_creation_5m_tokens"] or 0,
            "cache_creation_1h": r["cache_creation_1h_tokens"] or 0,
            "duration_ms":    r["duration_ms"] or 0,
            "stop_reason":    r["stop_reason"] or "",
            "service_tier":   r["service_tier"] or "",
            "inference_geo":  r["inference_geo"] or "",
            "is_sidechain":   bool(r["is_sidechain"]),
            "is_compact_summary": bool(r["is_compact_summary"]),
            "tool":           r["tool_name"],
            "tool_calls":     json.loads(r["tool_calls"]) if r["tool_calls"] else [],
            "cwd":            r["cwd"] or "",
        } for r in turns],
    }
    conn.close()
    return result


HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Claude Code Usage Dashboard</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>
  :root {
    --bg: #161617;      /* page base */
    --card: #1E1F20;    /* raised one step above the page */
    --border: #2C2D2E;
    --text: #BFBFBF;
    --muted: #4F4F50;
    --accent: #d97757;
    --blue: #48A0C7;
    --green: #74C991;
    --red: #C74E39;
    --raised: #2E2F31;  /* hover / raised surfaces — top of the elevation ladder */
    --selected: #262626;  /* selected chips / tabs (neutral, not accent) */
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: var(--bg); color: var(--text); font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; font-size: 14px; }

  /* VS Code-style scrollbars. The dashboard renders inside a webview iframe,
     which doesn't inherit VS Code's --vscode-* theme variables, so we set the
     scrollbar here: no arrows, grey thumb (#28292B, #8B8B8D on hover) over a
     #121314 track, in a 21px gutter. Also fits the dark UI standalone. */
  * { scrollbar-width: auto; scrollbar-color: #28292B #121314; }
  ::-webkit-scrollbar { width: 21px; height: 21px; }
  ::-webkit-scrollbar-track { background: #121314; }
  ::-webkit-scrollbar-thumb { background-color: #28292B; border: 3px solid transparent; background-clip: padding-box; }
  ::-webkit-scrollbar-thumb:hover { background-color: #8B8B8D; }
  ::-webkit-scrollbar-thumb:active { background-color: #8B8B8D; }
  ::-webkit-scrollbar-corner { background: #121314; }

  header { background: var(--card); border-bottom: 1px solid var(--border); padding: 16px 24px; display: flex; align-items: center; justify-content: space-between; }
  header h1 { font-size: 18px; font-weight: 600; color: var(--text); }
  header .header-title { display: flex; align-items: center; gap: 10px; }
  /* The icon is a monochrome silhouette (white shape on transparent). We paint
     it with the title color via a CSS mask + background-color, so it matches
     `header h1` — the lightest text color. */
  header .header-icon {
    width: 26px; height: 26px; flex-shrink: 0; display: block;
    background-color: var(--text);
    -webkit-mask: url("icon.svg") no-repeat center / contain;
    mask: url("icon.svg") no-repeat center / contain;
  }
  header .meta { color: var(--muted); font-size: 12px; text-align: right; line-height: 1.5; margin-right: 20px; }
  #rescan-btn { background: var(--card); border: 1px solid var(--border); color: var(--muted); padding: 4px 12px; border-radius: 6px; cursor: pointer; font-size: 12px; margin-top: 4px; }
  #rescan-btn:hover { color: var(--text); border-color: var(--accent); }
  #rescan-btn:disabled { opacity: 0.5; cursor: not-allowed; }

  #filter-bar { background: var(--card); border-bottom: 1px solid var(--border); padding: 10px 24px; display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
  .filter-label { font-size: 11px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.05em; color: var(--muted); white-space: nowrap; }
  .filter-sep { width: 1px; height: 22px; background: var(--border); flex-shrink: 0; }
  #model-checkboxes { display: flex; flex-wrap: wrap; gap: 6px; }
  .model-cb-label { display: flex; align-items: center; gap: 5px; padding: 3px 10px; border-radius: 20px; border: 1px solid var(--border); cursor: pointer; font-size: 12px; color: var(--muted); transition: border-color 0.15s, color 0.15s, background 0.15s; user-select: none; }
  .model-cb-label:hover { border-color: var(--accent); color: var(--text); }
  .model-cb-label.checked { background: var(--selected); border-color: var(--accent); color: var(--text); }
  .model-cb-label input { display: none; }
  .filter-btn { padding: 3px 10px; border-radius: 4px; border: 1px solid var(--border); background: transparent; color: var(--muted); font-size: 11px; cursor: pointer; white-space: nowrap; }
  .filter-btn:hover { border-color: var(--accent); color: var(--text); }
  .range-group { display: flex; border: 1px solid var(--border); border-radius: 6px; overflow: hidden; flex-shrink: 0; }
  .range-btn { padding: 4px 13px; background: transparent; border: none; border-right: 1px solid var(--border); color: var(--muted); font-size: 12px; cursor: pointer; transition: background 0.15s, color 0.15s; }
  .range-btn:last-child { border-right: none; }
  .range-btn:hover { background: var(--raised); color: var(--text); }
  .range-btn.active { background: var(--selected); color: var(--text); font-weight: 600; }

  .container { max-width: 1400px; margin: 0 auto; padding: 24px; }
  .stats-row { display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 16px; margin-bottom: 24px; }
  .stat-card { background: var(--card); border: 1px solid var(--border); border-radius: 8px; padding: 16px; }
  .stat-card .label { color: var(--muted); font-size: 11px; text-transform: uppercase; letter-spacing: 0.05em; margin-bottom: 6px; }
  .stat-card .value { font-size: 22px; font-weight: 700; }
  .stat-card .sub { color: var(--muted); font-size: 11px; margin-top: 4px; }

  .charts-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; margin-bottom: 24px; }
  /* min-width:0 lets the grid column shrink below the canvas's intrinsic
     pixel width; without it, narrowing the window can't narrow the container,
     so Chart.js's ResizeObserver never fires until a data refresh rebuilds the
     canvas. (Expanding already works — 1fr columns grow freely.) */
  .chart-card { background: var(--card); border: 1px solid var(--border); border-radius: 8px; padding: 20px; min-width: 0; }
  .chart-card.wide { grid-column: 1 / -1; }
  .chart-card h2 { font-size: 13px; font-weight: 600; color: var(--muted); text-transform: uppercase; letter-spacing: 0.05em; margin-bottom: 16px; }
  .chart-wrap { position: relative; height: 240px; }
  .chart-wrap.tall { height: 300px; }
  .chart-header { display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 10px; margin-bottom: 16px; }
  .chart-header h2 { margin-bottom: 0; }
  .chart-header-right { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
  .chart-day-count { font-size: 11px; color: var(--muted); }
  .tz-group { display: flex; border: 1px solid var(--border); border-radius: 6px; overflow: hidden; }
  .tz-btn { padding: 3px 10px; background: transparent; border: none; border-right: 1px solid var(--border); color: var(--muted); font-size: 11px; cursor: pointer; transition: background 0.15s, color 0.15s; text-transform: uppercase; letter-spacing: 0.05em; font-weight: 600; }
  .tz-btn:last-child { border-right: none; }
  .tz-btn:hover { background: var(--raised); color: var(--text); }
  .tz-btn.active { background: var(--selected); color: var(--text); }
  .peak-legend { display: inline-flex; align-items: center; gap: 5px; font-size: 11px; color: var(--muted); }
  .peak-swatch { width: 10px; height: 10px; background: var(--red); border-radius: 2px; display: inline-block; }

  table { width: 100%; border-collapse: collapse; }
  th { text-align: left; padding: 8px 12px; font-size: 11px; text-transform: uppercase; letter-spacing: 0.05em; color: var(--muted); border-bottom: 1px solid var(--border); white-space: nowrap; }
  th.sortable { cursor: pointer; user-select: none; }
  th.sortable:hover { color: var(--text); }
  .sort-icon { font-size: 9px; opacity: 0.8; }
  td { padding: 10px 12px; border-bottom: 1px solid var(--border); font-size: 13px; }
  tr:last-child td { border-bottom: none; }
  tr:hover td { background: var(--raised); }
  .model-tag { display: inline-block; padding: 2px 7px; border-radius: 4px; font-size: 11px; background: rgba(72,160,199,0.15); color: var(--blue); }
  .cost { color: var(--green); font-family: monospace; }
  .cost-na { color: var(--muted); font-family: monospace; font-size: 11px; }
  .num { font-family: monospace; }
  .muted { color: var(--muted); }
  .section-title { font-size: 13px; font-weight: 600; color: var(--muted); text-transform: uppercase; letter-spacing: 0.05em; margin-bottom: 12px; }
  .section-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px; }
  .section-header .section-title { margin-bottom: 0; }
  .export-btn { background: var(--card); border: 1px solid var(--border); color: var(--muted); padding: 3px 10px; border-radius: 5px; cursor: pointer; font-size: 11px; }
  .export-btn:hover { color: var(--text); border-color: var(--accent); }
  .link-btn { background: transparent; border: none; color: var(--blue); cursor: pointer; font: inherit; padding: 0; }
  .link-btn:hover { text-decoration: underline; }
  .tool-detail-link { margin-left: 8px; font-size: 11px; color: var(--muted); }
  .tool-detail-link:hover { color: var(--blue); }
  .tool-overlay { position: fixed; inset: 0; background: rgba(0,0,0,0.45); z-index: 90; }
  .tool-overlay.hidden, .tool-panel.hidden { display: none; }
  .tool-panel { position: fixed; top: 0; right: 0; width: min(620px, 94vw); height: 100vh; background: var(--card);
                border-left: 1px solid var(--border); z-index: 100; display: flex; flex-direction: column;
                box-shadow: -10px 0 30px rgba(0,0,0,0.5); }
  .tool-panel-head { display: flex; align-items: center; justify-content: space-between; padding: 14px 18px;
                     border-bottom: 1px solid var(--border); flex: 0 0 auto; }
  .tool-panel-head .title { font-size: 13px; font-weight: 600; }
  .tool-panel-head .sub { font-size: 11px; color: var(--muted); }
  .tool-panel-body { padding: 16px 18px; overflow-y: auto; flex: 1 1 auto; }
  .tool-call-block { margin-bottom: 20px; }
  .tool-call-block h4 { font-size: 12px; color: var(--accent); text-transform: uppercase; letter-spacing: 0.05em; margin-bottom: 6px; }
  .tool-call-block .field { font-size: 11px; color: var(--muted); margin: 8px 0 3px; text-transform: uppercase; letter-spacing: 0.04em; }
  .tool-call-block pre { background: var(--bg); border: 1px solid var(--border); border-radius: 6px; padding: 10px;
                         font-family: monospace; font-size: 12px; line-height: 1.5; white-space: pre-wrap;
                         word-break: break-word; max-height: 60vh; overflow: auto; }
  .session-filter-bar { display: grid; grid-template-columns: minmax(160px, 1fr) minmax(120px, 0.8fr) minmax(120px, 0.8fr) 100px auto auto; gap: 10px; align-items: end; margin-bottom: 14px; }
  .filter-field label { display: block; color: var(--muted); font-size: 10px; text-transform: uppercase; letter-spacing: 0.05em; margin-bottom: 5px; }
  .filter-field input, .filter-field select { width: 100%; background: var(--bg); border: 1px solid var(--border); color: var(--text); border-radius: 5px; padding: 6px 8px; font: inherit; font-size: 12px; }
  .filter-field input:focus, .filter-field select:focus { outline: none; border-color: var(--accent); }
  .filter-check { display: flex; align-items: center; gap: 7px; color: var(--muted); font-size: 12px; padding-bottom: 7px; white-space: nowrap; }
  .filter-check input { accent-color: var(--accent); }
  .pricing-input { width: 86px; background: var(--bg); border: 1px solid var(--border); color: var(--text); border-radius: 5px; padding: 5px 7px; font: inherit; font-size: 12px; font-family: monospace; }
  .pricing-input:focus { outline: none; border-color: var(--accent); }
  .pricing-source { color: var(--muted); font-size: 11px; }
  .detail-stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(130px, 1fr)); gap: 10px; margin-bottom: 14px; }
  .detail-stat { border: 1px solid var(--border); border-radius: 6px; padding: 10px; }
  .detail-stat .label { color: var(--muted); font-size: 10px; text-transform: uppercase; letter-spacing: 0.05em; margin-bottom: 4px; }
  .detail-stat .value { font-size: 15px; font-weight: 700; }
  .detail-workspace-header { display: flex; justify-content: space-between; align-items: center; gap: 16px; margin-bottom: 18px; }
  .detail-title-group { min-width: 0; }
  .detail-title { color: var(--accent); font-size: 20px; font-weight: 700; margin-bottom: 4px; }
  .detail-subtitle { color: var(--muted); font-size: 12px; overflow-wrap: anywhere; }
  .detail-actions { display: flex; align-items: center; gap: 8px; flex-shrink: 0; }
  .timeline-wrap { position: relative; height: 300px; margin-bottom: 16px; border: 1px solid var(--border); border-radius: 6px; padding: 10px; }
  .hidden { display: none; }
  .table-card { background: var(--card); border: 1px solid var(--border); border-radius: 8px; padding: 20px; margin-bottom: 24px; overflow-x: auto; }
  .table-foot { display: flex; justify-content: flex-end; align-items: center; gap: 12px; margin-top: 12px; }
  .table-foot:empty { margin-top: 0; }
  .show-more-btn { background: transparent; border: 1px solid var(--border); color: var(--muted); padding: 4px 12px; border-radius: 6px; cursor: pointer; font-size: 12px; }
  .show-more-btn:hover { color: var(--text); border-color: var(--accent); }
  .show-more-link { color: var(--blue); text-decoration: none; font-size: 12px; cursor: pointer; }
  .show-more-link:hover { text-decoration: underline; }

  footer { border-top: 1px solid var(--border); padding: 20px 24px; margin-top: 8px; }
  .footer-content { max-width: 1400px; margin: 0 auto; }
  .footer-content p { color: var(--muted); font-size: 12px; line-height: 1.7; margin-bottom: 4px; }
  .footer-content p:last-child { margin-bottom: 0; }
  .footer-content a { color: var(--blue); text-decoration: none; }
  .footer-content a:hover { text-decoration: underline; }

  @media (max-width: 900px) { .session-filter-bar { grid-template-columns: 1fr 1fr; } }
  @media (max-width: 768px) { .charts-grid { grid-template-columns: 1fr; } .chart-card.wide { grid-column: 1; } .session-filter-bar { grid-template-columns: 1fr; } }
</style>
</head>
<body>
<header>
  <div class="header-title">
    <span class="header-icon" role="img" aria-label="Claude Usage"></span>
    <h1>Claude Code Usage</h1>
  </div>
  <div class="meta" id="meta">Loading...</div>
  <button id="rescan-btn" onclick="triggerRescan()" title="Rebuild the database from scratch by re-scanning all JSONL files. Use if data looks stale or costs seem wrong.">&#x21bb; Rescan</button>
</header>

<div id="filter-bar">
  <div class="filter-label">Models</div>
  <div id="model-checkboxes"></div>
  <button class="filter-btn" onclick="selectAllModels()">All</button>
  <button class="filter-btn" onclick="clearAllModels()">None</button>
  <div class="filter-sep"></div>
  <div class="filter-label">Range</div>
  <div class="range-group">
    <button class="range-btn" data-range="today" onclick="setRange('today')">Today</button>
    <button class="range-btn" data-range="week" onclick="setRange('week')">This Week</button>
    <button class="range-btn" data-range="month" onclick="setRange('month')">This Month</button>
    <button class="range-btn" data-range="prev-month" onclick="setRange('prev-month')">Prev Month</button>
    <button class="range-btn" data-range="7d"  onclick="setRange('7d')">7d</button>
    <button class="range-btn" data-range="30d" onclick="setRange('30d')">30d</button>
    <button class="range-btn" data-range="90d" onclick="setRange('90d')">90d</button>
    <button class="range-btn" data-range="all" onclick="setRange('all')">All</button>
  </div>
</div>

<main class="container" id="dashboard-view">
  <div class="stats-row" id="stats-row"></div>
  <div class="charts-grid">
    <div class="chart-card wide">
      <h2 id="daily-chart-title">Daily Token Usage</h2>
      <div class="chart-wrap tall"><canvas id="chart-daily"></canvas></div>
    </div>
    <div class="chart-card wide">
      <div class="chart-header">
        <h2 id="hourly-chart-title">Average Hourly Distribution</h2>
        <div class="chart-header-right">
          <span class="peak-legend" title="Mon–Fri 05:00–11:00 PT — Anthropic peak-hour throttling window"><span class="peak-swatch"></span>Peak hours (PT)</span>
          <span class="chart-day-count" id="hourly-day-count"></span>
          <div class="tz-group">
            <button class="tz-btn" data-tz="local" onclick="setHourlyTZ('local')">Local</button>
            <button class="tz-btn" data-tz="utc"   onclick="setHourlyTZ('utc')">UTC</button>
          </div>
        </div>
      </div>
      <div class="chart-wrap"><canvas id="chart-hourly"></canvas></div>
    </div>
    <div class="chart-card">
      <h2>By Model</h2>
      <div class="chart-wrap"><canvas id="chart-model"></canvas></div>
    </div>
    <div class="chart-card">
      <h2>Top Projects by Tokens</h2>
      <div class="chart-wrap"><canvas id="chart-project"></canvas></div>
    </div>
  </div>
  <div class="table-card">
    <div class="section-title">Cost by Model</div>
    <table>
      <thead><tr>
        <th>Model</th>
        <th class="sortable" onclick="setModelSort('turns')">Turns <span class="sort-icon" id="msort-turns"></span></th>
        <th class="sortable" onclick="setModelSort('input')">Input <span class="sort-icon" id="msort-input"></span></th>
        <th class="sortable" onclick="setModelSort('output')">Output <span class="sort-icon" id="msort-output"></span></th>
        <th class="sortable" onclick="setModelSort('cache_read')">Cache Read <span class="sort-icon" id="msort-cache_read"></span></th>
        <th class="sortable" onclick="setModelSort('cache_creation')">Cache Creation <span class="sort-icon" id="msort-cache_creation"></span></th>
        <th class="sortable" onclick="setModelSort('cost')">Est. Cost <span class="sort-icon" id="msort-cost"></span></th>
      </tr></thead>
      <tbody id="model-cost-body"></tbody>
    </table>
    <div class="table-foot" id="model-cost-foot"></div>
  </div>
  <div class="table-card">
    <div class="section-title">Cost Breakdown</div>
    <table>
      <thead><tr>
        <th>Input</th>
        <th>Output</th>
        <th>Cache Read</th>
        <th>Cache Creation</th>
        <th>Total</th>
        <th>Est. Cache Savings</th>
        <th>Cache Share</th>
      </tr></thead>
      <tbody id="cost-breakdown-body"></tbody>
    </table>
  </div>
  <div class="table-card">
    <div class="section-header">
      <div class="section-title">Token Pricing ($ / MTok)</div>
      <button class="export-btn" onclick="resetPricing()" title="Restore built-in dashboard pricing">Reset</button>
    </div>
    <table>
      <thead><tr>
        <th>Model</th>
        <th>Input</th>
        <th>Output</th>
        <th>Cache Read</th>
        <th>Cache Creation</th>
        <th>Source</th>
      </tr></thead>
      <tbody id="pricing-body"></tbody>
    </table>
  </div>
  <div class="table-card">
    <div class="section-header"><div class="section-title">Recent Sessions</div><button class="export-btn" onclick="exportSessionsCSV()" title="Export all filtered sessions to CSV">&#x2913; CSV</button></div>
    <div class="session-filter-bar">
      <div class="filter-field">
        <label for="session-project-filter">Project</label>
        <input id="session-project-filter" type="search" placeholder="Any project" oninput="onSessionFilterChange()">
      </div>
      <div class="filter-field">
        <label for="session-branch-filter">Branch</label>
        <input id="session-branch-filter" type="search" placeholder="Any branch" oninput="onSessionFilterChange()">
      </div>
      <div class="filter-field">
        <label for="session-tool-filter">Tool</label>
        <select id="session-tool-filter" onchange="onSessionFilterChange()"><option value="">All tools</option></select>
      </div>
      <div class="filter-field">
        <label for="session-min-cost-filter">Min Cost</label>
        <input id="session-min-cost-filter" type="number" min="0" step="0.01" placeholder="$0" oninput="onSessionFilterChange()">
      </div>
      <label class="filter-check"><input id="session-cache-filter" type="checkbox" onchange="onSessionFilterChange()">Cache creation</label>
      <button class="filter-btn" onclick="clearSessionFilters()">Clear</button>
    </div>
    <table>
      <thead><tr>
        <th>Session</th>
        <th>Project</th>
        <th class="sortable" onclick="setSessionSort('last')">Last Active <span class="sort-icon" id="sort-icon-last"></span></th>
        <th class="sortable" onclick="setSessionSort('duration_min')">Duration <span class="sort-icon" id="sort-icon-duration_min"></span></th>
        <th>Model</th>
        <th class="sortable" onclick="setSessionSort('turns')">Turns <span class="sort-icon" id="sort-icon-turns"></span></th>
        <th class="sortable" onclick="setSessionSort('input')">Input <span class="sort-icon" id="sort-icon-input"></span></th>
        <th class="sortable" onclick="setSessionSort('output')">Output <span class="sort-icon" id="sort-icon-output"></span></th>
        <th class="sortable" onclick="setSessionSort('cache_read')">Cache Read <span class="sort-icon" id="sort-icon-cache_read"></span></th>
        <th class="sortable" onclick="setSessionSort('cache_creation')">Cache Creation <span class="sort-icon" id="sort-icon-cache_creation"></span></th>
        <th class="sortable" onclick="setSessionSort('cost')">Est. Cost <span class="sort-icon" id="sort-icon-cost"></span></th>
      </tr></thead>
      <tbody id="sessions-body"></tbody>
    </table>
    <div class="table-foot" id="sessions-foot"></div>
  </div>
  <div class="table-card">
    <div class="section-title">Top Tools by Cost</div>
    <table>
      <thead><tr>
        <th>Tool</th>
        <th>Turns</th>
        <th>Input</th>
        <th>Output</th>
        <th>Cache Read</th>
        <th>Cache Creation</th>
        <th>Est. Cost</th>
      </tr></thead>
      <tbody id="tool-cost-body"></tbody>
    </table>
  </div>
  <div class="table-card">
    <div class="section-title">Expensive Session Signals</div>
    <table>
      <thead><tr>
        <th>Session</th>
        <th>Project</th>
        <th>Cost</th>
        <th>Cost / Min</th>
        <th>Tokens / Min</th>
        <th>Output / Turn</th>
        <th>Cache Share</th>
      </tr></thead>
      <tbody id="session-signals-body"></tbody>
    </table>
  </div>
  <div class="table-card">
    <div class="section-header"><div class="section-title">Cost by Project</div><button class="export-btn" onclick="exportProjectsCSV()" title="Export all projects to CSV">&#x2913; CSV</button></div>
    <table>
      <thead><tr>
        <th>Project</th>
        <th class="sortable" onclick="setProjectSort('sessions')">Sessions <span class="sort-icon" id="psort-sessions"></span></th>
        <th class="sortable" onclick="setProjectSort('turns')">Turns <span class="sort-icon" id="psort-turns"></span></th>
        <th class="sortable" onclick="setProjectSort('input')">Input <span class="sort-icon" id="psort-input"></span></th>
        <th class="sortable" onclick="setProjectSort('output')">Output <span class="sort-icon" id="psort-output"></span></th>
        <th class="sortable" onclick="setProjectSort('cost')">Est. Cost <span class="sort-icon" id="psort-cost"></span></th>
      </tr></thead>
      <tbody id="project-cost-body"></tbody>
    </table>
    <div class="table-foot" id="project-cost-foot"></div>
  </div>
  <div class="table-card">
    <div class="section-header"><div class="section-title">Cost by Project &amp; Branch</div><button class="export-btn" onclick="exportProjectBranchCSV()" title="Export project+branch breakdown to CSV">&#x2913; CSV</button></div>
    <table>
      <thead><tr>
        <th>Project</th>
        <th>Branch</th>
        <th class="sortable" onclick="setProjectBranchSort('sessions')">Sessions <span class="sort-icon" id="pbsort-sessions"></span></th>
        <th class="sortable" onclick="setProjectBranchSort('turns')">Turns <span class="sort-icon" id="pbsort-turns"></span></th>
        <th class="sortable" onclick="setProjectBranchSort('input')">Input <span class="sort-icon" id="pbsort-input"></span></th>
        <th class="sortable" onclick="setProjectBranchSort('output')">Output <span class="sort-icon" id="pbsort-output"></span></th>
        <th class="sortable" onclick="setProjectBranchSort('cost')">Est. Cost <span class="sort-icon" id="pbsort-cost"></span></th>
      </tr></thead>
      <tbody id="project-branch-cost-body"></tbody>
    </table>
    <div class="table-foot" id="project-branch-cost-foot"></div>
  </div>
</main>

<main class="container hidden" id="session-detail-view">
  <div class="detail-workspace-header">
    <div class="detail-title-group">
      <div class="detail-title" id="session-detail-title">Session Drilldown</div>
      <div class="detail-subtitle" id="session-detail-subtitle"></div>
    </div>
    <div class="detail-actions">
      <button class="export-btn" onclick="closeSessionDetail()" title="Return to dashboard">&larr; Back</button>
      <button class="export-btn" onclick="exportSessionTurnsCSV()" title="Export selected session turns to CSV">&#x2913; CSV</button>
    </div>
  </div>
  <div class="detail-stats" id="session-detail-stats"></div>
  <div class="table-card">
    <div class="section-title">Tool Breakdown</div>
    <table>
      <thead><tr>
        <th>Tool</th>
        <th>Turns</th>
        <th>Input</th>
        <th>Output</th>
        <th>Cache Read</th>
        <th>Cache Creation</th>
        <th>Est. Cost</th>
        <th>Cost Share</th>
      </tr></thead>
      <tbody id="session-tool-breakdown-body"></tbody>
    </table>
  </div>
  <div class="table-card">
    <div class="section-title">Session Timeline</div>
    <div class="timeline-wrap"><canvas id="chart-session-timeline"></canvas></div>
  </div>
  <div class="table-card">
    <div class="section-title">Turns</div>
    <table>
      <thead><tr>
        <th>Time</th>
        <th>Tool</th>
        <th>Model</th>
        <th>Input</th>
        <th>Output</th>
        <th>Cache Read</th>
        <th>Cache Creation</th>
        <th>Cache 5m</th>
        <th>Cache 1h</th>
        <th>Duration</th>
        <th>Stop</th>
        <th>Tier</th>
        <th>Est. Cost</th>
      </tr></thead>
      <tbody id="session-detail-body"></tbody>
    </table>
  </div>
</main>

<div id="tool-detail-overlay" class="tool-overlay hidden" onclick="closeToolPanel()"></div>
<aside id="tool-detail-panel" class="tool-panel hidden">
  <div class="tool-panel-head">
    <div>
      <div class="title" id="tool-panel-title">Tool calls</div>
      <div class="sub" id="tool-panel-sub"></div>
    </div>
    <button class="export-btn" onclick="closeToolPanel()" title="Close">&#x2715; Close</button>
  </div>
  <div class="tool-panel-body" id="tool-panel-body"></div>
</aside>

<footer>
  <div class="footer-content">
    <p>Cost estimates use editable dashboard pricing defaults based on Anthropic API pricing (<a href="https://claude.com/pricing#api" target="_blank">claude.com/pricing#api</a>) as of May 2026. Custom price edits are saved in this browser only. Only models containing <em>opus</em>, <em>sonnet</em>, or <em>haiku</em> in the name are included in cost calculations. Actual costs for Max/Pro subscribers differ from API pricing.</p>
    <p>
      GitHub: <a href="https://github.com/phuryn/claude-usage" target="_blank">https://github.com/phuryn/claude-usage</a>
      &nbsp;&middot;&nbsp;
      Created by: <a href="https://www.productcompass.pm" target="_blank">The Product Compass Newsletter</a>
      &nbsp;&middot;&nbsp;
      License: MIT
    </p>
  </div>
</footer>

<script>
// ── Helpers ────────────────────────────────────────────────────────────────
function esc(s) {
  const d = document.createElement('div');
  d.textContent = String(s);
  return d.innerHTML;
}

// ── State ──────────────────────────────────────────────────────────────────
let rawData = null;
let selectedModels = new Set();
let selectedRange = '30d';
let charts = {};
let sessionSortCol = 'last';
let modelSortCol = 'cost';
let modelSortDir = 'desc';
let projectSortCol = 'cost';
let projectSortDir = 'desc';
let branchSortCol = 'cost';
let branchSortDir = 'desc';
let lastFilteredSessions = [];
let lastByModel = [];
let lastByProject = [];
let lastByProjectBranch = [];
let lastByTool = [];
let selectedSessionDetail = null;
let sessionFilters = { project: '', branch: '', tool: '', minCost: 0, hasCacheCreation: false };
let sessionSortDir = 'desc';

// Tables reveal rows in steps: 10 -> 25 -> 50, capped at 50 because rendering
// more than that visibly hurts performance. Past 50 the footer offers a
// "Download CSV to see more" link instead of another in-table step, plus a
// Show less button that resets straight back to 10. Limits persist across
// re-renders so sorting/filtering keeps the user's chosen depth (visible rows
// always reflect the active sort).
const TABLE_STEPS = [10, 25, 50];
const TABLE_MAX = TABLE_STEPS[TABLE_STEPS.length - 1];  // hard cap on in-table rows
function nextTableLimit(current, total) {
  for (const s of TABLE_STEPS) {
    if (s > current && s < total) return s;
  }
  return Math.min(total, TABLE_MAX);  // reveal everything, but never past the cap
}
let modelLimit = TABLE_STEPS[0];
let sessionsLimit = TABLE_STEPS[0];
let projectLimit = TABLE_STEPS[0];
let branchLimit = TABLE_STEPS[0];
let hourlyTZ = 'local';  // 'local' or 'utc'

// ── Peak-hour config ───────────────────────────────────────────────────────
// Anthropic throttles Mon–Fri 05:00–11:00 PT. We approximate as fixed UTC hours
// 12–17 (matches PDT; during PST the window shifts by 1h — accepted simplification).
const PEAK_HOURS_UTC = new Set([12, 13, 14, 15, 16, 17]);

// Local-timezone offset in hours (signed). Fractional offsets (e.g. India UTC+5:30)
// are rounded to the nearest hour for bucket alignment.
function localOffsetHours() {
  return Math.round(-new Date().getTimezoneOffset() / 60);
}

// Return the UTC hour (0–23) corresponding to a displayed-hour bucket.
function displayHourToUTC(displayHour, tzMode) {
  if (tzMode === 'utc') return displayHour;
  return ((displayHour - localOffsetHours()) % 24 + 24) % 24;
}

// Return the displayed-hour bucket for a UTC hour.
function utcHourToDisplay(utcHour, tzMode) {
  if (tzMode === 'utc') return utcHour;
  return ((utcHour + localOffsetHours()) % 24 + 24) % 24;
}

function isPeakHour(displayHour, tzMode) {
  return PEAK_HOURS_UTC.has(displayHourToUTC(displayHour, tzMode));
}

function formatHourLabel(h) {
  return String(h).padStart(2, '0') + ':00';
}

function tzDisplayName(tzMode) {
  if (tzMode === 'utc') return 'UTC';
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || 'Local';
  } catch(e) {
    return 'Local';
  }
}

// ── Pricing (Anthropic API, May 2026 defaults; editable locally) ───────────
const PRICING_STORAGE_KEY = 'claudeUsagePricingOverrides';
const DEFAULT_PRICING = {
  'claude-opus-4-8':   { input:  5.00, output: 25.00, cache_write:  6.25, cache_read: 0.50 },
  'claude-opus-4-7':   { input:  5.00, output: 25.00, cache_write:  6.25, cache_read: 0.50 },
  'claude-opus-4-6':   { input:  5.00, output: 25.00, cache_write:  6.25, cache_read: 0.50 },
  'claude-opus-4-5':   { input:  5.00, output: 25.00, cache_write:  6.25, cache_read: 0.50 },
  'claude-sonnet-4-7': { input:  3.00, output: 15.00, cache_write:  3.75, cache_read: 0.30 },
  'claude-sonnet-4-6': { input:  3.00, output: 15.00, cache_write:  3.75, cache_read: 0.30 },
  'claude-sonnet-4-5': { input:  3.00, output: 15.00, cache_write:  3.75, cache_read: 0.30 },
  'claude-haiku-4-7':  { input:  1.00, output:  5.00, cache_write:  1.25, cache_read: 0.10 },
  'claude-haiku-4-6':  { input:  1.00, output:  5.00, cache_write:  1.25, cache_read: 0.10 },
  'claude-haiku-4-5':  { input:  1.00, output:  5.00, cache_write:  1.25, cache_read: 0.10 },
};
let PRICING = JSON.parse(JSON.stringify(DEFAULT_PRICING));
let pricingOverrides = {};

function loadPricing() {
  try {
    pricingOverrides = JSON.parse(localStorage.getItem(PRICING_STORAGE_KEY) || '{}') || {};
  } catch(e) {
    pricingOverrides = {};
  }
  PRICING = JSON.parse(JSON.stringify(DEFAULT_PRICING));
  for (const [model, override] of Object.entries(pricingOverrides)) {
    if (!PRICING[model]) continue;
    PRICING[model] = { ...PRICING[model], ...override };
  }
}

function savePricing() {
  localStorage.setItem(PRICING_STORAGE_KEY, JSON.stringify(pricingOverrides));
}

function renderPricingEditor() {
  const body = document.getElementById('pricing-body');
  if (!body) return;
  body.innerHTML = Object.keys(DEFAULT_PRICING).map(model => {
    const p = PRICING[model];
    const overridden = !!pricingOverrides[model];
    const input = (field) => `<input class="pricing-input" type="number" min="0" step="0.01" value="${p[field].toFixed(2)}" data-model="${esc(model)}" data-field="${field}" onchange="onPricingChange(this)">`;
    return `<tr>
      <td><span class="model-tag">${esc(model)}</span></td>
      <td>${input('input')}</td>
      <td>${input('output')}</td>
      <td>${input('cache_read')}</td>
      <td>${input('cache_write')}</td>
      <td class="pricing-source">${overridden ? 'Custom' : 'Default'}</td>
    </tr>`;
  }).join('');
}

function onPricingChange(input) {
  const model = input.dataset.model;
  const field = input.dataset.field;
  const value = parseFloat(input.value);
  if (!DEFAULT_PRICING[model] || !field || !isFinite(value) || value < 0) {
    input.value = PRICING[model]?.[field]?.toFixed(2) || '0.00';
    return;
  }
  if (!pricingOverrides[model]) pricingOverrides[model] = {};
  pricingOverrides[model][field] = value;
  PRICING[model][field] = value;

  const defaults = DEFAULT_PRICING[model];
  const overrides = pricingOverrides[model];
  if (Object.keys(overrides).every(k => overrides[k] === defaults[k])) {
    delete pricingOverrides[model];
  }

  savePricing();
  renderPricingEditor();
  applyFilter();
  if (selectedSessionDetail && !document.getElementById('session-detail-view').classList.contains('hidden')) {
    renderSessionDetail(selectedSessionDetail);
  }
}

function resetPricing() {
  pricingOverrides = {};
  localStorage.removeItem(PRICING_STORAGE_KEY);
  loadPricing();
  renderPricingEditor();
  applyFilter();
  if (selectedSessionDetail && !document.getElementById('session-detail-view').classList.contains('hidden')) {
    renderSessionDetail(selectedSessionDetail);
  }
}

function isBillable(model) {
  if (!model) return false;
  const m = model.toLowerCase();
  return m.includes('opus') || m.includes('sonnet') || m.includes('haiku');
}

function getPricing(model) {
  if (!model) return null;
  if (PRICING[model]) return PRICING[model];
  for (const key of Object.keys(PRICING)) {
    if (model.startsWith(key)) return PRICING[key];
  }
  const m = model.toLowerCase();
  if (m.includes('opus'))   return PRICING['claude-opus-4-8'];
  if (m.includes('sonnet')) return PRICING['claude-sonnet-4-6'];
  if (m.includes('haiku'))  return PRICING['claude-haiku-4-5'];
  return null;
}

function calcCost(model, inp, out, cacheRead, cacheCreation) {
  return calcCostBreakdown(model, inp, out, cacheRead, cacheCreation).total;
}

function calcCostBreakdown(model, inp, out, cacheRead, cacheCreation) {
  const zero = { input: 0, output: 0, cache_read: 0, cache_creation: 0, total: 0, cache_savings: 0 };
  if (!isBillable(model)) return zero;
  const p = getPricing(model);
  if (!p) return zero;
  const input = inp * p.input / 1e6;
  const output = out * p.output / 1e6;
  const cache_read = cacheRead * p.cache_read / 1e6;
  const cache_creation = cacheCreation * p.cache_write / 1e6;
  const cache_savings = cacheRead * Math.max(p.input - p.cache_read, 0) / 1e6;
  return { input, output, cache_read, cache_creation, total: input + output + cache_read + cache_creation, cache_savings };
}

// ── Formatting ─────────────────────────────────────────────────────────────
function fmt(n) {
  if (n >= 1e9) return (n/1e9).toFixed(2)+'B';
  if (n >= 1e6) return (n/1e6).toFixed(2)+'M';
  if (n >= 1e3) return (n/1e3).toFixed(1)+'K';
  return n.toLocaleString();
}
function fmtCost(c)    { return '$' + c.toLocaleString(undefined, { minimumFractionDigits: 4, maximumFractionDigits: 4 }); }
function fmtCostBig(c) { return '$' + c.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 }); }
function fmtPct(v)     { return isFinite(v) ? (v * 100).toFixed(1) + '%' : '0.0%'; }

// ── Chart colors ───────────────────────────────────────────────────────────
// Warm/neutral palette kept in sync with the CSS :root variables so charts match
// the Claude Code interface (less blue). Chart legends/axes use C.axis (a touch
// lighter than --muted so small labels stay legible on the dark card); grid uses
// C.border.
const C = {
  text:   '#BFBFBF',
  muted:  '#4F4F50',
  axis:   '#6F6F70',
  border: '#2C2D2E',
  card:   '#1E1F20',
  blue:   '#48A0C7',
  green:  '#74C991',
  red:    '#C74E39',
  accent: '#d97757',
  amber:  '#D9A84E',
  purple: '#9B7EC7',
  teal:   '#5BB8A3',
  mauve:  '#C77E9B',
};
const TOKEN_COLORS = {
  input:          'rgba(72,160,199,0.85)',   // blue
  output:         'rgba(217,119,87,0.85)',    // accent / coral
  cache_read:     'rgba(116,201,145,0.75)',   // green
  cache_creation: 'rgba(217,168,78,0.75)',    // amber
};
// Hover lifts on a dark theme: bars/series go to full opacity (a touch brighter).
const TOKEN_HOVER = {
  input:          'rgba(72,160,199,1)',
  output:         'rgba(217,119,87,1)',
  cache_read:     'rgba(116,201,145,1)',
  cache_creation: 'rgba(217,168,78,1)',
};
// Donut / categorical palette — warm, Anthropic-leaning (clay, tan, sage, dusty
// blue, mauve, ochre, taupe, terracotta) rather than a saturated rainbow.
const MODEL_COLORS = ['#D97757','#C9A26B','#7FA98C','#6E97A8','#B98AA0','#D9A84E','#A88B6A','#C2705A'];

// Tooltip color swatches: solid fill, no border (Chart.js's default draws a
// bordered box that looked offset/inconsistent). Lines use their solid stroke
// color instead of the translucent area fill.
Chart.defaults.color = C.axis;
// multiKeyBackground defaults to white and is drawn behind each tooltip swatch,
// peeking out as a thin white border on plain-box charts — make it transparent.
Chart.defaults.plugins.tooltip.multiKeyBackground = 'transparent';
Chart.defaults.plugins.tooltip.callbacks.labelColor = (ctx) => {
  const ds = ctx.dataset || {};
  let col = Array.isArray(ds.backgroundColor) ? ds.backgroundColor[ctx.dataIndex] : ds.backgroundColor;
  if (ds.type === 'line') col = ds.borderColor;
  return { borderColor: col, backgroundColor: col, borderWidth: 0 };
};

// Legend visibility must survive repaints (filter changes, auto-refresh, sort) —
// the charts are destroyed and rebuilt each render, which otherwise resets any
// series the user toggled off. We track hidden series by label per chart and
// reapply on rebuild: dataset charts via `dataset.hidden`, the doughnut via
// per-slice data visibility (see applyModelHidden).
const hiddenSeries = { daily: new Set(), hourly: new Set(), project: new Set(), model: new Set() };
function legendToggle(key) {
  return (e, item, legend) => {
    const ci = legend.chart;
    const ds = ci.data.datasets[item.datasetIndex];
    ds.hidden = !ds.hidden;
    if (ds.hidden) hiddenSeries[key].add(ds.label); else hiddenSeries[key].delete(ds.label);
    ci.update();
  };
}

// ── Time range ─────────────────────────────────────────────────────────────
const RANGE_LABELS = { 'today': 'Today', 'week': 'This Week', 'month': 'This Month', 'prev-month': 'Previous Month', '7d': 'Last 7 Days', '30d': 'Last 30 Days', '90d': 'Last 90 Days', 'all': 'All Time' };
const RANGE_TICKS  = { 'today': 1, 'week': 7, 'month': 15, 'prev-month': 15, '7d': 7, '30d': 15, '90d': 13, 'all': 12 };
const VALID_RANGES = Object.keys(RANGE_LABELS);

function rangeIncludesToday(range) {
  if (range === 'all') return true;
  const { start, end } = getRangeBounds(range);
  const today = new Date().toISOString().slice(0, 10);
  if (start && today < start) return false;
  if (end && today > end) return false;
  return true;
}

function getRangeBounds(range) {
  if (range === 'all') return { start: null, end: null };
  const today = new Date();
  const iso = d => d.toISOString().slice(0, 10);
  if (range === 'today') {
    const t = iso(today);
    return { start: t, end: t };
  }
  if (range === 'week') {
    const day = today.getDay();
    const diffToMon = day === 0 ? 6 : day - 1;
    const mon = new Date(today); mon.setDate(today.getDate() - diffToMon);
    const sun = new Date(mon); sun.setDate(mon.getDate() + 6);
    return { start: iso(mon), end: iso(sun) };
  }
  if (range === 'month') {
    const start = new Date(today.getFullYear(), today.getMonth(), 1);
    const end = new Date(today.getFullYear(), today.getMonth() + 1, 0);
    return { start: iso(start), end: iso(end) };
  }
  if (range === 'prev-month') {
    const start = new Date(today.getFullYear(), today.getMonth() - 1, 1);
    const end = new Date(today.getFullYear(), today.getMonth(), 0);
    return { start: iso(start), end: iso(end) };
  }
  const days = range === '7d' ? 7 : range === '30d' ? 30 : 90;
  const d = new Date();
  d.setDate(d.getDate() - days);
  return { start: iso(d), end: null };
}

function readURLRange() {
  const p = new URLSearchParams(window.location.search).get('range');
  return VALID_RANGES.includes(p) ? p : '30d';
}

function setRange(range) {
  selectedRange = range;
  document.querySelectorAll('.range-btn').forEach(btn =>
    btn.classList.toggle('active', btn.dataset.range === range)
  );
  updateURL();
  applyFilter();
  scheduleAutoRefresh();
}

function setHourlyTZ(mode) {
  hourlyTZ = mode;
  document.querySelectorAll('.tz-btn').forEach(btn =>
    btn.classList.toggle('active', btn.dataset.tz === mode)
  );
  applyFilter();
}

// ── Model filter ───────────────────────────────────────────────────────────
function modelPriority(m) {
  const ml = m.toLowerCase();
  if (ml.includes('opus'))   return 0;
  if (ml.includes('sonnet')) return 1;
  if (ml.includes('haiku'))  return 2;
  return 3;
}

function readURLModels(allModels) {
  const param = new URLSearchParams(window.location.search).get('models');
  if (!param) {
    const billable = allModels.filter(m => isBillable(m));
    // Fallback: if the user only has non-billable / unknown models (e.g. all
    // local-LLM runs), default to all models so the dashboard isn't blank.
    return new Set(billable.length ? billable : allModels);
  }
  const fromURL = new Set(param.split(',').map(s => s.trim()).filter(Boolean));
  return new Set(allModels.filter(m => fromURL.has(m)));
}

function isDefaultModelSelection(allModels) {
  const billable = allModels.filter(m => isBillable(m));
  const expected = billable.length ? billable : allModels;
  if (selectedModels.size !== expected.length) return false;
  return expected.every(m => selectedModels.has(m));
}

function buildFilterUI(allModels) {
  const sorted = [...allModels].sort((a, b) => {
    const pa = modelPriority(a), pb = modelPriority(b);
    return pa !== pb ? pa - pb : a.localeCompare(b);
  });
  selectedModels = readURLModels(allModels);
  const container = document.getElementById('model-checkboxes');
  container.innerHTML = sorted.map(m => {
    const checked = selectedModels.has(m);
    return `<label class="model-cb-label ${checked ? 'checked' : ''}" data-model="${esc(m)}">
      <input type="checkbox" value="${esc(m)}" ${checked ? 'checked' : ''} onchange="onModelToggle(this)">
      ${esc(m)}
    </label>`;
  }).join('');
}

function onModelToggle(cb) {
  const label = cb.closest('label');
  if (cb.checked) { selectedModels.add(cb.value);    label.classList.add('checked'); }
  else            { selectedModels.delete(cb.value); label.classList.remove('checked'); }
  updateURL();
  applyFilter();
}

function selectAllModels() {
  document.querySelectorAll('#model-checkboxes input').forEach(cb => {
    cb.checked = true; selectedModels.add(cb.value); cb.closest('label').classList.add('checked');
  });
  updateURL(); applyFilter();
}

function clearAllModels() {
  document.querySelectorAll('#model-checkboxes input').forEach(cb => {
    cb.checked = false; selectedModels.delete(cb.value); cb.closest('label').classList.remove('checked');
  });
  updateURL(); applyFilter();
}

// ── Session filters ───────────────────────────────────────────────────────
function buildSessionFilterUI(sessions) {
  const tools = new Set();
  for (const s of sessions) {
    for (const tool of (s.tools || [])) {
      if (tool) tools.add(tool);
    }
  }
  const toolSelect = document.getElementById('session-tool-filter');
  const current = toolSelect.value;
  toolSelect.innerHTML = '<option value="">All tools</option>' + [...tools].sort((a, b) => a.localeCompare(b)).map(t =>
    `<option value="${esc(t)}">${esc(t)}</option>`
  ).join('');
  if ([...toolSelect.options].some(o => o.value === current)) toolSelect.value = current;
}

function readSessionFilters() {
  sessionFilters = {
    project: document.getElementById('session-project-filter').value.trim().toLowerCase(),
    branch: document.getElementById('session-branch-filter').value.trim().toLowerCase(),
    tool: document.getElementById('session-tool-filter').value,
    minCost: parseFloat(document.getElementById('session-min-cost-filter').value) || 0,
    hasCacheCreation: document.getElementById('session-cache-filter').checked,
  };
}

function onSessionFilterChange() {
  readSessionFilters();
  applyFilter();
}

function clearSessionFilters() {
  document.getElementById('session-project-filter').value = '';
  document.getElementById('session-branch-filter').value = '';
  document.getElementById('session-tool-filter').value = '';
  document.getElementById('session-min-cost-filter').value = '';
  document.getElementById('session-cache-filter').checked = false;
  readSessionFilters();
  applyFilter();
}

function applySessionFilters(sessions) {
  return sessions.filter(s => {
    if (sessionFilters.project && !(s.project || '').toLowerCase().includes(sessionFilters.project)) return false;
    if (sessionFilters.branch && !(s.branch || '').toLowerCase().includes(sessionFilters.branch)) return false;
    if (sessionFilters.tool && !(s.tools || []).includes(sessionFilters.tool)) return false;
    if (sessionFilters.hasCacheCreation && !(s.cache_creation > 0)) return false;
    if (sessionFilters.minCost && calcCost(s.model, s.input, s.output, s.cache_read, s.cache_creation) < sessionFilters.minCost) return false;
    return true;
  });
}

// ── URL persistence ────────────────────────────────────────────────────────
function updateURL() {
  const allModels = Array.from(document.querySelectorAll('#model-checkboxes input')).map(cb => cb.value);
  const params = new URLSearchParams();
  if (selectedRange !== '30d') params.set('range', selectedRange);
  if (!isDefaultModelSelection(allModels)) params.set('models', Array.from(selectedModels).join(','));
  const search = params.toString() ? '?' + params.toString() : '';
  history.replaceState(null, '', window.location.pathname + search);
}

function updateURLForSession(sessionId) {
  const params = new URLSearchParams(window.location.search);
  params.set('session', sessionId);
  history.replaceState(null, '', window.location.pathname + '?' + params.toString());
}

function readURLSession() {
  return new URLSearchParams(window.location.search).get('session') || '';
}

// ── Session sort ───────────────────────────────────────────────────────────
function setSessionSort(col) {
  if (sessionSortCol === col) {
    sessionSortDir = sessionSortDir === 'desc' ? 'asc' : 'desc';
  } else {
    sessionSortCol = col;
    sessionSortDir = 'desc';
  }
  updateSortIcons();
  applyFilter();
}

function updateSortIcons() {
  document.querySelectorAll('.sort-icon').forEach(el => el.textContent = '');
  const icon = document.getElementById('sort-icon-' + sessionSortCol);
  if (icon) icon.textContent = sessionSortDir === 'desc' ? ' \u25bc' : ' \u25b2';
}

function sortSessions(sessions) {
  return [...sessions].sort((a, b) => {
    let av, bv;
    if (sessionSortCol === 'cost') {
      av = calcCost(a.model, a.input, a.output, a.cache_read, a.cache_creation);
      bv = calcCost(b.model, b.input, b.output, b.cache_read, b.cache_creation);
    } else if (sessionSortCol === 'duration_min') {
      av = parseFloat(a.duration_min) || 0;
      bv = parseFloat(b.duration_min) || 0;
    } else {
      av = a[sessionSortCol] ?? 0;
      bv = b[sessionSortCol] ?? 0;
    }
    if (av < bv) return sessionSortDir === 'desc' ? 1 : -1;
    if (av > bv) return sessionSortDir === 'desc' ? -1 : 1;
    return 0;
  });
}

function aggregateCostBreakdown(rows) {
  return rows.reduce((acc, r) => {
    const c = calcCostBreakdown(r.model, r.input, r.output, r.cache_read, r.cache_creation);
    acc.input += c.input;
    acc.output += c.output;
    acc.cache_read += c.cache_read;
    acc.cache_creation += c.cache_creation;
    acc.total += c.total;
    acc.cache_savings += c.cache_savings;
    acc.tokens += (r.input || 0) + (r.output || 0) + (r.cache_read || 0) + (r.cache_creation || 0);
    acc.cache_tokens += (r.cache_read || 0) + (r.cache_creation || 0);
    return acc;
  }, { input: 0, output: 0, cache_read: 0, cache_creation: 0, total: 0, cache_savings: 0, tokens: 0, cache_tokens: 0 });
}

function sessionSignals(sessions) {
  return sessions.map(s => {
    const cost = calcCost(s.model, s.input, s.output, s.cache_read, s.cache_creation);
    const minutes = Math.max(parseFloat(s.duration_min) || 0, 1 / 60);
    const tokens = (s.input || 0) + (s.output || 0) + (s.cache_read || 0) + (s.cache_creation || 0);
    const promptTokens = (s.input || 0) + (s.cache_read || 0) + (s.cache_creation || 0);
    return {
      ...s,
      cost,
      cost_per_min: cost / minutes,
      tokens_per_min: tokens / minutes,
      output_per_turn: s.turns ? (s.output || 0) / s.turns : 0,
      cache_share: promptTokens ? ((s.cache_read || 0) + (s.cache_creation || 0)) / promptTokens : 0,
    };
  }).sort((a, b) => b.cost - a.cost);
}

// ── Aggregation & filtering ────────────────────────────────────────────────
function applyFilter() {
  if (!rawData) return;

  const { start, end } = getRangeBounds(selectedRange);

  // Filter daily rows by model + date range
  const filteredDaily = rawData.daily_by_model.filter(r =>
    selectedModels.has(r.model) && (!start || r.day >= start) && (!end || r.day <= end)
  );

  // Daily chart: aggregate by day
  const dailyMap = {};
  for (const r of filteredDaily) {
    if (!dailyMap[r.day]) dailyMap[r.day] = { day: r.day, input: 0, output: 0, cache_read: 0, cache_creation: 0 };
    const d = dailyMap[r.day];
    d.input          += r.input;
    d.output         += r.output;
    d.cache_read     += r.cache_read;
    d.cache_creation += r.cache_creation;
  }
  const daily = Object.values(dailyMap).sort((a, b) => a.day.localeCompare(b.day));

  // By model: aggregate tokens + turns from daily data
  const modelMap = {};
  for (const r of filteredDaily) {
    if (!modelMap[r.model]) modelMap[r.model] = { model: r.model, input: 0, output: 0, cache_read: 0, cache_creation: 0, turns: 0, sessions: 0 };
    const m = modelMap[r.model];
    m.input          += r.input;
    m.output         += r.output;
    m.cache_read     += r.cache_read;
    m.cache_creation += r.cache_creation;
    m.turns          += r.turns;
  }

  // Filter sessions by model + date range
  const rangeFilteredSessions = rawData.sessions_all.filter(s =>
    selectedModels.has(s.model) && (!start || s.last_date >= start) && (!end || s.last_date <= end)
  );
  const filteredSessions = applySessionFilters(rangeFilteredSessions);

  // Add session counts into modelMap
  for (const s of filteredSessions) {
    if (modelMap[s.model]) modelMap[s.model].sessions++;
  }

  const byModel = Object.values(modelMap).sort((a, b) => (b.input + b.output) - (a.input + a.output));

  // By project: aggregate from filtered sessions
  const projMap = {};
  for (const s of filteredSessions) {
    if (!projMap[s.project]) projMap[s.project] = { project: s.project, input: 0, output: 0, cache_read: 0, cache_creation: 0, turns: 0, sessions: 0, cost: 0 };
    const p = projMap[s.project];
    p.input          += s.input;
    p.output         += s.output;
    p.cache_read     += s.cache_read;
    p.cache_creation += s.cache_creation;
    p.turns          += s.turns;
    p.sessions++;
    p.cost += calcCost(s.model, s.input, s.output, s.cache_read, s.cache_creation);
  }
  const byProject = Object.values(projMap).sort((a, b) => (b.input + b.output) - (a.input + a.output));

  // By project+branch: aggregate from filtered sessions
  const projBranchMap = {};
  for (const s of filteredSessions) {
    const key = s.project + '\x00' + (s.branch || '');
    if (!projBranchMap[key]) projBranchMap[key] = { project: s.project, branch: s.branch || '', input: 0, output: 0, cache_read: 0, cache_creation: 0, turns: 0, sessions: 0, cost: 0 };
    const pb = projBranchMap[key];
    pb.input          += s.input;
    pb.output         += s.output;
    pb.cache_read     += s.cache_read;
    pb.cache_creation += s.cache_creation;
    pb.turns          += s.turns;
    pb.sessions++;
    pb.cost += calcCost(s.model, s.input, s.output, s.cache_read, s.cache_creation);
  }
  const byProjectBranch = Object.values(projBranchMap).sort((a, b) => b.cost - a.cost);

  // Totals
  const totals = {
    sessions:       filteredSessions.length,
    turns:          byModel.reduce((s, m) => s + m.turns, 0),
    input:          byModel.reduce((s, m) => s + m.input, 0),
    output:         byModel.reduce((s, m) => s + m.output, 0),
    cache_read:     byModel.reduce((s, m) => s + m.cache_read, 0),
    cache_creation: byModel.reduce((s, m) => s + m.cache_creation, 0),
    cost:           byModel.reduce((s, m) => s + calcCost(m.model, m.input, m.output, m.cache_read, m.cache_creation), 0),
  };
  const costTotals = aggregateCostBreakdown(filteredSessions);

  // By tool: aggregate from server-side per-day tool data
  const toolMap = {};
  const filteredTools = (rawData.tool_by_model || []).filter(r =>
    selectedModels.has(r.model) && (!start || r.day >= start) && (!end || r.day <= end)
  );
  for (const r of filteredTools) {
    if (!toolMap[r.tool]) toolMap[r.tool] = { tool: r.tool, input: 0, output: 0, cache_read: 0, cache_creation: 0, turns: 0, cost: 0 };
    const t = toolMap[r.tool];
    t.input          += r.input;
    t.output         += r.output;
    t.cache_read     += r.cache_read;
    t.cache_creation += r.cache_creation;
    t.turns          += r.turns;
    t.cost           += calcCost(r.model, r.input, r.output, r.cache_read, r.cache_creation);
  }
  const byTool = Object.values(toolMap).sort((a, b) => b.cost - a.cost);
  const hotSessions = sessionSignals(filteredSessions);

  // Hourly aggregation (filtered by model + range, then bucketed by UTC hour)
  const hourlySrc = (rawData.hourly_by_model || []).filter(r =>
    selectedModels.has(r.model) && (!start || r.day >= start) && (!end || r.day <= end)
  );
  const hourlyAgg = aggregateHourly(hourlySrc, hourlyTZ);

  // Update daily chart title
  document.getElementById('daily-chart-title').textContent = 'Daily Token Usage \u2014 ' + RANGE_LABELS[selectedRange];
  document.getElementById('hourly-chart-title').textContent = 'Average Hourly Distribution \u2014 ' + RANGE_LABELS[selectedRange];

  renderStats(totals);
  renderDailyChart(daily);
  renderHourlyChart(hourlyAgg);
  renderModelChart(byModel);
  renderProjectChart(byProject);
  renderCostBreakdown(costTotals);
  lastFilteredSessions = sortSessions(filteredSessions);
  lastByModel = byModel;
  lastByProject = sortProjects(byProject);
  lastByProjectBranch = sortProjectBranch(byProjectBranch);
  lastByTool = byTool;
  renderSessionsTable(lastFilteredSessions.slice(0, 20));
  renderModelCostTable(byModel);
  renderToolCostTable(lastByTool.slice(0, 15));
  renderSessionSignalsTable(hotSessions.slice(0, 15));
  renderProjectCostTable(lastByProject.slice(0, 20));
  renderProjectBranchCostTable(lastByProjectBranch.slice(0, 20));
}

// ── Renderers ──────────────────────────────────────────────────────────────
function renderStats(t) {
  const rangeLabel = RANGE_LABELS[selectedRange].toLowerCase();
  const stats = [
    { label: 'Sessions',       value: t.sessions.toLocaleString(), sub: rangeLabel },
    { label: 'Turns',          value: fmt(t.turns),                sub: rangeLabel },
    { label: 'Input Tokens',   value: fmt(t.input),                sub: rangeLabel },
    { label: 'Output Tokens',  value: fmt(t.output),               sub: rangeLabel },
    { label: 'Cache Read',     value: fmt(t.cache_read),           sub: 'from prompt cache' },
    { label: 'Cache Creation', value: fmt(t.cache_creation),       sub: 'writes to prompt cache' },
    { label: 'Est. Cost',      value: fmtCostBig(t.cost),          sub: 'API pricing, May 2026', color: C.green },
  ];
  document.getElementById('stats-row').innerHTML = stats.map(s => `
    <div class="stat-card">
      <div class="label">${s.label}</div>
      <div class="value" style="${s.color ? 'color:' + s.color : ''}">${esc(s.value)}</div>
      ${s.sub ? `<div class="sub">${esc(s.sub)}</div>` : ''}
    </div>
  `).join('');
}

function renderCostBreakdown(c) {
  const cacheShare = c.tokens ? c.cache_tokens / c.tokens : 0;
  document.getElementById('cost-breakdown-body').innerHTML = `<tr>
    <td class="cost">${fmtCost(c.input)}</td>
    <td class="cost">${fmtCost(c.output)}</td>
    <td class="cost">${fmtCost(c.cache_read)}</td>
    <td class="cost">${fmtCost(c.cache_creation)}</td>
    <td class="cost">${fmtCostBig(c.total)}</td>
    <td class="cost">${fmtCostBig(c.cache_savings)}</td>
    <td class="num">${fmtPct(cacheShare)}</td>
  </tr>`;
}

// Bucket rows into 24 hours (display-TZ), summing turns + output, and count
// the unique days in the input so the caller can compute per-day averages.
function aggregateHourly(rows, tzMode) {
  const byHour = {};
  for (let h = 0; h < 24; h++) byHour[h] = { turns: 0, output: 0 };
  const days = new Set();
  for (const r of rows) {
    const displayHour = utcHourToDisplay(r.hour, tzMode);
    byHour[displayHour].turns  += r.turns  || 0;
    byHour[displayHour].output += r.output || 0;
    if (r.day) days.add(r.day);
  }
  const dayCount = days.size;
  const hours = [];
  for (let h = 0; h < 24; h++) {
    hours.push({
      hour:       h,
      avgTurns:   dayCount ? byHour[h].turns  / dayCount : 0,
      avgOutput:  dayCount ? byHour[h].output / dayCount : 0,
      totalTurns: byHour[h].turns,
      peak:       isPeakHour(h, tzMode),
    });
  }
  return { hours, dayCount };
}

function renderHourlyChart(agg) {
  const dayCountEl = document.getElementById('hourly-day-count');
  dayCountEl.textContent = agg.dayCount
    ? agg.dayCount + ' day' + (agg.dayCount === 1 ? '' : 's') + ' averaged · ' + tzDisplayName(hourlyTZ)
    : 'No data · ' + tzDisplayName(hourlyTZ);

  const ctx = document.getElementById('chart-hourly').getContext('2d');
  if (charts.hourly) charts.hourly.destroy();

  const labels = agg.hours.map(h => formatHourLabel(h.hour));
  const turns  = agg.hours.map(h => h.avgTurns);
  const output = agg.hours.map(h => h.avgOutput);
  const barColors      = agg.hours.map(h => h.peak ? 'rgba(199,78,57,0.9)' : TOKEN_COLORS.input);
  const barHoverColors = agg.hours.map(h => h.peak ? 'rgba(199,78,57,1)'   : TOKEN_HOVER.input);

  charts.hourly = new Chart(ctx, {
    data: {
      labels: labels,
      datasets: [
        {
          type: 'bar',
          label: 'Avg turns / hour',
          hidden: hiddenSeries.hourly.has('Avg turns / hour'),
          data: turns,
          backgroundColor: barColors,
          hoverBackgroundColor: barHoverColors,
          pointStyle: 'rect',
          yAxisID: 'y',
          order: 2,
        },
        {
          type: 'line',
          label: 'Avg output tokens / hour',
          hidden: hiddenSeries.hourly.has('Avg output tokens / hour'),
          data: output,
          borderColor: TOKEN_COLORS.output,
          backgroundColor: 'rgba(217,119,87,0.15)',
          borderWidth: 2,
          pointRadius: 2,
          pointHoverRadius: 4,
          pointHoverBackgroundColor: TOKEN_HOVER.output,
          pointStyle: 'circle',
          pointBackgroundColor: TOKEN_COLORS.output,
          pointBorderColor: TOKEN_COLORS.output,
          tension: 0.3,
          yAxisID: 'y1',
          order: 1,
        },
      ]
    },
    options: {
      responsive: true, maintainAspectRatio: false, resizeDelay: 150,
      interaction: { mode: 'index', intersect: false },
      plugins: {
        legend: { onClick: legendToggle('hourly'), labels: { color: C.axis, usePointStyle: true, boxWidth: 8, boxHeight: 8 } },
        tooltip: {
          usePointStyle: true,
          callbacks: {
            title: (items) => {
              if (!items.length) return '';
              const idx = items[0].dataIndex;
              const h = agg.hours[idx];
              const base = formatHourLabel(h.hour) + ' ' + tzDisplayName(hourlyTZ);
              return h.peak ? base + ' · Peak — Anthropic US hours' : base;
            },
            label: (item) => {
              if (item.dataset.label && item.dataset.label.indexOf('turns') !== -1) {
                return ' Avg turns: ' + item.parsed.y.toFixed(2);
              }
              return ' Avg output: ' + fmt(item.parsed.y);
            },
          }
        },
      },
      scales: {
        x: { ticks: { color: C.axis, maxRotation: 0, autoSkip: false, font: { size: 10 } }, grid: { color: C.border } },
        y:  { position: 'left',  beginAtZero: true, ticks: { color: C.axis, callback: v => v.toFixed(1) },     grid: { color: C.border }, title: { display: true, text: 'Avg turns / hour',         color: C.axis, font: { size: 11 } } },
        y1: { position: 'right', beginAtZero: true, ticks: { color: C.axis, callback: v => fmt(v) }, grid: { drawOnChartArea: false },   title: { display: true, text: 'Avg output tokens / hour', color: C.axis, font: { size: 11 } } },
      }
    }
  });
}

function renderDailyChart(daily) {
  const ctx = document.getElementById('chart-daily').getContext('2d');
  if (charts.daily) charts.daily.destroy();
  charts.daily = new Chart(ctx, {
    type: 'bar',
    data: {
      labels: daily.map(d => d.day),
      datasets: [
        { label: 'Input',          hidden: hiddenSeries.daily.has('Input'),          data: daily.map(d => d.input),          backgroundColor: TOKEN_COLORS.input,          hoverBackgroundColor: TOKEN_HOVER.input,          stack: 'io',    yAxisID: 'y1' },
        { label: 'Output',         hidden: hiddenSeries.daily.has('Output'),         data: daily.map(d => d.output),         backgroundColor: TOKEN_COLORS.output,         hoverBackgroundColor: TOKEN_HOVER.output,         stack: 'io',    yAxisID: 'y1' },
        { label: 'Cache Read',     hidden: hiddenSeries.daily.has('Cache Read'),     data: daily.map(d => d.cache_read),     backgroundColor: TOKEN_COLORS.cache_read,     hoverBackgroundColor: TOKEN_HOVER.cache_read,     stack: 'cache', yAxisID: 'y' },
        { label: 'Cache Creation', hidden: hiddenSeries.daily.has('Cache Creation'), data: daily.map(d => d.cache_creation), backgroundColor: TOKEN_COLORS.cache_creation, hoverBackgroundColor: TOKEN_HOVER.cache_creation, stack: 'cache', yAxisID: 'y' },
      ]
    },
    options: {
      responsive: true, maintainAspectRatio: false, resizeDelay: 150,
      plugins: { legend: { onClick: legendToggle('daily'), labels: { color: C.axis, boxWidth: 12 } } },
      scales: {
        x: { ticks: { color: C.axis, maxTicksLimit: RANGE_TICKS[selectedRange] }, grid: { color: C.border } },
        y:  { position: 'left',  ticks: { color: C.green, callback: v => fmt(v) }, grid: { color: C.border }, title: { display: true, text: 'Cache', color: C.green } },
        y1: { position: 'right', ticks: { color: C.blue, callback: v => fmt(v) }, grid: { drawOnChartArea: false },    title: { display: true, text: 'Input / Output', color: C.blue } },
      }
    }
  });
}

function renderModelChart(byModel) {
  const ctx = document.getElementById('chart-model').getContext('2d');
  if (charts.model) charts.model.destroy();
  if (!byModel.length) { charts.model = null; return; }
  charts.model = new Chart(ctx, {
    type: 'doughnut',
    data: {
      labels: byModel.map(m => m.model),
      datasets: [{ data: byModel.map(m => m.input + m.output), backgroundColor: MODEL_COLORS, hoverBackgroundColor: MODEL_COLORS, hoverOffset: 8, borderWidth: 2, borderColor: C.card, hoverBorderColor: C.card }]
    },
    options: {
      responsive: true, maintainAspectRatio: false, resizeDelay: 150,
      plugins: {
        legend: {
          position: 'bottom',
          labels: { color: C.axis, boxWidth: 12, font: { size: 11 } },
          onClick: (e, item, legend) => {
            const ci = legend.chart;
            ci.toggleDataVisibility(item.index);
            const label = ci.data.labels[item.index];
            if (!ci.getDataVisibility(item.index)) hiddenSeries.model.add(label); else hiddenSeries.model.delete(label);
            ci.update();
          },
        },
        tooltip: { callbacks: { label: ctx => ` ${ctx.label}: ${fmt(ctx.raw)} tokens` } }
      }
    }
  });
  // Reapply any slices the user toggled off in a previous render.
  byModel.forEach((m, i) => {
    if (hiddenSeries.model.has(m.model) && charts.model.getDataVisibility(i)) charts.model.toggleDataVisibility(i);
  });
  charts.model.update();
}

function renderProjectChart(byProject) {
  const top = byProject.slice(0, 10);
  const ctx = document.getElementById('chart-project').getContext('2d');
  if (charts.project) charts.project.destroy();
  if (!top.length) { charts.project = null; return; }
  charts.project = new Chart(ctx, {
    type: 'bar',
    data: {
      labels: top.map(p => p.project.length > 22 ? '\u2026' + p.project.slice(-20) : p.project),
      datasets: [
        { label: 'Input',  hidden: hiddenSeries.project.has('Input'),  data: top.map(p => p.input),  backgroundColor: TOKEN_COLORS.input,  hoverBackgroundColor: TOKEN_HOVER.input },
        { label: 'Output', hidden: hiddenSeries.project.has('Output'), data: top.map(p => p.output), backgroundColor: TOKEN_COLORS.output, hoverBackgroundColor: TOKEN_HOVER.output },
      ]
    },
    options: {
      indexAxis: 'y', responsive: true, maintainAspectRatio: false, resizeDelay: 150,
      plugins: { legend: { onClick: legendToggle('project'), labels: { color: C.axis, boxWidth: 12 } } },
      scales: {
        x: { ticks: { color: C.axis, callback: v => fmt(v) }, grid: { color: C.border } },
        y: { ticks: { color: C.axis, font: { size: 11 } }, grid: { color: C.border } },
      }
    }
  });
}

// Fills a table card's footer with the row-reveal control. Three states:
//   - more rows fit under the cap        -> "Show more" (plus "Show less" once expanded)
//   - cap reached but more records exist -> "Download CSV to see all (N)" + "Show less"
//   - every row is already visible       -> "Show less"
// "Show less" is hidden at the initial step (nothing to collapse yet). Renders
// nothing when the whole table fits in the first step. Carets: more = down (▾),
// less = up (▴).
function renderTableToggle(footId, total, limit, lessName, moreName, csvName) {
  const foot = document.getElementById(footId);
  if (!foot) return;
  if (total <= TABLE_STEPS[0]) { foot.innerHTML = ''; return; }
  const less = '<button class="show-more-btn" onclick="' + lessName + '()">Show less ▴</button>';
  const more = '<button class="show-more-btn" onclick="' + moreName + '()">Show more ▾</button>';
  let html;
  if (limit < total && limit < TABLE_MAX) {
    // more rows fit under the cap; Show less only once we're past the first step
    html = (limit > TABLE_STEPS[0] ? less : '') + more;
  } else if (limit < total) {           // cap reached, remaining rows only via CSV
    html = '<a class="show-more-link" href="#" onclick="' + csvName + '(); return false;">Download CSV to see all (' + total + ')</a>' + less;
  } else {                              // everything already visible
    html = less;
  }
  foot.innerHTML = html;
}

// After collapsing a table, bring its top back into view — the user may have
// scrolled down through the expanded rows.
function scrollTableToTop(bodyId) {
  const card = document.getElementById(bodyId)?.closest('.table-card');
  if (card) card.scrollIntoView({ behavior: 'smooth', block: 'start' });
}

// "Show more" advances one step (capped at TABLE_MAX); "Show less" resets to 10
// and scrolls back to the top of that table.
function moreModelRows()   { modelLimit    = nextTableLimit(modelLimit,    lastByModel.length);        renderModelCostTable(lastByModel); }
function lessModelRows()   { modelLimit    = TABLE_STEPS[0]; renderModelCostTable(lastByModel);            scrollTableToTop('model-cost-body'); }
function moreSessionRows() { sessionsLimit = nextTableLimit(sessionsLimit, lastFilteredSessions.length); renderSessionsTable(lastFilteredSessions); }
function lessSessionRows() { sessionsLimit = TABLE_STEPS[0]; renderSessionsTable(lastFilteredSessions);    scrollTableToTop('sessions-body'); }
function moreProjectRows() { projectLimit  = nextTableLimit(projectLimit,  lastByProject.length);       renderProjectCostTable(lastByProject); }
function lessProjectRows() { projectLimit  = TABLE_STEPS[0]; renderProjectCostTable(lastByProject);        scrollTableToTop('project-cost-body'); }
function moreBranchRows()  { branchLimit   = nextTableLimit(branchLimit,   lastByProjectBranch.length); renderProjectBranchCostTable(lastByProjectBranch); }
function lessBranchRows()  { branchLimit   = TABLE_STEPS[0]; renderProjectBranchCostTable(lastByProjectBranch); scrollTableToTop('project-branch-cost-body'); }

function renderSessionsTable(sessions) {
  const shown = sessions.slice(0, sessionsLimit);
  document.getElementById('sessions-body').innerHTML = shown.map(s => {
    const cost = calcCost(s.model, s.input, s.output, s.cache_read, s.cache_creation);
    const costCell = isBillable(s.model)
      ? `<td class="cost">${fmtCost(cost)}</td>`
      : `<td class="cost-na">n/a</td>`;
    return `<tr>
      <td class="muted" style="font-family:monospace"><button class="link-btn" data-session="${esc(s.full_session_id)}" onclick="openSessionDetail(this.dataset.session)">${esc(s.session_id)}&hellip;</button></td>
      <td>${esc(s.project)}</td>
      <td class="muted">${esc(s.last)}</td>
      <td class="muted">${esc(s.duration_min)}m</td>
      <td><span class="model-tag">${esc(s.model)}</span></td>
      <td class="num">${s.turns}</td>
      <td class="num">${fmt(s.input)}</td>
      <td class="num">${fmt(s.output)}</td>
      <td class="num">${fmt(s.cache_read)}</td>
      <td class="num">${fmt(s.cache_creation)}</td>
      ${costCell}
    </tr>`;
  }).join('');
  renderTableToggle('sessions-foot', sessions.length, sessionsLimit, 'lessSessionRows', 'moreSessionRows', 'exportSessionsCSV');
}

function renderToolCostTable(rows) {
  document.getElementById('tool-cost-body').innerHTML = rows.map(t => {
    const costCell = t.cost ? `<td class="cost">${fmtCost(t.cost)}</td>` : `<td class="cost-na">n/a</td>`;
    return `<tr>
      <td>${esc(t.tool)}</td>
      <td class="num">${fmt(t.turns)}</td>
      <td class="num">${fmt(t.input)}</td>
      <td class="num">${fmt(t.output)}</td>
      <td class="num">${fmt(t.cache_read)}</td>
      <td class="num">${fmt(t.cache_creation)}</td>
      ${costCell}
    </tr>`;
  }).join('');
}

function renderSessionSignalsTable(rows) {
  document.getElementById('session-signals-body').innerHTML = rows.map(s => {
    return `<tr>
      <td class="muted" style="font-family:monospace"><button class="link-btn" data-session="${esc(s.full_session_id)}" onclick="openSessionDetail(this.dataset.session)">${esc(s.session_id)}&hellip;</button></td>
      <td>${esc(s.project)}</td>
      <td class="cost">${fmtCost(s.cost)}</td>
      <td class="cost">${fmtCost(s.cost_per_min)}</td>
      <td class="num">${fmt(s.tokens_per_min)}</td>
      <td class="num">${fmt(s.output_per_turn)}</td>
      <td class="num">${fmtPct(s.cache_share)}</td>
    </tr>`;
  }).join('');
}

// ── Tool call detail (compact summary + slide-in panel) ─────────────────────
function toolTruncate(s, n) {
  s = String(s == null ? '' : s).replace(/\s+/g, ' ').trim();
  return s.length > n ? s.slice(0, n - 1) + '…' : s;
}
function toolBaseName(p) {
  if (!p) return '';
  return String(p).replace(/\\/g, '/').replace(/\/+$/, '').split('/').pop();
}
// Compact one-line label for a single tool call: "Bash: git status", "Skill: humanizer", …
function toolCallSummary(tc) {
  const name = tc.name || 'tool';
  const inp = tc.input || {};
  switch (name) {
    case 'Bash':
    case 'PowerShell':  return name + ': ' + toolTruncate(inp.description || inp.command, 64);
    case 'Skill':       return 'Skill: ' + (inp.skill || '?') + (inp.args ? ' ' + toolTruncate(inp.args, 30) : '');
    case 'Read':        return 'Read: ' + toolBaseName(inp.file_path);
    case 'Write':       return 'Write: ' + toolBaseName(inp.file_path);
    case 'Edit':        return 'Edit: ' + toolBaseName(inp.file_path);
    case 'NotebookEdit':return 'NotebookEdit: ' + toolBaseName(inp.notebook_path || inp.file_path);
    case 'Grep':        return 'Grep: ' + toolTruncate(inp.pattern, 44);
    case 'Glob':        return 'Glob: ' + toolTruncate(inp.pattern, 44);
    case 'Agent':       return 'Agent: ' + (inp.subagent_type || toolTruncate(inp.description, 30));
    case 'WebSearch':   return 'WebSearch: ' + toolTruncate(inp.query, 44);
    case 'WebFetch':    return 'WebFetch: ' + toolTruncate(inp.url, 44);
    case 'Task':        return 'Task: ' + toolTruncate(inp.subject || inp.description, 40);
    case 'ToolSearch':  return 'ToolSearch: ' + toolTruncate(inp.query, 40);
    default:
      if (name.indexOf('mcp__') === 0) {
        const parts = name.split('__');
        return 'mcp/' + parts[parts.length - 1];
      }
      return name;
  }
}
// Table cell: compact summary of all tool calls in the turn + a "details" link.
function toolCellHTML(t, i) {
  const calls = t.tool_calls || [];
  if (!calls.length) return esc(t.tool || '<none>');
  let label = toolCallSummary(calls[0]);
  if (calls.length > 1) label += ' +' + (calls.length - 1);
  return esc(label) +
    ` <button class="link-btn tool-detail-link" onclick="openToolPanel(${i})" title="Show full tool inputs">details</button>`;
}
function openToolPanel(i) {
  const t = (selectedSessionDetail && selectedSessionDetail.turns) ? selectedSessionDetail.turns[i] : null;
  if (!t) return;
  const calls = t.tool_calls || [];
  document.getElementById('tool-panel-title').textContent =
    calls.length + (calls.length === 1 ? ' tool call' : ' tool calls');
  document.getElementById('tool-panel-sub').textContent = (t.timestamp || '') + (t.model ? ' · ' + t.model : '');
  document.getElementById('tool-panel-body').innerHTML = calls.map(tc => {
    let body = '';
    const inp = tc.input || {};
    if ((tc.name === 'Bash' || tc.name === 'PowerShell') && inp.command) {
      if (inp.description) body += `<div class="field">Description</div><pre>${esc(inp.description)}</pre>`;
      body += `<div class="field">Command</div><pre>${esc(inp.command)}</pre>`;
    } else {
      body = `<pre>${esc(JSON.stringify(inp, null, 2))}</pre>`;
    }
    return `<div class="tool-call-block"><h4>${esc(tc.name || 'tool')}</h4>${body}</div>`;
  }).join('') || '<p class="muted">No tool inputs recorded for this turn.</p>';
  document.getElementById('tool-detail-overlay').classList.remove('hidden');
  document.getElementById('tool-detail-panel').classList.remove('hidden');
}
function closeToolPanel() {
  document.getElementById('tool-detail-overlay').classList.add('hidden');
  document.getElementById('tool-detail-panel').classList.add('hidden');
}
document.addEventListener('keydown', e => { if (e.key === 'Escape') closeToolPanel(); });

function renderSessionDetail(d) {
  selectedSessionDetail = d;
  const s = d.session;
  const c = calcCostBreakdown(s.model, s.input, s.output, s.cache_read, s.cache_creation);
  const tokens = (s.input || 0) + (s.output || 0) + (s.cache_read || 0) + (s.cache_creation || 0);
  const promptTokens = (s.input || 0) + (s.cache_read || 0) + (s.cache_creation || 0);
  document.getElementById('dashboard-view').classList.add('hidden');
  document.getElementById('filter-bar').classList.add('hidden');
  document.getElementById('session-detail-view').classList.remove('hidden');
  document.getElementById('session-detail-title').textContent = 'Session Drilldown — ' + s.display_id + '…';
  document.getElementById('session-detail-subtitle').textContent = s.session_id + ' · ' + s.project;
  document.getElementById('session-detail-stats').innerHTML = [
    ['Project', s.project],
    ['Model', s.model],
    ['Duration', s.duration_min + 'm'],
    ['Turns', fmt(s.turns)],
    ['Tokens', fmt(tokens)],
    ['Cost', fmtCost(c.total)],
    ['Cache Share', fmtPct(promptTokens ? ((s.cache_read || 0) + (s.cache_creation || 0)) / promptTokens : 0)],
    ['Cache Savings', fmtCost(c.cache_savings)],
  ].map(([label, value]) => `<div class="detail-stat"><div class="label">${esc(label)}</div><div class="value">${esc(value)}</div></div>`).join('');
  document.getElementById('session-detail-body').innerHTML = d.turns.map((t, i) => {
    const cost = calcCost(t.model, t.input, t.output, t.cache_read, t.cache_creation);
    return `<tr>
      <td class="muted">${esc(t.timestamp)}</td>
      <td>${toolCellHTML(t, i)}</td>
      <td><span class="model-tag">${esc(t.model)}</span></td>
      <td class="num">${fmt(t.input)}</td>
      <td class="num">${fmt(t.output)}</td>
      <td class="num">${fmt(t.cache_read)}</td>
      <td class="num">${fmt(t.cache_creation)}</td>
      <td class="num">${fmt(t.cache_creation_5m || 0)}</td>
      <td class="num">${fmt(t.cache_creation_1h || 0)}</td>
      <td class="muted">${t.duration_ms ? (t.duration_ms / 1000).toFixed(1) + 's' : ''}</td>
      <td class="muted">${esc(t.stop_reason || '')}</td>
      <td class="muted">${esc(t.service_tier || '')}</td>
      <td class="cost">${fmtCost(cost)}</td>
    </tr>`;
  }).join('');
  renderSessionToolBreakdown(d.turns);
  renderSessionTimeline(d.turns);
  window.scrollTo({ top: 0, behavior: 'smooth' });
}

function renderSessionToolBreakdown(turns) {
  const byTool = {};
  for (const t of turns) {
    const tool = t.tool || '<none>';
    if (!byTool[tool]) byTool[tool] = { tool, turns: 0, input: 0, output: 0, cache_read: 0, cache_creation: 0, cost: 0 };
    const row = byTool[tool];
    row.turns++;
    row.input += t.input || 0;
    row.output += t.output || 0;
    row.cache_read += t.cache_read || 0;
    row.cache_creation += t.cache_creation || 0;
    row.cost += calcCost(t.model, t.input, t.output, t.cache_read, t.cache_creation);
  }
  const rows = Object.values(byTool).sort((a, b) => b.cost - a.cost);
  const totalCost = rows.reduce((sum, r) => sum + r.cost, 0);
  document.getElementById('session-tool-breakdown-body').innerHTML = rows.map(r => {
    return `<tr>
      <td>${esc(r.tool)}</td>
      <td class="num">${fmt(r.turns)}</td>
      <td class="num">${fmt(r.input)}</td>
      <td class="num">${fmt(r.output)}</td>
      <td class="num">${fmt(r.cache_read)}</td>
      <td class="num">${fmt(r.cache_creation)}</td>
      <td class="cost">${fmtCost(r.cost)}</td>
      <td class="num">${fmtPct(totalCost ? r.cost / totalCost : 0)}</td>
    </tr>`;
  }).join('');
}

function renderSessionTimeline(turns) {
  const ctx = document.getElementById('chart-session-timeline').getContext('2d');
  if (charts.sessionTimeline) charts.sessionTimeline.destroy();
  const labels = turns.map((t, i) => (i + 1) + ' · ' + (t.tool || '<none>'));
  charts.sessionTimeline = new Chart(ctx, {
    type: 'bar',
    data: {
      labels,
      datasets: [
        { label: 'Input',          data: turns.map(t => t.input),          backgroundColor: TOKEN_COLORS.input,          stack: 'tokens', yAxisID: 'y' },
        { label: 'Output',         data: turns.map(t => t.output),         backgroundColor: TOKEN_COLORS.output,         stack: 'tokens', yAxisID: 'y' },
        { label: 'Cache Read',     data: turns.map(t => t.cache_read),     backgroundColor: TOKEN_COLORS.cache_read,     stack: 'tokens', yAxisID: 'y' },
        { label: 'Cache Creation', data: turns.map(t => t.cache_creation), backgroundColor: TOKEN_COLORS.cache_creation, stack: 'tokens', yAxisID: 'y' },
        {
          label: 'Cost',
          type: 'line',
          data: turns.map(t => calcCost(t.model, t.input, t.output, t.cache_read, t.cache_creation)),
          borderColor: '#4ade80',
          backgroundColor: 'rgba(74,222,128,0.18)',
          pointRadius: 3,
          tension: 0.25,
          yAxisID: 'y1',
        },
      ]
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      plugins: {
        legend: { labels: { color: '#8892a4', boxWidth: 12, font: { size: 11 } } },
        tooltip: {
          callbacks: {
            title: items => {
              const t = turns[items[0].dataIndex];
              return (t.timestamp || '') + ' · ' + (t.tool || '<none>');
            },
            label: item => item.dataset.label === 'Cost'
              ? ' Cost: ' + fmtCost(item.parsed.y)
              : ' ' + item.dataset.label + ': ' + fmt(item.parsed.y),
          }
        }
      },
      scales: {
        x: { ticks: { color: '#8892a4', maxRotation: 0, autoSkip: true, maxTicksLimit: 12 }, grid: { color: '#2a2d3a' } },
        y: { stacked: true, beginAtZero: true, ticks: { color: '#8892a4', callback: v => fmt(v) }, grid: { color: '#2a2d3a' } },
        y1: { position: 'right', beginAtZero: true, ticks: { color: '#4ade80', callback: v => '$' + Number(v).toFixed(2) }, grid: { drawOnChartArea: false } },
      }
    }
  });
}

function closeSessionDetail() {
  document.getElementById('session-detail-view').classList.add('hidden');
  document.getElementById('dashboard-view').classList.remove('hidden');
  document.getElementById('filter-bar').classList.remove('hidden');
  const params = new URLSearchParams(window.location.search);
  params.delete('session');
  const search = params.toString() ? '?' + params.toString() : '';
  history.replaceState(null, '', window.location.pathname + search);
  window.scrollTo({ top: 0, behavior: 'smooth' });
}

async function openSessionDetail(sessionId) {
  try {
    const resp = await fetch('/api/session?session_id=' + encodeURIComponent(sessionId));
    const d = await resp.json();
    if (d.error) throw new Error(d.error);
    renderSessionDetail(d);
    updateURLForSession(sessionId);
  } catch(e) {
    console.error(e);
  }
}

function setModelSort(col) {
  if (modelSortCol === col) {
    modelSortDir = modelSortDir === 'desc' ? 'asc' : 'desc';
  } else {
    modelSortCol = col;
    modelSortDir = 'desc';
  }
  updateModelSortIcons();
  applyFilter();
}

function updateModelSortIcons() {
  document.querySelectorAll('[id^="msort-"]').forEach(el => el.textContent = '');
  const icon = document.getElementById('msort-' + modelSortCol);
  if (icon) icon.textContent = modelSortDir === 'desc' ? ' \u25bc' : ' \u25b2';
}

function sortModels(byModel) {
  return [...byModel].sort((a, b) => {
    let av, bv;
    if (modelSortCol === 'cost') {
      av = calcCost(a.model, a.input, a.output, a.cache_read, a.cache_creation);
      bv = calcCost(b.model, b.input, b.output, b.cache_read, b.cache_creation);
    } else {
      av = a[modelSortCol] ?? 0;
      bv = b[modelSortCol] ?? 0;
    }
    if (av < bv) return modelSortDir === 'desc' ? 1 : -1;
    if (av > bv) return modelSortDir === 'desc' ? -1 : 1;
    return 0;
  });
}

function renderModelCostTable(byModel) {
  const sorted = sortModels(byModel);
  const shown = sorted.slice(0, modelLimit);
  document.getElementById('model-cost-body').innerHTML = shown.map(m => {
    const cost = calcCost(m.model, m.input, m.output, m.cache_read, m.cache_creation);
    const costCell = isBillable(m.model)
      ? `<td class="cost">${fmtCost(cost)}</td>`
      : `<td class="cost-na">n/a</td>`;
    return `<tr>
      <td><span class="model-tag">${esc(m.model)}</span></td>
      <td class="num">${fmt(m.turns)}</td>
      <td class="num">${fmt(m.input)}</td>
      <td class="num">${fmt(m.output)}</td>
      <td class="num">${fmt(m.cache_read)}</td>
      <td class="num">${fmt(m.cache_creation)}</td>
      ${costCell}
    </tr>`;
  }).join('');
  renderTableToggle('model-cost-foot', sorted.length, modelLimit, 'lessModelRows', 'moreModelRows', 'exportModelCSV');
}

// ── Project cost table sorting ────────────────────────────────────────────
function setProjectSort(col) {
  if (projectSortCol === col) {
    projectSortDir = projectSortDir === 'desc' ? 'asc' : 'desc';
  } else {
    projectSortCol = col;
    projectSortDir = 'desc';
  }
  updateProjectSortIcons();
  applyFilter();
}

function updateProjectSortIcons() {
  document.querySelectorAll('[id^="psort-"]').forEach(el => el.textContent = '');
  const icon = document.getElementById('psort-' + projectSortCol);
  if (icon) icon.textContent = projectSortDir === 'desc' ? ' \u25bc' : ' \u25b2';
}

function sortProjects(byProject) {
  return [...byProject].sort((a, b) => {
    const av = a[projectSortCol] ?? 0;
    const bv = b[projectSortCol] ?? 0;
    if (av < bv) return projectSortDir === 'desc' ? 1 : -1;
    if (av > bv) return projectSortDir === 'desc' ? -1 : 1;
    return 0;
  });
}

function renderProjectCostTable(byProject) {
  const sorted = sortProjects(byProject);
  const shown = sorted.slice(0, projectLimit);
  document.getElementById('project-cost-body').innerHTML = shown.map(p => {
    return `<tr>
      <td>${esc(p.project)}</td>
      <td class="num">${p.sessions}</td>
      <td class="num">${fmt(p.turns)}</td>
      <td class="num">${fmt(p.input)}</td>
      <td class="num">${fmt(p.output)}</td>
      <td class="cost">${fmtCost(p.cost)}</td>
    </tr>`;
  }).join('');
  renderTableToggle('project-cost-foot', sorted.length, projectLimit, 'lessProjectRows', 'moreProjectRows', 'exportProjectsCSV');
}

// ── Project+Branch cost table sorting ────────────────────────────────────
function setProjectBranchSort(col) {
  if (branchSortCol === col) {
    branchSortDir = branchSortDir === 'desc' ? 'asc' : 'desc';
  } else {
    branchSortCol = col;
    branchSortDir = 'desc';
  }
  updateProjectBranchSortIcons();
  applyFilter();
}

function updateProjectBranchSortIcons() {
  document.querySelectorAll('[id^="pbsort-"]').forEach(el => el.textContent = '');
  const icon = document.getElementById('pbsort-' + branchSortCol);
  if (icon) icon.textContent = branchSortDir === 'desc' ? ' \u25bc' : ' \u25b2';
}

function sortProjectBranch(rows) {
  return [...rows].sort((a, b) => {
    const pa = (a.project || '').toLowerCase();
    const pb = (b.project || '').toLowerCase();
    if (pa < pb) return -1;
    if (pa > pb) return 1;
    const av = a[branchSortCol] ?? 0;
    const bv = b[branchSortCol] ?? 0;
    if (av < bv) return branchSortDir === 'desc' ? 1 : -1;
    if (av > bv) return branchSortDir === 'desc' ? -1 : 1;
    return 0;
  });
}

function renderProjectBranchCostTable(rows) {
  const sorted = sortProjectBranch(rows);
  const shown = sorted.slice(0, branchLimit);
  document.getElementById('project-branch-cost-body').innerHTML = shown.map(pb => {
    return `<tr>
      <td>${esc(pb.project)}</td>
      <td class="muted" style="font-family:monospace">${esc(pb.branch || '\u2014')}</td>
      <td class="num">${pb.sessions}</td>
      <td class="num">${fmt(pb.turns)}</td>
      <td class="num">${fmt(pb.input)}</td>
      <td class="num">${fmt(pb.output)}</td>
      <td class="cost">${fmtCost(pb.cost)}</td>
    </tr>`;
  }).join('');
  renderTableToggle('project-branch-cost-foot', sorted.length, branchLimit, 'lessBranchRows', 'moreBranchRows', 'exportProjectBranchCSV');
}

// ── CSV Export ────────────────────────────────────────────────────────────
function csvField(val) {
  const s = String(val);
  if (s.includes(',') || s.includes('"') || s.includes('\n')) {
    return '"' + s.replace(/"/g, '""') + '"';
  }
  return s;
}

function csvTimestamp() {
  const d = new Date();
  return d.getFullYear() + '-' + String(d.getMonth()+1).padStart(2,'0') + '-' + String(d.getDate()).padStart(2,'0')
    + '_' + String(d.getHours()).padStart(2,'0') + String(d.getMinutes()).padStart(2,'0');
}

function downloadCSV(reportType, header, rows) {
  const lines = [header.map(csvField).join(',')];
  for (const row of rows) {
    lines.push(row.map(csvField).join(','));
  }
  const blob = new Blob([lines.join('\n')], { type: 'text/csv;charset=utf-8;' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = reportType + '_' + csvTimestamp() + '.csv';
  a.click();
  URL.revokeObjectURL(a.href);
}

function exportModelCSV() {
  const header = ['Model', 'Turns', 'Input', 'Output', 'Cache Read', 'Cache Creation', 'Est. Cost'];
  const rows = sortModels(lastByModel).map(m => {
    const cost = calcCost(m.model, m.input, m.output, m.cache_read, m.cache_creation);
    return [m.model, m.turns, m.input, m.output, m.cache_read, m.cache_creation, cost.toFixed(4)];
  });
  downloadCSV('cost_by_model', header, rows);
}

function exportSessionsCSV() {
  const header = ['Session', 'Project', 'Last Active', 'Duration (min)', 'Model', 'Turns', 'Input', 'Output', 'Cache Read', 'Cache Creation', 'Est. Cost'];
  const rows = lastFilteredSessions.map(s => {
    const cost = calcCost(s.model, s.input, s.output, s.cache_read, s.cache_creation);
    return [s.session_id, s.project, s.last, s.duration_min, s.model, s.turns, s.input, s.output, s.cache_read, s.cache_creation, cost.toFixed(4)];
  });
  downloadCSV('sessions', header, rows);
}

function exportSessionTurnsCSV() {
  if (!selectedSessionDetail) return;
  const header = ['Time', 'Tool', 'Model', 'Input', 'Output', 'Cache Read', 'Cache Creation', 'Cache 5m', 'Cache 1h', 'Duration (ms)', 'Stop Reason', 'Service Tier', 'Inference Geo', 'Sidechain', 'Compact Summary', 'Est. Cost'];
  const rows = selectedSessionDetail.turns.map(t => {
    const cost = calcCost(t.model, t.input, t.output, t.cache_read, t.cache_creation);
    return [t.timestamp, t.tool, t.model, t.input, t.output, t.cache_read, t.cache_creation, t.cache_creation_5m || 0, t.cache_creation_1h || 0, t.duration_ms || 0, t.stop_reason || '', t.service_tier || '', t.inference_geo || '', t.is_sidechain ? 1 : 0, t.is_compact_summary ? 1 : 0, cost.toFixed(4)];
  });
  downloadCSV('session_' + selectedSessionDetail.session.display_id + '_turns', header, rows);
}

function exportProjectsCSV() {
  const header = ['Project', 'Sessions', 'Turns', 'Input', 'Output', 'Cache Read', 'Cache Creation', 'Est. Cost'];
  const rows = lastByProject.map(p => {
    return [p.project, p.sessions, p.turns, p.input, p.output, p.cache_read, p.cache_creation, p.cost.toFixed(4)];
  });
  downloadCSV('projects', header, rows);
}

function exportProjectBranchCSV() {
  const header = ['Project', 'Branch', 'Sessions', 'Turns', 'Input', 'Output', 'Cache Read', 'Cache Creation', 'Est. Cost'];
  const rows = lastByProjectBranch.map(pb => {
    return [pb.project, pb.branch, pb.sessions, pb.turns, pb.input, pb.output, pb.cache_read, pb.cache_creation, pb.cost.toFixed(4)];
  });
  downloadCSV('projects_by_branch', header, rows);
}

// ── Rescan ────────────────────────────────────────────────────────────────
async function triggerRescan() {
  const btn = document.getElementById('rescan-btn');
  btn.disabled = true;
  btn.textContent = '\u21bb Scanning...';
  try {
    const resp = await fetch('/api/rescan', { method: 'POST' });
    const d = await resp.json();
    btn.textContent = '\u21bb Rescan (' + d.new + ' new, ' + d.updated + ' updated)';
    await loadData();
  } catch(e) {
    btn.textContent = '\u21bb Rescan (error)';
    console.error(e);
  }
  setTimeout(() => { btn.textContent = '\u21bb Rescan'; btn.disabled = false; }, 3000);
}

// ── Data loading ───────────────────────────────────────────────────────────
async function loadData() {
  try {
    const resp = await fetch('/api/data');
    const d = await resp.json();
    if (d.error) {
      document.body.innerHTML = '<div style="padding:40px;color:#C74E39">' + esc(d.error) + '</div>';
      return;
    }
    const refreshNote = rangeIncludesToday(selectedRange) ? '<br>Auto-refresh in 30s' : '';
    document.getElementById('meta').innerHTML = 'Updated: ' + esc(d.generated_at) + refreshNote;

    const isFirstLoad = rawData === null;
    rawData = d;

    if (isFirstLoad) {
      // Restore range from URL, mark active button
      selectedRange = readURLRange();
      document.querySelectorAll('.range-btn').forEach(btn =>
        btn.classList.toggle('active', btn.dataset.range === selectedRange)
      );
      // Mark default TZ button active
      document.querySelectorAll('.tz-btn').forEach(btn =>
        btn.classList.toggle('active', btn.dataset.tz === hourlyTZ)
      );
      // Build model filter (reads URL for model selection too)
      buildFilterUI(d.all_models);
      buildSessionFilterUI(d.sessions_all || []);
      readSessionFilters();
      updateSortIcons();
      updateModelSortIcons();
      updateProjectSortIcons();
      updateProjectBranchSortIcons();
    }

    applyFilter();
    if (isFirstLoad) {
      const sessionId = readURLSession();
      if (sessionId) openSessionDetail(sessionId);
    }
  } catch(e) {
    console.error(e);
  }
}

let autoRefreshTimer = null;
function scheduleAutoRefresh() {
  if (autoRefreshTimer) { clearInterval(autoRefreshTimer); autoRefreshTimer = null; }
  if (rangeIncludesToday(selectedRange)) {
    autoRefreshTimer = setInterval(loadData, 30000);
  }
}

loadPricing();
renderPricingEditor();
loadData();
scheduleAutoRefresh();
</script>
</body>
</html>
"""


def find_icon_file():
    """Locate the header icon.svg (repo-root ``resources/icon.svg``).

    Returns the path, or ``None`` so the /icon.svg route can 404 gracefully
    (the header ``<img>`` then just renders empty alt text).
    """
    candidate = Path(__file__).resolve().parent / "resources" / "icon.svg"
    return candidate if candidate.is_file() else None


class DashboardHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def handle(self):
        try:
            super().handle()
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            pass

    def do_GET(self):
        # self.path includes the query string, but every URL the UI emits has
        # one (e.g. "/?range=all"); compare the bare path so bookmarkable
        # URLs don't fall through to 404.
        parsed = urlparse(self.path)
        path = parsed.path
        if path in ("/", "/index.html"):
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(HTML_TEMPLATE.encode("utf-8"))

        elif path == "/api/data":
            data = get_dashboard_data()
            body = json.dumps(data).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        elif path == "/api/session":
            params = parse_qs(parsed.query)
            session_id = params.get("session_id", [""])[0]
            data = get_session_detail(session_id)
            body = json.dumps(data).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        elif path == "/icon.svg":
            icon = find_icon_file()
            if icon is None:
                self.send_response(404)
                self.end_headers()
                return
            body = icon.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "image/svg+xml")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "max-age=86400")
            self.end_headers()
            self.wfile.write(body)

        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/api/rescan":
            # Full rebuild: delete DB and rescan from scratch.
            # Pass DB_PATH / DEFAULT_PROJECTS_DIRS explicitly so tests that
            # patch the module globals are honored (scan's defaults are
            # frozen at def time and would otherwise target the real paths).
            import scanner
            db_path = DB_PATH
            if db_path.exists():
                db_path.unlink()
            result = scanner.scan(
                db_path=db_path,
                projects_dirs=scanner.DEFAULT_PROJECTS_DIRS,
                verbose=False,
            )
            body = json.dumps(result).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()


def serve(host=None, port=None):
    host = host or os.environ.get("HOST", "localhost")
    port = port or int(os.environ.get("PORT", "8080"))
    server = ThreadingHTTPServer((host, port), DashboardHandler)
    print(f"Dashboard running at http://{host}:{port}")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    serve()
