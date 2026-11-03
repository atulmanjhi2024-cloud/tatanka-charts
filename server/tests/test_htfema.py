"""1hr 21 EMA port: settings, scaled length, the server payload and the browser code (node). Parity with the
original C# is checked by tests/htfema_parity.py (needs mono)."""
import shutil
from datetime import datetime

import pytest

from amt_parity import synth
from chartsrv import htfema
from chartsrv.tpo_service import chart_bars
from htfema_parity import js_values

needs_node = pytest.mark.skipif(not shutil.which("node"), reason="needs node")


def test_settings_and_scaled_length():
    s = htfema.settings({})
    assert (s["HtfPeriod"], s["EmaLength"], s["Mode"]) == (60, 21, "SmoothScaled")
    assert htfema.GetScaledLength(s, "Minute", 5) == 252 and htfema.GetScaledLength(s, "Minute", 1) == 1260
    assert htfema.GetScaledLength(s, "Tick", 2000) == 21                        # untimed charts: the plain length
    assert htfema.GetScaledLength(htfema.settings({"EmaLength": 3}), "Minute", 8) == 22   # 22.5: to even, as C#
    assert htfema.GetScaledLength(s, "Day", 1) == 1
    for bad in [{"Mode": "Fast"}, {"HtfPeriodType": "Week"}, {"HtfPeriod": 0}, {"EmaLength": 501}, {"Nope": 1}]:
        with pytest.raises(ValueError):
            htfema.settings(bad)


def test_nt_ema_seeds_with_the_first_value():
    e = htfema.NtEma(3)
    assert [e.add(x) for x in (10, 14, 14)] == [10, 12, 13]


def _payload(m1, s, chart_min, start):
    scaled = htfema.GetScaledLength(s, "Minute", chart_min)
    return htfema.compute(s, m1, chart_min, scaled, start, m1[-1].t + 60_000)


@needs_node
@pytest.mark.parametrize("mode", htfema.MODES)
def test_chart_starting_mid_history_matches_a_full_run(mode):
    """The chart holds only recent bars: the server's seed and HTF timeline make it draw what a chart with all
    the history would."""
    s = htfema.settings({"Mode": mode})
    m1 = synth(30, 6, datetime(2026, 10, 16, 12, 7))
    bars = [{"t": b[0], "c": b[6]} for b in chart_bars(m1, 5)]
    full = js_values(bars, _payload(m1, s, 5, bars[0]["t"]), 300_000)
    cut = 3000
    part = js_values(bars[cut:], _payload(m1, s, 5, bars[cut]["t"]), 300_000)
    assert part == full[cut:] and all(v is not None for v in part)


def test_payload_shape_and_window():
    s = htfema.settings({})
    m1 = synth(10, 2, datetime(2026, 10, 16, 12, 7))
    d = _payload(m1, s, 5, m1[-600].t)
    assert d["scaled"] == 252 and d["smooth"]["count"] > 252 and d["htf"][0][0] < m1[-600].t <= d["htf"][1][0]
    assert all(v is not None for _, v in d["htf"])
    assert htfema.history_ms(s, 5, 252, 10**13, 10**13) == 10**13 - 14 * 86_400_000   # 8 lengths of 1h and of 252x5m


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("CHART_DB", str(tmp_path / "chart.db"))
    monkeypatch.setenv("CHART_LEVELS_DIR", str(tmp_path))
    monkeypatch.setenv("CHART_FEED", "sim")
    monkeypatch.setenv("CHART_SIM_WARMUP_MIN", "300")
    from fastapi.testclient import TestClient
    from chartsrv.app import app
    with TestClient(app) as c:
        yield c


def test_htfema_endpoint(client):
    import time
    start = int(time.time() * 1000) - 3 * 3600_000
    r = client.get("/api/htfema", params={"symbol": "es", "spec": "5m", "start": start})
    assert r.status_code == 200 and r.json()["scaled"] == 252 and r.json()["smooth"] is not None
    r = client.get("/api/htfema", params={"symbol": "ES", "spec": "2000t", "start": start, "Mode": "LiveHTF"})
    assert r.status_code == 200 and r.json()["scaled"] == 21 and r.json()["smooth"] is None and r.json()["ends"] == []
    for spec in ("8k", "1W", "1M"):   # Renko was a KeyError (500)
        r = client.get("/api/htfema", params={"symbol": "ES", "spec": spec, "start": start})
        assert r.status_code == 200, spec
    for params, code in [({"Mode": "Fast"}, 400), ({"HtfPeriodType": "Week"}, 400), ({"EmaLength": 0}, 400),
                         ({"spec": "xx"}, 400), ({"symbol": "ZZ"}, 404)]:
        q = {"symbol": "ES", "spec": "5m", "start": start, **params}
        assert client.get("/api/htfema", params=q).status_code == code, params
