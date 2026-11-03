"""Fixes from round 3 of the bug hunt of the standard indicators (web/ta.js, ta.py): the chart's pivots fold a holiday
halt into the next trade date with the instrument's own holiday group, and indicator alerts on pivots do the same. The
formulas themselves are in test_ta.py; here the chart's applyHistory runs in node with what it calls stubbed."""
import json
import shutil
import subprocess
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from chartsrv import ind_alerts as ia
from chartsrv import sessions, ta
from chartsrv.config import load_holidays
from chartsrv.sessions import CT

from test_hunt_web_chart import app_part

ROOT = Path(__file__).resolve().parents[2]
needs_node = pytest.mark.skipif(not shutil.which("node"), reason="needs node")


def _mlk_bars():
    """30-minute bars of Friday Jan 16 2026 (H 7000 L 6900 C 6950), MLK Day's short session to its 12:00 CT halt
    (H 6960 L 6940) and Tuesday's session."""
    at = lambda d, h: int(datetime(2026, 1, d, h, tzinfo=CT).timestamp() * 1000)   # noqa: E731
    out = []
    for (d0, h0, d1, h1, lo, hi, c) in [(15, 17, 16, 16, 6900, 7000, 6950), (18, 17, 19, 12, 6940, 6960, 6955),
                                        (19, 17, 20, 16, 6950, 7010, 7000)]:
        out += [{"t": t, "o": lo, "h": hi, "l": lo, "c": c, "v": 100} for t in range(at(d0, h0), at(d1, h1), 1_800_000)]
    return out


# applyHistory as written, run where every name it uses that the test doesn't give is a stub that takes anything
APPLY_JS = r"""
const fs = require('fs'), vm = require('vm');
const ta = vm.createContext({ Intl, Math, Date, Map, Number, Object, Array, JSON });
vm.runInContext(fs.readFileSync(process.argv[1], 'utf8') + '\nthis.setSessionClock = setSessionClock; this.setSessionHolidays = setSessionHolidays; this.TaPivotsRun = TaPivotsRun;', ta);
const bars = JSON.parse(process.argv[2]), out = {};
const stub = new Proxy(function () {}, { get: (t, k) => (k === Symbol.toPrimitive ? () => 0 : k === Symbol.unscopables ? undefined : stub), apply: () => stub });
const tuesday = bars.findIndex((b) => b.t >= Date.UTC(2026, 0, 19, 23));   // Monday 17:00 CT
const known = {
  setSessionClock: ta.setSessionClock, setSessionHolidays: ta.setSessionHolidays,
  exch: () => ({ tz: 'America/Chicago', rth: null, day: '17:00' }), inst: () => S.instruments[S.symbol] || {},
  calSpec: () => false, TZ_EXCHANGE: 'America/Chicago',   // 30-minute bars: on the exchange's own clock
  indsUpdate: () => {   // the chart's indicators, on its bars: Tuesday's daily PP
    const r = new ta.TaPivotsRun('Daily', 'floor');
    out[S.symbol] = S.bars.map((b) => r.step(b)[0])[tuesday];
  },
};
const S = { seqTime: new Map(), fp: new Map(), dsmaLive: new Map(), instruments: { ES: {}, CL: { calendar: 'energy' } },
  holidays: JSON.parse(process.argv[3]) };
Object.assign(known, { S, bars });
with (new Proxy(known, { has: (t, k) => k in t || !(k in globalThis), get: (t, k) => (k in t ? t[k] : k === Symbol.unscopables ? undefined : stub) })) {
  (function () {
    /*APP*/
    for (const [sym, ext] of [['ES', null], ['CL', null], ['AAPL', { symbol: 'AAPL' }]]) {
      S.symbol = sym; S.ext = ext;
      applyHistory({ kind: 'time', bars });
    }
  })();
}
console.log(JSON.stringify(out));
"""


@needs_node
def test_the_chart_gives_its_indicators_the_holiday_halts_of_its_group():
    # ES and CL (energy: halted 13:30) fold MLK Day's short session into Tuesday, whose pivots are Friday's; a free-data
    # chart (a stock) has no CME calendar: every session a day of its own
    sessions.set_calendar(load_holidays(ROOT / "config"))
    hello = {d.isoformat(): {k: v for k, v in h.items() if k != "name"} for d, h in sessions.holidays().items()}
    src = APPLY_JS.replace("/*APP*/", app_part("applyHistory"))
    r = subprocess.run(["node", "-e", src, str(ROOT / "web" / "ta.js"), json.dumps(_mlk_bars()), json.dumps(hello)],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    got = json.loads(r.stdout.strip().split("\n")[-1])
    assert got["ES"] == got["CL"] == 6950 and round(got["AAPL"], 2) == round((6960 + 6940 + 6955) / 3, 2)


def test_indicator_alerts_on_pivots_fold_a_holiday_halt_as_the_chart():
    # an alert's series runs on the instrument's bars with its holiday group: Tuesday's PP after MLK Day is Friday's
    sessions.set_calendar(load_holidays(ROOT / "config"))
    bars = [SimpleNamespace(**b) for b in _mlk_bars()]
    p = {"a": {"ind": "pivots", "line": "pp", "range": "Daily"}, "b": {"value": 0.0}, "on": "close"}
    w = SimpleNamespace(series=SimpleNamespace(bars=bars, group="equity"), version=1)
    st = ia._State(p, w)
    assert st.pa == 6950 and st.pb == 0.0
    assert ta.runner("pivots", {"range": "Daily"}).step(bars[-1])[0] != 6950   # without the group: Monday's half day
