"""Round 11 of the bug hunt, gap alerts while their watch loads and the round-10 alert hold: a new gap or a fill whose
minute comes while a symbol's gap watch loads (a server start, the reload after a gap fill or a Gap settings save) is
sent once it has loaded, and only once; a held new gap that filled during the hold is sent with its fill, and none is
sent when the stretch's history failed; an open page reads the gap alerts again when an instrument is put back."""
import asyncio
import json
import shutil
import threading
import time
from datetime import timedelta

import pytest

import chartsrv.gaps_service as gs
from chartsrv.gapfill import filling
from chartsrv.models import Bar, Tick
from chartsrv.store import Store
from test_hunt10_hold import GAP, add_on, at_open, ct_ms, events  # noqa: F401  (at_open: the fixture)
from test_hunt7_drawings import _client

UP = "[ES] GAP UP 50.00 pts | Open: 6050.00 | RTH close: 6000.00"


@pytest.fixture
def gate(monkeypatch):
    """Gap watches load until the test opens it (90 days of minutes take about half a second a symbol, six at once
    12 to 25 s); open from the start unless cleared."""
    opened = threading.Event()
    real = gs._load

    def load(*a):
        opened.wait(10)
        real(*a)
    monkeypatch.setattr(gs, "_load", load)
    yield opened
    opened.set()


def stored(tmp_path, rows: list[tuple[int, float]], last: int | None = None):
    """ES's stored 1-minute bars, its last trade at `last` (else the last bar's open), and its gap alerts on, all
    hours. Returns that last trade's time."""
    last = rows[-1][0] if last is None else last
    st = Store(tmp_path / "chart.db")
    st.write([Tick("ES", last, rows[-1][1], 1, 1)], [("ES", Bar(t, px, px, px, px, 1, 0, 0, 1)) for t, px in rows])
    st.set_setting("gap_alerts", {"ES": {**GAP, "on": True, "rth": False}})
    st.close()
    return last


