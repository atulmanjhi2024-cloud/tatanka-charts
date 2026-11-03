"""Fixes from round 3 of the market-core bug hunt: a gap filled past the live feed's last minute is charted and studied,
the simulator's daily charts stay its own, a settlement and the watchlist's quotes while the database can't be written
(a full disk), the daily upkeep never holds up the server for a reader, and a failed store write is not logged every
second."""
import asyncio
import json
import logging
import sqlite3
import time
from datetime import date, datetime, timedelta

import pytest

from chartsrv import market as market_mod
from chartsrv import sessions
from chartsrv.feeds.ninjatrader import NinjaTraderFeed
from chartsrv.market import Client
from chartsrv.models import Bar, Tick
from chartsrv.sessions import day_open
from chartsrv.settlement import Settlements
from chartsrv.store import connect, iter_bars_1m

from test_review_fixes import build_market, history

CT = sessions.CT


def ms(d, h, mi, s=0):
    return int(datetime(2026, 10, d, h, mi, s, tzinfo=CT).timestamp() * 1000)


def _full(*a):
    raise sqlite3.OperationalError("database or disk is full")


# --- a gap filled past the last live minute ---------------------------------------------------------------------
@pytest.mark.parametrize("kind", ["minutes", "ticks"])
def test_a_gap_filled_past_the_last_live_minute_is_charted_and_studied(tmp_path, monkeypatch, kind):
    """ES traded live until Friday 13:00, NinjaTrader was closed with the server left running, and on Saturday the
    add-on filled the rest of Friday (1-minute bars or trades): the charts that reload and the studies read every
    filled minute, not only those up to the last live one."""
    monkeypatch.setenv("CHART_WORKERS", "0")
    clock = {"now": ms(23, 12, 50)}
    monkeypatch.setattr(market_mod, "now_ms", lambda: clock["now"])
    monkeypatch.setattr(time, "time", lambda: clock["now"] / 1000)
    m, _ = build_market(tmp_path, monkeypatch, feed="ninjatrader")
    feed = NinjaTraderFeed("127.0.0.1", 0, "")
    feed.stats["version"] = 11

    async def go():
        for i in range(127):   # a trade every 5 s, 12:50:00 to 13:00:30
            clock["now"] = ms(23, 12, 50) + i * 5000
            feed._line(m, f"T|ES|{clock['now']}|{7000 + i % 8 * 0.25}|1|1".encode())
        m.flush()
        open_chart = Client()
        await history(m, open_chart, 1, "ES", "1m")
        clock["now"] = ms(24, 10, 0)   # Saturday: the add-on connects again
        m.gapfill.requests(clock["now"], live=True, minutes=True, contracts=True)
        for k in range(ms(23, 13, 1), ms(23, 16, 0), 60_000):
            if kind == "minutes":
                feed._line(m, f"Y|ES|{k}|7010|7012|7008|7011|10".encode())
            else:
                for j in range(10):
                    feed._line(m, f"X|ES|{k + j * 5000}|7011|1".encode())
        feed._line(m, f"Z|ES|{kind}|179|ok".encode())
        assert open_chart.outbox.get_nowait()["type"] == "reload"
        c = Client()
        return await history(m, c, 1, "ES", "1m"), await history(m, c, 2, "ES", "1D")
    m1, d1 = asyncio.run(go())
    assert len(m1["bars"]) == 190 and m1["bars"][-1]["t"] == ms(23, 15, 59) and sum(b["v"] for b in m1["bars"]) == 1917
    assert d1["bars"][-1]["c"] == 7011 and d1["bars"][-1]["v"] == 1917
    split, live = m.recent_minutes("ES")   # TPO, gaps, the profiles, Daily SMA, HTF EMA
    friday = list(iter_bars_1m(m.store.path, "ES", ms(23, 12, 0), split)) + live
    assert len(friday) == 190 and sum(b.v for b in friday) == 1917


# --- the simulator's daily charts -------------------------------------------------------------------------------
def test_the_simulators_daily_charts_get_no_older_days_from_yahoo(tmp_path, monkeypatch):
    """Its trades are invented: Yahoo's real days would sit in front of them, and its first day take Yahoo's open."""
    m, _ = build_market(tmp_path, monkeypatch, feed="sim")
    today = date(2026, 10, 20)
    monkeypatch.setattr(market_mod, "now_ms", lambda: day_open(today) + 20 * 3_600_000)
    m.store.write([], [("ES", Bar(t=day_open(today) + 3_600_000, o=6900, h=6910, l=6898, c=6905, v=12, n=5))])
    asked = []

    async def yahoo(ticker, spec):
        asked.append(ticker)
        return [{"t": day_open(today - timedelta(days=i)), "o": 6600, "h": 6640, "l": 6580, "c": 6620, "v": 1_500_000}
                for i in range(30, -1, -1)], {}
    monkeypatch.setattr(m.ext, "_yahoo", yahoo)
    d1 = asyncio.run(history(m, Client(), 1, "ES", "1D"))
    assert asked == [] and [(b["o"], b["l"], b["v"]) for b in d1["bars"]] == [(6900, 6898, 12)]


# --- a settlement while the database can't be written -----------------------------------------------------------
def _settling(tmp_path, monkeypatch):
    """ES charted, its 20 Oct settlement window's trade stored; 21 Oct's waits in the buffer (the disk filled at
    14:59), and the first quote after the window comes at 15:01:30."""
    m, _ = build_market(tmp_path, monkeypatch)
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    (cfg / "settlements.json").write_text(json.dumps({"markets": {
        "ES": {"tz": "America/Chicago", "window": ["14:59:30", "15:00:00"], "calendar": "equity"}}}))
    m.settlements = Settlements(m, cfg)
    m.store.write([Tick("ES", ms(20, 14, 59, 40), 7755.0, 10, 1)], [])
    now = ms(21, 15, 1, 30)
    monkeypatch.setattr(market_mod, "now_ms", lambda: now)
    m.store.write = _full
    m.on_tick(Tick("ES", ms(21, 14, 59, 40), 7760.0, 10, 1))
    m.on_tick(Tick("ES", ms(21, 15, 0, 10), 7770.0, 5, 1))   # after the window: not in it
    return m, NinjaTraderFeed("127.0.0.1", 0, "")


