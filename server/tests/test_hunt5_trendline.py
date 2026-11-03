"""Hunt 5, trend-line alerts: an alert fires where the line is drawn on the chart it was switched on from. The browser
records how that chart lays the line out (drawings.js lineLayout) and the server's level (drawings.line_price) and
the Alerts tab's "now" (drawingLinePrice) follow it: by the clock on W and M charts, in bars of the chart's length
(a bar a holiday closure cuts short counts whole) on seconds, minute and hour charts, straight in log price on a
logarithmic scale. hello gives the browser every holiday the server's calendar holds."""
import json
import shutil
import subprocess
from datetime import date, timedelta

import pytest

from chartsrv import drawings, sessions
from chartsrv import market as market_mod
from chartsrv.config import load_holidays
from test_redesign_trendline_alert import CONFIG, WEB, H, ct, hello_holidays, needs_node, open_bars, trend

WEEK = 7 * 24 * H

# drawings.js in node with a stand-in page (the line's settings panel), a time scale (bar i at x = 6i) and a price
# scale that is Regular, or Logarithmic as Lightweight Charts maps it (log10(p + 1e-4) + 4, then straight to pixels).
# switchOn(d): the panel's alert box ticked on this chart, giving the alert saved. drawn(d, times): the line rendered
# through the primitive, read back as prices at those times, and the label texts. now(params, times): the Alerts
# tab's level.
HARNESS = r"""
const vm = require('vm'), fs = require('fs'), IN = JSON.parse(fs.readFileSync(0, 'utf8'));
function el() {
  const ls = {}, kids = new Map();
  const e = { style: {}, dataset: {}, hidden: false, textContent: '', value: '', children: [],
    classList: { toggle() {}, add() {}, remove() {}, contains: () => false },
    addEventListener(t, f) { (ls[t] ||= []).push(f); }, fire(t, ev) { (ls[t] || []).forEach((f) => f(ev)); },
    querySelector(s) { if (!kids.has(s)) kids.set(s, el()); return kids.get(s); },
    querySelectorAll: () => [], replaceChildren(...c) { e.children = c; }, append(...c) { e.children.push(...c); },
    contains: () => false, closest() { return e; }, select() {} };
  return e;
}
const ctx = { localStorage: { getItem: () => null, setItem() {} }, window: { addEventListener() {}, innerWidth: 1200 },
  document: { addEventListener() {}, createElement: el, activeElement: null },
  Option: function (text, value) { this.text = text; this.value = value; },
  setTimeout, clearTimeout, structuredClone, console, requestAnimationFrame: () => 0 };
vm.runInNewContext(['press.js', 'studies.js', 'drawings.js'].map((f) => fs.readFileSync(IN.web + '/' + f, 'utf8')).join(';') +
  ';this.create=createDrawings;this.linePrice=drawingLinePrice;', ctx);
let bars = IN.bars || [], per = IN.per || 0, market = IN.market || null, byClock = !!IN.byClock, months = IN.months || 0, log = !!IN.log;
const SP = 6, PH = 1000, LO = IN.lo ?? 1, HI = IN.hi ?? 10000;
const f = (p) => (log ? Math.log10(p + 1e-4) + 4 : p), fInv = (v) => (log ? 10 ** (v - 4) - 1e-4 : v);
const priceToCoordinate = (p) => PH * (1 - (f(p) - f(LO)) / (f(HI) - f(LO)));
const coordinateToPrice = (y) => fInv(f(LO) + (1 - y / PH) * (f(HI) - f(LO)));
const timeScale = { logicalToCoordinate: (i) => i * SP, coordinateToLogical: (x) => x / SP, options: () => ({ barSpacing: SP }) };
const puts = [], ui = { bar: el(), edit: el(), paste: el() };
let prim = null;
const D = ctx.create({ chart: { timeScale: () => timeScale, priceScale: () => ({ width: () => 0 }), options: () => ({}), applyOptions() {} },
  series: { attachPrimitive(p) { prim = p; p.attached({ requestUpdate() {} }); }, priceToCoordinate, coordinateToPrice },
  el: el(), bars: () => bars, periodMs: () => per, market: () => market, byClock: () => byClock, months: () => months, logScale: () => log,
  tick: () => 0.01, fmt: String, symbol: () => 'ES', toast() {}, ask: async () => '', alerts: () => IN.alerts || [], ui,
  api: async (method, path, body) => { if (method === 'PUT') puts.push(JSON.parse(JSON.stringify(body))); return body ? { ...body } : { drawings: [] }; } });
function switchOn(d) {
  D.state.list.set(d.id, d);
  D.select(d.id);
  const box = el();
  Object.assign(box, { type: 'checkbox', checked: true });
  box.dataset.k = 'alertOn';
  ui.edit.fire('input', { target: box });
  return puts[puts.length - 1].alert;
}
function drawn(d, times, W = 1e5) {
  D.state.list.clear();
  D.state.list.set(d.id, d);
  const segs = [], texts = [];
  let at = null;
  const c2d = new Proxy({}, { get: (_, k) => (k === 'moveTo' ? (x, y) => { at = [x, y]; } : k === 'lineTo' ? (x, y) => { segs.push([at, [x, y]]); at = [x, y]; }
    : k === 'fillText' ? (s) => texts.push(s) : k === 'measureText' ? () => ({ width: 10 }) : () => {}), set: () => true });
  prim.views[0].renderer().draw({ useMediaCoordinateSpace: (fn) => fn({ context: c2d, mediaSize: { width: W, height: PH } }) });
  const [[xa, ya], [xb, yb]] = segs[0];
  return { at: times.map((t) => coordinateToPrice(ya + (yb - ya) * (D.timeToLogical(t) * SP - xa) / (xb - xa))), texts };
}
const now = (params, times) => times.map((t) => ctx.linePrice(params, t, IN.hol || null, IN.group || 'equity'));
const out = {};
"""


