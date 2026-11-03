"""Round 14 of the bug hunt, study values: an RTH chart's session VWAP is the 17:00 session's (the one the Session VWAP
alert watches), live, from history and in a replay; a chart's first session's VWAP counts from its open, on seconds
charts too; PVP keeps as many daily profiles as set and SVP as many sessions; and the anchored VWAP on a longer
timeframe starts at the bar that holds the anchor."""
import random
from datetime import datetime
from types import SimpleNamespace

import pytest

from chartsrv import replay, sessions, vp
from chartsrv.bars import make_series, parse_spec
from chartsrv.config import Instrument
from chartsrv.indicators import defaults
from chartsrv.market import build_history
from chartsrv.models import Bar, Tick
from chartsrv.sessions import CT
from chartsrv.store import Store
from chartsrv.studies import RthClock
from chartsrv.vp_service import MinuteState, VpService, build_minutes, trading_day
from test_hunt_web_chart import WEB, needs_node, run_app

RTH = ("08:30", "15:00")
ES = Instrument("ES", "E-mini", 0.25, 50, "ES", rth=RTH)
CLOCK = RthClock(RTH)
BIG = 2 ** 62
MIN = 60_000


def ms(*a) -> int:
    return int(datetime(*a, tzinfo=CT).timestamp() * 1000)


def prices(start, end, step, seed=1):
    """(time, price) every `step` ms the market is open: a walk, about 40 points higher outside the regular hours."""
    rnd, px, t = random.Random(seed), 6800.0, start
    while t < end:
        if sessions.is_open(t):
            px = round(px + rnd.randint(-4, 4) * 0.25 + 0.02, 2)
            yield t, round(px + (40 if CLOCK.day(t) is None else 0), 2)
        t += step


