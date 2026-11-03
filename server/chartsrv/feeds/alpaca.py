"""Watchlist quotes for U.S. stocks and ETFs from Alpaca's market data API, in real time: IEX's trades with a free
account (alpaca.markets; a paper-trading account is enough, no deposit), or every exchange's (SIP) with a paid data
plan. Market data only: this app never sends Alpaca an order. Keys under Connections > Alpaca, or ALPACA_KEY_ID and
ALPACA_SECRET in .env.

It asks for a snapshot of every stock in the lists every few seconds (the free plan allows 200 requests a minute).
The change is against the previous session's close; IEX's volume is a small slice of the day's, so it is left out."""
import asyncio
import logging
import re
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import httpx

from .. import stopping
from .yahoo import yahoo_symbol

log = logging.getLogger(__name__)
API = "https://data.alpaca.markets/v2/stocks/snapshots"
ASSETS = ("https://paper-api.alpaca.markets/v2/assets", "https://api.alpaca.markets/v2/assets")   # paper keys, live keys
ALPACA_EXCHANGES = {"NASDAQ": "NASDAQ", "NYSE": "NYSE", "ARCA": "NYSE Arca", "NYSEARCA": "NYSE Arca", "AMEX": "NYSE American",
                    "BATS": "Cboe BZX", "OTC": "OTC"}
BATCH = 200
POLL_S = 5
UNKNOWN_RETRY_S = 1800
ET = ZoneInfo("America/New_York")
STOCK_RE = re.compile(r"^[A-Z]{1,5}(\.[A-Z]{1,2})?$")
STOCK_TYPES = (None, "stock", "etf", "fund")



def parse_assets(items) -> list[tuple]:
    """Alpaca's /v2/assets: every active US stock and ETF it has, OTC ones too (symbol, name, type, exchange, yahoo, rank)."""
    rows = []
    for a in items or []:
        if not isinstance(a, dict) or a.get("class") != "us_equity" or a.get("status") != "active":
            continue
        sym = str(a.get("symbol") or "").upper()
        if STOCK_RE.match(sym):
            ex = ALPACA_EXCHANGES.get(str(a.get("exchange") or ""), str(a.get("exchange") or ""))
            rows.append((sym, str(a.get("name") or sym), "stock", ex, "", -1 if ex == "OTC" else 0))
    return rows


async def assets(http: httpx.AsyncClient, key_id: str, secret: str) -> list[tuple]:
    """The asset list, from the paper endpoint (the usual free keys) or else the live one."""
    headers = {"APCA-API-KEY-ID": key_id, "APCA-API-SECRET-KEY": secret}
    for url in ASSETS:
        r = await http.get(url, params={"status": "active", "asset_class": "us_equity"}, headers=headers, timeout=60)
        if r.status_code in (401, 403):
            continue
        r.raise_for_status()
        rows = parse_assets(r.json())
        if not rows:
            raise ValueError("Alpaca listed no assets")
        return rows
    raise ValueError("Alpaca refused the keys")

def ts_ms(s) -> int | None:
    """Alpaca's RFC 3339 times (nanoseconds and all) as epoch ms."""
    m = re.match(r"^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.(\d+))?(Z|[+-]\d\d:\d\d)$", str(s or ""))
    if not m:
        return None
    t = datetime.fromisoformat(m.group(1) + (m.group(3) if m.group(3) != "Z" else "+00:00"))
    return int(t.timestamp() * 1000) + int((m.group(2) or "0")[:3].ljust(3, "0"))


def _et_day(ms: int | None):
    return datetime.fromtimestamp(ms / 1000, timezone.utc).astimezone(ET).date() if ms is not None else None