def run(body: str, **inp) -> dict:
    js = HARNESS + body + "\nconsole.log(JSON.stringify(out));"
    res = subprocess.run(["node", "-e", js], input=json.dumps({"web": str(WEB), **inp}), capture_output=True, text=True)
    assert res.returncode == 0, res.stderr
    return json.loads(res.stdout)


def off(d: dict) -> dict:
    """The drawing with its alert switched off, as it is before the box is ticked."""
    return {**d, "alert": {**d["alert"], "on": False}}


def saved(d: dict, alert: dict) -> dict:
    """The server's `line` alert params for the drawing saved with that alert."""
    return drawings.alert_params(drawings.clean({**d, "alert": alert}, d["id"]))


def weekly(first: date, n: int) -> list[dict]:
    """A 1W chart's bars: each week's, stamped at the 17:00 CT open before its Monday (bars.calendar_bucket)."""
    return [{"t": sessions.day_open(first + timedelta(weeks=i))} for i in range(n)]


@needs_node
def test_tl1_an_alert_switched_on_from_a_weekly_chart_keeps_to_its_line_on_the_clock():
    """tl-1: a 1W chart lays its bars a week apart on the clock and draws a line straight in them. Switched on there,
    the alert keeps to the clock, as before market time came in: the server's level and the Alerts tab's "now" are
    the line the chart draws, at each bar and inside the week, though Good Friday, Memorial Day, July 4 and Labor Day
    lie between (in market time it was 7 points off by October). From a D chart the alert keeps to market time."""
    sessions.set_calendar(load_holidays(CONFIG))
    weeks = weekly(date(2026, 3, 16), 33)             # 15 Mar to 25 Oct, all in daylight saving time
    d = off(trend(weeks[0]["t"], 6800.0, weeks[4]["t"], 6900.0, extendRight=True))   # +25 a week
    times = [w["t"] + k for w in weeks[1:] for k in (0, 3 * 24 * H + 22 * H)]          # each bar's open and Thursday 15:00
    out = run("out.alert = switchOn(IN.d); out.drawn = drawn(IN.d, IN.times).at;", bars=weeks, per=WEEK, byClock=True,
              d=d, times=times)
    assert out["alert"] == {"on": True, "direction": "cross", "repeat": False, "clock": True}
    params = saved(d, out["alert"])
    server = [drawings.line_price(params, t, "equity") for t in times]
    assert max(abs(x - y) for x, y in zip(out["drawn"], server)) < 1e-6
    shown = run("out.now = now(IN.p, IN.times);", p=params, times=times, hol=hello_holidays())["now"]
    assert shown == pytest.approx(server, abs=1e-9)
    market = {k: v for k, v in params.items() if k != "clock"}                          # the level before this fix
    assert max(abs(x - drawings.line_price(market, t, "equity")) for x, t in zip(out["drawn"], times)) > 7
    daily = run("out.alert = switchOn(IN.d);", bars=weeks, per=24 * H, d=d)["alert"]
    assert daily == {"on": True, "direction": "cross", "repeat": False}


