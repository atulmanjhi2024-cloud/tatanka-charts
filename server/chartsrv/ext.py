"""Charts for symbols the app has no trades for: stocks, ETFs, indices, currency pairs, crypto and futures it doesn't
chart live. Best source first: IBKR (TWS or IB Gateway connected, with the symbol's market data subscription), Alpaca
(US stocks and ETFs, intraday, its keys set), then Yahoo Finance (delayed, free, no account); FRED and EIA series
from their own. Any minute or hour timeframe up to a day, and N days, weeks or months, are built from the source's
nearest shorter bars (3m from 1m, 2h from 1h, 3M from 1M). Daily, weekly and monthly bars are stamped as the app's
own (the 17:00 CT open before the day, its Monday or its 1st), so the chart labels them alike. Answers are kept for a
short while, so a chart polling for its last bar costs a source little.

Each answer names its exchange's clock: {tz, rth ["HH:MM", "HH:MM"] or None, day ("HH:MM": when its trading day
starts)}. US stocks: New York, 09:30-16:00, the day from midnight; crypto: UTC around the clock; currencies: the
17:00 New York roll; futures: CME's 17:00 Chicago open. ETH (rth=False) adds a stock's pre- and post-market."""
import logging
import math
import re
import time
from datetime import date, datetime, time as dtime, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx

from . import watchlists as wl
from .bars import calendar_bucket, parse_spec
from .feeds import alpaca, ibkr
from .feeds.fred import source_of
from .feeds.yahoo import UA, ticker_for
from .sessions import day_open, to_ct

log = logging.getLogger(__name__)
YAHOO_CHART = "https://query1.finance.yahoo.com/v8/finance/chart/"
# spec -> (Yahoo interval, range, seconds an answer is kept)
YAHOO_SPECS = {"1m": ("1m", "7d", 20), "2m": ("2m", "60d", 20), "5m": ("5m", "60d", 30), "15m": ("15m", "60d", 60),
               "30m": ("30m", "60d", 60), "1h": ("60m", "730d", 120), "1D": ("1d", "20y", 300), "1W": ("1wk", "max", 1800),
               "1M": ("1mo", "max", 3600)}
YAHOO_BASES = ((60, "1h"), (30, "30m"), (15, "15m"), (5, "5m"), (2, "2m"), (1, "1m"))   # longest history first
MAX_MINUTES = 24 * 60   # the longest intraday bar
FRED_SPECS = ("1D", "1W", "1M")
ALPACA_BARS = 4000   # about this many bars of history on an Alpaca chart
CALENDAR = ("1D", "1W", "1M")
US_STOCK = {"tz": "America/New_York", "rth": ["09:30", "16:00"], "day": "00:00"}
CME = {"tz": "America/Chicago", "rth": None, "day": "17:00"}
NO_EXCHANGE = {"tz": "America/New_York", "rth": None, "day": "00:00"}   # FRED, EIA: dates only
# IBKR, by contract type: its exchange's clock and its minutes of trading in a day (how far back to ask)
IB_CLOCK = {"STK": US_STOCK, "IND": US_STOCK, "CASH": {"tz": "America/New_York", "rth": None, "day": "17:00"},
            "CRYPTO": {"tz": "UTC", "rth": None, "day": "00:00"}, "CONTFUT": CME}
IB_DAY_MINUTES = {"STK": 390, "IND": 390, "CASH": 1440, "CRYPTO": 1440, "CONTFUT": 1380}
IB_SPAN = {"D": "10 Y", "W": "20 Y", "M": "20 Y"}           # daily and longer bars: this far back
IB_REFRESH = {"D": "1 M", "W": "3 M", "M": "1 Y"}           # a poll asks only for this much and joins it on
IB_KEEP_S = (10, 60)        # an IBKR answer is kept this long (intraday, daily and longer)
IB_FULL_S = 30 * 60         # and its history asked again in full this often; polls in between ask for the latest only
IB_NONE_S = 10 * 60         # IBKR had no bars for a chart (no subscription, no contract): the next source this long
MAX_BARS = 10_000
CACHE_MAX = 300
STALE_S = 10 * 60           # an expired answer is kept this much longer: what a failed request falls back on


