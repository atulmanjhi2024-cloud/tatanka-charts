"""Round 10 of the bug hunt, the round-9 alert hold (gapfill.filling) and Gap hours checks: an instrument a futures
plugin supplies put back on Instruments has its stretch filled by that plugin, not asked of the add-on (whose answer
the server drops, holding its alerts for good); a reconnect's seconds-long stretch holds no alert, and a new gap
seen while a long one is filled is sent after; a kept config of an instrument off Instruments is checked against its
hours and one whose alerts were refused is never shown as on; and RTH times an instrument's hours turn round are
refused."""
import asyncio
import json
import shutil
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from chartsrv import sessions
from chartsrv.feeds.ninjatrader import NinjaTraderFeed
from chartsrv.gapfill import filling
from chartsrv.models import Bar, Tick
from chartsrv.store import Store
from test_feed_plugins import _port
from test_hunt7_drawings import _client, set_instruments
from test_instruments_edit import file_rows

GAP = {"new": True, "fill": True, "RthStart": 83000, "RthEnd": 150000, "MinimumGapPoints": 0.0}


class Writer:
    """The add-on's end of the connection: the lines the server sends it."""

    def __init__(self):
        self.lines = []

    def write(self, b: bytes):
        self.lines.extend(x for x in b.decode().split("\n") if x)

    def close(self):
        pass


def add_on(m):
    """A NinjaTrader add-on (16) connected to the running server."""
    nt = NinjaTraderFeed("127.0.0.1", 0, "t")
    nt.writer, nt.stats["version"], nt.stats["connected"] = Writer(), 16, True
    m.feed_objs.append(nt)
    return nt


def ct_ms(d, h, m, s=0):
    return int(datetime(d.year, d.month, d.day, h, m, s, tzinfo=sessions.CT).timestamp() * 1000)


def events(c):
    return [e["message"] for e in c.get("/api/alert-events").json()]


# ---- k-hg10-2: a reconnect's few seconds hold no alert; a new gap seen while a long stretch is filled is sent after ------

@pytest.fixture
def at_open(monkeypatch):
    """A clock at 08:29:58 CT on the latest weekday whose day before was one too (no CME holiday either), running on."""
    cfg = Path(__file__).resolve().parents[2] / "config" / "holidays.json"
    off = {date.fromisoformat(d["date"]) for d in json.loads(cfg.read_text()).get("days", [])} if cfg.exists() else set()
    real = time.time
    now = real()
    day = datetime.fromtimestamp(now, sessions.CT).date() - timedelta(days=1)
    while day.weekday() >= 5 or day in off or (day - timedelta(days=1)) in off or (day - timedelta(days=1)).weekday() >= 5:
        day -= timedelta(days=1)
    shift = [ct_ms(day, 8, 29, 58) / 1000 - now]
    monkeypatch.setattr(time, "time", lambda: real() + shift[0])
    return day, shift


