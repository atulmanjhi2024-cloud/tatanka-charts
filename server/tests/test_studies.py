"""VWAP, value area and volume profiles."""
import asyncio
import json
import math
import random
from datetime import datetime

from fastapi.testclient import TestClient

from chartsrv.bars import make_series, parse_spec
from chartsrv.config import Instrument
from chartsrv.models import Bar, Tick
from chartsrv.sessions import CT
from chartsrv.store import Store
from chartsrv.studies import ProfileBook, RthClock, Vwap, range_profile, value_area

ES = Instrument("ES", "E-mini", 0.25, 50, "ES", rth=("08:30", "15:15"))


def ms(y, mo, d, h, mi=0, s=0):
    return int(datetime(y, mo, d, h, mi, s, tzinfo=CT).timestamp() * 1000)


def test_vwap_and_sd_match_direct_formula():
    rng = random.Random(1)
    trades = [(7800 + rng.randint(-40, 40) * 0.25, rng.randint(1, 30)) for _ in range(5000)]
    v = Vwap()
    for p, q in trades:
        v.add(1, p, q)
    tv = sum(q for _, q in trades)
    mean = sum(p * q for p, q in trades) / tv
    sd = math.sqrt(sum(q * (p - mean) ** 2 for p, q in trades) / tv)
    got = v.value()
    assert abs(got[0] - mean) < 1e-9 and abs(got[1] - sd) < 1e-9
    v.add(2, 7000.0, 5)                       # new session key resets
    assert v.value() == (7000.0, 0.0)


def test_value_area_70pct_from_poc():
    vols = [1, 2, 5, 10, 30, 12, 6, 3, 1]     # total 70, POC index 4
    poc, lo, hi = value_area(vols)
    assert poc == 4 and sum(vols[lo:hi + 1]) >= 0.7 * sum(vols)
    assert (lo, hi) == (3, 5)                  # 30 -> +12 -> +10 = 52 >= 49
    assert value_area([5, 5, 5])[0] == 1       # tie goes to the middle


def test_rth_clock():
    c = RthClock(("08:30", "15:15"))
    assert c.day(ms(2026, 10, 14, 8, 29)) is None
    assert c.day(ms(2026, 10, 14, 8, 30)) == datetime(2026, 10, 14).toordinal()
    assert c.day(ms(2026, 10, 14, 15, 15)) is None
    assert c.day(ms(2026, 10, 17, 10)) is None     # Saturday


def test_bars_carry_session_and_rth_vwap():
    s = make_series(parse_spec("5m"), 0.25, 2, rth=ES.rth)
    s.on_tick(Tick("ES", ms(2026, 10, 14, 7, 0), 100.0, 10, 1))          # overnight: ETH only
    b = s.last
    assert b.st[:2] == [100.0, 0.0] and b.st[2] is None
    s.on_tick(Tick("ES", ms(2026, 10, 14, 8, 30), 102.0, 10, 1))         # RTH opens: RTH VWAP starts
    assert s.last.st[0] == 101.0 and s.last.st[2] == 102.0
    s.on_tick(Tick("ES", ms(2026, 10, 14, 17, 0, 1), 90.0, 5, -1))       # new Globex session resets ETH
    assert s.last.st[0] == 90.0 and s.last.st[2] is None
    assert "st" in s.last.to_dict()


def test_vwap_from_1m_history_uses_typical_price():
    s = make_series(parse_spec("15m"), 0.25, 2, rth=ES.rth)
    s.on_bar(Bar(t=ms(2026, 10, 14, 9, 0), o=1, h=103, l=97, c=100, v=10))
    assert s.last.st[0] == 100.0 and s.last.st[2] == 100.0


def test_profile_book_live_and_history(tmp_path):
    store = Store(tmp_path / "t.db")
    t0 = ms(2026, 10, 14, 9, 0)
    hist = [Tick("ES", t0 + i, p, 2, 1 if i % 2 else -1) for i, p in enumerate([100.0, 100.25, 100.25, 100.5])]
    store.write(hist, [])
    book = ProfileBook(ES)
    book.load(store.path, "ES", ms(2026, 10, 14, 10), store.max_tick_rowid())
    book.add(Tick("ES", t0 + 100, 100.25, 4, 1))                          # live tick, same session
    eth = [p for p in book.snapshot() if p["kind"] == "eth"][-1]
    rth = [p for p in book.snapshot() if p["kind"] == "rth"][-1]
    assert eth["p0"] == 100.0 and eth["v"] == [2, 8, 2] and eth["poc"] == 100.25 and eth["total"] == 12
    assert rth["v"] == eth["v"] and not eth["approx"]
    assert eth["d"] == [-2, 4, 2]


