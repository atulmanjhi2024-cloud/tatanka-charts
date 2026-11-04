"""The AMT Toolkit's alert settings are each symbol's (round 14): every ES chart has ES's, every NQ chart NQ's, and each
symbol's alert runner runs on its own, so an alert switch changed on an NQ chart leaves ES's alerts, runners and charts
as they are. Before, they were one set for every chart and symbol: unticking one on an NQ tab unticked it on the ES tabs
of the same workspace too. The levels file stays one setting for every chart. An install's settings from before carry
over to every symbol once."""
import asyncio
from types import SimpleNamespace

from chartsrv import amt_hub, sessions
from chartsrv.market import Client
from chartsrv.store import Store
from test_bug_hunt import _warm
from test_hunt13_amt import client, needs_node, pages  # noqa: F401  (client: the app, as a fixture)
from test_review_fixes import build_market

OLD = {"cc_alert_80": False, "lvl_zoneWidthTicks": 12, "LevelAlertMode": "Both", "alertcap_max": 50}   # alert settings


def _sent(c) -> list:
    out = []
    while not c.outbox.empty():
        out.append(c.outbox.get_nowait()["type"])
    return out


async def _alerting(m, *syms):
    """Waits for the symbols' alert runners to be loaded."""
    for _ in range(3000):
        es = [m.amt.entries.get(m.amt.alert_key(s)) for s in syms]
        if all(e is not None and e.ready.is_set() for e in es):
            return es
        await asyncio.sleep(0.01)
    raise AssertionError("the alert runners did not load")


# ---- the runners: NQ's change leaves ES's as they are ------------------------------------------------------------------

def test_an_nq_alert_switch_change_leaves_es_alert_runner_and_charts_alone(tmp_path, monkeypatch):
    """ES's and NQ's AMT alerts on, an ES 5m chart open. On an NQ chart: Alert 80% Rule Confirmed unticked, Alert VWAP
    Cross ticked, a wider zone. NQ's alert runner starts again on them (with the RTH VWAP it now needs); ES's alert
    runner, the ES chart's runner and what they run on stay as they were, and the ES chart is not told to reload
    (before: one set for all, so ES's alerts lost the 80% rule and every chart reloaded)."""
    sessions.set_calendar({"days": []})
    monkeypatch.setenv("CHART_WORKERS", "0")

    async def go():
        m, _ = build_market(tmp_path, monkeypatch)
        _warm(m)
        m.amt.set_alerts("ES", True)
        m.amt.set_alerts("NQ", True)
        es, nq = await _alerting(m, "ES", "NQ")
        c = Client()
        c.subs[1] = object()
        await m.amt.subscribe(c, 1, "ES", 5)
        chart = c.amt_entries[1]
        assert chart is es                                    # the ES 5m chart is on ES's alert runner
        _sent(c)
        m.amt.merge_overrides({"cc_alert_80": False, "vwap_alert": True, "lvl_zoneWidthTicks": 20}, "NQ")
        es2, nq2 = await _alerting(m, "ES", "NQ")
        assert es2 is es and c.amt_entries[1] is chart and _sent(c) == []
        assert (es.runner.S.cc_alert_80, es.runner.S.vwap_enable, es.runner.S.lvl_zoneWidthTicks) == (True, False, 8)
        assert nq2 is not nq
        assert (nq2.runner.S.cc_alert_80, nq2.runner.S.vwap_enable, nq2.runner.S.lvl_zoneWidthTicks) == (False, True, 20)
        assert m.amt.alert_key("ES") == ("ES", amt_hub.ALERT_MINUTES) and len(m.amt.alert_key("NQ")) == 3
        assert m.amt.settings("ES").cc_alert_80 is True and m.amt.settings("NQ").cc_alert_80 is False
        # an NQ chart with its own settings has NQ's alert settings, whatever it sends; an ES one ES's
        _, mine = m.amt.chart_settings({"vwap_width": 4, "cc_alert_80": True, "lvl_zoneWidthTicks": 5}, "NQ")
        assert mine["cc_alert_80"] is False and mine["lvl_zoneWidthTicks"] == 20 and mine["vwap_width"] == 4
        _, mine = m.amt.chart_settings({"vwap_width": 4, "cc_alert_80": False}, "ES")
        assert "cc_alert_80" not in mine and "lvl_zoneWidthTicks" not in mine
        # every chart's settings still reload every symbol's charts
        m.amt.merge_overrides({"mvp_tf_mode": "Session"}, "NQ")
        assert _sent(c) == ["amt_reset"]
    asyncio.run(go())


