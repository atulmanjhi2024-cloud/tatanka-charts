"""Each chart's own AMT Toolkit settings: a runner per distinct set, charts on the shared settings share one, and the
alerts and the levels file stay the shared ones."""
import asyncio

from chartsrv import amt_hub, sessions
from chartsrv.market import Client
from test_amt import client  # noqa: F401  (the app, as a fixture)
from test_bug_hunt import _warm
from test_review_fixes import build_market


def test_charts_keep_their_own_amt_settings(tmp_path, monkeypatch):
    sessions.set_calendar({"days": []})

    async def go():
        m, _ = build_market(tmp_path, monkeypatch)
        _warm(m)
        m.amt.set_overrides({"mvp_tf_mode": "Session", "LevelAlertMode": "Both"})
        a, b, c = Client(), Client(), Client()
        for cl, sid in ((a, 1), (b, 2), (c, 3)):
            cl.subs[sid] = object()
        await m.amt.subscribe(a, 1, "ES", 5)                                     # the shared settings
        await m.amt.subscribe(b, 2, "ES", 5, {"mvp_tf_mode": "Session", "vwap_width": 4, "LevelAlertMode": "Cross"})
        await m.amt.subscribe(c, 3, "ES", 5, {"mvp_tf_mode": "Session"})       # the shared ones again: no new runner
        own = [k for k in m.amt.entries if len(k) == 3]
        assert len(own) == 1 and ("ES", 5) in m.amt.entries
        assert (c, 3) in m.amt.entries[("ES", 5)].subs and (b, 2) in m.amt.entries[own[0]].subs
        runner = m.amt.entries[own[0]].runner
        assert runner.S.vwap_width == 4 and runner.S.LevelAlertMode == "Both"   # alerts: always the shared mode
        assert m.amt.entries[("ES", 5)].runner.S.vwap_width != 4
        try:
            await m.amt.subscribe(b, 2, "ES", 5, {"mvp_val_pct": 0})
            raise AssertionError("bad settings taken")
        except ValueError as e:
            assert "between 0 and 100" in str(e)
        assert any(" own " in k for k in m.amt.health())
        m.amt.set_overrides({})   # Apply to all: every runner starts again on the new shared settings
        assert not m.amt.entries
    asyncio.run(go())


def test_chart_settings_check(client):
    before = client.get("/api/amt").json()["overrides"]   # your NinjaTrader settings, seeded
    r = client.post("/api/amt/settings/check", json={"vwap_width": 3, "mvp_tf_mode": "Weekly"})
    assert r.status_code == 200 and r.json() == {"overrides": {"vwap_width": 3}}   # NinjaTrader's default is no change
    assert client.post("/api/amt/settings/check", json={"alertcap_max": 300}).json() == {"overrides": {}}   # shared only
    bad = client.post("/api/amt/settings/check", json={"row_scale": "3x"})
    assert bad.status_code == 400 and "Invalid Profile Row Scale" in bad.json()["detail"]
    assert "LevelsFile" in client.get("/api/amt").json()["shared"] and "LevelAlertMode" in amt_hub.SHARED
    assert client.get("/api/amt").json()["overrides"] == before   # nothing saved


def test_every_amt_alert_switch_is_shared_so_turning_one_off_stops_its_alert():
    """The alert runner uses the shared settings: a group's own alert switch (Alert 80% Rule Confirmed, Alert
    pdVAH/pdVAL/pdPOC Cross) kept only by a chart left the alert on (before: per chart, so its alerts kept coming)."""
    for k in ("cc_alert_80", "cc_alert_va", "pd_alert_hl", "lvl_alerts", "ib_alert_time", "mvp_use_cur_alerts",
              "spike_alert", "LevelAlertMode"):
        assert k in amt_hub.SHARED, k
    assert "vwap_width" not in amt_hub.SHARED and "mvp_tf_mode" not in amt_hub.SHARED
