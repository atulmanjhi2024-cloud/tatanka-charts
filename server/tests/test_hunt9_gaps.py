"""Round 9 of the bug hunt, the round-8 gap alert fixes: Gap RTH times that an instrument's own hours turn into no
session (ES's 11:00-12:00 is 10:30-10:30 on CL's 08:00-13:30) are refused before anything is stored, and one stored
before never stops the server, a request or an instrument put back; and an instrument put back during RTH sends no
alert from the history that stopped when it was taken off while the add-on's fill of that stretch is still on."""
import asyncio
import logging
import time
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from chartsrv import sessions
from chartsrv.feeds.ninjatrader import NinjaTraderFeed
from chartsrv.models import Bar, Tick
from chartsrv.store import Store
from test_hunt7_drawings import _client, set_instruments
from test_instruments_edit import file_rows

GAP = {"new": True, "fill": True, "RthStart": 83000, "RthEnd": 150000, "MinimumGapPoints": 0.0}
MIDDAY = {**GAP, "RthStart": 110000, "RthEnd": 120000}   # fine on ES; 10:30-10:30 on CL's 08:00-13:30


def stored_gap_alerts(c, m):
    return c.portal.call(m.store.get_setting, "gap_alerts")


class Told:
    """A feed with the NinjaTrader add-on's hook for chart futures added: what it was told."""
    name = "told"

    def __init__(self):
        self.told = []

    def charts_added(self, market, syms):
        self.told.append(list(syms))


def watching(m, sent):
    """The feed above added to the running server, and the types of what open pages are sent from now on."""
    feed = Told()
    m.feed_objs.append(feed)
    real = m.broadcast
    m.broadcast = lambda msg: (sent.append(msg.get("type")), real(msg))[1]
    return feed


# ---- k-gd9-1: Gap RTH times that make no session on an instrument's own hours ------------------------------------------

def test_gap_times_that_make_no_session_on_an_instruments_hours_are_refused_and_nothing_is_stored(tmp_path, monkeypatch):
    """ES's and CL's gap alerts on. A Gap settings save of 11:00-12:00 for every symbol, or for CL, and switching CL's
    alerts on with them, answer 400 naming CL, and nothing is stored (before: the save's 400 came after the store, and
    the next start raised). For ES alone (a save on an ES chart, switching ES's alerts on) they are ES's only since
    round 14, so CL's hours have no say: taken, CL's config left as it was. Switching alerts off is never refused.
    CL's Gap Analysis with them answers a 400 that says why, not a server error; ES's is drawn. The server starts
    again, ES's alerts running on them."""
    app, client = _client(tmp_path, monkeypatch)
    with client as c:
        m = app.state.market
        for s in ("ES", "CL"):
            assert c.put(f"/api/gaps/alerts/{s}", json={**GAP, "on": True}).status_code == 200
        before = stored_gap_alerts(c, m)
        for path, body in (("/api/gaps/alerts", MIDDAY), ("/api/gaps/alerts?symbol=CL", MIDDAY),
                           ("/api/gaps/alerts/CL", {**MIDDAY, "on": True})):
            r = c.put(path, json=body)
            assert r.status_code == 400 and "CL" in r.json()["detail"], (path, r.text)
            assert stored_gap_alerts(c, m) == before == m.gaps.alert_cfg
        for path, body in (("/api/gaps/alerts?symbol=ES", MIDDAY), ("/api/gaps/alerts/ES", {**MIDDAY, "on": True})):
            assert c.put(path, json=body).status_code == 200, path
        got = stored_gap_alerts(c, m)
        assert (got["ES"]["RthStart"], got["ES"]["RthEnd"]) == (110000, 120000) and got["CL"] == before["CL"]
        q = {"spec": "5m", "RthStart": 110000, "RthEnd": 120000}
        r = c.get("/api/gaps", params={**q, "symbol": "CL"})
        assert r.status_code == 400 and "CL" in r.json()["detail"], r.text
        assert c.get("/api/gaps", params={**q, "symbol": "ES"}).status_code == 200
        assert c.put("/api/gaps/alerts/CL", json={**MIDDAY, "on": False}).status_code == 200   # off: always
        assert sorted(m.gaps.watches) == ["ES"]
    app, client = _client(tmp_path, monkeypatch)
    with client as c:
        assert sorted(app.state.market.gaps.watches) == ["ES"]
        assert app.state.market.gaps.watches["ES"].cfg["RthStart"] == 110000


