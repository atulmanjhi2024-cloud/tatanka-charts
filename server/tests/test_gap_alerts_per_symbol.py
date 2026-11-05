"""Gap Analysis' alert settings are each symbol's (round 14, as the AMT Toolkit's): every ES chart has ES's alert
switches (Alert on new gap, Alert on full gap fill), every NQ chart NQ's, and each symbol's gap alerts run on their own
switches, RTH times and minimum gap size, so a change on an NQ chart leaves ES's alerts, their watch and what ES charts
show as they are. Before, the switches were one set for every chart and symbol, and any chart's Gap settings save gave
every symbol's alerts its RTH times and minimum size (in points, which differ by instrument). An install's switches
from before carry over to every symbol once."""
import asyncio
from types import SimpleNamespace

import chartsrv.gaps_service as gs
from chartsrv import sessions
from chartsrv.app import GapAlertsIn
from chartsrv.config import Instrument
from chartsrv.store import Store
from test_hunt5_rthstudies import MIN, GapMarket, gap_minutes, ms
from test_hunt13_amt import client, needs_node, pages  # noqa: F401  (client: the app, as a fixture)
from test_review_fixes import build_market

TIMES = {"RthStart": 83000, "RthEnd": 150000}
ON = {"on": True, "rth": False, **TIMES, "MinimumGapPoints": 0.0}
MIDDAY = {"RthStart": 110000, "RthEnd": 120000}   # fine on ES; 10:30-10:30 on CL's 08:00-13:30


# ---- the alerts: NQ's change leaves ES's as they are --------------------------------------------------------------------

def test_an_nq_gap_settings_save_leaves_es_alerts_and_their_watch_alone(monkeypatch):
    """ES's and NQ's gap alerts on, all hours, minimum size 0 and both switches on (test_hunt5_rthstudies' minutes: a
    50-point gap up on Monday, filled at Monday's close, a 10-point gap down on Tuesday). An NQ chart saves its Gap
    settings with 'Alert on new gap' unticked and a minimum of 20 points: NQ's alerts send its fill only (the 10 points
    are no gap at 20), ES's watch goes on untouched and still sends all three (before: every symbol's took them, so ES
    lost its new-gap alerts and its 10-point gap)."""
    m1, split = gap_minutes(), ms(2026, 10, 19, 8)
    monkeypatch.setattr(gs, "iter_bars_1m", lambda db, sym, a, b: [x for x in m1 if a <= x.t < b])
    monkeypatch.setattr(gs, "LIVE_BAR_AGE_MS", 10**15)        # the test's bars are not from today
    market = GapMarket(split)
    market.instruments["NQ"] = Instrument(symbol="NQ", name="NQ", tick_size=0.25, point_value=20, rth=("08:30", "15:00"))
    svc = gs.GapService(market)
    sent = []

    async def go():
        for sym in ("ES", "NQ"):
            svc.set_alerts(sym, GapAlertsIn(**ON).model_dump())
            await svc.watches[sym].ready.wait()
        es = svc.watches["ES"]
        svc.follow({"new": False, **TIMES, "MinimumGapPoints": 20.0}, "NQ")   # PUT /api/gaps/alerts?symbol=NQ
        await svc.watches["NQ"].ready.wait()
        assert svc.watches["ES"] is es and es.cfg == {**ON, "new": True, "fill": True}
        assert svc.alert_cfg["NQ"] == {**ON, "MinimumGapPoints": 20.0, "new": False, "fill": True}
        assert (svc.switches_of("ES"), svc.switches_of("NQ")) == ({"new": True, "fill": True}, {"new": False, "fill": True})
        for b in m1:
            if b.t >= split:
                for sym in ("ES", "NQ"):
                    n = len(svc.m.store.events)
                    svc.on_minute(sym, b, True)
                    sent.extend(sessions.to_ct(b.t + MIN).strftime("%a %H:%M ") + e["message"] for e in svc.m.store.events[n:])
    asyncio.run(go())
    assert [x for x in sent if "[ES]" in x] == [
        "Mon 08:31 [ES] GAP UP 50.00 pts | Open: 6050.00 | RTH close: 6000.00",
        "Mon 15:01 [ES] GAP UP FILLED | 50.00 pts in 390 bars",
        "Tue 08:31 [ES] GAP DOWN 10.00 pts | Open: 6040.00 | RTH close: 6050.00"]
    assert [x for x in sent if "[NQ]" in x] == ["Mon 15:01 [NQ] GAP UP FILLED | 50.00 pts in 390 bars"]


