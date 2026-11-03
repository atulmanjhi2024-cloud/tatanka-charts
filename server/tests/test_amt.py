"""AMT Toolkit port: levels parsing, .NET formatting, replay modes and the live server path.
Line-for-line parity with the original C# engine is checked by tests/amt_parity.py (needs mono)."""
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from amt_parity import dump, synth
from chartsrv import amt
from chartsrv.amt_runner import AmtRunner


def test_levels_paste_line_sorts_by_side_of_pivot():
    levels, date, sym = amt.parse_levels("```\n**ES levels**\n2026-01-05, 5000.25, 5010.25, 4990.25, 5020, 4980.25\n```")
    assert date == datetime(2026, 1, 5) and sym is None
    assert [(l.Name, l.Price) for l in levels] == [("PIVOT", 5000.25), ("RES1", 5010.25), ("RES2", 5020.0),
                                                   ("SUPP1", 4990.25), ("SUPP2", 4980.25)]


def test_levels_labeled_block_and_errors():
    levels, date, sym = amt.parse_levels("Symbol: es\nDate: 2026-10-05\nPivot Level: 100\n"
                                         "Resistance Levels: 101, 102\nSupport Levels: 99")
    assert sym == "ES" and date == datetime(2026, 10, 5) and [l.Name for l in levels] == ["PIVOT", "RES1", "RES2", "SUPP1"]
    for bad, msg in [("2026-01-05, 5,000.25, 5010.25, 4990.25", "more than 20%"),
                     ("2026-10-05, 100, 100, 99", "equals the pivot"),
                     ("2026-10-05, 100, 101", "above and one below"),
                     ("Pivot Level: 100\nSupport Levels: 99", "Include Pivot Level"),
                     ("Date: 2026-10-04\n2026-10-05, 100, 101, 99", "differ"),
                     ("nothing useful", "No levels found")]:
        with pytest.raises(ValueError, match=msg):
            amt.parse_levels(bad)


def test_dotnet_number_formatting():
    assert amt._fmt(56.125, 2) == "56.13"          # 15 digits, then half away from zero
    assert amt._fmt(2.5, 0) == "3" and amt._fmt(62.5, 0) == "63"
    assert amt._fmt(-0.004, 2) == "0.00"
    assert amt.price_str(7745.26, "ES", 0.25) == "7745.25" and amt.price_str(65.123, "CL", 0.01) == "65.12"
    assert amt.price_str(110.5, "ZN", 0.015625) == "110'160"


def _run(visuals: bool, days=21):
    r = AmtRunner("ES", 0.25, 5, amt.Settings())
    r.commit_visuals = visuals
    r.set_levels(6810.0, [6790.0, 6830.0], None, None)
    for i, m in enumerate(synth(days, 5, datetime(2026, 10, 16, 10, 0))):
        r.add_minute(m)
        if i % 50 == 0:
            r.commit_ready()
    r.commit_ready()
    f, _ = r.preview()
    out = []
    dump(out, f)
    return r, out


def test_history_replay_without_drawing_lists_leaves_identical_state():
    fast, a = _run(False)
    full, b = _run(True)
    assert a == b
    assert [x.Name for x in fast.history] == [x.Name for x in full.history]


def test_frame_has_ib_overnight_and_prior_day_structure():
    r, out = _run(False)
    f = r.frame
    names = {l.Name for l in f.Lines}
    assert f.Bar.Start == datetime(2026, 10, 16, 9, 55)          # Friday 09:55 CT bar in preview
    names = {n.replace("FLIPPED ", "") for n in names}       # a level price traded through is renamed
    assert {"IB High", "IB Low", "OVN High", "OVN Low", "pdHigh", "pdLow", "pdPOC", "pdVAH", "pdVAL",
            "Developing POC", "PIVOT", "RES1", "SUPP1"} <= names
    assert f.Values[0] == "RTH" and f.Labels[1] == "DAY TYPE"
    ib_h = next(l for l in f.Lines if l.Name == "IB High")
    assert ib_h.Start == datetime(2026, 10, 16, 8, 30)
    assert r.history and not any(x.Label for x in r.history)    # past days' lines, unlabeled
    assert {x.Name for x in r.history} >= {"IB High", "IB Low", "pdHigh", "pdLow"}
    payload = r.payload(True, True, 0)
    assert payload["ready"] and payload["profile"]["v"] and payload["dash"]["values"][0] == "RTH"
    assert all(len(l) == 12 for l in payload["lines"])


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("CHART_DB", str(tmp_path / "chart.db"))
    monkeypatch.setenv("CHART_DOCUMENTS_DIR", str(tmp_path))
    monkeypatch.setenv("CHART_FEED", "sim")
    monkeypatch.setenv("CHART_SIM_WARMUP_MIN", "60")
    from chartsrv.app import app
    with TestClient(app) as c:
        # invented daily levels on the Plan tab, near the simulator's ES (6900), dated long ago
        assert c.put("/api/amt/levels/ES", json={"text": "2026-01-15, 6900.00, 6920.00, 6940.00, 6880.00, 6860.00"}).status_code == 200
        yield c


