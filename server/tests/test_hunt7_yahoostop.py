"""Bug hunt round 7, stops: a stop (Market.stop's task.cancel(): a restart in place, the container stopping, a
connection saved) that lands in the same loop pass as one of httpx's connections is swallowed by anyio's connect_tcp,
and a background loop then slept out its wait (Yahoo's backoff) and polled on, the stop waiting for it for good. Each
loop that asks the network in its own task now ends at its next wait instead (chartsrv/stopping.py)."""
import asyncio
import json
from types import SimpleNamespace

import anyio
import httpx
import pytest

from chartsrv import market as mk, stopping, updates
from chartsrv.feeds.alpaca import AlpacaQuotes
from chartsrv.feeds.coinbase import CoinbaseQuotes
from chartsrv.feeds.fred import FredQuotes
from chartsrv.feeds.plugins.finnhub import FinnhubQuotes
from chartsrv.feeds.yahoo import YahooQuotes
from test_review_fixes import build_market


async def swallowed_stop(stop=None):
    """What httpcore's connect does on asyncio when the stop lands in the same loop pass as its connection:
    anyio.connect_tcp's task group, whose attempt that connected cancels the group, takes the task's stop for its own
    cancel and swallows it. The task runs on, marked as cancelling. stop: the task stopped (this one's, or the loop's
    whose gather passes the stop on to this one)."""
    task = asyncio.current_task()

    async def connected():
        tg.cancel_scope.cancel()   # this address connected: the other attempts stop
        (stop or task).cancel()    # and the stop, in the same pass

    async with anyio.create_task_group() as tg:
        tg.start_soon(connected)
        await anyio.sleep(5)


def _anyio_swallows() -> bool:
    """Whether this anyio swallows the stop as above (before 4.16.0); a newer one raises it, so the request itself ends
    the loop at once and it never reaches the error or answer counted below. Either way the loop ends at once."""
    async def go():
        try:
            await swallowed_stop()
        except asyncio.CancelledError:
            return False
        return True
    return asyncio.run(go())


SWALLOWS = _anyio_swallows()


class Stopping(httpx.AsyncBaseTransport):
    """Its first request (or request `at`) is stopped as above, then refused (as a proxy refuses Yahoo: 403 to
    CONNECT), or answered (answer); the others are answered."""
    def __init__(self, answer=None, refuse=True, at=1):
        self.answer, self.refuse, self.at, self.calls = answer or (lambda req: httpx.Response(404)), refuse, at, 0
        self.stop = None   # the task stopped: the one asking

    async def handle_async_request(self, request):
        self.calls += 1
        if self.calls == self.at:
            await swallowed_stop(self.stop)
            if self.refuse:
                raise httpx.ProxyError("403 Forbidden", request=request)
        return self.answer(request)


def ends_at_once(start, timeout=1.0) -> bool:
    """Runs start()'s loop (made in the running loop) until the stop its first request swallows: whether it ended,
    cancelled, within `timeout`."""
    async def go():
        task = asyncio.create_task(start())
        done, _ = await asyncio.wait({task}, timeout=timeout)
        if not done:   # sleeping out its wait: stopped again, so the test ends
            task.cancel()
            await asyncio.wait({task}, timeout=2)
        return bool(done) and task.cancelled()
    return asyncio.run(go())


def market(tmp_path, monkeypatch, transport=None, **env):
    m, _ = build_market(tmp_path, monkeypatch, http=httpx.AsyncClient(transport=transport or Stopping()), **env)
    return m


def test_the_check_reads_the_tasks_stop():
    async def go():
        stopping.check()   # not asked to stop: nothing
        try:
            await swallowed_stop()
        except asyncio.CancelledError:
            return "raised"   # (an anyio that raises the stop: the loops end on their own)
        assert asyncio.current_task().cancelling() == 1
        with pytest.raises(asyncio.CancelledError):
            await stopping.pause(60)
        return "swallowed"
    assert asyncio.run(go()) in ("swallowed", "raised")


