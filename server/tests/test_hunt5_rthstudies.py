"""Round 5, rth-studies: Daily SMA x4's daily bars are the 1D chart's (a holiday halt's session folded into the next
trade date, on an RTH chart too), a halt session's forming day runs to the close of the date it counts toward, and gap
alerts keep to the RTH chart's hours when they were switched on from one."""
import json
import shutil
import subprocess
from datetime import datetime
from pathlib import Path

import pytest

from chartsrv import dsma, sessions
from chartsrv.bars import make_series, parse_spec
from chartsrv.config import ROOT, load_holidays
from chartsrv.models import Bar
from chartsrv.sessions import CT
from chartsrv.studies import RthClock

WEB = Path(__file__).resolve().parents[2] / "web"
needs_node = pytest.mark.skipif(not shutil.which("node"), reason="needs node")
MIN = 60_000
ROOTS = {"ES": (0.25, "equity", ("08:30", "15:00")), "CL": (0.01, "energy", ("08:00", "13:30"))}


def ms(*a) -> int:
    return int(datetime(*a, tzinfo=CT).timestamp() * 1000)


def minute_bars(start: int, end: int, group: str, step=30) -> list[Bar]:
    """A bar every `step` minutes the market is open, each close its own (so every day's close says which it is)."""
    out = []
    for t in range(start, end, step * MIN):
        if sessions.is_open(t, group):
            p = 100 + (t // MIN) % 997 * 0.25
            out.append(Bar(t=t, o=p, h=p, l=p, c=p, v=1))
    return out


def chart_1d(m1, root, rth):
    tick, group, hours = ROOTS[root]
    series = make_series(parse_spec("1D"), tick, 2, 1000, hours, studies=False, rth_only=rth, group=group)
    for b in m1:
        series.on_bar(b)
    return [{"t": b.t, "c": b.c} for b in series.bars]


def dsma_days(m1, root, rth, now, start=0):
    _, group, hours = ROOTS[root]
    stored = [(b.t, b.c) for b in m1]
    dsma._cache.clear()
    return dsma.compute("hunt5", root, group, hours if rth else None, start, 8, now, [], now,
                        lambda db, sym, a, b: [x for x in stored if a <= x[0] < b])


JS = """
const vm = require('vm'), fs = require('fs'), ctx = {};
vm.runInNewContext(fs.readFileSync(%s, 'utf8') + ';this.vals=dailySmaValues;this.prep=dailySmaPrep;', ctx);
const a = JSON.parse(fs.readFileSync(0, 'utf8')), n = a.bars.length, p = ctx.prep(a.d.days, [3, 1, 1, 1]);
const draw = ctx.vals(a.bars, a.d, p, { cal: true, periodMs: 0, price: a.price });       // dsmaDraw
const live = ctx.vals(a.bars.slice(-1), a.d, p, { cal: true, periodMs: 0, price: a.price });   // dsmaLive
console.log(JSON.stringify({ sma: draw[0].map((x) => (Number.isNaN(x) ? null : x)), developing: draw.developing,
  live: live[0][0], liveDeveloping: live.developing }));""" % json.dumps(str(WEB / "studies.js"))


def plotted(bars, d, price):
    """What the chart page plots for a 3 SMA on these 1D bars (dsmaDraw, then dsmaLive with the live price)."""
    return json.loads(subprocess.run(["node", "-e", JS], input=json.dumps({"bars": bars, "d": d, "price": price}),
                                     capture_output=True, text=True, check=True).stdout)


def sma3(closes):
    return sum(closes[-3:]) / 3


# ---- rs-1: the RTH daily bars fold a holiday halt as the RTH 1D chart does --------------------------------------------

@pytest.mark.parametrize("rth", [False, True], ids=["eth", "rth"])
@pytest.mark.parametrize("root", ["ES", "CL"])
def test_dsma_daily_bars_are_the_1d_chart_s_bars_across_holidays(root, rth):
    """Thanksgiving and MLK Day (halts: CL's at its 13:30 RTH close, a full regular session), the early closes and closed
    days between: one DSMA day per 1D chart bar, with its close, ending at the close of the date the bar stands for."""
    sessions.set_calendar(load_holidays(ROOT / "config"))
    _, group, hours = ROOTS[root]
    now = ms(2026, 1, 24)   # a Saturday: every day complete
    m1 = minute_bars(ms(2025, 11, 16, 17), now, group)
    bars, d = chart_1d(m1, root, rth), dsma_days(m1, root, rth, now)
    days = d["days"][-len(bars):]
    assert len(d["days"]) == len(bars) and not d["forming"]
    assert [x[2] for x in days] == [b["c"] for b in bars]
    clock = RthClock(hours)
    want = [sessions.first_closure(*clock.window(sessions.to_ct(b["t"])), group) if rth else
            sessions.first_closure(b["t"], dsma._session_end(b["t"]), group) for b in bars]
    assert [x[1] for x in days] == want
    assert all(x[0] <= b["t"] < x[1] for x, b in zip(days, bars))
    mlk = [sessions.to_ct(x[0]).date().isoformat() for x in days if sessions.to_ct(x[1]).date().isoformat() == "2026-01-20"]
    assert mlk == (["2026-01-19"] if rth else ["2026-01-18"])   # Monday's session in Tuesday's day, opening Monday


@needs_node
@pytest.mark.parametrize("root", ["ES", "CL"])
def test_rth_1d_chart_plots_the_sma_of_its_own_closes_around_a_halt(root):
    """The RTH 1D chart folds MLK Day into Tuesday's bar: the Friday bar's 3 SMA is Wed-Thu-Fri's (not one with Monday's
    close in it) and every bar's is the SMA of the chart's own last three closes, as NinjaTrader's DSMA on a 1D chart."""
    sessions.set_calendar(load_holidays(ROOT / "config"))
    _, group, _ = ROOTS[root]
    now = ms(2026, 1, 24)
    m1 = minute_bars(ms(2025, 12, 28, 17), now, group)
    bars = chart_1d(m1, root, True)
    out = plotted(bars, dsma_days(m1, root, True, now, bars[0]["t"]), bars[-1]["c"])
    closes = [b["c"] for b in bars]
    stamps = [sessions.to_ct(b["t"]).strftime("%a %d") for b in bars]
    assert "Mon 19" not in stamps and "Tue 20" in stamps
    for i in range(2, len(bars)):
        assert out["sma"][i] == pytest.approx(sma3(closes[:i + 1])), stamps[i]


# ---- rs-3: a halt session's forming day runs to the close of the trade date it counts toward --------------------------

@needs_node
@pytest.mark.parametrize("rth", [False, True], ids=["eth", "rth"])
def test_halt_session_forming_1d_bar_develops_and_the_bar_before_keeps_its_own(rth):
    """MLK Day 2026, 10:00 CT, an ES 1D chart: the forming bar (stamped at Tuesday's open) shows the developing SMA with
    the live price, at load and on each tick; the bar before (Friday's trade date) its own, not the forming day's."""
    sessions.set_calendar(load_holidays(ROOT / "config"))
    now = ms(2026, 1, 19, 10)
    m1 = minute_bars(ms(2025, 12, 28, 17), now, "equity")
    bars = chart_1d(m1, "ES", rth)
    d = dsma_days(m1, "ES", rth, now, bars[0]["t"])
    assert d["forming"] and d["days"][-1][1] == (ms(2026, 1, 20, 15) if rth else ms(2026, 1, 20, 16))
    closes = [b["c"] for b in bars]
    for price in (closes[-1], closes[-1] + 50):
        out = plotted(bars, d, price)
        assert out["developing"] and out["liveDeveloping"]
        assert out["sma"][-1] == out["live"] == pytest.approx(sma3(closes[:-1] + [price]))
        assert out["sma"][-2] == pytest.approx(sma3(closes[:-1]))


# ---- rs-2: gap alerts switched on from an RTH chart keep to the regular hours ----------------------------------------

def gap_minutes() -> list[Bar]:
    """ES, Thu Oct 15 17:00 to Tue Oct 20 15:00 2026: Friday's RTH closes at 6000; Monday's RTH opens 50 higher and holds;
    from Monday's 15:00 close it trades 5990 (all hours fill the gap there, the RTH chart keeps it open); Tuesday's RTH
    opens at 6040 and trades 5998 at 11:00 (the RTH chart's fill)."""
    def price(t):
        c = sessions.to_ct(t)
        m, rth = c.hour * 60 + c.minute, 510 <= c.hour * 60 + c.minute < 900
        if c.day == 19:
            return 6050.0 if rth else 6010.0 if m < 510 else 5990.0
        if c.day == 20:
            return (5998.0 if 660 <= m < 670 else 6040.0) if rth else 5990.0 if m < 510 else 6040.0
        return 6000.0
    out = []
    for t in range(ms(2026, 10, 15, 17), ms(2026, 10, 20, 15), MIN):
        if sessions.is_open(t):
            p = price(t)
            out.append(Bar(t=t, o=p, h=p, l=p, c=p, v=10, n=3))
    return out


class GapStore:
    path = ":memory:"

    def __init__(self):
        self.kv, self.events = {}, []

    def get_setting(self, k, default=None):
        return self.kv.get(k, default)

    def set_setting(self, k, v):
        self.kv[k] = v

    def add_event(self, ev):
        self.events.append(ev)
        return len(self.events)


class GapMarket:
    def __init__(self, split):
        from chartsrv.config import Instrument
        self.store, self.split = GapStore(), split
        self.instruments = {"ES": Instrument(symbol="ES", name="ES", tick_size=0.25, point_value=50,
                                             rth=("08:30", "15:00"))}

    def recent_minutes(self, sym):
        return self.split, []

    def broadcast(self, msg):
        pass

    async def _deliver(self, ev):
        pass


def gap_alerts_sent(monkeypatch, body: dict) -> list[str]:
    """The alerts GapService sends for gap_minutes(), switched on with this PUT body (history to Monday 08:00, then each
    minute live), each as 'Day HH:MM message' at the minute's close."""
    import asyncio

    import chartsrv.gaps_service as gs
    from chartsrv.app import GapAlertsIn
    m1, split = gap_minutes(), ms(2026, 10, 19, 8)
    monkeypatch.setattr(gs, "iter_bars_1m", lambda db, sym, a, b: [x for x in m1 if a <= x.t < b])
    monkeypatch.setattr(gs, "LIVE_BAR_AGE_MS", 10**15)        # the test's bars are not from today
    svc = gs.GapService(GapMarket(split))
    sent = []

    async def go():
        svc.set_alerts("ES", GapAlertsIn(**body).model_dump())
        await svc.watches["ES"].ready.wait()
        for b in m1:
            if b.t >= split:
                n = len(svc.m.store.events)
                svc.on_minute("ES", b, True)
                sent.extend(sessions.to_ct(b.t + MIN).strftime("%a %H:%M ") + e["message"] for e in svc.m.store.events[n:])
    asyncio.run(go())
    return sent


def test_gap_alerts_from_an_rth_chart_fill_as_the_rth_chart_does(monkeypatch):
    body = {"on": True, "new": True, "fill": True, "RthStart": 83000, "RthEnd": 150000, "MinimumGapPoints": 0.0}
    assert gap_alerts_sent(monkeypatch, {**body, "rth": True}) == [
        "Mon 08:31 [ES] GAP UP 50.00 pts | Open: 6050.00 | RTH close: 6000.00",
        "Tue 08:31 [ES] GAP DOWN 10.00 pts | Open: 6040.00 | RTH close: 6050.00",
        "Tue 11:01 [ES] GAP UP FILLED | 50.00 pts in 540 bars"]   # Monday's 390 RTH minutes and Tuesday's 150
    # a config without the flag (an ETH chart's, or one saved before it existed): all hours, as before
    assert gap_alerts_sent(monkeypatch, body) == [
        "Mon 08:31 [ES] GAP UP 50.00 pts | Open: 6050.00 | RTH close: 6000.00",
        "Mon 15:01 [ES] GAP UP FILLED | 50.00 pts in 390 bars",
        "Tue 08:31 [ES] GAP DOWN 10.00 pts | Open: 6040.00 | RTH close: 6050.00"]


GAP_JS = """
const calls = [], els = {};
const $ = (q) => (els[q] ||= { textContent: '', checked: false });
const api = async (method, url, body) => { calls.push([url, body.rth]); return { new: true, fill: true, ...body }; };   // the switches: the server's
const gapSpecOk = () => true, indReady = () => true;
const S = { symbol: 'ES', rth: true, st: { gap: true }, gapAlerts: {}, gaps: { gaps: [] }, gapError: '',
  instruments: { ES: { rth: ['08:30', '15:00'] }, NQ: { rth: ['08:30', '15:00'] }, CL: { rth: ['08:00', '13:30'] }, XX: {} },
  gapSet: { RthStart: 83000, RthEnd: 150000, MinimumGapPoints: 0, AlertOnNewGap: true, AlertOnGapFill: true, EnabledAnalysis: true, days: 90 } };
/*APP*/
(async () => {
  const out = {};
  await putGapAlerts(true);                        // ticked on ES's RTH chart
  renderGapInfo();
  out.rthInfo = $('#gap-info').textContent;
  S.symbol = 'XX';                                 // an instrument without regular hours: all hours
  await putGapAlerts(true);
  S.symbol = 'NQ';
  S.rth = false;
  await putGapAlerts(true);                        // ticked on NQ's ETH chart
  renderGapInfo();
  out.ethInfo = $('#gap-info').textContent;
  S.gapAlerts.CL = { on: true, rth: true };
  S.gapAlerts.XX.on = false;
  out.put = calls.splice(0);
  await gapAlertsFollow();                         // a Gap settings save on NQ's ETH chart: each keeps its own hours
  out.follow = calls.splice(0);
  console.log(JSON.stringify(out));
})();
"""


@needs_node
def test_the_chart_switches_gap_alerts_on_with_its_hours():
    from test_hunt_web_chart import run_app
    out = run_app(["hhmm", "gapRth", "gapRthOk", "GAP_RTH_BAD", "gapAlertsPut", "renderGapInfo", "putGapAlerts", "gapAlertsFollow"], GAP_JS)
    assert out["put"] == [["/api/gaps/alerts/ES", True], ["/api/gaps/alerts/XX", False], ["/api/gaps/alerts/NQ", False]]
    assert out["rthInfo"].endswith("Alerts: new gap + fill (RTH).") and out["ethInfo"].endswith("Alerts: new gap + fill.")
    # one request with the shared settings and no hours: the server keeps each one's own (test_hunt7_gapsmonthly)
    assert out["follow"] == [["/api/gaps/alerts", None]]