def parse_snapshots(doc, volume: bool = False) -> dict[str, tuple]:
    """{symbol: (last, previous close, day volume or None, trade time ms)}. Before the open (or on a holiday) the
    daily bar is the last session's, so the change is against its close."""
    snaps = doc.get("snapshots") if isinstance(doc, dict) and isinstance(doc.get("snapshots"), dict) else doc
    out = {}
    for sym, s in (snaps or {}).items() if isinstance(snaps, dict) else ():
        if not isinstance(s, dict):
            continue
        trade, day, prev_day = s.get("latestTrade") or {}, s.get("dailyBar") or {}, s.get("prevDailyBar") or {}
        last = trade.get("p")
        if not isinstance(last, (int, float)) or isinstance(last, bool) or last != last or last <= 0:
            continue
        at = ts_ms(trade.get("t"))
        current = bool(day) and _et_day(ts_ms(day.get("t"))) == _et_day(at)
        prev = prev_day.get("c") if current else day.get("c")
        vol = day.get("v") if volume and current else None
        out[sym] = (float(last), float(prev) if isinstance(prev, (int, float)) else None,
                    float(vol) if isinstance(vol, (int, float)) else None, at)
    return out


def stock_keys(market) -> list[str]:
    """The watchlist keys that are U.S. stock or ETF tickers (not futures, indices, currencies or crypto). A stock
    whose ticker is a futures root (ZS, Zscaler) is one: the future is keyed ZS1!."""
    type_of = getattr(getattr(market, "symbols", None), "type_of", lambda k: None)
    out = []
    for key in sorted(market.watch_keys):
        if not STOCK_RE.match(key) or type_of(key) not in STOCK_TYPES:
            continue
        if yahoo_symbol(key) == key.replace(".", "-"):   # Yahoo would read it as a stock too (VIX, DXY: no)
            out.append(key)
    return out


class AlpacaQuotes:
    name = "alpaca"

    def __init__(self, settings, transport=None):
        self.key_id, self.secret = settings.alpaca_key_id or "", settings.alpaca_secret or ""
        self.sip = getattr(settings, "alpaca_feed", "iex") == "sip"
        self.transport = transport   # tests
        self.unknown: dict[str, float] = {}   # tickers Alpaca had nothing for -> when to ask again
        self.stats = {"ok": 0, "errors": 0, "symbols": 0, "last_msg_ms": None, "last_error": ""}

    def watch_changed(self, market):
        self.unknown.clear()

    async def poll(self, http, market) -> int:
        now = time.monotonic()
        keys = [k for k in stock_keys(market) if self.unknown.get(k, 0) <= now and market.quote_wanted(k, "alpaca")]
        self.stats["symbols"] = len(keys)
        n = 0
        for i in range(0, len(keys), BATCH):
            part = keys[i:i + BATCH]
            r = await http.get(API, params={"symbols": ",".join(part), "feed": "sip" if self.sip else "iex"})
            if r.status_code in (401, 403):
                raise PermissionError("Alpaca refused the keys" + (" (SIP needs a paid data plan)" if self.sip else ""))
            r.raise_for_status()
            got = parse_snapshots(r.json(), volume=self.sip)
            for k in part:
                q = got.get(k)
                if q is None:
                    self.unknown[k] = now + UNKNOWN_RETRY_S
                    continue
                market.on_quote(k, q[0], q[1], q[2], source="alpaca", at=q[3])
                n += 1
        return n

    async def run(self, market):
        if not (self.key_id and self.secret):
            self.stats["last_error"] = "needs an API key and secret"
            log.warning("Alpaca quotes need keys: Connections > Alpaca")
            await asyncio.Event().wait()   # until Connections restarts it with keys
        delay = POLL_S
        headers = {"APCA-API-KEY-ID": self.key_id, "APCA-API-SECRET-KEY": self.secret, "Accept": "application/json"}
        async with httpx.AsyncClient(headers=headers, timeout=15, transport=self.transport) as http:
            while True:
                try:
                    await self.poll(http, market)
                    self.stats["ok"] += 1
                    self.stats["last_msg_ms"], self.stats["last_error"] = int(time.time() * 1000), ""
                    delay = POLL_S
                except PermissionError as e:
                    self.stats["errors"] += 1
                    self.stats["last_error"] = str(e)
                    delay = 300
                    log.warning("%s; next try in %ds", e, delay)
                except (httpx.HTTPError, ValueError) as e:   # never the request itself in the log: headers hold keys
                    self.stats["errors"] += 1
                    self.stats["last_error"] = type(e).__name__ + (f" {e.response.status_code}" if isinstance(e, httpx.HTTPStatusError) else "")
                    delay = min(300, delay * 2)
                    log.warning("Alpaca quotes failed (%s); next try in %ds", self.stats["last_error"], delay)
                await stopping.pause(delay)   # not after a stop the request swallowed (stopping.py)