def _day_key(d: date, spec: str) -> date:
    return d - timedelta(days=d.weekday()) if spec == "1W" else d.replace(day=1) if spec == "1M" else d


def _add(out: list[dict], t: int, o: float, h: float, lo: float, c: float, v: int):
    if out and out[-1]["t"] == t:   # the same day again (Yahoo's live row), or a week/month made of days
        b = out[-1]
        b["h"], b["l"], b["c"], b["v"] = max(b["h"], h), min(b["l"], lo), c, b["v"] + v
    elif not out or t > out[-1]["t"]:
        out.append({"t": t, "o": o, "h": h, "l": lo, "c": c, "v": v})


def parse_yahoo_chart(doc: dict, spec: str) -> tuple[list[dict], dict]:
    """Yahoo's v8 chart answer: bars oldest first (incomplete rows left out) and its meta."""
    res = (((doc or {}).get("chart") or {}).get("result") or [None])[0]
    if not res:
        err = ((doc or {}).get("chart") or {}).get("error") or {}
        raise ValueError(f"Yahoo has no data: {err.get('description') or err.get('code') or 'empty'}")
    meta = res.get("meta") or {}
    q = (((res.get("indicators") or {}).get("quote")) or [{}])[0]
    try:
        tz = ZoneInfo(meta.get("exchangeTimezoneName") or "America/New_York")
    except (KeyError, ValueError):
        tz = timezone(timedelta(seconds=int(meta.get("gmtoffset") or 0)))
    col = lambda k, i: (q.get(k) or [])[i] if i < len(q.get(k) or []) else None   # noqa: E731
    out: list[dict] = []
    for i, ts in enumerate(res.get("timestamp") or []):
        c = col("close", i)
        if not isinstance(c, (int, float)) or not isinstance(ts, (int, float)):
            continue
        o, h, lo = (col(k, i) for k in ("open", "high", "low"))
        o = o if isinstance(o, (int, float)) else c
        h = max(x for x in (h, o, c) if isinstance(x, (int, float)))
        lo = min(x for x in (lo, o, c) if isinstance(x, (int, float)))
        v = col("volume", i)
        t = day_open(_day_key(datetime.fromtimestamp(ts, tz).date(), spec)) if spec in CALENDAR else int(ts * 1000)
        _add(out, t, float(o), float(h), float(lo), float(c), int(v) if isinstance(v, (int, float)) else 0)
    return out[-MAX_BARS:], meta


def exchange_of(meta: dict) -> dict:
    """The exchange's clock from Yahoo's chart meta: its zone, regular hours (when it has a close) and day start."""
    kind = str(meta.get("instrumentType") or "").upper()
    if kind == "FUTURE":
        return dict(CME)
    if kind == "CURRENCY":
        return {"tz": "America/New_York", "rth": None, "day": "17:00"}
    name = str(meta.get("exchangeTimezoneName") or "America/New_York")
    try:
        tz = ZoneInfo(name)
    except (KeyError, ValueError):
        return {"tz": "UTC", "rth": None, "day": "00:00"}
    reg = (meta.get("currentTradingPeriod") or {}).get("regular") or {}
    a, b, rth = reg.get("start"), reg.get("end"), None
    if isinstance(a, (int, float)) and isinstance(b, (int, float)) and 0 < b - a < 23 * 3600:   # around the clock: none
        rth = [datetime.fromtimestamp(x, tz).strftime("%H:%M") for x in (a, b)]
    return {"tz": name, "rth": rth, "day": "00:00"}


def fred_bars(obs: list[tuple[str, float]], spec: str) -> list[dict]:
    out: list[dict] = []
    for d, v in obs:
        _add(out, day_open(_day_key(date.fromisoformat(d), spec)), v, v, v, v, 0)
    return out[-MAX_BARS:]