def test_amt_over_websocket(client):
    with client.websocket_connect("/ws") as ws:
        ws.send_json({"op": "sub", "id": 1, "symbol": "ES", "spec": "5m"})
        ws.send_json({"op": "amt", "id": 1, "symbol": "ES", "minutes": 5})
        got = None
        for _ in range(500):
            m = ws.receive_json()
            if m["type"] == "amt":
                got = m
                break
        assert got and got["full"] and got["data"]["ready"]
        d = got["data"]
        assert d["dash"]["labels"][0] == "SESSION" and d["lines"]
        assert any(l[0] == "PIVOT" for l in d["lines"])             # the pasted levels feed the Toolkit
        assert "STALE LEVELS" in d["warning"]                        # levels dated Jan 15
        assert "past" in d and "history" in d and "samples" in d


def test_amt_settings_and_alert_switches(client):
    assert client.put("/api/amt/settings", json={"mvp_tf_mode": "Session"}).status_code == 200
    assert client.get("/api/amt").json()["overrides"] == {"mvp_tf_mode": "Session"}
    assert client.put("/api/amt/settings", json={"no_such_setting": 1}).status_code == 400
    r = client.put("/api/amt/alerts/es", json={"on": True})        # the mode is the Level alert mode setting
    assert r.status_code == 200 and r.json() == {"on": True}
    assert client.get("/api/amt").json()["alerts"]["ES"]["on"] is True
    # NinjaTrader's defaults are not stored as changes; the property grid's checks and ValidateSettings' apply
    ok = {"mvp_tf_mode": "Weekly", "LevelAlertMode": "Both", "vwap_enable": True, "vwap_col": 0xFF00FF00, "mvp_val_pct": 70.0,
          "ib_session": "0830-0900", "LevelsFile": "%USERPROFILE%\\Documents\\Tatanka\\levels\\{root}.txt", "tzguard_enable": False}
    assert client.put("/api/amt/settings", json=ok).json()["overrides"] == {k: v for k, v in ok.items() if k != "mvp_tf_mode"}
    for bad, msg in [({"alertcap_max": 300}, "exceeds its maximum (200)"), ({"tick_auto": "yes"}, "expected bool"),
                     ({"row_scale": "3x"}, "Invalid Profile Row Scale"), ({"ib_session": "8:30-9:30"}, "HHmm-HHmm"),
                     ({"mvp_calc_tf": "45"}, "Calc Precision"), ({"vwap_col": -1}, "not a color"),
                     ({"mvp_val_pct": 0}, "between 0 and 100"), ({"MaxStructures": 2.5}, "whole number"),
                     ({"lvl_text": "2026-01-05, 5000.25, 5010.25, 4990.25"}, "unknown")]:   # per symbol now
        r = client.put("/api/amt/settings", json=bad)
        assert r.status_code == 400 and msg in r.json()["detail"], (bad, r.json())
    d = client.get("/api/amt").json()
    assert d["overrides"]["LevelAlertMode"] == "Both" and len(d["schema"]) == 271
    assert {x["name"] for x in d["schema"]} == set(d["defaults"]) - {"lvl_text"}
    assert next(x for x in d["schema"] if x["name"] == "row_scale")["choices"][0] == "Auto"


def test_rth_vwap_points_go_out_once():
    r = AmtRunner("ES", 0.25, 5, amt.Settings({"vwap_enable": True}))
    for i, m in enumerate(synth(3, 5, datetime(2026, 10, 16, 10, 0))):
        r.add_minute(m)
    r.commit_ready()
    r.preview()
    first = r.payload(True, True, True, vwap_known=None)
    assert len(first["vwap"]) == r.vwap_total > 50 and first["style"]["vwap"] == [0xFFFFFFFF, 2, "Solid"]
    assert r.payload(False, False, False, vwap_known=r.vwap_total).get("vwap_add") is None
    assert r.payload(False, False, False, vwap_known=r.vwap_total - 3)["vwap_add"] == first["vwap"][-3:]
    assert "vwap" in r.payload(False, False, False, vwap_known=-5)          # unknown history: all of it again
    off = AmtRunner("ES", 0.25, 5, amt.Settings())
    for m in synth(1, 5, datetime(2026, 10, 16, 10, 0)):
        off.add_minute(m)
    off.commit_ready()
    off.preview()
    assert "vwap" not in off.payload(True, True, True) and off.payload(True, True, True)["style"]["price_tags"] is True


