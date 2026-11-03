"""Time & sales and big-trade bubbles: fills at the same millisecond and side are one trade (a sweep)."""
import asyncio

from chartsrv.models import Tick
from chartsrv.store import big_trades, sweeps
from test_review_fixes import build_market


def test_stored_trades_grouped_into_sweeps(tmp_path, monkeypatch):
    m, _ = build_market(tmp_path, monkeypatch)
    t = 1_760_000_000_000
    ticks = [Tick("ES", t, 6900.0, 10, 1), Tick("ES", t, 6900.25, 30, 1), Tick("ES", t, 6900.0, 5, -1),
             Tick("ES", t + 1, 6899.75, 2, -1), Tick("ES", t + 2, 6899.5, 60, -1), Tick("NQ", t, 25000.0, 99, 1)]
    m.store.write(ticks, [])
    rows = big_trades(m.store.path, "ES", t, t + 10, 1)
    assert [(r[0], r[2], r[3], r[4]) for r in rows] == [(t, 40, 1, 2), (t, 5, -1, 1), (t + 1, 2, -1, 1), (t + 2, 60, -1, 1)]
    assert abs(rows[0][1] - (6900.0 * 10 + 6900.25 * 30) / 40) < 1e-9
    assert [r[2] for r in big_trades(m.store.path, "ES", t, t + 10, 40)] == [40, 60]
    assert [r[0] for r in big_trades(m.store.path, "ES", t, t + 10, 1, limit=2, newest_first=True)] == [t + 2, t + 1]
    assert [s[2] for s in sweeps(ticks[:5])] == [40, 5, 2, 60]


def test_live_tape_goes_to_the_clients_that_asked(tmp_path, monkeypatch):
    m, _ = build_market(tmp_path, monkeypatch)
    a, b = m.connect(), m.connect()
    m.set_tape(a, "ES", 20)
    m.set_tape(b, "NQ", 1)
    t = 1_760_000_000_000
    for tk in [Tick("ES", t, 6900.0, 15, 1), Tick("ES", t, 6900.25, 10, 1), Tick("ES", t + 5, 6900.0, 3, -1)]:
        m.on_tick(tk, live=False)

    async def one_flush():
        task = asyncio.create_task(m._client_loop())
        await asyncio.sleep(0.2)
        task.cancel()
    asyncio.run(one_flush())
    got = []
    while not a.outbox.empty():
        msg = a.outbox.get_nowait()
        if msg["type"] == "trades":
            got += msg["trades"]
    assert [(r[2], r[3]) for r in got] == [(25, 1)]
    assert all(b.outbox.get_nowait()["type"] != "trades" for _ in range(b.outbox.qsize()))
    m.disconnect(a)
    assert "ES" not in m.tape_subs
