"""Round 7 of the bug hunt, the round-6 gap alert and M-chart trend line fixes: a Gap settings save leaves each
symbol's gap alerts on or off and at the hours they have on the server (the page's copy of those is from its hello),
and gets past a symbol no longer an instrument; a trend line's alert switched on from an M chart keeps to the line that
chart draws, in its month bars, not by the clock."""
import shutil

import pytest

needs_node = pytest.mark.skipif(not shutil.which("node"), reason="needs node")
GAP_PARTS = ["hhmm", "gapRth", "gapRthOk", "GAP_RTH_BAD", "gapAlertsPut", "renderGapInfo", "putGapAlerts", "gapAlertsFollow",
             "GAP_SWITCH"]


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


# The page's gapAlertsFollow (a Gap settings save) with S.gapAlerts as given: what it sends, which the test then sends
# to the real server.
FOLLOW_JS = """
const calls = [], els = {};
const $ = (q) => (els[q] ||= { textContent: '', checked: false });
const api = async (method, url, body) => {
  calls.push([method, url, body]);
  const [path, query] = url.split('?'), sym = new URLSearchParams(query || '').get('symbol') || path.split('/').pop();
  if (sym !== 'alerts' && !S.instruments[sym]) throw new Error(`unknown symbol ${sym}`);   // app.py: 404
  return path.endsWith('/alerts') ? { alerts: {} } : { ...body };
};
const gapSpecOk = () => true, indReady = () => true;
const S = { symbol: 'ES', rth: false, st: { gap: true }, gaps: { gaps: [] }, gapError: '',
  instruments: { ES: { rth: ['08:30', '15:00'] }, NQ: { rth: ['08:30', '15:00'] } }, gapAlerts: COPY,
  gapSet: { RthStart: 83000, RthEnd: 150000, MinimumGapPoints: 2, AlertOnNewGap: true, AlertOnGapFill: false, EnabledAnalysis: true, days: 90 } };
/*APP*/
(async () => {
  let err = null;
  try { await gapAlertsFollow(S.gapSet, { AlertOnGapFill: false }); } catch (e) { err = e.message; }   // fill unticked there
  console.log(JSON.stringify({ calls, err }));
})();
"""


def follow(copy: dict) -> dict:
    import json

    from test_hunt_web_chart import run_app
    return run_app(GAP_PARTS, FOLLOW_JS.replace("COPY", json.dumps(copy)))


@needs_node
def test_a_gap_settings_save_keeps_the_alerts_switched_off_or_to_other_hours_on_another_page(client):
    body = {"new": True, "fill": True, "RthStart": 83000, "RthEnd": 150000, "MinimumGapPoints": 0.0}
    for sym in ("ES", "NQ"):
        assert client.put(f"/api/gaps/alerts/{sym}", json={**body, "on": True, "rth": False}).status_code == 200
    copy = client.get("/api/gaps/alerts").json()["alerts"]           # this chart's hello
    # then, on another chart (or the phone): NQ's gap alerts off, ES's switched on again from an RTH chart
    assert client.put("/api/gaps/alerts/NQ", json={**body, "on": False, "rth": False}).status_code == 200
    assert client.put("/api/gaps/alerts/ES", json={**body, "on": True, "rth": True}).status_code == 200
    out = follow(copy)                                               # this chart: Gap settings saved
    assert out["err"] is None
    for method, url, b in out["calls"]:
        assert client.request(method, url, json=b).status_code == 200, url
    got = client.get("/api/gaps/alerts").json()["alerts"]
    assert (got["ES"]["on"], got["ES"]["rth"], got["NQ"]["on"]) == (True, True, False)
    # this ES chart's settings are ES's alerts' (they keep their own on and hours), restarted with them; NQ's keep
    # theirs (round 14: each symbol's)
    assert (got["ES"]["MinimumGapPoints"], got["ES"]["fill"]) == (2, False)
    assert (got["NQ"]["MinimumGapPoints"], got["NQ"]["fill"]) == (0, True)
    from chartsrv.app import M
    assert M().gaps.watches["ES"].cfg["MinimumGapPoints"] == 2 and "NQ" not in M().gaps.watches


