"""Watchlist quotes from Yahoo Finance, the backup source: stocks, ETFs, indices, FX, crypto, and futures when neither
NinjaTrader nor IBKR is quoting them. Yahoo's quotes can lag (futures about 10 minutes, some exchanges 15-20), so a
symbol that NinjaTrader or IBKR quoted in the last two minutes keeps their quote (Market.on_quote).

It asks Yahoo's public chart endpoints, no account or key: the spark endpoint for 20 symbols a request, and, if that
is refused, the chart endpoint one symbol at a time, more slowly. TradingView symbols become Yahoo's by yahoo_symbol()
(ES1! -> ES=F, SPX -> ^GSPC, EURUSD -> EURUSD=X, BTCUSD -> BTC-USD, ZS -> ZS: the stock, never ZS1!'s ZS=F);
config/yahoo.json adds or replaces any."""
import asyncio
import json
import logging
import re
import time

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
    m = re.match(r"^([A-Z]{2,5})(USDT?|USDC|EUR)$", key)
    if m and m.group(1) in CRYPTO:
        return f"{m.group(1)}-{'USD' if m.group(2).startswith('USD') else m.group(2)}"
    if STOCK_RE.match(key):
        return key.replace(".", "-")   # BRK.B -> BRK-B
    return None


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


def quote_of(meta: dict, ts: list | None = None, closes: list | None = None) -> tuple[float, float | None, float | None, int | None] | None:
    """(last, previous close, day volume, quote time ms) from a chart response's meta, or None. The previous close is
    the last daily close before the trading day of the latest price (the meta's own previous close can be two days
    old: Yahoo counts it from the start of the range asked for); the meta's, when there are no daily bars."""
    last = meta.get("regularMarketPrice")
    if not isinstance(last, (int, float)) or last != last:
        return None
    prev = None
    at, off = meta.get("regularMarketTime"), meta.get("gmtoffset") or 0
    if isinstance(at, (int, float)) and ts and closes:
        day = lambda t: (int(t) + int(off)) // 86_400   # noqa: E731  (the exchange's calendar day)
        before = [c for t, c in zip(ts, closes) if isinstance(t, (int, float)) and isinstance(c, (int, float))
                  and c == c and day(t) < day(at)]
        prev = before[-1] if before else None
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
