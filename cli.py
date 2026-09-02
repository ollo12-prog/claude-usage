"""
cli.py - Command-line interface for the Claude Code usage dashboard.

Commands:
  scan      - Scan JSONL files and update the database
  today     - Print today's usage summary
  stats     - Print all-time usage statistics
  dashboard - Scan + open browser + start dashboard server
"""

import os
import sys
import sqlite3
from pathlib import Path
from datetime import datetime, date, timedelta

from scanner import VERSION

DB_PATH = Path(os.environ.get("CLAUDE_USAGE_DB", Path.home() / ".claude" / "usage.db"))

# Timestamps are stored ISO-8601 UTC; these commands compare day buckets against
# Python's local `date.today()`, so bucketing must use SQLite's DST-aware
# 'localtime' modifier to match (a bare `substr` would be UTC and off by up to a
# day near midnight). Falls back to the UTC substr for any malformed timestamp.
LOCAL_DAY = "COALESCE(date(timestamp, 'localtime'), substr(timestamp, 1, 10))"

# Sonnet 5 launched on an introductory rate that expires; after that it bills at
# the standard Sonnet rate. Resolved once at import against today's date.
# ponytail: one dated model, so one dated constant — if a second model ever needs
# this, replace it with an effective-dated tier list rather than a second special case.
SONNET_5_INTRO_ENDS = date(2026, 8, 31)
SONNET_5_INTRO    = {"input": 2.00, "output": 10.00, "cache_read": 0.20, "cache_write": 2.50, "cache_write_1h": 4.00}
SONNET_5_STANDARD = {"input": 3.00, "output": 15.00, "cache_read": 0.30, "cache_write": 3.75, "cache_write_1h": 6.00}

def sonnet_5_pricing(day=None):
    """Sonnet 5 rate active on `day` (a date; defaults to today)."""
    return SONNET_5_INTRO if (day or date.today()) <= SONNET_5_INTRO_ENDS else SONNET_5_STANDARD

PRICING = {
    # Fable / Mythos — Anthropic's most capable class, priced at 2x Opus.
    # (Mythos 5 shares Fable 5's pricing; Project-Glasswing access only.)
    # 5.1 prices cache HITS at 0.025x input ($0.25), not the 0.1x ($1.00) every
    # other model uses (platform.claude.com/docs/en/about-claude/pricing,
    # 2026-09-02). Listed above the 5.0 rows: the prefix scan in get_pricing() is
    # first-wins over insertion order, so a dated "claude-fable-5-1-..." id must
    # meet this key before "claude-fable-5" can swallow it.
    "claude-fable-5-1":  {"input": 10.00, "output": 50.00, "cache_read": 0.25, "cache_write": 12.50, "cache_write_1h": 20.00},
    "claude-mythos-5-1": {"input": 10.00, "output": 50.00, "cache_read": 0.25, "cache_write": 12.50, "cache_write_1h": 20.00},
    "claude-fable-5":    {"input": 10.00, "output": 50.00, "cache_read": 1.00, "cache_write": 12.50, "cache_write_1h": 20.00},
    "claude-mythos-5":   {"input": 10.00, "output": 50.00, "cache_read": 1.00, "cache_write": 12.50, "cache_write_1h": 20.00},
    # Opus 5 bills at Opus 4.8's rates. Listed explicitly rather than left to the
    # "opus" substring fallback so it stays pinned if the 4.x rates ever diverge.
    "claude-opus-5":     {"input": 5.00, "output": 25.00, "cache_read": 0.50, "cache_write": 6.25, "cache_write_1h": 10.00},
    "claude-opus-4-8":   {"input": 5.00, "output": 25.00, "cache_read": 0.50, "cache_write": 6.25, "cache_write_1h": 10.00},
    "claude-opus-4-7":   {"input": 5.00, "output": 25.00, "cache_read": 0.50, "cache_write": 6.25, "cache_write_1h": 10.00},
    "claude-opus-4-6":   {"input": 5.00, "output": 25.00, "cache_read": 0.50, "cache_write": 6.25, "cache_write_1h": 10.00},
    "claude-opus-4-5":   {"input": 5.00, "output": 25.00, "cache_read": 0.50, "cache_write": 6.25, "cache_write_1h": 10.00},
    "claude-sonnet-5":   sonnet_5_pricing(),
    "claude-sonnet-4-7": {"input": 3.00, "output": 15.00, "cache_read": 0.30, "cache_write": 3.75, "cache_write_1h": 6.00},
    "claude-sonnet-4-6": {"input": 3.00, "output": 15.00, "cache_read": 0.30, "cache_write": 3.75, "cache_write_1h": 6.00},
    "claude-sonnet-4-5": {"input": 3.00, "output": 15.00, "cache_read": 0.30, "cache_write": 3.75, "cache_write_1h": 6.00},
    "claude-haiku-4-7":  {"input": 1.00, "output":  5.00, "cache_read": 0.10, "cache_write": 1.25, "cache_write_1h": 2.00},
    "claude-haiku-4-6":  {"input": 1.00, "output":  5.00, "cache_read": 0.10, "cache_write": 1.25, "cache_write_1h": 2.00},
    "claude-haiku-4-5":  {"input": 1.00, "output":  5.00, "cache_read": 0.10, "cache_write": 1.25, "cache_write_1h": 2.00},
}