def test_the_shared_gap_settings_keep_each_alerts_on_and_hours(client):
    body = {"new": True, "fill": True, "RthStart": 83000, "RthEnd": 150000, "MinimumGapPoints": 0.0}
    assert client.put("/api/gaps/alerts/ES", json={**body, "on": True, "rth": True}).status_code == 200
    assert client.put("/api/gaps/alerts/CL", json={**body, "on": False, "rth": True}).status_code == 200
    from chartsrv.app import M
    M().gaps.alert_cfg["NQ"] = {"on": True, **body}                  # saved before the hours flag: all hours
    r = client.put("/api/gaps/alerts", json={**body, "MinimumGapPoints": 1.5, "on": False, "rth": False})
    assert r.status_code == 200
    a = r.json()["alerts"]
    assert {s: (c["on"], c.get("rth"), c["MinimumGapPoints"]) for s, c in a.items()} == {
        "ES": (True, True, 1.5), "CL": (False, True, 1.5), "NQ": (True, None, 1.5)}
    assert client.get("/api/gaps/alerts").json()["alerts"] == a
    assert client.put("/api/gaps/alerts", json={**body, "RthEnd": 83000}).status_code == 400


def test_a_gap_settings_save_reaches_alerts_of_a_symbol_no_longer_an_instrument(client):
    body = {"new": True, "fill": True, "RthStart": 83000, "RthEnd": 150000, "MinimumGapPoints": 0.0}
    assert client.put("/api/gaps/alerts/ES", json={**body, "on": True}).status_code == 200
    from chartsrv.app import M
    g = M().gaps
    # RTY's, left on when it was removed in Connections > Instruments, stored before NQ's
    g.alert_cfg["RTY"] = {"on": True, **body}
    g.alert_cfg["NQ"] = {"on": True, **body}
    assert "RTY" not in client.get("/api/gaps/alerts").json()["alerts"]
    r = client.put("/api/gaps/alerts", json={**body, "MinimumGapPoints": 2.0})
    assert r.status_code == 200 and set(r.json()["alerts"]) == {"ES", "NQ"}
    # RTY's takes them too, for when it is back (k-gd8-4), and still runs no watch
    assert g.alert_cfg["NQ"]["MinimumGapPoints"] == 2 and g.alert_cfg["RTY"]["MinimumGapPoints"] == 2
    assert "RTY" not in g.watches


@needs_node
def test_the_page_follows_with_a_symbol_no_longer_an_instrument_in_its_copy():
    on = {"on": True, "rth": False, "MinimumGapPoints": 0}
    out = follow({"ES": on, "RTY": on, "NQ": on})
    assert out["err"] is None
    assert [(m, u) for m, u, _ in out["calls"]] == [("PUT", "/api/gaps/alerts?symbol=ES")]
    sent = out["calls"][0][2]
    assert "on" not in sent and "rth" not in sent and sent["MinimumGapPoints"] == 2 and sent["fill"] is False


# ---- k-ts7-3: a trend line's alert switched on from an M chart keeps to the line that chart draws --------------------

