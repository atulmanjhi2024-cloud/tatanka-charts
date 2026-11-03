"""The feed plugin interface: what a data feed written for this app gets and sends. docs/FEEDS.md is the guide.

A plugin is one .py file with a class deriving from FeedPlugin. Built-in ones live in chartsrv/feeds/plugins/; your
own go in the feeds/ folder next to docker-compose.yml (mounted at /feeds). Each shows under the + of its kind in
Connections (Futures feed or Quotes) with the fields it declares; its keys are kept like every other key (never sent
back to a browser, never logged). More than one can run, and the same plugin more than once (two accounts).

Futures feeds send trades for chart instruments (Connections > Instruments). Each instrument gets its trades from one
feed only: the ones a plugin lists in its "Futures" field come from it, all others from NinjaTrader (or the
simulator, which then runs alone: real and invented trades never mix). Quote feeds send watchlist quotes.

    class MyFeed(FeedPlugin):
        TITLE = "My broker"
        KIND = "futures"
        FIELDS = [secret("api_key", "API key"), text("host", "Server", default="api.example.com")]

        async def run(self, market):
            async with connect(self.values["host"], self.values["api_key"]) as conn:
                self.connected()
                async for t in conn.trades(self.symbols):
                    self.trade(market, t.symbol, t.ms, t.price, t.size, t.side)

run() is restarted 10 s after it raises or returns (an error shows on its card); it is cancelled when the feed is
turned off or saved again. The helpers check every value, so a bad one is counted and dropped, never charted."""
import asyncio
import logging
import math
import re
import time
from pathlib import Path
from urllib.parse import quote, quote_plus

from .. import sessions, stopping

log = logging.getLogger(__name__)
SYMBOL_RE = re.compile(r"^[A-Z0-9][A-Z0-9.\-_/]{0,19}(1!)?$")   # a future as its front month too: "ZS1!"
FEEDS_DIR = Path("/feeds")   # the feeds folder (CHART_FEEDS_DIR): plugins may read files kept there


# ---- fields: what Connections asks for. Values arrive in self.values by key (secrets too), "" when not set -------------
def secret(key: str, label: str, note: str = "") -> dict:
    """A key, password or token: stored on the server, never shown again."""
    return {"key": key, "type": "secret", "label": label, "note": note, "plugin": True}


def text(key: str, label: str, note: str = "", default: str = "") -> dict:
    return {"key": key, "type": "text", "label": label, "note": note, "default": default}


def number(key: str, label: str, note: str = "", default: int = 0, min: int = 0, max: int = 1_000_000) -> dict:   # noqa: A002
    return {"key": key, "type": "int", "label": label, "note": note, "default": default, "min": min, "max": max}


def choice(key: str, label: str, options: list[tuple], note: str = "", default=None) -> dict:
    return {"key": key, "type": "choice", "label": label, "note": note, "options": [list(o) for o in options],
            "default": options[0][0] if default is None else default}


def without_keys(text: str, fields: list, values: dict) -> str:
    """`text` with the values of the secret `fields` taken out, also as a URL in an error carries them: a query
    parameter's percent-encoded (+ / = and spaces), and one written into the URL with only its spaces and such escaped."""
    for f in fields:
        v = values.get(f["key"])
        if f["type"] == "secret" and isinstance(v, str) and len(v) >= 4:
            for form in (v, quote_plus(v, safe=""), quote(v, safe=""), quote(v, safe="!#$%&'()*+,/:;=?@[\\]^`{|}~")):
                text = text.replace(form, "•••")
    return text


