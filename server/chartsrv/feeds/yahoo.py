"""Watchlist quotes from Yahoo Finance, the backup source: stocks, ETFs, indices, FX, crypto, and futures when neither
NinjaTrader nor IBKR is quoting them. Yahoo's quotes can lag (futures about 10 minutes, some exchanges 15-20), so a
symbol that NinjaTrader or IBKR quoted in the last two minutes keeps their quote (Market.on_quote).

It asks Yahoo's public chart endpoints, no account or key: the spark endpoint for 20 symbols a request, and, if that
is refused, the chart endpoint one symbol at a time, more slowly. TradingView symbols become Yahoo's by yahoo_symbol()
(ES1! -> ES=F, SPX -> ^GSPC, EURUSD -> EURUSD=X, BTCUSD -> BTC-USD, ZS -> ZS: the stock, never ZS1!'s ZS=F);
config/yahoo.json adds or replaces any.

CloseFill asks the same daily bars for the previous close of a stock, ETF or index another source quotes without one
(IBKR sends no close for some tickers), whether or not these quotes are on."""
import asyncio
import json
import logging
import re
import time
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx

from .. import stopping, watchlists as wl

log = logging.getLogger(__name__)
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/124.0 Safari/537.36")
HOSTS = ("https://query1.finance.yahoo.com", "https://query2.finance.yahoo.com")
BATCH = 20
UNKNOWN_RETRY_S = 1800   # a ticker Yahoo has no price for is asked again after this, or when the lists change

FUTURES = {"ES", "NQ", "RTY", "YM", "MES", "MNQ", "M2K", "MYM", "CL", "MCL", "NG", "RB", "HO", "BZ", "GC", "MGC", "SI",
           "SIL", "HG", "PL", "PA", "ZB", "ZN", "ZF", "ZT", "UB", "TN", "ZC", "ZS", "ZW", "ZL", "ZM", "LE", "HE", "GF",
           "SR3", "6E", "6J", "6B", "6A", "6C", "6S", "6M", "NKD", "NIY"}
# TradingView names Yahoo spells differently. Futures without a Yahoo twin (FDAX, NCF) are left unquoted rather than
# shown at another instrument's price, but FTUK1! (no free FTSE 100 futures quote) shows the FTSE 100 index;
# config/yahoo.json can map others (e.g. "FDAX": "^GDAXI" for the index).
NAMES = {
    "SPX": "^GSPC", "NDX": "^NDX", "VIX": "^VIX", "VVIX": "^VVIX", "SKEW": "^SKEW", "MOVE": "^MOVE", "DJI": "^DJI",
    "RUT": "^RUT", "DXY": "DX-Y.NYB", "HSI": "^HSI", "KOSPI": "^KS11", "NI225": "^N225", "NIFTY": "^NSEI",
    "SX5E": "^STOXX50E", "TNX": "^TNX", "US10Y": "^TNX", "US30Y": "^TYX", "US05Y": "^FVX", "US02Y": "2YY=F",
    "BRENT": "BZ=F", "PALLADIUM": "PA=F", "PLATINUM": "PL=F", "XAUUSD": "GC=F", "XAGUSD": "SI=F", "XCUUSD": "HG=F",
    "NK225": "NIY=F", "USOIL": "CL=F", "UKOIL": "BZ=F", "FTUK": "^FTSE",
}
CURRENCIES = {"USD", "EUR", "GBP", "JPY", "CHF", "CAD", "AUD", "NZD", "CNH", "CNY", "HKD", "SEK", "NOK", "MXN", "SGD",
              "ZAR", "TRY", "INR", "KRW", "BRL", "DKK", "PLN"}
CRYPTO = {"BTC", "ETH", "SOL", "XRP", "LTC", "BCH", "DOGE", "ADA", "AVAX", "LINK", "DOT"}
STOCK_RE = re.compile(r"^[A-Z]{1,5}([.\-][A-Z]{1,2})?$")
CRYPTO_RE = re.compile(r"^([A-Z]{2,5})(USDT?|USDC|EUR)$")
NY = ZoneInfo("America/New_York")


