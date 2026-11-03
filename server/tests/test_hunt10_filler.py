"""Round 10 of the bug hunt, a futures plugin's gap filler as its feed stops (round 9 made serve() wait for it): the
wait is bounded and stops the filler again, so a history slow to end once stopped (a websocket's close handshake with
a silent broker, a stop its request swallowed) neither holds a restart nor outlasts set_feed's 5 s; the old feed and
its filler close and write only windows that are still their own, never the ones a new feed opened; and a stop a
request swallowed ends the fill at the next request, whatever that request answered."""
import asyncio
import base64
import hashlib
import time

import anyio
import httpx
import websockets

from chartsrv.feeds.sdk import FeedPlugin
from chartsrv.gapfill import filling
from chartsrv.models import Bar, Tick
from test_hunt7_yahoostop import swallowed_stop
from test_review_fixes import build_market


def stored_an_hour_ago(tmp_path, monkeypatch, owners: dict):
    """A market whose instruments' stored data stops an hour ago, each supplied by the feed `owners` names."""
    m, _ = build_market(tmp_path, monkeypatch)
    monkeypatch.setattr(m, "history_changed", lambda s: None)
    last = int(time.time() * 1000) - 3_600_000
    for sym in owners:
        m.store.write([Tick(sym, last, 7000.5, 2, 1)],
                      [(sym, Bar(t=last // 60_000 * 60_000, o=7000, h=7001, l=6999, c=7000.5, v=2, n=1))])
    m.trade_owner = dict(owners)
    return m, last


def closes(m, sym: str, after: int) -> list[float]:
    return [r[0] for r in m.store.db.execute("SELECT c FROM bars_1m WHERE symbol=? AND t>?", (sym, after))]


async def silent(reader, writer):
    """A broker's websocket gone silent: it accepts the connection, then answers nothing (not even the close)."""
    req = await reader.readuntil(b"\r\n\r\n")
    key = next(x.split(b":", 1)[1].strip() for x in req.split(b"\r\n") if x.lower().startswith(b"sec-websocket-key"))
    accept = base64.b64encode(hashlib.sha1(key + b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11").digest())
    writer.write(b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                 b"Sec-WebSocket-Accept: " + accept + b"\r\n\r\n")
    await writer.drain()
    await asyncio.sleep(3600)


async def swallowed_as_stopped(feed_task: asyncio.Task) -> int:
    """httpx connecting as the feed stops (stopping.py): anyio's connect_tcp, whose winning attempt cancels its group in
    the pass the stop reaches this request, takes that stop for its own. Returns the task's cancelling() after."""
    async def lands():
        while not feed_task.cancelling():   # the feed stopped: its serve() stops this filler next
            await asyncio.sleep(0)
        tg.cancel_scope.cancel()
    async with anyio.create_task_group() as tg:
        tg.start_soon(lands)
        await anyio.sleep(30)
    return asyncio.current_task().cancelling()


class Broker(FeedPlugin):
    """The feed as it is saved again: connects at once, and its history (its own request, `ask` s) answers."""
    TITLE = "Broker"
    KIND = "futures"
    ask = 0.2
    price = 7000.5

    async def run(self, market):
        self.connected()
        await asyncio.sleep(3600)

    async def history(self, symbol, start, end):
        await asyncio.sleep(self.ask)
        for t in range(start // 60_000 * 60_000 + 60_000, end - 60_000, 60_000):
            yield ("bar", t, self.price, self.price, self.price, self.price, 3)


async def new_feed_filled(m, syms, timeout=4.0):
    """Until the new feed's history is in for `syms`."""
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        if all(m.gapfill.status.get(s, {}).get("note") == "from Broker" for s in syms) and not m.gapfill.windows:
            return True
        await asyncio.sleep(0.05)
    return False


async def stop_all():
    others = asyncio.all_tasks() - {asyncio.current_task()}
    for t in others:
        t.cancel()
    if others:
        await asyncio.wait(others, timeout=3)


# ---- k-mi10-1: saved again while the old filler is slow to end: the new feed's gap history is kept ----------------------
def test_saved_again_while_its_history_hangs_on_a_silent_websocket_keeps_the_new_feeds_history(tmp_path, monkeypatch):
    """The broker's websocket went silent and the user saves the feed to restart it. The old filler, stopped in ES's
    history, spent websockets' close handshake (up to 10 s) ending: set_feed gave up after 5 s and started the new
    feed, then the old serve closed the new feed's NQ window and its history was thrown away. Stopped again, the old
    filler ends at once (websockets still closes that connection itself, at its next keepalive ping)."""
    m, last = stored_an_hour_ago(tmp_path, monkeypatch, {"ES": "broker", "NQ": "broker"})
    port = []

    class Old(Broker):
        async def history(self, symbol, start, end):
            async with websockets.connect(f"ws://127.0.0.1:{port[0]}/") as ws:
                await ws.send(f'{{"chart": "{symbol}", "from": {start}, "to": {end}}}')
                async for msg in ws:
                    yield msg

    async def go():
        srv = await asyncio.start_server(silent, "127.0.0.1", 0)
        port.append(srv.sockets[0].getsockname()[1])
        m._add_feed(Old("broker", {}, ["ES", "NQ"]))
        await asyncio.sleep(0.5)   # its ES history request hangs
        t0 = time.monotonic()
        await m.set_feed("broker", Broker("broker", {}, ["ES", "NQ"]))   # Connections > Save
        took = time.monotonic() - t0
        filled = await new_feed_filled(m, ["ES", "NQ"])
        await stop_all()
        srv.close()
        return took, filled
    took, filled = asyncio.run(go())
    assert took < 1.5 and filled
    assert all(len(closes(m, s, last)) > 50 for s in ("ES", "NQ"))


def test_saved_again_while_its_history_runs_on_after_a_stop_it_swallowed_keeps_the_new_feeds_history(tmp_path, monkeypatch):
    """The round-9 case: the stop swallowed as the old filler's ES request connects (stopping.py), its answer (a night
    of trades, read whole) 6 s later. It was seen only at that first item, after set_feed's 5 s: the old filler and
    the old serve then closed both of the new feed's windows. Stopped again, the request ends then."""
    m, last = stored_an_hour_ago(tmp_path, monkeypatch, {"ES": "broker", "NQ": "broker"})
    feed, swallowed = [], []

    class Old(Broker):
        async def history(self, symbol, start, end):
            swallowed.append(await swallowed_as_stopped(feed[0]))
            await asyncio.sleep(6)
            yield ("bar", start // 60_000 * 60_000 + 60_000, 1, 1, 1, 1, 1)

    async def go():
        m._add_feed(Old("broker", {}, ["ES", "NQ"]))
        feed.append(m.feed_tasks["broker"])
        await asyncio.sleep(0.5)
        t0 = time.monotonic()
        await m.set_feed("broker", Broker("broker", {}, ["ES", "NQ"]))
        took = time.monotonic() - t0
        filled = await new_feed_filled(m, ["ES", "NQ"])
        await stop_all()
        return took, filled
    took, filled = asyncio.run(go())
    assert swallowed in ([1], [])   # (an anyio that raises the stop instead: nothing swallowed, nothing to wait for)
    assert took < 1.5 and filled
    assert all(c == 7000.5 for s in ("ES", "NQ") for c in closes(m, s, last)) and len(closes(m, "NQ", last)) > 50


def test_saved_again_while_its_run_is_slow_to_end_keeps_the_new_feeds_windows(tmp_path, monkeypatch):
    """The old feed's run() takes longer than set_feed's 5 s to end (its broker connection's close handshake), so the new
    feed opened its windows first, NQ's too (put back on Instruments while the old one ran: fill_added). The old
    filler's last word on ES, and the old serve's on NQ, closed those windows and the new feed's history went nowhere.
    They close only windows that are still their own."""
    m, last = stored_an_hour_ago(tmp_path, monkeypatch, {"ES": "broker", "NQ": "broker"})
    m.trade_owner = {"ES": "broker"}   # NQ off Instruments

    class Old(Broker):
        async def run(self, market):
            self.connected()
            try:
                await asyncio.sleep(3600)
            finally:
                await asyncio.sleep(5.5)

        async def history(self, symbol, start, end):   # a request that hangs, and ends at once when stopped
            await asyncio.sleep(3600)
            yield

    class New(Broker):
        ask = 1.0

    async def go():
        old = Old("broker", {}, ["ES", "NQ"])
        m._add_feed(old)
        await asyncio.sleep(0.3)   # its ES history request hangs
        m.trade_owner["NQ"] = "broker"   # NQ put back: its stretch is filled after ES's
        old.fill_added(m, ["NQ"])
        assert set(m.gapfill.windows) == {"ES", "NQ"}
        old_task = m.feed_tasks["broker"]
        await m.set_feed("broker", New("broker", {}, ["ES", "NQ"]))   # gives up on the old one after 5 s
        await asyncio.wait({old_task}, timeout=3)   # its last words, after the new feed opened its windows
        filled = await new_feed_filled(m, ["ES", "NQ"])
        await stop_all()
        return old_task.done(), filled
    old_done, filled = asyncio.run(go())
    assert old_done and filled
    assert all(len(closes(m, s, last)) > 50 for s in ("ES", "NQ"))


def test_an_old_filler_starts_nothing_on_the_new_feeds_windows(tmp_path, monkeypatch):
    """What that old filler, not stopped yet, does once the new feed opened its windows: ES's answer is not written into
    the new ES window, and it asks for nothing more: not NQ (put back while it ran: fill_added) in the new feed's NQ
    window, and no hole opened over a window of the new feed's."""
    m, last = stored_an_hour_ago(tmp_path, monkeypatch, {"ES": "broker", "NQ": "broker"})
    m.trade_owner = {"ES": "broker"}   # NQ off Instruments
    monkeypatch.setattr(m.gapfill, "holes", lambda sym, now: [(last + 60_000, last + 600_000)])
    answer, asked = asyncio.Event(), []

    class Old(Broker):
        async def history(self, symbol, start, end):
            asked.append(symbol)
            await answer.wait()
            async for item in super().history(symbol, start, end):
                yield item

    async def go():
        old = Old("broker", {}, ["ES", "NQ"])
        task = asyncio.create_task(old.serve(m))
        await asyncio.sleep(0.1)   # in ES's history
        m.trade_owner["NQ"] = "broker"
        old.fill_added(m, ["NQ"])   # NQ put back: filled after ES
        now = int(time.time() * 1000)
        new = {s: (m.gapfill.open(s, now), m.gapfill.windows[s])[1] for s in ("ES", "NQ")}   # the new feed's
        m.gapfill.queue["ES"] = queue = [(last + 60_000, last + 120_000)]
        answer.set()
        filler = old._filler
        await asyncio.wait({filler}, timeout=2)
        task.cancel()
        await asyncio.wait({task}, timeout=2)
        return filler.done(), new, queue
    filler_done, new, queue = asyncio.run(go())
    assert filler_done and asked == ["ES"]
    assert m.gapfill.windows == new and m.gapfill.queue["ES"] is queue and not m.gapfill.windows["ES"].rows


def test_an_old_filler_in_its_holes_opens_no_next_hole_over_the_new_feeds_window(tmp_path, monkeypatch):
    """Saved again while the old filler works through ES's holes, its run() slow to end (the filler is stopped only
    once it has): the new feed opens ES's window meanwhile. The old filler's hole answer is dropped, and it opens no
    next hole over that window, so the new feed's history of ES's stretch is stored."""
    m, last = stored_an_hour_ago(tmp_path, monkeypatch, {"ES": "broker"})
    holes = [(last - (k + 2) * 3_600_000, last - (k + 2) * 3_600_000 + 600_000) for k in range(10)]
    monkeypatch.setattr(m.gapfill, "holes", lambda sym, now: list(holes))
    answer, closed, asked = asyncio.Event(), asyncio.Event(), []

    class Old(Broker):
        TITLE = "Old broker"   # (its answers' notes: not the new feed's)

        async def run(self, market):
            self.connected()
            try:
                await asyncio.sleep(3600)
            finally:
                await closed.wait()   # its broker connection's close handshake

        async def history(self, symbol, start, end):
            if end > last:   # the stretch at the end: the silent broker has none
                return
            asked.append((start, end))
            await answer.wait()
            for t in range(start // 60_000 * 60_000 + 60_000, end - 60_000, 60_000):
                yield ("bar", t, 6000.0, 6000.0, 6000.0, 6000.0, 1)

    async def go():
        old_task = asyncio.create_task(Old("broker", {}, ["ES"]).serve(m))
        for _ in range(100):   # until it is in its first hole
            if asked:
                break
            await asyncio.sleep(0.05)
        old_task.cancel()   # saved again: its run() still ending, its filler not stopped yet
        new_task = asyncio.create_task(Broker("broker", {}, ["ES"]).serve(m))
        await asyncio.sleep(0.05)   # the new feed's ES window open
        answer.set()   # the old hole's answer comes
        filled = await new_feed_filled(m, ["ES"])
        closed.set()
        await asyncio.wait({old_task}, timeout=2)
        new_task.cancel()
        await asyncio.wait({new_task}, timeout=2)
        return filled, old_task.done()
    filled, old_done = asyncio.run(go())
    assert old_done and len(asked) == 1
    assert filled and len(closes(m, "ES", last)) > 50 and all(c == 7000.5 for c in closes(m, "ES", last))


# ---- k-mi10-2: a stop waits 2 s at most for the filler ------------------------------------------------------------------
class Retry(FeedPlugin):
    """A rate-limited API: on an HTTP error it waits and asks again, and it answers 429 for good. The stop is swallowed
    as its first request connects. `tries`: its requests so far."""
    TITLE = "Retry"
    KIND = "futures"
    tries = 0

    async def run(self, market):
        self.connected()
        await asyncio.sleep(3600)

    async def history(self, symbol, start, end):
        req = httpx.Request("GET", "https://api.example.com/history")
        await swallowed_as_stopped(self.task)
        while True:
            try:
                self.tries += 1
                raise httpx.HTTPStatusError("429", request=req, response=httpx.Response(429, request=req))
            except httpx.HTTPError:
                await asyncio.sleep(1)
        yield


async def tries_after(feed: Retry) -> int:
    """Its requests in the 1.2 s after (it asks again every second): none once its filler has ended."""
    n = feed.tries
    await asyncio.sleep(1.2)
    return feed.tries - n


def test_a_stop_ends_while_its_history_retries_after_a_stop_it_swallowed(tmp_path, monkeypatch):
    """Market.stop (a restart in place, the container stopping) waited for good on the filler: nothing it does ever
    raises the stop it swallowed. Stopped again, it ends, and so does the stop (not only by the 2 s bound: the
    filler asks the API no more)."""
    m, _ = stored_an_hour_ago(tmp_path, monkeypatch, {"ES": "retry"})

    async def go():
        feed = Retry("retry", {}, ["ES"])
        m._add_feed(feed)
        feed.task = m.feed_tasks["retry"]
        await asyncio.sleep(0.5)
        stop = asyncio.create_task(m.stop())
        done, _ = await asyncio.wait({stop}, timeout=3)
        tries = await tries_after(feed)
        await stop_all()
        return bool(done), tries
    assert asyncio.run(go()) == (True, 0)


def test_turned_off_while_its_history_retries_its_alerts_are_not_held(tmp_path, monkeypatch):
    """Turned off in Connections: set_feed gave up after 5 s, and the old serve, still waiting on its filler, never
    closed its windows, so ES's gap, indicator, study and AMT alerts were held (gapfill.filling) for as long. Nor
    does the filler go on asking the API beside the next feed."""
    m, _ = stored_an_hour_ago(tmp_path, monkeypatch, {"ES": "retry"})

    async def go():
        feed = Retry("retry", {}, ["ES"])
        m._add_feed(feed)
        feed.task = m.feed_tasks["retry"]
        await asyncio.sleep(0.5)
        assert filling(m, "ES")
        m.trade_owner = {}   # NinjaTrader supplies it again
        t0 = time.monotonic()
        await m.set_feed("retry", None)
        took = time.monotonic() - t0
        held = filling(m, "ES")
        tries = await tries_after(feed)
        await stop_all()
        return took, held, tries
    took, held, tries = asyncio.run(go())
    assert took < 3 and not held and tries == 0


def test_a_stop_ends_at_once_with_a_history_on_a_silent_websocket(tmp_path, monkeypatch):
    """A history waiting on a silent websocket spent websockets' close handshake (10 s) ending once stopped: a stop took
    docker's whole 10 s grace, so its final flush could be killed with it."""
    m, _ = stored_an_hour_ago(tmp_path, monkeypatch, {"ES": "broker"})
    port = []

    class Old(Broker):
        async def history(self, symbol, start, end):
            async with websockets.connect(f"ws://127.0.0.1:{port[0]}/") as ws:
                async for msg in ws:
                    yield msg

    async def go():
        srv = await asyncio.start_server(silent, "127.0.0.1", 0)
        port.append(srv.sockets[0].getsockname()[1])
        m._add_feed(Old("broker", {}, ["ES"]))
        await asyncio.sleep(0.5)
        t0 = time.monotonic()
        stop = asyncio.create_task(m.stop())
        await asyncio.wait({stop}, timeout=3)
        took = time.monotonic() - t0
        await stop_all()
        srv.close()
        return took
    assert asyncio.run(go()) < 1.5


def test_a_chained_fill_whose_earlier_one_swallowed_the_stop_ends_too(tmp_path, monkeypatch):
    """NQ put back while ES's fill runs: its fill (fill_added) waits for ES's, and the feed's stop reaches ES's through
    it. ES's request swallowed that stop and its history retries: stopped again through the chain, both end, and both
    windows are closed."""
    m, _ = stored_an_hour_ago(tmp_path, monkeypatch, {"ES": "retry", "NQ": "retry"})
    m.trade_owner = {"ES": "retry"}

    async def go():
        feed = Retry("retry", {}, ["ES", "NQ"])
        m._add_feed(feed)
        feed.task = m.feed_tasks["retry"]
        await asyncio.sleep(0.5)
        m.trade_owner["NQ"] = "retry"
        first = feed._filler
        feed.fill_added(m, ["NQ"])
        chained = feed._filler
        assert chained is not first and set(m.gapfill.windows) == {"ES", "NQ"}
        stop = asyncio.create_task(m.stop())
        done, _ = await asyncio.wait({stop}, timeout=3)
        got = bool(done), first.done(), chained.done(), set(m.gapfill.windows), await tries_after(feed)
        await stop_all()   # (it ends every task: what the stop did is read before)
        return got
    assert asyncio.run(go()) == (True, True, True, set(), 0)


# ---- k-mi10-3: a stop swallowed in a request that answered nothing ends the fill at the next request -------------------
def test_a_stop_swallowed_in_a_failed_history_request_asks_no_other_instrument(tmp_path, monkeypatch):
    """The broker's history service is down. The stop is swallowed as ES's request connects, and the request fails: no
    item came, so the per-item check never ran, and the filler went on to ask NQ, CL and GC (and their holes), each a
    request the stop waited on (past set_feed's 5 s, on the new feed's windows)."""
    m, _ = stored_an_hour_ago(tmp_path, monkeypatch, {s: "hist" for s in ("ES", "NQ", "CL", "GC")})
    asked = []

    class Down(FeedPlugin):
        TITLE = "Hist"
        KIND = "futures"

        async def history(self, symbol, start, end):
            asked.append(symbol)
            if len(asked) == 1:
                await swallowed_stop()
            raise httpx.ReadTimeout("timed out")
            yield

    async def go():
        feed = Down("hist", {}, ["ES", "NQ", "CL", "GC"])
        task = asyncio.create_task(feed.fill_gaps(m))
        await asyncio.wait({task}, timeout=3)
        return task.done() and task.cancelled()
    assert asyncio.run(go()) and asked == ["ES"]
    assert m.gapfill.status["ES"]["kind"] == "error" and "ES" not in m.gapfill.windows
