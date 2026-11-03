"""Round 12 of the bug hunt, which feed supplies an instrument (Market._owners_changed): one handed back from a futures
plugin to NinjaTrader has the add-on asked for its stretch at once, its alerts held meanwhile; and a hole the feed
before had nothing for is asked of the feed that takes the instrument over."""
import asyncio
import time
from datetime import datetime, timedelta

from chartsrv import sessions
from chartsrv.gapfill import ASKED, PROBE_MS, filling
from chartsrv.models import Bar, Tick
from chartsrv.store import Store
from test_feed_plugins import _port
from test_hunt7_drawings import _client
from test_hunt10_hold import add_on, events, wait_for

GAP = {"new": True, "fill": True, "RthStart": 80000, "RthEnd": 133000, "MinimumGapPoints": 0.0}


def ct(d, h, m, s=0):
    return int(datetime(d.year, d.month, d.day, h, m, s, tzinfo=sessions.CT).timestamp() * 1000)


def bars_between(c, m, a, b):
    async def go():
        m.flush_for_read()
        return m.store.db.execute("SELECT COUNT(*) FROM bars_1m WHERE symbol='CL' AND t>? AND t<?", (a, b)).fetchone()[0]
    return c.portal.call(go)


# ---- k-fo12-1: an instrument handed back from a plugin to NinjaTrader has its stretch asked of the add-on ---------------

