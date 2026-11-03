"""Futures feed: trades forwarded by the TatankaChartFeed NinjaTrader 8 add-on, and NinjaTrader's
history for any stretch the stored data is missing (see gapfill.py), quotes for the futures in the watchlists, and
your fills and positions (read-only; journal.py), from the TatankaChartFeed add-on (ninjatrader/TatankaChartFeed.cs) over a local TCP socket. Wire format
is documented there.

One add-on connection at a time, so two NinjaTrader instances can never double the ticks: while one is connected
and talking, another is turned away (BYE; add-on 12 drops what it queued meanwhile, the first sent it, and tries again
in 30 s); one gone quiet for QUIET_S is replaced. One with the wrong token is turned away too (BYE|wrong token), and
its card says so. Timestamps are the add-on's receive time (UTC ms). Ticks older than LIVE_WINDOW_MS (buffered while
this server was down) are stored and charted but do not trigger alerts."""
import asyncio
import hmac
import logging
import math
import re
import sqlite3
import time

from .. import journal, sessions
from .. import watchlists as wl
from ..models import Tick

log = logging.getLogger(__name__)
LIVE_WINDOW_MS = 10_000
IDLE_TIMEOUT_S = 30        # the add-on sends a heartbeat every 5 s
QUIET_S = 10               # a connection quiet this long is replaced by a new one; one talking keeps the feed
ADDON_VERSION = 19         # ninjatrader/TatankaChartFeed.cs's Version: an older one is told to update
CONTRACT_RE = re.compile(r"^[A-Z0-9]{1,8} (\d\d-\d\d|[A-Z]{3}\d\d)$")   # NinjaTrader's names: "ES DEC26" or "ES 12-26"


def _num(s: str) -> float | None:
    try:
        v = float(s)
    except ValueError:
        return None
    return v if math.isfinite(v) and v > 0 else None