def test_a_database_that_holds_such_a_config_starts(tmp_path, monkeypatch, caplog):
    """Stored by the version before: ES's and CL's gap alerts on with 11:00-12:00. The server starts (before: a
    ValueError out of Market.start, the container restarting in a loop), ES's alerts run and CL's are left off with
    a line in the log naming it; a gap fill of CL (history_changed) and switching NQ's alerts on carry on; a Gap
    settings save that fits every instrument starts CL's."""
    st = Store(tmp_path / "chart.db")
    st.set_setting("gap_alerts", {s: {**MIDDAY, "on": True, "rth": False} for s in ("ES", "CL")})
    st.close()
    app, client = _client(tmp_path, monkeypatch)
    with caplog.at_level(logging.WARNING, logger="chartsrv.gaps_service"), client as c:
        m = app.state.market
        assert sorted(m.gaps.watches) == ["ES"]
        assert any("CL" in r.getMessage() for r in caplog.records if r.name == "chartsrv.gaps_service"), caplog.text
        c.portal.call(m.history_changed, "CL")
        assert c.put("/api/gaps/alerts/NQ", json={**GAP, "on": True}).status_code == 200
        assert sorted(m.gaps.watches) == ["ES", "NQ"]
        assert c.put("/api/gaps/alerts", json=GAP).status_code == 200
        assert sorted(m.gaps.watches) == ["CL", "ES", "NQ"]


def test_an_instrument_put_back_with_such_a_kept_config_is_told_to_the_feed_and_the_server_starts(tmp_path, monkeypatch):
    """CL's and ES's gap alerts on; CL taken off Instruments; 11:00-12:00 saved meanwhile by the version before, which
    checked only the instruments left (CL's kept config took them for when it is back; now such a save is refused:
    test_hunt10_hold). CL put back: the save answers 200, the add-on is told to forward CL, open pages hear, ES's gap
    alerts run and CL's stay off; the server starts again (before: a 500 half-way, the add-on never told, a second
    save finding nothing to add, and every start raising)."""
    app, client = _client(tmp_path, monkeypatch)
    with client as c:
        for s in ("ES", "CL"):
            assert c.put(f"/api/gaps/alerts/{s}", json={**GAP, "on": True}).status_code == 200
        set_instruments(c, [r for r in file_rows(tmp_path) if r["symbol"] != "CL"])
    st = Store(tmp_path / "chart.db")
    st.set_setting("gap_alerts", {s: {**v, **MIDDAY} for s, v in st.get_setting("gap_alerts").items()})
    st.close()
    app, client = _client(tmp_path, monkeypatch)
    with client as c:
        m, sent = app.state.market, []
        feed = watching(m, sent)
        assert m.gaps.alert_cfg["CL"]["RthStart"] == 110000
        set_instruments(c, file_rows(tmp_path))   # CL back
        assert feed.told == [["CL"]] and "instruments" in sent
        assert "CL" in m.instruments and sorted(m.gaps.watches) == ["ES"]
        m.feed_objs.remove(feed)
    app, client = _client(tmp_path, monkeypatch)
    with client as c:
        assert sorted(app.state.market.gaps.watches) == ["ES"]


def test_the_feed_is_told_of_an_instrument_put_back_before_its_alerts_load(tmp_path, monkeypatch):
    """Whatever goes wrong loading the alerts kept for an instrument put back (here its study alert watch), the
    add-on has been told to forward it and open pages have heard, so it is live all the same."""
    app, client = _client(tmp_path, monkeypatch)
    with client as c:
        assert c.post("/api/alerts", json={"symbol": "CL", "kind": "study", "study": "vwap"}).status_code == 201
        set_instruments(c, [r for r in file_rows(tmp_path) if r["symbol"] != "CL"])
    app, client = _client(tmp_path, monkeypatch)
    with client as c:
        m, sent = app.state.market, []
        feed = watching(m, sent)

        def broken():
            raise RuntimeError("study alert watch")
        monkeypatch.setattr(m.study_sources, "ensure", broken)
        with pytest.raises(RuntimeError):
            c.put("/api/instruments/config", json={"instruments": file_rows(tmp_path)})
        assert feed.told == [["CL"]] and "instruments" in sent
        m.feed_objs.remove(feed)


