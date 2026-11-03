"""Second review of the bug-hunt fixes, HTF EMA: in bar replay the last bar shown takes the HTF bars to its own close,
not to the present; a 2D, 3D, week or month bar that is over keeps its value when the next one opens; the forming bar
follows the HTF bars that close inside it (the server is asked again)."""
import json
import shutil
from datetime import datetime
from functools import lru_cache

import pytest

from amt_parity import synth
from chartsrv import htfema
from chartsrv.amt_runner import epoch_ms
from chartsrv.bars import make_series, parse_spec
from chartsrv.replay import next_trading_session, trading_session
from chartsrv.tpo_service import chart_bars
from htfema_parity import WEB, js_values
from test_htfema import client  # noqa: F401
from test_hunt_web_chart import run_app

needs_node = pytest.mark.skipif(not shutil.which("node"), reason="needs node")
EXACT = htfema.settings({"Mode": "ExactHTF"})


@lru_cache(maxsize=1)
def minutes():
    return tuple(synth(75, 3, datetime(2026, 11, 1, 19, 0)))   # to Sunday 2026-11-01 19:00 CT


def chart(spec, mins, now):
    """The chart's bars, its series and the server's payload as of `now` (ExactHTF), as /api/htfema builds it."""
    sp = parse_spec(spec)
    series = make_series(sp, 0.25, 2, 10_000)
    for m in mins:
        series.on_bar(m)
    bars = [{"t": b.t, "c": b.c} for b in series.bars]
    chart_minutes = sp.size // 60 if sp.cal not in ("W", "M") else None
    d = htfema.compute(EXACT, mins, chart_minutes, htfema.GetScaledLength(EXACT, *htfema.chart_period_spec(sp)),
                       bars[0]["t"], now)
    return sp, series, bars, d


# ---- bar replay: /api/htfema runs on the real clock, the chart's bars stop at the replay clock -----------------------

@needs_node
@pytest.mark.parametrize("spec", ["2D", "3D", "1W", "1M", "t3"])
def test_htf_ema_in_bar_replay_the_last_bar_shows_its_own_close_not_the_present(spec):
    """Replay knows when the next bar opens: the last bar shown takes the HTF bars that closed before then (one
    closing with it runs after it, as on a tie), the value it keeps once that bar is shown. t3: a tick-style chart."""
    m1 = minutes()
    now = m1[-1].t + 60_000
    if spec == "t3":
        bars = [{"t": b[0], "c": b[6]} for b in chart_bars(m1, 3)]
        d = htfema.compute(EXACT, m1, None, htfema.GetScaledLength(EXACT, "Tick", 3), bars[0]["t"], now)
        period, cal = 0, False
    else:
        sp, _, bars, d = chart(spec, m1, now)
        period, cal = sp.size * 1000, True
    k = len(bars) // 2   # the replay clock half way through
    shown = js_values(bars[:k], d, period, cal, after=bars[k]["t"])[-1]
    stepped = js_values(bars[:k + 1], d, period, cal)[k - 1]
    assert shown == stepped != d["htf"][-1][1]   # not the present's


# ---- a calendar bar that is over, still the chart's last (a weekend, 16:00-17:00) ------------------------------------

@needs_node
@pytest.mark.parametrize("spec, at, over", [
    ("1W", "2026-10-24 12:00", True), ("2D", "2026-10-24 12:00", True), ("3D", "2026-10-24 12:00", True),
    ("1M", "2026-10-31 12:00", True), ("2D", "2026-10-27 16:30", True),
    ("2D", "2026-10-31 12:00", False), ("3D", "2026-10-27 16:30", False), ("1W", "2026-10-27 16:30", False)])
def test_htf_ema_calendar_bar_that_is_over_keeps_its_value_when_the_next_one_opens(spec, at, over):
    """Saturday: the week is over, and its last HTF bar (Friday 15:00-16:00) runs after it, as on a tie. The server
    says when the next bar opens, and the week shows on Saturday what it shows once that bar is there. A bar that
    goes on after the break (2D Friday and Monday) still takes every closed HTF bar."""
    now = epoch_ms(datetime.fromisoformat(at))
    m1 = [m for m in minutes() if m.t < now]
    sp, series, bars, d = chart(spec, m1, now)
    nxt = htfema.next_open(series.bucket, now)
    got = js_values(bars, d, sp.size * 1000, True, after=nxt)
    if not over:
        assert nxt is None and got[-1] == d["htf"][-1][1]
        return
    later = next_trading_session(trading_session(now)) + 3_600_000 + 60_000   # an hour into the next bar
    _, series2, bars2, d2 = chart(spec, [m for m in minutes() if m.t < later - 60_000], later)
    assert bars2[-1]["t"] == nxt and len(bars2) == len(bars) + 1
    then = js_values(bars2, d2, sp.size * 1000, True, after=htfema.next_open(series2.bucket, later))
    assert got[-1] == then[-2] != d["htf"][-1][1]


def test_htfema_endpoint_says_when_the_next_calendar_bar_opens(client, monkeypatch):  # noqa: F811
    """/api/htfema on a Saturday: a week chart's next bar opens Sunday 17:00; minute and day charts get none."""
    from chartsrv import app
    sat = epoch_ms(datetime(2026, 10, 24, 12, 0))
    monkeypatch.setattr(app, "now_ms", lambda: sat)
    q = {"symbol": "ES", "start": sat - 30 * 86_400_000, "Mode": "ExactHTF"}
    got = {spec: client.get("/api/htfema", params={**q, "spec": spec}).json()["next"] for spec in ("1W", "1M", "5m", "1D")}
    assert got == {"1W": epoch_ms(datetime(2026, 10, 25, 17, 0)), "1M": None, "5m": None, "1D": None}