def yahoo_symbol(key: str, extra: dict | None = None, future: bool = False) -> str | None:
    """Yahoo's ticker for a watchlist quote key (a future as "ES1!", or a TradingView ticker), or None. A bare key
    is never read as a future (ZS is Zscaler), unless future: it is a futures root ("FDAX" from FDAX1!), never to be
    read as a stock ticker."""
    if root := wl.future_root(key):
        if extra and key in extra:
            return extra[key] or None
        key, future = root, True
    if extra and key in extra:
        return extra[key] or None
    if key in NAMES:
        return NAMES[key]
    if future:
        return f"{key}=F" if key in FUTURES else None
    if len(key) == 6 and key[:3] in CURRENCIES and key[3:] in CURRENCIES:
        return f"{key[3:]}=X" if key[:3] == "USD" else f"{key}=X"
    m = CRYPTO_RE.match(key)
    if m and m.group(1) in CRYPTO:
        return f"{m.group(1)}-{'USD' if m.group(2).startswith('USD') else m.group(2)}"
    if STOCK_RE.match(key):
        return key.replace(".", "-")   # BRK.B -> BRK-B
    return None


def clock_of(key: str) -> str:
    """Whose clock a quote key's trading day runs on (ext.py's, for its chart): "fx" a currency pair (the 17:00 New
    York roll), "crypto" (00:00 UTC), else "stock": a stock's, ETF's or index's, from midnight New York. A future's
    day is CME's, its settlement's (settlement.py), not this."""
    if len(key) == 6 and key[:3] in CURRENCIES and key[3:] in CURRENCIES:
        return "fx"
    m = CRYPTO_RE.match(key)
    return "crypto" if m and m.group(1) in CRYPTO else "stock"


def market_day(key: str, ms: int) -> date:
    """The trading day a quote of `key` at `ms` counts in, on its clock (clock_of): a stock's pre-market, session and
    after-hours are one day, from midnight New York, as Yahoo's and Alpaca's quotes count it."""
    clock = clock_of(key)
    if clock == "crypto":
        return datetime.fromtimestamp(ms / 1000, timezone.utc).date()
    local = datetime.fromtimestamp(ms / 1000, NY)
    return (local + timedelta(hours=7)).date() if clock == "fx" else local.date()


def ticker_for(key: str, extra: dict | None = None, known=None) -> str | None:
    """Yahoo's ticker for a quote key: config/yahoo.json's (a future's as "FDAX1!", or by its root, as it was named
    before), else the symbol database's (known: its yahoo_for), else yahoo_symbol()'s."""
    root = wl.future_root(key)
    for k in (key, root) if root else (key,):
        if extra and k in extra:
            return extra[k] or None
    return (known(key) if known else None) or yahoo_symbol(key)


def _closes(resp: dict) -> tuple[list, list]:
    """A response's daily bar times (s) and closes."""
    ts = resp.get("timestamp") or []
    q = ((resp.get("indicators") or {}).get("quote") or [{}])[0] or {}
    return (ts if isinstance(ts, list) else []), (q.get("close") or [] if isinstance(q, dict) else [])


def _last_before(ts: list, closes: list, off, at) -> float | None:
    """The last daily close before the exchange's calendar day of `at` (s), its days by its UTC offset `off` (s)."""
    if not isinstance(at, (int, float)) or not ts or not closes:
        return None
    day = lambda t: (int(t) + int(off or 0)) // 86_400   # noqa: E731  (the exchange's calendar day)
    before = [c for t, c in zip(ts, closes) if isinstance(t, (int, float)) and isinstance(c, (int, float))
              and not isinstance(c, bool) and c == c and day(t) < day(at)]
    return float(before[-1]) if before else None


def close_before(resp: dict, ms: int) -> float | None:
    """A spark or chart response's previous close for a quote at `ms`: its last daily close before that quote's
    trading day (the exchange's calendar day), whatever the day of Yahoo's own latest price. None without one."""
    return _last_before(*_closes(resp or {}), ((resp or {}).get("meta") or {}).get("gmtoffset"), ms / 1000)


def quote_of(meta: dict, ts: list | None = None, closes: list | None = None) -> tuple[float, float | None, float | None, int | None] | None:
    """(last, previous close, day volume, quote time ms) from a chart response's meta, or None. The previous close is
    the last daily close before the trading day of the latest price (the meta's own previous close can be two days
    old: Yahoo counts it from the start of the range asked for); the meta's, when there are no daily bars."""
    last = meta.get("regularMarketPrice")
    if not isinstance(last, (int, float)) or last != last:
        return None
    prev = _last_before(ts, closes, meta.get("gmtoffset"), meta.get("regularMarketTime"))
    if not isinstance(prev, (int, float)):
        prev = meta.get("previousClose")
    if not isinstance(prev, (int, float)):
        prev = meta.get("chartPreviousClose")
    vol = meta.get("regularMarketVolume")
    at = meta.get("regularMarketTime")
    return (float(last), float(prev) if isinstance(prev, (int, float)) else None,
            float(vol) if isinstance(vol, (int, float)) else None, int(at * 1000) if isinstance(at, (int, float)) else None)


