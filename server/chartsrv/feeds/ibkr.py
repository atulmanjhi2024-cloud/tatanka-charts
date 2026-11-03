"""Watchlist quotes and chart history from TWS or IB Gateway via ib_async. Market data only: no orders, no account calls.

Subscribes whatever the watchlists and open free-data charts hold (market.watch_keys), turned into IBKR contracts by
contract_for(); symbols it cannot place (TradingView-only tickers like DXY or US02Y) are skipped. When that set changes
it subscribes the new symbols and drops the gone ones on the same connection. history() gives a chart's bars
(ext.py), which needs the symbol's market data subscription at IBKR. Not yet run against a live Gateway; first
connection needs checking (docs/WATCHLIST.md, "IBKR")."""
import asyncio
import logging
import math
import re
import time

from .. import watchlists as wl

log = logging.getLogger(__name__)

# Futures roots by exchange (TradingView's "ES1!" arrives here as that, its quote key).
FUT_EXCHANGE = {
    **dict.fromkeys(["ES", "NQ", "RTY", "MES", "MNQ", "M2K", "LE", "HE", "GF", "SR3", "6E", "6J", "6B", "6A", "6C"], "CME"),
    **dict.fromkeys(["YM", "MYM", "ZB", "ZN", "ZF", "ZT", "UB", "TN", "ZC", "ZS", "ZW", "ZL", "ZM"], "CBOT"),
    **dict.fromkeys(["CL", "MCL", "NG", "RB", "HO", "PL", "PA"], "NYMEX"),
    **dict.fromkeys(["GC", "MGC", "SI", "SIL", "HG"], "COMEX"),
}
INDEX = {"SPX": "CBOE", "VIX": "CBOE", "TNX": "CBOE", "SKEW": "CBOE", "VVIX": "CBOE", "RUT": "RUSSELL", "NDX": "NASDAQ",
         "FVX": "CBOE", "TYX": "CBOE", "IRX": "CBOE"}
# Treasury yields by TradingView's names, from Cboe's yield indexes (Cboe Streaming Market Indexes at IBKR); US02Y has none
YIELDS = {"US05Y": "FVX", "US10Y": "TNX", "US30Y": "TYX"}
# Cboe's yield indexes are ten times the yield (its rules for the options on them: the yield "multiplied by ten"; TNX
# 42.5 for a 4.25% 10-year yield), and IBKR sends them as Cboe does ("TNX-IND-CBOE ... x10"); Yahoo's ^TNX and
# TradingView's US10Y are in percent.
YIELD_INDEXES = {"IRX", "FVX", "TNX", "TYX"}
YIELD_SCALE = 10
CURRENCIES = {"USD", "EUR", "GBP", "JPY", "CHF", "CAD", "AUD", "NZD", "CNH", "HKD", "SEK", "NOK", "MXN", "SGD"}
CRYPTO = {"BTC", "ETH", "LTC", "BCH"}
NOT_STOCKS = {"DXY", "HSI", "KOSPI", "NIFTY", "MOVE", "BRENT", "DJI", "SX5E", "NI225", "PALLADIUM"}
STOCK_RE = re.compile(r"^[A-Z]{1,5}(\.[A-Z])?$")


# CME currency futures: IBKR names them by the currency, with the CME code as the trading class.
FX_FUT = {"6E": "EUR", "6J": "JPY", "6B": "GBP", "6A": "AUD", "6C": "CAD"}


def contract_for(key: str) -> dict | None:
    """IBKR contract fields for a watchlist quote key, or None. A future only as XX1! (ZS1!, soybeans); a bare key
    never is one (ZS is Zscaler's stock)."""
    if root := wl.future_root(key):
        if root in FX_FUT:
            return {"secType": "CONTFUT", "symbol": FX_FUT[root], "tradingClass": root, "exchange": FUT_EXCHANGE[root]}
        if root in FUT_EXCHANGE:
            return {"secType": "CONTFUT", "symbol": root, "exchange": FUT_EXCHANGE[root]}
        return None   # a future IBKR is not set up for here: never the stock with its root's ticker (BZ1!'s BZ)
    key = YIELDS.get(key, key)
    if key in INDEX:
        return {"secType": "IND", "symbol": key, "exchange": INDEX[key]}
    if len(key) == 6 and key[:3] in CURRENCIES and key[3:] in CURRENCIES:
        return {"secType": "CASH", "symbol": key[:3], "currency": key[3:], "exchange": "IDEALPRO"}
    if len(key) == 6 and key[:3] in CRYPTO and key[3:] == "USD":
        return {"secType": "CRYPTO", "symbol": key[:3], "currency": "USD", "exchange": "PAXOS"}
    if STOCK_RE.match(key) and key not in NOT_STOCKS:
        return {"secType": "STK", "symbol": key.replace(".", " "), "exchange": "SMART", "currency": "USD"}
    return None


