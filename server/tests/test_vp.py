"""The volume-profile service (vp_service.py): PVP touches, answers that skip what a client holds, the API.
The calculations themselves are checked against the NinjaTrader code by tests/vp_parity.py."""
import json
import random
import time
from dataclasses import replace
from datetime import datetime, timedelta

from fastapi.testclient import TestClient

from chartsrv import vp
from chartsrv.models import Bar
from chartsrv.sessions import CT, to_ct
from chartsrv.vp_service import MinuteSource, MinuteState

MIN = 60_000


def minutes(seed: int, days: int) -> list[Bar]:
    """1-minute bars (stamped at their open, as stored) of a random walk over the weekday Globex sessions."""
    rng = random.Random(seed)
    t = int(datetime(2026, 10, 4, 17, 0, tzinfo=CT).timestamp() * 1000)   # Sunday 17:00: Monday's session
    end = t + days * 24 * 3600_000
    p, out = 4000 * 4, []
    while t < end:
        ct = to_ct(t)
        if ct.hour != 16 and not (ct.weekday() == 4 and ct.hour >= 16) and ct.weekday() != 5 and not (ct.weekday() == 6 and ct.hour < 17):
            p += rng.choice((-3, -1, 0, 1, 3)) + (40 if rng.random() < 0.002 else 0)
            lo, hi = p - rng.randint(0, 4), p + rng.randint(0, 4)
            o, c = rng.randint(lo, hi), rng.randint(lo, hi)
            out.append(Bar(t=t, o=o / 4, h=hi / 4, l=lo / 4, c=c / 4, v=rng.randint(1, 500)))
        t += MIN
    return out


def reference_touches(bars, keep):
    """Touches.Observe as the indicator calls it, on every minute after a profile closed (brute force)."""
    ser, src = vp.PeriodicSeries("Daily", 1, keep, 0.25, True, 50, 68.0, False), MinuteSource()
    closed, prev = [], None
    for b in bars:
        a = src.args(b)
        ser.add(*a)
        if ser.current is not prev:
            if prev is not None and prev.closed:
                closed.append((prev, prev.snapshot(), {}))
            prev = ser.current
        for p, snap, touch in closed:
            if a[7] > p.last_chart:
                for name in ("poc", "vah", "val"):
                    if name not in touch and a[3] <= snap[name] <= a[4]:
                        touch[name] = a[7]
    return {p.start_chart: touch for p, _, touch in closed if p in ser.profiles}


def test_pvp_touches_match_observe():
    bars = minutes(3, 9)
    for keep in (20, 3):
        want = reference_touches([replace(b) for b in bars], keep)
        st = MinuteState(vp.PeriodicSeries("Daily", 1, keep, 0.25, True, 50, 68.0, False))
        half = len(bars) // 2
        st.feed([replace(b) for b in bars[:half]])
        st.feed([replace(b) for b in bars[half - 1:]])   # the last minute again, as a forming one is read twice
        got = {p.start_chart: p.touch for p in st.series.profiles if p.closed}
        assert got == want and len(got) == min(keep, 7) - 1   # 7 trading days, the last one developing
        if keep == 20:   # some touched, some naked
            assert any(len(t) == 3 for t in got.values()) and any(len(t) < 3 for t in got.values())
        assert st.series.current.closed is False and not hasattr(st.series.current, "touch")


def _ct_hhmm(ms: int) -> str:
    return to_ct(ms).strftime("%H:%M")


