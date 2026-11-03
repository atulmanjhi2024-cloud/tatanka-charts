"""AAVP on an intraday chart: NinjaTrader's minute series loads the chart's days, so its profile starts no earlier than
the chart's first bar. This app's holds the anchor's whole stretch (a Year from Jan 1); its left edge was counted back
from the first bar by the bar length over every night and weekend, far off the chart, and the histogram (30% of that
span) with it: nothing showed. It is drawn from the first bar now, as in NinjaTrader; a profile starting inside the
loaded bars is where it was."""
import json
import subprocess

from test_hunt_web_chart import WEB, needs_node

STUDIES = (WEB / "studies.js").read_text()


def part(name: str) -> str:
    start = STUDIES.index(f"function {name}(")
    return STUDIES[start:STUDIES.index("\n}\n", start) + 3]


JS = """
/*STUDIES*/
const M = 60e3, P = 30 * M, ct = (s) => Date.parse(s + '-06:00');
// a 30-minute chart's 3000 bars, the market's hours only (23 a day, 5 days a week), up to Mon Nov 2 2026 16:00 CT
const bars = [];
for (let t = ct('2026-11-02T15:30:00'); bars.length < 3000; t -= P) {
  const d = new Date(t - 6 * 3600e3), wd = d.getUTCDay(), h = d.getUTCHours();
  if (wd === 6 || (wd === 0 && h < 17) || (wd === 5 && h >= 16) || h === 16) continue;
  bars.unshift({ t, o: 7000, h: 7001, l: 6999, c: 7000, v: 10 });
}
const n = bars.length, sp = 0.5;   // zoomed all the way out: every bar on screen
const ts = { getVisibleLogicalRange: () => ({ from: 0, to: n - 1 }), logicalToCoordinate: (i) => i * sp,
             options: () => ({ barSpacing: sp }) };
const chart = { timeScale: () => ts }, series = { coordinateToPrice: (y) => 8000 - y, priceToCoordinate: (p) => 8000 - p };
let drawn = null;
const vpBox = () => {}, vpLevel = () => {};
const vpMinuteRows = (ctx, p, tick, o, v, left, right, width) => { drawn = { left, right, width }; };
const set = { WidthPercent: 30, PinToVisibleEdge: false, EdgePaddingPixels: 12, ShowProfile: true, Placement: 'Left' };
const W = n * sp;
const draw = (start) => {
  drawn = null;
  const v = vpView(chart, series, bars, P, 1000);
  drawAavp(null, W, 1000, v, { set, bars, d: { profile: { start, last: bars[n - 1].t + P } } });
  return drawn && { left: Math.round(drawn.left), right: Math.round(drawn.right), width: Math.round(drawn.width) };
};
console.log(JSON.stringify({ year: draw(ct('2025-12-31T17:00:00')), inside: draw(bars[1000].t), W, first: bars[0].t }));
"""


@needs_node
def test_a_year_anchor_before_the_first_bar_is_drawn_from_the_first_bar():
    js = JS.replace("/*STUDIES*/", "\n".join(part(f) for f in ("barIndexFrom", "barIndexTo", "logicalX", "vpView",
                                                                "vpLayout", "drawAavp")))
    out = json.loads(subprocess.run(["node", "-e", js], capture_output=True, text=True, check=True).stdout)
    year, inside, w = out["year"], out["inside"], out["W"]
    # from the first bar to the last: 30% of the chart, on screen (before: left at about -9000 px, all of it off it)
    assert year["left"] == 0 and year["right"] == w and year["width"] == round(0.3 * w)
    assert inside["left"] == 500 and inside["width"] == round(0.3 * (inside["right"] - 500))   # where it was
