"""Round 14 of the bug hunt, the journal: the Journal tab asks again after a reconnect (every symbol too), MAE and MFE
of a trade scaled into, of one still open (up to now) and of one held across the chart's contract roll (none), and
Today from 17:00:00 CT. The app.js parts run in node with the rest stubbed."""
import json
import os
import shutil
import subprocess

import pytest

from chartsrv import journal
from chartsrv.models import Bar
from chartsrv.store import Store
from test_hunt_web_chart import app_part, ct_ms
from test_journal import fill

needs_node = pytest.mark.skipif(not shutil.which("node"), reason="needs node")
MIN = 60_000
T0 = 1_760_000_000_000 - 1_760_000_000_000 % MIN


def node(src: str, tz: str = "America/Chicago"):
    """Runs src in node in the time zone tz; what it logs last is the result (JSON)."""
    r = subprocess.run(["node", "-e", src], capture_output=True, text=True, env={**os.environ, "TZ": tz})
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout.strip().split("\n")[-1])


# ---- r14-journal-1: a reconnect asks for the journal again, also for every symbol ------------------------------------

RECONNECT_JS = r"""
const stub = new Proxy(function () {}, {
  get: (t, k) => (k === Symbol.toPrimitive ? () => 'stub' : k === 'then' || k === Symbol.unscopables ? undefined : stub),
  apply: () => stub, set: () => true });
const els = {};
const known = {
  $: (s) => (els[s] ||= { classList: { contains: (c) => s === '#tab-journal' && c === 'on', toggle() {}, add() {}, remove() {} },
    checked: s === '#jr-sym' ? ONLY : false, value: s === '#jr-range' ? '1' : '', textContent: '', style: {},
    replaceChildren() {}, querySelectorAll: () => [] }),
  S: { symbol: 'ES', spec: '5m', st: {}, cs: {}, subId: 0, quotes: {}, instruments: {}, vp: {} },
  local: { get: (k, d) => d, set() {} },
  asked: [], timers: [],
  api: (method, url) => { known.asked.push(url.split('?')[0]); return new Promise(() => {}); },
  setTimeout: (f) => { known.timers.push(f); return 1; }, clearTimeout: () => {},
  TK_MULTI: false, extTimer: 0, WL_FUT: /^(\w+)1!$/, SPEC_RE: /^\d+[mhDWM]$/,
  chartSym: (s) => s, isExtSym: () => false, tapeOpen: () => false,
};
with (new Proxy(known, { has: (t, k) => k in t || !(k in globalThis), get: (t, k) => (k in t ? t[k] : k === Symbol.unscopables ? undefined : stub),
  set: (t, k, v) => { t[k] = v; return true; } })) {
  (function () {
    /*APP*/
    const hello = { type: 'hello', instruments: { ES: {} }, catalog: {}, levels: [], holidays: {}, only_futures: [], alerts: [],
      events: [], quotes: [], watchlists: {}, notify: {} };
    onMessage(hello);
    while (timers.length) timers.shift()();
    console.log(JSON.stringify(asked.filter((u) => u === '/api/journal').length));
  })();
}
"""


@needs_node
@pytest.mark.parametrize("only", [False, True])
def test_the_journal_asks_again_after_a_reconnect(only):
    """The Journal tab open (Today) while the page's socket was closed (a phone locked, the server restarted): fills
    made meanwhile were never heard, so the reconnect's hello asks for the journal once, for every symbol (the
    default) as for this symbol only (before: every symbol was never asked again, its trades and P&L stale)."""
    parts = ["onMessage", "subscribe", "tapeSymbol", "journalOpen", "jrReq", "journalLoad"]
    src = RECONNECT_JS.replace("ONLY", json.dumps(only)).replace("/*APP*/", "\n".join(app_part(p) for p in parts))
    assert node(src) == 1


# ---- r14-journal-2: a trade scaled into keeps its MAE and MFE --------------------------------------------------------

def _store(tmp_path, bars):
    st = Store(tmp_path / "c.db")
    st.insert_bars_if_missing("ES", bars)
    return tmp_path / "c.db"


@pytest.mark.parametrize("step, mae, mfe", [(0.5, 5.5, 9.5),     # added higher, price rising: average 6905
                                             (-0.5, 9.5, 5.5)])   # averaged down, price falling: 6895