# ---- k-gd9-2: the alerts of an instrument put back wait for the add-on's fill of the stretch it was off -----------------

def ct_ms(d, h, m):
    return int(datetime(d.year, d.month, d.day, h, m, tzinfo=sessions.CT).timestamp() * 1000)


def weekdays_before(day, n):
    out, d = [], day
    while len(out) < n:
        d -= timedelta(days=1)
        if d.weekday() < 5:
            out.append(d)
    return out[::-1]


def rth_bars(d, price, every=30):
    return [(ct_ms(d, 8, 30) + k * 60_000, price) for k in range(0, 390, every)]


class Writer:
    """The add-on's end of the connection: the lines the server sends it."""

    def __init__(self):
        self.lines = []

    def write(self, b: bytes):
        self.lines.extend(x for x in b.decode().split("\n") if x)

    def close(self):
        pass


def test_an_instrument_put_back_during_rth_sends_no_alert_before_the_add_ons_fill(tmp_path, monkeypatch, in_session):
    """NQ's gap alerts on and two intrabar indicator alerts, EMA 9 (5m) above 20000 and above 21100. NQ's stored
    history: a day at 20000, the next opening with a gap down at 19900 and staying there; NQ is taken off Instruments
    then. Days later it is put back at 10:30 CT with the add-on connected: told to forward NQ and asked for the stretch
    since. Its first live trades at 21000 come before that history and send nothing (before: a GAP UP against the
    RTH close of days ago, a FILLED for the gap down long filled, and EMA(9) 20120 crossed above 20000, a value the
    chart never shows). The fill lands (the days since at 21000): the alerts load again with it and work as usual: a
    trade at 22000 crosses EMA 9 above 21100, one at 19900 fills the gap up the first day back opened with."""
    now = in_session
    today = datetime.fromtimestamp(now / 1000, sessions.CT).date()
    days = weekdays_before(today, 12)
    app, client = _client(tmp_path, monkeypatch)
    with client as c:
        st = Store(tmp_path / "chart.db")
        rows = rth_bars(days[0], 20000.0) + rth_bars(days[1], 19900.0)
        st.write([Tick("NQ", ct_ms(days[1], 14, 59), 19900.0, 1, 1)],
                 [("NQ", Bar(t, px, px, px, px, 1, 0, 0, 1)) for t, px in rows])
        st.close()
        assert c.put("/api/gaps/alerts/NQ", json={**GAP, "on": True}).status_code == 200
        for v in (20000, 21100):
            r = c.post("/api/alerts", json={"symbol": "NQ", "kind": "ind", "a": {"ind": "ema", "len": 9}, "b": {"value": v},
                                            "spec": "5m", "direction": "above", "on": "bar", "repeat": True})
            assert r.status_code == 201, r.text
        set_instruments(c, [r for r in file_rows(tmp_path) if r["symbol"] not in ("NQ", "MNQ")])
    app, client = _client(tmp_path, monkeypatch)
    with client as c:
        m = app.state.market
        nt = NinjaTraderFeed("127.0.0.1", 0, "t")
        nt.writer, nt.stats["version"], nt.stats["connected"] = Writer(), 16, True
        m.feed_objs.append(nt)

        def ready():
            for w in [m.gaps.watches["NQ"], *(w for k, w in m.ind_alerts.watches.items() if k[0] == "NQ")]:
                c.portal.call(w.ready.wait)

        def trade(ts, px):
            c.portal.call(nt._line, m, f"T|NQ|{ts}|{px}|1|1\n".encode())
            c.portal.call(asyncio.sleep, 0.1)

        def sent():
            return sorted(e["message"] for e in c.get("/api/alert-events").json())

        set_instruments(c, file_rows(tmp_path))   # NQ back
        assert any(x.startswith("GAP|NQ|") for x in nt.writer.lines), nt.writer.lines   # its history asked for
        ready()
        for k in range(4):   # the first live trades
            trade(now + k * 1000, 21000.0)
        assert sent() == []

        # the add-on's answer: the days since NQ was off and today's first two hours, at 21000, as 1-minute bars
        fill = [t for d in days[2:] for t, _ in rth_bars(d, 0)] + [ct_ms(today, 8, 30) + k * 60_000 for k in range(0, 120, 10)]
        for t in fill:
            c.portal.call(nt._line, m, f"Y|NQ|{t}|21000|21000|21000|21000|1\n".encode())
        c.portal.call(nt._line, m, f"Z|NQ|minutes|{len(fill)}|ok\n".encode())
        ready()
        trade(now + 60_000, 21000.0)
        assert sent() == []
        trade(now + 120_000, 22000.0)
        trade(now + 180_000, 19900.0)
        ema, gap = sent()
        assert ema == "NQ 5m: EMA(9) 21200.00 crossed above 21100 (last 22000.00)"
        assert gap.startswith("[NQ] GAP UP FILLED | 1100.00 pts in "), gap
        m.feed_objs.remove(nt)


