"""
scanner.py - Scans Claude Code JSONL transcript files and stores data in SQLite.
"""

import json
import os
import glob
import sqlite3
from pathlib import Path
from datetime import datetime, timezone

# Single source of truth for the app version reported by the CLI (`--version`),
# the dashboard footer, and pyproject.toml (which reads this attr dynamically).
# CHANGELOG.md is the canonical version reference, but a Homebrew/uv install
# ships only the three Python files, so the runtime version has to live here as
# a constant. Keep it in lockstep with the top CHANGELOG heading — a parity test
# guards both; see tests/test_version.py.
VERSION = "1.5.6"

PROJECTS_DIR = Path.home() / ".claude" / "projects"
XCODE_PROJECTS_DIR = Path.home() / "Library" / "Developer" / "Xcode" / "CodingAssistant" / "ClaudeAgentConfig" / "projects"
DB_PATH = Path(os.environ.get("CLAUDE_USAGE_DB", Path.home() / ".claude" / "usage.db"))
DEFAULT_PROJECTS_DIRS = [PROJECTS_DIR, XCODE_PROJECTS_DIR]

# Synthetic message_id prefix for turns minted from `usage.iterations[]` entries
# of type 'advisor_message' (see parse_jsonl_file). Doubles as the marker that
# keeps those turns out of the session's primary-model vote — matched on the id
# rather than tool_name so a user's own tool named "advisor" can't be mistaken
# for one.
ADVISOR_ID_PREFIX = "advisor:"


def is_advisor_turn(turn):
    """True if this turn was minted from an advisor_message iteration."""
    return (turn.get("message_id") or "").startswith(ADVISOR_ID_PREFIX)


# Higher number = higher priority when choosing a session's primary model.
# Fable / Mythos are Anthropic's most capable class, so they outrank Opus.
MODEL_PRIORITY = {"fable": 5, "mythos": 5, "opus": 3, "sonnet": 2, "haiku": 1}


def _model_priority(model):
    """Return a priority score for a model name (higher = more capable)."""
    if not model:
        return 0
    m = model.lower()
    for keyword, priority in MODEL_PRIORITY.items():
        if keyword in m:
            return priority
    return 0


def get_db(db_path=DB_PATH):
    # Ensure the parent directory exists — on a fresh install or CI runner
    # ~/.claude may not yet exist, and sqlite3.connect needs the parent dir.
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def init_db(conn):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS sessions (
            session_id      TEXT PRIMARY KEY,
            project_name    TEXT,
            first_timestamp TEXT,
            last_timestamp  TEXT,
            git_branch      TEXT,
            total_input_tokens      INTEGER DEFAULT 0,
            total_output_tokens     INTEGER DEFAULT 0,
            total_cache_read        INTEGER DEFAULT 0,
            total_cache_creation    INTEGER DEFAULT 0,
            model           TEXT,
            turn_count      INTEGER DEFAULT 0,
            topic           TEXT
        );

        CREATE TABLE IF NOT EXISTS turns (
            id                      INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id              TEXT,
            timestamp               TEXT,
            model                   TEXT,
            input_tokens            INTEGER DEFAULT 0,
            output_tokens           INTEGER DEFAULT 0,
            cache_read_tokens       INTEGER DEFAULT 0,
            cache_creation_tokens   INTEGER DEFAULT 0,
            cache_creation_5m_tokens INTEGER DEFAULT 0,
            cache_creation_1h_tokens INTEGER DEFAULT 0,
            tool_name               TEXT,
            cwd                     TEXT,
            message_id              TEXT,
            duration_ms             INTEGER DEFAULT 0,
            stop_reason             TEXT,
            service_tier            TEXT,
            inference_geo           TEXT,
            is_sidechain            INTEGER DEFAULT 0,
            is_compact_summary      INTEGER DEFAULT 0,
            is_subagent             INTEGER DEFAULT 0,
            agent_id                TEXT
        );

        CREATE TABLE IF NOT EXISTS processed_files (
            path    TEXT PRIMARY KEY,
            mtime   REAL,
            lines   INTEGER
        );

        CREATE TABLE IF NOT EXISTS agents (
            agent_id              TEXT PRIMARY KEY,
            agent_type            TEXT,
            dispatched_in_session TEXT,
            completed_at          TEXT,
            status                TEXT,
            total_tokens          INTEGER,
            total_duration_ms     INTEGER,
            tool_use_count        INTEGER
        );

        CREATE TABLE IF NOT EXISTS schema_meta (
            key   TEXT PRIMARY KEY,
            value TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_turns_session ON turns(session_id);
        CREATE INDEX IF NOT EXISTS idx_turns_timestamp ON turns(timestamp);
        CREATE INDEX IF NOT EXISTS idx_sessions_first ON sessions(first_timestamp);
        CREATE INDEX IF NOT EXISTS idx_agents_type ON agents(agent_type);
    """)
    # Add columns if upgrading from older schema.
    existing_cols = {
        row["name"] for row in conn.execute("PRAGMA table_info(turns)").fetchall()
    }
    migrations = {
        "message_id": "TEXT",
        "cache_creation_5m_tokens": "INTEGER DEFAULT 0",
        "cache_creation_1h_tokens": "INTEGER DEFAULT 0",
        "duration_ms": "INTEGER DEFAULT 0",
        "stop_reason": "TEXT",
        "service_tier": "TEXT",
        "inference_geo": "TEXT",
        "is_sidechain": "INTEGER DEFAULT 0",
        "is_compact_summary": "INTEGER DEFAULT 0",
        "tool_calls": "TEXT",
        "is_subagent": "INTEGER DEFAULT 0",
        "agent_id": "TEXT",
    }
    for col, spec in migrations.items():
        if col not in existing_cols:
            conn.execute(f"ALTER TABLE turns ADD COLUMN {col} {spec}")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_turns_subagent ON turns(is_subagent)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_turns_agent_id ON turns(agent_id)")
    # Session topic (from custom-title / ai-title records; added in a later
    # schema version). The one-time backfill of pre-existing sessions is driven
    # by scan() via the schema_meta 'topic_backfill_done' marker (not by the
    # column-add event), so it also covers DBs that gained the column from an
    # earlier build that predated the backfill.
    _ensure_column(conn, "sessions", "topic", "TEXT")
    # Provenance for `topic`: 'custom' (user-set) or 'ai' (generated). Lets a
    # later ai-title update the topic without ever clobbering a custom one.
    _ensure_column(conn, "sessions", "topic_source", "TEXT")
    # Dispatch task label. Async ("async_launched") dispatches carry a
    # `description` but no agentType; this preserves the task text so the
    # dashboard can show it instead of a bare 'async' tag.
    _ensure_column(conn, "agents", "description", "TEXT")
    # Conditional unique index: only dedup non-null message IDs
    conn.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_turns_message_id
        ON turns(message_id) WHERE message_id IS NOT NULL AND message_id != ''
    """)
    conn.commit()


