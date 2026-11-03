"""Connections: feeds, keys and alert channels set in the app over .env; the Alpaca and Coinbase quote feeds."""
import asyncio
import json
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx
import pytest

from chartsrv import connections as conns
from chartsrv.config import load_settings
from chartsrv.feeds import alpaca, coinbase, feed_names
from test_review_fixes import build_market

HOOK = "https://discord.com/api/webhooks/1234/abcDEF-ghi_jkl"


def test_clean_and_build(tmp_path, monkeypatch):
    with pytest.raises(ValueError, match="Discord webhook"):
        conns.clean("discord", {"discord_webhook": "https://example.com/hook"})
    for cid, body in [("ibkr", {"ibkr_port": 0}), ("ibkr", {"ibkr_port": True}), ("ibkr", {"nope": 1}),
                      ("ibkr", {"ibkr_host": "a b"}), ("ibkr", {"ibkr_market_data_type": 2}), ("nothing", {}),
                      ("rithmic", {}), ("ninjatrader", {"nt_token": "has|pipe"}), ("ntfy", {"ntfy_url": "ftp://x"})]:
        with pytest.raises(ValueError):
            conns.clean(cid, body)
    assert conns.clean("discord", {"discord_webhook": f"  {HOOK} "}) == {"discord_webhook": HOOK}

    monkeypatch.setenv("CHART_FEED", "ninjatrader")
    monkeypatch.setenv("CHART_QUOTES_FEED", "ibkr,yahoo")
    monkeypatch.setenv("CHART_NTFY_URL", "https://ntfy.sh/env-topic")
    base = load_settings()
    s = conns.build(base, {"alpaca": {"enabled": True, "alpaca_key_id": "PKABCDEFGH", "alpaca_secret": "s3cretsecret"},
                           "yahoo": {"enabled": False}, "ntfy": {"ntfy_url": ""}, "ibkr": {"ibkr_port": 4002}})
    assert feed_names(s) == ["ninjatrader", "ibkr", "alpaca", "fred"]
    assert (s.alpaca_key_id, s.ntfy_url, s.ibkr_port) == ("PKABCDEFGH", None, 4002)   # "" clears .env's
    assert conns.build(base, {}) is base
    assert conns.build(base, {"ninjatrader": {"enabled": False}}).feed == "none"
    monkeypatch.setenv("CHART_FEED", "sim")
    assert conns.build(load_settings(), {"ninjatrader": {"enabled": True}}).feed == "sim"   # never mixed with the sim


def test_secrets_never_leave_and_apply_at_once(tmp_path, monkeypatch):
    m, notifier = build_market(tmp_path, monkeypatch, CHART_QUOTES_FEED="yahoo", CHART_HEARTBEAT_URL="https://hc.example/ping-env")

    async def go():
        await m.start([])
        try:
            assert m.feed_names == []
            m.connections.put("discord", {"discord_webhook": HOOK})
            m.connections.put("alpaca", {"enabled": True, "alpaca_key_id": "PKABCDEFGH", "alpaca_secret": "s3cretsecret"})
            await m.apply_connections("discord")
            await m.apply_connections("alpaca")
            assert notifier.discord == HOOK and m.feed_names == ["alpaca"]
            a = next(f for f in m.feed_objs if f.name == "alpaca")
            assert (a.key_id, a.secret) == ("PKABCDEFGH", "s3cretsecret")
            task = m.feed_tasks["alpaca"]
            m.connections.put("alpaca", {"enabled": False})
            await m.apply_connections("alpaca")
            assert m.feed_names == [] and task.done() and "alpaca" not in m.feed_tasks
            pub = m.connections.public(m)
            text = json.dumps(pub)
            assert HOOK not in text and "s3cretsecret" not in text and "PKABCDEFGH" not in text and "ping-env" not in text
            by = {c["id"]: c for c in pub}
            hook = by["discord"]["fields"][0]
            assert (hook["set"], hook["source"]) == (True, "app")
            beat = by["heartbeat"]["fields"][0]
            assert (beat["set"], beat["source"], beat["env"]) == (True, ".env", True)
            assert by["alpaca"]["fields"][0]["value"] is False and by["ninjatrader"]["locked"] == ""
            m.connections.put("discord", {"discord_webhook": None})   # back to .env: none there
            await m.apply_connections("discord")
            assert notifier.discord is None
        finally:
            await m.stop()
    asyncio.run(go())
    m2, _ = build_market(tmp_path, monkeypatch, CHART_QUOTES_FEED="yahoo")   # kept across restarts
    assert m2.s.alpaca_key_id == "PKABCDEFGH" and "alpaca" not in feed_names(m2.s)


