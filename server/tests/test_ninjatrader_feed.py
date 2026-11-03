"""NinjaTrader add-on feed: a fake add-on speaks the wire protocol to the real TCP server."""
import asyncio
import sqlite3
import time

import pytest

from chartsrv.config import Instrument
from chartsrv.feeds.ninjatrader import NinjaTraderFeed


class FakeMarket:
    def __init__(self):
        self.instruments = {"ES": Instrument("ES", "E-mini", 0.25, 50, "ES"), "CL": Instrument("CL", "Crude", 0.01, 1000, "CL")}
        self.ticks, self.quotes, self.contracts = [], [], []

    def on_tick(self, tk, live=True):
        self.ticks.append((tk, live))

    def on_quote(self, sym, last, prev, vol=None):
        self.quotes.append((sym, last, prev) if vol is None else (sym, last, prev, vol))

    def on_contract(self, sym, contract):
        self.contracts.append((sym, contract))


async def start(token=""):
    feed, market = NinjaTraderFeed("127.0.0.1", 0, token), FakeMarket()
    server = await asyncio.start_server(lambda r, w: feed._handle(market, r, w), "127.0.0.1", 0)
    return feed, market, server, server.sockets[0].getsockname()[1]


async def send(port, *lines, wait=0.2):
    r, w = await asyncio.open_connection("127.0.0.1", port)
    w.write("".join(l + "\n" for l in lines).encode())
    await w.drain()
    await asyncio.sleep(wait)
    return r, w


def run(coro):
    return asyncio.run(coro)


def test_ticks_quotes_and_validation():
    async def go():
        feed, m, server, port = await start()
        now = int(time.time() * 1000)
        _, w = await send(port, "HELLO||1",
                          f"T|ES|{now}|6850.25|3|1",
                          f"T|CL|{now}|65.1199999|1|-1",          # rounded to tick
                          f"T|ES|{now - 60_000}|6849.00|1|0",      # buffered while server was down: not live
                          f"T|ZB|{now}|115.5|1|1",                 # not a chart symbol: ignored
                          "T|ES|x|1|1|1", f"T|ES|{now}|nan|1|1", f"T|ES|{now}|6850|1|7", "garbage",
                          "Q|ES|6850.25|6800", f"H|{now}")
        assert feed.stats["connected"]
        w.close()
        server.close()
        return feed, m
    feed, m = run(go())
    assert [(t.symbol, t.price, t.size, t.side, live) for t, live in m.ticks] == [
        ("ES", 6850.25, 3, 1, True), ("CL", 65.12, 1, -1, True), ("ES", 6849.0, 1, 0, False)]
    assert m.quotes == [("ES1!", 6850.25, 6800.0)]   # a future's quote: its front month's key
    assert feed.stats["bad_lines"] == 4 and feed.stats["ticks"] == 3


def test_bad_token_rejected():
    async def go():
        feed, m, server, port = await start(token="s3cret")
        now = int(time.time() * 1000)
        await send(port, "HELLO|wrong|1", f"T|ES|{now}|6850.25|1|1")
        await send(port, f"T|ES|{now}|6850.25|1|1")                 # no hello at all
        _, w = await send(port, "HELLO|s3cret|1", f"T|ES|{now}|6850.50|1|1")
        w.close()
        server.close()
        return m
    m = run(go())
    assert [t.price for t, _ in m.ticks] == [6850.50]


def test_a_token_with_non_ascii_characters():
    """Connections takes any token without spaces or |: 'Schlüssel' lets the add-on in, and a wrong one is refused."""
    from chartsrv.connections import _secret_ok
    assert _secret_ok("nt_token", "Schlüssel")

    async def go():
        feed, m, server, port = await start(token="Schlüssel")
        now = int(time.time() * 1000)
        await send(port, "HELLO|Schlüsse|1", f"T|ES|{now}|6850.25|1|1")
        _, w = await send(port, "HELLO|Schlüssel|9", f"T|ES|{now}|6850.50|1|1")
        connected = feed.stats["connected"]
        w.close()
        server.close()
        return m, connected
    m, connected = run(go())
    assert connected and [t.price for t, _ in m.ticks] == [6850.50]