def test_each_symbols_alerts_run_on_its_own_mode_and_storm_cap(tmp_path, monkeypatch):
    """ES's Level Alert Mode is Both and its storm cap 5 alerts an hour; NQ's are Zone Entry and the default 30. A tick
    of each is watched in its own symbol's mode, and six alerts of each: ES sends five and the cap's warning, NQ all six
    (before: the one shared mode and cap for both)."""
    sessions.set_calendar({"days": []})
    monkeypatch.setenv("CHART_WORKERS", "0")

    async def go():
        m, _ = build_market(tmp_path, monkeypatch)
        _warm(m)
        m.amt.merge_overrides({"LevelAlertMode": "Both", "alertcap_max": 5}, "ES")
        m.amt.merge_overrides({"LevelAlertMode": "Zone Entry"}, "NQ")
        m.amt.set_alerts("ES", True)
        m.amt.set_alerts("NQ", True)
        es, nq = await _alerting(m, "ES", "NQ")
        modes = []
        for e in (es, nq):
            e.runner.observe = lambda price, mode: modes.append(mode) or []
        from chartsrv.models import Tick
        m.amt.on_live_tick(Tick("ES", 1, 7000.0, 1, 1))
        m.amt.on_live_tick(Tick("NQ", 1, 21000.0, 1, 1))
        assert modes == ["Both", "Zone Entry"]
        sent = []
        m.amt._emit = lambda sym, now, price, text: sent.append((sym, text))
        n = [SimpleNamespace(Text=f"IBH Cross Up @ {i}") for i in range(6)]
        m.amt._notify("ES", n, 7000.0)
        m.amt._notify("NQ", n, 21000.0)
        assert [s for s, _ in sent] == ["ES"] * 6 + ["NQ"] * 6
        assert "storm cap reached (5/60m)" in sent[5][1] and "@ 5 |" in sent[11][1]
    asyncio.run(go())


# ---- the migration: once, to every symbol -----------------------------------------------------------------------------

def test_the_alert_settings_from_before_go_to_every_symbol_once(tmp_path, monkeypatch):
    """A server from before: one set of settings, alert settings among them, and AMT alerts kept for YM (taken off
    Instruments). Its first start gives every instrument, and YM, those alert settings; every chart's keep the rest
    (the levels file among them) and lose them; nothing set is lost. Then NQ's change and a restart: nothing is copied
    again, NQ keeps its own (also when every chart's settings have alert settings in them again)."""
    path = tmp_path / "chart.db"
    s = Store(path)
    s.set_setting("amt_settings", {**OLD, "LevelsFile": "https://levels.example/{root}", "vwap_width": 3})
    s.set_setting("amt_alerts", {"ES": {"on": False}, "YM": {"on": True}})
    s.close()
    m, _ = build_market(tmp_path, monkeypatch)   # the real start: the hub, then config/nt_settings.json's seed
    every = {"LevelsFile": "https://levels.example/{root}", "vwap_width": 3}
    assert set(m.amt.per_symbol) == {*m.instruments, "YM"} and {"ES", "NQ", "CL", "GC", "MES", "MNQ"} <= set(m.instruments)
    assert all(ov == OLD for ov in m.amt.per_symbol.values())
    assert {k: m.amt.overrides[k] for k in every} == every and not set(m.amt.overrides) & amt_hub.PER_SYMBOL
    assert m.store.get_setting(amt_hub.SYMBOL_SETTING) == m.amt.per_symbol
    assert not set(m.store.get_setting("amt_settings")) & amt_hub.PER_SYMBOL
    for sym in ("ES", "NQ", "CL"):                      # what a chart of each starts from: as before, every setting
        assert {k: m.amt.base(sym)[k] for k in {**OLD, **every}} == {**OLD, **every}
    # once: NQ's own change, then a restart
    m.amt.merge_overrides({"cc_alert_80": True, "LevelAlertMode": "Cross"}, "NQ")
    m.store.set_setting("amt_settings", {**m.store.get_setting("amt_settings"), "cc_alert_80": True})   # e.g. an older version's
    m2, _ = build_market(tmp_path, monkeypatch)
    assert m2.amt.per_symbol["NQ"] == {"lvl_zoneWidthTicks": 12, "alertcap_max": 50}
    assert m2.amt.per_symbol["ES"] == m2.amt.per_symbol["CL"] == OLD and "cc_alert_80" not in m2.amt.overrides
    assert m2.amt.settings("ES").cc_alert_80 is False and m2.amt.settings("NQ").cc_alert_80 is True


