"""The watchlist's details panel: the day, 52-week range and performance, a future's contract and roll."""
import asyncio
from datetime import date, timedelta

from chartsrv import details as det
from chartsrv.models import Bar, Tick
from chartsrv.sessions import day_open, session_start
from test_review_fixes import build_market


def test_from_daily_bars():
    today = date(2026, 10, 20)
    days = [today - timedelta(days=k) for k in range(400, 0, -1)]
    rows = [{"t": day_open(d), "o": 100 + i, "h": 101 + i, "l": 99 + i, "c": 100 + i} for i, d in enumerate(days)]
    out = det._from_daily(rows, 500.0, today)
    assert out["w52"] == [min(r["l"] for r, d in zip(rows, days) if d > today - timedelta(days=365)), rows[-1]["h"]]
    c_week = next(r["c"] for r, d in zip(reversed(rows), reversed(days)) if d <= today - timedelta(days=7))
    assert out["perf"]["1W"] == round((500 / c_week - 1) * 100, 2)
    assert list(out["perf"]) == ["1W", "1M", "3M", "YTD", "1Y"]
    assert det._from_daily([], 1.0, today) == {}


def test_a_live_future(tmp_path, monkeypatch):
    m, _ = build_market(tmp_path, monkeypatch)
    now = session_start(day_open(date(2026, 10, 20))) + 3 * 3_600_000
    start = session_start(now)
    m.store.write([], [("ES", Bar(t=start, o=6900, h=6910, l=6895, c=6905, v=10, n=3)),
                       ("ES", Bar(t=start + 60_000, o=6905, h=6920, l=6890, c=6915, v=10, n=3))])
    m.last_tick["ES"] = Tick("ES", now, 6915.0, 1, 1)
    m.contracts["ES"] = "ES 12-26"

    async def yahoo(ticker, spec):
        assert ticker == "ES=F"
        return [{"t": day_open(date(2026, 10, 1)), "o": 6700, "h": 6800, "l": 6650, "c": 6750}], {}
    monkeypatch.setattr(m.ext, "_yahoo", yahoo)
    out = asyncio.run(det.details(m, "CME_MINI:ES1!", now))
    assert out["day"] == {"o": 6900, "h": 6920, "l": 6890} and out["contract"] == "ES 12-26" and out["roll"] == "volume"
    assert "levels" not in out
    assert out["w52"] == [6650, 6800] and "earnings" not in out
    assert asyncio.run(det.details(m, "AAPL+MSFT", now)) == {"symbol": "AAPL+MSFT"}   # a formula: nothing of its own


def test_a_stock_keeps_its_day_through_the_afternoon(tmp_path, monkeypatch):
    from chartsrv.sessions import to_ct
    m, _ = build_market(tmp_path, monkeypatch)
    today = date(2026, 10, 21)
    rows = [{"t": day_open(today - timedelta(days=1)), "o": 240, "h": 245, "l": 238, "c": 244},
            {"t": day_open(today), "o": 244, "h": 250, "l": 243, "c": 249}]   # Yahoo stamps a day at its 17:00 CT open

    async def yahoo(ticker, spec):
        return rows, {}
    monkeypatch.setattr(m.ext, "_yahoo", yahoo)
    noon = day_open(today) + 7 * 3_600_000 + 12 * 3_600_000   # 12:00 CT
    for hours in (-3, 1, 3, 8):   # 09:00, 13:00, 15:00 and 20:00 CT
        now = noon + hours * 3_600_000
        assert to_ct(now).date() == today
        assert asyncio.run(det.details(m, "AAPL", now)).get("day") == {"o": 244, "h": 250, "l": 243}, hours
    rows.pop()   # only yesterday's: no Day row
    assert "day" not in asyncio.run(det.details(m, "AAPL", noon + 3_600_000))
