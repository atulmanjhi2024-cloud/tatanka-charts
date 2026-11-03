"""TPO Profile port: session math, rows, POC/value area, the chart-bar rebuild and /api/tpo.
Statement-for-statement parity with the original C# is checked by tests/tpo_parity.py (needs mono)."""
from datetime import datetime

import pytest

from amt_parity import synth
from chartsrv import tpo
from chartsrv.amt_runner import ct, epoch_ms
from chartsrv.bars import make_series, parse_spec
from chartsrv.tpo_service import Daily, chart_bars, payload, run

P = tpo.TpoProfile


def test_settings_validation():
    assert tpo.settings({})["SessionTime"] == "0830-1500"
    assert tpo.settings({"BlockMinutes": 15, "ShowLive": True})["BlockMinutes"] == 15
    for bad, msg in [({"BlockMinutes": 3}, "5-240"), ({"ShowLive": 1}, "true or false"), ({"LetterDays": "3"}, "whole"),
                     ({"Nope": 1}, "unknown"), ({"SessionTime": 830}, "text")]:
        with pytest.raises(ValueError, match=msg):
            tpo.settings(bad)


def test_session_parsing_matches_the_csharp_rules():
    assert P.ParseSession("0830-1500") == (True, 510, 900)
    assert P.ParseSession("830-1500 ") == (True, 510, 900)      # 3 digits get a leading 0, spaces trimmed
    assert P.ParseSession("08:30-15:00")[0] is False
    assert P.ParseSession("1700-0830") == (True, 1020, 510)
    x = P(tpo.settings({"SessionTime": "1700-0830"}), 0.25)
    assert x.IsInSession(datetime(2026, 10, 15, 2, 0)) and not x.IsInSession(datetime(2026, 10, 15, 12, 0))
    assert x.GetSessionOpen(datetime(2026, 10, 15, 2, 0)) == datetime(2026, 10, 14, 17, 0)
    rth = P(tpo.settings({}), 0.25)
    assert not rth.IsInSession(datetime(2026, 10, 15, 8, 30))   # (start, end] on the close stamp
    assert rth.IsInSession(datetime(2026, 10, 15, 8, 35)) and rth.IsInSession(datetime(2026, 10, 15, 15, 0))
    assert P(tpo.settings({"SessionTime": "junk"}), 0.25).IsInSession(datetime(2026, 10, 15, 3, 0))


def test_poc_tie_goes_to_the_middle_then_lower_and_value_area_70pct():
    x = P(tpo.settings({}), 0.25)
    rows = {0: [0], 1: [0, 1], 2: [0, 1, 2], 3: [1], 4: [1, 2, 3], 5: [2, 3, 4], 6: [3]}
    p = x.BuildProfileFromMap(rows, datetime(2026, 1, 1), datetime(2026, 1, 1), 1.0)
    assert p.MaxCount == 3 and p.PocPrice == 2.0            # rows 2, 4 and 5 tie; 2 and 4 are equally near 3
    assert P.GetValueArea(p) == (4.0, 0.0)                  # 14 TPOs, target 10: below on ties, then up
    assert P.GetLetter(0) == "A" and P.GetLetter(26) == "a" and P.GetLetter(52) == "?"


def test_nt_atr_matches_ninjatrader_warmup():
    a = tpo.NtAtr(10)
    assert a.add(10, 0, 5) == 10
    assert a.add(12, 6, 11) == (10 + 7) / 2                 # TR = max(12-6, |12-5|, |6-5|) = 7
    for _ in range(30):
        a.add(12, 6, 11)
    assert abs(a.values[-1] - 6) < 0.05


@pytest.mark.parametrize("spec", ["5m", "7m", "1h", "4h"])
def test_chart_bars_match_the_chart_series(spec):
    minutes = parse_spec(spec).size // 60
    m1 = synth(6, 2, datetime(2026, 10, 16, 12, 0))
    series = make_series(parse_spec(spec), 0.25, 2, 100_000)
    for b in m1:
        series.on_bar(b)
    ours = chart_bars(m1, minutes)
    assert [(b[0], b[5], b[2], b[3], b[6]) for b in ours] == [(b.t, b.o, b.h, b.l, b.c) for b in series.bars]
    for k, close, *_ in ours:                               # close stamps: one bar later, cut at 16:00
        c = ct(close)
        assert close - k <= minutes * 60_000 and (close - k == minutes * 60_000 or (c.hour, c.minute) == (16, 0))


