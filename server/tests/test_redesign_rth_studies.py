"""TPO Profile, Gap Analysis and HTF EMA on an RTH-only chart: built on the chart's own regular-hours bars (counted from
the RTH open, the last one cut at the RTH close), as NinjaTrader runs them on an RTH trading-hours template, not on
Globex bars aligned to 17:00 that hold the pre-market. The server side, the three endpoints' rth parameter and what
the chart page sends."""
import shutil
from datetime import datetime, timedelta

import pytest

from amt_parity import synth
from chartsrv import gaps, htfema, sessions, tpo
from chartsrv.amt_runner import ct, epoch_ms
from chartsrv.bars import make_series, parse_spec
from chartsrv.gaps_service import run as gap_run
from chartsrv.models import Bar
from chartsrv.tpo_service import Daily, chart_bars, payload, run as tpo_run
from htfema_parity import js_values
from test_hunt_web_chart import run_app

needs_node = pytest.mark.skipif(not shutil.which("node"), reason="needs node")
RTH = ("08:30", "15:00")
END = datetime(2026, 10, 16, 12, 7)   # a Friday


def scenario(end: datetime, days: int) -> list[Bar]:
    """1-minute bars of `days` Globex sessions up to `end`, flat in each part of the day so every price says where it
    came from: the night at base + 50, the pre-market (08:00-08:30) at base + 100, the regular hours at base + 120 and
    the hour after them at base + 200; base climbs 100 a session (each RTH open gaps 100 above the last RTH close)."""
    start = sessions.session_start(epoch_ms(end))
    for _ in range(days - 1):
        start = sessions.previous_session_start(start)
    out, t, base, session = [], start, 7000.0, start
    while t < epoch_ms(end):
        if sessions.is_open(t):
            s = sessions.session_start(t)
            if s != session:
                session, base = s, base + 100
            c = ct(t)
            m = c.hour * 60 + c.minute
            p = base + (100 if 480 <= m < 510 else 120 if 510 <= m < 900 else 200 if 900 <= m < 960 else 50)
            out.append(Bar(t=t, o=p, h=p + 0.25, l=p - 0.25, c=p, v=10))
        t += 60_000
    return out


# ---- the chart's bars -------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("spec", ["5m", "7m", "30m", "1h", "4h"])
def test_rth_chart_bars_are_the_rth_chart_s_own_bars(spec):
    minutes = parse_spec(spec).size // 60
    m1 = synth(8, 3, END)
    series = make_series(parse_spec(spec), 0.25, 2, 100_000, RTH, rth_only=True)
    for b in m1:
        series.on_bar(b)
    ours = chart_bars(m1, minutes, RTH)
    assert [(b[0], b[5], b[2], b[3], b[6]) for b in ours] == [(b.t, b.o, b.h, b.l, b.c) for b in series.bars]
    for k, close, _, _, sess, *_ in ours:      # close stamps one bar on, the day's last cut at the RTH close
        o, c = ct(sess), ct(close)
        assert (o.hour, o.minute) == (8, 30) and o.date() == ct(k).date()
        assert close - k == minutes * 60_000 or (c.hour, c.minute) == (15, 0)
        assert (c.hour, c.minute) <= (15, 0)
    assert chart_bars(m1, minutes) == chart_bars(m1, minutes, None)   # all hours: as before


# ---- Gap Analysis -----------------------------------------------------------------------------------------------------

def test_gap_analysis_on_an_rth_chart_opens_at_the_rth_open():
    """The failure scenario: the night trades lower, the pre-market at +100, the RTH opens at +120. On the RTH chart
    the gap is the 08:30 bar's open against the last RTH close, starting at the 08:30 bar."""
    m1 = scenario(END, 6)
    eng, opens = gap_run(gaps.settings({}), 60, m1, rth=RTH)
    assert len(eng.History) == 5
    for g in eng.History:
        st = ct(opens[g.StartBar])
        assert (st.hour, st.minute) == (8, 30)
        assert g.OpenPrice % 100 == 20 and g.OpenPrice - g.SettlePrice == 100   # RTH open vs the RTH close before
    eth, eth_opens = gap_run(gaps.settings({}), 60, m1)                         # an all-hours chart: as before
    assert [(ct(eth_opens[g.StartBar]).hour, g.OpenPrice % 100) for g in eth.History] == [(8, 0)] * 5