async def proxy(reader, writer):
    """A proxy that refuses every CONNECT, as this container's refuses Yahoo."""
    try:
        await reader.readuntil(b"\r\n\r\n")
        writer.write(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n")
        await writer.drain()
    except (asyncio.IncompleteReadError, ConnectionError):
        pass
    finally:
        writer.close()


def test_yahoo_stopped_as_its_connection_lands_ends_at_once(tmp_path, monkeypatch):
    """The real thing: Yahoo's poll through httpx, httpcore and anyio to a proxy on this PC, stopped at each loop pass
    of its start in turn (one of them is the pass its connection lands in). Each run ends at once; it slept out its
    30 s backoff and polled on."""
    m, _ = build_market(tmp_path, monkeypatch)
    m.watch_keys = {"AAPL", "SPX"}

    async def go():
        srv = await asyncio.start_server(proxy, "127.0.0.1", 0)
        url = f"http://127.0.0.1:{srv.sockets[0].getsockname()[1]}"
        for name in ("HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy"):
            monkeypatch.setenv(name, url)
        loop, hung = asyncio.get_running_loop(), []
        for k in range(40):
            y = YahooQuotes(SimpleNamespace(yahoo_poll_s=15, config_dir=tmp_path))
            task = asyncio.create_task(y.run(m))

            def stop_after(n, task=task):
                loop.call_soon(stop_after, n - 1) if n else task.cancel()
            stop_after(k)
            done, _ = await asyncio.wait({task}, timeout=1)
            if not done:
                hung.append(k)
                task.cancel()
                await asyncio.wait({task}, timeout=2)
        srv.close()
        return hung
    assert asyncio.run(go()) == []


@pytest.mark.parametrize("refuse", [True, False])
def test_yahoo_ends_at_once(tmp_path, monkeypatch, refuse):
    """Refused (its 30 s backoff), or answered (15 s to its next poll): it ends at the wait."""
    m = market(tmp_path, monkeypatch)
    m.watch_keys = {"AAPL"}
    spark = {"spark": {"result": [{"symbol": "AAPL", "response": [{"meta": {"regularMarketPrice": 230.5,
                                                                            "chartPreviousClose": 228.0}}]}]}}
    y = YahooQuotes(SimpleNamespace(yahoo_poll_s=15, config_dir=tmp_path),
                    transport=Stopping(lambda req: httpx.Response(200, json=spark), refuse=refuse))
    assert ends_at_once(lambda: y.run(m))
    assert not SWALLOWS or y.stats["errors" if refuse else "ok"] == 1


def test_yahoo_one_symbol_at_a_time_ends_at_once(tmp_path, monkeypatch):
    """Asked one symbol at a time, each request in a task of its own (gather): one that swallows the stop gather passed
    on to it still ends the poll, as gather raises the stop it was given."""
    m = market(tmp_path, monkeypatch)
    m.watch_keys = {"AAPL", "MSFT", "SPX"}
    t = Stopping()
    y = YahooQuotes(SimpleNamespace(yahoo_poll_s=15, config_dir=tmp_path), transport=t)
    y.stats["mode"] = "chart"

    async def start():
        t.stop = asyncio.current_task()
        await y.run(m)
    assert ends_at_once(start)


def test_alpaca_ends_at_once(tmp_path, monkeypatch):
    m = market(tmp_path, monkeypatch)
    m.watch_keys = {"AAPL"}
    a = AlpacaQuotes(SimpleNamespace(alpaca_key_id="k", alpaca_secret="s", alpaca_feed="iex"), transport=Stopping())
    assert ends_at_once(lambda: a.run(m))
    assert not SWALLOWS or a.stats["errors"] == 1


def test_fred_ends_at_once(tmp_path, monkeypatch):
    m = market(tmp_path, monkeypatch)
    m.watch_keys = {"FRED:DGS10"}
    f = FredQuotes()
    assert ends_at_once(lambda: f.run(m))   # its next round in 15 minutes
    assert not SWALLOWS or f.stats["errors"] == 1


def test_coinbase_ends_at_once(tmp_path, monkeypatch):
    """Its product list asked in the stop: it no longer goes on to subscribe and wait on the socket."""
    m = market(tmp_path, monkeypatch)
    m.watch_keys = {"BTCUSD"}
    subscribed = []

    class Socket:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def send(self, msg):
            subscribed.append(json.loads(msg))

        def __aiter__(self):
            return self

        async def __anext__(self):
            await asyncio.Event().wait()   # no trades

    cb = CoinbaseQuotes(connect=Socket, http=m.http)
    assert ends_at_once(lambda: cb.run(m))
    assert not subscribed

    cb = CoinbaseQuotes(connect=Socket, http=httpx.AsyncClient(transport=Stopping()))   # the socket's reader
    assert ends_at_once(lambda: cb._session(m, {"BTC-USD": ["BTCUSD"]}))   # stopped in its day opens: never connects
    assert not subscribed


def test_finnhub_ends_at_once(tmp_path, monkeypatch):
    """Its previous closes, 1.1 s apart: the next one is not asked."""
    m = market(tmp_path, monkeypatch)
    m.set_watchlists({"lists": [{"name": "a", "items": ["AAPL", "MSFT", "NVDA"]}]})
    f = FinnhubQuotes("finnhub", {"api_key": "k"}, [])
    assert ends_at_once(lambda: f.run(m))
    assert m.http._transport.calls == 1


@pytest.mark.parametrize("at", [1, 2])
def test_symbol_refresh_ends_at_once(tmp_path, monkeypatch, at):
    """Its next round is an hour off. Stopped in Nasdaq's list (one request, turned away), Coinbase's after it is not
    asked; stopped in Coinbase's, the last, it ends at the wait."""
    m = market(tmp_path, monkeypatch, Stopping(at=at), CHART_SYMBOLS_ONLINE=1)
    assert ends_at_once(m.symbols.run)
    assert m.http._transport.calls == at


def test_calendar_and_earnings_end_at_once(tmp_path, monkeypatch):
    m = market(tmp_path, monkeypatch, CHART_SYMBOLS_ONLINE=1)
    assert ends_at_once(m.calendar.run)   # 20 s to its next look
    m = market(tmp_path, monkeypatch, CHART_SYMBOLS_ONLINE=1)
    assert ends_at_once(m.earnings.run)   # one day a second, two weeks of them
    assert m.http._transport.calls == 1


def test_update_check_ends_at_once(tmp_path, monkeypatch):
    monkeypatch.setattr(updates, "FIRST_S", 0)
    m = market(tmp_path, monkeypatch, CHART_COMMIT="a" * 40)
    assert ends_at_once(m.updates.run)   # 6 hours to the next check
    assert not SWALLOWS or "GitHub did not answer" in m.updates.state["error"]


def test_daily_levels_link_ends_at_once(tmp_path, monkeypatch):
    m = market(tmp_path, monkeypatch)
    monkeypatch.setattr(m.daily, "spec", lambda: "https://example.com/levels/{root}.txt")
    assert ends_at_once(m.daily.run)   # its next look in 5 s


def test_watchdog_heartbeat_ends_at_once(tmp_path, monkeypatch):
    monkeypatch.setattr(mk, "WATCHDOG_S", 0.05)
    m = market(tmp_path, monkeypatch, CHART_HEARTBEAT_URL="https://hc-ping.com/x")
    assert ends_at_once(m._watchdog_loop)   # it pinged again and again
    assert m.http._transport.calls == 1 and (not SWALLOWS or m.heartbeat_last[0] is False)


def test_a_feed_that_fails_in_the_stop_is_not_restarted(tmp_path, monkeypatch):
    """A feed (a plugin's) that raises after the stop its request swallowed: Market._run_feed no longer waits 10 s and
    starts it again."""
    m, _ = build_market(tmp_path, monkeypatch)
    runs = []

    async def run(market):
        runs.append(1)
        await swallowed_stop()
        raise ConnectionError("the proxy refused")
    feed = SimpleNamespace(name="x", run=run)
    assert ends_at_once(lambda: m._run_feed(feed))
    assert runs == [1]