def minutes(start, end, seed=1):
    return [Bar(t=t, o=p, h=p + 0.5, l=p - 0.75, c=p + 0.25, v=10 + t // MIN % 7) for t, p in prices(start, end, MIN, seed)]


def ticks(start, end, every=20_000, seed=2):
    return [Tick("ES", t, p, 1 + t // every % 5, 1) for t, p in prices(start, end, every, seed)]


def built(path, spec, cutoff, split, now, maxlen=3000, **kw):
    s = make_series(parse_spec(spec), 0.25, 2, maxlen, RTH, group="equity", **kw)
    build_history(path, s, "ES", parse_spec(spec), cutoff, split, now)
    return s


# ---- r14-studies-1: an RTH chart's 'VWAP (session)' is the 17:00 session's, as the Session VWAP alert's -------------

def test_rth_chart_session_vwap_is_the_1700_sessions_from_history(tmp_path):
    store = Store(tmp_path / "t.db")
    split, now = ms(2026, 10, 28, 10), ms(2026, 10, 28, 10, 30)
    store.insert_bars_if_missing("ES", minutes(ms(2026, 10, 26, 17), split))   # Monday 17:00 to Wednesday 10:00
    store.write(ticks(split, now), [])                                           # and the trades since
    cutoff = store.max_tick_rowid()
    rth = built(store.path, "5m", cutoff, split, now, rth_only=True)
    eth = built(store.path, "5m", cutoff, split, now)
    alert = built(store.path, "1m", cutoff, split, now)   # the series a study alert reads (study_alerts.py)
    st = {b.t: b.st for b in eth.bars}
    # every RTH bar: the ETH chart's values at that bar, the session's as of its last trade (Tuesday's last bar too)
    assert len(rth.bars) > 100 and all(b.st == st[b.t] for b in rth.bars)
    assert rth.bars[-1].st[:2] == pytest.approx(list(alert.vwap.value()), abs=1e-3)
    assert rth.bars[-1].st[0] - rth.bars[-1].st[2] > 5   # overnight was higher: not the RTH VWAP


def test_rth_chart_session_vwap_is_the_1700_sessions_live():
    rth = make_series(parse_spec("5m"), 0.25, 2, 3000, RTH, rth_only=True)
    eth = make_series(parse_spec("5m"), 0.25, 2, 3000, RTH)
    for tk in ticks(ms(2026, 10, 27, 13), ms(2026, 10, 28, 10)):   # Tuesday's close, the night, Wednesday's open
        rth.on_tick(tk)
        eth.on_tick(tk)
    st = {b.t: b.st for b in eth.bars}
    assert [b.st for b in rth.bars] == [st[b.t] for b in rth.bars]
    assert rth.bars[-1].st[0] != rth.bars[-1].st[2]


def test_rth_chart_session_vwap_in_a_bar_replay(tmp_path):
    store = Store(tmp_path / "t.db")
    store.insert_bars_if_missing("ES", minutes(ms(2026, 10, 26, 17), ms(2026, 10, 28, 12)))
    now = ms(2026, 10, 28, 12)
    eth = built(store.path, "5m", 0, now, now)
    bars, _, _ = replay.build(store.path, "ES", ES, parse_spec("5m"), ms(2026, 10, 27, 12), now, BIG, rth_only=True)
    st = {b.t: b.st for b in eth.bars}
    assert len(bars) > 100 and [b.st for b in bars] == [st[b.t] for b in bars]


# ---- r14-studies-5: a chart's first session's VWAP counts from its open (minute and seconds charts) -----------------

def test_minute_chart_first_session_vwap_counts_from_its_open(tmp_path):
    store = Store(tmp_path / "t.db")
    now = ms(2026, 10, 29, 13)   # Thursday 13:00 CT; six sessions stored, from Thursday 17:00 a week before
    store.insert_bars_if_missing("ES", minutes(ms(2026, 10, 22, 17), now))
    for spec, maxlen in (("1m", 3000), ("5m", 200)):   # history from Tuesday 03:59 and from Wednesday 18:39
        whole = {b.t: b.st for b in built(store.path, spec, 0, now, now, 10 ** 6).bars}
        chart = built(store.path, spec, 0, now, now, maxlen)
        assert chart.bars[0].t > sessions.session_start(chart.bars[0].t)   # the chart starts inside a session...
        # ...whose VWAP, bands and RTH VWAP are the ones every later session has: from its open
        assert [b.st for b in chart.bars] == [whole[b.t] for b in chart.bars], spec


def test_seconds_chart_session_vwap_counts_from_its_open(tmp_path):
    """Seconds charts read trades only as far back as they hold bars, often from inside today's session: its stored
    minutes before that start the VWAPs, so the live line is the session's, the one a Session VWAP alert watches."""
    store = Store(tmp_path / "t.db")
    now = ms(2026, 10, 29, 13)
    src = list(prices(ms(2026, 10, 26, 17), now, MIN))
    # a trade of each minute's typical price and volume: its VWAP from trades is the one from its minutes
    store.insert_bars_if_missing("ES", [Bar(t=t, o=p, h=p, l=p, c=p, v=10) for t, p in src])
    store.write([Tick("ES", t + 30_000, p, 10, 1) for t, p in src], [])
    cutoff = store.max_tick_rowid()
    alert = built(store.path, "1m", cutoff, now, now)
    want = [*alert.vwap.value(), *alert.vwap_rth.value()]
    for spec, rth_only in (("1s", False), ("5s", False), ("15s", False), ("5s", True)):
        chart = built(store.path, spec, cutoff, now, now, rth_only=rth_only)
        assert chart.bars[0].t > sessions.session_start(now)   # trades from inside today's session
        assert chart.bars[-1].st == pytest.approx(want, abs=1e-3), (spec, rth_only)


# ---- r14-studies-2 and -3: PVP keeps as many daily profiles as set, SVP as many sessions --------------------------

NOW = ms(2026, 11, 2, 11)   # a Monday


@pytest.fixture
def svc(tmp_path):
    """A VpService on 200 days of ES minutes (one every 10 minutes the market is open), a Thursday with none stored."""
    store = Store(tmp_path / "t.db")
    hole = (ms(2026, 10, 14, 17), ms(2026, 10, 15, 16))
    store.insert_bars_if_missing("ES", [Bar(t=t, o=6800, h=6801, l=6799, c=6800, v=10)
                                        for t in range(NOW - 200 * 86_400_000, NOW, 10 * MIN)
                                        if sessions.is_open(t) and not hole[0] <= t < hole[1]])
    s = VpService.__new__(VpService)
    s.m = SimpleNamespace(store=store, instruments={"ES": SimpleNamespace(calendar="equity", rth=RTH)})
    return s


def test_pvp_keeps_as_many_profiles_as_set(svc):
    for period, mult, keep in (("Daily", 1, 20), ("Daily", 1, 60), ("Daily", 2, 20), ("Weekly", 1, 20)):
        s = {**defaults("pvp"), "Period": period, "PeriodMultiplier": mult, "ProfilesToKeep": keep}
        since = svc._first_minute("pvp", "ES", s, NOW)
        st = MinuteState(vp.PeriodicSeries(period, mult, keep, 0.25, s["RowsLayout"] == "NumberOfRows", s["RowSize"],
                                           float(s["ValueAreaPercent"]), s["ValueAreaMethod"] == "AtLeastTarget"))
        st = build_minutes(svc.m.store.path, "ES", st, since, NOW)
        # weekends and the day with nothing stored make no profile: the periods read are the ones kept
        assert len(st.series.profiles) == keep and st.series.profiles[0].window[0] == trading_day(since), (period, mult)
    s = {**defaults("pvp"), "Period": "Monthly", "ProfilesToKeep": 100}   # more than are stored: all of it is read
    since = svc._first_minute("pvp", "ES", s, NOW)
    assert svc.m.store.db.execute("SELECT 1 FROM bars_1m WHERE t<?", (since,)).fetchone() is None


def test_svp_finds_as_many_sessions_as_set(svc):
    for keep in (20, 40, 60, 100):
        w = svc._windows("ES", {**defaults("prof"), "SessionsToKeep": keep}, NOW)
        assert len(w) == keep and w == sorted(w), keep


# ---- r14-studies-4: the anchored VWAP on a longer timeframe starts at the bar holding the anchor --------------------

STUDIES = (WEB / "studies.js").read_text()


def studies_part(name: str) -> str:
    start = STUDIES.index(f"function {name}(")
    return STUDIES[start:STUDIES.index("\n}\n", start) + 3]


AVWAP_JS = """
/*STUDIES*/
const ct = (s) => Date.parse(s + '-05:00'), H = 3600e3;
const bar = (s, c) => ({ t: ct(s), o: c, h: c + 10, l: c - 10, c, v: 1000 });
const hours = [8, 9, 10, 11, 12].map((h) => bar(`2026-10-28T${String(h).padStart(2, '0')}:00:00`, 6800 + h));
const days = ['2026-10-26T17:00:00', '2026-10-27T17:00:00', '2026-10-28T17:00:00'].map((d, i) => bar(d, 6800 + 10 * i));
const rth = ['2026-10-27T13:30:00', '2026-10-27T14:30:00', '2026-10-28T08:30:00', '2026-10-28T09:30:00'].map((d) => bar(d, 6800));
const twoDays = ['2026-10-22T17:00:00', '2026-10-26T17:00:00'].map((d) => bar(d, 6800));   // 2D: Friday and Monday, then on
const anchor = ct('2026-10-28T10:05:00');   // set on a 5-minute chart
const at = (b) => b.t, show = (t) => (t == null ? null : new Date(t - 5 * H).toISOString().slice(5, 16));
const from = (bars, a, p) => { const out = anchoredVwap(bars, a, at, p); return out.length ? show(out[0].time) : null; };
// the app's own call: the chart's bar length
const store = {}, local = { get: (k, d) => store[k] ?? d, set: (k, v) => { store[k] = v; } };
let drawn = null;
const avwap = { setData: (d) => { drawn = d; }, applyOptions() {} }, timeOf = at;
const ax = { full: { format: () => '' } }, zs = () => '', $ = () => ({});
const S = { symbol: 'ES' };
/*APP*/
const app = (spec, kind, bars, a) => {
  Object.assign(S, { spec, kind, bars });
  store['anchor:ES'] = String(a);
  drawAvwap();
  return drawn.length ? show(drawn[0].time) : null;
};
console.log(JSON.stringify({
  h1: from(hours, anchor, H), d1: from(days, anchor, 24 * H),
  h1Forming: from(hours.slice(0, 3), anchor, H), d1Forming: from(days.slice(0, 2), anchor, 24 * H),
  rthNight: from(rth, ct('2026-10-28T03:00:00'), H), rthBeforeOpen: from(rth.slice(0, 2), ct('2026-10-28T03:00:00'), H),
  before: from(hours, ct('2026-10-27T10:00:00'), H), exact: from(hours, hours[2].t, H), ticks: from(hours, anchor, 0),
  app1h: app('1h', 'time', hours, anchor), app1D: app('1D', 'time', days, anchor),
  app2D: app('2D', 'time', twoDays, ct('2026-10-26T10:00:00')), appTicks: app('2000t', 'tick', hours, anchor),
}));
"""


@needs_node
def test_anchored_vwap_on_a_longer_timeframe_starts_at_the_bar_holding_the_anchor():
    js = AVWAP_JS.replace("/*STUDIES*/", "\n".join(studies_part(n) for n in ("barIndexFrom", "barIndexTo", "anchoredVwap")))
    out = run_app(["isTime", "specSeconds", "daysSpec", "anchorKey", "anchorMs", "drawAvwap"], js)
    assert out == {
        "h1": "10-28T10:00", "d1": "10-27T17:00",                 # the 10:00 hour, Wednesday's day bar
        "h1Forming": "10-28T10:00", "d1Forming": "10-27T17:00",   # the anchor in the forming bar: a line all the same
        "rthNight": "10-28T08:30", "rthBeforeOpen": None,          # an RTH chart, anchored overnight: from the open
        "before": "10-28T08:00", "exact": "10-28T10:00",           # before the loaded bars: the first; on a bar's open
        "ticks": "10-28T11:00",                                    # bars of no fixed length: the first one after
        "app1h": "10-28T10:00", "app1D": "10-27T17:00", "app2D": "10-22T17:00", "appTicks": "10-28T11:00",
    }
