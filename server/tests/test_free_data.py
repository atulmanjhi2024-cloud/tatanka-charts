"""More free data: CFTC Commitments of Traders, EIA energy series, the economic calendar."""
import asyncio
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx
import pytest

from chartsrv import cot, econ_calendar
from chartsrv.feeds import eia, fred
from test_review_fixes import build_market

ROWS = [   # newest first, as the CFTC may answer; one column name with the dataset's own typo-style variant
    {"report_date_as_yyyy_mm_dd": "2026-10-13T00:00:00.000", "market_and_exchange_names": "E-MINI S&P 500 - CHICAGO MERCANTILE EXCHANGE",
     "open_interest_all": "2100000", "comm_positions_long_all": "1000000", "comm_positions_short_all": "1200000",
     "noncomm_positions_long_all": "400000", "noncomm_positions_short_all": "250000",
     "nonrept_positions_long_all": "150000", "nonrept_positions_short_all": "100000"},
    {"report_date_as_yyyy_mm_dd": "2026-10-06T00:00:00.000", "market_and_exchange_names": "E-MINI S&P 500 - CHICAGO MERCANTILE EXCHANGE",
     "open_interest_all": "2000000", "comm_positions_long_all": "1100000", "comm_positions_short_all": "1150000",
     "noncomm_positions_long_all": "380000", "noncomm_positions_short_all": "260000",
     "nonrept_positions_long_all": "120000", "nonrept_positions_short_all": "90000"},
    {"report_date_as_yyyy_mm_dd": "bad"},
]


def _market(tmp_path, monkeypatch, answer, **env):
    calls = []

    def handler(req):
        calls.append(req.url)
        return answer(req)
    return build_market(tmp_path, monkeypatch, http=httpx.AsyncClient(transport=httpx.MockTransport(handler)), **env)[0], calls


def test_cot_weeks(tmp_path, monkeypatch):
    weeks, market = cot.parse_rows(ROWS)
    assert [w["day"] for w in weeks] == ["2026-10-06", "2026-10-13"] and market.startswith("E-MINI S&P 500")
    assert (weeks[-1]["comm"], weeks[-1]["large"], weeks[-1]["small"], weeks[-1]["oi"]) == (-200000, 150000, 50000, 2100000)
    fri = datetime(2026, 10, 16, 15, 30, tzinfo=ZoneInfo("America/New_York"))
    assert weeks[-1]["t"] == int(fri.timestamp() * 1000)   # known when published, Friday 3:30 pm ET

    def answer(req):
        assert req.url.params["cftc_contract_market_code"] == "13874A"
        return httpx.Response(200, json=ROWS)
    m, calls = _market(tmp_path, monkeypatch, answer)

    async def go():
        got = await m.cot.weeks("ES")
        assert got["code"] == "13874A" and len(got["rows"]) == 2
        await m.cot.weeks("CME_MINI:ES1!")   # the database answers now
        assert len(calls) == 1 and m.cot.code_for("CBOT:ZN1!") == "043602"
        with pytest.raises(ValueError):
            await m.cot.weeks("AAPL")
    asyncio.run(go())


def test_eia_series(tmp_path, monkeypatch):
    doc = {"response": {"data": [{"period": "2026-10-09", "value": 421.5}, {"period": "2026-10-02", "value": "425.25"},
                                 {"period": "2026-09", "value": 1}, {"period": "weird", "value": 2}, {"period": "2026-10-16", "value": None}]}}
    obs, dec = eia.parse_series(doc)
    assert obs == [("2026-09-01", 1.0), ("2026-10-02", 425.25), ("2026-10-09", 421.5)] and dec == 2
    assert eia.series_id("eia:pet.wcestus1.w") == "PET.WCESTUS1.W" and eia.series_id("FRED:X") is None

    def answer(req):
        assert req.url.params["api_key"] == "k1" and req.url.path.endswith("/PET.WCESTUS1.W")
        return httpx.Response(200, json=doc)
    m, calls = _market(tmp_path, monkeypatch, answer)

    async def go():
        with pytest.raises(ValueError, match="API key"):   # no key yet: says so, and is not remembered as missing
            await m.eia.series("PET.WCESTUS1.W")
        m.eia.key = lambda: "k1"
        got = await m.ext.bars("EIA:PET.WCESTUS1.W", "1D")
        assert got["source"] == "eia" and got["name"].startswith("U.S. Crude Oil Stocks") and len(got["bars"]) == 3
        m.watch_keys = {"EIA:PET.WCESTUS1.W"}
        task = asyncio.create_task(fred.FredQuotes().run(m))
        for _ in range(50):
            if "EIA:PET.WCESTUS1.W" in m.quotes:
                break
            await asyncio.sleep(0.01)
        task.cancel()
        q = m.quotes["EIA:PET.WCESTUS1.W"]
        assert (q["last"], q["prev"], q["src"]) == (421.5, 425.25, "eia")
    asyncio.run(go())
    assert len(calls) == 1