def test_profile_falls_back_to_bars_and_marks_approx(tmp_path):
    store = Store(tmp_path / "t.db")
    store.insert_bars_if_missing("ES", [Bar(t=ms(2026, 10, 14, 9, 0), o=100, h=101, l=100, c=101, v=50)])
    book = ProfileBook(ES)
    book.load(store.path, "ES", ms(2026, 10, 14, 10), 0)
    eth = book.snapshot()[0]
    assert eth["approx"] and eth["v"] == [10] * 5 and eth["total"] == 50


def test_visible_range_takes_the_bars_of_each_session_with_no_trades(tmp_path):
    """Two days on the chart: Tuesday from backfilled 1-minute bars, Wednesday's trades (the add-on connected since).
    Each session gives its trades, or its bars where it has none, all hours or RTH alike."""
    store = Store(tmp_path / "t.db")
    tue, wed = ms(2026, 10, 13, 8, 30), ms(2026, 10, 14, 8, 30)
    store.insert_bars_if_missing("ES", [Bar(t=tue + i * 60_000, o=6700, h=6700.25, l=6700, c=6700.25, v=1000) for i in range(300)])
    store.write([Tick("ES", wed + i * 1000, 6800.0, 1, 1) for i in range(600)],
                [("ES", Bar(t=wed + i * 60_000, o=6800, h=6800, l=6800, c=6800, v=60)) for i in range(10)])
    for rth in (False, True):
        p = range_profile(store.path, ES, "ES", ms(2026, 10, 12, 17), ms(2026, 10, 14, 16), True, rth)
        assert p["total"] == 300_600 and p["approx"] and p["poc"] in (6700.0, 6700.25)


def test_profiles_over_websocket_and_range_endpoint(tmp_path, monkeypatch, in_session):
    logs = tmp_path / "logs"
    logs.mkdir()
    monkeypatch.setenv("CHART_DB", str(tmp_path / "chart.db"))
    monkeypatch.setenv("CHART_LEVELS_DIR", str(logs))
    monkeypatch.setenv("CHART_FEED", "sim")
    monkeypatch.setenv("CHART_SIM_WARMUP_MIN", "20")
    from chartsrv.app import app
    with TestClient(app) as c, c.websocket_connect("/ws") as ws:
        ws.send_json({"op": "sub", "id": 1, "symbol": "ES", "spec": "1m"})
        got = {}
        for _ in range(400):
            m = ws.receive_json()
            got.setdefault(m["type"], m)
            if "history" in got:
                break
        assert "st" in got["history"]["bars"][-1] and "profiles" not in got   # session profiles: /api/vp/prof
        # the volume profiles ported from NinjaTrader, on the sim's trades and minutes (a session window of the whole day)
        assert c.put("/api/indicators/prof", json={"SessionStart": "00:00", "SessionEnd": "00:00"}).status_code == 200
        svp = c.get("/api/vp/prof", params={"symbol": "ES"}).json()
        s = svp["sessions"][-1]
        assert svp["tick"] == 0.25 and s["rows"] and s["val"] <= s["poc"] <= s["vah"] and s["start"] <= s["last"] < s["end"]
        for kind in ("aavp", "pvp"):
            d = c.get(f"/api/vp/{kind}", params={"symbol": "ES"}).json()
            p = d["profile"] if kind == "aavp" else d["profiles"][-1]
            assert p["rows"] and p["val"] <= p["poc"] <= p["vah"] and p["start"] < p["last"]
        assert c.get("/api/vp/nope", params={"symbol": "ES"}).status_code == 404
        assert c.put("/api/indicators/prof", json={"SessionStart": "8:3"}).status_code == 200   # NinjaTrader takes it too
        assert "error" in c.get("/api/vp/prof", params={"symbol": "ES"}).json()   # ... and then says so on the chart
        last = got["history"]["bars"][-1]["t"]
        r = c.get("/api/profile", params={"symbol": "ES", "start": last - 600_000, "end": last + 60_000}).json()
        assert r["kind"] == "range" and r["total"] > 0
        comp = c.get("/api/profile", params={"symbol": "ES", "sessions": 3}).json()
        assert comp["total"] >= r["total"]
        assert c.get("/api/profile", params={"symbol": "ES"}).status_code == 400