def parse_spark(doc: dict) -> dict[str, tuple]:
    out = {}
    for r in ((doc or {}).get("spark") or {}).get("result") or []:
        for resp in r.get("response") or []:
            q = quote_of(resp.get("meta") or {}, *_closes(resp))
            if q:
                out[r.get("symbol") or (resp.get("meta") or {}).get("symbol")] = q
    return out


def parse_chart(doc: dict) -> tuple | None:
    res = ((doc or {}).get("chart") or {}).get("result") or []
    return quote_of(res[0].get("meta") or {}, *_closes(res[0])) if res else None


class YahooQuotes:
    name = "yahoo"

    def __init__(self, settings, transport=None):
        self.poll_s = max(5, int(getattr(settings, "yahoo_poll_s", 15)))
        self.extra = self._load_extra(getattr(settings, "config_dir", None))
        self.transport = transport   # tests
        self.stats = {"ok": 0, "errors": 0, "symbols": 0, "mode": "spark", "last_msg_ms": None, "last_error": ""}
        self.unknown: dict[str, float] = {}   # Yahoo ticker -> when to ask again (it had no price for it)
        self.host = 0
        self.missed = ""   # the last round's spark batches that failed while others came, for Connections
        self.throttled = False   # the last round met a 429 after a batch came: the next waits longer, as after a failure
        self.resume = 0          # where the next round's spark batches start: the batch the last 429 stopped at

    @staticmethod
    def _load_extra(config_dir) -> dict:
        if not config_dir:
            return {}
        path = config_dir / "yahoo.json"
        try:
            raw = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        except ValueError:
            log.warning("config/yahoo.json is not valid JSON; ignored")
            return {}
        return {k.upper(): v for k, v in (raw.get("symbols") or {}).items() if isinstance(v, (str, type(None)))}

    def watch_changed(self, market):
        self.unknown.clear()

    def wanted(self, market) -> dict[str, list[str]]:
        """Yahoo ticker -> the quote keys it serves, for keys no better source is quoting now."""
        out: dict[str, list[str]] = {}
        known = getattr(getattr(market, "symbols", None), "yahoo_for", lambda k: None)   # the symbol database's tickers
        for key in sorted(market.watch_keys):
            y = ticker_for(key, self.extra, known)
            if y and self.unknown.get(y, 0) <= time.monotonic() and market.quote_wanted(key, "yahoo"):
                out.setdefault(y, []).append(key)
        return out

    async def _get(self, http, path: str, params: dict) -> dict:
        for attempt in range(2):
            host = HOSTS[self.host]
            r = await http.get(host + path, params=params)
            if r.status_code == 429 or r.status_code >= 500:
                self.host = 1 - self.host
                if attempt:
                    r.raise_for_status()
                continue
            r.raise_for_status()
            return r.json()
        return {}

    async def poll(self, http, market) -> int:
        """One round; returns how many quotes it delivered."""
        want = self.wanted(market)
        self.stats["symbols"] = sum(len(v) for v in want.values())
        tickers, got, answered, failed = list(want), {}, set(), None
        self.throttled = False
        if self.stats["mode"] == "spark":
            at = self.resume % len(tickers) if tickers else 0   # so a throttled round's tail is asked first next
            tickers = tickers[at:] + tickers[:at]
            for i in range(0, len(tickers), BATCH):
                part = tickers[i:i + BATCH]
                try:
                    got.update(parse_spark(await self._get(http, "/v7/finance/spark",
                                                           {"symbols": ",".join(part), "range": "5d", "interval": "1d"})))
                    answered.update(part)
                except (httpx.HTTPError, ValueError) as e:
                    status = e.response.status_code if isinstance(e, httpx.HTTPStatusError) else None
                    if status in (401, 403, 404):
                        log.info("Yahoo spark refused (%s): asking one symbol at a time", status)
                        self.stats["mode"] = "chart"
                        break
                    failed = failed or e   # one slow, failing or garbled batch: the others still count
                    if status == 429:      # throttled: the rest waits for the next round, which starts with it
                        self.throttled, self.resume = True, at + i
                        break
        if self.stats["mode"] == "chart":
            sem = asyncio.Semaphore(4)

            async def one(t):
                async with sem:
                    try:
                        q = parse_chart(await self._get(http, f"/v8/finance/chart/{t}", {"range": "5d", "interval": "1d"}))
                    except httpx.HTTPStatusError as e:
                        if e.response.status_code == 404:
                            self.unknown[t] = time.monotonic() + UNKNOWN_RETRY_S
                        return
                    except (httpx.HTTPError, ValueError):   # one slow or garbled answer: the others still count
                        return
                    if q:
                        got[t] = q
            await asyncio.gather(*(one(t) for t in tickers))
        n = 0
        for t, keys in want.items():
            q = got.get(t)
            if q is None:
                if self.stats["mode"] == "spark" and t in answered:
                    self.unknown[t] = time.monotonic() + UNKNOWN_RETRY_S   # spark answers every symbol it knows
                continue
            for key in keys:
                market.on_quote(key, q[0], q[1], q[2], source="yahoo", at=q[3])
                n += 1
        if failed is not None and self.stats["mode"] == "spark":
            if not answered:
                raise failed   # nothing came: the round failed, and the next waits longer
            self.missed = f"{len(tickers) - len(answered)} of {len(tickers)} not read: {type(failed).__name__}: {failed}"[:200]
            log.warning("Yahoo quotes: %s", self.missed)
        else:
            self.missed = ""
        return n

    async def run(self, market):
        delay = self.poll_s
        async with httpx.AsyncClient(headers={"User-Agent": UA, "Accept": "application/json"}, timeout=15,
                                     follow_redirects=True, transport=self.transport) as http:
            while True:
                try:
                    n = await self.poll(http, market)
                    self.stats["ok"] += 1
                    self.stats["last_msg_ms"] = int(time.time() * 1000)
                    self.stats["last_error"] = self.missed
                    if self.throttled:   # what came is delivered; the rest waits longer, so Yahoo's limit can reset
                        delay = min(300, delay * 2)
                        log.warning("Yahoo throttled the quotes; next try in %ds", delay)
                    else:
                        delay = self.poll_s if self.stats["mode"] == "spark" else max(60, self.poll_s)
                    log.debug("Yahoo: %d quotes", n)
                except Exception as e:   # network, HTTP, JSON: keep the last quotes, back off
                    self.stats["errors"] += 1
                    self.stats["last_error"] = f"{type(e).__name__}: {e}"[:200]
                    delay = min(300, delay * 2)
                    log.warning("Yahoo quotes failed (%s); next try in %ds", type(e).__name__, delay)
                await stopping.pause(delay)   # not after a stop the request swallowed (stopping.py)


