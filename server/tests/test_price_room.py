"""Auto scale keeps room around the last price: after NQ's 1900-point drop on Nov 5 the last price sat on the bottom
edge (the bars' range plus the scale's 20%/10% margins). With the last bar on screen and price near the bars' low or
high, room is added on that side, enough for a price at the very edge to sit about a fifth of the pane in (a third
shortened the candles too much). The room follows the bars' range, not the price: the first try moved the whole chart
on every tick. Back toward the middle no room is added; it turns on and off with a margin. Scrolled back, the bars fit
as before."""
import json
import subprocess

from test_hunt_web_chart import WEB, needs_node

APP = (WEB / "app.js").read_text()
FN = APP[APP.index("const PRICE_ROOM"):APP.index("\n}\n", APP.index("function roomAroundPrice(")) + 3]

JS = """
const L = { PriceScaleMode: { Normal: 0, Logarithmic: 1 } };
let mode = 0, vis = { from: 0, to: 99 };
const S = { bars: [] };
const chart = { timeScale: () => ({ getVisibleLogicalRange: () => vis }),
                priceScale: () => ({ options: () => ({ mode, scaleMargins: { top: 0.2, bottom: 0.1 } }) }) };
/*FN*/
// where the last price lands on the pane, 0 the bottom, 1 the top, with the scale's margins
const at = (r, p) => {
  const f = mode ? Math.log : (x) => x, lo = f(r.priceRange.minValue), hi = f(r.priceRange.maxValue);
  return 0.1 + (f(p) - lo) / (hi - lo) * 0.7;
};
const run = (lo, hi, last) => {
  S.bars = Array.from({ length: 100 }, (_, i) => ({ c: i === 99 ? last : lo }));
  const r = roomAroundPrice(() => ({ priceRange: { minValue: lo, maxValue: hi } }));
  return { lo: +r.priceRange.minValue.toFixed(2), hi: +r.priceRange.maxValue.toFixed(2), at: +at(r, last).toFixed(4) };
};
const out = { room: PRICE_ROOM };
out.drop = run(26520, 28460, 26520);                                        // at the bars' low: room below
out.ticks = [26560, 26700, 26850, 27000].map((p) => run(26520, 28460, p));  // up to 0.25 of the range: the same room
out.middle = run(26520, 28460, 27200);                                      // back toward the middle: none
out.line = run(26520, 28460, 26900);                                        // down again to 0.2, not past 0.15: none
roomSide = 0; out.rally = run(26520, 28460, 28460);                         // at the high: room above
roomSide = 0; vis = { from: 0, to: 80 }; out.back = run(26520, 28460, 26520);   // last bar off screen
vis = { from: 0, to: 99 }; roomSide = 0; mode = 1; out.log = run(100, 400, 100);
console.log(JSON.stringify(out));
"""


@needs_node
def test_the_room_follows_the_bars_range_and_not_every_tick():
    out = json.loads(subprocess.run(["node", "-e", JS.replace("/*FN*/", FN)], capture_output=True, text=True,
                                    check=True).stdout)
    k = out["room"]
    assert k == 0.22
    drop = out["drop"]
    assert drop["hi"] == 28460 and abs(drop["at"] - k) < 1e-3 and 26050 < drop["lo"] < 26200   # ~400 points below
    assert all((t["lo"], t["hi"]) == (drop["lo"], drop["hi"]) for t in out["ticks"])   # the scale holds still
    assert (out["middle"]["lo"], out["middle"]["hi"]) == (26520, 28460)
    assert (out["line"]["lo"], out["line"]["hi"]) == (26520, 28460)
    assert out["rally"]["lo"] == 26520 and abs(out["rally"]["at"] - (1 - k)) < 1e-3
    assert (out["back"]["lo"], out["back"]["hi"]) == (26520, 28460)
    assert abs(out["log"]["at"] - k) < 1e-3 and out["log"]["hi"] == 400