def test_connection_tests(tmp_path, monkeypatch):
    seen = []

    def answer(req):
        seen.append(req)
        if "discord" in req.url.host:
            return httpx.Response(204)
        if "alpaca" in req.url.host:
            ok = req.headers.get("APCA-API-KEY-ID") == "PKGOODKEYS"
            return httpx.Response(200, json={"SPY": {"latestTrade": {"p": 512.25, "t": "2026-10-16T19:59:59.5Z"}}}) if ok else httpx.Response(403)
        return httpx.Response(500)
    m, _ = build_market(tmp_path, monkeypatch, http=httpx.AsyncClient(transport=httpx.MockTransport(answer)))
    from chartsrv.notify import Notifier
    m.notifier = Notifier(m.http, None, None)

    async def go():
        r = await conns.test(m, "discord", {"discord_webhook": HOOK})
        assert r == {"ok": True, "message": "Sent: check the channel"} and m.connections.saved == {}   # tried, not saved
        assert (await conns.test(m, "alpaca", {"alpaca_key_id": "PKBADKEYS1", "alpaca_secret": "whatever12"}))["message"] == "Alpaca refused the keys"
        r = await conns.test(m, "alpaca", {"alpaca_key_id": "PKGOODKEYS", "alpaca_secret": "whatever12"})
        assert r["ok"] and "SPY 512.25" in r["message"]
        r = await conns.test(m, "yahoo", {})
        assert r == {"ok": False, "message": "No answer (HTTP 500)"}
        srv = await asyncio.start_server(lambda r, w: w.close(), "127.0.0.1", 0)
        port = srv.sockets[0].getsockname()[1]
        assert (await conns.test(m, "ibkr", {"ibkr_host": "127.0.0.1", "ibkr_port": port}))["ok"]
        srv.close()
        await srv.wait_closed()
        assert not (await conns.test(m, "ibkr", {"ibkr_host": "127.0.0.1", "ibkr_port": port}))["ok"]
        assert not (await conns.test(m, "eia", {}))["ok"]
    asyncio.run(go())


# ---- Alpaca ----------------------------------------------------------------------------------------------------------
def _snap(trade_t, day_t, day_c, prev_c, vol=1000):
    return {"latestTrade": {"p": 101.5, "t": trade_t}, "dailyBar": {"t": day_t, "c": day_c, "v": vol},
            "prevDailyBar": {"t": "2026-10-14T04:00:00Z", "c": prev_c}}


def test_alpaca(tmp_path, monkeypatch):
    doc = {"AAPL": _snap("2026-10-16T14:31:02.123456789Z", "2026-10-16T04:00:00Z", 101.0, 99.0),   # in session
           "MSFT": _snap("2026-10-16T12:05:00Z", "2026-10-15T04:00:00Z", 100.0, 98.0),             # pre-market
           "BAD": {"latestTrade": {"p": None}}}
    got = alpaca.parse_snapshots(doc)
    assert got["AAPL"][:3] == (101.5, 99.0, None) and got["MSFT"][1] == 100.0 and "BAD" not in got
    assert alpaca.parse_snapshots({"snapshots": doc}, volume=True)["AAPL"][2] == 1000.0
    assert got["AAPL"][3] == int(datetime(2026, 10, 16, 14, 31, 2, 123000, tzinfo=ZoneInfo("UTC")).timestamp() * 1000)

    calls = []

    def answer(req):
        calls.append(req)
        if req.headers["APCA-API-KEY-ID"] != "PKABCDEFGH":
            return httpx.Response(401)
        return httpx.Response(200, json={k: v for k, v in doc.items() if k in req.url.params["symbols"].split(",")})
    m, _ = build_market(tmp_path, monkeypatch)
    m.watch_keys = {"AAPL", "MSFT", "ES1!", "SPX", "BTCUSD", "EURUSD", "FRED:DGS10", "NVDA"}
    assert alpaca.stock_keys(m) == ["AAPL", "MSFT", "NVDA"]
    s = load_settings()
    from dataclasses import replace
    feed = alpaca.AlpacaQuotes(replace(s, alpaca_key_id="PKABCDEFGH", alpaca_secret="s3cretsecret"))

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(answer), headers={"APCA-API-KEY-ID": "PKABCDEFGH"}) as http:
            assert await feed.poll(http, m) == 2
            assert await feed.poll(http, m) == 2   # NVDA, unknown to Alpaca, waits before it is asked again
        async with httpx.AsyncClient(transport=httpx.MockTransport(answer), headers={"APCA-API-KEY-ID": "nope"}) as http:
            with pytest.raises(PermissionError):
                await feed.poll(http, m)
    asyncio.run(go())
    assert calls[0].url.params["feed"] == "iex" and "NVDA" not in calls[1].url.params["symbols"]
    assert (m.quotes["AAPL"]["last"], m.quotes["AAPL"]["prev"], m.quote_from["AAPL"][0]) == (101.5, 99.0, "alpaca")