# ---- the previous close a quote came without -------------------------------------------------------------------------
FILL_BATCH = BATCH      # symbols a request
FILL_GATHER_S = 1       # the quotes that come together (a feed's first ones) are asked together
FILL_EVERY_S = 5        # a request this often at most
FILL_RETRY_S = (30, 300)   # after a failed request its symbols are asked again this long after, doubling to the most


class CloseFill:
    """The previous close of a stock, ETF or index quoted without one: IBKR sends no close tick for some tickers (their
    ticker's close stays NaN), and while it quotes them no other source does (Market.quote_wanted). Market.on_quote
    sets such a quote against the close known for its trading day (an earlier quote's, any source's); when none is,
    it notes the symbol here (want) and goes on. This asks Yahoo's daily bars for those (the close before the quote's
    trading day, as Yahoo's own quotes have it: close_before), up to FILL_BATCH symbols a request and a request every
    FILL_EVERY_S at most, in the background; once per symbol per trading day, again only after a failed request. The
    answer is a reference only (Market.close_hint): the price, its source and its time stay the quote's."""

    def __init__(self, market):
        self.m = market
        self.queue: dict[str, tuple[date, int]] = {}   # quote key -> (the trading day its close is wanted for, a quote's time then)
        self.asked: dict[str, date] = {}               # quote key -> the trading day it was last asked for
        self.wake = asyncio.Event()
        self.host = 0
        self.mode = "spark"   # "chart" once spark is refused: one symbol a request, as YahooQuotes does then
        self.stats = {"asked": 0, "filled": 0, "errors": 0, "last_error": ""}

    def want(self, key: str, day: date, ms: int):
        """A quote of `key` at `ms` has no previous close for its trading day `day`: asked for, once that day."""
        if self.asked.get(key) == day or clock_of(key) != "stock":
            return
        self.queue[key] = (day, ms)
        self.wake.set()

    async def run(self):
        retry = FILL_RETRY_S[0]
        while True:
            await self.wake.wait()
            await stopping.pause(FILL_GATHER_S)
            self.wake.clear()
            try:
                await self.ask()
                wait, retry = FILL_EVERY_S, FILL_RETRY_S[0]
            except Exception as e:   # network, HTTP, JSON: its symbols wait for the next request, which waits longer
                self.stats["errors"] += 1
                self.stats["last_error"] = f"{type(e).__name__}: {e}"[:200]
                log.warning("Previous closes not read from Yahoo (%s); asked again in %ds", type(e).__name__, retry)
                wait, retry = retry, min(FILL_RETRY_S[1], retry * 2)
            if self.queue:
                self.wake.set()
            await stopping.pause(wait)

    async def ask(self) -> int:
        """One request, for up to FILL_BATCH of the symbols waiting; returns how many got their close. A symbol whose
        close a quote brought meanwhile, or no longer quoted (off the lists), is not asked; one Yahoo has none for is
        not asked again that day. A failed request puts its symbols back and raises."""
        part = dict(list(self.queue.items())[:FILL_BATCH])
        tickers: dict[str, list[str]] = {}
        for key, (day, _) in part.items():
            del self.queue[key]
            self.asked[key] = day   # done for the day, whatever the answer (one with no Yahoo ticker too)
            known = self.m.closes.get(key)
            if not (known and known[0] >= day) and key in self.m.quotes and (t := self.m.ext.yahoo_ticker(key)):
                tickers.setdefault(t, []).append(key)
        if not tickers:
            return 0
        self.stats["asked"] += sum(len(k) for k in tickers.values())
        try:
            got = await self._fetch(sorted(tickers))
        except Exception:
            for keys in tickers.values():
                for key in keys:
                    self.asked.pop(key, None)
                    self.queue.setdefault(key, part[key])
            raise
        n = 0
        for t, keys in tickers.items():
            for key in keys:
                day, ms = part[key]
                prev = close_before(got.get(t), ms)
                if prev is not None and self.m.close_hint(key, day, prev):
                    n += 1
        self.stats["filled"] += n
        return n

    async def _fetch(self, tickers: list[str]) -> dict[str, dict]:
        """Yahoo ticker -> its response (meta, daily bar times and closes) over the last five days."""
        http = self.m.http
        if http is None:
            raise ValueError("no network")
        params = {"range": "5d", "interval": "1d"}
        if self.mode == "spark":
            try:
                doc = await self._get(http, "/v7/finance/spark", {"symbols": ",".join(tickers), **params})
            except httpx.HTTPStatusError as e:
                if e.response.status_code not in (401, 403, 404):
                    raise
                log.info("Yahoo spark refused (%s): previous closes one symbol at a time", e.response.status_code)
                self.mode = "chart"
            else:
                return {r.get("symbol"): (r.get("response") or [{}])[0] or {}
                        for r in ((doc or {}).get("spark") or {}).get("result") or [] if isinstance(r, dict)}
        out = {}
        for t in tickers:
            try:
                doc = await self._get(http, f"/v8/finance/chart/{t}", params)
            except httpx.HTTPStatusError as e:
                if e.response.status_code == 404:   # Yahoo has no such ticker: the others still count
                    continue
                raise
            if res := ((doc or {}).get("chart") or {}).get("result") or []:
                out[t] = res[0] or {}
        return out

    async def _get(self, http, path: str, params: dict) -> dict:
        """As YahooQuotes._get: the other host after a 429 or a server error."""
        for attempt in range(2):
            r = await http.get(HOSTS[self.host] + path, params=params, timeout=15, follow_redirects=True,
                               headers={"User-Agent": UA, "Accept": "application/json"})
            if r.status_code == 429 or r.status_code >= 500:
                self.host = 1 - self.host
                if attempt:
                    r.raise_for_status()
                continue
            r.raise_for_status()
            return r.json()
        return {}