def m_bars(n: int, first: int, last: int) -> list[dict]:
    """An nM chart's bars from `first` to `last` (years): one per n months from January, each stamped at the 17:00 CT
    open before the group's first day (bars.calendar_bucket)."""
    from datetime import date

    from chartsrv import sessions
    return [{"t": sessions.day_open(date(first + m // 12, m % 12 + 1, 1))} for m in range(0, (last - first) * 12, n)]


@needs_node
def test_an_alert_switched_on_from_an_m_chart_follows_the_line_it_draws():
    """On 1M, 3M and 12M charts, through leap and non-leap Februaries: the alert switched on there records the chart's
    months, and the server's level and the Alerts tab's "now" are the line the chart draws, at each bar's open and
    inside it, years on. By the clock (as before) a line from February to March 2022 was 5 bars off 5 years on."""
    from chartsrv import drawings
    from test_hunt5_trendline import off, run, saved
    from test_redesign_trendline_alert import H, trend
    D = 24 * H
    # n: (the points' bars, counted from January 2010: Feb-Mar 2022, Feb-Mar 2016 ...; how many bars on to check; times
    # inside a bar)
    cases = {
        1: ([(145, 146), (73, 74), (108, 114), (157, 167), (24, 25)], (1, 12, 36, 60, 72),
            (0, 14 * D + 10 * H, 27 * D + 20 * H)),
        3: ([(24, 25), (48, 50), (39, 45), (8, 9)], (1, 4, 12, 24), (0, 45 * D, 88 * D + 6 * H)),
        12: ([(6, 7), (11, 13), (9, 10), (2, 3)], (1, 3, 6, 10), (0, 180 * D, 364 * D + 5 * H)),
    }
    for n, (pairs, ahead, inside) in cases.items():
        bars = m_bars(n, 2010, 2041)
        lines = []
        for k, (ia, ib) in enumerate(pairs):
            shift = (k % 2) * inside[1]     # every other line's points inside their bars
            d = off(trend(bars[ia]["t"] + shift, 4000.0, bars[ib]["t"] + shift // 3, 4000.0 + 37.5 * (ib - ia),
                          extendRight=True, extendLeft=k == 3))
            times = [bars[ib + e]["t"] + x for e in ahead for x in inside]
            times += [bars[ia]["t"] + x for x in inside if x >= shift]
            times += [bars[ia - 2]["t"] + inside[1]] if k == 3 else []
            lines.append({"d": d, "times": times})
        out = run("""out.alert = switchOn(IN.lines[0].d);
out.res = IN.lines.map((L) => ({ at: drawn(L.d, L.times).at,
  l1: D.timeToLogical(L.d.pts[0].t), l2: D.timeToLogical(L.d.pts[1].t) }));""",
                  bars=bars, per=n * 31 * D, byClock=True, months=n, lines=lines, lo=1000, hi=40000)
        assert out["alert"] == {"on": True, "direction": "cross", "repeat": False, "months": n}
        params = [saved(L["d"], out["alert"]) for L in lines]
        shown = run("out.now = IN.ps.map((p, i) => now(p, IN.lines[i].times));", ps=params, lines=lines)["now"]
        for L, p, r, tab in zip(lines, params, out["res"], shown):
            bar = (p["p2"] - p["p1"]) / (r["l2"] - r["l1"])      # the line's rise a bar on this chart
            server = [drawings.line_price(p, t, "equity") for t in L["times"]]
            assert max(abs(s - c) / bar for s, c in zip(server, r["at"])) < 1e-6, (n, L["d"]["pts"])
            assert max(abs(s - c) / bar for s, c in zip(server, tab)) < 1e-9, (n, L["d"]["pts"])
        if n == 1:   # February to March 2022, by the clock: 5.26 bars off 60 bars on
            clock = {k: v for k, v in params[0].items() if k != "months"} | {"clock": True}
            t, c = lines[0]["times"][10], out["res"][0]["at"][10]                # 60 bars and 14 days on
            assert abs(drawings.line_price(clock, t) - c) / 37.5 > 5
    # an alert switched on from an M chart before (by the clock) keeps it, as saved: nothing re-arms on the upgrade
    bars = m_bars(1, 2022, 2028)
    old = saved(trend(bars[1]["t"], 2000.0, bars[2]["t"], 2100.0, extendRight=True),
                {"on": True, "direction": "cross", "repeat": False, "clock": True})
    t = bars[62]["t"] + 14 * D
    assert old["clock"] is True and "months" not in old
    assert drawings.line_price(old, t) == pytest.approx(2000 + 100 * (t - bars[1]["t"]) / (bars[2]["t"] - bars[1]["t"]))
    new = {k: v for k, v in old.items() if k != "clock"} | {"months": 1}
    assert drawings.line_price(new, t) == pytest.approx(2000 + 100 * (61 + 14 / 31))   # the chart's 61 bars and 14 days on
    plain = drawings.clean(trend(bars[1]["t"], 2000.0, bars[2]["t"], 2100.0), "t")
    for bad in (0, 13, "1", True, 1.5):
        with pytest.raises(ValueError):
            drawings.clean({**plain, "alert": {"on": True, "months": bad}}, "t")