# ---- TPO Profile ------------------------------------------------------------------------------------------------------

def _tpo(s, m1, rth, minutes=60):
    daily = Daily("equity", rth)
    daily.add_minutes(m1, 2**62)
    eng, open_of = tpo_run(tpo.settings(s), 0.25, minutes, m1, daily, 2**62, rth=rth)
    return eng, payload(eng, open_of, minutes)


@pytest.mark.parametrize("filtered", [True, False])
def test_tpo_on_an_rth_chart_starts_at_the_rth_open_without_the_premarket(filtered):
    """The profile's first letter is the chart's first RTH bar (08:30-09:30), and the 08:00-08:30 pre-market at +100
    is in no row; Filter by Time off: one profile per RTH session, from its open to its close."""
    m1 = scenario(END, 6)
    s = {"FilterByTime": filtered, "ShowLive": True, "LetterDays": 5}
    eng, d = _tpo(s, m1, RTH)
    assert len(d["profiles"]) == 6
    assert {ct(p["x0"]).strftime("%H:%M") for p in d["profiles"][1:]} == {"08:30"}   # (the first bar loaded: no update)
    assert {r[0] % 100 for p in d["profiles"] for r in p["rows"]} == {20}      # the regular hours' prices only
    assert {(p.TimeStart.strftime("%H:%M"), p.TimeEnd.strftime("%H:%M")) for p in eng.saved[1:]} == {("09:30", "15:00")}
    _, eth = _tpo(s, m1, None)                                                 # all hours: as before
    assert {ct(p["x0"]).strftime("%H:%M") for p in eth["profiles"][1:]} == {"08:00" if filtered else "17:00"}
    assert 0 in {r[0] % 100 for p in eth["profiles"] for r in p["rows"]}      # the 08:00 bar's pre-market


def test_tpo_daily_atr_on_an_rth_chart_comes_from_rth_daily_bars():
    m1 = scenario(END, 6)
    daily = Daily("equity", RTH)
    daily.add_minutes(m1, 2**62)
    assert [(ct(k).hour, ct(k).minute) for k in daily.keys] == [(8, 30)] * 6
    assert [hlc[0] - hlc[1] for hlc in daily.hlc] == [0.5] * 6 and all(hlc[2] % 100 == 20 for hlc in daily.hlc)
    rth_open = [k for k, *_ in chart_bars(m1, 60, RTH) if ct(k).hour == 8]
    assert [daily.before(k) for k in rth_open] == list(range(6))               # each day sees the days before it
    eth = Daily("equity")
    eth.add_minutes(m1, 2**62)
    assert [hlc[0] - hlc[1] for hlc in eth.hlc][:-1] == [150.5] * 5            # all hours: night to the 15:00 hour


def test_rth_daily_bars_fold_a_holiday_halt_into_the_next_day():
    """A holiday halt's session has no settlement: its RTH hours join the next day's daily bar, as the ETH daily bars
    and the RTH 1D chart do (for TPO's ATR and for HTF EMA's Day source)."""
    sessions.set_calendar({"days": [{"date": "2026-10-13", "name": "Halt", "halt": "12:00"}]})   # a Tuesday
    m1 = scenario(END, 6)
    daily = Daily("equity", RTH)
    daily.add_minutes(m1, 2**62)
    assert [ct(k).date().day for k in daily.keys] == [9, 12, 13, 15, 16]       # Tuesday's with Wednesday's, keyed by Tuesday
    wed = epoch_ms(datetime(2026, 10, 14, 8, 30))
    assert daily.before(wed) == 2                                              # Wednesday's bars: Friday's and Monday's
    s = htfema.settings({"HtfPeriodType": "Day", "HtfPeriod": 1})
    days = htfema.htf_bars(s, m1, "equity", RTH)
    series = make_series(parse_spec("1D"), 0.25, 2, 1000, RTH, rth_only=True, group="equity")
    for b in m1:
        series.on_bar(b)
    assert [(ct(d[1]), d[2], d[3], d[6]) for d in days] == [
        (ct(b.t).replace(hour=15, minute=0), b.h, b.l, b.c) for b in series.bars]   # the RTH 1D chart's days, at their close