def extract_tool_calls(content):
    """Return a list of {name, input} dicts for every tool_use block in a
    message's content list. Captures *all* tool calls in a turn (an assistant
    message can fire several at once), preserving each call's full input."""
    calls = []
    if isinstance(content, list):
        for item in content:
            if isinstance(item, dict) and item.get("type") == "tool_use":
                calls.append({
                    "name": item.get("name"),
                    "input": item.get("input") or {},
                })
    return calls


def _ensure_column(conn, table, column, decl):
    """Add a column to an existing table if it isn't already present.

    Returns True if the column was just added (an upgrade of an existing DB),
    False if it was already there (fresh DB or already-migrated).
    """
    cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
        return True
    return False


def _meta_get(conn, key):
    """Read a value from the schema_meta key/value table (None if absent)."""
    row = conn.execute(
        "SELECT value FROM schema_meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def _meta_set(conn, key, value):
    """Upsert a value into the schema_meta key/value table."""
    conn.execute(
        "INSERT OR REPLACE INTO schema_meta (key, value) VALUES (?, ?)",
        (key, value))


def _extract_title(record):
    """Extract a session title from a custom-title or ai-title record."""
    rtype = record.get("type")
    if rtype == "custom-title":
        return record.get("customTitle")
    if rtype == "ai-title":
        return record.get("aiTitle")
    return None


def _backfill_topics(conn, jsonl_files):
    """One-time backfill of topics for a DB created before topic support.

    Transcript files scanned before the topic column existed are already in
    processed_files, so an incremental scan skips them and never sees the
    custom-title / ai-title records they already contain. Re-read just those
    records (turns are left untouched, so token totals cannot drift) and set the
    topic for any session that doesn't have one yet. Runs once, gated by a flag
    in schema_meta (see scan()). Returns the number of sessions filled.
    """
    needing = {r["session_id"] for r in conn.execute(
        "SELECT session_id FROM sessions WHERE topic IS NULL OR topic = ''")}
    if not needing:
        return 0

    titles = {}          # session_id -> (chosen title, source)
    has_custom = set()   # sessions whose topic came from a custom-title record
    for filepath in jsonl_files:
        try:
            with open(filepath, encoding="utf-8", errors="replace") as f:
                for line in f:
                    # Cheap prefilter: only title records carry the substring
                    # "title" (in their "custom-title" / "ai-title" type), so we
                    # skip JSON-parsing the ~99% of lines that are turns.
                    if "title" not in line:
                        continue
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    title = _extract_title(record)
                    if not title:
                        continue
                    sid = record.get("sessionId")
                    if sid not in needing:
                        continue
                    # custom-title wins; ai-title only if no custom-title seen.
                    if record.get("type") == "custom-title":
                        titles[sid] = (title, "custom")
                        has_custom.add(sid)
                    elif sid not in has_custom:
                        titles.setdefault(sid, (title, "ai"))
        except Exception as e:
            print(f"  Warning: error reading {filepath}: {e}")

    for sid, (title, source) in titles.items():
        conn.execute(
            "UPDATE sessions SET topic = ?, topic_source = ? WHERE session_id = ? "
            "AND (topic IS NULL OR topic = '')", (title, source, sid))
    conn.commit()
    return len(titles)


def project_name_from_cwd(cwd):
    """Derive a friendly project name from cwd path."""
    if not cwd:
        return "unknown"
    # Normalize to forward slashes, take last 2 components
    parts = cwd.replace("\\", "/").rstrip("/").split("/")
    if len(parts) >= 2:
        return "/".join(parts[-2:])
    return parts[-1] if parts else "unknown"


def is_subagent_record(record, source_path=""):
    """True if a record belongs to a dispatched subagent (Task/Agent tool).

    Subagents are detected three ways: an explicit ``isSidechain`` flag, an
    ``agentId`` on the record (or its ``data`` wrapper), or a transcript path
    under a ``subagents`` directory (Claude Code writes one jsonl per subagent).
    """
    if record.get("isSidechain"):
        return True
    if record.get("agentId"):
        return True
    data = record.get("data")
    if isinstance(data, dict) and data.get("agentId"):
        return True
    sp = str(source_path).replace("\\", "/").lower()
    return "/subagents/" in sp


def record_agent_id(record):
    """Pull the subagent id off a record, if any (top-level or data wrapper)."""
    agent_id = record.get("agentId")
    if not agent_id:
        data = record.get("data")
        if isinstance(data, dict):
            agent_id = data.get("agentId")
    return agent_id


def extract_agent_dispatch(record):
    """Pull subagent identity from a parent's tool_result record.

    Claude Code writes a ``toolUseResult`` dict on the user-side record that
    closes out an Agent/Task tool invocation. It carries ``agentId`` (matching
    the subagent jsonl's records) and ``agentType`` (the human-readable type
    such as 'general-purpose' or 'Explore') plus aggregate stats.
    """
    if record.get("type") != "user":
        return None
    tur = record.get("toolUseResult")
    if not isinstance(tur, dict):
        return None
    agent_id = tur.get("agentId")
    if not agent_id:
        return None
    agent_type = tur.get("agentType")
    if not agent_type:
        # Async/background dispatches ("status": "async_launched") carry no
        # agentType and no aggregate stats at launch, and never emit a later
        # completion record. Capture them as 'async' instead of dropping them —
        # otherwise every background agent shows as 'unknown' in the dashboard.
        # ponytail: single 'async' bucket; surface description per-row if wanted.
        if tur.get("isAsync") or tur.get("status") == "async_launched":
            agent_type = "async"
        else:
            return None
    return {
        "agent_id": agent_id,
        "agent_type": agent_type,
        "description": tur.get("description"),
        "dispatched_in_session": record.get("sessionId"),
        "completed_at": record.get("timestamp", ""),
        "status": tur.get("status"),
        "total_tokens": tur.get("totalTokens"),
        "total_duration_ms": tur.get("totalDurationMs"),
        "tool_use_count": tur.get("totalToolUseCount"),
    }


def upsert_agents(conn, agents):
    """Insert or update agent dispatch metadata. Last write wins per agent_id."""
    if not agents:
        return
    conn.executemany("""
        INSERT INTO agents
            (agent_id, agent_type, description, dispatched_in_session, completed_at,
             status, total_tokens, total_duration_ms, tool_use_count)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(agent_id) DO UPDATE SET
            agent_type            = excluded.agent_type,
            description           = excluded.description,
            dispatched_in_session = excluded.dispatched_in_session,
            completed_at          = excluded.completed_at,
            status                = excluded.status,
            total_tokens          = excluded.total_tokens,
            total_duration_ms     = excluded.total_duration_ms,
            tool_use_count        = excluded.tool_use_count
    """, [
        (a["agent_id"], a["agent_type"], a.get("description"),
         a.get("dispatched_in_session"), a.get("completed_at"), a.get("status"),
         a.get("total_tokens"), a.get("total_duration_ms"), a.get("tool_use_count"))
        for a in agents
    ])


def parse_jsonl_file(filepath):
    """Parse a JSONL file and return (session_metas, turns, agents, line_count).

    Deduplicates streaming events by message.id — Claude Code logs multiple
    JSONL records per API response, all sharing the same message.id. Only the
    last record per message_id is kept (it has the final usage tallies).
    """
    seen_messages = {}  # message_id -> turn dict (dedup streaming records)
    turns_no_id = []    # turns without a message_id (kept as-is)
    session_meta = {}   # session_id -> dict
    agents = {}         # agent_id -> dispatch dict
    line_count = 0

    try:
        with open(filepath, encoding="utf-8", errors="replace") as f:
            for line_count, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue

                rtype = record.get("type")
                if rtype not in ("assistant", "user", "custom-title", "ai-title"):
                    continue

                session_id = record.get("sessionId")
                if not session_id:
                    continue

                # Extract session title from title records
                title = _extract_title(record)
                if title:
                    if session_id not in session_meta:
                        session_meta[session_id] = {
                            "session_id": session_id,
                            "project_name": "unknown",
                            "first_timestamp": "",
                            "last_timestamp": "",
                            "git_branch": "",
                            "model": None,
                            "topic": None,
                            "topic_source": None,
                        }
                    meta = session_meta[session_id]
                    # custom-title always wins; ai-title only if no custom-title set
                    if rtype == "custom-title":
                        meta["topic"] = title
                        meta["topic_source"] = "custom"
                    elif rtype == "ai-title" and meta.get("topic_source") != "custom":
                        meta["topic"] = title
                        meta["topic_source"] = "ai"
                    continue

                if rtype == "user":
                    dispatch = extract_agent_dispatch(record)
                    if dispatch is not None:
                        agents[dispatch["agent_id"]] = dispatch

                timestamp = record.get("timestamp", "")
                cwd = record.get("cwd", "")
                git_branch = record.get("gitBranch", "")

                # Update session metadata from any record
                if session_id not in session_meta:
                    session_meta[session_id] = {
                        "session_id": session_id,
                        "project_name": project_name_from_cwd(cwd),
                        "first_timestamp": timestamp,
                        "last_timestamp": timestamp,
                        "git_branch": git_branch,
                        "model": None,
                        "topic": None,
                        "topic_source": None,
                    }
                else:
                    meta = session_meta[session_id]
                    if timestamp and (not meta["first_timestamp"] or timestamp < meta["first_timestamp"]):
                        meta["first_timestamp"] = timestamp
                    if timestamp and (not meta["last_timestamp"] or timestamp > meta["last_timestamp"]):
                        meta["last_timestamp"] = timestamp
                    if git_branch and not meta["git_branch"]:
                        meta["git_branch"] = git_branch

                if rtype == "assistant":
                    msg = record.get("message", {})
                    usage = msg.get("usage", {})
                    model = msg.get("model", "")
                    message_id = msg.get("id", "")

                    input_tokens = usage.get("input_tokens", 0) or 0
                    output_tokens = usage.get("output_tokens", 0) or 0
                    cache_read = usage.get("cache_read_input_tokens", 0) or 0
                    cache_creation = usage.get("cache_creation_input_tokens", 0) or 0
                    cache_creation_detail = usage.get("cache_creation", {})
                    if not isinstance(cache_creation_detail, dict):
                        cache_creation_detail = {}
                    cache_creation_5m = cache_creation_detail.get("ephemeral_5m_input_tokens", 0) or 0
                    cache_creation_1h = cache_creation_detail.get("ephemeral_1h_input_tokens", 0) or 0

                    # Only record turns that have actual token usage
                    if input_tokens + output_tokens + cache_read + cache_creation == 0:
                        continue

                    # Extract every tool call in this turn (with full inputs)
                    tool_calls = extract_tool_calls(msg.get("content", []))
                    tool_name = tool_calls[0]["name"] if tool_calls else None

                    if model:
                        session_meta[session_id]["model"] = model

                    turn = {
                        "session_id": session_id,
                        "timestamp": timestamp,
                        "model": model,
                        "input_tokens": input_tokens,
                        "output_tokens": output_tokens,
                        "cache_read_tokens": cache_read,
                        "cache_creation_tokens": cache_creation,
                        "cache_creation_5m_tokens": cache_creation_5m,
                        "cache_creation_1h_tokens": cache_creation_1h,
                        "tool_name": tool_name,
                        "cwd": cwd,
                        "message_id": message_id,
                        "duration_ms": record.get("durationMs", 0) or 0,
                        "stop_reason": record.get("stopReason") or msg.get("stop_reason") or "",
                        "service_tier": usage.get("service_tier", "") or "",
                        "inference_geo": usage.get("inference_geo", "") or "",
                        "is_sidechain": 1 if record.get("isSidechain") else 0,
                        "is_compact_summary": 1 if record.get("isCompactSummary") else 0,
                        "tool_calls": json.dumps(tool_calls) if tool_calls else None,
                        "is_subagent": 1 if is_subagent_record(record, filepath) else 0,
                        "agent_id": record_agent_id(record),
                    }

                    # Dedup: last record per message_id wins (final usage tallies)
                    if message_id:
                        seen_messages[message_id] = turn
                    else:
                        turns_no_id.append(turn)

                    # Advisor inferences run *inside* an assistant message as extra
                    # `usage.iterations[]` entries of type 'advisor_message', on a
                    # different (usually stronger) model named by the record's
                    # top-level `advisorModel`. The envelope's top-level
                    # input/output_tokens count ONLY the 'message' iterations, so
                    # advisor tokens are invisible there — a silent undercount of the
                    # entire advisor spend. (cache_read / cache_creation are already
                    # totals; advisor iterations carry none.)
                    #
                    # Each one becomes its own turn rather than extra columns: a turn
                    # is already "one inference on one model", and cost is computed
                    # per turn from that turn's model, so the advisor model is priced
                    # correctly by every existing query with no call-site change. The
                    # synthetic message_id keeps re-scans and branch copies idempotent.
                    for idx, it in enumerate(usage.get("iterations") or []):
                        if not isinstance(it, dict) or it.get("type") != "advisor_message":
                            continue
                        adv_in = it.get("input_tokens", 0) or 0
                        adv_out = it.get("output_tokens", 0) or 0
                        if adv_in + adv_out == 0:
                            continue
                        adv_turn = dict(
                            turn,
                            # advisorModel is the authority; fall back to the parent
                            # model so an unnamed advisor is priced, not free (older
                            # Claude Code builds emit iterations without advisorModel).
                            model=record.get("advisorModel") or model,
                            input_tokens=adv_in,
                            output_tokens=adv_out,
                            cache_read_tokens=it.get("cache_read_input_tokens", 0) or 0,
                            cache_creation_tokens=it.get("cache_creation_input_tokens", 0) or 0,
                            cache_creation_5m_tokens=0,
                            cache_creation_1h_tokens=0,
                            tool_name="advisor",
                            tool_calls=None,
                            message_id="%s%s:%d" % (ADVISOR_ID_PREFIX, message_id, idx),
                        )
                        # is_sidechain / is_subagent are inherited on purpose: advisor
                        # cost is main-chain cost when the parent is.
                        if message_id:
                            seen_messages[adv_turn["message_id"]] = adv_turn
                        else:
                            # No parent id to key on: the synthetic id would collide
                            # across messages, so follow the parent into the
                            # un-deduped list rather than dropping all but one.
                            adv_turn["message_id"] = ""
                            turns_no_id.append(adv_turn)

    except Exception as e:
        print(f"  Warning: error reading {filepath}: {e}")

    turns = turns_no_id + list(seen_messages.values())
    return list(session_meta.values()), turns, list(agents.values()), line_count


def aggregate_sessions(session_metas, turns):
    """Aggregate turn data back into session-level stats."""
    from collections import defaultdict, Counter

    session_stats = defaultdict(lambda: {
        "total_input_tokens": 0,
        "total_output_tokens": 0,
        "total_cache_read": 0,
        "total_cache_creation": 0,
        "turn_count": 0,
        "model": None,
    })
    session_model_counts = defaultdict(Counter)

    for t in turns:
        s = session_stats[t["session_id"]]
        s["total_input_tokens"] += t["input_tokens"]
        s["total_output_tokens"] += t["output_tokens"]
        s["total_cache_read"] += t["cache_read_tokens"]
        s["total_cache_creation"] += t["cache_creation_tokens"]
        s["turn_count"] += 1
        # Advisor turns don't vote for the session's primary model: they're an
        # auxiliary inference on a different model, and in a short session with
        # several advisor calls they could otherwise outvote the model the session
        # actually ran on.
        if t["model"] and not is_advisor_turn(t):
            session_model_counts[t["session_id"]][t["model"]] += 1

    for sid, counts in session_model_counts.items():
        if counts:
            session_stats[sid]["model"] = counts.most_common(1)[0][0]

    # Merge into session_metas
    result = []
    for meta in session_metas:
        sid = meta["session_id"]
        stats = session_stats[sid]
        result.append({**meta, **stats})
    return result


def upsert_sessions(conn, sessions):
    for s in sessions:
        # Check if session exists
        existing = conn.execute(
            "SELECT total_input_tokens, total_output_tokens, total_cache_read, "
            "total_cache_creation, turn_count FROM sessions WHERE session_id = ?",
            (s["session_id"],)
        ).fetchone()

        # A session seen only via a title record (custom-title / ai-title carry a
        # sessionId but no timestamp) has no real content. Don't let it INSERT a
        # phantom, token-less row; if the session already exists it still falls
        # through to the UPDATE below and sets its topic.
        if existing is None and not s.get("first_timestamp"):
            continue

        if existing is None:
            conn.execute("""
                INSERT INTO sessions
                    (session_id, project_name, first_timestamp, last_timestamp,
                     git_branch, total_input_tokens, total_output_tokens,
                     total_cache_read, total_cache_creation, model, turn_count,
                     topic, topic_source)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                s["session_id"], s["project_name"], s["first_timestamp"],
                s["last_timestamp"], s["git_branch"],
                s["total_input_tokens"], s["total_output_tokens"],
                s["total_cache_read"], s["total_cache_creation"],
                s["model"], s["turn_count"], s.get("topic"), s.get("topic_source")
            ))
        else:
            # Update: add new tokens on top of existing (since we only insert new turns)
            # Keep the highest-priority model (e.g. opus over haiku from subagents)
            existing_row = conn.execute(
                "SELECT model, topic, topic_source FROM sessions WHERE session_id = ?",
                (s["session_id"],)
            ).fetchone()
            existing_model = existing_row["model"]
            new_model = s["model"]
            if _model_priority(new_model) > _model_priority(existing_model):
                model_to_set = new_model
            else:
                model_to_set = existing_model

            # Topic provenance: custom-title always wins and always writes; an
            # ai-title only writes when there's no topic yet or the existing one
            # is itself AI-generated (never clobbers a user-set custom title).
            new_topic = s.get("topic")
            new_source = s.get("topic_source")
            existing_topic = existing_row["topic"]
            existing_source = existing_row["topic_source"]
            if new_source == "custom":
                topic_to_set, source_to_set = new_topic, "custom"
            elif new_source == "ai" and (not existing_topic or existing_source == "ai"):
                topic_to_set, source_to_set = new_topic, "ai"
            else:
                topic_to_set, source_to_set = existing_topic, existing_source

            conn.execute("""
                UPDATE sessions SET
                    last_timestamp = MAX(last_timestamp, ?),
                    total_input_tokens = total_input_tokens + ?,
                    total_output_tokens = total_output_tokens + ?,
                    total_cache_read = total_cache_read + ?,
                    total_cache_creation = total_cache_creation + ?,
                    turn_count = turn_count + ?,
                    model = ?,
                    topic = ?,
                    topic_source = ?
                WHERE session_id = ?
            """, (
                s["last_timestamp"],
                s["total_input_tokens"], s["total_output_tokens"],
                s["total_cache_read"], s["total_cache_creation"],
                s["turn_count"], model_to_set, topic_to_set, source_to_set,
                s["session_id"]
            ))


def insert_turns(conn, turns):
    conn.executemany("""
        INSERT OR IGNORE INTO turns
            (session_id, timestamp, model, input_tokens, output_tokens,
             cache_read_tokens, cache_creation_tokens,
             cache_creation_5m_tokens, cache_creation_1h_tokens,
             tool_name, cwd, message_id, duration_ms, stop_reason,
             service_tier, inference_geo, is_sidechain, is_compact_summary,
             tool_calls, is_subagent, agent_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, [
        (t["session_id"], t["timestamp"], t["model"],
         t["input_tokens"], t["output_tokens"],
         t["cache_read_tokens"], t["cache_creation_tokens"],
         t.get("cache_creation_5m_tokens", 0), t.get("cache_creation_1h_tokens", 0),
         t["tool_name"], t["cwd"], t.get("message_id", ""),
         t.get("duration_ms", 0), t.get("stop_reason", ""),
         t.get("service_tier", ""), t.get("inference_geo", ""),
         t.get("is_sidechain", 0), t.get("is_compact_summary", 0),
         t.get("tool_calls"), t.get("is_subagent", 0), t.get("agent_id"))
        for t in turns
    ])


def scan(projects_dir=None, projects_dirs=None, db_path=DB_PATH, verbose=True):
    conn = get_db(db_path)
    init_db(conn)

    if projects_dirs:
        dirs_to_scan = [Path(d) for d in projects_dirs]
    elif projects_dir:
        dirs_to_scan = [Path(projects_dir)]
    else:
        dirs_to_scan = DEFAULT_PROJECTS_DIRS

    jsonl_files = []
    any_dir_found = False
    for d in dirs_to_scan:
        if not d.exists():
            continue
        any_dir_found = True
        if verbose:
            print(f"Scanning {d} ...")
        jsonl_files.extend(glob.glob(str(d / "**" / "*.jsonl"), recursive=True))
    jsonl_files.sort()

    # One-time topic backfill for DBs whose sessions predate topic support: fill
    # topics from title records in already-processed transcripts that an
    # incremental scan would otherwise never revisit. Runs once, gated by the
    # schema_meta 'topic_backfill_done' marker. It runs before the main loop, so
    # on a fresh DB the sessions table is still empty and this no-ops; only DBs
    # with pre-existing untitled sessions do real work. Only set the marker once
    # a projects directory was actually found and scanned — otherwise (e.g. the
    # dir is transiently unmounted) a later scan retries instead of permanently
    # skipping the backfill.
    # Advisor turns are NEW rows, so no in-place UPDATE can add them, and the
    # incremental walk below skips files whose mtime is unchanged — an existing DB
    # would silently keep its pre-fix undercount forever. Clear processed_files
    # exactly once so the next walk re-reads everything. Safe to repeat: turn
    # inserts are INSERT OR IGNORE against the unique message_id index and session
    # totals are recomputed from `turns` afterwards; it is only slow, hence the marker.
    if any_dir_found and _meta_get(conn, "advisor_reparse_done") != "1":
        cleared = conn.execute("DELETE FROM processed_files").rowcount
        _meta_set(conn, "advisor_reparse_done", "1")
        conn.commit()
        if verbose and cleared:
            print(f"One-time re-parse of {cleared} transcript(s) to pick up advisor turns.")

    if any_dir_found and _meta_get(conn, "topic_backfill_done") != "1":
        filled = _backfill_topics(conn, jsonl_files)
        _meta_set(conn, "topic_backfill_done", "1")
        conn.commit()
        if verbose and filled:
            print(f"Backfilled topic for {filled} existing session(s).")

    new_files = 0
    updated_files = 0
    skipped_files = 0
    total_turns = 0
    total_sessions = set()

    for filepath in jsonl_files:
        try:
            mtime = os.path.getmtime(filepath)
        except OSError:
            continue

        row = conn.execute(
            "SELECT mtime, lines FROM processed_files WHERE path = ?",
            (filepath,)
        ).fetchone()

        if row and abs(row["mtime"] - mtime) < 0.01:
            skipped_files += 1
            continue

        is_new = row is None
        if verbose:
            status = "NEW" if is_new else "UPD"
            print(f"  [{status}] {filepath}")

        if is_new:
            # New file: full parse (single read, returns line count)
            session_metas, turns, agents, line_count = parse_jsonl_file(filepath)
            upsert_agents(conn, agents)

            if turns or session_metas:
                sessions = aggregate_sessions(session_metas, turns)
                upsert_sessions(conn, sessions)
                insert_turns(conn, turns)
                for s in sessions:
                    total_sessions.add(s["session_id"])
                total_turns += len(turns)
                new_files += 1

        else:
            # Updated file: read once, process only new lines
            old_lines = row["lines"] if row else 0
            seen_messages = {}  # message_id -> turn (dedup streaming)
            turns_no_id = []
            new_session_metas = {}
            agents = {}         # agent_id -> dispatch dict
            line_count = 0

            try:
                with open(filepath, encoding="utf-8", errors="replace") as f:
                    for line_count, line in enumerate(f, 1):
                        if line_count <= old_lines:
                            continue
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            record = json.loads(line)
                        except json.JSONDecodeError:
                            continue

                        rtype = record.get("type")
                        if rtype not in ("assistant", "user", "custom-title", "ai-title"):
                            continue

                        session_id = record.get("sessionId")
                        if not session_id:
                            continue

                        # Extract session title from title records
                        title = _extract_title(record)
                        if title:
                            if session_id not in new_session_metas:
                                new_session_metas[session_id] = {
                                    "session_id": session_id,
                                    "project_name": "unknown",
                                    "first_timestamp": "",
                                    "last_timestamp": "",
                                    "git_branch": "",
                                    "model": None,
                                    "topic": None,
                                    "topic_source": None,
                                }
                            meta = new_session_metas[session_id]
                            # custom-title always wins; ai-title only if no custom-title set
                            if rtype == "custom-title":
                                meta["topic"] = title
                                meta["topic_source"] = "custom"
                            elif rtype == "ai-title" and meta.get("topic_source") != "custom":
                                meta["topic"] = title
                                meta["topic_source"] = "ai"
                            continue

                        if rtype == "user":
                            dispatch = extract_agent_dispatch(record)
                            if dispatch is not None:
                                agents[dispatch["agent_id"]] = dispatch

                        timestamp = record.get("timestamp", "")
                        cwd = record.get("cwd", "")

                        # Track session metadata from new lines
                        if session_id not in new_session_metas:
                            new_session_metas[session_id] = {
                                "session_id": session_id,
                                "project_name": project_name_from_cwd(cwd),
                                "first_timestamp": timestamp,
                                "last_timestamp": timestamp,
                                "git_branch": record.get("gitBranch", ""),
                                "model": None,
                                "topic": None,
                                "topic_source": None,
                            }
                        else:
                            meta = new_session_metas[session_id]
                            if timestamp and (not meta["last_timestamp"] or timestamp > meta["last_timestamp"]):
                                meta["last_timestamp"] = timestamp
                            if timestamp and (not meta["first_timestamp"] or timestamp < meta["first_timestamp"]):
                                meta["first_timestamp"] = timestamp

                        if rtype == "assistant":
                            msg = record.get("message", {})
                            usage = msg.get("usage", {})
                            model = msg.get("model", "")
                            message_id = msg.get("id", "")

                            input_tokens = usage.get("input_tokens", 0) or 0
                            output_tokens = usage.get("output_tokens", 0) or 0
                            cache_read = usage.get("cache_read_input_tokens", 0) or 0
                            cache_creation = usage.get("cache_creation_input_tokens", 0) or 0
                            cache_creation_detail = usage.get("cache_creation", {})
                            if not isinstance(cache_creation_detail, dict):
                                cache_creation_detail = {}
                            cache_creation_5m = cache_creation_detail.get("ephemeral_5m_input_tokens", 0) or 0
                            cache_creation_1h = cache_creation_detail.get("ephemeral_1h_input_tokens", 0) or 0

                            if input_tokens + output_tokens + cache_read + cache_creation == 0:
                                continue

                            tool_calls = extract_tool_calls(msg.get("content", []))
                            tool_name = tool_calls[0]["name"] if tool_calls else None

                            if model:
                                new_session_metas[session_id]["model"] = model

                            turn = {
                                "session_id": session_id,
                                "timestamp": timestamp,
                                "model": model,
                                "input_tokens": input_tokens,
                                "output_tokens": output_tokens,
                                "cache_read_tokens": cache_read,
                                "cache_creation_tokens": cache_creation,
                                "cache_creation_5m_tokens": cache_creation_5m,
                                "cache_creation_1h_tokens": cache_creation_1h,
                                "tool_name": tool_name,
                                "cwd": cwd,
                                "message_id": message_id,
                                "duration_ms": record.get("durationMs", 0) or 0,
                                "stop_reason": record.get("stopReason") or msg.get("stop_reason") or "",
                                "service_tier": usage.get("service_tier", "") or "",
                                "inference_geo": usage.get("inference_geo", "") or "",
                                "is_sidechain": 1 if record.get("isSidechain") else 0,
                                "is_compact_summary": 1 if record.get("isCompactSummary") else 0,
                                "tool_calls": json.dumps(tool_calls) if tool_calls else None,
                                "is_subagent": 1 if is_subagent_record(record, filepath) else 0,
                                "agent_id": record_agent_id(record),
                            }

                            if message_id:
                                seen_messages[message_id] = turn
                            else:
                                turns_no_id.append(turn)
            except Exception as e:
                print(f"  Warning: {e}")

            if line_count <= old_lines:
                # File didn't grow (mtime changed but no new content)
                conn.execute("UPDATE processed_files SET mtime = ? WHERE path = ?",
                             (mtime, filepath))
                conn.commit()
                skipped_files += 1
                continue

            new_turns = turns_no_id + list(seen_messages.values())
            upsert_agents(conn, list(agents.values()))

            if new_turns or new_session_metas:
                sessions = aggregate_sessions(list(new_session_metas.values()), new_turns)
                upsert_sessions(conn, sessions)
                insert_turns(conn, new_turns)
                for s in sessions:
                    total_sessions.add(s["session_id"])
                total_turns += len(new_turns)
            updated_files += 1

        # Record file as processed (line_count already known from the single read)
        conn.execute("""
            INSERT OR REPLACE INTO processed_files (path, mtime, lines)
            VALUES (?, ?, ?)
        """, (filepath, mtime, line_count))
        conn.commit()

    # Recompute session totals from actual turns in DB.
    # This ensures correctness when INSERT OR IGNORE skips duplicate turns
    # but upsert_sessions had already added their tokens additively.
    if new_files or updated_files:
        conn.execute("""
            UPDATE sessions SET
                total_input_tokens = COALESCE((SELECT SUM(input_tokens) FROM turns WHERE turns.session_id = sessions.session_id), 0),
                total_output_tokens = COALESCE((SELECT SUM(output_tokens) FROM turns WHERE turns.session_id = sessions.session_id), 0),
                total_cache_read = COALESCE((SELECT SUM(cache_read_tokens) FROM turns WHERE turns.session_id = sessions.session_id), 0),
                total_cache_creation = COALESCE((SELECT SUM(cache_creation_tokens) FROM turns WHERE turns.session_id = sessions.session_id), 0),
                turn_count = COALESCE((SELECT COUNT(*) FROM turns WHERE turns.session_id = sessions.session_id), 0)
        """)
        conn.commit()

    if verbose:
        print(f"\nScan complete:")
        print(f"  New files:     {new_files}")
        print(f"  Updated files: {updated_files}")
        print(f"  Skipped files: {skipped_files}")
        print(f"  Turns added:   {total_turns}")
        print(f"  Sessions seen: {len(total_sessions)}")

    conn.close()
    return {"new": new_files, "updated": updated_files, "skipped": skipped_files,
            "turns": total_turns, "sessions": len(total_sessions)}


if __name__ == "__main__":
    import sys
    projects_dir = None
    for i, arg in enumerate(sys.argv[1:]):
        if arg == "--projects-dir" and i + 1 < len(sys.argv[1:]):
            projects_dir = Path(sys.argv[i + 2])
            break
    scan(projects_dir=projects_dir)