def test_a_new_install_and_one_with_no_alert_settings_changed(tmp_path):
    """Nothing set before: every symbol's alert settings are NinjaTrader's, stored at once (so a restart copies
    nothing again: a symbol's stored ones stay), and every chart's settings are left as they were."""
    s = Store(tmp_path / "chart.db")
    s.set_setting("amt_settings", {"vwap_width": 3})
    inst = SimpleNamespace(tick_size=0.25, rth=("08:30", "15:00"), calendar="equity")
    h = amt_hub.AmtHub(SimpleNamespace(store=s, instruments={"ES": inst, "NQ": inst}))
    assert h.per_symbol == {"ES": {}, "NQ": {}} and h.overrides == {"vwap_width": 3}
    assert s.get_setting(amt_hub.SYMBOL_SETTING) == {"ES": {}, "NQ": {}} and s.get_setting("amt_settings") == {"vwap_width": 3}
    s.set_setting(amt_hub.SYMBOL_SETTING, {"ES": {"vwap_alert": True}})
    assert amt_hub.AmtHub(SimpleNamespace(store=s, instruments={"ES": inst, "NQ": inst})).per_symbol == {"ES": {"vwap_alert": True}}


# ---- the API ------------------------------------------------------------------------------------------------------------

def test_the_api_reads_and_changes_one_symbols_alert_settings(client):
    """PATCH /api/amt/settings?symbol=NQ: its alert settings NQ's, the rest every chart's; GET /api/amt and the
    broadcast give every chart's and each symbol's; PUT with a symbol replaces that one's; no symbol: every symbol's
    (as before, for every chart); an unknown symbol is refused, nothing changed."""
    start = client.get("/api/amt").json()
    assert set(start["per_symbol"]) == amt_hub.SHARED - {"LevelsFile"} and "LevelsFile" in start["shared"]
    es = start["symbols"]["ES"]
    r = client.patch("/api/amt/settings?symbol=nq", json={"cc_alert_80": False, "ib_alert_time": "0830-1000", "vwap_width": 3})
    assert r.status_code == 200
    d = client.get("/api/amt").json()
    assert d["symbols"]["NQ"] == {**start["symbols"]["NQ"], "cc_alert_80": False, "ib_alert_time": "0830-1000"}
    assert d["symbols"]["ES"] == es and d["overrides"] == {**start["overrides"], "vwap_width": 3}
    told = [x for x in client.heard if x["type"] == "amt_settings"][-1]
    assert told["symbols"] == d["symbols"] and told["overrides"] == d["overrides"]
    assert client.patch("/api/amt/settings?symbol=ZZZ", json={"cc_alert_80": True}).status_code == 404
    assert client.patch("/api/amt/settings?symbol=NQ", json={"cc_alert_80": "no"}).status_code == 400
    assert client.get("/api/amt").json()["symbols"] == d["symbols"]
    r = client.put("/api/amt/settings?symbol=ES", json={"LevelAlertMode": "Both", "vwap_width": 4}).json()
    assert r["symbols"]["ES"] == {"LevelAlertMode": "Both"} and r["symbols"]["NQ"] == d["symbols"]["NQ"]
    assert r["overrides"] == {"vwap_width": 4}
    r = client.patch("/api/amt/settings", json={"alertcap_max": 60}).json()   # no symbol: every symbol's
    assert all(ov.get("alertcap_max") == 60 for ov in r["symbols"].values()) and r["symbols"]["ES"]["LevelAlertMode"] == "Both"
    chk = client.post("/api/amt/settings/check?symbol=NQ", json={"vwap_width": 5, "cc_alert_80": True}).json()["overrides"]
    assert chk["vwap_width"] == 5 and chk["cc_alert_80"] is False and chk["alertcap_max"] == 60


# ---- the pages: the dialog shows and saves the chart's symbol's ----------------------------------------------------------