def opening(tmp_path, day, stops_at) -> int:
    """The day before's RTH closing at 6000, then pre-market at 6040 from 07:00 to its last trade at stops_at (h, m, s)."""
    prev, last = day - timedelta(days=1), ct_ms(day, *stops_at)
    rows = [(ct_ms(prev, 8, 30) + k * 60_000, 6000.0) for k in range(0, 390, 30)]
    rows += [(ct_ms(day, 7, 0) + k * 60_000, 6040.0) for k in range((last - ct_ms(day, 7, 0)) // 60_000 + 1)]
    return stored(tmp_path, rows, last)


def trader(c, m, nt):
    def trade(px, dt=0):
        c.portal.call(nt._line, m, f"T|ES|{int(time.time() * 1000) + dt}|{px}|1|1\n".encode())
        c.portal.call(asyncio.sleep, 0.02)
    return trade


def loaded(c, m, gate):
    """Opens the gate and waits for ES's watch (and any it replaced) to have loaded."""
    gate.set()
    c.portal.call(m.gaps.watches["ES"].ready.wait)
    c.portal.call(asyncio.sleep, 0.1)


# ---- k-gc11-1: a new gap or a fill whose minute comes while the watch loads is sent once it has loaded, once -------------

def test_the_opens_gap_while_the_watch_loads_after_a_restart_is_sent(tmp_path, monkeypatch, at_open, gate):
    """The server restarts at 08:29:58 with NinjaTrader running (k-hg10-2's scenario): ES's watch is still loading its
    90 days when the add-on reconnects and ES opens 6050 against a 6000 RTH close at 08:30:01. Once it has loaded, the
    gap is sent, once (before: the minutes queued while it loaded went into the engine silently, never sent)."""
    day, shift = at_open
    opening(tmp_path, day, (8, 29, 57))
    app, client = _client(tmp_path, monkeypatch)
    with client as c:
        m = app.state.market
        nt = add_on(m)
        hello = int(time.time() * 1000)
        c.portal.call(lambda: m.gapfill.requests(hello, live=True, minutes=True, contracts=True))
        assert not filling(m, "ES")   # a second's stretch: no hold
        trade = trader(c, m, nt)
        shift[0] += 3   # 08:30:01
        trade(6050.0)
        trade(6050.25, 300)
        assert not m.gaps.watches["ES"].ready.is_set() and events(c) == []
        loaded(c, m, gate)
        assert events(c) == [UP]
        trade(6050.5, 1000)
        trade(6051.0, 2000)
        assert events(c) == [UP]
        m.feed_objs.remove(nt)


@pytest.mark.parametrize("behind", [False, True])
def test_the_opens_gap_while_the_watch_loads_again_with_the_stretch_is_sent(tmp_path, monkeypatch, at_open, gate, behind):
    """NinjaTrader started at 08:29:58 with ES's data stopping at 08:26 (a stretch that holds its alerts); its answer
    comes at 08:29:59.9 and ES's watch loads again with it; ES opens at 08:30:01 while it does: sent once loaded. Or
    the answer comes at 08:30:01 with the open's first trade right behind it, read with it before the load starts."""
    day, shift = at_open
    last = opening(tmp_path, day, (8, 26, 0))
    gate.set()
    app, client = _client(tmp_path, monkeypatch)
    with client as c:
        m = app.state.market
        c.portal.call((w0 := m.gaps.watches["ES"]).ready.wait)
        nt = add_on(m)
        hello = int(time.time() * 1000)
        c.portal.call(lambda: m.gapfill.requests(hello, live=True, minutes=True, contracts=True))
        assert filling(m, "ES")
        gate.clear()
        shift[0] += 3 if behind else 1.9   # 08:29:59.9: the stretch's pre-market trades
        for k in range(1, 4):
            c.portal.call(nt._line, m, f"X|ES|{last + k * 30_000}|6040|1\n".encode())
        if behind:
            c.portal.call(lambda: [nt._line(m, x.encode()) for x in ("Z|ES|ticks|3|ok", f"T|ES|{int(time.time() * 1000)}|6050|1|1")])
        else:
            c.portal.call(nt._line, m, b"Z|ES|ticks|3|ok\n")
        assert not filling(m, "ES") and m.gaps.watches["ES"] is not w0
        trade = trader(c, m, nt)
        if not behind:
            shift[0] += 1.1   # 08:30:01
            trade(6050.0)
        trade(6050.25, 300)
        assert not m.gaps.watches["ES"].ready.is_set() and events(c) == []
        loaded(c, m, gate)
        trade(6050.5, 1000)
        assert events(c) == [UP]
        m.feed_objs.remove(nt)


def test_a_fill_while_a_gap_save_reloads_the_watch_is_sent_and_no_gap_twice(tmp_path, monkeypatch, at_open, gate):
    """ES's gap sent at 08:30:01. Another chart's Gap settings OK (minimum size 0.25) at 08:30:20, while the gap's
    minute is forming: the watch loads again with it as history, not sent again. Another at 08:50, and ES trades
    back through the RTH close while that one loads: 'GAP UP FILLED' once it has (before: never)."""
    day, shift = at_open
    opening(tmp_path, day, (8, 29, 57))
    gate.set()
    app, client = _client(tmp_path, monkeypatch)
    with client as c:
        m = app.state.market
        c.portal.call(m.gaps.watches["ES"].ready.wait)
        nt = add_on(m)
        trade = trader(c, m, nt)
        shift[0] += 3   # 08:30:01
        trade(6050.0)
        assert events(c) == [UP]
        for at, px, least in ((19, 6051.0, 0.25), (20 * 60, 5999.75, 0.5)):   # 08:30:20, then 08:50:20
            shift[0] += at
            trade(6020.0)
            gate.clear()
            assert c.put("/api/gaps/alerts", json={**GAP, "MinimumGapPoints": least}).status_code == 200
            trade(px, 100)
            assert not m.gaps.watches["ES"].ready.is_set()
            loaded(c, m, gate)
            trade(px, 1000)
        assert events(c)[::-1] == [UP, "[ES] GAP UP FILLED | 50.00 pts in 1 bars"]
        m.feed_objs.remove(nt)


def test_a_gap_whose_watch_is_replaced_while_it_loads_is_sent_once(tmp_path, monkeypatch, at_open, gate):
    """The restart again: ES's watch loading, the add-on asked for the second before its first live trade. ES opens at
    08:30:01, then the add-on's answer (a trade in that second) is stored and ES's watch, still loading, is replaced
    by one loading with it; another trade. Once both have loaded the gap is sent, once: by the watch it came to."""
    day, shift = at_open
    last = opening(tmp_path, day, (8, 29, 57))
    app, client = _client(tmp_path, monkeypatch)
    with client as c:
        m = app.state.market
        w0 = m.gaps.watches["ES"]
        nt = add_on(m)
        hello = int(time.time() * 1000)
        c.portal.call(lambda: m.gapfill.requests(hello, live=True, minutes=True, contracts=True))
        trade = trader(c, m, nt)
        shift[0] += 3   # 08:30:01
        trade(6050.0)
        c.portal.call(nt._line, m, f"X|ES|{last + 1500}|6040|1\n".encode())
        c.portal.call(nt._line, m, b"Z|ES|ticks|1|ok\n")
        assert m.gaps.watches["ES"] is not w0 and not w0.ready.is_set()
        trade(6050.25, 300)
        assert events(c) == []
        loaded(c, m, gate)
        c.portal.call(w0.ready.wait)
        trade(6050.5, 1000)
        assert events(c) == [UP]
        m.feed_objs.remove(nt)


# ---- k-gc11-2 and k-gc11-3: the held gap, with its fill when it filled meanwhile; none from an unfilled stretch -----------

def held(tmp_path, monkeypatch, at_open, rows, prices, answer):
    """ES's data as `rows`; NinjaTrader started at 08:29:58 (ES's alerts held); ES opens at 08:30:01 and trades
    `prices` ((ms after, price)); the add-on's answer for the stretch at 08:31:31 (`answer`: its trades at 6000, or a
    Z line); then two more trades. The alert events sent during the hold and after, oldest first."""
    _, shift = at_open
    last = stored(tmp_path, rows)
    app, client = _client(tmp_path, monkeypatch)
    with client as c:
        m = app.state.market
        c.portal.call(m.gaps.watches["ES"].ready.wait)
        nt = add_on(m)
        hello = int(time.time() * 1000)
        c.portal.call(lambda: m.gapfill.requests(hello, live=True, minutes=True, contracts=True))
        assert filling(m, "ES")
        trade = trader(c, m, nt)
        shift[0] += 3   # 08:30:01
        for dt, px in prices:
            trade(px, dt)
        during = events(c)
        shift[0] += 90   # 08:31:31
        if answer == "trades":
            for k in range(1, 4):
                c.portal.call(nt._line, m, f"X|ES|{last + k * 30_000}|6000|1\n".encode())
            c.portal.call(nt._line, m, b"Z|ES|ticks|3|ok\n")
            assert filling(m, "ES")   # the rest after the trades (08:27-08:30) asked again as minutes: none
            c.portal.call(nt._line, m, b"Z|ES|minutes|0|ok\n")
        else:
            c.portal.call(nt._line, m, answer.encode())
        assert not filling(m, "ES")
        c.portal.call(m.gaps.watches["ES"].ready.wait)
        trade(6001.0)
        trade(6001.25, 500)
        m.feed_objs.remove(nt)
        return during, events(c)[::-1]


@pytest.mark.parametrize("answer", ["trades", "Z|ES|minutes|0|ok\n"])
def test_a_held_gap_that_filled_during_the_hold_is_sent_with_its_fill(tmp_path, monkeypatch, at_open, answer):
    """ES's data stops at 08:26. It opens 6050 against 6000 at 08:30:01 and trades back to 5999.75 at 08:30:31, while
    its alerts are held; the add-on's answer comes at 08:31:31. The next trade sends the gap and its fill (before: the
    gap as an open one, its fill never)."""
    day, _ = at_open
    rows = [(t, 6000.0) for t in range(ct_ms(day - timedelta(days=1), 8, 30) - 3 * 86_400_000, ct_ms(day, 8, 26), 60_000)]
    during, sent = held(tmp_path, monkeypatch, at_open, rows, [(0, 6050.0), (30_000, 5999.75)], answer)
    assert during == [] and sent == [UP, "[ES] GAP UP FILLED | 50.00 pts in 0 bars"]


def test_a_held_gap_is_not_sent_when_the_stretchs_history_failed(tmp_path, monkeypatch, at_open):
    """ES's data stops yesterday at 12:00 at 6000 (the PC off); its RTH close, in the stretch, is not stored. ES opens
    6050 while its alerts are held, and NinjaTrader's history request fails (Z|ES|error): the stretch is not filled,
    so the gap, measured from yesterday's noon price, is not sent (before: sent after the error)."""
    day, _ = at_open
    prev = day - timedelta(days=1)
    rows = [(t, 6000.0) for t in range(ct_ms(prev, 8, 30) - 3 * 86_400_000, ct_ms(prev, 12, 0) + 1, 60_000)]
    during, sent = held(tmp_path, monkeypatch, at_open, rows, [(0, 6050.0), (500, 6050.25)], "Z|ES|error|0|BarsRequest failed\n")
    assert during == sent == []


# ---- k-gc11-4: an open page reads the gap alerts again when an instrument is put back -----------------------------------

PUT_BACK_JS = """
const els = {};
const $ = (q) => (els[q] ||= { textContent: '', checked: false, value: '' });
const gapSpecOk = () => true, indReady = () => true, local = { set() {} }, isExtSym = () => false, watch = { render() {} };
const WL_FUT = /^([A-Z0-9]{1,6})1!$/, TK_MULTI = false;
let gapDays = 90, appConfig = null;
const SERVER = SERVER_ALERTS;
const api = async (method, url) => (method === 'GET' && url === '/api/gaps/alerts' ? { alerts: SERVER, watches: {} } : {});
const S = { symbol: 'CL', rth: false, st: { gap: true }, gaps: { gaps: [] }, gapError: '', instruments: { ES: {} }, catalog: {},
  gapAlerts: { ES: { on: true } },
  gapSet: { RthStart: 83000, RthEnd: 150000, MinimumGapPoints: 0, AlertOnNewGap: true, AlertOnGapFill: true, EnabledAnalysis: true, days: 90 } };
/*APP*/
(async () => {
  onMessage({ type: 'instruments', instruments: { ES: {}, CL: { rth: ['08:00', '13:30'] } }, catalog: {}, config: 'x' });
  for (let i = 0; i < 5; i++) await null;
  console.log(JSON.stringify({ cl: S.gapAlerts.CL || null, checked: $('#gap-alerts').checked, info: $('#gap-info').textContent }));
})();
"""


@pytest.mark.skipif(not shutil.which("node"), reason="needs node")
def test_an_open_page_reads_the_gap_alerts_again_when_an_instrument_is_put_back(tmp_path, monkeypatch):
    """CL's gap alerts kept on while it was off Instruments; a page opened then has no CL in its gap alerts (the
    server shows only the instruments'). CL put back live: the server runs CL's alerts and broadcasts 'instruments';
    the page reads the gap alerts again, so CL's chart ticks 'Alerts to Discord/phone' (or says why they are off)
    (before: unticked, while CL's alerts fired, until the page reconnected)."""
    from test_hunt7_drawings import set_instruments
    from test_hunt_web_chart import run_app
    from test_instruments_edit import file_rows
    app, client = _client(tmp_path, monkeypatch)
    with client as c:
        assert c.put("/api/gaps/alerts/CL", json={**GAP, "on": True}).status_code == 200
        set_instruments(c, [r for r in file_rows(tmp_path) if r["symbol"] != "CL"])
    app, client = _client(tmp_path, monkeypatch)
    with client as c:
        assert "CL" not in c.get("/api/gaps/alerts").json()["alerts"]   # the page's hello
        sent = []
        m = app.state.market
        orig = m.broadcast
        m.broadcast = lambda msg: (sent.append(msg.get("type")), orig(msg))[1]
        set_instruments(c, file_rows(tmp_path))   # CL back
        got = c.get("/api/gaps/alerts").json()
        assert got["alerts"]["CL"]["on"] is True and "CL" in got["watches"] and "instruments" in sent
    parts = ["hhmm", "gapRth", "gapAlertsPut", "renderGapInfo", "renderGapMenu", "loadGapAlerts", "onMessage"]
    out = run_app(parts, PUT_BACK_JS.replace("SERVER_ALERTS", json.dumps(got["alerts"])))
    assert out["cl"] == got["alerts"]["CL"] and out["checked"] and out["info"].endswith(" Alerts: new gap + fill."), out
    why = "RTH 11:00-12:00 is no session on CL: its regular hours (08:00-13:30 CT) move the start and end to the same time."
    refused = {**got["alerts"], "CL": {**got["alerts"]["CL"], "on": False, "error": why}}
    out = run_app(parts, PUT_BACK_JS.replace("SERVER_ALERTS", json.dumps(refused)))
    assert not out["checked"] and out["info"].endswith(f" Alerts off: {why}"), out