def get_pricing(model):
    if not model:
        return None
    if model in PRICING:
        return PRICING[model]
    for key in PRICING:
        if model.startswith(key):
            return PRICING[key]
    # Substring fallback: match model family by keyword
    m = model.lower()
    if "fable-5-1" in m or "mythos-5-1" in m:
        return PRICING["claude-fable-5-1"]
    if "fable" in m or "mythos" in m:
        return PRICING["claude-fable-5"]
    if "opus" in m:
        return PRICING["claude-opus-4-8"]
    if "sonnet" in m:
        return PRICING["claude-sonnet-4-6"]
    if "haiku" in m:
        return PRICING["claude-haiku-4-5"]
    return None

def calc_cost(model, inp, out, cache_read, cache_creation, cache_creation_1h=0):
    """Cost of one turn. `cache_creation` is the TOTAL cache-write tokens;
    `cache_creation_1h` is the portion written with a 1-hour TTL, which bills at
    2x input instead of the 5-minute 1.25x. Defaulting it to 0 prices everything
    at the 5m rate — correct for rows recorded before the split was stored."""
    p = get_pricing(model)
    if not p:
        return 0.0
    # The 1h portion can never exceed the total it is part of.
    cc_1h = min(cache_creation_1h or 0, cache_creation or 0)
    return (
        inp                         * p["input"]       / 1_000_000 +
        out                         * p["output"]      / 1_000_000 +
        cache_read                  * p["cache_read"]  / 1_000_000 +
        (cache_creation - cc_1h)    * p["cache_write"] / 1_000_000 +
        cc_1h * p.get("cache_write_1h", p["cache_write"]) / 1_000_000
    )


def row_cost(r):
    """Cost of one aggregated query row. Every cli query selects the same column
    aliases (inp/out/cr/cc/cc1h), so the 1h split is applied in one place."""
    return calc_cost(r["model"], r["inp"] or 0, r["out"] or 0, r["cr"] or 0,
                     r["cc"] or 0, r["cc1h"] or 0)

def fmt(n):
    if n >= 1_000_000:
        return f"{n/1_000_000:.2f}M"
    if n >= 1_000:
        return f"{n/1_000:.1f}K"
    return str(n)

def fmt_cost(c):
    return f"${c:.4f}"

def hr(char="-", width=60):
    print(char * width)

def require_db():
    if not DB_PATH.exists():
        print("Database not found. Run: python cli.py scan")
        sys.exit(1)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    # Ensure the schema is current before querying. The read commands query the
    # `agents` table and the `is_subagent`/`agent_id` columns, so a pre-existing
    # DB from before those were added would raise "no such column" when a read
    # command runs before the next scan migrates it. init_db is idempotent
    # (CREATE ... IF NOT EXISTS + additive column checks), so this is a cheap
    # no-op once migrated. Mirrors get_dashboard_data in dashboard.py.
    from scanner import init_db
    init_db(conn)
    return conn


# ── Commands ──────────────────────────────────────────────────────────────────

def cmd_scan(projects_dir=None):
    from scanner import scan
    scan(projects_dir=Path(projects_dir) if projects_dir else None)