def test_economic_calendar(tmp_path, monkeypatch):
    now = int(time.time() * 1000)
    iso = lambda ms: datetime.fromtimestamp(ms / 1000, ZoneInfo("America/New_York")).isoformat()   # noqa: E731
    week = [{"title": "CPI m/m", "country": "USD", "date": iso(now + 3 * 60_000), "impact": "High", "forecast": "0.3%", "previous": "0.2%"},
            {"title": "German ZEW", "country": "EUR", "date": iso(now + 2 * 60_000), "impact": "High"},
            {"title": "Crude Oil Inventories", "country": "USD", "date": iso(now + 4 * 60_000), "impact": "Medium"},
            {"title": "Payrolls", "country": "USD", "date": iso(now + 2 * 86_400_000), "impact": "High"},
            {"title": "bad", "country": "USD", "date": "not a date"}]
    assert [e["title"] for e in econ_calendar.parse(week)] == ["German ZEW", "CPI m/m", "Crude Oil Inventories", "Payrolls"]
    with pytest.raises(ValueError):
        econ_calendar.clean_settings({"minutes": 0})
    assert econ_calendar.clean_settings({"currencies": ["usd", "eur"], "impacts": ["Medium", "High"]})["impacts"] == ["High", "Medium"]

    def answer(req):
        return httpx.Response(200, json=week) if "thisweek" in req.url.path else httpx.Response(404)
    m, _ = _market(tmp_path, monkeypatch, answer)
    fired = []
    m._on_alert = lambda ev: fired.append(ev["message"])

    async def go():
        assert await m.calendar.refresh() == 4
        m.calendar._warn()
        m.calendar._warn()
        assert fired == ["USD CPI m/m in 3 min (High impact) · forecast 0.3% · previous 0.2%"]   # once; EUR and Medium skipped
        m.calendar.put_settings({"impacts": ["High", "Medium"]})
        m.calendar._warn()
        assert len(fired) == 2 and "Crude Oil Inventories" in fired[1]
        assert [e["title"] for e in m.calendar.between(now, now + 86_400_000)] == ["German ZEW", "CPI m/m", "Crude Oil Inventories"]
    asyncio.run(go())


def test_economic_calendar_warning_is_not_sent_again_after_a_restart(tmp_path, monkeypatch):
    """A restart inside the minutes before a release (Server > Restart, an import's restart, an update) does not
    warn of it again; one not warned of yet still is."""
    now = int(time.time() * 1000)
    iso = lambda ms: datetime.fromtimestamp(ms / 1000, ZoneInfo("America/New_York")).isoformat()   # noqa: E731
    week = [{"title": "CPI m/m", "country": "USD", "date": iso(now + 3 * 60_000), "impact": "High"}]

    def answer(req):
        return httpx.Response(200, json=week) if "thisweek" in req.url.path else httpx.Response(404)
    fired = []

    async def run():   # a server: reads the calendar, then warns
        m, _ = _market(tmp_path, monkeypatch, answer)
        m._on_alert = lambda ev: fired.append(ev["message"])
        await m.calendar.refresh()
        m.calendar._warn()
    asyncio.run(run())
    assert fired == ["USD CPI m/m in 3 min (High impact)"]
    week.append({"title": "Payrolls", "country": "USD", "date": iso(now + 4 * 60_000), "impact": "High"})
    asyncio.run(run())   # restarted, still inside CPI's five minutes
    assert fired == ["USD CPI m/m in 3 min (High impact)", "USD Payrolls in 4 min (High impact)"]