# ---- chart history: real-time intraday stock charts -----------------------------------------------------------------
BARS_API = "https://data.alpaca.markets/v2/stocks/bars"
SIP_DELAY_S = 16 * 60        # the free plan's SIP history ends 15 minutes ago; IEX fills the rest, live
HIST_S = 300                 # a chart's SIP history is used again this long (a reload, another timeframe), then asked again
RTH = (9 * 60 + 30, 16 * 60)  # regular hours, ET minutes; bars are counted from the open, as TradingView's
EXT = (4 * 60, 20 * 60)       # with the pre- and post-market (ETH): bars counted from 4:00, on the hour as TradingView's
MAX_PAGES = 8


def is_stock(market, key: str) -> bool:
    type_of = getattr(getattr(market, "symbols", None), "type_of", lambda k: None)
    return bool(STOCK_RE.match(key)) and type_of(key) in STOCK_TYPES and yahoo_symbol(key) == key.replace(".", "-")


def spec_minutes(spec: str) -> int | None:
    """Minutes of an intraday spec Alpaca can chart ("3m" 3, "2h" 120), up to a whole session; else None."""
    m = re.fullmatch(r"(\d+)([mh])", spec)
    if not m:
        return None
    n = int(m.group(1)) * (60 if m.group(2) == "h" else 1)
    return n if 1 <= n <= RTH[1] - RTH[0] else None


def base_minutes(minutes: int) -> int:
    """The bar size to ask Alpaca for: the largest of 30, 15, 5, 1 that divides the chart's."""
    return next(b for b in (30, 15, 5, 1) if minutes % b == 0)


def parse_bars(doc, symbol: str) -> list[tuple[int, float, float, float, float, float]]:
    """(start ms, o, h, l, c, v) from a bars answer (multi-symbol, keyed by symbol, or single-symbol, a list)."""
    raw = (doc or {}).get("bars")
    rows = raw.get(symbol) if isinstance(raw, dict) else raw
    out = []
    for b in rows or []:
        try:
            t = ts_ms(b["t"])
            o, h, lo, c = (float(b[k]) for k in "ohlc")
            v = float(b.get("v") or 0)
        except (KeyError, TypeError, ValueError):
            continue
        if t is not None and min(o, h, lo, c) > 0:
            out.append((t, o, h, lo, c, v))
    return out


