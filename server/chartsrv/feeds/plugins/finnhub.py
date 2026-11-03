"""Real-time quotes for the watchlists' US stocks and ETFs from Finnhub's websocket (a free key at finnhub.io): every
trade, its change against the previous close (Finnhub's quote endpoint, asked per symbol at connect and again every
hour while connected). The free plan streams up to 50 symbols."""
import asyncio
import json
import logging
import re
import time

import httpx

from chartsrv import stopping
from chartsrv.feeds.sdk import FeedPlugin, secret

log = logging.getLogger(__name__)
WS = "wss://ws.finnhub.io"
REST = "https://finnhub.io/api/v1/quote"
LIST = "https://finnhub.io/api/v1/stock/symbol"
MICS = {"XNAS": "NASDAQ", "XNYS": "NYSE", "ARCX": "NYSE Arca", "XASE": "NYSE American", "BATS": "Cboe BZX", "IEXG": "IEX",
        "OTCM": "OTC", "OOTC": "OTC", "PINX": "OTC"}
STOCK_RE = re.compile(r"^[A-Z]{1,5}(\.[A-Z])?$")
MAX_SYMBOLS = 50
PREV_EVERY_S = 3600   # the previous close is asked again after an hour: a socket up through a new session moves to its close


class FinnhubQuotes(FeedPlugin):
    TITLE = "Finnhub (stocks, real time)"
    KIND = "quotes"
    ABOUT = ("Real-time US stock and ETF quotes for the watchlists, from Finnhub's websocket. Free key at finnhub.io "
             "(Dashboard); the free plan streams up to 50 symbols.")
    FIELDS = [secret("api_key", "API key", "finnhub.io > Dashboard > API key")]

    keys: set = set()   # the symbols it streams now

    def wanted(self, market) -> list[str]:
        type_of = getattr(getattr(market, "symbols", None), "type_of", lambda k: None)
        keys = [k for k in super().wanted(market) if STOCK_RE.match(k) and type_of(k) in (None, "stock", "etf")]
        return keys[:MAX_SYMBOLS]

    def _quote_key(self, market, symbol: str) -> str:
        """Its symbols are stocks: ZS is Zscaler's quote, never soybeans' ZS1! (a ZS row deleted while closes() runs
        on, the future still listed)."""
        return symbol

    def watch_changed(self, market):
        if hasattr(self, "changed") and set(self.wanted(market)) != self.keys:   # a flag or a new order: nothing to do
            self.changed.set()

    async def run(self, market):
        from websockets.asyncio.client import connect

        self.changed = asyncio.Event()
        key = self.values["api_key"]
        if not key:
            raise ValueError("Enter the API key")
        prev, asked = {}, {}   # symbol -> its previous close; when that was asked (time.monotonic())
        while True:
            self.changed.clear()
            keys = self.wanted(market)
            self.keys = set(keys)
            if not keys:
                self.waiting("No US stocks or ETFs in the watchlists")
                await self.changed.wait()
                continue
            prev = {k: v for k, v in prev.items() if k in self.keys}
            await self.closes(market, key, keys, prev, asked)
            async with connect(f"{WS}?token={key}", open_timeout=15, ping_interval=20, max_size=2 ** 20) as ws:
                for k in keys:
                    await ws.send(json.dumps({"type": "subscribe", "symbol": k}))
                self.connected()
                reader = asyncio.create_task(self.read(ws, market, prev))
                changed = asyncio.create_task(self.changed.wait())
                while not (done := (await asyncio.wait({reader, changed}, timeout=PREV_EVERY_S, return_when=asyncio.FIRST_COMPLETED))[0]):
                    await self.closes(market, key, keys, prev, asked, quote=False)   # the socket kept: its trades go on
                reader.cancel()
                changed.cancel()
                if reader in done:
                    reader.result()   # its error, or the connection closed
                    raise ConnectionError("Finnhub closed the connection")

    async def closes(self, market, key: str, keys: list[str], prev: dict, asked: dict, quote: bool = True):
        """The previous close, for the change, of each symbol not asked in the last PREV_EVERY_S; at connect a price at
        once too."""
        for k in keys:
            if k in prev and time.monotonic() - asked[k] < PREV_EVERY_S:
                continue
            q = await self.rest(market.http, key, k)
            if q:
                prev[k], asked[k] = q.get("pc") or None, time.monotonic()
                if quote and q.get("c"):
                    self.quote(market, k, q["c"], prev[k], at=int(q["t"]) * 1000 if q.get("t") else None)
            await stopping.pause(1.1)   # the free plan: 60 calls a minute (none after a stop it swallowed: stopping.py)

    async def read(self, ws, market, prev: dict):
        async for raw in ws:
            msg = json.loads(raw)
            if msg.get("type") == "trade":
                for d in msg.get("data") or []:
                    s = d.get("s")
                    if s in prev or s in self.symbols or not self.symbols:
                        self.quote(market, s, d.get("p"), prev.get(s), at=d.get("t"))
            elif msg.get("type") == "error":
                raise RuntimeError(f"Finnhub: {msg.get('msg')}")

    async def listing(self, http):
        """Finnhub's US symbol list (OTC ones too), for the search box."""
        r = await http.get(LIST, params={"exchange": "US", "token": self.values["api_key"]}, timeout=60)
        if r.status_code in (401, 403):
            raise ValueError("Finnhub refused the key")
        r.raise_for_status()
        out = []
        for s in r.json() or []:
            sym = str(s.get("symbol") or "").upper() if isinstance(s, dict) else ""
            if STOCK_RE.match(sym):
                out.append((sym, str(s.get("description") or sym).title(), "etf" if s.get("type") == "ETP" else "stock",
                            MICS.get(str(s.get("mic") or ""), str(s.get("mic") or ""))))
        return out

    @staticmethod
    async def rest(http, key: str, sym: str) -> dict | None:
        try:
            r = await http.get(REST, params={"symbol": sym, "token": key}, timeout=15)
            if r.status_code in (401, 403):
                raise ValueError("Finnhub refused the key")
            r.raise_for_status()
            return r.json()
        except (httpx.HTTPError, json.JSONDecodeError) as e:
            log.info("Finnhub quote for %s not read (%s)", sym, type(e).__name__)
            return None

    @classmethod
    async def test(cls, values: dict) -> tuple[bool, str]:
        key = values.get("api_key") or ""
        if not key:
            return False, "Enter the API key"
        async with httpx.AsyncClient() as http:
            try:
                q = await cls.rest(http, key, "AAPL")
            except ValueError as e:
                return False, str(e)
        return (True, f"The key works: AAPL {q['c']:.2f}") if q and q.get("c") else (False, "Finnhub did not answer")
