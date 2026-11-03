"""Major earnings on the Calendar tab: Nasdaq's earnings calendar read per weekday, filtered by market cap and tickers."""
import asyncio
import time
from datetime import date, datetime, timedelta

import httpx
import pytest

from chartsrv import earnings
from chartsrv.sessions import CT
from test_free_data import _market

ROWS = [
    {"lastYearRptDt": "10/23/2025", "lastYearEPS": "$0.50", "time": "time-after-hours", "symbol": "TSLA", "name": "Tesla, Inc.",
     "marketCap": "$1,393,452,345,231", "fiscalQuarterEnding": "Sep/2026", "epsForecast": "$0.55", "noOfEsts": "12"},
    {"time": "time-pre-market", "symbol": "KO", "name": "Coca-Cola Company (The)", "marketCap": "$301,000,000,000",
     "fiscalQuarterEnding": "Sep/2026", "epsForecast": "$0.78", "noOfEsts": "9"},
    {"time": "time-not-supplied", "symbol": "TINY", "name": "Tiny Corp", "marketCap": "$1,200,000",
     "epsForecast": "($0.05)", "noOfEsts": ""},
    {"time": "time-after-hours", "symbol": "MID", "name": "Mid Corp", "marketCap": "$40,000,000,000", "epsForecast": "N/A"},
    {"symbol": "bad ticker!", "name": "x"},
    "not a row",
]


def test_parse_rows():
    day = date(2026, 10, 21)
    out = earnings.parse({"data": {"rows": ROWS}}, day)
    assert [r["symbol"] for r in out] == ["TSLA", "KO", "TINY", "MID"]
    tsla, ko, tiny, mid = out
    assert tsla["when"] == "AMC" and datetime.fromtimestamp(tsla["t"] / 1000, CT).strftime("%H:%M") == "15:05"
    assert ko["when"] == "BMO" and datetime.fromtimestamp(ko["t"] / 1000, CT).strftime("%H:%M") == "06:00"
    assert tiny["when"] == "" and datetime.fromtimestamp(tiny["t"] / 1000, CT).strftime("%Y-%m-%d %H:%M") == "2026-10-21 00:00"
    assert (tsla["cap"], tsla["eps"], tsla["ests"], tsla["quarter"]) == (1_393_452_345_231, 0.55, 12, "Sep/2026")
    assert (tiny["eps"], tiny["ests"], mid["eps"]) == (-0.05, None, None)
    assert tsla["id"] == "TSLA|2026-10-21" and tsla["day"] == "2026-10-21"
    for doc in (None, {}, {"data": None}, {"data": {"rows": None}}, [], "x"):   # a day with nothing: rows is null
        assert earnings.parse(doc, day) == []


def test_settings_and_filter():
    assert earnings.clean_settings({}) == earnings.DEFAULTS
    cfg = earnings.clean_settings({"on": True, "min_cap_b": 300, "symbols": ["mid", " MID", "brk.b"]})
    assert cfg == {"on": True, "min_cap_b": 300.0, "symbols": ["BRK.B", "MID"]}
    for bad in ({"min_cap_b": 1}, {"min_cap_b": True}, {"min_cap_b": "200"}, {"symbols": "AAPL"}, {"symbols": ["no way"]}, []):
        with pytest.raises(ValueError):
            earnings.clean_settings(bad)
    rows = earnings.parse({"data": {"rows": ROWS}}, date(2026, 10, 21))
    assert [r["symbol"] for r in earnings.major(rows, earnings.DEFAULTS)] == ["TSLA", "KO"]
    assert [r["symbol"] for r in earnings.major(rows, cfg)] == ["TSLA", "KO", "MID"]
    assert earnings.major(rows, {**cfg, "on": False}) == []


def test_refresh_reads_each_weekday(tmp_path, monkeypatch):
    days = []

    def answer(req):
        assert req.url.host == "api.nasdaq.com" and req.headers["Referer"] == "https://www.nasdaq.com/"
        days.append(req.url.params["date"])
        if len(days) == 2:
            return httpx.Response(503)   # one bad day doesn't lose the rest
        return httpx.Response(200, json={"data": {"rows": ROWS[:4] if len(days) == 1 else None}})
    m, _ = _market(tmp_path, monkeypatch, answer)
    sleeps = []

    async def no_sleep(s):
        sleeps.append(s)
    monkeypatch.setattr(earnings.asyncio, "sleep", no_sleep)
    today = datetime.now(CT).date()
    weekdays = [today + timedelta(days=i) for i in range(earnings.AHEAD_DAYS + 1) if (today + timedelta(days=i)).weekday() < 5]

    async def go():
        assert await m.earnings.refresh() == 3   # TSLA, KO, MID kept ($5B+); TINY not
        assert days == [d.isoformat() for d in weekdays] and len(sleeps) == len(weekdays)
        now = int(time.time() * 1000)
        assert [r["symbol"] for r in m.earnings.between(now - 86_400_000, now + 15 * 86_400_000)] == ["KO", "TSLA"]   # by time: before the open first
        m.earnings.fetched = time.time()
        m.earnings.put_settings({"min_cap_b": 20})
        assert m.earnings.fetched > 0   # a lower floor needs no new read
        assert {r["symbol"] for r in m.earnings.between(now - 86_400_000, now + 15 * 86_400_000)} == {"TSLA", "KO", "MID"}
        m.earnings.put_settings({"symbols": ["TINY"]})
        assert m.earnings.fetched == 0   # a ticker under the kept floor: read again
    asyncio.run(go())
    again = earnings.Earnings(m)   # kept across a restart
    assert [r["symbol"] for r in again.rows] == [r["symbol"] for r in m.earnings.rows] and again.cfg["min_cap_b"] == 20


def test_refresh_fails_when_nasdaq_turns_it_away(tmp_path, monkeypatch):
    m, calls = _market(tmp_path, monkeypatch, lambda req: httpx.Response(403))

    async def no_sleep(s):
        pass
    monkeypatch.setattr(earnings.asyncio, "sleep", no_sleep)
    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(m.earnings.refresh())
    assert len(calls) == 3 and m.earnings.rows == []   # gives up after three, keeps what it had


def test_calendar_api_includes_earnings(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    monkeypatch.setenv("CHART_DB", str(tmp_path / "chart.db"))
    logs = tmp_path / "logs"
    logs.mkdir()
    monkeypatch.setenv("CHART_LEVELS_DIR", str(logs))
    monkeypatch.setenv("CHART_FEED", "none")
    monkeypatch.setenv("CHART_SYMBOLS_ONLINE", "0")
    from chartsrv.app import app
    with TestClient(app) as c:
        m = app.state.market
        m.earnings.rows = earnings.parse({"data": {"rows": ROWS}}, datetime.now(CT).date() + timedelta(days=1))
        j = c.get("/api/calendar").json()
        assert [r["symbol"] for r in j["earnings"]] == ["TSLA", "KO"] and j["earnings_settings"]["min_cap_b"] == 200
        assert c.put("/api/calendar/earnings", json={"min_cap_b": 2}).status_code == 400
        assert c.put("/api/calendar/earnings", json={"symbols": ["tiny"]}).json()["symbols"] == ["TINY"]
        assert [r["symbol"] for r in c.get("/api/calendar").json()["earnings"]] == ["TSLA", "KO", "TINY"]
        assert c.put("/api/calendar/earnings", json={"on": False}).status_code == 200
        assert c.get("/api/calendar").json()["earnings"] == []
