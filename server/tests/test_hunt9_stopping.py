"""Round 9 of the bug hunt, stops that httpx swallows (stopping.py): the earnings refresh that gives up on its third
day still sees the stop, and a futures plugin's gap filler ends with its feed and with a stop its history swallowed."""
import asyncio
from types import SimpleNamespace

from chartsrv.feeds.sdk import FeedPlugin
from test_hunt7_yahoostop import Stopping, ends_at_once, market, swallowed_stop


def test_earnings_given_up_on_its_third_day_still_ends_at_once(tmp_path, monkeypatch):
    # every day turned away (404); the stop is swallowed in the third day's request, the one that makes it give up
    m = market(tmp_path, monkeypatch, Stopping(at=3), CHART_SYMBOLS_ONLINE=1)
    assert ends_at_once(m.earnings.run, timeout=5)   # a day a second: the third request comes after 2 s


class Hist(FeedPlugin):
    TITLE = "Hist"
    KIND = "futures"

    def __init__(self, history):
        super().__init__("hist", {}, ["ES"])
        self._history = history

    def history(self, symbol, start, end):
        return self._history()


class Gaps:
    def __init__(self):
        self.done_calls, self.minutes, self.windows = [], 0, {}

    def minute(self, *a):
        self.minutes += 1

    def tick(self, *a):
        pass

    def done(self, *a):
        self.done_calls.append(a)


def test_a_gap_fill_whose_history_swallowed_the_stop_ends_at_once():
    async def history():   # a REST history: the stop lands as it connects, then the minutes come on and on
        await swallowed_stop()
        for i in range(10_000):
            await asyncio.sleep(0.01)
            yield ("bar", i * 60_000, 1, 1, 1, 1, 1)
    gf = Gaps()
    assert ends_at_once(lambda: Hist(history)._fill(gf, "ES", (0, 1)))
    assert gf.minutes <= 1 and gf.done_calls[-1][1] == "error"


def test_a_feed_stopped_waits_for_its_gap_filler_to_end():
    """A feed saved again: the old one's gap filler must have ended (its window closed) before the new feed opens its
    own, or the old filler's last word closes the new window."""
    order = []

    class Slow(Hist):
        async def run(self, market):
            self.connected()
            await asyncio.sleep(60)

        async def _fill_when_connected(self, market):
            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                for _ in range(5):   # as anyio's connect does: a few passes to unwind
                    await asyncio.sleep(0)
                order.append("filler ended")
                raise

    async def go():
        feed = Slow(None)
        t = asyncio.create_task(feed.serve(SimpleNamespace(instruments={}, gapfill=None)))
        await asyncio.sleep(0.05)
        t.cancel()
        await asyncio.gather(t, return_exceptions=True)
        order.append("feed stopped")
    asyncio.run(go())
    assert order == ["filler ended", "feed stopped"]
