"""Regressions from the fourth bug hunt, volume profiles: the visible-range profile and the session profiles behind
POC/VAH/VAL alerts take the 1-minute bar of each minute with no trades, as the Session Volume Profile does, not the
bars of a whole session only when it has no trades at all. An AAVP or PVP nobody asked for in a while is caught up
in a worker, not on the event loop, and a Session VP poll reads only the finished sessions the client lacks."""
import asyncio
import time
from datetime import datetime
from types import SimpleNamespace

import pytest

from chartsrv import vp_service
from chartsrv.config import Instrument
from chartsrv.models import Bar, Tick
from chartsrv.sessions import CT
from chartsrv.store import Store
from chartsrv.studies import ProfileBook, range_profile

ES = Instrument("ES", "E-mini", 0.25, 50, "ES", rth=("08:30", "15:00"))
MIN = 60_000


def ct(d, h, mi=0, s=0):
    return int(datetime(2026, 10, d, h, mi, s, tzinfo=CT).timestamp() * 1000)


def _mixed_session(store):
    """Tuesday: the add-on was off 08:30-09:29 and the hole came back as 1-minute bars (100 a minute at 7000); from
    09:30 its trades (10 a minute at 7010, with the minutes built from them)."""
    bars = [("ES", Bar(t=ct(13, 8, 30) + i * MIN, o=7000, h=7000, l=7000, c=7000, v=100)) for i in range(60)]
    bars += [("ES", Bar(t=ct(13, 9, 30) + i * MIN, o=7010, h=7010, l=7010, c=7010, v=10)) for i in range(30)]
    store.write([Tick("ES", ct(13, 9, 30) + i * MIN + 1000, 7010.0, 10, 1) for i in range(30)], bars)


def test_visible_range_takes_the_bar_of_each_minute_with_no_trades(tmp_path):
    store = Store(tmp_path / "t.db")
    _mixed_session(store)
    for rth in (False, True):
        p = range_profile(store.path, ES, "ES", ct(13, 8, 0), ct(13, 10, 0), True, rth)
        assert p["total"] == 6300 and p["poc"] == 7000.0 and p["approx"]


def test_session_profiles_for_alerts_take_the_bar_of_each_minute_with_no_trades(tmp_path):
    store = Store(tmp_path / "t.db")
    _mixed_session(store)
    book = ProfileBook(ES)
    book.load(store.path, "ES", ct(13, 10, 0, 30), store.max_tick_rowid())
    for p in book.current():
        assert p["total"] == 6300 and p["poc"] == 7000.0 and p["approx"], p["kind"]


def test_session_profiles_load_leaves_the_forming_minute_to_the_trades_that_come_live(tmp_path):
    """The book loads at 10:00:30 with the trades stored to 10:00 (the cutoff). A trade at 10:00:20 came after it, and
    its minute was written meanwhile: that trade is added live (Market.book's pending), so its minute's bar is not."""
    store = Store(tmp_path / "t.db")
    _mixed_session(store)
    cutoff = store.max_tick_rowid()
    late = Tick("ES", ct(13, 10, 0, 20), 7010.0, 5, 1)
    store.write([late], [("ES", Bar(t=ct(13, 10), o=7010, h=7010, l=7010, c=7010, v=5))])
    book = ProfileBook(ES)
    book.load(store.path, "ES", ct(13, 10, 0, 30), cutoff)
    book.add(late)
    for p in book.current():
        assert p["total"] == 6305, p["kind"]


@pytest.mark.parametrize("kind", ["aavp", "pvp"])
def test_minute_profiles_after_a_pause_are_caught_up_in_a_worker(tmp_path, monkeypatch, kind):
    """A chart comes back to ES with AAVP or PVP on after days away (the page was hidden, or on other symbols) with the
    server up: the minutes stored since are fed to its series in the worker, not read through on the event loop."""
    monkeypatch.setenv("CHART_WORKERS", "0")
    monkeypatch.setattr(time, "time", lambda: ct(16, 10) / 1000)
    store = Store(tmp_path / "t.db")
    store.insert_bars_if_missing("ES", [Bar(t=t, o=7000, h=7000 + t // MIN % 7 * 0.25, l=7000, c=7000.25, v=10)
                                        for t in range(ct(1, 17), ct(16, 10), MIN)])
    split = [ct(12, 10)]
    m = SimpleNamespace(store=store, instruments={"ES": SimpleNamespace(tick_size=0.25)}, ind=SimpleNamespace(overrides={}),
                        recent_minutes=lambda symbol: (split[0], []))
    jobs, real = [], vp_service.build_minutes

    def job(db_path, symbol, state, since, before):
        jobs.append((since, before))
        return real(db_path, symbol, state, since, before)
    monkeypatch.setattr(vp_service, "build_minutes", job)
    given = {"Period": "Daily", "ProfilesToKeep": 4} if kind == "pvp" else {"AnchorPeriod": "Month"}
    svc = vp_service.VpService(m)
    asyncio.run(svc.get(kind, "ES", given=given))
    assert len(jobs) == 1
    split[0] = ct(12, 10, 5)
    asyncio.run(svc.get(kind, "ES", given=given))   # a poll a few minutes on: the few minutes since, here
    assert len(jobs) == 1
    split[0] = ct(16, 10)
    back = asyncio.run(svc.get(kind, "ES", given=given))
    assert jobs[1:] == [(ct(12, 10, 4), ct(16, 10))]
    fresh = asyncio.run(vp_service.VpService(m).get(kind, "ES", given=given))
    if kind == "pvp":
        assert [p["rows"] for p in back["profiles"]] == [p["rows"] for p in fresh["profiles"]]
    else:
        assert back["profile"]["rows"] == fresh["profile"]["rows"]


def test_svp_poll_reads_only_the_finished_sessions_the_client_does_not_hold(tmp_path, monkeypatch):
    """Every 3 s a chart with Session VP on asks again, naming the finished sessions it holds: those are not read from
    the cache and parsed again on the event loop, only the ones it lacks."""
    monkeypatch.setenv("CHART_WORKERS", "0")
    monkeypatch.setattr(time, "time", lambda: ct(14, 10, 30) / 1000)
    store = Store(tmp_path / "t.db")
    store.write([Tick("ES", t + 1000, 7000.0 + t // MIN % 9 * 0.25, 1, 1)
                 for d in (12, 13, 14) for t in range(ct(d, 8, 30), min(ct(d, 15), ct(14, 10, 30)), MIN)], [])
    m = SimpleNamespace(store=store, instruments={"ES": SimpleNamespace(tick_size=0.25, rth=("08:30", "15:00"), calendar="equity")},
                        ind=SimpleNamespace(overrides={}), flush_for_read=lambda symbol="": [])
    svc = vp_service.VpService(m)
    first = asyncio.run(svc.get("prof", "ES"))
    done = [s for s in first["sessions"] if s["done"]]
    assert [s["start"] for s in done] == [ct(12, 8, 30), ct(13, 8, 30)]
    reads = []
    store.db.set_trace_callback(lambda sql: reads.append(sql) if "FROM vp_cache" in sql else None)
    poll = asyncio.run(svc.get("prof", "ES", key=first["key"], have={s["start"] for s in done}))
    assert reads == [] and poll["sessions"][:2] == [{"start": s["start"], "same": True} for s in done]
    assert poll["sessions"][2] == first["sessions"][2]
    poll = asyncio.run(svc.get("prof", "ES", key=first["key"], have={ct(13, 8, 30)}))   # Monday not held: read, sent
    assert len(reads) == 1 and poll["sessions"][:2] == [done[0], {"start": ct(13, 8, 30), "same": True}]