def split_spec(spec: str) -> tuple[int, str]:
    """(minutes, "") for a minute or hour timeframe up to a day, (N, "D", "W" or "M") for N days, weeks or months;
    ValueError for what these charts don't take (ticks, seconds, over 24h)."""
    m = re.fullmatch(r"(\d+)([mhDWM])", spec)
    if not m:
        raise ValueError("these charts come in time bars: minutes, hours, days, weeks or months")
    parse_spec(spec)   # the app's own limits: 365D, 52W, 12M
    n, unit = int(m[1]), m[2]
    if unit in "DWM":
        return n, unit
    n *= 60 if unit == "h" else 1
    if not 1 <= n <= MAX_MINUTES:
        raise ValueError("intraday bars run up to 24h")
    return n, ""


def yahoo_base(spec: str, eth: bool = False, stock: bool = True) -> str:
    """The Yahoo timeframe a chart is built from: its own, or the one with the most history that divides it (3m from
    1m, 10m from 5m, 2h from 1h, 2D from 1D, 3M from 1M). A US stock's hourly bars with the pre- and post-market (eth)
    don't line up with its 9:30 open, so those come from 30 minutes; what has no pre-market (crypto, currencies,
    indices, futures, stocks listed elsewhere) keeps its 60-minute bars and the longer history."""
    if spec in YAHOO_SPECS and not (eth and stock and spec == "1h"):
        return spec
    n, unit = split_spec(spec)
    if unit:
        return f"1{unit}"
    return next(s for b, s in YAHOO_BASES if n % b == 0 and not (eth and stock and b == 60))


def anchor_of(exch: dict, rth: bool = True) -> str:
    """Where a day's intraday bars count from: its regular open, as TradingView (a New York stock with the pre- and
    post-market: 4:00), or where its trading day starts (17:00 New York for currencies, 00:00 UTC for crypto)."""
    if exch.get("rth"):
        return "04:00" if not rth and exch.get("tz") == "America/New_York" else exch["rth"][0]
    return exch.get("day") or "00:00"


def regroup(bars: list[dict], spec: str, exch: dict, rth: bool = True) -> list[dict]:
    """A chart's bars from shorter ones (oldest first, stamped as these charts stamp them): minutes and hours counted
    from the day's anchor in the exchange's zone (anchor_of), so the last bar of a day may be short; N days, weeks and
    months grouped from fixed starts, as the live charts group them (crypto's N days are calendar days)."""
    n, unit = split_spec(spec)
    if unit:
        if n == 1:
            return bars
        if unit == "D" and exch.get("tz") == "UTC" and not exch.get("rth"):   # crypto's clock: it trades every day
            def bucket(t: int) -> int:
                d = (to_ct(t) + timedelta(hours=7)).date().toordinal()   # the trade date of a 17:00 CT open
                return day_open(date.fromordinal(d - (d - 1) % n))
        else:
            bucket = lambda t: calendar_bucket(t, unit, n)   # noqa: E731
    else:
        tz, period = ZoneInfo(exch["tz"]), n * 60_000
        at = dtime.fromisoformat(anchor_of(exch, rth))
        days: dict[date, int] = {}

        def bucket(t: int) -> int:
            local = datetime.fromtimestamp(t / 1000, tz)
            day = local.date() if local.time() >= at else local.date() - timedelta(days=1)
            if day not in days:
                days[day] = int(datetime.combine(day, at, tz).timestamp() * 1000)
            a = days[day]
            return a + (t - a) // period * period
    out: list[dict] = []
    for b in bars:
        _add(out, bucket(b["t"]), b["o"], b["h"], b["l"], b["c"], b["v"])
    return out


def _decimals(tick: float) -> int | None:
    """The decimals a minimum tick needs (0.01: 2, 0.00005: 5, 0.25: 2)."""
    if not tick or tick <= 0:
        return None
    return len(f"{tick:.10f}".rstrip("0").split(".")[1])


