"""Guard the guard: break each measures-ingest / quota-burn rule in a temp copy of
the repo and confirm tests/test_measures.py goes red for every one.

    python scripts/falsify_measures.py
"""
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# (file, original, mutant) - each must turn the suite red on its own.
MUTANTS = [
    ("scanner.py", "INSERT OR IGNORE INTO measures", "INSERT OR REPLACE INTO measures"),
    ("scanner.py", "            if seen and seen[0] == mtime:\n                continue\n", ""),
    ("scanner.py", "not isinstance(r.get(\"sessionId\"), str) or not isinstance(r.get(\"ts\"), str)", "False"),
    ("scanner.py", "    if measure_dir is None and not (projects_dir or projects_dirs):", "    if measure_dir is None:"),
    ("scanner.py", "    if measure_dir is None and not (projects_dir or projects_dirs):\n        measure_dir = MEASURE_DIR\n", ""),
    ("dashboard.py", "reset = pct < ppct or (presets is not None and utc(ts) >= presets)", "reset = pct < ppct"),
    ("dashboard.py", "reset = pct < ppct or (presets is not None and utc(ts) >= presets)", "reset = False"),
    ("dashboard.py", "s[kind] += pct if reset else pct - ppct", "s[kind] += pct - ppct if pct > ppct else 0"),
    ("dashboard.py", "            prev[kind] = (pct, utc(resets) if isinstance(resets, str) else None)\n", ""),
    ("dashboard.py", 'w["stale"] = bool(r and r <= now)', 'w["stale"] = False'),
    ("dashboard.py", "return d if d.tzinfo else d.replace(tzinfo=timezone.utc)", "return d"),
    ("dashboard.py", "        return None  # DB not scanned", "        pass  # DB not scanned"),
    ("dashboard.py", "rows = sorted((r for r in rows if utc(r[1])), key=lambda r: utc(r[1]))", "rows = list(rows)"),
    ("dashboard.py", "and _is_num(w.get(\"percentUsed\"))", "and w.get(\"percentUsed\") is not None"),
    ("scanner.py", "    if not isinstance(limits, list):", "    if limits is None:"),
    ("scanner.py", "return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None", "return v"),
]


def run(tree):
    return subprocess.run([sys.executable, "-m", "unittest", "tests.test_measures"],
                          cwd=tree, capture_output=True, text=True).returncode


def main():
    failures = 0
    with tempfile.TemporaryDirectory() as tmp:
        tree = Path(tmp) / "repo"
        shutil.copytree(ROOT, tree, ignore=shutil.ignore_patterns(".git", "__pycache__"))
        if run(tree) != 0:
            sys.exit("baseline suite is red; fix that first")
        for fname, orig, mutant in MUTANTS:
            path = tree / fname
            src = path.read_text(encoding="utf-8")
            if src.count(orig) != 1:
                print(f"STALE  {fname}: pattern not found once: {orig!r}")
                failures += 1
                continue
            path.write_text(src.replace(orig, mutant), encoding="utf-8")
            caught = run(tree) != 0
            path.write_text(src, encoding="utf-8")
            print(f"{'caught' if caught else 'MISSED'} {fname}: {orig.strip()[:60]}")
            failures += not caught
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