# ---- the migration: once, to every symbol -------------------------------------------------------------------------------

def test_the_switches_from_before_go_to_every_symbol_once(tmp_path, monkeypatch):
    """A server from before: one set of alert switches ('Alert on full gap fill' off), ES's gap alerts on (its stored
    config still with fill on from before they were shared, its own minimum size 2), CL's off with its own hours, and
    YM's kept on while it is off Instruments. Its first start gives every instrument, and YM, those switches (so ES's
    alerts still send no fills and NQ, never switched on, starts with fill off as the dialog showed), each config keeps
    its own RTH times and minimum size, and nothing is said as turned off. Then NQ's change, an older version's switches
    written again, and a restart: nothing is copied again, NQ keeps its own."""
    path = tmp_path / "chart.db"
    s = Store(path)
    s.set_setting("gap_alert_switches", {"new": True, "fill": False})
    s.set_setting("gap_alert_notice", [])
    es = {"on": True, "rth": True, **TIMES, "MinimumGapPoints": 2.0, "new": True, "fill": True}
    cl = {"on": False, "rth": False, "RthStart": 80000, "RthEnd": 133000, "MinimumGapPoints": 0.5, "new": True, "fill": True}
    s.set_setting("gap_alerts", {"ES": es, "CL": cl, "YM": {**ON, "new": True, "fill": True}})
    s.close()
    m, _ = build_market(tmp_path, monkeypatch)
    g = m.gaps
    off = {"new": True, "fill": False}
    assert "YM" not in m.instruments and {"ES", "NQ", "CL", "GC", "MES", "MNQ"} <= set(m.instruments)
    assert g.symbol_switches == {sym: off for sym in {*m.instruments, "YM"}}
    assert m.store.get_setting(gs.SYMBOL_SWITCHES) == g.symbol_switches and g.notice == []
    assert g.switches()["NQ"] == off and "NQ" not in g.alert_cfg
    assert g.alert_cfg["ES"] == {**es, "fill": False} and g.alert_cfg["CL"] == {**cl, "fill": False}
    assert g.alert_cfg["YM"]["fill"] is False
    # once: NQ's own change, an older version's switches stored again, then a restart
    g.follow({"fill": True}, "NQ")
    m.store.set_setting("gap_alert_switches", {"new": False, "fill": False})
    stored = m.store.get_setting(gs.SYMBOL_SWITCHES)
    m.store.close()
    m2, _ = build_market(tmp_path, monkeypatch)
    g2 = m2.gaps
    assert m2.store.get_setting(gs.SYMBOL_SWITCHES) == stored == g2.symbol_switches
    assert g2.switches_of("NQ") == {"new": True, "fill": True} and g2.switches_of("ES") == g2.switches_of("YM") == off
    assert g2.alert_cfg["ES"] == {**es, "fill": False} and g2.notice == []