def test_a_second_add_on_is_turned_away_while_the_first_talks(monkeypatch):
    """Two NinjaTrader instances: the one connected keeps the feed, and the other is told BYE, its lines (the trades it
    queued, which the first sent already) not stored. Replaced, the first would come back in 5 s with its own queue and
    the two would take turns, every trade stored twice."""
    async def go():
        feed, m, server, port = await start()
        now = int(time.time() * 1000)
        _, w1 = await send(port, "HELLO||11", f"T|ES|{now}|1.00|1|1")
        r2, w2 = await send(port, "HELLO||11", f"T|ES|{now}|1.00|1|1", f"T|ES|{now}|2.00|1|1")
        bye = await asyncio.wait_for(r2.readline(), 2)
        w1.write(f"T|ES|{now}|2.00|1|1\n".encode())
        await w1.drain()
        await asyncio.sleep(0.2)
        held = feed.writer is not None and feed.stats["connected"]
        w2.close()
        w1.close()
        await asyncio.sleep(0.2)
        server.close()
        return feed, m, bye, held
    feed, m, bye, held = run(go())
    assert bye.startswith(b"BYE|") and held
    assert [t.price for t, _ in m.ticks] == [1.0, 2.0] and not feed.stats["connected"]


def test_a_connection_gone_quiet_is_replaced(monkeypatch):
    """One that has sent nothing for a while (the PC asleep, a dead socket) is replaced by a new connection."""
    from chartsrv.feeds import ninjatrader
    monkeypatch.setattr(ninjatrader, "QUIET_S", 0.3)

    async def go():
        feed, m, server, port = await start()
        now = int(time.time() * 1000)
        _, w1 = await send(port, "HELLO||1", f"T|ES|{now}|1.00|1|1", wait=0.5)
        _, w2 = await send(port, "HELLO||1", f"T|ES|{now}|2.00|1|1")
        w1.write(f"T|ES|{now}|3.00|1|1\n".encode())                   # old connection: ignored
        try:
            await w1.drain()
        except ConnectionError:
            pass
        w2.write(f"T|ES|{now}|4.00|1|1\n".encode())
        await w2.drain()
        await asyncio.sleep(0.2)
        connected = feed.stats["connected"]
        w2.close()
        await asyncio.sleep(0.2)
        server.close()
        return feed, m, connected
    feed, m, connected = run(go())
    assert [t.price for t, _ in m.ticks] == [1.0, 2.0, 4.0]
    assert connected and not feed.stats["connected"]


def test_contract_lines():
    async def go():
        feed, m, server, port = await start()
        _, w = await send(port, "HELLO||1", "C|ES|ES 12-26", "C|ES|ES 03-27", "C|CL|CL DEC26", "C|ZB|ZB 12-26", "C|ES|ES March", "C|ES")
        w.close()
        server.close()
        return feed, m
    feed, m = run(go())
    assert m.contracts == [("ES", "ES 12-26"), ("ES", "ES 03-27"), ("CL", "CL DEC26")] and feed.stats["bad_lines"] == 2   # ZB: not charted here


def test_the_add_on_hears_the_chart_symbols():
    """On connect the server lists the futures it charts (Connections > Instruments): the add-on forwards those."""
    async def go():
        feed, m, server, port = await start()
        r, w = await send(port, "HELLO||1")
        first = await asyncio.wait_for(r.readline(), 2)
        w.close()
        server.close()
        return first
    assert run(go()) == b"I|ES,CL\n"


def test_a_failed_database_write_keeps_the_connection():
    """A line whose write fails (the disk full, the database locked by an import past the busy timeout) is logged and
    skipped: the connection goes on, not dropped to come back and fail the same way on every connect."""
    class Full:
        def on_feed(self, sym, price):
            raise sqlite3.OperationalError("database or disk is full")

    async def go():
        feed, m, server, port = await start()
        m.settlements = Full()
        now = int(time.time() * 1000)
        _, w = await send(port, "HELLO||12", "S|ES|6850.25", f"T|ES|{now}|6850.50|1|1")
        connected = feed.stats["connected"]
        w.close()
        server.close()
        return feed, m, connected
    feed, m, connected = run(go())
    assert connected and [t.price for t, _ in m.ticks] == [6850.50] and feed.stats["bad_lines"] == 0
