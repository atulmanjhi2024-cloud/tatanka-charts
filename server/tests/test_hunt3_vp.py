"""Fixes from round 3 of the bug hunt in the volume-profile service (vp_service.py): a session or periodic profile
built while a gap fill or an import lands is neither kept nor named current, a page's build key from before a restart
never matches the new run's, and a forming session that nobody asked for a while is built again in a worker instead
of read through every symbol's trades on the event loop."""
import asyncio
import json
import sqlite3
import threading
import time
from datetime import datetime
from types import SimpleNamespace

import pytest

from chartsrv import vp_service
from chartsrv.models import Bar, Tick
from chartsrv.sessions import CT
from chartsrv.store import Store

MIN = 60_000


def ct(d, h, mi=0, s=0):
    return int(datetime(2026, 10, d, h, mi, s, tzinfo=CT).timestamp() * 1000)


NOW = ct(14, 10, 30)                          # a Wednesday morning: Tuesday's 08:30-15:00 session is finished
TUE, TUE_END = ct(13, 8, 30), ct(13, 15)
HOLE = (ct(13, 10), ct(13, 10, 20))           # nothing stored there (the add-on was off): no trades, no bars


@pytest.fixture
def market(tmp_path, monkeypatch):
    """The service's view of the server: a real Store, ES, the saved settings. Jobs run in threads (CHART_WORKERS=0),
    so a test can hold one while the event loop goes on."""
    monkeypatch.setenv("CHART_WORKERS", "0")
    monkeypatch.setattr(time, "time", lambda: NOW / 1000)
    store = Store(tmp_path / "t.db")
    inst = SimpleNamespace(tick_size=0.25, rth=("08:30", "15:00"), calendar="equity")
    return SimpleNamespace(store=store, instruments={"ES": inst}, ind=SimpleNamespace(overrides={}),
                           flush_for_read=lambda symbol="": [], recent_minutes=lambda symbol: (NOW // MIN * MIN, []))


def _tuesday(store):
    """A trade a minute at 7000 through Tuesday's session, but for the hole."""
    ticks, bars = [], []
    for t in range(TUE, TUE_END, MIN):
        if not HOLE[0] <= t < HOLE[1]:
            ticks.append(Tick("ES", t + 1000, 7000.0, 1, 1))
            bars.append(("ES", Bar(t=t, o=7000, h=7000, l=7000, c=7000, v=1)))
    store.write(ticks, bars)


def _hole_trades():
    """What the fill brings: 20 x 50 lots at 7100, so Tuesday's POC moves there."""
    return [Tick("ES", t + 30_000, 7100.0, 50, 1) for t in range(*HOLE, MIN)]


def _held(monkeypatch, name):
    """Wraps the worker job `name`: the first one reads the database, then waits until the test lets it go on."""
    read, go_on, real = threading.Event(), threading.Event(), getattr(vp_service, name)

    def job(*a):
        out = real(*a)
        if not read.is_set():
            read.set()
            go_on.wait(10)
        return out
    monkeypatch.setattr(vp_service, name, job)
    return read, go_on


async def _until(ev):
    while not ev.is_set():
        await asyncio.sleep(0.01)


def _tue(r):
    return next(s for s in r["sessions"] if s["start"] == TUE)


def _cached_tuesday(store):
    """Tuesday's cached session ticks ({} when not cached): what every later request, and the next run, reads."""
    row = store.db.execute("SELECT data FROM vp_cache WHERE key LIKE ?", (f"svp2|ES|{TUE}|%",)).fetchone()
    return {k: v for k, v in json.loads(row[0])["ticks"]} if row else {}


def test_svp_built_while_a_gap_fill_lands_is_not_kept_or_named_current(market, monkeypatch):
    _tuesday(market.store)
    svc = vp_service.VpService(market)
    read, go_on = _held(monkeypatch, "svp_job")

    async def go():
        racing = asyncio.create_task(svc.get("prof", "ES"))
        await _until(read)   # the job has read Tuesday without the hole's trades; a second chart waits behind it
        waiting = asyncio.create_task(svc.get("prof", "ES"))
        await asyncio.sleep(0.05)
        market.store.write(_hole_trades(), [])   # the hole is answered: Market.history_changed -> forget()
        svc.forget("ES")
        go_on.set()
        first, second = await racing, await waiting
        fresh = await svc.get("prof", "ES")
        polls = [await svc.get("prof", "ES", key=r["key"], have={s["start"] for s in r["sessions"] if s.get("done")})
                 for r in (first, second)]
        return first, fresh, polls
    first, fresh, polls = asyncio.run(go())
    assert _tue(fresh)["poc"] == 7100.0
    # what was built before the fill isn't kept: the session cached is the one worked out after it
    assert _cached_tuesday(market.store)[round(7100 / 0.25)] == [1000.0, 0.0]
    # nor named current: the page holding the answer built from before gets Tuesday again (as does the one behind it)
    assert first["key"] != fresh["key"]
    assert [_tue(p) for p in polls] == [_tue(fresh)] * 2
    # and the next run (a restart) reads the right session
    again = asyncio.run(vp_service.VpService(market).get("prof", "ES"))
    assert _tue(again)["poc"] == 7100.0 and _tue(again)["rows"] == _tue(fresh)["rows"]


def test_svp_built_while_another_program_imports_is_not_kept(market, monkeypatch):
    """A Databento import or backfill writes the trades and deletes the symbol's cached sessions in one transaction;
    the server restarts a minute later and keeps the cached sessions taken from trades alone."""
    _tuesday(market.store)
    svc = vp_service.VpService(market)
    read, go_on = _held(monkeypatch, "svp_job")

    async def go():
        task = asyncio.create_task(svc.get("prof", "ES"))
        await _until(read)
        db = sqlite3.connect(market.store.path)   # the import, as databento.py writes a day
        with db:
            db.executemany("INSERT INTO ticks VALUES (?,?,?,?,?)", [(t.symbol, t.ts, t.price, t.size, t.side) for t in _hole_trades()])
            db.execute("DELETE FROM vp_cache WHERE key LIKE ?", ("svp2|ES|%",))
        db.close()
        go_on.set()
        return await task
    asyncio.run(go())
    assert round(7100 / 0.25) in _cached_tuesday(market.store) or not _cached_tuesday(market.store)
    after = asyncio.run(vp_service.VpService(market).get("prof", "ES"))   # the restart
    assert _tue(after)["poc"] == 7100.0


def test_pvp_built_while_a_gap_fill_lands_is_not_named_current(market, monkeypatch):
    """Closed periodic profiles built before the fill go out under the old build key: the page gets them again."""
    bars = [Bar(t=t, o=7000, h=7000.25, l=7000, c=7000.25, v=10) for t in range(ct(9, 17), NOW, MIN)
            if not HOLE[0] <= t < HOLE[1]]
    market.store.insert_bars_if_missing("ES", bars)
    svc = vp_service.VpService(market)
    given = {**svc.settings("pvp"), "Period": "Daily", "ProfilesToKeep": 4}
    read, go_on = _held(monkeypatch, "build_minutes")

    async def go():
        racing = asyncio.create_task(svc.get("pvp", "ES", given=given))
        await _until(read)   # a second chart waits behind it, for the series it builds
        waiting = asyncio.create_task(svc.get("pvp", "ES", given=given))
        await asyncio.sleep(0.05)
        market.store.insert_bars_if_missing("ES", [Bar(t=t, o=7100, h=7100, l=7100, c=7100, v=2000) for t in range(*HOLE, MIN)])
        svc.forget("ES")
        go_on.set()
        first, second = await racing, await waiting
        polls = [await svc.get("pvp", "ES", key=r["key"], have={p["start"] for p in r["profiles"] if p["closed"]}, given=given)
                 for r in (first, second)]
        return first, polls, await svc.get("pvp", "ES", given=given)
    first, polls, fresh = asyncio.run(go())
    assert any(p["closed"] for p in first["profiles"]) and first["key"] != fresh["key"]
    assert [p["profiles"] for p in polls] == [fresh["profiles"]] * 2
    assert any(p["poc"] == 7100.0 for p in fresh["profiles"])   # Tuesday, with the filled minutes


def test_a_build_key_from_before_a_restart_never_matches(market):
    """Tuesday from 1-minute bars only; then its trades are imported and the server restarts. A page still open
    asks with the key and the sessions it holds, and gets Tuesday again, now from the trades."""
    market.store.insert_bars_if_missing("ES", [Bar(t=t, o=6800, h=6800.5, l=6800, c=6800.5, v=100) for t in range(TUE, TUE_END, MIN)])
    first = asyncio.run(vp_service.VpService(market).get("prof", "ES"))
    assert _tue(first)["approx"] == 390
    db = sqlite3.connect(market.store.path)   # the import
    with db:
        db.executemany("INSERT INTO ticks VALUES (?,?,?,?,?)", [("ES", t + 1000, 6801.0, 100, 1) for t in range(TUE, TUE_END, MIN)])
        db.execute("DELETE FROM vp_cache WHERE key LIKE ?", ("svp2|ES|%",))
    db.close()
    svc = vp_service.VpService(market)   # the restart: the new run's history count starts at 0 again
    poll = asyncio.run(svc.get("prof", "ES", key=first["key"], have={s["start"] for s in first["sessions"] if s["done"]}))
    assert poll["key"] != first["key"] and _tue(poll)["approx"] == 0 and _tue(poll)["poc"] == 6801.0
    k = ("pvp", "ES", "Weekly")
    assert svc.build_key(k, 0) != vp_service.VpService(market).build_key(k, 0)   # the PVP's key too


def test_svp_forming_session_after_a_pause_is_built_again_in_a_worker(market, monkeypatch):
    """A chart comes back to ES after a while (the page was hidden, or on NQ): rather than read every symbol's trades
    stored since on the event loop, the forming session is worked out again in the worker, from ES's own."""
    today = ct(14, 8, 30)
    market.store.write([Tick("ES", today + i * 10_000, 7000.0 + i % 3 * 0.25, 1, 1) for i in range(600)], [])
    svc = vp_service.VpService(market)
    jobs, real = [], vp_service.svp_job

    def job(db_path, symbol, windows, *a):
        jobs.append(windows)
        return real(db_path, symbol, windows, *a)
    monkeypatch.setattr(vp_service, "svp_job", job)
    forming = (today, ct(14, 15))
    asyncio.run(svc.get("prof", "ES"))
    assert jobs == [[forming]]
    market.store.write([Tick("ES", ct(14, 10, 10), 7001.0, 3, 1)], [])
    asyncio.run(svc.get("prof", "ES"))   # a poll 3 s later: caught up here, with the few trades since
    assert len(jobs) == 1
    market.store.write([Tick("NQ", ct(14, 10, 11) + i, 25000.0, 1, 1) for i in range(vp_service.SVP_CATCH_UP)]
                       + [Tick("ES", ct(14, 10, 20), 7002.0, 4, 1)], [])
    back = asyncio.run(svc.get("prof", "ES"))
    assert jobs[1:] == [[forming]]
    assert back["sessions"][-1] == asyncio.run(vp_service.VpService(market).get("prof", "ES"))["sessions"][-1]


def test_svp_answers_while_the_database_cant_be_written(market):
    """A full disk: the sessions worked out are sent, just not kept (worked out again next time)."""
    market.store.write([Tick("ES", t + 1000, 7000.0 + (t - TUE) // MIN * 0.25, 1, 1) for t in range(TUE, TUE_END, MIN)], [])
    svc = vp_service.VpService(market)
    db = market.store.db
    db.execute(f"PRAGMA max_page_count = {db.execute('PRAGMA page_count').fetchone()[0]}")   # not a page more
    r = asyncio.run(svc.get("prof", "ES"))
    assert _tue(r)["approx"] == 0 and len(_tue(r)["rows"]) == 390 and not _cached_tuesday(market.store)
