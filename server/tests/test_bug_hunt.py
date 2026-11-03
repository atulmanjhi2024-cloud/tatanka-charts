"""Regressions from the October 2026 bug hunt: subscription races in Market, the first calendar bar, live sweeps
grouped as the stored ones."""
import asyncio
from datetime import datetime

from chartsrv import market as market_mod
from chartsrv import sessions
from chartsrv.bars import CALENDAR, make_series, parse_spec
from chartsrv.market import DAY_MS, Client, build_history
from chartsrv.models import Bar, Tick
from chartsrv.store import Store, big_trades, sweeps
from test_review_fixes import build_market


def _warm(m):
    now = market_mod.now_ms()
    for i in range(2000):
        m.on_tick(Tick("ES", now - 120_000 + i, 7000 + i % 7 * 0.25, 1, 1))


def _types(c):
    out = []
    while not c.outbox.empty():
        out.append(c.outbox.get_nowait()["type"])
    return out


def test_amt_subscription_made_while_the_chart_loads_stays(tmp_path, monkeypatch):
    """The web sends sub id=N, then amt id=N: the chart finishing its load must not drop the AMT subscription."""
    sessions.set_calendar({"days": []})

    async def go():
        m, _ = build_market(tmp_path, monkeypatch)
        _warm(m)
        await m.amt.entry("ES", 5)                       # warm: another chart, or AMT alerts on
        c = Client()
        await asyncio.gather(m.subscribe(c, 7, "ES", "1D"), m.amt.subscribe(c, 7, "ES", 5))
        assert 7 in c.subs and 7 in c.amt_entries and (c, 7) in m.amt.entries[("ES", 5)].subs
    asyncio.run(go())


def test_disconnect_while_loading_drops_the_amt_subscription(tmp_path, monkeypatch):
    sessions.set_calendar({"days": []})

    async def go():
        m, _ = build_market(tmp_path, monkeypatch)
        _warm(m)
        await m.amt.entry("ES", 5)
        c = m.connect()
        t1 = asyncio.create_task(m.subscribe(c, 3, "ES", "1D"))
        await m.amt.subscribe(c, 3, "ES", 5)
        m.disconnect(c)                                  # the tab closed during the load
        await t1
        assert not m.amt.entries[("ES", 5)].subs and not c.subs
    asyncio.run(go())


def test_gap_fill_during_a_load_reloads_the_chart(tmp_path, monkeypatch):
    sessions.set_calendar({"days": []})

    async def go():
        m, _ = build_market(tmp_path, monkeypatch)
        _warm(m)
        c = Client()
        sub = asyncio.create_task(m.subscribe(c, 1, "ES", "1D"))
        await asyncio.sleep(0)                           # loading in a worker thread
        m.history_changed("ES")                          # a gap fill finished meanwhile
        await sub
        assert _types(c) == ["reload"] and 1 not in c.subs   # the browser subscribes again, to the new history
    asyncio.run(go())


def test_first_calendar_bar_is_whole(tmp_path):
    """The window starts mid-session: that first, partial daily bar is left out rather than drawn short."""
    st = Store(tmp_path / "c.db")
    split = int(datetime(2026, 10, 16, 12, 0, tzinfo=sessions.CT).timestamp() * 1000)   # a Friday noon CT
    bars, t = [], split - 410 * DAY_MS
    while t < split:
        if sessions.is_open(t):
            bars.append(Bar(t=t, o=100, h=101, l=99, c=100, v=1))
        t += 60_000
    st.insert_bars_if_missing("ES", bars)
    s = make_series(parse_spec("1D"), 0.25, 2)
    build_history(tmp_path / "c.db", s, "ES", parse_spec("1D"), 0, split, split)
    assert s.bars[0].t >= split - CALENDAR["1D"] * DAY_MS and s.bars[0].v == s.bars[1].v == 1380


def test_live_sweeps_group_as_the_stored_trades(tmp_path):
    st = Store(tmp_path / "c.db")
    t0 = 1_760_000_000_000
    ticks = [Tick("ES", t0, 7000.0, 5, 1), Tick("ES", t0, 6999.75, 2, -1), Tick("ES", t0, 7000.25, 5, 1),
             Tick("ES", t0 + 1, 7000.0, 1, 1)]
    st.write(ticks, [])
    assert sweeps(ticks) == big_trades(tmp_path / "c.db", "ES", t0, t0 + 2, 1)
    assert sweeps(ticks)[0] == [t0, 7000.125, 10, 1, 2]


def test_bad_input_is_a_400_not_a_500(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from chartsrv.app import app
    monkeypatch.setenv("CHART_DB", str(tmp_path / "chart.db"))
    monkeypatch.setenv("CHART_LEVELS_DIR", str(tmp_path))
    monkeypatch.setenv("CHART_FEED", "none")
    with TestClient(app) as c:
        nan = '{"symbol": "ES", "kind": "price", "price": NaN}'
        assert c.post("/api/alerts", content=nan, headers={"content-type": "application/json"}).status_code == 422
        assert c.post("/api/alerts", json={"symbol": "ES", "kind": "ind", "a": {"ind": ["ema"]}, "b": {"value": 1}}).status_code == 400
        assert c.put("/api/drawings/ES/d1", json={"type": ["trend"], "pts": []}).status_code in (400, 404, 422)
        assert c.put("/api/indicators/nosuch/ES", json={}).status_code == 400
        ws = '{"name": "x", "data": {"v": NaN}}'
        assert c.put("/api/workspaces/w1", content=ws, headers={"content-type": "application/json"}).status_code == 400
        assert c.put("/api/workspaces/w1%0A", json={"name": "x", "data": {}}).status_code == 400
        assert c.get("/api/workspaces", params={"full": True}).status_code == 200
        assert c.get("/api/alert-events", params={"limit": -1}).status_code == 200
        r = c.post("/api/footprint", json={"symbol": "ES", "starts": [0], "end": 9 * DAY_MS})
        assert r.status_code == 400
