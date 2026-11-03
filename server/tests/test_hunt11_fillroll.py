"""Round 11 of the bug hunt, the round-10 put-back fill (FeedPlugin.fill_added) and feed changes: an instrument a futures
plugin supplies put back after its contract rolled has the roll recorded and its stretch filled from the new
contract's sessions only; one moved from NinjaTrader to a plugin while the add-on's history for it is still coming
has NinjaTrader's window closed; and a plugin whose Futures field names an instrument taken off can still be saved."""
import asyncio
import time
from datetime import datetime

from chartsrv import sessions
from chartsrv.gapfill import ROLLED, filling
from chartsrv.market import Client
from chartsrv.models import Bar, Tick
from chartsrv.store import Store
from test_feed_plugins import _port
from test_hunt7_drawings import _client, set_instruments
from test_hunt10_hold import add_on, ct_ms, events, wait_for
from test_instruments_edit import file_rows


# ---- k-fl11-1: a plugin's instrument put back after a roll while it was off: the roll recorded, its fill trimmed ----------

def stored_cl(tmp_path, last: int, contract: str):
    st = Store(tmp_path / "chart.db")
    st.write([Tick("CL", last, 65.0, 1, 1)], [("CL", Bar(last // 60_000 * 60_000, 65.0, 65.0, 65.0, 65.0, 1, 0, 0, 1))])
    st.set_setting("contracts", {"CL": contract})
    st.close()


def test_a_plugins_instrument_put_back_after_a_roll_while_it_was_off_has_the_roll_recorded(tmp_path, monkeypatch, in_session):
    """CL comes from Your own program (given a history() of the new contract, 1.00 below the old one); it is taken off
    Instruments with its last trade on CL 12-26 at 08:30. While CL is off the program says it is on CL 01-27 (dropped
    then: CL is no instrument). CL is put back at 10:30: the roll is recorded then (before: never, until the plugin's
    next connect sent a 'Contract roll' days late and lost that restart's own stretch), and the stretch asked is the
    new contract's sessions only, none of its prices stored in the old contract's session (before: the 2 hours since
    08:30, the old contract's session, filled with the new one's prices)."""
    now = in_session
    app, client = _client(tmp_path, monkeypatch)
    with client as c:
        r = c.put("/api/connections/lines", json={"enabled": True, "port": _port(), "symbols": "CL"})
        assert r.status_code == 200, r.text
        set_instruments(c, [r for r in file_rows(tmp_path) if r["symbol"] != "CL"])
    last = now - 2 * 3_600_000
    stored_cl(tmp_path, last, "CL 12-26")
    app, client = _client(tmp_path, monkeypatch)
    with client as c:
        m = app.state.market
        sent = []

        async def send(title, text, *a, **k):
            sent.append((title, text))
        m.notifier.send = send
        lines = next(f for f in m.feed_objs if f.name == "lines")
        assert wait_for(c, lambda: lines.stats.get("note", "").startswith("Listening"))   # (else run() sets it back)
        asked = []

        async def history(sym, start, end):
            asked.append((sym, start, end))
            t = (start // 60_000 + 1) * 60_000
            while t < end:
                yield ("trade", t + 1000, 64.0, 1)
                t += 60_000
        lines.history = history
        c.portal.call(lines.connected)   # its program connected ...
        c.portal.call(lines.line, m, ["C", "CL", "CL 01-27"])   # ... on the new contract
        assert m.contracts.get("CL") == "CL 12-26" and not sent   # (CL is off Instruments: nothing to roll yet)
        set_instruments(c, file_rows(tmp_path))   # CL back
        assert wait_for(c, lambda: asked and "CL" not in m.gapfill.windows)
        c.portal.call(asyncio.sleep, 0.3)
        assert m.contracts["CL"] == "CL 01-27" and c.portal.call(m.store.get_setting, "contracts")["CL"] == "CL 01-27"
        assert [t for t, _ in sent] == ["Contract roll"] and "(was CL 12-26)" in sent[0][1]
        assert c.portal.call(m.store.get_setting, ROLLED).get("CL")
        old_session_ends = sessions.next_session_start(sessions.session_start(last)) - 1
        assert [a[:2] for a in asked] == [("CL", old_session_ends)]

        async def new_prices_in_the_old_session():
            m.flush_for_read()
            return m.store.db.execute("SELECT COUNT(*) FROM bars_1m WHERE symbol='CL' AND t>? AND t<? AND c=64.0",
                                      (last, old_session_ends)).fetchone()[0]
        assert c.portal.call(new_prices_in_the_old_session) == 0
        t = int(time.time() * 1000)
        for k in range(5):   # its live trades, on the new contract
            assert c.portal.call(lines.trade, m, "CL", t + k * 1000, 64.0, 1, 1)
    # the next restart, 10 minutes down: the program's C line is no roll (before: a late 'Contract roll', which moved
    # this restart's window to the next session, so its 10 minutes were never asked)
    real = time.time
    monkeypatch.setattr(time, "time", lambda: real() + 600)
    app, client = _client(tmp_path, monkeypatch)
    with client as c:
        m = app.state.market
        sent = []
        m.notifier.send = send
        lines = next(f for f in m.feed_objs if f.name == "lines")
        assert wait_for(c, lambda: lines.stats.get("note", "").startswith("Listening"))
        asked = []
        lines.history = history
        c.portal.call(lines.connected)
        c.portal.call(lines.line, m, ["C", "CL", "CL 01-27"])
        assert wait_for(c, lambda: asked and "CL" not in m.gapfill.windows)
        assert [a[:2] for a in asked] == [("CL", t + 4000)] and not sent


# ---- k-fl11-2: an instrument moved from NinjaTrader to a plugin while the add-on's history for it is coming -------------

def test_an_instrument_moved_to_a_plugin_while_ninjatraders_history_comes_has_that_window_closed(tmp_path, monkeypatch, in_session):
    """CL's stored data is 3 hours old, with a CL session VWAP alert; the add-on connects and is asked for CL's
    stretch, and its live CL trades come (stored) while its answer is still coming. Then CL is moved to Your own
    program. NinjaTrader's window for CL is closed then (before: never, its Z line is dropped as another feed's and
    the plugin's own open() found nothing to fill, so CL's gap, indicator, study and AMT alerts stayed held until a
    restart): CL's alerts send again on the plugin's trades."""
    now = in_session
    last = now - 3 * 3_600_000
    stored_cl(tmp_path, last, "CL 12-26")
    app, client = _client(tmp_path, monkeypatch)
    with client as c:
        m = app.state.market
        assert c.post("/api/alerts", json={"symbol": "CL", "kind": "study", "study": "vwap"}).status_code == 201
        nt = add_on(m)
        hello = int(time.time() * 1000)
        assert any(g.startswith("GAP|CL|") for g in c.portal.call(lambda: m.gapfill.requests(hello, live=True, minutes=True,
                                                                                             contracts=True)))
        for k in range(3):   # NinjaTrader's live CL trades; its answer for the stretch is still coming
            c.portal.call(nt._line, m, f"T|CL|{int(time.time() * 1000) + k}|65.5|1|1\n".encode())
        assert filling(m, "CL")
        r = c.put("/api/connections/lines", json={"enabled": True, "port": _port(), "symbols": "CL"})
        assert r.status_code == 200, r.text
        assert "CL" not in m.gapfill.windows and not filling(m, "CL")
        assert m.gapfill.status["CL"]["state"] == "dropped"
        lines = next(f for f in m.feed_objs if f.name == "lines")
        assert wait_for(c, lambda: lines.stats.get("note", "").startswith("Listening"))
        c.portal.call(lines.connected)   # its program connected
        c.portal.call(nt._line, m, b"Z|CL|ticks|0|ok\n")   # the add-on's answer: dropped as another feed's
        c.portal.call(asyncio.sleep, 0.5)
        assert not filling(m, "CL")
        c.portal.call(m.study_sources.watches["CL"].ready.wait)
        t = int(time.time() * 1000)
        for k, px in enumerate([65.0, 65.5, 66.0, 65.0, 64.0, 65.2, 65.0, 65.6, 64.9]):
            c.portal.call(lines.trade, m, "CL", t + k * 1000, px, 1, 1)
            c.portal.call(asyncio.sleep, 0.05)
        assert any(e.startswith("CL touched session VWAP") for e in events(c)), events(c)
        m.feed_objs.remove(nt)


def test_an_instrument_moved_to_a_plugin_between_ninjatraders_holes_has_the_filled_ones_reloaded(tmp_path, monkeypatch, in_session):
    """(k-fo12-3) CL has three holes today, with a CL chart open; the add-on connects, fills the first (09:00-09:29)
    and is asked for the second when CL is moved to Your own program (no history, nothing to fill). The second's
    window and the third, still to ask, are dropped, and what NinjaTrader filled reloads then: the open chart is told
    to reload and loads the first hole's minutes (before the k-fl11-2 fix: nothing settled CL, so its charts and
    alert watches showed the hole until the add-on disconnected or the chart was opened again)."""
    day = datetime.fromtimestamp(in_session / 1000, sessions.CT).date()
    spans = [(8, 0, 60), (9, 30, 15), (10, 0, 15), (10, 22, 8)]   # holes 09:00-09:29, 09:45-09:59, 10:15-10:21
    minutes = [ct_ms(day, h, mi) + k * 60_000 for h, mi, n in spans for k in range(n)]
    st = Store(tmp_path / "chart.db")
    st.write([Tick("CL", t + 1000, 65.0, 1, 1) for t in minutes[:-1]] + [Tick("CL", minutes[-1] + 30_000, 65.0, 1, 1)],
             [("CL", Bar(t, 65.0, 65.0, 65.0, 65.0, 1, 0, 0, 1)) for t in minutes])
    st.close()

    async def drain(cl):
        out = []
        while not cl.outbox.empty():
            out.append(cl.outbox.get_nowait())
        return out

    def hole_minutes(c, m):   # what a chart opened now loads
        cl = Client()
        c.portal.call(m.subscribe, cl, 1, "CL", "1m")
        bars = next(x for x in c.portal.call(drain, cl) if x["type"] == "history")["bars"]
        return sum(ct_ms(day, 9, 0) <= b["t"] < ct_ms(day, 9, 30) for b in bars)

    app, client = _client(tmp_path, monkeypatch)
    with client as c:
        m = app.state.market
        chart = Client()
        c.portal.call(m.subscribe, chart, 1, "CL", "1m")
        assert [x["type"] for x in c.portal.call(drain, chart)] == ["history"]
        nt = add_on(m)
        hello = int(time.time() * 1000)
        c.portal.call(lambda: m.gapfill.requests(hello, live=True, minutes=True, contracts=True))
        assert len(m.gapfill.queue["CL"]) == 3
        c.portal.call(nt._line, m, b"Z|CL|ticks|0|ok\n")   # the stretch up to now: nothing
        assert nt.writer.lines[-1].endswith(f"|{ct_ms(day, 9, 30)}")   # the first hole
        for k in range(30):   # NinjaTrader's trades for it
            c.portal.call(nt._line, m, f"X|CL|{ct_ms(day, 9, k, 30)}|65.5|2\n".encode())
        c.portal.call(nt._line, m, b"Z|CL|ticks|30|ok\n")
        assert nt.writer.lines[-1].endswith(f"|{ct_ms(day, 10, 0)}") and "CL" in m.gapfill.windows   # the second, asked
        assert "CL" in m.gapfill.changed and not c.portal.call(drain, chart)   # (reloads once the holes are done)
        r = c.put("/api/connections/lines", json={"enabled": True, "port": _port(), "symbols": "CL"})
        assert r.status_code == 200, r.text
        assert "CL" not in m.gapfill.windows and not m.gapfill.queue.get("CL")
        assert {"type": "reload", "id": 1} in c.portal.call(drain, chart)
        assert hole_minutes(c, m) == 30
        m.feed_objs.remove(nt)


# ---- k-fl11-3: a plugin whose Futures field names an instrument taken off can still be saved, turned off included -------

def test_a_plugin_whose_futures_name_an_instrument_taken_off_can_still_be_turned_off_and_saved(tmp_path, monkeypatch):
    """Your own program supplies CL and GC; CL is taken off Instruments (and the server restarts). Its card can still be
    turned off, given a new token and turned on again (before: every save refused with 'Futures: CL is not in
    Connections > App > Instruments', so the feed could not be turned off); a Futures field that adds an instrument not
    on Instruments is still refused."""
    app, client = _client(tmp_path, monkeypatch)
    with client as c:
        assert c.put("/api/connections/lines", json={"enabled": True, "port": _port(), "symbols": "CL,GC"}).status_code == 200
        set_instruments(c, [r for r in file_rows(tmp_path) if r["symbol"] != "CL"])
    app, client = _client(tmp_path, monkeypatch)
    with client as c:
        m = app.state.market
        assert "CL" not in m.instruments and any(f.name == "lines" for f in m.feed_objs)
        r = c.put("/api/connections/lines", json={"enabled": False})
        assert r.status_code == 200, r.text
        assert not any(f.name == "lines" for f in m.feed_objs) and m.trade_owner == {}
        r = c.put("/api/connections/lines", json={"token": "newtoken1"})
        assert r.status_code == 200, r.text
        r = c.put("/api/connections/lines", json={"enabled": True, "symbols": "GC,CL"})
        assert r.status_code == 200, r.text
        assert m.trade_owner == {"CL": "lines", "GC": "lines"}
        r = c.put("/api/connections/lines", json={"symbols": "CL,GC,ZZ"})
        assert r.status_code == 400 and r.json()["detail"] == "Futures: ZZ is not in Connections > App > Instruments", r.text