def chart_symbol(sym: str, instruments: dict, only=wl.ONLY_FUTURES) -> str:
    """The symbol as this chart takes it, or ValueError: one plain symbol (no formula), not a live chart's. only: the
    roots a bare one is written as the future of (6E: 6E1!), the symbol database's only_futures."""
    s = wl._clean_item(sym, only)
    toks = wl.symbols_in(s)
    if s.startswith(wl.SECTION) or len(toks) != 1 or any(ch in wl.bare(s) for ch in "+-*/()"):
        raise ValueError("one symbol, not a formula")
    if (wl.future_root(toks[0]) or toks[0]) in instruments and ":" not in toks[0]:   # ES1!, or ES alone
        raise ValueError(f"{toks[0]} has a live chart")
    return toks[0]


class ExtBars:
    def __init__(self, market):
        self.m = market
        self.cache: dict[tuple[str, str], tuple[float, dict]] = {}
        self.alpaca = alpaca.AlpacaBars(market.http)

    def _keep(self, key: tuple[str, str], until: float, answer: dict):
        """Keeps an answer until `until` (time.monotonic()), and STALE_S more as the stale one; answers older than that
        go, and the one expiring soonest when CACHE_MAX are held."""
        old = time.monotonic() - STALE_S
        for k in [k for k, (t, _) in self.cache.items() if t < old]:
            del self.cache[k]
        if len(self.cache) >= CACHE_MAX and key not in self.cache:
            self.cache.pop(min(self.cache, key=lambda k: self.cache[k][0]))
        self.cache[key] = (until, answer)

    def yahoo_ticker(self, sym: str) -> str | None:
        """Yahoo's ticker for a chart symbol, as its quote has it: ZS1! soybeans' ZS=F, ZS Zscaler's ZS."""
        extra = getattr(next((f for f in self.m.feed_objs if f.name == "yahoo"), None), "extra", {}) or {}
        return ticker_for(wl.quote_key(sym), extra, self.m.symbols.yahoo_for)

    async def bars(self, sym: str, spec: str, rth: bool = True) -> dict:
        """{symbol, spec, source, feed, name, type, decimals, tick, specs, custom (any minute or hour timeframe up to
        a day, N days, weeks or months), exchange (its clock), bars}; ValueError when there are none. rth=False:
        intraday bars with the pre- and post-market where the source has them."""
        info = self.m.symbols.info(sym) or {}
        series = source_of(self.m, sym)
        feed = ""
        if series:
            data, sid, src = series
            if spec not in FRED_SPECS:
                raise ValueError(f"{data.name} series are daily or longer: {', '.join(FRED_SPECS)}")
            obs, dec = await data.series(sid)
            name = info.get("name") or (data.feed.title(sid) if hasattr(data, "feed") else sid)
            bars, specs, exch = fred_bars(obs, spec), FRED_SPECS, NO_EXCHANGE
        else:
            split_spec(spec)
            watch = getattr(self.m, "watch_chart", None)
            if watch:
                watch(wl.quote_key(sym))   # quoted while charted: the live quote moves the last bar
            if (got := await self._ibkr(sym, spec, rth)) is not None:
                bars, feed, dec, name, exch = got
                src, specs, name = "ibkr", tuple(YAHOO_SPECS), info.get("name") or name or sym
            elif (got := await self._alpaca(sym, spec, rth)) is not None:
                bars, feed = got
                last = bars[-1]["c"] if bars else 1
                dec, src, specs, name, exch = (2 if abs(last) >= 1 else 4), "alpaca", tuple(YAHOO_SPECS), info.get("name") or sym, US_STOCK
            else:
                y = self.yahoo_ticker(sym)
                if not y:
                    raise ValueError(f"no free data for {sym}")
                # only a US stock has a pre- and post-market (Yahoo's tickers elsewhere end in their exchange: 7203.T)
                stock = info.get("type") in alpaca.STOCK_TYPES and "." not in y
                base = yahoo_base(spec, not rth, stock)
                bars, meta = await self._yahoo(y, base, not rth and base not in CALENDAR)
                exch = exchange_of(meta)
                if base != spec:
                    bars = regroup(bars, spec, exch, rth or not stock)
                dec = meta.get("priceHint") if isinstance(meta.get("priceHint"), int) else None
                if dec is None:
                    last = bars[-1]["c"] if bars else 1
                    dec = 2 if abs(last) >= 1 else 4
                src, specs, name = "yahoo", tuple(YAHOO_SPECS), info.get("name") or meta.get("longName") or meta.get("shortName") or sym
        if not bars:
            raise ValueError(f"no data for {sym}")
        for i, b in enumerate(bars):
            b["seq"] = i + 1
        dec = max(0, min(8, int(dec)))
        return {"symbol": sym, "spec": spec, "source": src, "name": name, "type": info.get("type", ""), "decimals": dec,
                "tick": round(10 ** -dec, dec), "specs": list(specs), "bars": bars, "feed": feed, "exchange": dict(exch),
                "custom": not series}

    async def _ibkr(self, sym: str, spec: str, rth: bool = True) -> tuple | None:
        """(bars, feed "live" or "delayed", decimals, name, exchange clock) from IBKR when it is connected and has the
        symbol's bars; None: the next source. A future only as XX1! (BZ1!), a stock never as one (BZ is a stock too),
        as contract_for() reads the quote key. Polls ask only for the newest bars and join them on; the full history
        is asked again every IB_FULL_S."""
        feed = next((f for f in self.m.feed_objs if f.name == "ibkr" and hasattr(f, "history")), None)
        key = wl.quote_key(sym)
        cf = ibkr.contract_for(key)
        if feed is None or cf is None or not feed.connected():
            return None
        kind, (n, unit) = cf["secType"], split_spec(spec)
        exch = IB_CLOCK.get(kind, US_STOCK)
        eth = not rth and kind == "STK" and not unit   # daily and longer: regular hours, as TradingView
        if unit:
            base, size, span = f"1{unit}", ibkr.CAL_SIZES[unit], IB_SPAN[unit]
        else:
            at = dtime.fromisoformat(anchor_of(exch, not eth))   # an index has no pre-market: from its 9:30 open
            b = ibkr.base_minutes(n, at.hour * 60 + at.minute)
            base, size = f"{b}m", ibkr.BAR_SIZES[b]
            span = ibkr.duration(n, b, 960 if eth else IB_DAY_MINUTES.get(kind, 390), kind != "CRYPTO")
        ck = (f"ibkr:{key}", base + ("+eth" if eth else ""))
        now, hit = time.monotonic(), self.cache.get(ck)
        if hit and hit[1].get("none"):
            if hit[0] > now:
                return None
            hit = None
        got = hit[1] if hit else None
        if got is None or hit[0] <= now:
            fresh = got is not None and now - got["full"] < IB_FULL_S
            if fresh:   # the newest bars only, joined on
                since = time.time() - got["bars"][-1]["t"] / 1000
                span = IB_REFRESH[unit] if unit else f"{int(min(86_400, max(1800, since + 2 * b * 60)))} S"
            res = await feed.history(key, size, span, not eth)
            rows = self._ib_bars(res, unit) if res else []
            if fresh:
                if rows:
                    got = {**got, "bars": [x for x in got["bars"] if x["t"] < rows[0]["t"]] + rows}
            elif rows:
                last = rows[-1]["c"]
                got = {"bars": rows, "full": now, "dec": _decimals(res["tick"]) or (2 if abs(last) >= 1 else 4),
                       "name": res["name"]}
            elif got is None:   # no subscription, no contract: the next source for a while
                self._keep(ck, now + IB_NONE_S, {"none": True})
                log.info("no IBKR bars for %s %s; the next source for %d min", sym, base, IB_NONE_S // 60)
                return None
            else:   # the full history didn't come again: the bars it has, and the newest only for a while
                got = {**got, "full": now}
            got["bars"] = got["bars"][-MAX_BARS:]
            self._keep(ck, now + IB_KEEP_S[bool(unit)], got)
        bars = [dict(x) for x in got["bars"]]
        if base != spec:
            bars = regroup(bars, spec, exch, not eth)
        return bars, "live" if self.m.s.ibkr_market_data_type == 1 else "delayed", got["dec"], got["name"], exch

    @staticmethod
    def _ib_bars(res: dict, unit: str) -> list[dict]:
        """IBKR's rows as these charts' bars: intraday at their start, daily and longer as the app stamps them."""
        out: list[dict] = []
        for d, o, h, lo, c, v in res["rows"]:   # oldest first
            if not all(isinstance(x, (int, float)) and x == x for x in (o, h, lo, c)) or min(o, h, lo, c) <= 0:
                continue
            if isinstance(d, datetime):
                t = int((d if d.tzinfo else d.replace(tzinfo=timezone.utc)).timestamp() * 1000)
            elif isinstance(d, date) and unit:
                t = day_open(_day_key(d, f"1{unit}"))
            else:
                continue
            _add(out, t, float(o), float(max(h, o, c)), float(min(lo, o, c)), float(c),
                 int(v) if isinstance(v, (int, float)) and v == v and v > 0 else 0)
        return out

    def _alpaca_can(self, sym: str) -> bool:
        s = self.m.s
        return bool(s.alpaca_key_id and s.alpaca_secret and self.m.http is not None
                    and alpaca.is_stock(self.m, wl.quote_key(sym)))

    async def _alpaca(self, sym: str, spec: str, rth: bool = True) -> tuple[list[dict], str] | None:
        """Real-time intraday bars of a US stock or ETF from Alpaca, when its keys are set; None: use Yahoo."""
        s = self.m.s
        minutes = alpaca.spec_minutes(spec)
        if minutes is None or not self._alpaca_can(sym):
            return None
        days = min(730, math.ceil(ALPACA_BARS * minutes / 390 * 7 / 5) + 4)
        key = (f"alpaca:{sym}", spec if rth else f"{spec}+eth")
        hit = self.cache.get(key)
        if hit and hit[0] > time.monotonic():
            return [dict(b) for b in hit[1]["bars"]], hit[1]["feed"]
        try:
            bars, feed = await self.alpaca.bars(s, wl.quote_key(sym), minutes, days, extended=not rth)
        except (httpx.HTTPError, ValueError, PermissionError) as e:   # Yahoo then; never the request (headers hold keys)
            log.info("Alpaca bars for %s not read (%s); Yahoo instead", sym, type(e).__name__)
            return None
        if not bars:
            return None
        bars = bars[-MAX_BARS:]
        self._keep(key, time.monotonic() + 5, {"bars": bars, "feed": feed})
        return [dict(b) for b in bars], feed

    async def _yahoo(self, ticker: str, spec: str, prepost: bool = False) -> tuple[list[dict], dict]:
        key = (ticker, spec if not prepost else f"{spec}+eth")
        hit = self.cache.get(key)
        if hit and hit[0] > time.monotonic():
            return [dict(b) for b in hit[1]["bars"]], hit[1]["meta"]
        interval, rng, keep = YAHOO_SPECS[spec]
        if self.m.http is None:
            raise ValueError("no network")
        try:
            r = await self.m.http.get(YAHOO_CHART + ticker, params={"interval": interval, "range": rng, "includePrePost": "true" if prepost else "false"},
                                      headers={"User-Agent": UA, "Accept": "application/json"}, timeout=20, follow_redirects=True)
            if r.status_code == 404:
                raise ValueError(f"Yahoo has no {ticker}")
            r.raise_for_status()
            bars, meta = parse_yahoo_chart(r.json(), spec)
        except httpx.HTTPError as e:
            if hit:   # the last answer, stale, beats none
                return [dict(b) for b in hit[1]["bars"]], hit[1]["meta"]
            raise ValueError(f"Yahoo did not answer ({type(e).__name__})") from None
        self._keep(key, time.monotonic() + keep, {"bars": bars, "meta": meta})
        return [dict(b) for b in bars], meta
