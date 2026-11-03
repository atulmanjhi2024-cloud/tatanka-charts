"""Footprint: trades split by aggressor into each bar's price rows, and the endpoint agreeing with the chart."""
import time

import pytest

from chartsrv.footprint import compute
from chartsrv.models import Tick


def tk(ts, price, size, side):
    return Tick("ES", ts, price, size, side)


def test_rows_split_by_aggressor_and_tick_rule():
    ticks = [tk(100, 7400.0, 2, 1), tk(150, 7400.25, 3, -1), tk(160, 7400.25, 1, 0),   # 0: same price, sell before
             tk(200, 7400.5, 4, 0),                                                     # uptick: a buy
             tk(250, 7400.0, 5, 0),                                                     # downtick: a sell
             tk(300, 7399.75, 7, 1)]                                                    # at `end`: left out
    bars = compute(ticks, [100, 200], 300, 0.25)
    assert bars[0] == {"t": 100, "rows": [[29600, 0, 2], [29601, 4, 0]]}
    assert bars[1] == {"t": 200, "rows": [[29600, 5, 0], [29602, 0, 4]]}
    assert compute([tk(50, 7400.0, 1, 1)], [100], 200, 0.25) == [{"t": 100, "rows": []}]   # before the first bar


@pytest.fixture
def client(tmp_path, monkeypatch, in_session):
    monkeypatch.setenv("CHART_DB", str(tmp_path / "chart.db"))
    monkeypatch.setenv("CHART_LEVELS_DIR", str(tmp_path))
    monkeypatch.setenv("CHART_FEED", "sim")
    monkeypatch.setenv("CHART_SIM_WARMUP_MIN", "30")
    from fastapi.testclient import TestClient
    from chartsrv.app import app
    with TestClient(app) as c:
        yield c


def test_footprint_volume_matches_the_chart_bars(client):
    time.sleep(0.5)                                         # let the sim back-fill its trades
    with client.websocket_connect("/ws") as ws:
        ws.send_json({"op": "sub", "id": 1, "symbol": "ES", "spec": "1m"})
        for _ in range(500):
            m = ws.receive_json()
            if m["type"] == "history":
                break
    bars = m["bars"][:-1]                                   # closed bars: the forming one is still moving
    assert len(bars) >= 10
    starts = [b["t"] for b in bars]
    r = client.post("/api/footprint", json={"symbol": "es", "starts": starts, "end": starts[-1] + 60_000})
    assert r.status_code == 200 and r.json()["tick"] == 0.25
    got = {b["t"]: b for b in r.json()["bars"]}
    for b in bars:
        rows = got[b["t"]]["rows"]
        assert sum(bid + ask for _, bid, ask in rows) == b["v"]
        assert sum(ask - bid for _, bid, ask in rows) == b["d"]      # the sim always sends a side
        assert min(p for p, *_ in rows) * 0.25 == b["l"] and max(p for p, *_ in rows) * 0.25 == b["h"]
    for body, code in [({"symbol": "ES", "starts": [2, 1], "end": 3}, 400), ({"symbol": "ES", "starts": [1], "end": 1}, 400),
                       ({"symbol": "ES", "starts": [], "end": 1}, 422), ({"symbol": "ES", "starts": list(range(401)), "end": 1000}, 422),
                       ({"symbol": "ZZ", "starts": [1], "end": 2}, 404)]:
        assert client.post("/api/footprint", json=body).status_code == code, body