def test_a_settlement_is_worked_out_while_the_database_cant_be_written(tmp_path, monkeypatch):
    """From the stored trades and those waiting for the database: NinjaTrader's quote line goes through, so the
    add-on's connection is not dropped."""
    m, feed = _settling(tmp_path, monkeypatch)
    feed._line(m, b"Q|ES|7761.00|7700.00")
    saved = m.settlements.saved["ES"]
    assert (saved["t"], saved["price"], saved["src"]) == (ms(21, 15, 0), 7760.0, "trades")
    assert saved["prev"]["price"] == 7755.0 and "ES" in m.settlements.cache
    assert m.quotes["ES1!"]["prev"] == 7755.0   # until the 17:00 open, the change is from the one before


def test_a_quote_goes_through_when_its_settlement_cant_be_saved(tmp_path, monkeypatch):
    """Nor the settings (a truly full disk): the quote takes the feed's previous close, and the line goes through."""
    m, feed = _settling(tmp_path, monkeypatch)
    m.store.set_setting = _full
    feed._line(m, b"Q|ES|7761.00|7700.00")
    assert m.quotes["ES1!"]["last"] == 7761.0 and m.quotes["ES1!"]["prev"] == 7700.0


def test_a_visible_range_profile_reads_on_while_the_database_cant_be_written(tmp_path, monkeypatch):
    m, _ = build_market(tmp_path, monkeypatch)
    now = ms(21, 10, 0)
    m.on_tick(Tick("ES", now - 60_000, 7000.0, 3, 1))
    m.flush()
    m.store.write = _full
    m.on_tick(Tick("ES", now - 30_000, 7001.0, 2, 1))
    prof = asyncio.run(m.range_profile("ES", now - 120_000, now))
    assert prof["total"] == 3   # the stored trade; the one waiting for the database is left out


# --- the daily upkeep ---------------------------------------------------------------------------------------------
def test_upkeep_never_waits_for_a_reader_to_trim_the_log(tmp_path, monkeypatch):
    """Its checkpoint runs on the server's own loop: a chart's history load reading at that moment must not hold up
    the whole server for the database's busy timeout. The log is trimmed at a later run, with no reader in."""
    m, _ = build_market(tmp_path, monkeypatch)
    m.store.db.execute("PRAGMA busy_timeout=2000")
    m.store.write([Tick("ES", ms(21, 10, 0) + i, 7000.0, 1, 1) for i in range(5000)], [])
    wal = m.store.path.with_name(m.store.path.name + "-wal")
    reader = connect(m.store.path, readonly=True)
    reader.execute("BEGIN")
    reader.execute("SELECT COUNT(*) FROM ticks").fetchone()   # its read of the log goes on
    t0 = time.perf_counter()
    asyncio.run(m.upkeep.once())
    assert time.perf_counter() - t0 < 1.0 and m.upkeep.last["error"] == ""
    assert m.store.db.execute("PRAGMA busy_timeout").fetchone()[0] == 2000   # the store's own, back
    reader.rollback()
    reader.close()
    asyncio.run(m.upkeep.once())
    assert wal.stat().st_size == 0


# --- a failed store write in the log ------------------------------------------------------------------------------
def test_a_failed_store_write_is_logged_once_then_once_a_minute(tmp_path, monkeypatch, caplog):
    """A full disk: the store loop tries again every second, and a traceback each time would fill it further. The
    trades dropped meanwhile are counted in the minute's line, not logged at each try."""
    clock = {"now": ms(21, 10, 0)}
    monkeypatch.setattr(market_mod, "now_ms", lambda: clock["now"])
    monkeypatch.setattr(market_mod, "STORE_FLUSH_S", 0)
    monkeypatch.setattr(market_mod, "UNWRITTEN_MAX", 2)
    m, _ = build_market(tmp_path, monkeypatch)
    tries = []

    def full(*a):
        tries.append(1)
        raise sqlite3.OperationalError("database or disk is full")
    m.store.write = full
    for i in range(5):
        m.on_tick(Tick("ES", clock["now"] + i, 7000.0, 1, 1))

    async def loop(until):
        task = asyncio.create_task(m._store_loop())
        while not until():
            await asyncio.sleep(0)
        task.cancel()

    def logged():
        out = [(r.levelno, bool(r.exc_info), r.getMessage()) for r in caplog.records if r.name == "chartsrv.market"]
        caplog.clear()
        return out
    caplog.set_level(logging.INFO, "chartsrv.market")
    asyncio.run(loop(lambda: len(tries) >= 30))
    for _ in range(5):
        m.flush_for_read("ES")   # the readers meanwhile
    assert logged() == [(logging.ERROR, True, "store flush failed")]   # the first: in full
    clock["now"] += 61_000
    asyncio.run(loop(lambda: len(tries) >= 60))
    assert logged() == [(logging.WARNING, False, "database still not writable: 2 trades waiting for it, 3 older ones dropped")]
    del m.store.write   # space freed
    asyncio.run(loop(lambda: not m.tick_buf))
    assert [x[0] for x in logged()] == [logging.INFO] and m.write_failed_at is None and m.unwritten_dropped == 0
    assert m.store.db.execute("SELECT COUNT(*) FROM ticks").fetchone()[0] == 2