# Chart history (ext.py): the bar sizes IBKR has, by minutes; what the bars are of (TRADES unless here).
BAR_SIZES = {1: "1 min", 2: "2 mins", 3: "3 mins", 5: "5 mins", 10: "10 mins", 15: "15 mins", 20: "20 mins",
             30: "30 mins", 60: "1 hour"}
CAL_SIZES = {"D": "1 day", "W": "1 week", "M": "1 month"}
SHOW = {"CASH": "MIDPOINT", "CRYPTO": "AGGTRADES"}
HISTORY_TIMEOUT_S = 25
# TWS's answers to a market data request that ends it: the account's lines all taken (101), no such contract (200),
# no market data subscription for it (354, 10089, 10168)
REFUSED = {101, 200, 354, 10089, 10168}


def base_minutes(minutes: int, anchor: int) -> int:
    """The IBKR bar size a chart's bars are built from: the largest that divides both the chart's minutes and the
    minute of the day they count from (a stock's 9:30 open: 30-minute bars, as IBKR's hourly ones start on the hour)."""
    return max(b for b in BAR_SIZES if minutes % b == 0 and anchor % b == 0)


def duration(minutes: int, base: int, day_minutes: int, weekdays: bool = True) -> str:
    """How far back to ask: about 3000 of the chart's bars, at most about 8000 of IBKR's."""
    days = math.ceil(min(3000 * minutes, 8000 * base) / day_minutes * (7 / 5 if weekdays else 1))
    if days <= 30:
        return f"{max(1, days)} D"
    if days <= 364:
        return f"{math.ceil(days / 7)} W"
    return f"{min(20, days // 365)} Y"


def per_cent(key: str, v):
    """A Cboe yield index's value from IBKR (a quote, its previous close, a bar's price, its tick) in percent, as Yahoo
    and TradingView show it: always a tenth (YIELD_SCALE), whatever the yield, so a 0.55% of 2020 (5.5) and a bar
    across 2% are in the same scale as today's 4.25% (42.5)."""
    if v is not None and (contract_for(key) or {}).get("symbol") in YIELD_INDEXES:
        return v / YIELD_SCALE
    return v


def _num(x):
    return None if x is None or (isinstance(x, float) and math.isnan(x)) else x