def test_a_trade_scaled_into_keeps_its_mae_and_mfe(tmp_path, step, mae, mfe):
    """ES long 1 @ 6900 at 10:00 (that minute 6899.5-6900.5), 1 more 20 minutes later 10 points off and out 2 at 10:28.
    The roll check tests the fill that opened it against its minute, not the average entry, which an add at another
    price moves away from it (before: no MAE or MFE at all for any trade added to a few ticks off its first minute)."""
    path = _store(tmp_path, [Bar(t=T0 + i * MIN, o=6900 + i * step, h=6900.5 + i * step, l=6899.5 + i * step,
                                 c=6900 + i * step, v=10) for i in range(30)])
    add, out = 6900 + 20 * step, 6900 + 28 * step
    tr = journal.trades([fill(T0 + 5000, 1, 1, 6900.0, 1), fill(T0 + 20 * MIN + 5000, 1, 1, add, 2),
                         fill(T0 + 28 * MIN + 5000, -1, 2, out, 3)])[0]
    assert tr["entry"] == (6900 + add) / 2
    journal.excursions(path, tr, 0.25)
    assert (tr["mae"], tr["mfe"]) == (mae, mfe)
    other = journal.trades([fill(T0 + 5000, 1, 1, 6950.0, 1), fill(T0 + 20 * MIN + 5000, 1, 1, add, 2),
                            fill(T0 + 28 * MIN + 5000, -1, 2, out, 3)])[0]   # its first fill not in its minute's bar
    journal.excursions(path, other, 0.25)
    assert other["mae"] is None and other["mfe"] is None


# ---- r14-journal-3: an open trade's MAE and MFE run to now -----------------------------------------------------------

def _journal(tmp_path, monkeypatch, fills, bars, start, end, rolled=None):
    from fastapi.testclient import TestClient
    from chartsrv.app import app
    monkeypatch.setenv("CHART_DB", str(tmp_path / "chart.db"))
    monkeypatch.setenv("CHART_LEVELS_DIR", str(tmp_path))
    monkeypatch.setenv("CHART_FEED", "none")
    with TestClient(app) as c:
        m = app.state.market
        for f in fills:
            c.portal.call(m.store.add_fill, f)
        c.portal.call(m.store.insert_bars_if_missing, "ES", bars)
        if rolled is not None:
            c.portal.call(m.store.set_setting, "contract_rolled_at", rolled)
        return {t["account"]: t for t in c.get(f"/api/journal?start={start}&end={end}").json()["trades"]}


def test_an_open_trade_s_mae_and_mfe_run_to_now(tmp_path, monkeypatch):
    """ES long 1 @ 6900 still open 30 minutes on, price 30 points lower: its MAE is the 30 points, from every bar stored
    since its entry (before: its entry minute's only, MAE 0.50 beside 'open'). Closed there, the same."""
    bars = [Bar(t=T0 + i * MIN, o=6900 - i, h=6900.5 - i, l=6899.5 - i, c=6900 - i, v=10) for i in range(31)]
    got = _journal(tmp_path, monkeypatch, [fill(T0 + 5000, 1, 1, 6900.0, 1),
                                           fill(T0 + 5000, 1, 1, 6900.0, 2, "Sim102"), fill(T0 + 30 * MIN + 5000, -1, 1, 6870.0, 3, "Sim102")],
                   bars, T0 - MIN, T0 + 60 * MIN)
    assert got["Sim101"]["open"] and (got["Sim101"]["mae"], got["Sim101"]["mfe"]) == (30.5, 0.5)
    assert not got["Sim102"]["open"] and (got["Sim102"]["mae"], got["Sim102"]["mfe"]) == (30.5, 0.5)
    tr = journal.trades([fill(T0 + 5000, 1, 1, 6900.0, 1)])[0]
    journal.excursions(tmp_path / "chart.db", tr, 0.25, until=T0 + 10 * MIN + 1)   # open as of the range's end
    assert (tr["mae"], tr["mfe"]) == (10.5, 0.5)


# ---- r14-journal-4: none from the other contract month for a trade held across the chart's roll ----------------------

def test_a_trade_held_across_the_chart_s_roll_gets_no_excursions_from_the_other_month(tmp_path, monkeypatch):
    """The stored bars switch, unadjusted, from December (6900) to March (6960) at the chart's roll. A December trade
    held across it, closed or still open, gets no MAE or MFE (before: MFE 62 from March's bars); one closed before the
    roll, and a March one after it, keep theirs. Without the roll known (an older one), the closing fill's minute
    shows it."""
    bars = [Bar(t=T0 + i * MIN, o=6900, h=6901, l=6899, c=6900, v=10) for i in range(10)]
    bars += [Bar(t=T0 + i * MIN, o=6960, h=6962, l=6958, c=6960, v=10) for i in range(10, 20)]
    dec, mar = "ES 12-26", "ES 03-27"
    fills = [fill(T0 + 1000, 1, 1, 6900.0, 1, "Sim101", dec), fill(T0 + 5 * MIN, -1, 1, 6901.0, 2, "Sim101", dec),
             fill(T0 + 1000, 1, 1, 6900.0, 3, "Sim102", dec), fill(T0 + 15 * MIN, -1, 1, 6901.0, 4, "Sim102", dec),
             fill(T0 + 2 * MIN, 1, 1, 6900.0, 5, "Sim103", dec),
             fill(T0 + 12 * MIN, 1, 1, 6960.0, 6, "Sim104", mar), fill(T0 + 15 * MIN, -1, 1, 6961.0, 7, "Sim104", mar)]
    got = _journal(tmp_path, monkeypatch, fills, bars, T0 - MIN, T0 + 60 * MIN, {"ES": T0 + 10 * MIN - 1})
    ex = {a: (t["mae"], t["mfe"]) for a, t in got.items()}
    assert ex == {"Sim101": (1.0, 1.0), "Sim102": (None, None), "Sim103": (None, None), "Sim104": (2.0, 2.0)}
    held = journal.trades(fills[2:4])[0]
    journal.excursions(tmp_path / "chart.db", held, 0.25)   # no roll known
    assert held["mae"] is None and held["mfe"] is None


