"""Study alerts: touches of a moving level (VWAP, POC/VAH/VAL), re-arming, and levels that match the chart."""
import time
from datetime import datetime
from types import SimpleNamespace

import pytest

from chartsrv import sessions
from chartsrv.alerts import AlertEngine
from chartsrv.bars import make_series, parse_spec
from chartsrv.config import Instrument
from chartsrv.market import BookEntry
from chartsrv.models import Tick
from chartsrv.store import Store
from chartsrv.studies import ProfileBook
from chartsrv.study_alerts import StudySources, _Watch, describe

ES = Instrument("ES", "E-mini", 0.25, 50, "ES", rth=("08:30", "15:15"))


def test_touch_of_a_moving_level_fires_once_then_rearms(tmp_path):
    store = Store(tmp_path / "t.db")
    fired = []
    eng = AlertEngine(store, {"ES": ES}, None, fired.append)
    level = {"v": 7400.0}
    eng.studies = lambda sym, p, ts: (level["v"], 1)   # (level, its session)
    store.add_alert("ES", "study", {"study": "vwap", "band": 1, "tolerance_ticks": 1, "rearm_ticks": 4}, True, "")
    eng.reload()
    feed = lambda *px: [eng.on_tick(Tick("ES", 1_000_000 + i, p, 1, 0)) for i, p in enumerate(px)]   # noqa: E731
    feed(7400.0)                          # first look, on the level: never fires on startup, and not armed
    feed(7399.0, 7401.0)                  # crossing it again 4 ticks later: still not armed (needs 1 + 4 ticks)
    assert fired == []
    feed(7398.5, 7399.0, 7401.0)          # 6 ticks away re-arms it; then price runs through the band
    assert len(fired) == 1 and "touched session VWAP +1σ 7400.00" in fired[0]["message"]
    level["v"] = 7402.0                   # VWAP drifts up to the price: still inside the band, no repeat
    feed(7401.75, 7402.0)
    assert len(fired) == 1
    feed(7403.5)                          # 6 ticks away: 1 tolerance + 4 re-arm = re-armed
    level["v"] = 7403.25                  # ...and the level comes to the price
    feed(7403.5)
    assert len(fired) == 2


def test_study_alert_turned_back_on_does_not_fire_for_a_touch_while_it_was_off(tmp_path):
    """Paused and restarted in the same session: a first look, as a new alert in the same spot gets."""
    store = Store(tmp_path / "t.db")
    fired = []
    eng = AlertEngine(store, {"ES": ES}, None, fired.append)
    eng.studies = lambda sym, p, ts: (7400.0, 1)   # (level, its session)
    store.add_alert("ES", "study", {"study": "vwap", "band": 0, "tolerance_ticks": 1, "rearm_ticks": 4}, True, "")
    eng.reload()
    feed = lambda *px: [eng.on_tick(Tick("ES", 1_000_000 + i, p, 1, 0)) for i, p in enumerate(px)]   # noqa: E731
    feed(7390.0, 7390.25)                 # armed
    store.update_alert(1, enabled=False)
    eng.reload()
    feed(7395.0, 7400.0)                  # to the level while it was off
    store.update_alert(1, enabled=True)
    eng.reload()
    feed(7400.0, 7400.0)
    assert fired == []

def test_a_level_that_starts_over_at_the_open_does_not_fire(tmp_path):
    """At 17:00 CT (and at the RTH open) VWAP, its bands and the developing profile start over at the first trade.
    That is a new level appearing under the price, as at startup, not price touching the old one."""
    def ct(d, h, mi):
        return int(datetime(2025, 10, d, h, mi, tzinfo=sessions.CT).timestamp() * 1000)
    w = _Watch(make_series(parse_spec("1m"), ES.tick_size, ES.decimals, 3000, ES.rth))
    be = BookEntry(ProfileBook(ES))
    w.ready.set()
    be.ready.set()
    src = StudySources(SimpleNamespace(books={"ES": be}, instruments={"ES": ES}))
    src.watches["ES"] = w
    store = Store(tmp_path / "t.db")
    fired = []
    eng = AlertEngine(store, {"ES": ES}, None, fired.append)
    eng.studies = src.value
    for p in ({"study": "vwap", "band": 0}, {"study": "vwap", "band": 2}, {"study": "rth_vwap", "band": -1},
              {"study": "poc", "profile": "eth"}, {"study": "val", "profile": "rth"}):
        store.add_alert("ES", "study", {**p, "tolerance_ticks": 0, "rearm_ticks": 8}, True, "")
    eng.reload()

    def tick(ts, px):
        tk = Tick("ES", ts, px, 1, 1)
        w.series.on_tick(tk)
        be.book.add(tk)
        eng.on_tick(tk)
    for k in range(300):                                   # Monday's RTH around 7400, then a run up to 7419.5
        tick(ct(13, 8, 30) + k * 60_000, 7400 + (k % 5 - 2) * 0.25)
    for k in range(20):
        tick(ct(13, 14, 40) + k * 1000, 7410 + k * 0.5)
    n = len(fired)
    tick(ct(13, 17, 0) + 5, 7420.0)                         # the new session's first trade
    tick(ct(13, 17, 0) + 900, 7420.25)
    for k in range(60):                                    # overnight drifts up; Tuesday's RTH opens at 7440
        tick(ct(14, 7, 0) + k * 60_000, 7425 + k * 0.25)
    tick(ct(14, 8, 30) + 5, 7440.0)
    assert [e["message"] for e in fired[n:]] == []
    tick(ct(14, 8, 31), 7442.5)                             # 10 ticks away: re-armed...
    tick(ct(14, 8, 32), 7438.0)                             # ...and back through the RTH VWAP's -1σ band
    assert any("touched RTH VWAP -1σ" in e["message"] for e in fired[n:])