def opening(tmp_path, monkeypatch, at_open, stops_at, answer):
    """ES's gap alerts on; the day before closed at 6000, pre-market at 6040 with its last trade at `stops_at`
    (h, m, s). The add-on says hello at 08:29:58 (what _handle does: asked for every instrument's stretch), ES opens
    at 6050 at 08:30:01 and the add-on answers (`answer`: nothing, the stretch's trades, or nothing 7 minutes later);
    then more trades. The alert events sent."""
    day, shift = at_open
    prev = day - timedelta(days=1)
    st = Store(tmp_path / "chart.db")
    last = ct_ms(day, *stops_at)
    rows = [(ct_ms(prev, 8, 30) + k * 60_000, 6000.0) for k in range(0, 390, 30)]
    rows += [(ct_ms(day, 7, 0) + k * 60_000, 6040.0) for k in range((last - ct_ms(day, 7, 0)) // 60_000 + 1)]
    st.write([Tick("ES", last, 6040.0, 1, 1)], [("ES", Bar(t, px, px, px, px, 1, 0, 0, 1)) for t, px in rows])
    st.set_setting("gap_alerts", {"ES": {**GAP, "on": True, "rth": False}})
    st.close()
    app, client = _client(tmp_path, monkeypatch)
    with client as c:
        m = app.state.market
        c.portal.call((w0 := m.gaps.watches["ES"]).ready.wait)
        nt = add_on(m)
        hello = int(time.time() * 1000)
        asked = c.portal.call(lambda: m.gapfill.requests(hello, live=True, minutes=True, contracts=True))
        assert [g for g in asked if g.startswith("GAP|ES|")] == [f"GAP|ES|{last}|{hello}|live"]

        def trade(px, dt=0):
            c.portal.call(nt._line, m, f"T|ES|{int(time.time() * 1000) + dt}|{px}|1|1\n".encode())
            c.portal.call(asyncio.sleep, 0.05)
        shift[0] += 3   # 08:30:01
        trade(6050.0)
        trade(6050.25, 500)
        held = events(c)
        if answer == "trades":   # the stretch's: pre-market, at 6040
            for k in range(1, 4):
                c.portal.call(nt._line, m, f"X|ES|{last + k * 30_000}|6040|1\n".encode())
        if answer == "late":
            shift[0] += 7 * 60
        c.portal.call(nt._line, m, (b"Z|ES|ticks|3|ok\n" if answer == "trades" else b"Z|ES|minutes|0|ok\n"))
        assert not filling(m, "ES")
        assert (m.gaps.watches["ES"] is not w0) == (answer == "trades")   # loaded again with the fill when it brought any
        c.portal.call(m.gaps.watches["ES"].ready.wait)
        trade(6050.5, 1000)
        trade(6051.0, 2000)
        m.feed_objs.remove(nt)
        return held, events(c)


def test_a_server_restart_just_before_the_rth_open_holds_no_gap_alert(tmp_path, monkeypatch, at_open):
    """The server restarts at 08:29:58 with NinjaTrader running: ES's stored data stops a second before the add-on's
    hello, so the stretch it is asked for is a second long and holds nothing back: the 08:30 open's gap is sent at
    once (before: held until the add-on's empty answer, the engine's NewGap used up meanwhile, never sent)."""
    held, sent = opening(tmp_path, monkeypatch, at_open, (8, 29, 57), "none")
    assert held == sent == ["[ES] GAP UP 50.00 pts | Open: 6050.00 | RTH close: 6000.00"]


@pytest.mark.parametrize("answer", ["none", "trades"])
def test_a_new_gap_seen_while_a_long_stretch_is_filled_is_sent_after(tmp_path, monkeypatch, at_open, answer):
    """NinjaTrader started at 08:29:58, ES's stored data stopping at 08:26: a stretch that holds ES's alerts. The
    open's gap comes meanwhile: nothing is sent then, and once the add-on's answer is in (with nothing, or the
    stretch's pre-market trades, ES's watch loaded again with them) the next trade sends it (before: never)."""
    held, sent = opening(tmp_path, monkeypatch, at_open, (8, 26, 0), answer)
    assert held == [] and sent == ["[ES] GAP UP 50.00 pts | Open: 6050.00 | RTH close: 6000.00"]


def test_a_held_gap_whose_minute_is_no_longer_live_is_not_sent(tmp_path, monkeypatch, at_open):
    """The same, the add-on's answer 7 minutes later: the gap's first minute is no longer live (LIVE_BAR_AGE_MS)."""
    held, sent = opening(tmp_path, monkeypatch, at_open, (8, 26, 0), "late")
    assert held == sent == []


# ---- k-hg10-1: an instrument a futures plugin supplies, put back, is filled by that plugin, not asked of the add-on -------

def wait_for(c, done, s=5.0):
    end = time.monotonic() + s
    while not done() and time.monotonic() < end:
        c.portal.call(asyncio.sleep, 0.05)
    return done()


@pytest.mark.parametrize("ninjatrader", [True, False])
def test_an_instrument_a_plugin_supplies_put_back_has_its_stretch_filled_by_that_plugin(tmp_path, monkeypatch, in_session,
                                                                                         ninjatrader):
    """CL comes from a futures plugin (Your own program, its program connected; given a history() here), with a CL
    session VWAP alert; CL is taken off Instruments and put back two hours later, with the add-on connected or not.
    The add-on is not asked for CL (before: a GAP line it answers with 'not forwarded', which the server drops as
    another feed's, so CL's window never closed and its gap, indicator, study and AMT alerts stayed held until a
    restart); the plugin is asked for the stretch since CL's last stored trade (before: by no one), its answer closes
    the window and CL's alerts load again with it and alert."""
    now = in_session
    app, client = _client(tmp_path, monkeypatch)
    with client as c:
        r = c.put("/api/connections/lines", json={"enabled": True, "port": _port(), "symbols": "CL"})
        assert r.status_code == 200, r.text
        assert c.post("/api/alerts", json={"symbol": "CL", "kind": "study", "study": "vwap"}).status_code == 201
        set_instruments(c, [r for r in file_rows(tmp_path) if r["symbol"] != "CL"])
    last = now - 2 * 3_600_000   # 08:30
    st = Store(tmp_path / "chart.db")
    st.write([Tick("CL", last, 65.0, 1, 1)], [("CL", Bar(last, 65.0, 65.0, 65.0, 65.0, 1, 0, 0, 1))])
    st.close()
    app, client = _client(tmp_path, monkeypatch)
    with client as c:
        m = app.state.market
        assert m.trade_owner == {"CL": "lines"} and "CL" not in m.instruments
        nt = add_on(m) if ninjatrader else None
        lines = next(f for f in m.feed_objs if f.name == "lines")
        assert wait_for(c, lambda: lines.stats.get("note", "").startswith("Listening"))   # (else run() sets it back)
        c.portal.call(lines.connected)   # its program connected
        asked = []

        async def history(sym, start, end):
            asked.append((sym, start, end))
            yield ("trade", start + 60_000, 65.0, 1)
        lines.history = history
        set_instruments(c, file_rows(tmp_path))   # CL back
        assert nt is None or not [x for x in nt.writer.lines if x.startswith("GAP|CL|")], nt.writer.lines
        assert wait_for(c, lambda: "CL" not in m.gapfill.windows)
        assert [a[:2] for a in asked] == [("CL", last)] and now <= asked[0][2] <= int(time.time() * 1000)
        assert not filling(m, "CL") and m.gapfill.health()["CL"]["stored"] == 1
        c.portal.call(m.study_sources.watches["CL"].ready.wait)
        t = int(time.time() * 1000)
        for k, px in enumerate([65.0, 65.5, 66.0, 65.0, 64.0, 65.2, 65.0, 65.6, 64.9]):
            c.portal.call(lines.trade, m, "CL", t + k * 1000, px, 1, 1)
            c.portal.call(asyncio.sleep, 0.05)
        assert any(e.startswith("CL touched session VWAP") for e in events(c)), events(c)
        if nt is not None:
            m.feed_objs.remove(nt)


# ---- k-hg10-3: a kept config is checked on its instrument's hours, and one whose alerts were refused never shows on ------

MIDDAY = {**GAP, "RthStart": 110000, "RthEnd": 120000}   # fine on ES; 10:30-10:30 on CL's 08:00-13:30


def test_a_gap_save_is_checked_on_the_hours_of_one_off_instruments_whose_alerts_are_kept_on(tmp_path, monkeypatch):
    """ES's and CL's gap alerts on; CL (and GC) taken off Instruments. A Gap settings save of 11:00-12:00 on ES, fine
    on every instrument left, answers 400 naming CL, whose kept config would take them (its catalog hours, 08:00-13:30,
    make no session of them), and nothing is stored (before: 200, and CL's alerts silently dead once it was back)."""
    app, client = _client(tmp_path, monkeypatch)
    with client as c:
        for s in ("ES", "CL"):
            assert c.put(f"/api/gaps/alerts/{s}", json={**GAP, "on": True}).status_code == 200
        set_instruments(c, [r for r in file_rows(tmp_path) if r["symbol"] not in ("CL", "GC")])
    app, client = _client(tmp_path, monkeypatch)
    with client as c:
        m = app.state.market
        before = c.portal.call(m.store.get_setting, "gap_alerts")
        r = c.put("/api/gaps/alerts", json=MIDDAY)
        assert r.status_code == 400 and "no session on CL" in r.json()["detail"], r.text
        assert c.portal.call(m.store.get_setting, "gap_alerts") == before == m.gaps.alert_cfg
        assert c.put("/api/gaps/alerts", json={**GAP, "MinimumGapPoints": 2.0}).status_code == 200


def test_gap_alerts_refused_at_their_start_are_shown_off_with_why(tmp_path, monkeypatch):
    """CL's gap alerts kept on with 11:00-12:00 while it was off Instruments (stored by the version before). CL put
    back: its alerts are not started, and GET /api/gaps/alerts, what the page reads, gives them as off with the reason
    (before: on, the Studies panel saying 'Alerts: new gap + fill.' while none would ever fire). A Gap settings save
    that fits CL's hours starts them, shown on again."""
    app, client = _client(tmp_path, monkeypatch)
    with client as c:
        assert c.put("/api/gaps/alerts/ES", json={**GAP, "on": True}).status_code == 200
        set_instruments(c, [r for r in file_rows(tmp_path) if r["symbol"] != "CL"])
    st = Store(tmp_path / "chart.db")
    st.set_setting("gap_alerts", {**st.get_setting("gap_alerts"), "CL": {**MIDDAY, "on": True, "rth": False}})
    st.close()
    app, client = _client(tmp_path, monkeypatch)
    with client as c:
        m = app.state.market
        set_instruments(c, file_rows(tmp_path))   # CL back
        got = c.get("/api/gaps/alerts").json()
        assert "CL" not in got["watches"] and got["alerts"]["ES"]["on"] and "error" not in got["alerts"]["ES"]
        cl = got["alerts"]["CL"]
        assert cl["on"] is False and cl["error"].startswith("RTH 11:00-12:00 is no session on CL"), cl
        assert m.gaps.alert_cfg["CL"]["on"] is True   # kept as stored: on once its times fit
        r = c.put("/api/gaps/alerts", json=GAP)
        assert r.status_code == 200 and r.json()["alerts"]["CL"] == {**GAP, "on": True, "rth": False}
        assert "CL" in c.get("/api/gaps/alerts").json()["watches"]


PAGE_JS = """
const els = {};
const $ = (q) => (els[q] ||= { textContent: '', checked: true, value: '' });
const gapSpecOk = () => true, indReady = () => true, local = { set() {} };
let gapDays = 90;
const S = { symbol: 'CL', rth: false, st: { gap: true }, gaps: { gaps: [] }, gapError: '', instruments: { CL: { rth: ['08:00', '13:30'] } },
  gapAlerts: { CL: ALERT },
  gapSet: { RthStart: 83000, RthEnd: 150000, MinimumGapPoints: 0, AlertOnNewGap: true, AlertOnGapFill: true, EnabledAnalysis: true, days: 90 } };
/*APP*/
renderGapMenu();
console.log(JSON.stringify({ info: $('#gap-info').textContent, checked: $('#gap-alerts').checked }));
"""


@pytest.mark.skipif(not shutil.which("node"), reason="needs node")
def test_the_studies_panel_says_why_gap_alerts_are_off():
    """The page's Gap line with CL's alerts as the server gives them when they were refused: off, with the reason."""
    from test_hunt_web_chart import run_app
    why = "RTH 11:00-12:00 is no session on CL: its regular hours (08:00-13:30 CT) move the start and end to the same time."
    parts = ["hhmm", "gapRth", "renderGapInfo", "renderGapMenu"]
    out = run_app(parts, PAGE_JS.replace("ALERT", json.dumps({**MIDDAY, "on": False, "rth": False, "error": why})))
    assert not out["checked"] and out["info"].endswith(f" Alerts off: {why}"), out
    out = run_app(parts, PAGE_JS.replace("ALERT", json.dumps({**GAP, "on": True, "rth": False})))
    assert out["checked"] and out["info"].endswith(" Alerts: new gap + fill."), out


# ---- k-hg10-4: RTH times an instrument's own hours turn round are refused too ---------------------------------------------

def test_gap_times_an_instruments_hours_turn_round_are_refused(tmp_path, monkeypatch):
    """Instruments ES, NQ and GC (no CL). 11:00-12:00 is 09:50-09:30 on GC's 07:20-12:30: a session through the night,
    nothing like the hour entered. A Gap settings save, switching GC's alerts on and GC's Gap Analysis with them answer
    400 naming GC, as do 06:30-07:10 (06:30-06:00 on GC); nothing is stored (before: 200 everywhere, GC's alerts
    firing every morning at 09:50). An overnight session entered must stay one: 12:00-11:45 is 10:30-11:15 on CL."""
    from types import SimpleNamespace

    from chartsrv.gaps_service import check_hours
    app, client = _client(tmp_path, monkeypatch)
    with client as c:
        m = app.state.market
        assert c.put("/api/gaps/alerts/GC", json={**GAP, "on": True}).status_code == 200
        set_instruments(c, [r for r in file_rows(tmp_path) if r["symbol"] != "CL"])
    app, client = _client(tmp_path, monkeypatch)
    with client as c:
        m = app.state.market
        before = dict(m.gaps.alert_cfg)
        for times in ((110000, 120000), (63000, 71000)):
            body = {**GAP, "RthStart": times[0], "RthEnd": times[1]}
            for r in (c.put("/api/gaps/alerts", json=body), c.put("/api/gaps/alerts/GC", json={**body, "on": True}),
                      c.get("/api/gaps", params={"symbol": "GC", "spec": "5m", "RthStart": times[0], "RthEnd": times[1]})):
                assert r.status_code == 400 and "no session on GC" in r.json()["detail"] and "past each other" in r.json()["detail"], r.text
            assert c.get("/api/gaps", params={"symbol": "ES", "spec": "5m", "RthStart": times[0], "RthEnd": times[1]}).status_code == 200
        assert m.gaps.alert_cfg == before == c.portal.call(m.store.get_setting, "gap_alerts")
    s = {**GAP, "RthStart": 120000, "RthEnd": 114500}
    with pytest.raises(ValueError, match=r"no session on CL: .* past each other \(10:30-11:15\)"):
        check_hours(s, {"CL": SimpleNamespace(rth=("08:00", "13:30"))})
    check_hours(s, {"ES": SimpleNamespace(rth=("08:30", "15:00"))})
    check_hours({**GAP, "RthStart": 180000, "RthEnd": 83000}, {"CL": SimpleNamespace(rth=("08:00", "13:30"))})   # overnight on both
