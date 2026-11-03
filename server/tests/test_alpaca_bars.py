"""Real-time intraday stock charts from Alpaca: the consolidated tape to 15 minutes ago, IEX since, regular hours."""
import asyncio
import types
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx

from chartsrv.feeds import alpaca
from test_review_fixes import build_market

ET = ZoneInfo("America/New_York")


def _iso(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _ms(dt):
    return int(dt.timestamp() * 1000)


def test_session_bars():
    day = datetime(2026, 10, 16, tzinfo=ET)
    rows = []
    for m in range(4 * 60, 20 * 60, 30):   # 04:00-20:00 ET, 30-minute bars
        t = day + timedelta(minutes=m)
        rows.append((_ms(t), 100 + m / 1000, 101 + m / 1000, 99 + m / 1000, 100.5 + m / 1000, 10))
    two_h = alpaca.session_bars(rows, 120)
    assert [datetime.fromtimestamp(b["t"] / 1000, ET).strftime("%H:%M") for b in two_h] == ["09:30", "11:30", "13:30", "15:30"]
    assert [b["v"] for b in two_h] == [40, 40, 40, 10]   # 15:30 holds one half hour; nothing before 9:30 or after 16:00
    eth = alpaca.session_bars(rows, 120, extended=True)   # with the pre- and post-market: 4:00-20:00, on the hour
    assert [datetime.fromtimestamp(b["t"] / 1000, ET).strftime("%H:%M") for b in eth][:3] == ["04:00", "06:00", "08:00"]
    assert len(eth) == 8 and sum(b["v"] for b in eth) == 320
    assert alpaca.base_minutes(120) == 30 and alpaca.base_minutes(3) == 1 and alpaca.base_minutes(45) == 15
    assert (alpaca.spec_minutes("3m"), alpaca.spec_minutes("2h"), alpaca.spec_minutes("8h"), alpaca.spec_minutes("1D")) == (3, 120, None, None)
    doc = {"bars": {"AAPL": [{"t": "2026-10-16T13:30:00Z", "o": 1, "h": 2, "l": 0.5, "c": 1.5, "v": 9, "n": 3, "vw": 1.2},
                             {"t": "bad"}]}, "next_page_token": None}
    assert alpaca.parse_bars(doc, "AAPL") == [(_ms(datetime(2026, 10, 16, 13, 30, tzinfo=timezone.utc)), 1.0, 2.0, 0.5, 1.5, 9.0)]
    assert alpaca.parse_bars({"bars": doc["bars"]["AAPL"]}, "AAPL")[0][4] == 1.5   # the single-symbol answer: a list


def _alpaca_server(calls, sip=True, keys_ok=True):
    """Answers like Alpaca: a bar per step in regular and extended hours; SIP refused within 15 minutes, or always."""
    def handler(req):
        p = req.url.params
        calls.append(dict(p))
        if not keys_ok:
            return httpx.Response(401, json={"message": "forbidden"})
        start = datetime.fromisoformat(p["start"].replace("Z", "+00:00"))
        end = datetime.fromisoformat(p["end"].replace("Z", "+00:00"))
        if p["feed"] == "sip" and (not sip or end > datetime.now(timezone.utc) - timedelta(minutes=15)):
            return httpx.Response(403, json={"message": "subscription does not permit querying recent SIP data"})
        step = int(p["timeframe"].replace("Min", "")) if "Min" in p["timeframe"] else 1440
        t = start.replace(second=0, microsecond=0)
        t -= timedelta(minutes=t.minute % step) if step < 60 else timedelta(0)
        out = []
        while t < end and len(out) < 200_000:
            et = t.astimezone(ET)
            if et.weekday() < 5 and 4 <= et.hour < 20 and t >= start:
                v = 1 if p["feed"] == "iex" else 100
                out.append({"t": _iso(t), "o": 50, "h": 51, "l": 49, "c": 50.5, "v": v})
            t += timedelta(minutes=step)
        if p.get("sort") == "desc":
            out.reverse()
        page = int(p.get("page_token") or 0)
        chunk = out[page * 10000:(page + 1) * 10000]
        return httpx.Response(200, json={"bars": {p["symbols"]: chunk}, "next_page_token": str(page + 1) if len(out) > (page + 1) * 10000 else None})
    return handler


def test_stock_chart_from_alpaca(tmp_path, monkeypatch):
    calls = []
    m, _ = build_market(tmp_path, monkeypatch, http=httpx.AsyncClient(transport=httpx.MockTransport(_alpaca_server(calls))),
                        ALPACA_KEY_ID="PKABCDEFGH", ALPACA_SECRET="s3cretsecret")

    async def go():
        d = await m.ext.bars("AAPL", "3m")
        assert (d["source"], d["spec"], d["custom"], d["feed"]) == ("alpaca", "3m", True, "sip+iex")
        ts = [datetime.fromtimestamp(b["t"] / 1000, ET) for b in d["bars"]]
        assert all((t.hour, t.minute) >= (9, 30) and t.hour < 16 and (t.hour * 60 + t.minute - 570) % 3 == 0 for t in ts)
        assert len(d["bars"]) > 1000
        sip = [c for c in calls if c["feed"] == "sip"]
        iex = [c for c in calls if c["feed"] == "iex"]
        assert sip and all(c["timeframe"] == "1Min" for c in sip) and iex[-1]["timeframe"] == "1Min"
        n = len(calls)
        await asyncio.sleep(0)
        m.ext.cache.clear()   # a poll a few seconds later: only the IEX tail is asked again
        await m.ext.bars("AAPL", "3m")
        assert [c["feed"] for c in calls[n:]] == ["iex"]
        assert await m.ext._alpaca("AAPL", "1D") is None and m.ext._alpaca_can("AAPL")   # daily bars: Yahoo's
        assert d["exchange"] == {"tz": "America/New_York", "rth": ["09:30", "16:00"], "day": "00:00"}
        e = await m.ext.bars("AAPL", "3m", rth=False)   # ETH: the pre- and post-market too
        hours = {datetime.fromtimestamp(b["t"] / 1000, ET).hour for b in e["bars"]}
        assert min(hours) < 9 and max(hours) >= 16
    asyncio.run(go())


def test_alpaca_falls_back(tmp_path, monkeypatch):
    calls = []
    m, _ = build_market(tmp_path, monkeypatch, http=httpx.AsyncClient(transport=httpx.MockTransport(_alpaca_server(calls, sip=False))),
                        ALPACA_KEY_ID="PKABCDEFGH", ALPACA_SECRET="s3cretsecret")

    async def go():
        d = await m.ext.bars("MSFT", "15m")   # no SIP at all: IEX for the whole range
        assert (d["source"], d["feed"]) == ("alpaca", "iex") and all(b["v"] > 0 for b in d["bars"])
        assert await m.ext._alpaca("ES1!", "5m") is None and await m.ext._alpaca("BTCUSD", "5m") is None
    asyncio.run(go())

    calls2 = []

    def yahoo_or_alpaca(req):
        if "alpaca" in req.url.host:
            return _alpaca_server(calls2, keys_ok=False)(req)
        assert req.url.params["interval"] == "1m"   # 3m from Yahoo: built from its 1m bars
        return httpx.Response(200, json={"chart": {"result": [{"meta": {"priceHint": 2}, "timestamp": [1792152000],
                                                                "indicators": {"quote": [{"open": [1], "high": [2], "low": [0.5], "close": [1.5], "volume": [5]}]}}]}})
    (tmp_path / "b").mkdir()
    m2, _ = build_market(tmp_path / "b", monkeypatch, http=httpx.AsyncClient(transport=httpx.MockTransport(yahoo_or_alpaca)),
                         ALPACA_KEY_ID="PKBADKEYSS", ALPACA_SECRET="s3cretsecret")
    d = asyncio.run(m2.ext.bars("AAPL", "3m"))
    assert (d["source"], d["spec"], d["custom"]) == ("yahoo", "3m", True)   # refused keys: Yahoo, 3m from its 1m


def test_long_history_keeps_the_latest(tmp_path, monkeypatch):
    """A chart needing more bars than MAX_PAGES pages loses its oldest days, never its latest ones."""
    calls = []
    m, _ = build_market(tmp_path, monkeypatch, http=httpx.AsyncClient(transport=httpx.MockTransport(_alpaca_server(calls))),
                        ALPACA_KEY_ID="PKABCDEFGH", ALPACA_SECRET="s3cretsecret")

    async def go():
        d = await m.ext.bars("SPY", "12m")   # 12 is no multiple of 5: built from 1-minute bars, over 80,000 of them
        sip = [c for c in calls if c["feed"] == "sip"]
        assert len(sip) == alpaca.MAX_PAGES and all(c["sort"] == "desc" for c in sip)
        ts = [b["t"] for b in d["bars"]]
        assert ts == sorted(ts) and len(set(ts)) == len(ts)
        gap_days = max(b - a for a, b in zip(ts, ts[1:])) / 86_400_000
        assert gap_days < 4   # nights and weekends only: no hole before the live tail
    asyncio.run(go())


def test_a_stocks_history_goes_once_it_is_too_old_to_use():
    """r4-performance-4: the free plan's SIP history of a stock chart is used again for 5 minutes; after that it is of
    no use, and goes when the next one is kept (not only once 50 are held)."""
    calls = []
    alp = alpaca.AlpacaBars(httpx.AsyncClient(transport=httpx.MockTransport(_alpaca_server(calls))))
    keys = types.SimpleNamespace(alpaca_key_id="PKABCDEFGH", alpaca_secret="s3cretsecret", alpaca_feed="iex")

    async def go():
        for sym in ("AAPL", "MSFT", "NVDA"):
            await alp.bars(keys, sym, 5, 2)
        for k in [("AAPL", 5), ("MSFT", 5)]:   # opened 5 minutes ago
            alp.hist[k] = (alp.hist[k][0] - 300, *alp.hist[k][1:])
        await alp.bars(keys, "TSLA", 5, 2)
    asyncio.run(go())
    assert sorted(alp.hist) == [("NVDA", 5), ("TSLA", 5)]