def _bars(start, end, price_at):
    """1-minute bars from start to end (CT, naive), each at price_at(t)."""
    from chartsrv.amt_runner import epoch_ms
    from chartsrv.models import Bar as MinuteBar
    t, out = start, []
    while t < end:
        p = price_at(t)
        out.append(MinuteBar(t=epoch_ms(t), o=p, h=p, l=p, c=p, v=10))
        t += __import__("datetime").timedelta(minutes=1)
    return out


def test_cl_runs_at_its_own_regular_hours():
    """CL's RTH is 08:00-13:30 CT: its settle is the 13:29 close, its IB 08:00-09:00, not ES's 15:00 and 08:30."""
    from types import SimpleNamespace
    from chartsrv import amt_hub, sessions
    from chartsrv.config import Instrument
    hub = amt_hub.AmtHub.__new__(amt_hub.AmtHub)
    hub.m = SimpleNamespace(instruments={"CL": Instrument("CL", "Crude", 0.01, 1000, "CL", rth=("08:00", "13:30"), calendar="energy")})
    hub.overrides, hub._settings = {}, amt_hub.engine_settings({})
    s = hub.settings_for("CL")
    assert (s.ib_session, s.amt_sess, s.pd_sess_time, s.ib_stop_time) == ("0800-0900", "1700-0800:123456", "0800-1330", "0800-1330")
    r = AmtRunner("CL", 0.01, 5, s, rth_session=sessions.move_session("0830-1500", sessions.rth_mover(("08:00", "13:30"))))
    price = lambda t: 61.0 if (t.hour, t.minute) >= (13, 30) or (t.hour, t.minute) < (8, 0) else 60.0 if (t.hour, t.minute) >= (9, 0) else 62.0   # noqa: E731
    for m in _bars(datetime(2026, 10, 19, 17, 0), datetime(2026, 10, 20, 15, 0), price):
        r.add_minute(m)
    r.commit_ready()
    assert r.engine.settle == 60.0                          # the 13:25-13:30 bar's close, not 15:00's 61
    assert (r.engine.ibH, r.engine.ibL) == (62.0, 62.0)     # 08:00-09:00
    assert hub.settings_for.__doc__ and amt_hub.engine_settings({}).ib_stop_time == "0830-1500"


def test_mlk_day_counts_toward_tuesday():
    """No settlement on MLK Day: its short session counts toward Tuesday's trade date, so Tuesday's prior day is
    Friday's and so is its settle."""
    from chartsrv import sessions
    from chartsrv.config import load_holidays, ROOT
    sessions.set_calendar(load_holidays(ROOT / "config"))
    r = AmtRunner("ES", 0.25, 5, amt.Settings(), calendar="equity")
    day = lambda t: {16: 7000.0, 19: 7100.0}.get(t.day, 7050.0)   # noqa: E731  Fri 7000, MLK Monday 7100
    for m in _bars(datetime(2026, 1, 15, 17, 0), datetime(2026, 1, 16, 16, 0), day) + \
            _bars(datetime(2026, 1, 18, 17, 0), datetime(2026, 1, 19, 12, 0), day) + \
            _bars(datetime(2026, 1, 19, 17, 0), datetime(2026, 1, 20, 9, 0), day):
        r.add_minute(m)
    r.commit_ready()
    e = r.engine
    assert e.td == datetime(2026, 1, 20) and e.settle == 7000.0 and (e.pdH, e.pdL) == (7000.0, 7000.0)
    assert sorted(r.days) == [datetime(2026, 1, 16), datetime(2026, 1, 20)]   # no MLK day of its own
    assert e.ovnH == 7100.0   # Monday's trading is Tuesday's overnight


def test_ovn_lines_stay_to_the_globex_open():
    """The OVN High/Low lines show from the RTH open to 17:00, then a new overnight starts."""
    r = AmtRunner("ES", 0.25, 5, amt.Settings())
    r.commit_visuals = True
    price = lambda t: 7100.0 if t.hour < 8 or t.hour >= 17 else 7050.0   # noqa: E731
    lines = {}
    for m in _bars(datetime(2026, 10, 19, 17, 0), datetime(2026, 10, 20, 17, 10), price):
        r.add_minute(m)
        r.commit_ready()
        f, _ = r.preview()
        if f is not None:
            lines[f.Bar.Start.strftime("%H:%M")] = {x.Name for x in f.Lines}
    assert "OVN High" in lines["09:00"] and "OVN High" in lines["15:55"] and "OVN Low" in lines["15:55"]
    assert "OVN High" not in lines["17:05"]