def session_bars(rows, minutes: int, extended: bool = False) -> list[dict]:
    """Bars of `minutes` from smaller bars (oldest first): regular hours counted from 9:30 ET, or with `extended` the
    4:00-20:00 ET day counted from 4:00."""
    span = EXT if extended else RTH
    out: list[dict] = []
    for t, o, h, lo, c, v in sorted(rows):
        et = datetime.fromtimestamp(t / 1000, timezone.utc).astimezone(ET)
        tod = et.hour * 60 + et.minute
        if et.weekday() >= 5 or not span[0] <= tod < span[1]:
            continue
        k = span[0] + (tod - span[0]) // minutes * minutes
        start = et.replace(hour=k // 60, minute=k % 60, second=0, microsecond=0)
        bt = int(start.timestamp() * 1000)
        if out and out[-1]["t"] == bt:
            b = out[-1]
            b["h"], b["l"], b["c"], b["v"] = max(b["h"], h), min(b["l"], lo), c, b["v"] + int(v)
        elif not out or bt > out[-1]["t"]:
            out.append({"t": bt, "o": o, "h": h, "l": lo, "c": c, "v": int(v)})
    return out


class AlpacaBars:
    """Intraday history for a stock chart: the consolidated tape (every exchange) up to 15 minutes ago, which the free
    plan allows, then IEX's trades to now. With a paid plan (SIP) all of it is the consolidated tape."""

    def __init__(self, http):
        self.http = http
        self.hist: dict[tuple[str, int], tuple[float, int, list, int]] = {}   # (symbol, base) -> (when, cutoff, rows, start)

    async def _get(self, settings, symbol, tf, start_ms, end_ms, feed) -> list:
        headers = {"APCA-API-KEY-ID": settings.alpaca_key_id, "APCA-API-SECRET-KEY": settings.alpaca_secret}
        iso = lambda ms: datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")   # noqa: E731
        # newest first, so a span longer than MAX_PAGES pages loses its oldest bars, not its latest
        params = {"symbols": symbol, "timeframe": tf, "start": iso(start_ms), "end": iso(end_ms), "limit": 10000,
                  "adjustment": "split", "feed": feed, "sort": "desc"}
        rows: list = []
        for _ in range(MAX_PAGES):
            r = await self.http.get(BARS_API, params=params, headers=headers, timeout=20)
            if r.status_code in (401, 403):
                raise PermissionError(r.status_code)
            r.raise_for_status()
            doc = r.json()
            rows += parse_bars(doc, symbol)
            if not doc.get("next_page_token"):
                break
            params["page_token"] = doc["next_page_token"]
        rows.sort()
        return rows

    async def bars(self, settings, symbol: str, minutes: int, days: int, extended: bool = False) -> tuple[list[dict], str]:
        """(bars, feed note); `extended`: with the pre- and post-market. PermissionError: the keys are refused."""
        now = int(time.time() * 1000)
        base = base_minutes(minutes)
        start = now - days * 86_400_000
        if settings.alpaca_feed == "sip":
            try:
                return session_bars(await self._get(settings, symbol, f"{base}Min", start, now, "sip"), minutes, extended), "sip"
            except PermissionError:
                pass   # no SIP plan after all: as the free plan
        key = (symbol, base)
        hit = self.hist.get(key)
        if hit and time.time() - hit[0] < HIST_S and hit[2] and hit[3] <= start + 86_400_000:
            cutoff, hist = hit[1], hit[2]
        else:
            cutoff = (now - SIP_DELAY_S * 1000) // (base * 60_000) * (base * 60_000)
            try:
                hist = [r for r in await self._get(settings, symbol, f"{base}Min", start, cutoff, "sip")
                        if r[0] + base * 60_000 <= cutoff]
            except PermissionError:
                if hit is None and not await self._ok(settings):
                    raise
                return session_bars(await self._get(settings, symbol, f"{base}Min", start, now, "iex"), minutes, extended), "iex"
            for k in [k for k, v in self.hist.items() if time.time() - v[0] >= HIST_S]:   # too old to be used: they go
                del self.hist[k]
            self.hist[key] = (time.time(), cutoff, hist, start)
            if len(self.hist) > 50:
                self.hist.pop(min(self.hist, key=lambda k: self.hist[k][0]))
        tail = await self._get(settings, symbol, "1Min", cutoff, now, "iex")
        return session_bars(hist + [r for r in tail if r[0] >= cutoff], minutes, extended), "sip+iex"

    async def _ok(self, settings) -> bool:
        """Do the keys work at all (IEX)? Then SIP was what was refused."""
        try:
            await self._get(settings, "SPY", "1Day", int(time.time() * 1000) - 10 * 86_400_000, int(time.time() * 1000), "iex")
            return True
        except PermissionError:
            return False