class FeedPlugin:
    TITLE = "Feed"         # its name in Connections
    KIND = "futures"       # "futures": trades for chart instruments; "quotes": watchlist quotes; "data": DATA: series
    ABOUT = ""             # one or two sentences: what it gives, what account it needs
    FIELDS: list = []      # secret(), text(), number(), choice()
    REQUIRES: list = []    # pip packages it imports, e.g. ["databento"]: see docs/FEEDS.md, "Packages"
    QUOTE_RANK = 2         # quotes: ahead of Yahoo (1), level with IBKR and Alpaca (2), behind NinjaTrader (3)

    def __init__(self, name: str, values: dict, symbols: list[str]):
        self.name = name               # its connection id: "ibkr_trades", "ibkr_trades-2"
        self.values = values           # field key -> value
        self.symbols = list(symbols)   # futures: the instruments it supplies; quotes: the symbols it may quote
        self.stats = {"connected": False, "ticks": 0, "quotes": 0, "dropped": 0, "last_msg_ms": None, "last_error": "", "note": ""}
        self._named: dict[str, tuple[str, str]] = {}   # symbol -> the contract it last reported (name, was)

    # -- what a plugin writes ----------------------------------------------------------------------------------------
    async def run(self, market):
        """Connect, then send trades (self.trade) or quotes (self.quote) until cancelled. Raise on a lost connection
        (or return): it runs again 10 s later."""
        raise NotImplementedError

    async def history(self, symbol: str, start_ms: int, end_ms: int):
        """Optional, futures: the trades (("trade", ms, price, size)) or 1-minute bars (("bar", open_ms, o, h, l, c, v))
        of `symbol` between start_ms and end_ms, oldest first, as an async generator. Called once connected for every
        instrument whose stored data stops short of now, so a gap while the feed was down fills in, and for holes
        inside the stored data of the last days: end_ms can be days ago, so ask for that stretch, not up to now."""
        return
        yield

    def fills(self) -> bool:
        """Optional, futures: whether history() asks for anything (by default: when the plugin defines one). False
        while its gap fill is turned off (Databento's Fill gaps): the holes inside the stored data are then left for a
        feed that asks, not taken for asked and answered with nothing."""
        return type(self).history is not FeedPlugin.history

    # -- the symbol list -------------------------------------------------------------------------------------------
    async def listing(self, http) -> list[tuple[str, str, str, str]]:
        """Optional, quotes and data: the symbols it has, for the search box: (symbol, name, type, exchange); type one
        of stock, etf, fund, index, forex, crypto, economic, data. Asked daily and when it is saved."""
        return []

    # -- data plugins: series of values by date, charted and in the watchlists as DATA:<code> ----------------------
    def serves(self, code: str) -> bool:
        """Data: whether DATA:<code> is one of its series (a CSV feed: its own symbol; an API: any code it knows)."""
        return True

    async def series(self, http, code: str) -> tuple[list[tuple[str, float]], int]:
        """Data: the series' observations [("YYYY-MM-DD", value)], oldest first, and the decimals to show. Raise
        ValueError for a code it doesn't have. The app keeps the answer and asks again after 3 hours."""
        raise NotImplementedError

    def title(self, code: str) -> str:
        """Data: the series' name, for the chart."""
        return code

    def describe(self) -> str:
        """Data: a word for its Connections card ("DATA:GOLDOIL")."""
        return ""

    @classmethod
    async def test(cls, values: dict) -> tuple[bool, str]:
        """Optional: try the fields as typed (Connections > Test) without starting the feed."""
        return False, "This feed has no test: turn it on and watch its state"

    # -- what the app runs ----------------------------------------------------------------------------------------------
    async def serve(self, market):
        """run(), its error shown on the card (keys taken out), and the gap fill once it reports connected. The gaps'
        windows open first, before a live trade can arrive and be taken for the end of the stored data."""
        gf = getattr(market, "gapfill", None)
        self._gaps = {}   # symbol -> the window it opened
        if gf is not None and self.KIND == "futures":
            now = int(time.time() * 1000)
            self._gaps = {s: gf.windows[s] for s in self.symbols if self._supplies(market, s) and gf.open(s, now)}
        self._filler = asyncio.create_task(self._fill_when_connected(market))
        try:
            await self.run(market)
            task = asyncio.current_task()
            if not (task and task.cancelling()):   # its stream ended (async for stops on a clean close): run it again
                raise ConnectionError("the connection ended")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self.connected(False, self.scrub(f"{type(e).__name__}: {str(e).splitlines()[0] if str(e) else ''}".rstrip(": ")))
            raise
        finally:
            # the gap filler ends first: its last word on a window comes before a new feed opens its own (set_feed
            # waits 5 s). Stopped again while it unwinds (a stop its request swallowed, a websocket's close handshake
            # with a silent server), and 2 s at most: a history that won't end holds no restart
            filler, self._filler = self._filler, None   # the last fill, which waits for (so cancels) those before it
            filler.cancel()
            for _ in range(8):
                if (await asyncio.wait({filler}, timeout=0.25))[0]:
                    break
                filler.cancel()   # (through its gather, to the fill before it too)
            self.stats["connected"] = False
            for sym, w in self._gaps.items():   # stopped before its history came: what came is kept, the rest asked
                if gf.windows.get(sym) is w:   # next time (not one a new feed opened while this run() was ending)
                    gf.done(sym, "none", 0, f"{self.TITLE} stopped before its history")
            self._gaps = {}

    def _supplies(self, market, sym: str) -> bool:
        """Whether it sends `sym`'s trades: only then is that instrument's gap fill its own (a window open for another
        feed's would be closed)."""
        return sym in market.instruments and getattr(market, "trade_owner", {}).get(sym) == self.name

    async def _fill_when_connected(self, market):
        while not self.stats["connected"]:
            await asyncio.sleep(0.5)
        await self.fill_gaps(market)

    def fill_added(self, market, syms: list[str]):
        """Called by the app for instruments put on Instruments while it runs (back after being taken off, or opened
        from the catalog): the ones it supplies have the stretch since their stored data stops filled from history()
        as at connect, after any fill still going. Their windows open now, before their next live trade."""
        gf = getattr(market, "gapfill", None)
        if gf is None or self.KIND != "futures" or getattr(self, "_filler", None) is None:
            return   # not serving: serve() opens them when it starts
        now = int(time.time() * 1000)
        mine = [s for s in syms if self._supplies(market, s)]
        new = {s for s in mine if s not in gf.windows and gf.open(s, now)}
        for s in mine:   # the contract it reported while the instrument was off (dropped then): a roll meanwhile is
            if s in self._named:   # recorded now, and its window just opened keeps the new contract's sessions only
                self.contract(market, s, *self._named[s])
        if new:
            self._gaps.update({s: gf.windows[s] for s in new})
            self._filler = asyncio.create_task(self._fill_added(market, self._filler, new))

    async def _fill_added(self, market, before: asyncio.Task, syms: set[str]):
        await asyncio.gather(before, return_exceptions=True)   # one fill at a time (fill_gaps may have done these)
        while not self.stats["connected"]:
            await asyncio.sleep(0.5)
        for sym in sorted(s for s in syms if s in self._gaps):   # (a new feed's window there: this one stopped)
            if market.gapfill.windows.get(sym) is self._gaps[sym] \
                    and (span := market.gapfill.until(sym, int(time.time() * 1000))) is not None:
                await self._fill(market.gapfill, sym, span)
            self._gaps.pop(sym, None)

    def scrub(self, text: str) -> str:
        """`text` with this feed's secrets taken out: for error texts shown on its card or written to the log."""
        return without_keys(text, self.FIELDS, self.values)[:200]

    # -- helpers it calls ---------------------------------------------------------------------------------------------
    def connected(self, ok: bool = True, error: str = ""):
        """Its state on the Connections card. An error text is shown (its keys taken out)."""
        self.stats["connected"] = ok
        self.stats["last_error"] = "" if ok else self.scrub(error)
        self.stats["note"] = ""

    def waiting(self, note: str):
        """Not connected, and not an error: "Listening on port 8802", "Market closed"."""
        self.stats.update(connected=False, last_error="", note=self.scrub(note))

    def watch_changed(self, market):
        """Optional, quotes: the watchlists changed; market.watch_keys holds every symbol in them."""

    def wanted(self, market) -> list[str]:
        """Quotes: the watchlist symbols this feed may quote (its Symbols field; empty: all of them). A future is
        its front month ("ZS1!", soybeans; "ZS" is Zscaler's stock); a root in the Symbols field covers it."""
        keys = sorted(getattr(market, "watch_keys", ()) or ())
        return [k for k in keys if self._listed(k)] if self.symbols else keys

    def _listed(self, key: str) -> bool:
        """Whether its Symbols field lets it quote `key`: there, or a future whose root is (ZS covers ZS1!)."""
        return key in self.symbols or (key.endswith("1!") and key[:-2] in self.symbols)

    def _quote_key(self, market, symbol: str) -> str:
        """The quote key of a symbol a plugin quotes: a futures feed's root ("ZN") is its future's, ZN1!; a quotes
        feed's root whose future is listed and the root itself is not (a plugin from before futures were keyed by
        their front month) too."""
        if symbol.endswith("1!"):
            return symbol
        keys = getattr(market, "watch_keys", ()) or ()
        return f"{symbol}1!" if self.KIND == "futures" or (symbol not in keys and f"{symbol}1!" in keys) else symbol

    def trade(self, market, symbol: str, ms: int, price: float, size: float, side: int = 0) -> bool:
        """One trade: epoch milliseconds, price, contracts, aggressor side (1 buy, -1 sell, 0 unknown). Only for the
        instruments this feed supplies; rounded to the tick. False when dropped."""
        from ..models import Tick
        inst = market.instruments.get(symbol)
        if inst is None or market.trade_owner.get(symbol) != self.name:
            self.stats["dropped"] += 1
            return False
        try:
            ms, price, size = int(ms), float(price), int(size)
        except (TypeError, ValueError, OverflowError):
            self.stats["dropped"] += 1
            return False
        now = int(time.time() * 1000)
        if not (math.isfinite(price) and price > 0) or size <= 0 or side not in (-1, 0, 1) or not now - 14 * 86_400_000 < ms < now + 60_000 \
                or sessions.weekend(ms):   # no future trades then: a replay of the last trade (sessions.weekend)
            self.stats["dropped"] += 1
            return False
        self.stats["ticks"] += 1
        self.stats["last_msg_ms"] = now
        if hasattr(market, "gapfill"):
            market.gapfill.live(symbol, ms)
        market.on_tick(Tick(symbol, ms, inst.round(price), size, int(side)), live=now - ms < 120_000)   # a numpy side: stored as a number
        return True

    def quote(self, market, symbol: str, last: float, prev_close: float | None = None, volume: float | None = None,
              at: int | None = None) -> bool:
        """A watchlist quote: last price, previous close (for the change), the day's volume, its own time (ms).
        `symbol` as wanted() gives it ("ZS1!" the future, "ZS" the stock); a futures feed's root is its future's."""
        try:   # plain numbers (a pandas row's are numpy's, an API's can be text): every page gets them as JSON
            last = float(last)
            prev_close = float(prev_close) if prev_close is not None else None
            volume = float(volume) if volume is not None else None
            at = int(at) if at is not None else None
        except (TypeError, ValueError, OverflowError):
            self.stats["dropped"] += 1
            return False
        key = self._quote_key(market, str(symbol))
        if not SYMBOL_RE.match(str(symbol)) or not (math.isfinite(last) and last > 0) \
                or (self.KIND == "quotes" and self.symbols and not self._listed(key)):   # its Symbols field: only those
            self.stats["dropped"] += 1
            return False
        self.stats["quotes"] += 1
        self.stats["last_msg_ms"] = int(time.time() * 1000)
        market.on_quote(key, last, prev_close if prev_close and math.isfinite(prev_close) else None,
                        volume if volume is not None and math.isfinite(volume) and volume >= 0 else None, source=self.name, at=at)
        return True

    def contract(self, market, symbol: str, name: str, was: str | None = None):
        """The contract it now sends for `symbol` ("ES 12-26"): a change is a roll, sent to the alert channels. `was`:
        the name an earlier version of the plugin gave this same contract. A stored `was` is renamed without a roll
        only at the first report after the update that changed the naming (so far the IBKR futures plugin's). One for
        an instrument off Instruments is kept until it is put back (fill_added): plugins report it when they connect."""
        if not (isinstance(name, str) and name.strip()):
            return
        name, was = name.strip()[:24], was.strip()[:24] if isinstance(was, str) else ""
        if symbol in self.symbols:
            self._named[symbol] = (name, was)
        if symbol in market.instruments and market.trade_owner.get(symbol) == self.name:
            if was and was != name:
                market.on_contract(symbol, name, was=was)
            else:   # as before: a plugin's own stand-in for the market may take two
                market.on_contract(symbol, name)

    async def fill_gaps(self, market):
        """Called by the app once run() reports connected: history() for each of its instruments' gaps, then for the
        holes inside their stored data of the last days (gapfill.py)."""
        gf = getattr(market, "gapfill", None)
        if gf is None or self.KIND != "futures":
            return   # a quotes feed's Symbols can name a future: its gap fill is the futures feed's
        now = int(time.time() * 1000)
        gaps = getattr(self, "_gaps", {})
        for sym in self.symbols:   # a window there that isn't its own is a new feed's (this one stopped): left alone
            if not self._supplies(market, sym) or gf.windows.get(sym) is not gaps.get(sym):
                continue
            span = gf.until(sym, int(time.time() * 1000)) if sym in gaps else gf.open(sym, now)   # opened by serve()
            if span is not None:
                await self._fill(gf, sym, span)
            gaps.pop(sym, None)
        if not self.fills() or not hasattr(gf, "holes"):
            return   # it asks no history: its holes stay for a feed that does
        for sym in self.symbols:   # (one put back meanwhile: its stretch first, fill_added; a window open: a new feed's)
            if not self._supplies(market, sym) or sym in gaps or sym in gf.windows:
                continue
            gf.queue[sym] = gf.holes(sym, now)
            while gf.queue.get(sym) and sym not in gf.windows:   # a roll meanwhile empties it
                await self._fill(gf, sym, gf.open_hole(sym, *gf.queue[sym].pop(0)))
            gf.settle(sym)

    async def _fill(self, gf, sym: str, span: tuple[int, int]):
        kind, sent = "none", 0
        w = gf.windows.get(sym)   # its window: once a new feed's is there instead (this one stopped), it is left alone

        def done(how: str, note: str):
            if gf.windows.get(sym) is w:
                gf.done(sym, how, sent, note)
        try:
            stopping.check()   # a stop an earlier request swallowed (one that failed or answered nothing): ended here
            async for item in self.history(sym, *span):
                stopping.check()   # a stop its request swallowed (a plugin's httpx history): ended here
                if gf.windows.get(sym) is not w:
                    return
                if item[0] == "trade":
                    kind = "trades"
                    gf.tick(sym, int(item[1]), float(item[2]), int(item[3]))
                elif item[0] == "bar":
                    kind = "minutes"
                    gf.minute(sym, int(item[1]), *(float(x) for x in item[2:6]), int(item[6]))
                sent += 1
            done(kind, f"from {self.TITLE}")
        except asyncio.CancelledError:   # stopped: what came is kept, the rest asked next time
            done("error", f"{self.TITLE} stopped before its history")
            raise
        except Exception as e:   # a plugin's history must not stop its live trades
            log.warning("%s: history for %s failed (%s)", self.name, sym, type(e).__name__)
            done("error", f"{self.TITLE} history failed: {type(e).__name__}")