def test_a_new_install_stores_each_symbols_switches_at_once(tmp_path):
    """Nothing set before: every instrument's switches are on (NinjaTrader's defaults), stored at once, so a restart
    copies nothing again: a symbol's stored ones stay, and one with none stored (added later) has them on."""
    st = Store(tmp_path / "chart.db")
    inst = SimpleNamespace(rth=("08:30", "15:00"))
    g = gs.GapService(SimpleNamespace(store=st, instruments={"ES": inst, "NQ": inst}))
    on = {"new": True, "fill": True}
    assert g.switches() == {"ES": on, "NQ": on} and st.get_setting(gs.SYMBOL_SWITCHES) == {"ES": on, "NQ": on}
    st.set_setting(gs.SYMBOL_SWITCHES, {"ES": {"new": False, "fill": True}})
    again = gs.GapService(SimpleNamespace(store=st, instruments={"ES": inst, "NQ": inst}))
    assert again.switches() == {"ES": {"new": False, "fill": True}, "NQ": on} and again.notice == []


# ---- the API ------------------------------------------------------------------------------------------------------------

def test_the_api_reads_and_changes_one_symbols_gap_alert_settings(client):
    """PUT /api/gaps/alerts?symbol=nq: NQ's switches, and NQ's alerts take the RTH times and minimum size; ES's config
    and switches stay; GET and the broadcast give each symbol's (?symbol=NQ: NQ's only). An unknown symbol is refused
    (404), as are bad times, or times NQ's or CL's own hours make no session of (400), nothing changed; ES's own hours
    take them for ES. No symbol: every symbol's, as before."""
    for sym in ("ES", "NQ"):
        assert client.put(f"/api/gaps/alerts/{sym}", json=ON).status_code == 200
    start = client.get("/api/gaps/alerts").json()
    es = start["alerts"]["ES"]
    r = client.put("/api/gaps/alerts?symbol=nq", json={"fill": False, "RthStart": 90000, "RthEnd": 150000, "MinimumGapPoints": 20.0})
    assert r.status_code == 200
    d = client.get("/api/gaps/alerts").json()
    assert d["alerts"]["NQ"] == {**start["alerts"]["NQ"], "fill": False, "RthStart": 90000, "MinimumGapPoints": 20.0}
    assert d["alerts"]["ES"] == es and d["switches"]["ES"] == {"new": True, "fill": True}
    assert d["switches"]["NQ"] == {"new": True, "fill": False} and set(d["switches"]) == set(client.get("/api/instruments").json())
    told = [x for x in client.heard if x["type"] == "gap_alerts"][-1]
    assert told == {"type": "gap_alerts", "alerts": d["alerts"], "switches": d["switches"]}
    one = client.get("/api/gaps/alerts?symbol=NQ").json()
    assert one["alerts"] == {"NQ": d["alerts"]["NQ"]} and one["switches"] == {"NQ": d["switches"]["NQ"]}
    assert client.get("/api/gaps/alerts?symbol=ZZZ").status_code == 404
    n = len(client.heard)
    assert client.put("/api/gaps/alerts?symbol=ZZZ", json={"fill": True}).status_code == 404
    assert client.put("/api/gaps/alerts?symbol=NQ", json={"fill": True, "RthStart": 90000}).status_code == 400
    assert client.put("/api/gaps/alerts?symbol=NQ", json={"RthStart": 90000, "RthEnd": 90000}).status_code == 400
    r = client.put("/api/gaps/alerts?symbol=CL", json={"fill": False, **MIDDAY})
    assert r.status_code == 400 and "no session on CL" in r.json()["detail"], r.text
    assert client.get("/api/gaps/alerts").json() == {**d, "watches": client.get("/api/gaps/alerts").json()["watches"]}
    assert len(client.heard) == n
    r = client.put("/api/gaps/alerts?symbol=ES", json=MIDDAY).json()   # ES's own hours: an hour of its day
    assert (r["alerts"]["ES"]["RthStart"], r["alerts"]["NQ"]["RthStart"]) == (110000, 90000) and "CL" not in r["alerts"]
    r = client.put("/api/gaps/alerts", json={"new": False}).json()   # no symbol: every symbol's
    assert all(sw["new"] is False for sw in r["switches"].values()) and r["switches"]["NQ"]["fill"] is False
    assert (r["alerts"]["ES"]["new"], r["alerts"]["ES"]["fill"], r["alerts"]["NQ"]["MinimumGapPoints"]) == (False, True, 20.0)


