"""If/Then port: the daily ATR the chart is given and the per-symbol block on the server. The parsing, active
band, formats, wrapping and colors run in the browser and are checked against the original C# by
tests/ifthen_parity.py (needs mono and node)."""
import math

import pytest

from chartsrv.ifthen import TatankaBiasDailyAtr, previous_atr


def test_atr_is_pine_rma_seeded_with_an_sma():
    a = TatankaBiasDailyAtr(3)
    for i, (h, l, c) in enumerate([(10, 8, 9), (12, 9, 11), (11, 7, 8), (9, 6, 7)]):
        a.Update(i, h, l, c)
    # TR: 2, 3, 4, 3. NaN until 3 days, then SMA(2, 3, 4) = 3, then RMA: TR/3 + (1 - 1/3) * previous
    assert math.isnan(a.values[0]) and math.isnan(a.values[1]) and a.values[2] == 3.0
    assert a.Current == (1.0 / 3) * 3 + (1.0 - 1.0 / 3) * 3.0 and a.Previous == 3.0
    a.Update(3, 15, 6, 7)                              # the developing day again, wider: recomputed from day 2
    assert a.Current == (1.0 / 3) * 9 + (1.0 - 1.0 / 3) * 3.0 and a.Previous == 3.0


def test_previous_atr_follows_the_latest_daily_bar():
    hlc = [[10, 8, 9], [12, 9, 11], [11, 7, 8], [9, 6, 7]]
    last = (1.0 / 3) * 3 + (1.0 - 1.0 / 3) * 3.0
    assert previous_atr(hlc, 3, today_has_bars=True) == last      # today trades: the last completed day's ATR
    assert previous_atr(hlc, 3, today_has_bars=False) == 3.0      # weekend: the day before the last one
    assert previous_atr(hlc[:3], 3, today_has_bars=False) is None
    assert previous_atr([], 14, today_has_bars=True) is None


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("CHART_DB", str(tmp_path / "chart.db"))
    monkeypatch.setenv("CHART_LEVELS_DIR", str(tmp_path))
    monkeypatch.setenv("CHART_FEED", "sim")
    monkeypatch.setenv("CHART_SIM_WARMUP_MIN", "60")
    from fastapi.testclient import TestClient
    from chartsrv.app import app
    with TestClient(app) as c:
        yield c


def test_block_is_kept_per_symbol_and_pushed_to_open_charts(client):
    block = "U|7450|9999999|Accepts above 7450 → 7480\nP|7395|7405|Pivot\nD|0|7395|Below the pivot"
    assert client.get("/api/ifthen/es").json()["rows"] == ""
    with client.websocket_connect("/ws") as ws:
        r = client.put("/api/ifthen/es", json={"rows": block})
        assert r.status_code == 200 and r.json()["rows"] == block and r.json()["updated"]
        for _ in range(200):
            m = ws.receive_json()
            if m["type"] == "ifthen":
                break
        assert m["symbol"] == "ES" and m["rows"] == block
    d = client.get("/api/ifthen/ES", params={"atr": 5}).json()
    assert d["rows"] == block and "atr" in d and d["version"] == "1.0.0"
    assert client.get("/api/ifthen/NQ").json()["rows"] == ""
    assert client.put("/api/ifthen/ES", json={"rows": "x" * 20_001}).status_code == 422
    assert client.get("/api/ifthen/ES", params={"atr": 0}).status_code == 400
    assert client.get("/api/ifthen/ZZ").status_code == 404