@needs_node
def test_tl1_a_monthly_chart_and_the_old_alerts():
    """tl-1: an M chart's alert keeps to its month bars (round 7: exactly its line, test_hunt7_gapsmonthly); one
    switched on from an intraday chart afterwards goes back to that chart's bars; and alerts saved before keep their
    params, so nothing re-arms on the upgrade."""
    months = [{"t": sessions.day_open(date(2025 + (m - 1) // 12, (m - 1) % 12 + 1, 1))} for m in range(1, 23)]
    d = off(trend(months[0]["t"], 5000.0, months[6]["t"], 5600.0, extendRight=True))
    out = run("""out.month = switchOn(IN.d);
byClock = false; months = 0; per = 300000; market = { holidays: {}, group: 'equity' }; out.five = switchOn(IN.d);""",
              bars=months, per=31 * 24 * H, byClock=True, months=1, d=d)
    assert out["month"] == {"on": True, "direction": "cross", "repeat": False, "months": 1}
    assert out["five"] == {"on": True, "direction": "cross", "repeat": False, "per": 300_000}
    plain = drawings.clean(trend(ct(2026, 10, 15, 9), 6800.0, ct(2026, 10, 15, 10), 6802.0), "t")
    assert set(drawings.alert_params(plain)) == {"t1", "p1", "t2", "p2", "left", "right", "type", "direction", "drawing"}
    for bad in ({"clock": "yes"}, {"per": 7 * 60_000}, {"per": 0}, {"log": 1}, {"chart": "1W"}):
        with pytest.raises(ValueError):
            drawings.clean({**plain, "alert": {"on": True, **bad}}, "t")


@needs_node
def test_tl2_on_a_log_scale_the_alert_follows_the_line_drawn_straight_in_log_price():
    """tl-2: on a Logarithmic scale the chart draws a line straight in log price. Switched on there, the alert records
    it and its level grows by the same ratio: ES 1h, 6800 to 6830 a session later, is drawn at 7106.03 ten sessions
    on, where a straight price put the alert at 7100.00; a 1W line from 4000 to 5000 a year later is at 7812.50 two
    years on, not 7000. On a Regular scale nothing changes."""
    sessions.set_calendar(load_holidays(CONFIG))
    hol = hello_holidays()
    bars = open_bars(ct(2026, 10, 18, 17), ct(2026, 11, 6, 16), H, "equity")
    d = off(trend(ct(2026, 10, 19, 9), 6800.0, ct(2026, 10, 20, 9), 6830.0, extendRight=True))
    times = [ct(2026, 10, 19, 21), ct(2026, 10, 21, 9), ct(2026, 10, 26, 9), ct(2026, 11, 2, 9)]
    for log in (True, False):
        out = run("out.alert = switchOn(IN.d); out.drawn = drawn(IN.d, IN.times).at;", bars=bars, per=H, log=log,
                  market={"holidays": hol, "group": "equity"}, d=d, times=times, lo=6000, hi=8000)
        assert out["alert"].get("log", False) is log and out["alert"]["per"] == H
        params = saved(d, out["alert"])
        server = [drawings.line_price(params, t, "equity") for t in times]
        assert out["drawn"] == pytest.approx(server, abs=0.001)
        assert run("out.now = now(IN.p, IN.times);", p=params, times=times, hol=hol)["now"] == pytest.approx(server, abs=1e-9)
    assert server[-1] == pytest.approx(7100.0) and saved(d, {**out["alert"], "log": True}) != params
    assert drawings.line_price(saved(d, {"on": True, "per": H, "log": True}), times[-1], "equity") == pytest.approx(7106.03, abs=0.01)
    weeks = weekly(date(2021, 3, 1), 160)
    w = off(trend(weeks[0]["t"], 4000.0, weeks[52]["t"], 5000.0, extendRight=True))
    out = run("out.alert = switchOn(IN.d); out.drawn = drawn(IN.d, [IN.t]).at;", bars=weeks, per=WEEK, byClock=True,
              log=True, d=w, t=weeks[156]["t"], lo=3000, hi=9000)
    assert out["alert"] == {"on": True, "direction": "cross", "repeat": False, "clock": True, "log": True}
    lvl = drawings.line_price(saved(w, out["alert"]), weeks[156]["t"], "equity")
    assert lvl == pytest.approx(out["drawn"][0], abs=0.01) and lvl == pytest.approx(4000 * 1.25 ** 3, abs=0.01)
    through_zero = saved(off(trend(ct(2026, 10, 19, 9), -5.0, ct(2026, 10, 19, 10), 5.0)), {"on": True, "log": True})
    assert drawings.line_price(through_zero, ct(2026, 10, 19, 9, 30), "equity") == 0.0   # no log price: straight


@needs_node
def test_tl2_after_the_scale_is_flipped_the_line_says_which_scale_its_alert_keeps_to():
    """tl-2: the alert keeps the scale it was switched on over. Flipped to the other one, the chart draws the line
    elsewhere, and its bell says which scale the alert follows; on that scale it shows the plain bell."""
    d = trend(ct(2026, 10, 19, 9), 6800.0, ct(2026, 10, 20, 9), 6830.0, extendRight=True)
    bars = open_bars(ct(2026, 10, 18, 17), ct(2026, 10, 23, 16), H)
    alerts = [{"kind": "line", "enabled": True, "params": {"drawing": "t"}}]
    js = "out.t = [false, true].map((l) => { log = l; return drawn(IN.d, []).texts; });"
    as_log = run(js, bars=bars, per=H, d={**d, "alert": {**d["alert"], "log": True}}, alerts=alerts, lo=6000, hi=8000)["t"]
    as_regular = run(js, bars=bars, per=H, d=d, alerts=alerts, lo=6000, hi=8000)["t"]
    assert as_log == [["⏰ (log scale)"], ["⏰"]]
    assert as_regular == [["⏰"], ["⏰ (regular scale)"]]


SHORT_BARS = [   # group, bar length, the week's bars, the line's two points (09:00 and 11:00 the day before)
    ("energy", H, (2026, 11, 22), (2026, 11, 25)),          # Thanksgiving's 13:30 halt, the next day's 13:45 close
    ("metals", H, (2026, 11, 22), (2026, 11, 25)),
    ("equity", 20 * 60_000, (2026, 11, 22), (2026, 11, 25)),  # the 12:15 close leaves a 15-minute 12:00 bar
    ("equity", 30 * 60_000, (2026, 11, 22), (2026, 11, 25)),
    ("metals", 10 * 60_000, (2026, 11, 22), (2026, 11, 25)),
    ("equity", 2 * 60_000, (2026, 11, 22), (2026, 11, 25)),
    ("energy", 30 * 60_000, (2026, 12, 20), (2026, 12, 23)),  # Christmas Eve's 12:45 close
    ("energy", 15 * 60_000, (2026, 11, 22), (2026, 11, 25)),  # bars no closure cuts short: market time as before
]


@needs_node
@pytest.mark.parametrize("group,per,week,day", SHORT_BARS)
def test_tl3_bars_a_holiday_closure_cuts_short_count_whole(group, per, week, day):
    """tl-3: a bar a halt or early close cuts short (CL 1h at Thanksgiving's 13:30 halt: the 13:00 bar lasts half an
    hour) still takes a whole bar on the chart. An alert switched on from a seconds, minute or hour chart counts it
    whole too, so its level is the line that chart draws, at every bar and from bars it doesn't hold yet (a point
    placed past its last bar, one inside a closure). In market time it was 15 ticks off on CL 1h the next Monday."""
    sessions.set_calendar(load_holidays(CONFIG))
    hol = hello_holidays()
    start = ct(*week, 17)
    bars = open_bars(start, start + 9 * 24 * H - H, per, group)
    a, b = ct(*day, 9), ct(*day, 11)
    d = off(trend(a, 60.0, b, 60.4, extendRight=True))
    times = [x["t"] + k for x in bars[bars.index({"t": b}):][::max(1, H // per)] for k in (0, per // 3)]
    mk = {"holidays": hol, "group": group}
    out = run("""out.alert = switchOn(IN.d); out.drawn = drawn(IN.d, IN.times).at;
bars = IN.bars.slice(0, IN.cut); out.ahead = drawn(IN.d, IN.times).at;
out.inside = D.timeToLogical(IN.inside); bars = IN.bars; out.insideLoaded = D.timeToLogical(IN.inside);""",
              bars=bars, per=per, market=mk, d=d, times=times, cut=bars.index({"t": b}) + 2,
              inside=ct(*day, 13, 50) + 24 * H, lo=1, hi=200)
    assert out["alert"] == {"on": True, "direction": "cross", "repeat": False, "per": per}
    params = saved(d, out["alert"])
    server = [drawings.line_price(params, t, group) for t in times]
    assert max(abs(x - y) for x, y in zip(out["drawn"], server)) < 1e-6
    assert max(abs(x - y) for x, y in zip(out["ahead"], server)) < 1e-6       # bars still to come, past the last
    assert out["inside"] == pytest.approx(out["insideLoaded"], abs=1e-9)        # a point in a closure, either way
    shown = run("out.now = now(IN.p, IN.times);", p=params, times=times, hol=hol, group=group)["now"]
    assert shown == pytest.approx(server, abs=1e-9)
    market = {k: v for k, v in params.items() if k != "per"}
    gap = max(abs(x - drawings.line_price(market, t, group)) for x, t in zip(out["drawn"], times))
    bar = 0.4 * per / (2 * H)                                                    # the line's rise a bar
    assert gap > 0.2 * bar if per != 15 * 60_000 else gap < 1e-6


def test_tl4_hello_has_every_holiday_the_servers_calendar_holds(monkeypatch):
    """tl-4: the server's level counts the holidays of its whole calendar (two years ahead); hello gave the browser
    only 100 days of them, so a line with a point months ahead had another level in the Alerts tab and on the chart
    (6 ticks on 15 Dec with Presidents Day and Good Friday 2027 left out). hello now has them all, and it stays small."""
    sessions.set_calendar(load_holidays(CONFIG, today=date(2026, 10, 27)))
    monkeypatch.setattr(market_mod, "now_ms", lambda: ct(2026, 10, 27, 10))
    got = market_mod.Market.holiday_hours(None)
    assert got == hello_holidays() and max(got) >= "2028-12-25"
    assert len(json.dumps(got)) < 40_000
    if not shutil.which("node"):
        return
    d = trend(ct(2026, 10, 26, 9), 6800.0, ct(2027, 3, 31, 9), 7300.0, extendRight=True)
    params = drawings.alert_params(d)
    times = [ct(2026, 12, 15, 10), ct(2027, 1, 15, 10), ct(2027, 3, 1, 10)]
    shown = run("out.now = now(IN.p, IN.times);", p=params, times=times, hol=got)["now"]
    assert shown == pytest.approx([drawings.line_price(params, t, "equity") for t in times], abs=1e-9)
