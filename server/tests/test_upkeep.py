"""Keeping the database lean: trades older than the retention removed (bars kept), the alert history trimmed to the
newest, and a daily or longer chart of a future with little stored filled out with Yahoo's older days."""
import asyncio
from datetime import date, timedelta

import pytest

from chartsrv import market as market_mod
from chartsrv import upkeep
from chartsrv.models import Bar, Tick
from chartsrv.sessions import day_open
from test_review_fixes import Client, build_market, history

DAY = 86_400_000


def test_old_trades_go_bars_stay(tmp_path, monkeypatch):
    m, _ = build_market(tmp_path, monkeypatch)
    now = upkeep.now_ms()
    old, recent = now - 6 * upkeep.YEAR_MS, now - 3 * DAY
    m.store.write([Tick(s, old + i, 100.0, 1, 1) for s in ("ES", "NQ") for i in range(7)] +
                  [Tick(s, recent + i, 100.0, 1, 1) for s in ("ES", "NQ") for i in range(2)],
                  [("ES", Bar(t=old // 60_000 * 60_000, o=1, h=1, l=1, c=1, v=7, n=7))])
    count = lambda: m.store.db.execute("SELECT COUNT(*) FROM ticks").fetchone()[0]   # noqa: E731
    assert count() == 18 and m.upkeep.years() == 5
    monkeypatch.setattr(upkeep, "BATCH", 3)   # several slices per symbol
    version = m.store.data_version()
    asyncio.run(m.upkeep.once(now))
    assert count() == 4 and m.upkeep.last["deleted"] == 14 and not m.upkeep.last["more"]
    assert m.store.db.execute("SELECT COUNT(*) FROM bars_1m").fetchone()[0] == 1   # the 1-minute bar stays
    assert m.store.data_version() == version   # its own connection: not taken for an import (app.watch_db)

    m.store.write([Tick("ES", old, 100.0, 1, 1)], [])
    m.upkeep.set_years(0)   # always: nothing goes
    asyncio.run(m.upkeep.once(now))
    assert count() == 5 and m.backups.status()["retention"]["years"] == 0
    for bad in (4, -1, True, "5"):
        with pytest.raises(ValueError):
            m.upkeep.set_years(bad)
    m.upkeep.set_years(5)
    monkeypatch.setattr(upkeep, "BUDGET_S", -1)   # out of time: the rest at the next run
    asyncio.run(m.upkeep.once(now))
    assert m.upkeep.last["more"] and count() == 5


def test_alert_history_keeps_the_newest(tmp_path, monkeypatch):
    """An Every time alert can fire once a minute for months: the history keeps the newest EVENTS_KEPT, far more
    than the app shows (the newest 500), so it and the daily backups stay small."""
    m, _ = build_market(tmp_path, monkeypatch)
    monkeypatch.setattr(upkeep, "EVENTS_KEPT", 5)
    for i in range(12):
        m.store.add_event({"alert_id": 1, "symbol": "ES", "ts": i, "price": 7000.0, "message": f"fired {i}"})
    asyncio.run(m.upkeep.once())
    assert [e["message"] for e in m.store.list_events(500)] == [f"fired {i}" for i in range(11, 6, -1)]
    asyncio.run(m.upkeep.once())                      # nothing more to trim
    assert len(m.store.list_events(500)) == 5


def test_a_short_daily_chart_gets_older_days(tmp_path, monkeypatch):
    m, _ = build_market(tmp_path, monkeypatch)
    today = date(2026, 10, 20)
    monkeypatch.setattr(market_mod, "now_ms", lambda: day_open(today) + 20 * 3_600_000)
    first = day_open(today - timedelta(days=1))   # two days stored, the first one partial
    m.store.write([], [("ES", Bar(t=first + 22 * 3_600_000, o=6900, h=6905, l=6898, c=6903, v=10, n=4)),
                       ("ES", Bar(t=day_open(today) + 3_600_000, o=6903, h=6910, l=6901, c=6908, v=12, n=5))])
    days = [today - timedelta(days=i) for i in range(40, 0, -1) if (today - timedelta(days=i)).weekday() < 5]
    rows = [{"t": day_open(d), "o": 6800 + i, "h": 6810 + i, "l": 6790 + i, "c": 6805 + i, "v": 1000} for i, d in enumerate(days)]

    async def yahoo(ticker, spec):
        assert ticker == "ES=F" and spec == "1D"
        m.on_tick(Tick("ES", day_open(today) + 2 * 3_600_000, 6920.0, 3, 1))   # a trade while Yahoo answers: kept
        return [dict(r) for r in rows], {}
    monkeypatch.setattr(m.ext, "_yahoo", yahoo)

    async def go():
        return await history(m, Client(), 1, "ES", "1D"), await history(m, Client(), 2, "ES", "1W")
    d1, w1 = asyncio.run(go())
    bars = d1["bars"]
    assert bars[-1]["t"] == day_open(today) and bars[-2]["t"] == first   # this app's own two days stay its own
    assert bars[-1]["c"] == 6920.0 and bars[-1]["h"] == 6920.0   # the trade that came in meanwhile counts
    assert bars[-2]["o"] == rows[-1]["o"] and bars[-2]["h"] == max(6905, rows[-1]["h"])   # the partial day: Yahoo's open
    assert len(bars) == len(rows) + 1 and bars[0]["t"] == rows[0]["t"] and bars[0]["seq"] < 0
    assert [b["t"] for b in bars] == sorted(b["t"] for b in bars)
    assert 6 <= len(w1["bars"]) <= 8   # forty days in weeks
