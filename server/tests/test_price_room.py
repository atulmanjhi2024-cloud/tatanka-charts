"""Auto scale keeps room around the last price: after NQ's 1900-point drop on Nov 5 the last price sat on the bottom
edge (the bars' range plus the scale's 20%/10% margins). With the last bar on screen it stays at least about a fifth
of the way in from the top and bottom (a third shortened the candles too much); the range grows only on the side price presses against. Scrolled back, the bars fit
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
const out = { drop: run(26520, 28460, 26525), rally: run(26520, 28460, 28455), middle: run(26520, 28460, 27500) };
vis = { from: 0, to: 80 }; out.back = run(26520, 28460, 26525);
vis = { from: 0, to: 99 }; mode = 1; out.log = run(100, 400, 101);
out.room = PRICE_ROOM;
console.log(JSON.stringify(out));
"""


@needs_node
def test_the_last_price_keeps_room_around_it():
    out = json.loads(subprocess.run(["node", "-e", JS.replace("/*FN*/", FN)], capture_output=True, text=True,
                                    check=True).stdout)
    k = out["room"]
    assert k == 0.22
    assert out["drop"]["hi"] == 28460 and abs(out["drop"]["at"] - k) < 1e-3       # room below, the top as it was
    assert 26050 < out["drop"]["lo"] < 26200                                       # about 400 points under 26525
    assert out["rally"]["lo"] == 26520 and abs(out["rally"]["at"] - (1 - k)) < 1e-3   # room above
    assert out["middle"] == {"lo": 26520, "hi": 28460, "at": out["middle"]["at"]}   # already inside: unchanged
    assert (out["back"]["lo"], out["back"]["hi"]) == (26520, 28460)                  # last bar off screen: unchanged
    assert abs(out["log"]["at"] - k) < 1e-3 and out["log"]["hi"] == 400          # log scale: in log space