class NinjaTraderFeed:
    name = "ninjatrader"

    def __init__(self, host: str, port: int, token: str | None):
        self.host = host
        self.port = port
        self.token = token or ""
        self.writer: asyncio.StreamWriter | None = None
        self.heard = 0.0           # when the connection last sent a line (monotonic)
        self.warned = None         # the connection a turned-away one was logged against (once each)
        self.stats = {"connected": False, "ticks": 0, "bad_lines": 0, "last_msg_ms": None}

    def close(self):
        """Stopped from Connections: the add-on's connection goes too (it reconnects to the new listener)."""
        if self.writer is not None:
            self.writer.close()

    async def run(self, market):
        server = await asyncio.start_server(lambda r, w: self._handle(market, r, w), self.host, self.port)
        log.info("waiting for NinjaTrader add-on on %s:%d", self.host, self.port)
        async with server:
            await server.serve_forever()

    async def _handle(self, market, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        peer = writer.get_extra_info("peername")
        try:
            hello = (await asyncio.wait_for(reader.readline(), 10)).decode("utf-8", "replace").strip().split("|")
            given = (hello[1] if len(hello) > 1 else "").encode()   # bytes: compare_digest raises on a str with ü
            if hello[0] != "HELLO" or not hmac.compare_digest(given, self.token.encode()):
                log.warning("rejected NinjaTrader connection from %s: bad hello or token", peer)
                if hello[0] == "HELLO":   # said on its card and in NinjaScript Output (the add-on prints BYE's reason)
                    self.stats["refused"] = time.time()
                    writer.write(b"BYE|wrong token: Token in TatankaChartFeed.cs must be the server's "
                                 b"(Connections > NinjaTrader > Token)\n")
                    await writer.drain()
                    try:   # until it hangs up, as below (5 s at most; what it sends meanwhile is dropped, not kept)
                        async with asyncio.timeout(5):
                            while await reader.read(65536):
                                pass
                    except (TimeoutError, ConnectionError):
                        pass
                return
            if self.writer is not None and time.monotonic() - self.heard < QUIET_S:
                if self.warned is not self.writer:
                    self.warned = self.writer
                    log.warning("NinjaTrader connection from %s turned away: another add-on is connected", peer)
                writer.write(b"BYE|another NinjaTrader add-on is connected to the server\n")
                await writer.drain()
                try:   # until it hangs up (add-on 12, on BYE; an older one goes after 5 s), so no reset loses the BYE
                    await asyncio.wait_for(reader.read(), 5)
                except (asyncio.TimeoutError, ConnectionError):
                    pass
                return
            if self.writer is not None:
                log.warning("new NinjaTrader connection from %s replaces the previous one (quiet for %ds)", peer,
                            time.monotonic() - self.heard)
                self.writer.close()
            self.writer = writer
            self.heard = time.monotonic()
            self.stats["connected"] = True
            self.stats.pop("refused", None)
            self.stats["version"] = int(hello[2]) if len(hello) > 2 and hello[2].isdigit() else 0
            log.info("NinjaTrader add-on connected from %s", peer)
            gf = getattr(market, "gapfill", None)
            gaps = gf.requests(int(time.time() * 1000), live=self._live(),   # where stored data stops
                               minutes=self.stats.get("version", 0) >= 7, contracts=self.stats.get("version", 0) >= 8) if gf else []
            self._send_charts(market, writer)   # first: the add-on needs each future's feed and contract to answer
            if gaps:
                writer.write(("".join(g + "\n" for g in gaps)).encode())
                log.info("asked NinjaTrader for history: %s", ", ".join(g.split("|", 2)[1] for g in gaps))
            self._send_watch(market, writer)
            await writer.drain()
            while line := await asyncio.wait_for(reader.readline(), IDLE_TIMEOUT_S):
                if self.writer is not writer:
                    break  # replaced
                self.heard = time.monotonic()
                self._line(market, line)
        except asyncio.TimeoutError:
            log.warning("NinjaTrader add-on went quiet for %ss; dropping it", IDLE_TIMEOUT_S)
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            if self.writer is writer:
                self.writer = None
                self.stats["connected"] = False
                log.warning("NinjaTrader add-on disconnected")
                gf = getattr(market, "gapfill", None)
                owned = getattr(market, "trade_owner", {})
                for sym in [s for s in getattr(gf, "windows", {}) if s not in owned]:   # its answers won't come now (asked
                    del gf.windows[sym]   # again at the next connect): its alerts wait for none (gapfill.filling)
                    gf.status[sym] = {**gf.status.get(sym, {}), "state": "dropped", "note": "the add-on disconnected"}
                for sym in list(getattr(gf, "changed", ())):   # holes left unasked: what was filled reloads now
                    gf.settle(sym)
            writer.close()

    def _send_charts(self, market, writer):
        """I|<roots>: the futures this server charts (Connections > Instruments): the add-on forwards each one's trades,
        not only its built-in list; then an R line each: how it rolls. An add-on from before a line ignores it."""
        owned = getattr(market, "trade_owner", {})
        roots = [s for s in getattr(market, "instruments", {}) if s not in owned]
        writer.write(("I|" + ",".join(roots) + "\n").encode())
        contracts = getattr(market, "contracts", None)
        contracts = contracts if isinstance(contracts, dict) else {}
        for s in roots:   # R|<root>|<volume|dates|MM-yy>|<contract charted last>: how each one rolls (add-on 3+)
            last = re.sub(r"[^A-Za-z0-9 \-]", "", str(contracts.get(s, "")))[:20]
            mode = getattr(market.instruments.get(s), "contract", "volume")
            writer.write(f"R|{s}|{mode}|{last}\n".encode())

    def _send_watch(self, market, writer):
        """W|<roots>: the futures the watchlists hold ("ES,ZN,SR3"); the add-on quotes each one's front month."""
        doc = getattr(market, "wl", None)
        if doc is not None:
            writer.write(("W|" + ",".join(wl.futures_roots(doc)) + "\n").encode())

    def _live(self) -> bool:
        """The add-on waits for NinjaTrader's data connection, asks up to its first live trade (GAP|...|live), and
        sends history only on the connection it was asked on, so it gets holes too: add-on 6+."""
        return self.stats.get("version", 0) >= 6

    def charts_added(self, market, syms: list[str]):
        """Chart futures added (or handed back by a feed plugin) while connected: the add-on forwards them now and sends
        their history."""
        if self.writer is None:
            return   # it gets them on connect
        self._send_charts(market, self.writer)
        gf = getattr(market, "gapfill", None)
        owned = getattr(market, "trade_owner", {})
        now = int(time.time() * 1000)
        for sym in syms:
            if sym in owned:
                continue   # another feed's: that feed fills it (FeedPlugin.fill_added); the answer here would be dropped
            span = gf.open(sym, now, reach=gf.reach()) if gf else None
            if span:
                self.writer.write(f"GAP|{sym}|{span[0]}|{span[1]}{'|live' if self._live() else ''}\n".encode())

    def ticking_again(self, market, sym: str):
        """`sym` ticks again after the watchdog found it stale, on this same connection (NinjaTrader's data connection
        lost and back): the hole it left is asked for now, as at a connect (add-on 6+ answers holes)."""
        gf = getattr(market, "gapfill", None)
        if self.writer is not None and self._live() and gf and (line := gf.again(sym, int(time.time() * 1000))):
            self.writer.write((line + "\n").encode())

    def watch_changed(self, market):
        if self.writer is not None:
            self._send_watch(market, self.writer)

    def _line(self, market, raw: bytes):
        p = raw.decode("utf-8", "replace").rstrip("\r\n").split("|")
        now = int(time.time() * 1000)
        self.stats["last_msg_ms"] = now
        try:
            if p[0] == "T" and len(p) == 6:
                sym = p[1]
                inst = market.instruments.get(sym)
                if inst is None or sym in getattr(market, "trade_owner", {}):   # another feed supplies it
                    return
                ts, price, size, side = int(p[2]), float(p[3]), int(p[4]), int(p[5])
                if not (math.isfinite(price) and price > 0) or size <= 0 or side not in (-1, 0, 1):
                    raise ValueError("out of range")
                if sessions.weekend(ts):   # NinjaTrader replaying its last trade on (re)connecting: no trade then
                    self.stats["dropped"] = self.stats.get("dropped", 0) + 1
                    return
                if hasattr(market, "gapfill"):
                    market.gapfill.live(sym, ts)
                market.on_tick(Tick(sym, ts, inst.round(price), size, side), live=now - ts < LIVE_WINDOW_MS)
                self.stats["ticks"] += 1
            elif p[0] == "E":   # one of your fills (read-only)
                if hasattr(market, "on_fill"):
                    market.on_fill(journal.parse_line(p))
            elif p[0] == "P" and len(p) == 8:   # one of your positions, as NinjaTrader has it (add-on 13)
                if hasattr(market, "on_position"):
                    market.on_position(journal.parse_position(p))
            elif p[0] == "P" and len(p) == 3:   # on connect, after the open ones: the account's others are flat
                if hasattr(market, "on_flat"):
                    market.on_flat(*journal.parse_flat(p))
            elif p[0] == "Q" and len(p) == 4:   # a future's, by its root: keyed as its front month (ZS1!; ZS is a stock)
                market.on_quote(f"{p[1]}1!", _num(p[2]), _num(p[3]))
            elif p[0] == "Q" and len(p) == 5:   # with the day's volume
                vol = float(p[4])
                market.on_quote(f"{p[1]}1!", _num(p[2]), _num(p[3]), vol if math.isfinite(vol) and vol >= 0 else None)
            elif p[0] == "S" and len(p) == 3:   # the settlement its exchange published (add-on 9+)
                price = float(p[2])
                if not (math.isfinite(price) and price > 0):
                    raise ValueError("out of range")
                if hasattr(market, "settlements"):
                    market.settlements.on_feed(p[1], price)
            elif p[0] == "C" and len(p) == 3:
                if p[1] not in market.instruments or p[1] in getattr(market, "trade_owner", {}):
                    return   # not charted here, or another feed supplies it (its contract is the one charted)
                if not CONTRACT_RE.match(p[2]):
                    raise ValueError("bad contract")
                market.on_contract(p[1], p[2])
            elif p[0] == "C" and len(p) == 4 and p[3] == "quote":   # a watchlist future's quote rolled (add-on 12)
                if p[1] in market.instruments:
                    return   # charted here: its own C line rolls it
                if not CONTRACT_RE.match(p[2]):
                    raise ValueError("bad contract")
                if hasattr(market, "settlements"):
                    market.settlements.rolled(p[1])   # the settlements known are the old contract's
            elif p[0] in "XYZ" and len(p[0]) == 1 and (not hasattr(market, "gapfill") or
                                                       (len(p) > 1 and p[1] in getattr(market, "trade_owner", {}))):
                pass   # another feed supplies it: its window is that feed's
            elif p[0] == "X" and len(p) == 5 and p[1] in market.instruments:     # a historical trade (gap fill)
                price = float(p[3])
                if not (math.isfinite(price) and price > 0):
                    raise ValueError("out of range")
                market.gapfill.tick(p[1], int(p[2]), price, int(p[4]))
            elif p[0] == "Y" and len(p) == 8 and p[1] in market.instruments:     # a historical 1-minute bar
                o, h, lo, c = (float(x) for x in p[3:7])
                if not all(math.isfinite(x) and x > 0 for x in (o, h, lo, c)):
                    raise ValueError("out of range")
                market.gapfill.minute(p[1], int(p[2]), o, h, lo, c, int(float(p[7])))
            elif p[0] == "Z" and len(p) >= 5 and p[1] in market.instruments:     # history for a symbol is done
                market.gapfill.done(p[1], p[2], int(p[3]), "|".join(p[4:])[:200])
                if self.writer is not None and (nxt := market.gapfill.next(p[1])):   # its next hole
                    self.writer.write((nxt + "\n").encode())
            elif p[0] == "H":
                pass
            else:
                raise ValueError("unknown message")
        except ValueError:
            self.stats["bad_lines"] += 1
            if self.stats["bad_lines"] <= 5:
                log.warning("bad line from NinjaTrader add-on: %r", raw[:200])
        except sqlite3.Error as e:   # a full disk, an import's lock past the busy timeout: the connection goes on
            log.warning("database not writable (%s): NinjaTrader's %s line not stored", e, "|".join(p[:2]))