# ---- HTF EMA ----------------------------------------------------------------------------------------------------------

def _rth_bars(m1, spec):
    series = make_series(parse_spec(spec), 0.25, 2, 100_000, RTH, rth_only=True)
    for b in m1:
        series.on_bar(b)
    return list(series.bars)


@needs_node
@pytest.mark.parametrize("spec", ["5m", "7m"])
def test_htf_ema_exact_on_an_rth_chart_follows_the_rth_hour_bars(spec):
    """ExactHTF: each RTH bar shows NinjaTrader's EMA(21) of the RTH hour bars (08:30-09:30 ... 14:30-15:00) that
    closed before it did (the chart bar first on a tie), worked out here from the RTH chart's own 1h bars; a bar's
    close is one period on, the day's last cut at 15:00 (7m: 14:55-15:00)."""
    s = htfema.settings({"Mode": "ExactHTF"})
    m1 = synth(12, 4, END)
    minutes = parse_spec(spec).size // 60
    bars, hours = _rth_bars(m1, spec), _rth_bars(m1, "1h")
    close = lambda b, p: min(b.t + p, epoch_ms(ct(b.t).replace(hour=15, minute=0)))   # noqa: E731
    ema, emas = htfema.NtEma(21), []
    for h in hours:
        v = ema.add(h.c)
        emas.append((close(h, 3_600_000), v if len(ema.values) >= 21 else None))
    want = []
    for b in bars:
        seen = [v for c, v in emas if c < close(b, minutes * 60_000)]
        want.append(seen[-1] if seen else None)
    now = m1[-1].t + 60_000
    d = htfema.compute(s, m1, minutes, htfema.GetScaledLength(s, "Minute", minutes), bars[0].t, now, "equity", rth=RTH)
    assert {ct(c).strftime("%H:%M") for c, _ in d["htf"]} <= {"09:30", "10:30", "11:30", "12:30", "13:30", "14:30", "15:00"}
    assert {ct(e).strftime("%H:%M") for e in d["ends"]} == {"15:00"}
    got = js_values([{"t": b.t, "c": b.c} for b in bars], d, minutes * 60_000)
    assert got == want and sum(v is not None for v in got) > 200


@needs_node
@pytest.mark.parametrize("mode", htfema.MODES)
def test_htf_ema_on_an_rth_chart_starting_mid_history_matches_a_full_run(mode):
    s = htfema.settings({"Mode": mode})
    m1 = synth(30, 6, END)
    bars = [{"t": b.t, "c": b.c} for b in _rth_bars(m1, "5m")]
    now = m1[-1].t + 60_000

    def payload_from(t):
        return htfema.compute(s, m1, 5, 252, t, now, "equity", rth=RTH)
    full = js_values(bars, payload_from(bars[0]["t"]), 300_000)
    cut = 1200
    part = js_values(bars[cut:], payload_from(bars[cut]["t"]), 300_000)
    assert part == full[cut:] and all(v is not None for v in part)
    if mode == "SmoothScaled":
        assert payload_from(bars[cut]["t"])["smooth"]["count"] == cut            # the seed: the RTH bars before it


def test_htf_ema_history_counts_regular_hours():
    s = htfema.settings({})
    t = 10**13
    assert htfema.history_ms(s, 5, 252, t, t) == t - 14 * 86_400_000             # all hours: as before
    assert htfema.history_ms(s, 5, 252, t, t, rth=RTH) == t - 40 * 86_400_000        # 390 minutes a day: 8 x 21 hours
    day = htfema.settings({"HtfPeriodType": "Day", "HtfPeriod": 1})
    assert htfema.history_ms(day, None, 21, t, t, rth=RTH) == htfema.history_ms(day, None, 21, t, t)   # a bar a session
    assert htfema.history_ms(htfema.settings({"HtfPeriod": 600}), None, 21, t, t, rth=RTH) == t - 239 * 86_400_000   # likewise


