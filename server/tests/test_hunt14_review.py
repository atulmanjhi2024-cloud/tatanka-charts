"""Round 14 of the bug hunt, a review of the latest fixes: the gap alert switches worked out at the first start (from
each symbol's alerts as last saved, a chart's own over a stale starting setting, and what they turn off said once),
Yahoo's spark batches under a 429 (the round backs off, the next starts where it stopped), the object tree while all
drawings are hidden. The add-on's replay rule (old trades in a row only a few seconds apart) is in test_addon_replay.py."""
import json
import shutil
from types import SimpleNamespace

import pytest

from chartsrv.store import Store

needs_node = pytest.mark.skipif(not shutil.which("node"), reason="needs node")


# ---- r14-review-1: the gap alert switches from before they were every chart's -------------------------------------------

BASE = {"rth": False, "RthStart": 83000, "RthEnd": 150000, "MinimumGapPoints": 0.0}


def gap_service(tmp_path, **saved):
    from chartsrv.gaps_service import GapService
    st = Store(tmp_path / "gs.db")
    for k, v in saved.items():
        st.set_setting(k, v)
    inst = SimpleNamespace(rth=("08:30", "15:00"))
    return GapService(SimpleNamespace(store=st, instruments={"ES": inst, "NQ": inst, "CL": inst})), st


def every(switches: dict) -> dict:
    """Each instrument's alert switches, all the same: what the first start since they are each symbol's (round 14)
    gives every one of the switches worked out here."""
    return {s: switches for s in ("ES", "NQ", "CL")}


def test_gap_alert_switches_worked_out_once_from_the_alerts_that_are_on_and_what_they_turn_off_is_said(tmp_path):
    """ES's gap alerts were switched on from a chart with 'Alert on new gap' unticked, NQ's from one with it ticked
    (NQ sent new-gap alerts). The shared switch is off (it silences, never turns one on), stored at once so the next
    start keeps it, and a notice of it waits for the next page; a symbol whose alerts are off counts for nothing (it
    fires nothing). Before: worked out again at every start, from every config, and nothing said, so NQ's new-gap
    alerts stopped silently. Since round 14 the switches are each symbol's: every symbol starts from the shared ones
    worked out here."""
    from chartsrv.gaps_service import SYMBOL_SWITCHES
    g, st = gap_service(tmp_path, gap_alerts={"ES": {"on": True, "new": False, "fill": True, **BASE},
                                              "NQ": {"on": True, "new": True, "fill": True, **BASE},
                                              "CL": {"on": False, "new": True, "fill": False, **BASE}})
    assert g.switches() == every({"new": False, "fill": True})
    assert g.alert_cfg["NQ"]["new"] is False and g.alert_cfg["NQ"]["fill"] is True
    assert st.get_setting(SYMBOL_SWITCHES) == every({"new": False, "fill": True})
    assert g.notice == ["new"]
    again, _ = gap_service(tmp_path)   # the next start: as stored, the notice still waiting
    assert (again.switches(), again.notice) == (every({"new": False, "fill": True}), ["new"])
    assert again.told() == ["new"] and again.told() == []   # handed to one page only
    assert gap_service(tmp_path)[0].notice == []


def test_a_stale_starting_gap_setting_does_not_overrule_the_alerts_as_saved_since(tmp_path):
    """'Apply to all charts' once saved 'Alert on full gap fill' unticked as the starting setting; the chart then ticked
    it in its own settings and OK'd, so every symbol's alerts had fill on and fill alerts fired. The alerts as saved
    since are what count: fill stays on (before: off, silently). With no symbol's alerts saved at all the starting
    settings still count, and say so."""
    g, _ = gap_service(tmp_path, ind_settings={"gap": {"AlertOnGapFill": False}},
                       gap_alerts={"ES": {"on": True, "new": True, "fill": True, **BASE}})
    assert (g.switches(), g.notice, g.alert_cfg["ES"]["fill"]) == (every({"new": True, "fill": True}), [], True)
    g, _ = gap_service(tmp_path / "none", ind_settings={"gap": {"AlertOnNewGap": False}})
    assert (g.switches(), g.notice) == (every({"new": False, "fill": True}), ["new"])
    g, _ = gap_service(tmp_path / "fresh")
    assert (g.switches(), g.notice) == (every({"new": True, "fill": True}), [])


def test_the_gap_alert_switches_the_first_start_turned_off_are_said_once(tmp_path, monkeypatch):
    """The server, as stored before the update (ES's alerts with 'Alert on new gap' off, NQ's on): GET /api/gaps/alerts
    gives the notice, DELETE /api/gaps/alerts/notice hands it to the first page that asks and none after."""
    db = tmp_path / "chart.db"
    Store(db).set_setting("gap_alerts", {"ES": {"on": True, "new": False, "fill": True, **BASE},
                                         "NQ": {"on": True, "new": True, "fill": True, **BASE}})
    monkeypatch.setenv("CHART_DB", str(db))
    monkeypatch.setenv("CHART_LEVELS_DIR", str(tmp_path))
    monkeypatch.setenv("CHART_FEED", "sim")
    monkeypatch.setenv("CHART_SIM_WARMUP_MIN", "60")
    from fastapi.testclient import TestClient
    from chartsrv.app import app
    with TestClient(app) as c:
        got = c.get("/api/gaps/alerts").json()
        assert (got["switches"]["NQ"], got["notice"], got["alerts"]["NQ"]["new"]) == ({"new": False, "fill": True}, ["new"], False)
        assert all(sw == {"new": False, "fill": True} for sw in got["switches"].values())   # every symbol's, from then on its own
        assert c.delete("/api/gaps/alerts/notice").json() == {"notice": ["new"]}
        assert c.delete("/api/gaps/alerts/notice").json() == {"notice": []}
        assert c.get("/api/gaps/alerts").json()["notice"] == []