# ---- the browser: what it passes in replay and live, and when it asks again ------------------------------------------

DRAW_JS = """
const vm = require('vm'), fs = require('fs'), ctx = {};
vm.runInNewContext(fs.readFileSync(%s, 'utf8') + ';this.f=htfEmaValues;', ctx);
const htfEmaValues = ctx.f;
const hemaLine = { applyOptions() {}, setData() {} }, hemaStyle = () => ({}), renderHemaInfo = () => {};
const timeOf = (b) => b.t, isTime = () => S.spec !== '2000t', specSeconds = () => 604800;
// weeks open at 0, 100, 200 and 300 (ms here); HTF bars close at 30, 60 and 90 (the week's last session's), ...
const htf = [30, 60, 90, 130, 160, 190, 230, 260, 290].map((c, i) => [c, i + 1]);
const S = { st: { hema: true }, spec: '1W', bars: [], replay: null, hema: { scaled: 1, len: 21, mode: 'ExactHTF', htf, ends: [] } };
/*APP*/
const all = [0, 100, 200, 300].map((t) => ({ t, c: 1 }));
const run = (spec, n, replay, next) => {
  Object.assign(S, { spec, bars: all.slice(0, n), replay });
  S.hema.next = next;
  hemaDraw();
  return S.hemaVals.at(-1);
};
console.log(JSON.stringify({
  replay: run('1W', 2, { data: all, idx: 2, at: 200, nextAt: null }, 300),         // the server's next is the present's
  replayMore: run('1W', 2, { data: all.slice(0, 2), idx: 2, at: 190, nextAt: 200 }, null),   // the next stretch's first
  tickReplay: run('2000t', 2, { data: all, idx: 2, at: 250, nextAt: null }, null),
  over: run('1W', 2, null, 200),
  forming: run('1W', 2, null, null),
}));"""


@needs_node
def test_htf_ema_draw_takes_the_next_bar_from_replay_or_the_server():
    js = DRAW_JS % json.dumps(str(WEB / "studies.js"))
    out = run_app(["hemaPeriodMs", "hemaCal", "hemaAfter", "hemaPoint", "hemaDraw"], js)
    # the week opening at 100: HTF bars to 160 (190 closes with it); a tick bar closes when the next opens (200)
    assert out == {"replay": 5, "replayMore": 5, "tickReplay": 6, "over": 5, "forming": 9}


def test_htf_ema_payload_says_when_the_forming_htf_bar_closes():
    m1 = synth(10, 2, datetime(2026, 10, 16, 12, 7))   # to Friday 12:06
    d = htfema.compute(EXACT, m1, 5, 252, m1[-600].t, m1[-1].t + 60_000)
    assert d["htf"][-1][0] == epoch_ms(datetime(2026, 10, 16, 12, 0))   # the 11:00 hour, closed
    assert d["due"] == epoch_ms(datetime(2026, 10, 16, 13, 0))           # the 12:00 hour closes at 13:00
    assert htfema.compute(EXACT, m1, 5, 252, m1[-600].t, epoch_ms(datetime(2026, 10, 17, 12, 0)))["due"] is None   # Saturday


TICK_JS = """
let clock = 1.8e12;
Date.now = () => clock;
const asked = [];
const S = { st: { hema: true }, symbol: 'ES', spec: '2D', bars: [{ t: clock - 86400e3, c: 1 }], instruments: { ES: {} }, replay: null,
  hemaSet: { HtfPeriodType: 'Minute', HtfPeriod: 60, EmaLength: 21, Mode: 'ExactHTF' }, hema: null };
const indReady = () => true, hemaDraw = () => {}, hemaLive = () => {};
let due = clock + 3600e3;
const api = async (method, path) => { asked.push(path); return { mode: S.hemaSet.Mode, htf: [], due }; };
/*APP*/
const at = async (t, closed = false) => { clock = t; hemaTick(closed); await new Promise((r) => setTimeout(r, 5)); return asked.length; };
(async () => {
  await hemaRequest();   // the history
  const t0 = clock, out = {};
  out.inside = await at(t0 + 1800e3);             // the HTF bar still forming
  out.htfClosed = await at(t0 + 3601e3);          // it closed inside the forming 2D bar: asked again
  out.minute = await at(t0 + 3620e3);             // the server's clock behind (the same due): not more than once a minute
  due = t0 + 7200e3;
  out.later = await at(t0 + 3662e3);
  out.again = await at(t0 + 3800e3);              // the next HTF close is the new due
  S.hema.due = t0;
  S.hema.mode = 'SmoothScaled';
  out.smooth = await at(t0 + 3900e3);             // Smooth draws this chart's own EMA: nothing to ask
  S.hema.mode = 'ExactHTF';
  S.replay = {};
  out.replay = await at(t0 + 4000e3);
  S.replay = null;
  out.closed = await at(t0 + 4001e3, true);       // a chart bar closed: as before
  console.log(JSON.stringify(out));
})();"""


@needs_node
def test_htf_ema_forming_bar_asks_again_when_an_htf_bar_closes_inside_it():
    out = run_app(["hemaBusy", "hemaRequest", "hemaTick"], TICK_JS)
    assert out == {"inside": 1, "htfClosed": 2, "minute": 2, "later": 3, "again": 3, "smooth": 3, "replay": 3, "closed": 4}
