"""Fourth bug-hunt fixes in the feeds: your own program's and the NinjaTrader add-on's connections when their feed or
the server stops, on Python 3.12."""
import asyncio
import socket
import time

import pytest

from chartsrv.feeds.ninjatrader import NinjaTraderFeed
from chartsrv.plugins import BUILTIN_DIR, Plugins

from test_review_fixes import build_market


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


async def _program(port, hello):
    """A program of the user's, connected and past its HELLO (the feed's listener can take a moment to open)."""
    for _ in range(100):
        try:
            r, w = await asyncio.open_connection("127.0.0.1", port)
            break
        except OSError:
            await asyncio.sleep(0.02)
    w.write(hello.encode() + b"\n")
    await w.drain()
    await asyncio.sleep(0.1)
    return r, w


async def _hung_up(r):
    """Whether the server closed the program's connection (whatever it sent before)."""
    try:
        await asyncio.wait_for(r.read(), 1)
        return True
    except ConnectionError:
        return True
    except asyncio.TimeoutError:
        return False


@pytest.fixture
def python312(monkeypatch):
    """Python 3.12's listener: stopped, it waits for every connection to go, and hangs none up itself (3.13's
    serve_forever does: close_clients). The README's install without Docker runs on 3.12 (Ubuntu 24.04's)."""
    monkeypatch.setattr(asyncio.base_events.Server, "close_clients", lambda self: None, raising=False)


# ---- r4-crashes-1: your own program goes when its feed is turned off or saved again, and the server can stop ---------
def test_your_own_program_goes_when_its_feed_is_turned_off_or_the_server_stops(tmp_path, monkeypatch, python312):
    async def go():
        m, _ = build_market(tmp_path, monkeypatch)
        m.watch_keys = {"AAPL"}
        cls, port = Plugins([BUILTIN_DIR], set()).types["lines_quotes"], _free_port()
        m._add_feed(cls("lines_quotes", {"port": port, "token": "old"}, []))
        r, w = await _program(port, "HELLO|old|1")
        w.write(b"Q|AAPL|231.5|229\n")
        await w.drain()
        await asyncio.sleep(0.1)
        assert m.quotes["AAPL"]["last"] == 231.5
        t0 = time.monotonic()
        await m.set_feed("lines_quotes", None)   # On unticked
        took, gone = time.monotonic() - t0, await _hung_up(r)
        w.write(b"Q|AAPL|240|229\n")
        await asyncio.sleep(0.1)
        assert took < 2 and gone and m.quotes["AAPL"]["last"] == 231.5   # its quotes stop with it
        m._add_feed(cls("lines_quotes", {"port": port, "token": "new"}, []))   # on again, with a new token
        r2, w2 = await _program(port, "HELLO|new|1")
        assert m.feed_objs[-1].clients == 1
        await asyncio.wait_for(m.stop(), 2)   # a restart: it does not wait for the program to hang up
        assert await _hung_up(r2)
    asyncio.run(go())


def test_the_server_stops_with_the_ninjatrader_add_on_connected(tmp_path, monkeypatch, python312):
    async def go():
        m, _ = build_market(tmp_path, monkeypatch)
        port = _free_port()
        m._add_feed(NinjaTraderFeed("127.0.0.1", port, ""))
        r, _ = await _program(port, "HELLO||12")
        assert m.feed_objs[-1].stats["connected"]
        await asyncio.wait_for(m.stop(), 2)
        assert await _hung_up(r)
    asyncio.run(go())