def test_api_skips_what_the_client_holds(tmp_path, monkeypatch, in_session):
    logs = tmp_path / "logs"
    logs.mkdir()
    monkeypatch.setenv("CHART_DB", str(tmp_path / "chart.db"))
    monkeypatch.setenv("CHART_LEVELS_DIR", str(logs))
    monkeypatch.setenv("CHART_FEED", "sim")
    monkeypatch.setenv("CHART_SIM_WARMUP_MIN", "20")
    from chartsrv.app import app
    with TestClient(app) as c:
        m = app.state.market
        now = int(time.time() * 1000) // MIN * MIN   # the server's clock (in_session moves it)
        # a session that ended a few minutes ago: finished, so it can be held
        put = {"SessionStart": _ct_hhmm(now - 15 * MIN), "SessionEnd": _ct_hhmm(now - 3 * MIN)}
        assert c.put("/api/indicators/prof", json=put).status_code == 200
        for _ in range(100):   # the sim's warm-up trades
            first = c.get("/api/vp/prof", params={"symbol": "ES"}).json()
            if first["sessions"]:
                break
        s = first["sessions"][-1]
        assert s["done"] and s["rows"] and s["end"] <= now
        again = c.get("/api/vp/prof", params={"symbol": "ES", "key": first["key"], "have": str(s["start"])}).json()
        assert again["key"] == first["key"] and again["sessions"][-1] == {"start": s["start"], "same": True}
        other = c.get("/api/vp/prof", params={"symbol": "ES", "key": "nope", "have": str(s["start"])}).json()
        assert other["sessions"][-1]["rows"] == s["rows"]
        c.portal.call(m.vp.forget, "ES")   # new history (on the server's thread): a new build, everything sent again
        fresh = c.get("/api/vp/prof", params={"symbol": "ES", "key": first["key"], "have": str(s["start"])}).json()
        assert fresh["key"] != first["key"] and fresh["sessions"][-1]["rows"] == s["rows"]
        assert c.get("/api/vp/prof", params={"symbol": "ES", "have": "x"}).status_code == 400
        # PVP: the developing profile always comes whole, with no touches yet
        d = c.get("/api/vp/pvp", params={"symbol": "ES"}).json()
        p = d["profiles"][-1]
        assert d["key"] and p["closed"] is False and p["touch"] == {} and p["rows"]
        d2 = c.get("/api/vp/pvp", params={"symbol": "ES", "key": d["key"], "have": str(p["start"])}).json()
        assert d2["profiles"][-1]["rows"]
        # a chart tab's own settings, sent with the request, over the saved ones
        own = c.get("/api/vp/pvp", params={"symbol": "ES", "s": json.dumps({"Period": "Daily", "ProfilesToKeep": 2, "RowSize": 50})}).json()
        assert own["key"] != d["key"] and own["profiles"][-1]["tpr"] >= 1 and len(own["profiles"]) <= 2
        assert c.get("/api/vp/pvp", params={"symbol": "ES", "s": json.dumps({"Period": "Hourly"})}).status_code == 400
        a = c.get("/api/vp/aavp", params={"symbol": "ES"}).json()
        assert a["profile"]["first"] is True and a["first_td"] <= a["last_td"]   # nothing stored before the anchor
        assert (datetime.fromisoformat(a["profile"]["window"][1]) - datetime.fromisoformat(a["profile"]["window"][0])) >= timedelta(days=365)


def test_svp_takes_bars_where_no_trades_are_stored(tmp_path):
    """Minutes with no stored trades (the trade feed was off) take their 1-minute bar; minutes with trades use them."""
    from chartsrv.models import Tick
    from chartsrv.store import Store
    from chartsrv.vp_service import svp_job
    store = Store(tmp_path / "t.db")
    b = int(datetime(2026, 10, 14, 8, 30, tzinfo=CT).timestamp() * 1000)
    e = b + 10 * MIN
    bars = [Bar(t=b + i * MIN, o=100.25, h=100.5, l=100.0, c=100.5 if i % 2 else 100.0, v=30) for i in range(10)]
    store.insert_bars_if_missing("ES", bars)
    # trades for minutes 6 and 7 only (the feed came on, then went off again)
    store.write([Tick("ES", b + 6 * MIN + 1000, 100.25, 5, 1), Tick("ES", b + 7 * MIN + 2000, 100.0, 7, -1)], [])
    st, = svp_job(store.path, "ES", [(b, e)], store.max_tick_rowid(), 0.25)
    assert st.approx == 8 and st.trades == 2
    up = sum(r[0] for r in st.ticks.values()); down = sum(r[1] for r in st.ticks.values())
    assert abs(up + down - (8 * 30 + 12)) < 1e-9          # each bar's volume once, plus the trades
    assert abs(up - (4 * 30 + 5)) < 1e-9                  # odd minutes closed up; the first trade is up, the next one down
    assert st.last == e                                    # the last bar's end
    # the forming session (ends after now), trades coming in: no bar for this minute or later
    st2, = svp_job(store.path, "ES", [(b, e)], store.max_tick_rowid(), 0.25, now=b + 5 * MIN + 30_000)
    assert st2.approx == 5 and st2.trades == 2
    nothing, = svp_job(store.path, "NQ", [(b, e)], store.max_tick_rowid(), 0.25)
    assert nothing.ticks == {} and nothing.approx == 0


def test_sessions_partly_from_bars_are_not_kept_over_a_restart(tmp_path):
    """A session cached with minutes from 1-minute bars is worked out again after a restart (an import of its trades
    needs one); a session from trades alone stays."""
    from types import SimpleNamespace

    from chartsrv.store import Store
    from chartsrv.vp_service import SCHEMA_SQL, VpService
    store = Store(tmp_path / "t.db")
    store.db.execute(SCHEMA_SQL)
    with store.db:
        store.db.execute("INSERT INTO vp_cache VALUES ('svp2|ES|1|2|0.25', ?)", (json.dumps({"ticks": [], "last": 2, "approx": 0}),))
        store.db.execute("INSERT INTO vp_cache VALUES ('svp2|ES|3|4|0.25', ?)", (json.dumps({"ticks": [], "last": 4, "approx": 7}),))
        store.db.execute("INSERT INTO vp_cache VALUES ('svp|ES|5|6', '{}')")
    VpService(SimpleNamespace(store=store))
    assert [k for (k,) in store.db.execute("SELECT key FROM vp_cache ORDER BY key")] == ["svp2|ES|1|2|0.25"]