def _run(s, end=datetime(2026, 10, 16, 12, 7), now=None, days=25):
    now = now or epoch_ms(end)
    m1 = synth(days, 4, end)
    daily = Daily()
    daily.add_minutes(m1, 2**62)
    eng, open_of = run(tpo.settings(s), 0.25, 5, m1, daily, now)
    return eng, open_of, payload(eng, open_of, 5)


def test_payload_places_letters_from_the_first_session_bar():
    eng, open_of, d = _run({"ShowLive": True, "ShowValueArea": True})
    assert [p["mode"] for p in d["profiles"]] == ["P"] * 15 + ["L"] * 3 + ["L"]
    live = d["profiles"][-1]
    assert live["live"] and ct(live["x0"]) == datetime(2026, 10, 16, 8, 30)   # the 08:30-08:35 bar
    first = d["profiles"][-2]
    assert ct(first["x0"]) == datetime(2026, 10, 15, 8, 30) and first["cols"] == first["max"] + 5
    assert ct(first["poc_end"]) == ct(first["x0"] + first["cols"] * 300_000)
    assert max(len(r[1]) for r in first["rows"]) == first["max"] and first["va"][0] >= first["poc"] >= first["va"][1]
    assert all(isinstance(r[1], int) for r in d["profiles"][0]["rows"])         # outlines carry counts only
    assert ct(d["through"]) == datetime(2026, 10, 16, 12, 0)                    # last closed bar


def test_forming_bar_is_not_processed():
    now = epoch_ms(datetime(2026, 10, 16, 12, 3))       # inside the 12:00-12:05 bar
    _, _, d = _run({}, end=datetime(2026, 10, 16, 12, 4), now=now)
    assert ct(d["through"]) == datetime(2026, 10, 16, 11, 55)


def test_extended_poc_runs_to_the_next_profile_end():
    eng, open_of, d = _run({"ExtendPoc": True})
    a, b = d["profiles"][-2], d["profiles"][-1]           # Oct 14 and Oct 15
    assert ct(a["poc_end"]) == datetime(2026, 10, 15, 14, 55)   # the open of the next session's last bar
    assert ct(b["poc_end"]) == datetime(2026, 10, 16, 12, 5)    # last session: one bar past the last closed bar


def test_finished_session_keeps_its_row_height_when_the_atr_changes():
    """All-hours mode saves at the 17:00 open, after the daily bar moved the ATR: the C# re-prices the rows
    with the new height; the port keeps the height they were keyed with."""
    eng, _, _ = _run({"FilterByTime": False, "TargetRows": 100}, days=30)
    for p in eng.saved:
        assert all(abs(r.Price - 6800) < 1500 for r in p.Rows)
    assert len({p.TickStep for p in eng.saved}) > 1             # the row height did change along the way


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("CHART_DB", str(tmp_path / "chart.db"))
    monkeypatch.setenv("CHART_LEVELS_DIR", str(tmp_path))
    monkeypatch.setenv("CHART_FEED", "sim")
    monkeypatch.setenv("CHART_SIM_WARMUP_MIN", "600")
    from fastapi.testclient import TestClient
    from chartsrv.app import app
    with TestClient(app) as c:
        yield c


def test_tpo_endpoint(client):
    import time
    for _ in range(100):                                    # the sim feed back-fills its history first
        r = client.get("/api/tpo", params={"symbol": "es", "spec": "5m", "FilterByTime": "false", "ShowLive": "true"})
        assert r.status_code == 200
        d = r.json()
        if d["profiles"]:
            break
        time.sleep(0.1)
    assert d["ready"] and d["minutes"] == 5 and d["col"] == 300_000 and d["profiles"]
    assert d["profiles"][-1]["live"]
    for params, code in [({"spec": "2000t"}, 400), ({"spec": "1D"}, 400), ({"spec": "5m", "BlockMinutes": "1"}, 400),
                         ({"spec": "5m", "ShowLive": "yes"}, 400), ({"spec": "5m", "Bogus": "1"}, 400),
                         ({"spec": "1h"}, 200)]:
        assert client.get("/api/tpo", params={"symbol": "ES", **params}).status_code == code, params
    assert client.get("/api/tpo", params={"symbol": "ZZ", "spec": "5m"}).status_code == 404