@needs_node
def test_the_dialog_on_an_es_chart_shows_es_switches_after_an_nq_change():
    """The user's report: on an NQ tab, Alert 80% Rule Confirmed unticked and a wider zone, OK. Only NQ's change (the
    PATCH says NQ); the ES tab of the same page and an ES chart on another page show ES's, ticked and 8 ticks, and the
    NQ tab shows NQ's (before: one set for both, so ES's were unticked too). The alert settings are marked with the
    symbol."""
    out = pages("""
  const A = page(), B = page();   // A: the workspace's page, on its NQ tab; B: another page's ES chart
  A.S.symbol = 'NQ';
  await A.hello(); await B.hello();
  A.indDlg.open(true, 'amt');
  A.edit('amt', 'cc_alert_80', false);
  A.edit('amt', 'lvl_zoneWidthTicks', 20);
  await A.ok();
  heard(A); heard(B);
  A.indDlg.open(true, 'amt');
  out.nq = [A.shows('amt', 'cc_alert_80'), A.shows('amt', 'lvl_zoneWidthTicks')];
  const row = A.find(A.root.sel['.ind-body'], (e) => e.dataset?.name === 'cc_alert_80');
  out.mark = row.children[0].children.map((e) => e.textContent);
  A.S.symbol = 'ES'; A.indDlg.refresh();   // the ES tab
  out.esTab = [A.shows('amt', 'cc_alert_80'), A.shows('amt', 'lvl_zoneWidthTicks')];
  B.indDlg.open(true, 'amt');
  out.es = [B.shows('amt', 'cc_alert_80'), B.shows('amt', 'lvl_zoneWidthTicks')];
  out.server = copy(SRV.syms);
  out.sent = calls.filter((c) => c[1].startsWith('/api/amt/settings')).map((c) => c[1]);
""")
    assert out["server"] == {"NQ": {"cc_alert_80": False, "lvl_zoneWidthTicks": 20}}
    assert out["sent"] == ["/api/amt/settings?symbol=NQ"]
    assert out["nq"] == [False, 20] and out["mark"] == [" (NQ)"]
    assert out["esTab"] == [True, 8] and out["es"] == [True, 8]


@needs_node
def test_apply_to_all_charts_on_an_es_chart_leaves_nq_alert_switches():
    """NQ has Alert 80% Rule Confirmed off. On an ES chart, Alert IB High Cross unticked and Profile Row Width 3, then
    Apply to all charts: the width is every chart's starting setting, the IB alert ES's only, and NQ's switches stay
    as they were (before: every symbol's took this chart's)."""
    out = pages("""
  SRV.syms = { NQ: { cc_alert_80: false } };
  const B = page();
  await B.hello();
  B.indDlg.open(true, 'amt');
  B.edit('amt', 'ib_alert_ibh', false);
  B.edit('amt', 'vwap_width', 3);
  await B.applyAll();
  out.server = [copy(SRV.amt), copy(SRV.syms)];
  out.note = B.indAdapter('amt').allNote?.() || '';   // what Apply to all charts' question adds
""")
    assert out["server"] == [{"vwap_width": 3}, {"NQ": {"cc_alert_80": False}, "ES": {"ib_alert_ibh": False}}]
    assert "every ES chart only" in out["note"]


@needs_node
def test_alert_setting_edits_stay_with_the_symbol_they_were_made_for():
    """A layout's page edits the chart last clicked's: Alert 80% Rule Confirmed unticked on ES's, then NQ's chart is
    clicked before OK. The dialog shows NQ's (ticked) and OK saves the chart's own Profile Row Width only: neither
    symbol's alert settings change (before: ES's edit went to every symbol)."""
    out = pages("""
  const L = page();
  await L.hello();
  L.indDlg.open(true, 'amt');
  L.edit('amt', 'cc_alert_80', false);
  L.edit('amt', 'vwap_width', 3);
  L.S.symbol = 'NQ'; L.indDlg.refresh();
  out.shows = L.shows('amt', 'cc_alert_80');
  await L.ok();
  out.server = copy(SRV.syms);
  out.own = L.own.amt;
""")
    assert out["shows"] is True and out["server"] == {} and out["own"] == {"vwap_width": 3}


@needs_node
def test_an_alert_switch_a_tab_had_turned_off_goes_to_its_symbol_only():
    """A tab from before the switches were shared, NQ's, had Alert 80% Rule Confirmed off; an ES tab had nothing of
    the kind. Moved out once: off on NQ's charts, ES's as they were, and the message says whose (before: off for
    every symbol)."""
    out = pages("""
  const A = page();
  A.S.tabs = [{ sym: 'NQ', own: { set: { amt: { cc_alert_80: false, vwap_width: 3 } } } }, { sym: 'ES', own: { set: { amt: { vwap_width: 2 } } } }];
  await A.hello();
  await A.moveSharedOut();
  out.server = copy(SRV.syms);
  out.sets = A.S.tabs.map((t) => t.own.set);
  out.toasts = A.toasts;
""")
    assert out["server"] == {"NQ": {"cc_alert_80": False}}
    assert out["sets"] == [{"amt": {"vwap_width": 3}}, {"amt": {"vwap_width": 2}}]
    assert len(out["toasts"]) == 1
    assert "Off on every NQ chart, as this chart had it: AMT Previous Day Value & 80% Rule: Alert 80% Rule Confirmed." in out["toasts"][0]
