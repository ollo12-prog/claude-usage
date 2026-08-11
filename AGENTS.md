# AGENTS.md

Guidance for any coding agent (Codex, Claude Code, etc.) working on this repository.

> **Naming note.** This project *analyzes* Claude Code's local usage logs, so "Claude Code" below always refers to that product (the source of the JSONL data) — not to the agent reading this file. The agent working on the codebase is referred to as "the coding agent" or just "you".

> **Do NOT load the `claude-api` skill in this repo.** Its trigger matches any mention of Claude / Anthropic / Opus / Sonnet / Haiku / `claude-*` — every task here — but the skill covers **calling** the Anthropic API, which this repo never does: stdlib only, zero SDK imports, no network calls to Anthropic. Loading it costs ~167k tokens of context and re-sends that block every turn. Its only relevant content is the pricing table, and the authority for that here is the `PRICING` dict in [cli.py](cli.py) (see "Cost calculation"); verify rates against [claude.com/pricing#api](https://claude.com/pricing#api) instead. [.claude/settings.json](.claude/settings.json) enforces this for Claude Code by denying `Skill(claude-api)`; this note is for agents that don't read that file.

## Project shape

Three Python files, stdlib only, no `pip install` step. Python 3.8+.

- [scanner.py](scanner.py) — parses Claude Code JSONL transcripts into a SQLite DB at `~/.claude/usage.db`.
- [cli.py](cli.py) — terminal commands (`scan` / `today` / `week` / `stats` / `dashboard`).
- [dashboard.py](dashboard.py) — single-file `http.server` serving an embedded HTML/JS SPA on `localhost:8080`.

Use `python` on Windows, `python3` on macOS/Linux. Both work the same.

## Common commands

```
python cli.py scan                  # incremental scan (fast on re-run)
python cli.py today                 # today's usage by model
python cli.py week                  # last 7 days, per-day + by-model
python cli.py stats                 # all-time stats
python cli.py dashboard                          # scan + open http://localhost:8080
python cli.py dashboard --host 0.0.0.0 --port 9000
python cli.py scan --projects-dir PATH           # scan a custom transcripts dir
# or via env vars:
HOST=0.0.0.0 PORT=9000 python cli.py dashboard

python -m unittest discover -s tests -v             # full test suite (CI runs this)
python -m unittest tests.test_scanner -v            # one file
python -m unittest tests.test_scanner.TestProjectNameFromCwd.test_windows_path  # one test
```

CI ([.github/workflows/tests.yml](.github/workflows/tests.yml)) runs the suite on Python 3.9 / 3.11 / 3.12 against `main` and PRs.

## Architecture

### Data flow

```
~/.claude/projects/**/*.jsonl   →   scanner.parse_jsonl_file()
~/Library/.../Xcode/...                  ↓
                              aggregate_sessions() → upsert_sessions() + insert_turns()
                                         ↓
                              ~/.claude/usage.db (SQLite)
                                         ↓
                  cli.py queries   ←──────────→   dashboard.py /api/data
```

By default the scanner walks both `~/.claude/projects/` and the Xcode coding-assistant directory; missing dirs are silently skipped. Override with `--projects-dir`.

### SQLite schema (created/migrated in [scanner.py](scanner.py) `init_db`)

- **`turns`** — one row per assistant API response. The source of truth for tokens and per-model attribution.
- **`sessions`** — aggregated per session (denormalized totals + chosen primary model).
- **`processed_files`** — incremental-scan tracking: `(path, mtime, lines)`. A file is skipped if its mtime matches; if it grew, only lines past the stored `lines` count are processed.

A conditional unique index on `turns.message_id` (where non-empty) lets `INSERT OR IGNORE` cheaply dedupe replays across rescans.

### Non-obvious invariants

These three things will bite you if you don't know them:

1. **Streaming dedupe by `message.id`.** Claude Code writes multiple JSONL records per API response — only the *last* one for a given `message.id` has the final usage tallies. `parse_jsonl_file` keeps the last record per `message_id` in a dict; earlier records are discarded. Don't sum across records of the same `message_id`.

2. **Session totals are recomputed from `turns` at the end of `scan()`.** During an incremental scan `upsert_sessions` adds tokens additively, but `insert_turns` uses `INSERT OR IGNORE` against the `message_id` unique index — so if a turn is a duplicate, session totals would drift. The final `UPDATE sessions ... (SELECT SUM ... FROM turns)` block reconciles this. Preserve it if you refactor scan logic.

3. **Session primary model priority is opus > sonnet > haiku** (`_model_priority` in [scanner.py](scanner.py)). This prevents a subagent's haiku turn from overwriting the session's opus model when an existing session is updated. Per-turn model is always honored in the `turns` table; only the session-level summary uses the priority.

### Cost calculation

Costs are computed **per turn** (each turn knows its own model), then summed. This is true in both the CLI ([cli.py](cli.py) `calc_cost`) and the dashboard JS ([dashboard.py](dashboard.py) `calcCost` inside the embedded HTML). Aggregating tokens first and applying a single price is wrong for sessions that span multiple models.

Pricing is duplicated in two places that **must stay in sync**:
- [cli.py](cli.py) `PRICING` dict (Python)
- [dashboard.py](dashboard.py) `PRICING` const inside `HTML_TEMPLATE` (JavaScript)

`get_pricing` / `getPricing` resolve in three tiers: exact match → `startswith` (handles date-suffixed model IDs like `claude-opus-4-7-20260215`) → substring fallback on `opus` / `sonnet` / `haiku`. Models that don't match any tier return `None` and are billed at $0 (shown as `n/a`) — this is intentional so local/3rd-party models (gemma, glm, etc.) aren't charged at Sonnet rates.

### Dashboard server

`http.server.BaseHTTPRequestHandler`-based, two endpoints:
- `GET /api/data` → JSON snapshot from `get_dashboard_data()`. Returns *all* history; client-side filters by date range and model.
- `POST /api/rescan` → deletes the DB and runs a full rescan. Passes `db_path` and `projects_dirs` explicitly so tests that monkey-patch the module globals work — scan's default arg values are frozen at def time, so don't switch to bare defaults.

The entire UI lives in `HTML_TEMPLATE` as a raw string. Chart.js is loaded from CDN.

Client-side UI state (collapsed sections, the 24h update-check cache) is kept in **`localStorage`**, which is keyed by the page's origin — so a stable port keeps that state across reloads.

## Testing notes

- `tests/test_scanner.py` and `tests/test_dashboard.py` use `tempfile.NamedTemporaryFile` for an isolated DB; never touch the user's real `~/.claude/usage.db`.
- The `/api/rescan` test patches `dashboard.DB_PATH` and `scanner.DEFAULT_PROJECTS_DIRS` — keep that contract intact (see commit 8ae2664).
- On Windows, `~/.claude/` may not exist on a fresh checkout. `get_db` creates the parent dir (`mkdir(parents=True, exist_ok=True)`) — don't remove that or `sqlite3.connect` will fail in CI / fresh installs (commit b5d1e15).

## Respecting contributors

When merging community PRs, **preserve the original author's commit so they get GitHub contributor credit**. In practice:

- `git fetch origin pull/<N>/head:pr-<N>` → `git merge --no-ff pr-<N>` keeps the author commit verbatim inside the merge bubble (don't squash, don't rebase-flatten).
- For a partial merge — when only one hunk of a PR is wanted — use `git cherry-pick <commit-sha>` against the specific upstream commit so authorship is preserved. If the diff isn't a clean single commit, fall back to applying the hunk manually + adding a `Co-Authored-By: Name <email>` trailer.
- Improvements that the bot/maintainer makes _on top_ of a contributor's work go in **separate follow-up commits**, not amendments to the contributor's commit.
- When closing duplicate PRs (multiple authors fixed the same bug independently), thank each one and explain that landing the earliest version isn't a quality judgment.

This applies to all agents working on this repo, not just Claude Code.

## Versioning and releases

[SemVer](https://semver.org/). **`CHANGELOG.md` is the canonical version reference**; tags are a projection of it, created automatically.

Work lands directly on `main`; this fork has no `DEV` branch.

The release flow:
1. **Do not put a version heading on `main` before you intend to release.** The tag workflow's regex matches `## vX.Y.Z` followed by whitespace, so a `— TBD` heading counts and would tag and publish an unfinished release the moment it lands. (`tests/test_version.py` also fails while a heading and `scanner.VERSION` disagree, so a stray heading can't pass CI either.) Let unreleased changes sit without a heading; add the heading in the release commit.
2. To release: add the `## vX.Y.Z — YYYY-MM-DD` heading with its bullets and set `scanner.VERSION` to the same version in the same commit. `scanner.VERSION` is what `cli.py --version` and the dashboard footer report.
3. Push `main`. [`.github/workflows/tag-on-merge.yml`](.github/workflows/tag-on-merge.yml) sees the new `## vX.Y.Z` heading in the push's CHANGELOG diff and creates the tag plus a GitHub Release with that section as the notes. There is no manual `git tag` step.
4. After the tag exists, run [`scripts/bump-formula.sh`](scripts/bump-formula.sh) to repoint the Homebrew formula at it, and push. See "Homebrew formula and self-referential SHA" below.

The workflow is idempotent: it no-ops if the tag or Release already exists, and on any push that doesn't add a new version heading. It only adds missing tags; it never reconciles existing ones.

### CHANGELOG conventions

The workflow trusts the CHANGELOG, so the format matters. Every new release entry follows this exact shape:

```
## vX.Y.Z — TBD

### <Area>

- One bullet per change, past tense, with a PR/issue link and `thanks @author` where the change came from a contributor (#73, thanks @thomasleveil)
```

Format rules the workflow relies on:

| Field | Required form | Why |
|---|---|---|
| Heading | `## vX.Y.Z` (exactly two `#`, the `v` prefix, three numeric components — strict semver) | The workflow regex `^## v[0-9]+\.[0-9]+\.[0-9]+([[:space:]]|$)` won't match anything else. `v1.1`, `v1.1.0-rc1`, `V1.1.0` are all silently ignored. |
| Separator | ` — ` (em-dash with surrounding spaces) | Cosmetic but consistent. The workflow ignores everything after the version. |
| Date | `TBD` while accumulating; replace with `YYYY-MM-DD` *in the release commit* | The workflow doesn't enforce dates — but a `TBD` heading that ships to main means the release looks unfinished forever. |
| Subsections | `### Dashboard`, `### Scanner`, `### Packaging`, `### Project / docs` — pick the smallest set that fits | Keeps the CHANGELOG scannable. |
| Bullets | Past tense, credit external contributors with `thanks @login`. Bare `#N` only for this fork's own PRs/issues; write upstream ones as `phuryn/claude-usage#N` | A bare `#N` resolves against this repo, so an upstream number silently links to something unrelated. |

**The TBD → date rule is the only step a human must remember at release time.** If you forget, the workflow still tags correctly, but the CHANGELOG entry on main reads `## v1.1.3 — TBD` forever. Fix-up commit can correct it, but it'll feel sloppy.

Patch (`Z`) is the default. Bump minor (`Y`) when a non-breaking user-visible feature lands, major (`X`) only on breaking changes. Nothing automates the choice; whoever writes the CHANGELOG heading decides.

### Homebrew formula and self-referential SHA

The Homebrew formula at `Formula/claude-usage.rb` lives inside the repo it installs. Its `url` therefore cannot point at its own release: the tarball would contain the formula carrying that `sha256`, which is self-referential and uncomputable. The rule is that the formula points at an **already-frozen tag** — never the release it ships in — so brew tracks one release behind by design.

Brew reads the formula from the tap's default branch (`main`) HEAD, never from a tag. So the bump is a normal `main` commit that reaches brew users on the next push, and the pin only ever needs to move at release boundaries.

**Automate the bump; never hand-edit the three pinned lines.** [`scripts/bump-formula.sh`](scripts/bump-formula.sh) fetches a released tag's tarball, computes its `sha256`, and rewrites the `url` / `version` / `sha256` lines, leaving `head`, `homepage`, and comments alone. With no argument it targets the latest `v*` tag on origin — run it **after** a release tags and that is the just-frozen release, exactly what the rule requires. `REPO_SLUG` is `ollo12-prog/claude-usage`, so the formula serves this fork's code.