# ---- the pages: the dialog shows and saves the chart's symbol's ----------------------------------------------------------

BOTH_ON = """
  const cfg = { on: true, new: true, fill: true, rth: false, RthStart: 83000, RthEnd: 150000, MinimumGapPoints: 0 };
  SRV.gap = { ES: { ...cfg }, NQ: { ...cfg } };
"""


@needs_node
def test_the_dialog_on_an_es_chart_shows_es_gap_switches_after_an_nq_change():
    """The user's case, as for the AMT Toolkit: on an NQ tab, 'Alert on full gap fill' unticked and a minimum gap size
    of 20, OK. Only NQ's change (the PUT says NQ): the ES tab of the same page and an ES chart on another page show
    ES's, ticked, ES's alerts keep fills and minimum 0, and the NQ tab shows NQ's (before: one set for both, so ES's
    fill alerts stopped too, and ES's alerts took NQ's 20 points). The switches are marked with the symbol."""
    out = pages(BOTH_ON + """
  const A = page(), B = page();   // A: the workspace's page, on its NQ tab; B: another page's ES chart
  A.S.symbol = 'NQ';
  await A.hello(); await B.hello();
  A.indDlg.open(true, 'gap');
  A.edit('gap', 'AlertOnGapFill', false);
  A.edit('gap', 'MinimumGapPoints', 20);
  await A.ok();
  heard(A); heard(B);
  A.indDlg.open(true, 'gap');
  out.nq = A.shows('gap', 'AlertOnGapFill');
  const row = A.find(A.root.sel['.ind-body'], (e) => e.dataset?.name === 'AlertOnGapFill');
  out.mark = row.children[0].children.map((e) => e.textContent);
  A.renderGapInfo(); out.nqInfo = A.$('#gap-info').textContent;
  A.S.symbol = 'ES'; A.indDlg.refresh();   // the ES tab
  out.esTab = A.shows('gap', 'AlertOnGapFill');
  B.indDlg.open(true, 'gap');
  out.es = B.shows('gap', 'AlertOnGapFill');
  B.renderGapInfo(); out.esInfo = B.$('#gap-info').textContent;
  out.server = [copy(SRV.switches), SRV.gap.ES.fill, SRV.gap.ES.MinimumGapPoints, SRV.gap.NQ.fill, SRV.gap.NQ.MinimumGapPoints];
  out.sent = calls.filter((c) => c[1].startsWith('/api/gaps/alerts')).map((c) => c[1]);
""")
    assert out["server"] == [{"ES": {"new": True, "fill": True}, "NQ": {"new": True, "fill": False}}, True, 0, False, 20]
    assert out["sent"] == ["/api/gaps/alerts?symbol=NQ"]
    assert out["nq"] is False and out["mark"] == [" (NQ)"] and out["nqInfo"].endswith(" Alerts: new gap.")
    assert out["esTab"] is True and out["es"] is True and out["esInfo"].endswith(" Alerts: new gap + fill.")