def test_rth_profiles_outside_regular_hours(tmp_path):
    """From the RTH close to the next RTH open the latest RTH profile is done: it is the previous one (not the one
    before it), and there is no developing one until the open."""
    def ct(d, h, mi=0):
        return int(datetime(2025, 10, d, h, mi, tzinfo=sessions.CT).timestamp() * 1000)
    be = BookEntry(ProfileBook(ES))
    be.ready.set()
    src = StudySources(SimpleNamespace(books={"ES": be}, instruments={"ES": ES}))
    for d, px in ((13, 7000.0), (14, 7100.0)):          # Monday's RTH trades at 7000, Tuesday's at 7100
        for k in range(60):
            be.book.add(Tick("ES", ct(d, 9) + k * 60_000, px, 10, 1))
    poc = lambda which, ts: src.value("ES", {"study": "poc", "profile": "rth", "which": which}, ts)   # noqa: E731
    day = lambda d: datetime(2025, 10, d).toordinal()   # noqa: E731
    assert (poc("previous", ct(14, 10)), poc("current", ct(14, 10))) == ((7000.0, day(13)), (7100.0, day(14)))
    for ts in (ct(14, 15, 30), ct(14, 20), ct(15, 8, 29)):   # after Tuesday's RTH, overnight, just before Wednesday's
        be.book.add(Tick("ES", ts, 7150.0, 1, 1))
        assert (poc("previous", ts), poc("current", ts)) == ((7100.0, day(14)), None), ts
    be.book.add(Tick("ES", ct(15, 8, 31), 7150.0, 1, 1))   # Wednesday's RTH
    assert (poc("previous", ct(15, 8, 31)), poc("current", ct(15, 8, 31))) == ((7100.0, day(14)), (7150.0, day(15)))

def test_names():
    assert describe({"study": "rth_vwap", "band": -2}) == "RTH VWAP -2σ"
    assert describe({"study": "poc", "profile": "rth", "which": "previous"}) == "previous RTH POC"
    assert describe({"study": "vah"}) == "developing ETH VAH"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("CHART_DB", str(tmp_path / "chart.db"))
    monkeypatch.setenv("CHART_LEVELS_DIR", str(tmp_path))
    monkeypatch.setenv("CHART_FEED", "sim")
    monkeypatch.setenv("CHART_SIM_WARMUP_MIN", "90")
    from fastapi.testclient import TestClient
    from chartsrv.app import app
    with TestClient(app) as c:
        yield c


def test_alert_levels_are_the_chart_levels(client):
    from chartsrv.app import M
    for body in ({"study": "vwap"}, {"study": "poc", "profile": "eth"}):
        r = client.post("/api/alerts", json={"symbol": "ES", "kind": "study", **body})
        assert r.status_code == 201 and r.json()["repeat"] is True
    m = M()
    src = m.study_sources
    for _ in range(100):
        if "ES" in src.watches and src.watches["ES"].ready.is_set() and m.books.get("ES") and m.books["ES"].ready.is_set():
            break
        time.sleep(0.1)
    with client.websocket_connect("/ws") as ws:
        ws.send_json({"op": "sub", "id": 1, "symbol": "ES", "spec": "1m"})
        for _ in range(500):
            msg = ws.receive_json()
            if msg["type"] == "history":
                break
    bar = msg["bars"][-1]
    ts = bar["t"] + 1
    vwap, _ = src.value("ES", {"study": "vwap"}, ts)
    assert abs(vwap - bar["st"][0]) < 0.05       # chart VWAP (rounded to 4 decimals)
    eth_poc = next(p["poc"] for p in m.books["ES"].book.current() if p["kind"] == "eth")
    assert src.value("ES", {"study": "poc", "profile": "eth"}, ts)[0] == eth_poc
    for body, code in [({"kind": "study"}, 400), ({"kind": "study", "study": "median"}, 422),
                       ({"kind": "study", "study": "vwap", "band": 4}, 422)]:
        assert client.post("/api/alerts", json={"symbol": "ES", **body}).status_code == code, body
    a = next(x for x in client.get("/api/alerts").json() if x["kind"] == "study" and x["params"]["study"] == "vwap")
    assert a["params"] == {"study": "vwap", "tolerance_ticks": 0, "rearm_ticks": 8, "band": 0}