# ---- r14-journal-5: Today starts at 17:00:00 CT ------------------------------------------------------------------------

TODAY_JS = r"""
const RealDate = Date;
let NOW = 0;
class FakeDate extends RealDate { constructor(...a) { super(...(a.length ? a : [NOW])); } static now() { return NOW; } }
const els = {}, $ = (s) => (els[s] ||= { checked: false, value: '1', textContent: '', classList: { contains: (c) => s === '#tab-journal' && c === 'on' } });
const S = { symbol: 'ES' }, local = { get: (k, d) => d };
let asked = '';
const api = (m, url) => { asked = url; return new Promise(() => {}); };
(function (Date) {
  /*APP*/
  const out = [];
  for (const now of NOWS) {
    NOW = now;
    journalLoad();
    out.push(Number(/start=(\d+)/.exec(asked)[1]));
  }
  console.log(JSON.stringify(out));
})(FakeDate);
"""


@needs_node
@pytest.mark.parametrize("tz", ["America/Chicago", "America/New_York", "Europe/Berlin"])
def test_today_starts_at_17_00_00_ct(tz):
    """Today asks from the 17:00 CT open itself, whatever second the tab is loaded: a scalp of the reopen (out at
    17:00:30) is in it (before: from 17:00 and the clock's seconds, 17:00:40.250 when loaded at 08:45:40.250)."""
    nows = [ct_ms(2026, 10, 14, 8, 45, 40) + 250, ct_ms(2026, 10, 14, 17, 0, 59) + 900, ct_ms(2026, 10, 14, 16, 59, 59) + 999]
    src = TODAY_JS.replace("NOWS", json.dumps(nows)).replace("/*APP*/", "\n".join(
        app_part(p) for p in ["journalOpen", "jrReq", "journalLoad"]))
    assert node(src, tz) == [ct_ms(2026, 10, 13, 17, 0), ct_ms(2026, 10, 14, 17, 0), ct_ms(2026, 10, 13, 17, 0)]


# ---- the Journal tab asks again when the side panel is shown (a fill heard while it was hidden) ------------------------

SIDE_JS = r"""
const stub = new Proxy(function () {}, {
  get: (t, k) => (k === Symbol.toPrimitive ? () => 'stub' : k === 'then' || k === Symbol.unscopables ? undefined : stub),
  apply: () => stub, set: () => true });
const els = {};
const el = (s) => {
  const on = new Set(s === '#tab-journal' ? ['on'] : s === 'main' ? ['side-off'] : []);
  return { classList: { contains: (c) => on.has(c), toggle: (c, f = !on.has(c)) => { if (f) on.add(c); else on.delete(c); return f; } },
    checked: false, value: '1', textContent: '', style: {}, setAttribute() {}, replaceChildren() {}, querySelectorAll: () => [] };
};
const known = {
  $: (s) => (els[s] ||= el(s)),
  S: { symbol: 'ES' }, local: { get: (k, d) => d, set() {} }, TK_CELL: null,
  asked: [], api: (method, url) => { known.asked.push(url.split('?')[0]); return new Promise(() => {}); },
  tapeOpen: () => false,
};
with (new Proxy(known, { has: (t, k) => k in t || !(k in globalThis), get: (t, k) => (k in t ? t[k] : k === Symbol.unscopables ? undefined : stub),
  set: (t, k, v) => { t[k] = v; return true; } })) {
  (function () {
    /*APP*/
    const out = [];
    for (let i = 0; i < 2; i++) {   // shown, then hidden again
      $('#side-toggle').onclick();
      out.push([!$('main').classList.contains('side-off'), asked.filter((u) => u === '/api/journal').length]);
    }
    console.log(JSON.stringify(out));
  })();
}
"""


@needs_node
def test_the_journal_asks_again_when_the_side_panel_is_shown():
    """The Journal tab picked, the side panel hidden (a PC: ⇥), and a fill comes: the journal isn't asked then (it
    isn't shown), so showing the panel again asks for it (before: the trades and P&L from before the fill stayed until
    the next one). Hiding it asks nothing."""
    from test_hunt_web_chart import app_lines
    parts = ["setSide", "journalOpen", "jrReq", "journalLoad"]
    src = SIDE_JS.replace("/*APP*/", "\n".join(app_part(p) for p in parts) + "\n" + app_lines("$('#side-toggle').onclick", "$('#side-toggle').onclick"))
    assert node(src) == [[True, 1], [False, 1]]
