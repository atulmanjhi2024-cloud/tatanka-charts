"""Every trade of chosen futures from Interactive Brokers (IB Gateway or TWS with the API on, read-only): IBKR's
tick-by-tick "Last" stream on the front month, which it picks (CONTFUT) and this feed follows through rolls. Gaps
while it was off fill from IBKR's 1-minute bars. Market data only: no orders, no account calls.

IBKR allows a few tick-by-tick streams at once (3 on most accounts) and needs its live CME data subscription for
them; delayed data has no tick-by-tick."""
import asyncio
import logging
import math
import time
from datetime import datetime, timezone

from chartsrv.feeds.ibkr import contract_for
from chartsrv.feeds.sdk import FeedPlugin, number, text

log = logging.getLogger(__name__)
ROLL_CHECK_S = 1800


def contract_name(root: str, month: str) -> str:
    """"ES", "202612" -> "ES 12-26", as NinjaTrader names contracts."""
    return f"{root} {month[4:6]}-{month[2:4]}" if len(month) >= 6 else root


class IbkrTrades(FeedPlugin):
    TITLE = "Interactive Brokers futures"
    KIND = "futures"
    ABOUT = ("Every trade of the futures you choose, from IB Gateway or TWS (API on, read-only). Needs IBKR's live CME "
             "data for them; IBKR allows a few tick-by-tick streams at once (3 on most accounts).")
    FIELDS = [text("host", "Host", "host.docker.internal from Docker; 127.0.0.1 otherwise", default="host.docker.internal"),
              number("port", "Port", "Gateway 4001 (paper 4002), TWS 7496 (paper 7497)", default=4001, min=1, max=65535),
              number("client_id", "Client ID", "a number no other API app uses (not the IBKR quotes' one)", default=32, min=0, max=999_999)]
    REQUIRES = ["ib_async"]

    async def run(self, market):
        from ib_async import IB, StartupFetch

        self.ib = ib = IB()
        gone = asyncio.Event()
        ib.disconnectedEvent += lambda: gone.set()
        try:
            try:
                await ib.connectAsync(self.values["host"], int(self.values["port"]), clientId=int(self.values["client_id"]),
                                      timeout=15, readonly=True, fetchFields=StartupFetch(0))
            except (OSError, asyncio.TimeoutError) as e:
                raise ConnectionError(f"nothing answers on {self.values['host']}:{self.values['port']} ({type(e).__name__})") from None
            ib.reqMarketDataType(1)   # tick-by-tick is live only
            self.futs, by_ticker = {}, {}
            for sym in self.symbols:
                fut = await self.front(ib, sym)
                if fut is None:
                    continue
                t = ib.reqTickByTickData(fut, "Last")
                by_ticker[id(t)] = sym
                self.futs[sym] = fut
                # was: its name before it read the contract month, by the last trading day ("CL 11-26" for CLZ6): the
                # first start after the update renames a stored one instead of taking it for a roll
                self.contract(market, sym, contract_name(sym, fut.month),
                              was=contract_name(sym, fut.lastTradeDateOrContractMonth))
            if not self.futs:
                raise RuntimeError("IBKR knows none of these futures")

            def on_pending(tickers):
                for t in tickers:
                    sym = by_ticker.get(id(t))
                    for tb in (t.tickByTicks if sym else ()):
                        if tb is not None and tb.price and math.isfinite(tb.price):
                            self.trade(market, sym, int(tb.time.timestamp() * 1000), tb.price, tb.size)
            ib.pendingTickersEvent += on_pending
            self.connected()
            log.info("%s: IBKR trades for %s", self.name, ", ".join(self.futs))
            next_check = time.monotonic() + ROLL_CHECK_S
            while not gone.is_set():
                await asyncio.sleep(1)
                if time.monotonic() > next_check:   # a roll: start again on the new front month
                    next_check = time.monotonic() + ROLL_CHECK_S
                    for sym, fut in self.futs.items():
                        now = await self.front(ib, sym)
                        if now is not None and now.conId != fut.conId:
                            raise RuntimeError(f"{sym} rolled to {contract_name(sym, now.month)}")
            raise ConnectionError("IB Gateway disconnected")
        finally:
            ib.disconnect()

    async def front(self, ib, sym: str):
        """The front-month contract IBKR gives for the root (its CONTFUT), qualified, or None. Its .month is the contract
        month ("202612"): its lastTradeDateOrContractMonth is the last trading day, in the month before for CL."""
        from ib_async import Contract, Future
        spec = contract_for(f"{sym}1!")   # the chart instrument's future, never a stock with its root for a ticker
        if not spec or spec.get("secType") != "CONTFUT":
            log.warning("%s: no IBKR exchange known for %s", self.name, sym)
            return None
        q = await ib.qualifyContractsAsync(Contract(**spec))
        if not q or q[0] is None:
            log.warning("%s: IBKR could not find %s", self.name, sym)
            return None
        details = await ib.reqContractDetailsAsync(Future(conId=q[0].conId, exchange=q[0].exchange))
        if not details:
            log.warning("%s: IBKR could not find %s", self.name, sym)
            return None
        fut = details[0].contract
        fut.month = details[0].contractMonth or fut.lastTradeDateOrContractMonth
        return fut

    async def history(self, symbol: str, start_ms: int, end_ms: int):
        """IBKR's 1-minute bars of the current contract over the gap or hole (ending at end_ms, not now)."""
        fut = getattr(self, "futs", {}).get(symbol)
        if fut is None:
            return
        days = max(1, math.ceil((end_ms - start_ms) / 86_400_000))
        end = datetime.fromtimestamp(math.ceil(end_ms / 60_000) * 60, timezone.utc)   # ib_async sends it as UTC
        bars = await self.ib.reqHistoricalDataAsync(fut, end, f"{min(days, 14)} D", "1 min", "TRADES", False, formatDate=2)
        for b in bars or []:
            t = b.date if isinstance(b.date, datetime) else datetime.combine(b.date, datetime.min.time(), timezone.utc)
            ms = int(t.timestamp() * 1000)
            if start_ms <= ms < end_ms:
                yield ("bar", ms, b.open, b.high, b.low, b.close, int(b.volume or 0))

    @classmethod
    async def test(cls, values: dict) -> tuple[bool, str]:
        host, port = values.get("host") or "127.0.0.1", int(values.get("port") or 4001)
        try:
            _, w = await asyncio.wait_for(asyncio.open_connection(host, port), 5)
            w.close()
            return True, f"IB Gateway/TWS answers on {host}:{port}"
        except (OSError, asyncio.TimeoutError):
            return False, (f"Nothing answers on {host}:{port}. Start IB Gateway or TWS and turn the API on (Configure, API, "
                           "Settings: Enable ActiveX and Socket Clients; Read-Only API).")
