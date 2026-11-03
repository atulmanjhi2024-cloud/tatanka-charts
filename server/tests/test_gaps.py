"""Gap Analysis port: session keys, gaps and fills, the chart payload, the browser's live-bar code, alerts and
/api/gaps. Statement-for-statement parity with the original C# is checked by tests/gap_parity.py (needs mono)."""
import asyncio
import json
import shutil
import subprocess
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from amt_parity import synth
from chartsrv import gaps
from chartsrv.amt_runner import ct, epoch_ms
from chartsrv.gaps_service import GapService, payload, run
from chartsrv.models import Bar

E = gaps.GapEngine
WEB = Path(__file__).resolve().parents[2] / "web"


def test_settings_and_time_parsing():
    assert gaps.settings({})["RthEnd"] == 150000
    assert E.TryParseTime(83000) == (True, timedelta(hours=8, minutes=30))
    assert E.TryParseTime(86000)[0] is False and E.TryParseTime(240000)[0] is False
    for bad in [{"RthStart": 150000}, {"RthEnd": 999999}, {"MinimumGapPoints": -1}, {"Nope": 1}, {"RthStart": "0830"}]:
        with pytest.raises(ValueError):
            gaps.settings(bad)


def test_session_key_uses_bar_end_times():
    e = gaps.engine_for(gaps.settings({}))
    d = datetime(2026, 10, 15)
    assert e.SessionKey(d.replace(hour=8, minute=30)) == gaps.DMIN          # the 08:25-08:30 bar is pre-RTH
    assert e.SessionKey(d.replace(hour=8, minute=31)) == d
    assert e.SessionKey(d.replace(hour=15, minute=0)) == d                  # the last RTH bar
    assert e.SessionKey(d.replace(hour=15, minute=1)) == gaps.DMIN
    night = gaps.engine_for(gaps.settings({"RthStart": 170000, "RthEnd": 83000}))
    assert night.SessionKey(d.replace(hour=2)) == d - timedelta(days=1)
    assert night.SessionKey(d.replace(hour=12)) == gaps.DMIN


def _bar(e, i, end, o, h, l, c):
    return e.Update(i, end, o, h, l, c)


def test_gap_partial_fill_full_fill_and_consecutive():
    e = gaps.engine_for(gaps.settings({}))
    d1, d2, d3 = datetime(2026, 10, 13), datetime(2026, 10, 14), datetime(2026, 10, 15)
    _bar(e, 0, d1.replace(hour=14, minute=55), 100, 101, 99, 100)
    _bar(e, 1, d1.replace(hour=15), 100, 101, 99, 100.5)                    # RTH close 100.5
    _bar(e, 2, d1.replace(hour=15, minute=5), 100.5, 103, 100, 102)         # after RTH: ignored for the close
    u = _bar(e, 3, d2.replace(hour=8, minute=35), 104, 105, 103, 104.5)     # gap up 3.5, low 103 fills 1.0
    g = u.NewGap
    assert g and g.Direction == 1 and g.Size == 3.5 and g.FillEdge == 103 and abs(g.FillPercent - 100 / 3.5) < 1e-9
    _bar(e, 4, d2.replace(hour=8, minute=40), 104.5, 106, 103.5, 105)       # higher low: the fill never grows back
    assert g.FillEdge == 103 and not g.Filled
    _bar(e, 5, d2.replace(hour=15), 105, 106, 104, 106)
    u = _bar(e, 6, d3.replace(hour=8, minute=35), 107, 108, 100.4, 101)     # second gap up fills both
    assert u.NewGap.Direction == 1 and e.consecutive == 2
    assert {x.Ordinal for x in u.FilledGaps} == {0, 1} and g.FillBar == 6 and g.BarsToFill == 3 and g.FillPercent == 100
    assert e.History[0].NextGapBar == 6 and e.History[0].DrawingEndBar(9, False) == 5


def test_repeated_updates_of_the_forming_bar_end_where_one_update_does():
    m1 = synth(20, 5, datetime(2026, 10, 16, 12, 7))
    a, _ = run(gaps.settings({}), 5, m1)
    b = gaps.engine_for(gaps.settings({}))
    from chartsrv.tpo_service import chart_bars
    for i, (k, close, h, l, _, o, c) in enumerate(chart_bars(m1, 5)):
        for frac in (0.3, 0.7):                                             # a growing bar, then its final state
            b.Update(i, ct(close), o, o + (h - o) * frac, o - (o - l) * frac, (o + c) / 2)
        b.Update(i, ct(close), o, h, l, c)
    key = lambda e: [(g.StartBar, g.FillBar, g.FillEdge, g.Direction, g.SettlePrice) for g in e.History]  # noqa: E731
    assert key(a) == key(b) and a.consecutive == b.consecutive and len(a.History) > 5


def test_payload_maps_bars_to_open_times_and_includes_the_forming_bar():
    m1 = synth(20, 5, datetime(2026, 10, 16, 12, 7))
    eng, opens = run(gaps.settings({}), 5, m1)
    d = payload(eng, opens, 5)
    assert ct(d["t"]) == datetime(2026, 10, 16, 12, 5) and d["bar"] == len(opens) - 1      # forming bar
    for g, r in zip(d["gaps"], eng.History):
        assert g["st"] == opens[r.StartBar] and ct(g["st"]).time() == datetime(2026, 1, 1, 8, 30).time()
        assert (g["ft"] is None) == (r.FillBar < 0)
        assert g["nt"] == (opens[r.NextGapBar - 1] if r.NextGapBar >= 0 else None)