def test_htf_ema_next_open_waits_for_the_rth_close():
    bucket = make_series(parse_spec("2D"), 0.25, 2, 1, RTH, rth_only=True, group="equity").bucket
    thu = epoch_ms(datetime(2026, 10, 15, 15, 30))   # after the RTH close of the last session in a Wednesday-Thursday bar
    assert htfema.next_open(bucket, thu) is None                                # all hours: still trading
    assert htfema.next_open(bucket, thu, RTH) == bucket(epoch_ms(datetime(2026, 10, 15, 18))) != bucket(thu)
    assert htfema.next_open(bucket, epoch_ms(datetime(2026, 10, 15, 14, 59)), RTH) is None


# ---- the endpoints ----------------------------------------------------------------------------------------------------

@pytest.fixture
def stored(tmp_path, monkeypatch, in_session):
    """The app with no feed, on a database holding 25 sessions of scenario() bars up to now (10:30 CT)."""
    from fastapi.testclient import TestClient

    from chartsrv.app import app
    from chartsrv.store import Store
    monkeypatch.setenv("CHART_LEVELS_DIR", str(tmp_path))
    monkeypatch.setenv("CHART_FEED", "none")
    monkeypatch.setenv("CHART_DB", str(tmp_path / "chart.db"))
    Store(tmp_path / "chart.db").write([], [("ES", b) for b in scenario(ct(in_session), 25)])
    with TestClient(app) as c:
        yield c


def test_the_three_endpoints_take_rth(stored):
    c = stored
    g = c.get("/api/gaps", params={"symbol": "ES", "spec": "1h", "rth": "true"}).json()
    assert g["gaps"] and all(ct(x["st"]).strftime("%H:%M") == "08:30" and x["open"] % 100 == 20 for x in g["gaps"])
    g = c.get("/api/gaps", params={"symbol": "ES", "spec": "1h"}).json()
    assert g["gaps"] and all(ct(x["st"]).strftime("%H:%M") == "08:00" and x["open"] % 100 == 0 for x in g["gaps"])

    # TPO: the RTH daily bars' ATR (100 a day) gives 5-tick rows, the Globex days' (150.5) 8-tick ones; neither
    # answer is kept for the other's request
    q = {"symbol": "ES", "spec": "1h", "ShowLive": "true"}
    for rth, step, x0 in [(True, 1.25, "08:30"), (False, 2.0, "08:00"), (True, 1.25, "08:30")]:
        r = c.get("/api/tpo", params={**q, **({"rth": "true"} if rth else {})})
        assert r.status_code == 200, r.text
        d = r.json()
        assert d["step"] == step and {ct(p["x0"]).strftime("%H:%M") for p in d["profiles"]} == {x0}
    assert c.get("/api/tpo", params={**q, "rth": "false"}).json()["step"] == 2.0
    m = c.app.state.market
    assert set(m.tpo.daily["ES"]) == {False, True}
    c.portal.call(m.history_changed, "ES")                                     # a gap fill: both built again
    assert "ES" not in m.tpo.daily

    start = c.get("/api/gaps", params={"symbol": "ES", "spec": "1h", "rth": "true"}).json()["gaps"][-3]["st"]
    h = c.get("/api/htfema", params={"symbol": "ES", "spec": "5m", "start": start, "rth": "true"}).json()
    assert {ct(e).strftime("%H:%M") for e in h["ends"]} == {"15:00"}
    assert {ct(x[0]).strftime("%H:%M") for x in h["htf"]} <= {"09:30", "10:30", "11:30", "12:30", "13:30", "14:30", "15:00"}
    h = c.get("/api/htfema", params={"symbol": "ES", "spec": "5m", "start": start}).json()
    assert {ct(e).strftime("%H:%M") for e in h["ends"]} == {"16:00"}
    assert c.get("/api/htfema", params={"symbol": "ES", "spec": "1D", "start": start, "rth": "true"}).status_code == 200


# ---- the chart page ---------------------------------------------------------------------------------------------------