class IBKRQuotes:
    name = "ibkr"

    def __init__(self, settings):
        self.s = settings
        self.changed = asyncio.Event()
        self.stats = {"connected": False, "symbols": 0, "last_msg_ms": None, "last_error": ""}
        self.ib = None   # the connection while it is up; chart history asks on it too
        self.contracts: dict[str, object] = {}   # quote key -> the contract its quotes come from (None: IBKR has none)
        self.details: dict[str, object] = {}     # quote key -> its contract details, for history (None: none)

    def watch_changed(self, market):
        self.changed.set()

    def connected(self) -> bool:
        return self.ib is not None and self.ib.isConnected()

    async def run(self, market):
        from ib_async import IB, StartupFetch  # optional dependency, only for this feed

        while True:
            ib = IB()
            gone = asyncio.Event()
            ib.disconnectedEvent += lambda: gone.set()
            self.changed.clear()
            try:
                await ib.connectAsync(self.s.ibkr_host, self.s.ibkr_port, clientId=self.s.ibkr_client_id,
                                      timeout=15, readonly=True, fetchFields=StartupFetch(0))
                ib.reqMarketDataType(self.s.ibkr_market_data_type)
                self.ib, self.contracts, self.details = ib, {}, {}
                subs: dict[str, object] = {}   # quote key -> its ticker
                by_ticker: dict[int, set[str]] = {}   # a ticker -> its quote keys (US10Y and TNX: Cboe's TNX)

                def on_pending(tickers):
                    for t in tickers:
                        for sym in by_ticker.get(id(t), ()):
                            last = _num(t.last) or _num(t.marketPrice())
                            market.on_quote(sym, per_cent(sym, last), per_cent(sym, _num(t.close)), _num(t.volume),
                                            source="ibkr")
                    self.stats["last_msg_ms"] = int(time.time() * 1000)

                def on_error(req_id, code, msg, contract):
                    """A quote TWS refused is not counted, and is asked again at the next sync (lines freed when
                    the lists or charts change)."""
                    t = ib.wrapper.reqId2Ticker.get(req_id) if code in REFUSED else None
                    keys = by_ticker.pop(id(t), set()) if t is not None else set()
                    for key in keys:
                        subs.pop(key, None)
                    if keys:
                        log.info("IBKR refused quotes for %s (%s: %s)", ", ".join(sorted(keys)), code, msg)
                        self.stats["symbols"] = len(subs)

                ib.pendingTickersEvent += on_pending
                ib.errorEvent += on_error
                await self._sync(ib, market, subs, by_ticker)
                log.info("IBKR connected, %d quotes subscribed", len(subs))
                self.stats.update(connected=True, symbols=len(subs), last_error="")
                while not gone.is_set():
                    waiters = [asyncio.create_task(gone.wait()), asyncio.create_task(self.changed.wait())]
                    await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
                    for w in waiters:
                        w.cancel()
                    if self.changed.is_set() and not gone.is_set():   # the lists or charts changed: same connection
                        self.changed.clear()
                        await self._sync(ib, market, subs, by_ticker)
                        self.stats["symbols"] = len(subs)
                log.warning("IBKR disconnected")
                self.stats["last_error"] = "disconnected"
            except (OSError, asyncio.TimeoutError, ConnectionError) as e:
                log.warning("IBKR connect failed: %s", e)
                self.stats["last_error"] = f"connect failed: {type(e).__name__}"
            finally:
                self.ib = None
                self.stats["connected"] = False
                ib.disconnect()
            await asyncio.sleep(15)

    async def _sync(self, ib, market, subs: dict, by_ticker: dict):
        """Subscribes the watched symbols not yet subscribed and drops the ones no longer watched. The future and the
        stock with its root for a ticker (ZS1!, ZS) are two keys, each its own contract. Two keys of one contract (US10Y
        and TNX) share its ticker, asked for once: ib_async keeps one ticker per contract, and cancels only its latest
        request. Its market data is cancelled when the last of them goes."""
        want = {k for k in market.watch_keys if contract_for(k)}
        for key in [k for k in subs if k not in want]:
            t = subs.pop(key)
            keys = by_ticker.get(id(t), set())
            keys.discard(key)
            if not keys:
                by_ticker.pop(id(t), None)
                ib.cancelMktData(t.contract)
        for key in sorted(want - subs.keys()):
            c = await self._contract(ib, key)
            if c is not None and key not in subs and ib.isConnected():
                t = next((s for s in subs.values() if s.contract == c), None) or ib.reqMktData(c, "", False, False)
                subs[key] = t
                by_ticker.setdefault(id(t), set()).add(key)

    async def _contract(self, ib, key: str):
        """The contract a quote key's quotes come from (a future's front month), qualified once per connection."""
        if key in self.contracts:
            return self.contracts[key]
        from ib_async import Contract, Future

        c = Contract(**contract_for(key))
        q = await ib.qualifyContractsAsync(c)
        if not q or q[0] is None:
            log.warning("IBKR could not qualify %s", key)
            c = None
        else:
            c = q[0]
            if c.secType == "CONTFUT":  # market data needs the actual front-month contract
                c = Future(conId=c.conId, exchange=c.exchange)
                await ib.qualifyContractsAsync(c)
        self.contracts[key] = c
        return c

    async def history(self, key: str, size: str, span: str, rth: bool = True) -> dict | None:
        """A chart's bars from IBKR's historical data: {"kind" (secType), "name", "tick" (its minimum tick), "rows":
        [(date for daily and longer, else a UTC datetime, o, h, l, c, v)]}, `size` bars (BAR_SIZES, CAL_SIZES) over
        `span` back from now; rth: a stock's or index's regular hours only. None when IBKR is not connected or has no
        such contract; no rows without its market data subscription (IBKR answers with an error, logged)."""
        ib = self.ib
        if ib is None or not ib.isConnected():
            return None
        d = await self._details(ib, key)
        if d is None:
            return None
        c = d.contract
        try:
            got = await ib.reqHistoricalDataAsync(c, "", span, size, SHOW.get(c.secType, "TRADES"),
                                                  bool(rth) and c.secType in ("STK", "IND"), formatDate=2,
                                                  timeout=HISTORY_TIMEOUT_S)
        except Exception as e:   # noqa: BLE001  a request error or a dropped connection: the next source
            log.info("IBKR history for %s not read (%s)", key, type(e).__name__)
            return None
        rows = [(b.date, *(per_cent(key, x) for x in (b.open, b.high, b.low, b.close)), b.volume) for b in got or []]
        return {"kind": c.secType, "name": d.longName or "", "tick": per_cent(key, d.minTick or 0), "rows": rows}

    async def _details(self, ib, key: str):
        """The contract details a quote key's history comes from (a future's continuous contract), once per connection."""
        if key in self.details:
            return self.details[key]
        spec = contract_for(key)
        if spec is None:
            return None
        from ib_async import Contract

        try:
            got = await asyncio.wait_for(ib.reqContractDetailsAsync(Contract(**spec)), 15)
        except Exception as e:   # noqa: BLE001  asked again next time
            log.info("IBKR contract for %s not read (%s)", key, type(e).__name__)
            return None
        self.details[key] = got[0] if len(got) == 1 else None
        if self.details[key] is None:
            log.info("IBKR has %s contracts for %s; no history from IBKR", len(got) or "no", key)
        return self.details[key]