def test_an_instrument_handed_back_to_ninjatrader_has_its_stretch_asked_and_its_alerts_held(tmp_path, monkeypatch, in_session):
    """CL comes from Your own program, whose program went away at 07:30 CT; CL has gap alerts on (RTH 08:00-13:30).
    At 10:30 the plugin is turned off, so NinjaTrader (its add-on connected) supplies CL again. The add-on is asked
    for CL's stretch since its last stored trade at once, and CL's alerts are held until it comes (before: no GAP
    line, so 07:30-10:30 stayed empty until the add-on's next connect, and NinjaTrader's first live CL trade sent a
    GAP UP 'opening' at 10:30 at its price); with the stretch filled, no gap opens at the 10:30 price."""
    now = in_session
    day = datetime.fromtimestamp(now / 1000, sessions.CT).date()
    prev = day - timedelta(days=1)
    while prev.weekday() >= 5:
        prev -= timedelta(days=1)
    app, client = _client(tmp_path, monkeypatch)
    with client as c:
        r = c.put("/api/connections/lines", json={"enabled": True, "port": _port(), "symbols": "CL"})
        assert r.status_code == 200, r.text
    st = Store(tmp_path / "chart.db")
    rows = [(ct(prev, 8, 0) + k * 60_000, 65.0) for k in range(331)]   # the day before's RTH, at 65
    rows += [(ct(day, 7, 0) + k * 60_000, 65.2) for k in range(31)]    # today 07:00-07:30, at 65.2
    last = ct(day, 7, 30, 10)
    st.write([Tick("CL", last, 65.2, 1, 1)], [("CL", Bar(t, px, px, px, px, 1, 0, 0, 1)) for t, px in rows])
    st.set_setting("gap_alerts", {"CL": {**GAP, "on": True, "rth": True}})
    st.close()
    app, client = _client(tmp_path, monkeypatch)
    with client as c:
        m = app.state.market
        assert m.trade_owner == {"CL": "lines"}
        c.portal.call(m.gaps.watches["CL"].ready.wait)
        nt = add_on(m)
        hello = int(time.time() * 1000)
        assert not [g for g in c.portal.call(lambda: m.gapfill.requests(hello, live=True, minutes=True, contracts=True))
                    if "|CL|" in g]   # the plugin's, then
        nt.writer.lines.clear()
        r = c.put("/api/connections/lines", json={"enabled": False})   # CL back to NinjaTrader
        assert r.status_code == 200, r.text
        assert m.trade_owner == {}
        assert nt.writer.lines[0].startswith("I|") and "CL" in nt.writer.lines[0][2:].split(",")
        gap = [g for g in nt.writer.lines if g.startswith("GAP|CL|")]
        assert len(gap) == 1 and gap[0].startswith(f"GAP|CL|{last}|") and gap[0].endswith("|live"), nt.writer.lines
        assert filling(m, "CL")
        t0 = int(time.time() * 1000)
        for k, px in enumerate([66.0, 66.05, 66.1]):   # NinjaTrader's live CL trades at 10:30, its answer still coming
            c.portal.call(nt._line, m, f"T|CL|{t0 + k * 1000}|{px}|1|1\n".encode())
            c.portal.call(asyncio.sleep, 0.05)
        c.portal.call(asyncio.sleep, 0.5)
        assert not events(c)   # held
        sent = 0
        for t in range(ct(day, 7, 31), t0 // 60_000 * 60_000, 60_000):   # its answer: the stretch, as 1-minute bars
            px = 65.2 if t < ct(day, 8, 0) else 65.9
            c.portal.call(nt._line, m, f"Y|CL|{t}|{px}|{px}|{px}|{px}|5\n".encode())
            sent += 1
        c.portal.call(nt._line, m, f"Z|CL|minutes|{sent}|ok\n".encode())
        assert "CL" not in m.gapfill.windows and not filling(m, "CL") and m.gapfill.status["CL"]["state"] == "done"
        assert bars_between(c, m, last, t0 // 60_000 * 60_000) == sent
        c.portal.call(asyncio.sleep, 1.0)
        for k, px in enumerate([66.1, 66.0]):
            c.portal.call(nt._line, m, f"T|CL|{t0 + 5000 + k * 1000}|{px}|1|1\n".encode())
            c.portal.call(asyncio.sleep, 0.05)
        c.portal.call(asyncio.sleep, 0.5)
        assert not any("Open: 66" in e for e in events(c)), events(c)
        m.feed_objs.remove(nt)


# ---- k-fo12-2: a hole the feed before had nothing for is asked of the feed that takes the instrument over --------------

def stored_with_hole(tmp_path, day):
    """CL's 1-minute bars and trades 08:00-09:00 and 09:30-10:29 today: 09:01-09:29 is a hole."""
    minutes = [ct(day, 8, 0) + k * 60_000 for k in range(61)] + [ct(day, 9, 30) + k * 60_000 for k in range(60)]
    st = Store(tmp_path / "chart.db")
    st.write([Tick("CL", t + 1000, 65.0, 1, 1) for t in minutes],
             [("CL", Bar(t, 65.0, 65.0, 65.0, 65.0, 1, 0, 0, 1)) for t in minutes])
    st.close()


def test_a_hole_ninjatrader_had_nothing_for_is_asked_of_the_plugin_that_takes_the_instrument_over(tmp_path, monkeypatch, in_session):
    """CL comes from NinjaTrader and 09:01-09:29 has no bars; at the add-on's connect the hole is asked and NinjaTrader's
    provider has nothing for it. CL is then moved to Your own program, given a history() that has those minutes:
    it is asked for the hole and fills it (before: the hole was kept as asked for CL, whichever feed answered, so the
    plugin skipped it until it was over 7 days old)."""
    day = datetime.fromtimestamp(in_session / 1000, sessions.CT).date()
    stored_with_hole(tmp_path, day)
    app, client = _client(tmp_path, monkeypatch)
    with client as c:
        m = app.state.market
        nt = add_on(m)
        hello = int(time.time() * 1000)
        c.portal.call(lambda: m.gapfill.requests(hello, live=True, minutes=True, contracts=True))
        assert m.gapfill.queue["CL"] == [(ct(day, 9, 0, 1), ct(day, 9, 30))]
        c.portal.call(nt._line, m, b"Z|CL|ticks|0|ok\n")   # the stretch up to now: nothing
        assert nt.writer.lines[-1] == f"GAP|CL|{ct(day, 9, 0, 1) - PROBE_MS}|{ct(day, 9, 30)}"
        c.portal.call(nt._line, m, b"Z|CL|ticks|0|ok\n")   # the hole: NinjaTrader has nothing for it
        assert c.portal.call(m.store.get_setting, ASKED).get("CL")
        r = c.put("/api/connections/lines", json={"enabled": True, "port": _port(), "symbols": "CL"})
        assert r.status_code == 200, r.text
        lines = next(f for f in m.feed_objs if f.name == "lines")
        assert wait_for(c, lambda: lines.stats.get("note", "").startswith("Listening"))
        got = []

        async def history(self, sym, start, end):
            got.append((sym, start, end))
            t = (start // 60_000 + 1) * 60_000
            while t < end:
                yield ("trade", t + 1000, 65.0, 1)
                t += 60_000
        monkeypatch.setattr(type(lines), "history", history)   # (fill_gaps asks holes of a class with one)
        c.portal.call(lines.connected)
        assert wait_for(c, lambda: got and "CL" not in m.gapfill.windows)
        c.portal.call(asyncio.sleep, 0.3)
        assert got == [("CL", ct(day, 9, 0, 1) - PROBE_MS, ct(day, 9, 30))]
        assert bars_between(c, m, ct(day, 9, 0), ct(day, 9, 30)) == 29
        m.feed_objs.remove(nt)


def test_a_hole_the_plugin_had_nothing_for_is_asked_of_ninjatrader_once_it_supplies_the_instrument_again(tmp_path, monkeypatch, in_session):
    """The other way round: CL comes from Your own program, whose history() has nothing for the hole 09:01-09:29. The
    plugin is turned off and the add-on connects: NinjaTrader is asked for the hole (before: skipped, as asked)."""
    day = datetime.fromtimestamp(in_session / 1000, sessions.CT).date()
    app, client = _client(tmp_path, monkeypatch)
    with client as c:
        r = c.put("/api/connections/lines", json={"enabled": True, "port": _port(), "symbols": "CL"})
        assert r.status_code == 200, r.text
    stored_with_hole(tmp_path, day)
    app, client = _client(tmp_path, monkeypatch)
    with client as c:
        m = app.state.market
        lines = next(f for f in m.feed_objs if f.name == "lines")
        assert wait_for(c, lambda: lines.stats.get("note", "").startswith("Listening"))
        got = []

        async def history(self, sym, start, end):
            got.append((sym, start, end))
            return
            yield
        monkeypatch.setattr(type(lines), "history", history)
        c.portal.call(lines.connected)
        assert wait_for(c, lambda: (c.portal.call(m.store.get_setting, ASKED) or {}).get("CL"))
        assert got == [("CL", ct(day, 9, 0, 1) - PROBE_MS, ct(day, 9, 30))]
        r = c.put("/api/connections/lines", json={"enabled": False})
        assert r.status_code == 200, r.text
        nt = add_on(m)
        hello = int(time.time() * 1000)
        c.portal.call(lambda: m.gapfill.requests(hello, live=True, minutes=True, contracts=True))
        assert m.gapfill.queue["CL"] == [(ct(day, 9, 0, 1), ct(day, 9, 30))]
        m.feed_objs.remove(nt)