REQ_JS = """
const S = { st: { tpo: true, gap: true, hema: true }, symbol: 'ES', spec: '1h', rth: true, instruments: { ES: { rth: ['08:30', '15:00'] } },
  tpoSet: { SessionTime: '0830-1500', ShowLive: false, BoxTransparency: 0 }, bars: [{ t: 1.8e12, c: 1 }],
  gapSet: { RthStart: 83000, RthEnd: 150000, MinimumGapPoints: 0, days: 90, EnabledAnalysis: true },
  hemaSet: { HtfPeriodType: 'Minute', HtfPeriod: 60, EmaLength: 21, Mode: 'SmoothScaled' }, tpo: null, gaps: null, hema: null };
const indReady = () => true, tpoPrim = { update() {} }, gapPrim = { update() {} }, renderTpoInfo = () => {}, renderGapInfo = () => {};
const gapCatchUp = () => {}, hemaDraw = () => {};
const asked = [];
let hold = null;
const api = async (method, path) => {
  asked.push(path);
  if (hold) await hold;
  return { profiles: [], gaps: [], bar: 0, t: 0, htf: [], mode: 'SmoothScaled' };
};
/*APP*/
const rth = () => asked.splice(0).map((p) => new URLSearchParams(p.split('?')[1]).get('rth'));
(async () => {
  const out = {};
  await tpoRequest(); await gapRequest(); await hemaRequest();
  out.rth = rth();
  S.rth = false;
  await tpoRequest(); await gapRequest(); await hemaRequest();
  out.eth = rth();
  // asked on the RTH chart, answered once the chart is back on all hours: not that chart's answer
  S.rth = true;
  S.tpo = S.gaps = S.hema = null;
  let release;
  hold = new Promise((r) => { release = r; });
  const pending = [tpoRequest(), gapRequest(), hemaRequest()];
  S.rth = false;
  release();
  await Promise.all(pending);
  out.stale = [S.tpo, S.gaps, S.hema];
  console.log(JSON.stringify(out));
})();"""


@needs_node
def test_the_chart_asks_for_its_own_hours_and_drops_an_answer_for_the_other():
    out = run_app(["replayUntil", "studyView", "TPO_DRAWING", "tpoSpecOk", "tpoBusy", "tpoRequest", "GAP_SERVER", "gapSpecOk", "gapRthOk", "GAP_RTH_BAD",
                   "gapBusy", "gapRequest", "hemaBusy", "hemaRequest"], REQ_JS)
    assert out["rth"] == ["true", "true", "true"]
    assert out["eth"] == [None, None, None]                    # all hours: the requests as before
    assert out["stale"] == [None, None, None]


def test_scenario_is_what_it_says():
    m1 = scenario(END, 2)
    by = {ct(b.t).strftime("%H:%M"): b.c for b in m1[-1000:]}
    assert by["07:59"] % 100 == 50 and by["08:00"] % 100 == 0 and by["08:30"] % 100 == 20 and by["12:06"] % 100 == 20
    assert ct(m1[0].t) == datetime(2026, 10, 14, 17, 0) and timedelta(minutes=1) == timedelta(milliseconds=m1[1].t - m1[0].t)


def test_an_rth_seconds_chart_counts_its_bars_from_the_rth_open():
    # 7-second bars of an RTH chart open at 08:30:00, 08:30:07...: trades at 08:30:05 and 08:30:08 are two bars (from
    # the 17:00 session open they would share the one opening at 08:30:04); the night's trades are not the chart's
    open_ = epoch_ms(datetime(2026, 10, 15, 8, 30))
    mins = [Bar(t=open_ - 60_000, o=9, h=9, l=9, c=9, n=1), Bar(t=open_, o=10, h=11, l=10, c=11, n=2)]
    secs = [(open_ - 30_000, 9.0), (open_ + 5_000, 10.0), (open_ + 8_000, 11.0)]
    d = htfema.compute(htfema.settings({}), mins, None, 9, open_ + 60_000, open_ + 60_000, chart_seconds=7, rth=RTH,
                       second_closes=lambda lo, hi: iter([x for x in secs if lo <= x[0] < hi]))
    e = htfema.NtEma(9)
    for c in (10.0, 11.0):
        e.add(c)
    assert d["smooth"]["count"] == 2 and d["smooth"]["seed"] == pytest.approx(e.values[-1])