@pytest.mark.skipif(not shutil.which("node"), reason="needs node")
def test_browser_live_bar_and_snapshot_match_the_engine():
    """studies.js gapLiveBar/gapSnapshot against GapEngine.Update/Snapshot: the server state at one bar, then
    the browser applies the following bars of that session."""
    m1 = synth(30, 8, datetime(2026, 10, 16, 14, 0))
    from chartsrv.tpo_service import chart_bars
    bars = chart_bars(m1, 5)
    split = next(i for i, b in enumerate(bars) if ct(b[0]) == datetime(2026, 10, 16, 9, 0))
    head, opens = run(gaps.settings({}), 5, m1)       # full run, for the expected result
    early = gaps.engine_for(gaps.settings({}))
    for i, (k, close, h, l, _, o, c) in enumerate(bars[:split + 1]):
        early.Update(i, ct(close), o, h, l, c)
    d = payload(early, opens[:split + 1], 5)
    later = [{"t": b[0], "h": b[2], "l": b[3]} for b in bars[split:]]
    cases = [(opens[a], opens[b], ext, hide, mx) for a, b in [(0, len(opens) - 1), (len(opens) - 300, len(opens) - 1),
                                                               (len(opens) - 2000, len(opens) - 1500)]
             for ext in (True, False) for hide in (True, False) for mx in (0, 2)]
    js = f"""
const vm = require('vm'), fs = require('fs'), ctx = {{}};
vm.runInNewContext(fs.readFileSync({json.dumps(str(WEB / 'studies.js'))}, 'utf8') + ';this.L=gapLiveBar;this.S=gapSnapshot;', ctx);
const d = {json.dumps(d)}; d.curBar = d.bar; d.curT = d.t;
{json.dumps(later)}.forEach((b, i) => ctx.L(d, b, d.bar + i));
const snaps = {json.dumps(cases)}.map(([f, l, e, h, m]) => ctx.S(d, f, l, e, h, m).map((g) => g.n));
console.log(JSON.stringify({{gaps: d.gaps.map((g) => [g.n, g.fb, g.edge]), snaps}}));"""
    out = json.loads(subprocess.run(["node", "-e", js], capture_output=True, text=True, check=True).stdout)
    assert out["gaps"] == [[g.Ordinal, g.FillBar, g.FillEdge] for g in head.History]
    index = {t: i for i, t in enumerate(opens)}
    for (f, l, ext, hide, mx), got in zip(cases, out["snaps"]):
        want = head.Snapshot(index[f], index[l], ext, hide, mx)
        assert got == [g.Ordinal for g in want.VisibleGaps], (f, l, ext, hide, mx)


class FakeStore:
    def __init__(self):
        self.events, self.kv = [], {}

    def get_setting(self, k, default=None):
        return self.kv.get(k, default)

    def set_setting(self, k, v):
        self.kv[k] = v

    def add_event(self, ev):
        self.events.append(ev)
        return len(self.events)


class FakeMarket:
    def __init__(self):
        from chartsrv.config import Instrument
        self.store = FakeStore()
        self.instruments = {"ES": Instrument(symbol="ES", name="ES", tick_size=0.25, point_value=50)}
        self.sent = []

    def broadcast(self, msg):
        self.sent.append(msg)

    async def _deliver(self, ev):
        pass


def test_alerts_fire_on_live_updates_only(monkeypatch):
    import chartsrv.gaps_service as gs
    monkeypatch.setattr(gs, "LIVE_BAR_AGE_MS", 10**15)        # the test's bars are not from today

    async def go():
        svc = GapService(FakeMarket())
        w = svc.watches["ES"] = gs.GapWatch({"on": True, "new": True, "fill": True, **gaps.DEFAULTS})
        w.ready.set()
        svc.on_minute("ES", Bar(t=epoch_ms(datetime(2026, 10, 15, 14, 59)), o=100, h=101, l=99, c=100), live=False)
        live = Bar(t=epoch_ms(datetime(2026, 10, 16, 8, 30)), o=104, h=104.5, l=103, c=103.5)   # opens 4 higher
        svc.on_minute("ES", live, live=True)
        live.l = 99.75                                          # a later tick in the same minute fills it
        svc.on_minute("ES", live, live=True)
        await asyncio.sleep(0)
        return svc, w

    svc, w = asyncio.run(go())
    assert [e["message"] for e in svc.m.store.events] == [
        "[ES] GAP UP 4.00 pts | Open: 104.00 | RTH close: 100.00", "[ES] GAP UP FILLED | 4.00 pts in 0 bars"]
    assert len(w.engine.History) == 1 and [m["type"] for m in svc.m.sent] == ["alert", "alert"]


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


def test_gap_endpoints(client):
    r = client.get("/api/gaps", params={"symbol": "es", "spec": "5m"})
    assert r.status_code == 200 and r.json()["ready"] and "gaps" in r.json()
    for params, code in [({"spec": "2000t"}, 400), ({"spec": "5m", "RthStart": 150000}, 400),
                         ({"spec": "5m", "days": 0}, 400), ({"spec": "1h", "MinimumGapPoints": 2.5}, 200)]:
        assert client.get("/api/gaps", params={"symbol": "ES", **params}).status_code == code, params
    r = client.put("/api/gaps/alerts/es", json={"on": True, "RthStart": 80000, "RthEnd": 133000})
    assert r.status_code == 200 and r.json()["RthStart"] == 80000 and r.json()["new"] is True
    assert client.get("/api/gaps/alerts").json()["alerts"]["ES"]["on"] is True
    assert client.put("/api/gaps/alerts/ES", json={"on": True, "RthStart": 80000, "RthEnd": 80000}).status_code == 400
    assert client.put("/api/gaps/alerts/ZZ", json={"on": True}).status_code == 404
