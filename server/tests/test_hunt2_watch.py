"""Second bug-hunt fixes in Market.watch_chart: a free-data chart that switches between a stock and the future with
the same ticker (KC, Kingsoft Cloud, and KC1!, coffee) never shows the other's quote: the two have their own quote
keys, and IBKR quotes the stock while one is charted."""
import asyncio
import time

import httpx

from chartsrv.feeds import ibkr
from test_ext import yahoo_doc
from test_hunt_quotes import IB
from test_review_fixes import build_market


def ibkr_market(tmp_path, monkeypatch):
    """A market with IBKR's quote feed in it, and a step that re-syncs it the way its run loop does: only once
    watch_changed has set its changed event. Returns the market, that step (it gives the subscribed keys) and the
    number of re-syncs so far."""
    http = httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(200, json=yahoo_doc([1_792_000_000], [380.0]))))
    m, _ = build_market(tmp_path, monkeypatch, http=http)
    m.set_watchlists({"lists": [{"name": "a", "items": ["AAPL"]}]})
    feed, ib, subs, syncs = ibkr.IBKRQuotes(None), IB(), {}, [0]
    m.feed_objs.append(feed)
    feed.changed.clear()

    def chart(sym):
        asyncio.run(m.ext.bars(sym, "1D"))
        if feed.changed.is_set():
            feed.changed.clear()
            syncs[0] += 1
            asyncio.run(feed._sync(ib, m, subs, {}))
        return set(subs)
    return m, chart, syncs


def test_a_pane_switched_from_the_stock_to_the_future_drops_the_stock(tmp_path, monkeypatch):
    m, chart, syncs = ibkr_market(tmp_path, monkeypatch)
    assert "KC" in chart("KC")         # Kingsoft Cloud, from the search: STK KC at IBKR
    assert "KC1!" not in chart("KC1!") and "KC1!" in m.watch_keys   # then coffee: its own key, not Kingsoft's price
    n = syncs[0]
    for _ in range(3):                 # its polls: nothing more to move
        chart("KC1!")
    assert syncs[0] == n
    assert "KC" in chart("KC")         # and back to the stock
    m.chart_keys["KC"] = time.monotonic() - 1   # its chart closed long ago, nothing pruned it since
    assert "KC" not in chart("KC1!")


def test_a_pane_switched_from_the_future_to_the_stock_quotes_the_stock(tmp_path, monkeypatch):
    m, chart, syncs = ibkr_market(tmp_path, monkeypatch)
    assert "KC" not in chart("KC1!")   # coffee
    assert "KC" in chart("KC")         # then Kingsoft Cloud: IBKR quotes the stock, not Yahoo's KC=F
    n = syncs[0]
    for _ in range(3):
        assert "KC" in chart("KC")
    assert syncs[0] == n
    assert "KC" in chart("KC1!") and {"KC", "KC1!"} <= m.watch_keys   # both charted: each quoted as itself
    m.chart_keys["KC1!"] = time.monotonic() - 1   # coffee's chart closed hours ago
    assert "KC" in chart("KC") and "KC1!" not in m.watch_keys
    assert {"AAPL", "KC", "ORCL"} <= chart("ORCL")
