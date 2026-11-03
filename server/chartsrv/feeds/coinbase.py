"""Real-time crypto quotes from Coinbase Exchange's public websocket, no account: every crypto pair in the watchlists
that Coinbase trades against the dollar (BTCUSD, ETHUSD, and their USDT/USDC twins, quoted at the dollar pair). The
change is against the day's open at 00:00 UTC, as TradingView counts a crypto day (Coinbase's daily candle; the first
trade after midnight opens the next day); against 24 hours ago only when that can't be read."""
import asyncio
import json
import logging
import re
import time
from datetime import datetime, timezone

import httpx

from .. import stopping

log = logging.getLogger(__name__)
WS = "wss://ws-feed.exchange.coinbase.com"
PRODUCTS = "https://api.exchange.coinbase.com/products"
KEY_RE = re.compile(r"^([A-Z0-9]{2,10}?)(USD|USDT|USDC)$")
STABLE = {"USDT", "USDC", "DAI", "PYUSD", "USD"}


def product_for(key: str) -> str | None:
    """"BTCUSD" -> "BTC-USD"."""
    m = KEY_RE.match(key)
    return f"{m.group(1)}-USD" if m and m.group(1) not in STABLE else None


def _num(x) -> float | None:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if v == v and v > 0 else None


def iso_ms(s) -> int | None:
    m = re.match(r"^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.(\d+))?Z$", str(s or ""))
    if not m:
        return None
    t = datetime.fromisoformat(m.group(1)).replace(tzinfo=timezone.utc)
    return int(t.timestamp() * 1000) + int((m.group(2) or "0")[:3].ljust(3, "0"))


class CoinbaseQuotes:
    name = "coinbase"

    def __init__(self, connect=None, http=None):
        self.connect = connect   # tests: () -> async context manager with send() and async iteration
        self.http = http         # for the product list; else the server's own client
        self.changed = asyncio.Event()
        self.valid: set[str] | None = None   # Coinbase's USD products, once read
        self.bad: set[str] = set()           # products it refused
        self.day_open: dict[str, tuple[int, float]] = {}   # product -> (UTC day number, its open at 00:00 UTC)
        self.stats = {"connected": False, "ok": 0, "errors": 0, "symbols": 0, "last_msg_ms": None, "last_error": ""}

    def watch_changed(self, market):
        self.changed.set()

    def wanted(self, market) -> dict[str, list[str]]:
        """Coinbase product -> the watchlist keys it quotes."""
        type_of = getattr(getattr(market, "symbols", None), "type_of", lambda k: None)
        out: dict[str, list[str]] = {}
        for key in sorted(market.watch_keys):
            p = product_for(key)
            if not p or p in self.bad or type_of(key) not in (None, "crypto"):
                continue
            if self.valid is not None and p not in self.valid:
                continue
            out.setdefault(p, []).append(key)
        return out

    async def _products(self, http):
        if self.valid is not None or http is None:
            return
        try:
            r = await http.get(PRODUCTS, headers={"Accept": "application/json"}, timeout=20)
            r.raise_for_status()
            self.valid = {p["id"] for p in r.json() if isinstance(p, dict) and p.get("quote_currency") == "USD"
                          and isinstance(p.get("id"), str)}
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as e:   # the subscription's own errors still tell
            log.info("Coinbase product list not read (%s)", type(e).__name__)

    async def _day_opens(self, http, products):
        """Each product's open at 00:00 UTC today, from its daily candle ([time, low, high, open, close, volume])."""
        today = int(time.time() // 86_400)
        for p in products:
            if http is None or self.day_open.get(p, (None,))[0] == today:
                continue
            try:
                r = await http.get(f"{PRODUCTS}/{p}/candles", params={"granularity": 86_400},
                                   headers={"Accept": "application/json"}, timeout=15)
                r.raise_for_status()
                for c in r.json():
                    if isinstance(c, list) and len(c) >= 4 and int(c[0]) // 86_400 == today and _num(c[3]):
                        self.day_open[p] = (today, float(c[3]))
                        break
            except Exception as e:   # the change falls back to 24 hours ago
                log.info("Coinbase day open for %s not read (%s)", p, type(e).__name__)
            stopping.check()   # a stop the request swallowed (stopping.py): no more of them, and no socket

    def on_message(self, market, msg: dict, keys: dict[str, list[str]]):
        t = msg.get("type")
        if t == "ticker":
            last, p = _num(msg.get("price")), msg.get("product_id")
            at = iso_ms(msg.get("time"))
            day = (at or int(time.time() * 1000)) // 86_400_000
            o = self.day_open.get(p)
            if o and o[0] < day and last:   # a new UTC day: its first trade opens it
                o = self.day_open[p] = (day, last)
            prev = o[1] if o and o[0] == day else _num(msg.get("open_24h"))
            for k in keys.get(p, ()):
                market.on_quote(k, last, prev, _num(msg.get("volume_24h")), source="coinbase", at=at)
            self.stats["ok"] += 1
            self.stats["last_msg_ms"] = int(time.time() * 1000)
        elif t == "error":
            reason = str(msg.get("reason") or msg.get("message") or "")[:200]
            m = re.search(r"([A-Z0-9]{2,10}-USD) is not a valid product", reason)
            if m:
                self.bad.add(m.group(1))
                self.changed.set()   # subscribe again without it
            self.stats["errors"] += 1
            self.stats["last_error"] = reason

    async def _session(self, market, keys: dict[str, list[str]]):
        if self.connect:
            cm = self.connect()
        else:
            from websockets.asyncio.client import connect
            cm = connect(WS, open_timeout=15, ping_interval=20, max_size=2 ** 20)
        await self._day_opens(self.http or getattr(market, "http", None), sorted(keys))
        async with cm as ws:
            await ws.send(json.dumps({"type": "subscribe", "product_ids": sorted(keys), "channels": ["ticker"]}))
            self.stats["connected"] = True
            log.info("Coinbase connected, %d products", len(keys))
            async for raw in ws:
                try:
                    msg = json.loads(raw)
                except ValueError:
                    continue
                if isinstance(msg, dict):
                    self.on_message(market, msg, keys)
                if self.changed.is_set():
                    return

    async def run(self, market):
        delay = 5
        while True:
            self.changed.clear()
            await self._products(self.http or getattr(market, "http", None))
            stopping.check()   # a stop the request swallowed (stopping.py): not on to wait for the socket
            keys = self.wanted(market)
            self.stats["symbols"] = sum(len(v) for v in keys.values())
            if not keys:
                self.stats["connected"] = False
                await self.changed.wait()
                continue
            reader = asyncio.create_task(self._session(market, keys))
            changed = asyncio.create_task(self.changed.wait())
            try:
                await asyncio.wait([reader, changed], return_when=asyncio.FIRST_COMPLETED)
            finally:
                for t in (reader, changed):
                    t.cancel()
                await asyncio.gather(reader, changed, return_exceptions=True)
            self.stats["connected"] = False
            err = reader.exception() if reader.done() and not reader.cancelled() else None
            if self.changed.is_set():
                delay = 5
                continue
            if err:
                self.stats["errors"] += 1
                self.stats["last_error"] = type(err).__name__
                log.warning("Coinbase disconnected (%s); again in %ds", type(err).__name__, delay)
            await asyncio.sleep(delay)
            delay = min(120, delay * 2) if err else 5