def cmd_today():
    conn = require_db()
    today = date.today().isoformat()

    rows = conn.execute(f"""
        SELECT
            COALESCE(model, 'unknown') as model,
            SUM(input_tokens)          as inp,
            SUM(output_tokens)         as out,
            SUM(cache_read_tokens)     as cr,
            SUM(cache_creation_tokens) as cc,
            SUM(cache_creation_1h_tokens) as cc1h,
            COUNT(*)                   as turns
        FROM turns
        WHERE {LOCAL_DAY} = ?
        GROUP BY model
        ORDER BY inp + out DESC
    """, (today,)).fetchall()

    sessions = conn.execute(f"""
        SELECT COUNT(DISTINCT session_id) as cnt
        FROM turns
        WHERE {LOCAL_DAY} = ?
    """, (today,)).fetchone()

    subagent = conn.execute(f"""
        SELECT
            COUNT(*) as turns,
            SUM(input_tokens + output_tokens + cache_read_tokens + cache_creation_tokens) as tokens
        FROM turns
        WHERE {LOCAL_DAY} = ?
          AND COALESCE(is_subagent, 0) = 1
    """, (today,)).fetchone()

    print()
    hr()
    print(f"  Today's Usage  ({today})")
    hr()

    if not rows:
        print("  No usage recorded today.")
        print()
        return

    total_inp = total_out = total_cr = total_cc = total_turns = 0
    total_cost = 0.0

    for r in rows:
        cost = row_cost(r)
        total_cost += cost
        total_inp += r["inp"] or 0
        total_out += r["out"] or 0
        total_cr  += r["cr"]  or 0
        total_cc  += r["cc"]  or 0
        total_turns += r["turns"]
        print(f"  {r['model']:<30}  turns={r['turns']:<4}  in={fmt(r['inp'] or 0):<8}  out={fmt(r['out'] or 0):<8}  cost={fmt_cost(cost)}")

    hr()
    print(f"  {'TOTAL':<30}  turns={total_turns:<4}  in={fmt(total_inp):<8}  out={fmt(total_out):<8}  cost={fmt_cost(total_cost)}")
    print()
    print(f"  Sessions today:   {sessions['cnt']}")
    print(f"  Subagent tokens:  {fmt(subagent['tokens'] or 0)}  ({fmt(subagent['turns'] or 0)} turns)")
    print(f"  Cache read:       {fmt(total_cr)}")
    print(f"  Cache creation:   {fmt(total_cc)}")
    hr()
    print()
    conn.close()


def cmd_week():
    conn = require_db()

    today_d = date.today()
    start_d = today_d - timedelta(days=6)
    start = start_d.isoformat()
    end = today_d.isoformat()

    by_day_model = conn.execute(f"""
        SELECT
            {LOCAL_DAY}                as day,
            COALESCE(model, 'unknown') as model,
            SUM(input_tokens)          as inp,
            SUM(output_tokens)         as out,
            SUM(cache_read_tokens)     as cr,
            SUM(cache_creation_tokens) as cc,
            SUM(cache_creation_1h_tokens) as cc1h,
            COUNT(*)                   as turns
        FROM turns
        WHERE {LOCAL_DAY} BETWEEN ? AND ?
        GROUP BY day, model
    """, (start, end)).fetchall()

    by_model = conn.execute(f"""
        SELECT
            COALESCE(model, 'unknown') as model,
            SUM(input_tokens)          as inp,
            SUM(output_tokens)         as out,
            SUM(cache_read_tokens)     as cr,
            SUM(cache_creation_tokens) as cc,
            SUM(cache_creation_1h_tokens) as cc1h,
            COUNT(*)                   as turns
        FROM turns
        WHERE {LOCAL_DAY} BETWEEN ? AND ?
        GROUP BY model
        ORDER BY inp + out DESC
    """, (start, end)).fetchall()

    sessions = conn.execute(f"""
        SELECT COUNT(DISTINCT session_id) as cnt
        FROM turns
        WHERE {LOCAL_DAY} BETWEEN ? AND ?
    """, (start, end)).fetchone()

    print()
    hr()
    print(f"  Weekly Usage  ({start} to {end})")
    hr()

    if not by_model:
        print("  No usage recorded in the last 7 days.")
        print()
        conn.close()
        return

    # Aggregate per-day across models (with per-turn cost attribution)
    per_day = {}
    for r in by_day_model:
        d = r["day"]
        bucket = per_day.setdefault(d, {"turns": 0, "inp": 0, "out": 0, "cost": 0.0})
        bucket["turns"] += r["turns"]
        bucket["inp"]   += r["inp"] or 0
        bucket["out"]   += r["out"] or 0
        bucket["cost"]  += row_cost(r)

    print("  By Day:")
    for i in range(7):
        d = (start_d + timedelta(days=i)).isoformat()
        b = per_day.get(d, {"turns": 0, "inp": 0, "out": 0, "cost": 0.0})
        print(f"    {d}  turns={b['turns']:<4}  in={fmt(b['inp']):<8}  out={fmt(b['out']):<8}  cost={fmt_cost(b['cost'])}")

    hr()
    print("  By Model:")

    total_inp = total_out = total_cr = total_cc = total_turns = 0
    total_cost = 0.0
    for r in by_model:
        cost = row_cost(r)
        total_cost  += cost
        total_inp   += r["inp"] or 0
        total_out   += r["out"] or 0
        total_cr    += r["cr"]  or 0
        total_cc    += r["cc"]  or 0
        total_turns += r["turns"]
        print(f"    {r['model']:<30}  turns={r['turns']:<4}  in={fmt(r['inp'] or 0):<8}  out={fmt(r['out'] or 0):<8}  cost={fmt_cost(cost)}")

    hr()
    print(f"    {'TOTAL':<30}  turns={total_turns:<4}  in={fmt(total_inp):<8}  out={fmt(total_out):<8}  cost={fmt_cost(total_cost)}")
    print()
    print(f"  Sessions this week:  {sessions['cnt']}")
    print(f"  Cache read:          {fmt(total_cr)}")
    print(f"  Cache creation:      {fmt(total_cc)}")
    hr()
    print()
    conn.close()