@needs_node
def test_apply_to_all_charts_on_an_es_chart_leaves_nq_gap_alerts_alone():
    """NQ has 'Alert on full gap fill' off and a minimum of 20 points. On an ES chart, 'Alert on new gap' unticked and
    a minimum of 5, then Apply to all charts: the minimum is every chart's starting setting and ES's alerts', the
    switch ES's only, and NQ's switches and alerts stay as they were (before: every symbol's took this chart's switches
    and 5 points). The question says so."""
    out = pages(BOTH_ON + """
  SRV.switches.NQ = { new: true, fill: false }; SRV.gap.NQ = { ...SRV.gap.NQ, fill: false, MinimumGapPoints: 20 };
  const B = page();
  await B.hello();
  B.indDlg.open(true, 'gap');
  B.edit('gap', 'AlertOnNewGap', false);
  B.edit('gap', 'MinimumGapPoints', 5);
  await B.applyAll();
  out.server = [copy(SRV.switches), copy(SRV.gap)];
  out.start = copy(B.S.ind.overrides.gap);
  out.sent = calls.filter((c) => c[1].startsWith('/api/gaps/alerts')).map((c) => c[1]);
  out.note = B.indAdapter('gap').allNote?.() || '';   // what Apply to all charts' question adds
""")
    switches, alerts = out["server"]
    assert switches == {"ES": {"new": False, "fill": True}, "NQ": {"new": True, "fill": False}}
    assert (alerts["ES"]["new"], alerts["ES"]["MinimumGapPoints"]) == (False, 5)
    assert (alerts["NQ"]["new"], alerts["NQ"]["fill"], alerts["NQ"]["MinimumGapPoints"]) == (True, False, 20)
    assert out["start"] == {"MinimumGapPoints": 5} and out["sent"] == ["/api/gaps/alerts?symbol=ES"]
    assert "alert switches go to every ES chart only" in out["note"] and "only ES's gap alerts take the RTH" in out["note"]


@needs_node
def test_gap_switch_edits_stay_with_the_symbol_they_were_made_for():
    """A layout's page edits the chart last clicked's: 'Alert on full gap fill' unticked on ES's and a minimum of 3,
    then NQ's chart is clicked before OK. The dialog shows NQ's (ticked) and OK saves the chart's own minimum, which
    NQ's alerts take: neither symbol's switches change (before: ES's edit went to every symbol)."""
    out = pages(BOTH_ON + """
  const L = page();
  await L.hello();
  L.indDlg.open(true, 'gap');
  L.edit('gap', 'AlertOnGapFill', false);
  L.edit('gap', 'MinimumGapPoints', 3);
  L.S.symbol = 'NQ'; L.indDlg.refresh();
  out.shows = L.shows('gap', 'AlertOnGapFill');
  await L.ok();
  out.server = [copy(SRV.switches), SRV.gap.ES.MinimumGapPoints, SRV.gap.NQ.MinimumGapPoints];
  out.sent = calls.filter((c) => c[1].startsWith('/api/gaps/alerts')).map((c) => [c[1], c[2]]);
  out.own = L.own.gap;
""")
    assert out["shows"] is True
    assert out["server"] == [{"ES": {"new": True, "fill": True}, "NQ": {"new": True, "fill": True}}, 0, 3]
    assert out["sent"] == [["/api/gaps/alerts?symbol=NQ", {"RthStart": 83000, "RthEnd": 150000, "MinimumGapPoints": 3}]]
    assert out["own"] == {"MinimumGapPoints": 3}


@needs_node
def test_a_gap_switch_a_tab_had_turned_off_goes_to_its_symbol_only():
    """A tab from before the switches were shared, NQ's, had 'Alert on full gap fill' off; an ES tab had nothing of the
    kind. Moved out once: off on NQ's charts, ES's as they were, and the message says whose (before: off for every
    symbol)."""
    out = pages("""
  const A = page();
  A.S.tabs = [{ sym: 'NQ', own: { set: { gap: { AlertOnGapFill: false, ShowGapZones: false } } } },
    { sym: 'ES', own: { set: { gap: { ShowGapZones: false } } } }];
  await A.hello();
  await A.moveSharedOut();
  out.server = copy(SRV.switches);
  out.sets = A.S.tabs.map((t) => t.own.set);
  out.toasts = A.toasts;
""")
    assert out["server"] == {"ES": {"new": True, "fill": True}, "NQ": {"new": True, "fill": False}}
    assert out["sets"] == [{"gap": {"ShowGapZones": False}}, {"gap": {"ShowGapZones": False}}]
    assert len(out["toasts"]) == 1
    assert "Off on every NQ chart, as this chart had it: Gap Analysis: Alert on full gap fill." in out["toasts"][0]