@needs_node
def test_a_page_says_the_gap_alert_switches_the_server_turned_off_once():
    """The server turned 'Alert on new gap' off at its first start (NQ's new-gap alerts stop): the first page that opens
    says so, with the switch a chart of its own had off in the same message; the next page says nothing (before: no
    message, as the page only told what it turned off itself, and the server's was off already)."""
    from test_hunt13_amt import pages
    out = pages("""
  SRV.switches = { ES: { new: false, fill: true }, NQ: { new: false, fill: true } }; SRV.notice = ['new'];
  SRV.gap = { ES: { on: true, new: false, fill: true, rth: false }, NQ: { on: true, new: false, fill: true, rth: false } };
  const A = page();
  A.S.tabs = [{ sym: 'ES', own: { set: { gap: { AlertOnGapFill: false } } } }];
  await A.hello(); await A.moveSharedOut();
  const B = page();
  await B.hello(); await B.moveSharedOut();
  SRV.notice = ['new'];
  const C = page();   // nothing of its own, and the notice it heard of claimed by another page meanwhile
  await C.hello(); SRV.notice = []; await C.moveSharedOut();
  out.toasts = [A.toasts, B.toasts, C.toasts];
  out.server = [copy(SRV.switches), SRV.notice];
""")
    a, b, c = out["toasts"]
    assert len(a) == 1 and "Off on every ES chart, as this chart had it: Gap Analysis: Alert on full gap fill." in a[0]
    assert "Off on every chart, as the gap alerts had it before: Gap Analysis: Alert on new gap." in a[0]
    assert b == [] and c == []
    assert out["server"] == [{"ES": {"new": False, "fill": False}, "NQ": {"new": False, "fill": True}}, []]


# ---- r14-review-2: Yahoo throttling mid-round ---------------------------------------------------------------------------

def test_yahoo_throttled_mid_round_backs_off_and_the_next_round_starts_where_it_stopped(monkeypatch):
    """100 Yahoo tickers (five spark batches) and a Yahoo that answers two spark requests per round, then 429 on both
    hosts. Each round delivers what came and backs off as a failed round does (30, 60 ... 300 s), and starts with the
    batch the last one was throttled at, so every ticker is quoted by the third round; a round with no 429 waits the
    poll time again. Before: every round counted as a success 15 s later and asked batches 1 and 2 only, so 60 tickers
    were never asked."""
    import asyncio
    import itertools
    import string

    import httpx

    from chartsrv.feeds import yahoo
    from test_yahoo import spark
    keys = ["".join(p) for p in itertools.islice(itertools.product(string.ascii_uppercase, repeat=3), 100)]
    limit, asked = {"n": 2}, []

    def answer(req):
        if limit["n"] <= 0:
            return httpx.Response(429)
        limit["n"] -= 1
        part = req.url.params["symbols"].split(",")
        asked.append(sorted(keys).index(part[0]) // yahoo.BATCH + 1)
        return httpx.Response(200, json=spark({s: (10.0, 9.0, 100) for s in part}))

    class Market:
        watch_keys = set(keys)
        quoted: set = set()

        def quote_wanted(self, key, src):
            return True

        def on_quote(self, key, *a, **k):
            self.quoted.add(key)

    class Done(Exception):
        pass
    waits, seen = [], []

    async def pause(s):
        waits.append(s)
        seen.append(len(m.quoted))
        limit["n"] = 2 if len(waits) < 6 else 99   # the 7th round: Yahoo no longer throttles
        if len(waits) == 7:
            raise Done
    monkeypatch.setattr(yahoo.stopping, "pause", pause)
    m = Market()
    y = yahoo.YahooQuotes(SimpleNamespace(yahoo_poll_s=15, config_dir=None), transport=httpx.MockTransport(answer))
    with pytest.raises(Done):
        asyncio.run(y.run(m))
    assert waits == [30, 60, 120, 240, 300, 300, 15]
    assert asked[:6] == [1, 2, 3, 4, 5, 1] and seen[:3] == [40, 80, 100]
    assert y.stats["errors"] == 0 and y.stats["ok"] == 7 and y.stats["last_error"] == ""


# ---- r14-review-3: the object tree while all drawings are hidden --------------------------------------------------------

@needs_node
def test_a_drawing_picked_in_the_object_tree_while_all_are_hidden_shows_them_again():
    """Hide all, then a drawing's row clicked in the object tree (still open, its header offering Show all): the
    drawings show again, as for a new drawing, so the one picked is drawn with its handles and settings bar, and Delete
    removes a line that is seen (before: selected unseen, its bar up, and Delete removed it with nothing drawn). A row
    of a drawing hidden on its own (its eye) is still refused."""
    from test_hunt3_drawings import run
    out = run(r"""
table('ES').set('a', hline('a', 7000));
table('ES').set('b', hline('b', 7010));
table('ES').set('c', hline('c', 7020, { style: { color: '#2962ff', text: '', hidden: true } }));
await open('ES');
await press('hide');
D.showTree(true); await ticks();
row('c').onclick(); await ticks();
const own = { hidden: D.state.hidden, selected: D.state.selected };
row('b').onclick(); await ticks();
console.log(JSON.stringify({ own, hidden: D.state.hidden, selected: D.state.selected, bar: !ui.edit.hidden, handles: arcs(),
  header: ui.tree.children[0].children.at(-2).textContent }));""")
    assert out["own"] == {"hidden": True, "selected": None}
    assert out == {**out, "hidden": False, "selected": "b", "bar": True, "handles": 1, "header": "Hide all"}