def cmd_stats():
    conn = require_db()

    # Session-level info (count, date range)
    session_info = conn.execute("""
        SELECT
            COUNT(*)                  as sessions,
            MIN(first_timestamp)      as first,
            MAX(last_timestamp)       as last
        FROM sessions
    """).fetchone()

    # All-time totals from turns (more accurate — per-turn model attribution)
    totals = conn.execute("""
        SELECT
            SUM(input_tokens)             as inp,
            SUM(output_tokens)            as out,
            SUM(cache_read_tokens)        as cr,
            SUM(cache_creation_tokens)    as cc,
            SUM(cache_creation_1h_tokens) as cc1h,
            COUNT(*)                      as turns
        FROM turns
    """).fetchone()

    # By model from turns (each turn has the actual model used)
    by_model = conn.execute("""
        SELECT
            COALESCE(model, 'unknown') as model,
            SUM(input_tokens)          as inp,
            SUM(output_tokens)         as out,
            SUM(cache_read_tokens)     as cr,
            SUM(cache_creation_tokens) as cc,
            SUM(cache_creation_1h_tokens) as cc1h,
            COUNT(*)                   as turns,
            COUNT(DISTINCT session_id) as sessions
        FROM turns
        GROUP BY model
        ORDER BY inp + out DESC
    """).fetchall()

    # Top 5 projects from turns (join with sessions for project name)
    top_projects = conn.execute("""
        SELECT
            COALESCE(s.project_name, 'unknown') as project_name,
            SUM(t.input_tokens)  as inp,
            SUM(t.output_tokens) as out,
            COUNT(*)             as turns,
            COUNT(DISTINCT t.session_id) as sessions
        FROM turns t
        LEFT JOIN sessions s ON t.session_id = s.session_id
        GROUP BY s.project_name
        ORDER BY inp + out DESC
        LIMIT 5
    """).fetchall()

    # Subagent totals (subagent tokens are included in the all-time totals above)
    subagent = conn.execute("""
        SELECT
            COUNT(*) as turns,
            SUM(input_tokens + output_tokens + cache_read_tokens + cache_creation_tokens) as tokens
        FROM turns
        WHERE COALESCE(is_subagent, 0) = 1
    """).fetchone()

    # Daily average (last 30 days)
    daily_avg = conn.execute(f"""
        SELECT
            AVG(daily_inp) as avg_inp,
            AVG(daily_out) as avg_out
        FROM (
            SELECT
                {LOCAL_DAY} as day,
                SUM(input_tokens) as daily_inp,
                SUM(output_tokens) as daily_out
            FROM turns
            WHERE timestamp >= datetime('now', '-30 days')
            GROUP BY day
        )
    """).fetchone()

    # Build total cost across all models
    total_cost = sum(
        row_cost(r)
        for r in by_model
    )

    print()
    hr("=")
    print("  Claude Code Usage - All-Time Statistics")
    hr("=")

    first_date = (session_info["first"] or "")[:10]
    last_date = (session_info["last"] or "")[:10]
    print(f"  Period:           {first_date} to {last_date}")
    print(f"  Total sessions:   {session_info['sessions'] or 0:,}")
    print(f"  Total turns:      {fmt(totals['turns'] or 0)}")
    print(f"  Subagent turns:   {fmt(subagent['turns'] or 0)}")
    print()
    print(f"  Input tokens:     {fmt(totals['inp'] or 0):<12}  (raw prompt tokens)")
    print(f"  Output tokens:    {fmt(totals['out'] or 0):<12}  (generated tokens)")
    print(f"  Cache read:       {fmt(totals['cr'] or 0):<12}  (90% cheaper than input)")
    print(f"  Cache creation:   {fmt(totals['cc'] or 0):<12}  (25% premium on input)")
    print(f"  Subagent tokens:  {fmt(subagent['tokens'] or 0):<12}  (included in totals)")
    print()
    print(f"  Est. total cost:  ${total_cost:.4f}")
    hr()

    print("  By Model:")
    for r in by_model:
        cost = row_cost(r)
        print(f"    {r['model']:<30}  sessions={r['sessions']:<4}  turns={fmt(r['turns'] or 0):<6}  "
              f"in={fmt(r['inp'] or 0):<8}  out={fmt(r['out'] or 0):<8}  cost={fmt_cost(cost)}")

    hr()
    print("  Top Projects:")
    for r in top_projects:
        print(f"    {(r['project_name'] or 'unknown'):<40}  sessions={r['sessions']:<3}  "
              f"turns={fmt(r['turns'] or 0):<6}  tokens={fmt((r['inp'] or 0)+(r['out'] or 0))}")

    if daily_avg["avg_inp"]:
        hr()
        print("  Daily Average (last 30 days):")
        print(f"    Input:   {fmt(int(daily_avg['avg_inp'] or 0))}")
        print(f"    Output:  {fmt(int(daily_avg['avg_out'] or 0))}")

    hr("=")
    print()
    conn.close()