# ---- Coinbase --------------------------------------------------------------------------------------------------------
class FakeWs:
    def __init__(self, msgs):
        self.msgs, self.sent = list(msgs), []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def send(self, raw):
        self.sent.append(json.loads(raw))

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self.msgs:
            await asyncio.sleep(3600)
        return self.msgs.pop(0)


def test_coinbase(tmp_path, monkeypatch):
    assert [coinbase.product_for(k) for k in ("BTCUSD", "ETHUSDT", "USDCUSD", "AAPL", "SOLUSDC")] == ["BTC-USD", "ETH-USD", None, None, "SOL-USD"]
    m, _ = build_market(tmp_path, monkeypatch)
    m.watch_keys = {"BTCUSD", "ETHUSD", "DOGEUSD", "AAPL", "ES"}
    ws = FakeWs([json.dumps({"type": "subscriptions"}), "not json",
                 json.dumps({"type": "ticker", "product_id": "BTC-USD", "price": "64000.5", "open_24h": "63000",
                             "volume_24h": "12000.5", "time": "2026-10-18T15:00:00.123456Z"}),
                 json.dumps({"type": "error", "message": "Failed to subscribe", "reason": "DOGE-USD is not a valid product"})])
    feed = coinbase.CoinbaseQuotes(connect=lambda: ws)
    feed.valid = {"BTC-USD", "ETH-USD", "DOGE-USD"}

    async def go():
        task = asyncio.create_task(feed.run(m))
        for _ in range(100):
            if feed.bad and len(ws.sent) > 1:
                break
            await asyncio.sleep(0.01)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    asyncio.run(go())
    assert ws.sent[0] == {"type": "subscribe", "product_ids": ["BTC-USD", "DOGE-USD", "ETH-USD"], "channels": ["ticker"]}
    assert ws.sent[1]["product_ids"] == ["BTC-USD", "ETH-USD"]   # subscribed again without the one it refused
    q = m.quotes["BTCUSD"]
    assert (q["last"], q["prev"], m.quote_from["BTCUSD"][0]) == (64000.5, 63000.0, "coinbase")   # no day open read: 24 h

    # TradingView's crypto day: from the open at 00:00 UTC (Coinbase's daily candle), the first trade after midnight next
    day = 1_792_281_600   # 2026-10-18 00:00 UTC
    feed.day_open["BTC-USD"] = (day // 86_400, 63500.0)
    keys = {"BTC-USD": ["BTCUSD"]}
    feed.on_message(m, {"type": "ticker", "product_id": "BTC-USD", "price": "64000.5", "open_24h": "63000",
                        "time": "2026-10-18T15:00:00Z"}, keys)
    assert m.quotes["BTCUSD"]["prev"] == 63500.0
    feed.on_message(m, {"type": "ticker", "product_id": "BTC-USD", "price": "64100", "open_24h": "63000",
                        "time": "2026-10-19T00:00:01Z"}, keys)
    assert m.quotes["BTCUSD"]["prev"] == 64100.0 and feed.day_open["BTC-USD"] == (day // 86_400 + 1, 64100.0)

    class Candles:   # the daily candle, read when it subscribes
        async def get(self, url, **kw):
            assert url.endswith("/BTC-USD/candles") and kw["params"] == {"granularity": 86_400}
            today = int(time.time() // 86_400) * 86_400
            return httpx.Response(200, json=[[today, 1, 2, 65000.25, 3, 4], [today - 86_400, 1, 2, 64000, 3, 4]],
                                  request=httpx.Request("GET", url))
    asyncio.run(feed._day_opens(Candles(), ["BTC-USD"]))
    assert feed.day_open["BTC-USD"] == (int(time.time() // 86_400), 65000.25)


def test_bad_urls_and_double_saves(tmp_path, monkeypatch):
    """A URL httpx can't use is refused when saved; two saves at once still leave one feed, which turning it off
    stops."""
    with pytest.raises(ValueError):
        conns.clean("heartbeat", {"heartbeat_url": "https://hc-ping.com:abc/x"})
    m, _ = build_market(tmp_path, monkeypatch, CHART_QUOTES_FEED="alpaca")

    async def go():
        from chartsrv.feeds import make_feeds
        await m.start(make_feeds(m.s, m.instruments, m.levels))
        await asyncio.sleep(0.05)
        m.connections.put("alpaca", {"alpaca_feed": "iex"})
        await asyncio.gather(m.apply_connections("alpaca"), m.apply_connections("alpaca"))
        assert m.feed_names.count("alpaca") == 1
        m.connections.put("alpaca", {"enabled": False})
        await m.apply_connections("alpaca")
        running = [t for t in m.tasks if not t.done() and "_run_feed" in repr(t.get_coro())]
        assert "alpaca" not in m.feed_names
        assert all(t.get_coro().cr_frame.f_locals["feed"].name != "alpaca" for t in running)
        await m.stop()
    asyncio.run(go())
