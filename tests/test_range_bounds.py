"""Behavioural check on the dashboard's "Last N days" bounds.

The bounds are open-ended (`day >= start`, no end), so an off-by-one start makes
every "Last N Days" range span N+1 calendar days. Runs the real JS under node.
"""
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from dashboard import HTML_TEMPLATE as _SRC

HARNESS = """
let displayTZ = 'local';  // localISODate follows the display timezone
%(SRC)s
// Count the days a bound spans, inclusive of today, by walking it forward —
// independent of the arithmetic getRangeBounds used to build the start date.
function span(startISO) {
  const today = localISODate(new Date());
  const d = new Date(startISO + 'T12:00:00');
  let n = 0;
  while (localISODate(d) <= today) {
    if (++n > 400) throw new Error('runaway walk from ' + startISO);
    d.setDate(d.getDate() + 1);
  }
  return n;
}
// Both display timezones: the span is a count of the calendar days in that TZ,
// so switching to UTC must not change how many days a range covers.
for (const mode of ['local', 'utc']) {
  displayTZ = mode;
  for (const [range, want] of [['7d', 7], ['30d', 30], ['90d', 90]]) {
    const { start, end } = getRangeBounds(range);
    if (end !== null) throw new Error(range + ' must be open-ended, got end=' + end);
    const got = span(start);
    if (got !== want) throw new Error(mode + ' ' + range + ' spans ' + got + ' days, want ' + want);
  }
}
console.log('ok');
"""


def _range_fns(src):
    """The self-contained slice of the frontend that computes range bounds."""
    return src[src.index("function localISODate("):src.index("function readURLRange(")]


@unittest.skipUnless(shutil.which("node"), "node not installed")
class TestRangeBounds(unittest.TestCase):
    def test_last_n_days_spans_n_days(self):
        r = subprocess.run(["node", "-e", HARNESS.replace("%(SRC)s", _range_fns(_SRC))],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("ok", r.stdout)