def test_study_and_amt_alerts_wait_for_the_fill_of_the_stretch_before_the_live_trades(tmp_path, monkeypatch, in_session):
    """The same hold for the other two kinds whose watches load from stored history: while the stretch up to the live
    trades is being filled, a study alert sees no level and AMT notices are counted as sent but not sent; a hole
    inside the stored data being filled holds nothing; once the fill is done they alert as usual."""
    now = in_session
    app, client = _client(tmp_path, monkeypatch)
    with client as c:
        m = app.state.market
        assert c.post("/api/alerts", json={"symbol": "ES", "kind": "study", "study": "vwap"}).status_code == 201
        c.portal.call(m.study_sources.watches["ES"].ready.wait)
        c.portal.call(m.on_tick, Tick("ES", now, 6000.0, 1, 1))
        p = {"study": "vwap"}
        assert m.study_sources.value("ES", p, now)[0] == 6000.0

        def notify():
            c.portal.call(m.amt._notify, "ES", [SimpleNamespace(Text="AMT notice")], 6000.0)
            return [e["message"] for e in c.get("/api/alert-events").json()]

        # the add-on asked, 5 minutes after ES's last trade, for its history up to its live trades (a stretch of a
        # second or two holds nothing: test_hunt10_hold)
        c.portal.call(lambda: m.gapfill.open("ES", now + 5 * 60_000, least=0))
        assert m.study_sources.value("ES", p, now) is None
        assert notify() == []
        c.portal.call(m.gapfill.done, "ES", "none", 0, "nothing")
        c.portal.call(m.gapfill.open_hole, "ES", now - 3_600_000, now - 3_000_000)   # a hole: nothing held
        assert m.study_sources.value("ES", p, now)[0] == 6000.0
        assert notify() == ["[ES] AMT notice | close 6000.00"]


def test_a_fill_the_add_on_disconnected_from_holds_no_alerts(tmp_path, monkeypatch):
    """The add-on disconnects before it answers (NinjaTrader closed, or the futures feed switched to Rithmic in place):
    the stretches it was asked for close with it (asked again at its next connect), so no alert waits for an answer
    that won't come while another feed's trades arrive."""
    from chartsrv.gapfill import filling
    from test_review_fixes import build_market

    async def go():
        m, _ = build_market(tmp_path, monkeypatch, feed="ninjatrader")
        now = int(time.time() * 1000)
        m.store.write([Tick("ES", now - 3_600_000, 7000.5, 2, 1)], [])
        feed = NinjaTraderFeed("127.0.0.1", 0, "")
        server = await asyncio.start_server(lambda r, w: feed._handle(m, r, w), "127.0.0.1", 0)
        r, w = await asyncio.open_connection("127.0.0.1", server.sockets[0].getsockname()[1])
        w.write(b"HELLO||16\n")
        await w.drain()
        while not (await asyncio.wait_for(r.readline(), 3)).decode().startswith("GAP|ES|"):
            pass
        asked = filling(m, "ES")
        w.close()
        await asyncio.sleep(0.3)
        server.close()
        return asked, filling(m, "ES"), m
    asked, held, m = asyncio.run(go())
    assert asked and not held and m.gapfill.health()["ES"]["state"] == "dropped"