def cmd_dashboard(projects_dir=None, host=None, port=None, no_browser=False):
    import threading
    import time

    from dashboard import serve

    host = host or os.environ.get("HOST", "localhost")
    port = int(port or os.environ.get("PORT", "8080"))

    # Bind and serve the port *first*, then scan in the background. A cold scan
    # over a large ~/.claude/projects backlog can take well over a minute.
    # Serving up front means the port is live immediately; the dashboard shows
    # whatever's already in the DB and auto-refreshes as the background scan
    # commits new data.
    #
    # Capture cmd_scan into a local so the background thread closes over the
    # current binding — keeps the test suite's mock.patch(cli.cmd_scan) effective
    # and prevents the thread from ever touching the real DB after a patch lifts.
    scan = cmd_scan

    def background_scan():
        print("Scanning in the background...")
        scan(projects_dir=projects_dir)
        print("Background scan complete.")

    threading.Thread(target=background_scan, daemon=True).start()

    # Open a browser for users running this as a script (see README).
    # Pass --no-browser to suppress it (e.g. headless / remote hosts).
    if not no_browser:
        import webbrowser

        def open_browser():
            time.sleep(1.0)
            webbrowser.open(f"http://{host}:{port}")

        threading.Thread(target=open_browser, daemon=True).start()

    serve(host=host, port=port)


# ── Entry point ───────────────────────────────────────────────────────────────

USAGE = """
Claude Code Usage Dashboard

Usage:
  python cli.py scan [--projects-dir PATH]   Scan JSONL files and update database
  python cli.py today                        Show today's usage summary
  python cli.py week                         Show last 7 days (per-day + by-model)
  python cli.py stats                        Show all-time statistics
  python cli.py dashboard [--projects-dir PATH] [--host HOST] [--port PORT] [--no-browser]
                                                 Scan + start dashboard (opens a browser unless --no-browser)
  python cli.py --version                    Print the version and exit
"""

COMMANDS = {
    "scan": cmd_scan,
    "today": cmd_today,
    "week": cmd_week,
    "stats": cmd_stats,
    "dashboard": cmd_dashboard,
}

def parse_named_arg(args, flag):
    """Extract a --flag VALUE pair from an argument list."""
    for i, arg in enumerate(args):
        if arg == flag and i + 1 < len(args):
            return args[i + 1]
    return None

def main():
    """Console entry point (``claude-usage``) and ``python cli.py`` dispatch."""
    if len(sys.argv) >= 2 and sys.argv[1] in ("--version", "-V", "version"):
        print(VERSION)
        sys.exit(0)

    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        print(USAGE)
        sys.exit(0)

    command = sys.argv[1]
    rest = sys.argv[2:]
    projects_dir = parse_named_arg(rest, "--projects-dir")

    if command == "dashboard":
        cmd_dashboard(
            projects_dir=projects_dir,
            host=parse_named_arg(rest, "--host"),
            port=parse_named_arg(rest, "--port"),
            no_browser="--no-browser" in rest,
        )
    elif command == "scan" and projects_dir:
        cmd_scan(projects_dir=projects_dir)
    else:
        COMMANDS[command]()


if __name__ == "__main__":
    main()
