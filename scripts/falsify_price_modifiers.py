"""Guard the guard: break each fast-mode / US-geo pricing rule in a temp copy of
the repo and confirm tests/test_price_modifiers.py goes red for every one.

    python scripts/falsify_price_modifiers.py
"""
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# (file, original, mutant) - each must turn the suite red on its own.
MUTANTS = [
    ("scanner.py", "THEN 2.0 ELSE 1.0 END\"", "THEN 1.0 ELSE 1.0 END\""),
    ("scanner.py", "THEN 1.1 ELSE 1.0 END)\"", "THEN 1.0 ELSE 1.0 END)\""),
    ("scanner.py", '"speed": usage.get("speed", "") or "",', '"speed": "",'),
    ("scanner.py", '                            speed="",\n', ""),
    ("scanner.py", "AND (speed IS NULL OR speed != 'fast')\",", "AND 0\","),
    ("cli.py", "calc_cost(r[\"model\"], r[\"x_inp\"]", "0 * calc_cost(r[\"model\"], r[\"x_inp\"]"),
    ("dashboard.py", "    return x if any(x.values()) else None", "    return None"),
    ("dashboard.py", "  if (!x) return c;", "  return c;"),
    ("dashboard.py", "for (const k of Object.keys(a.x)) a.x[k] += b.x[k] || 0;", ""),
    ("dashboard.py", "    addSurcharge(m, r);\n", ""),
    ("dashboard.py", 'xm_expr = xm_expr.replace(col, "NULL")', 'xm_expr = "0"'),
]


def run(tree):
    return subprocess.run([sys.executable, "-m", "unittest", "tests.test_price_modifiers"],
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
