"""A trend line's alert level is the line the chart draws: straight in market time (the hours the market trades), as
the chart draws it straight in bars, so the two stay together over the daily break, weekends and holidays. The
server (sessions.market_ms, drawings.line_price), the Alerts list's "(now X)" (drawingLinePrice) and the chart past
its loaded bars (web/drawings.js) all measure it the same way."""
import json
import shutil
import subprocess
from datetime import datetime
from pathlib import Path

import pytest

from chartsrv import drawings, sessions
from chartsrv.alerts import AlertEngine
from chartsrv.config import Instrument, load_holidays
from chartsrv.models import Tick
from chartsrv.store import Store

WEB = Path(__file__).resolve().parents[2] / "web"
CONFIG = Path(__file__).resolve().parents[2] / "config"
ES = Instrument("ES", "E-mini S&P", 0.25, 50, "ES")
CL = Instrument("CL", "Crude", 0.01, 1000, "CL", calendar="energy")
H = 3_600_000
needs_node = pytest.mark.skipif(not shutil.which("node"), reason="needs node")


def ct(*a) -> int:
    return int(datetime(*a, tzinfo=sessions.CT).timestamp() * 1000)


def trend(t1, p1, t2, p2, **style):
    return drawings.clean({"type": "trend", "pts": [{"t": t1, "p": p1}, {"t": t2, "p": p2}], "style": style,
                           "alert": {"on": True, "direction": "cross"}}, "t")


def open_bars(start, end, per, group=None):
    """A chart's time bars from start to end: one each period the market trades, as the server builds them."""
    return [{"t": t} for t in range(start, end + 1, per) if sessions.is_open(t, group)]


def hello_holidays():
    """The holidays as hello sends them: by date, without the names."""
    return {d.isoformat(): {k: v for k, v in h.items() if k != "name"} for d, h in sessions.holidays().items()}


# drawings.js in node (as index.html loads it) with a chart of `bars` and, given `market`, an intraday chart of a
# future here (env.market). level(d, t): the drawn line's price at time t, from the bar positions it is drawn at.
HARNESS = r"""
const vm = require('vm'), fs = require('fs'), IN = JSON.parse(fs.readFileSync(0, 'utf8'));
const ctx = { localStorage: { getItem: () => null, setItem() {} }, window: { addEventListener() {} }, document: { addEventListener() {} } };
vm.runInNewContext(['press.js', 'studies.js', 'drawings.js'].map((f) => fs.readFileSync(IN.web + '/' + f, 'utf8')).join(';') +
  ';this.create=createDrawings;this.linePrice=drawingLinePrice;this.marketMs=typeof marketMs==="function"?marketMs:null;', ctx);
const q = () => ({ addEventListener() {}, querySelector: q, querySelectorAll: () => [], classList: { toggle() {} }, style: {} });
let bars = IN.bars || [], per = IN.per || 0, market = IN.market || null;
const D = ctx.create({ chart: { timeScale: () => ({}), priceScale: () => ({ width: () => 0 }), options: () => ({}), applyOptions() {} },
  series: { attachPrimitive() {} }, el: q(), bars: () => bars, periodMs: () => per, market: () => market, tick: () => 0.25, fmt: String,
  symbol: () => 'ES', api: async () => ({ drawings: [] }), toast() {}, alerts: () => [], ui: { bar: q(), edit: q(), paste: q() } });
const level = (d, t) => { const [a, b] = d.pts, la = D.timeToLogical(a.t), lb = D.timeToLogical(b.t);
  return a.p + (b.p - a.p) * (D.timeToLogical(t) - la) / (lb - la); };
const out = {};
"""


def run_js(body: str, **inp) -> dict:
    js = HARNESS + body + "\nconsole.log(JSON.stringify(out));"
    res = subprocess.run(["node", "-e", js], input=json.dumps({"web": str(WEB), **inp}), capture_output=True, text=True)
    assert res.returncode == 0, res.stderr
    return json.loads(res.stdout)


def test_market_ms_counts_only_the_hours_the_market_trades():
    """Within a session it runs with the clock; over the 16:00-17:00 CT break, a weekend (a DST one too) and a
    holiday closure of the instrument's group it stands still."""
    mk = sessions.market_ms
    assert mk(ct(2026, 10, 14, 11)) - mk(ct(2026, 10, 14, 9, 30)) == 1.5 * H
    assert mk(ct(2026, 10, 14, 18)) - mk(ct(2026, 10, 14, 15)) == 2 * H               # the daily break
    assert mk(ct(2026, 10, 19, 9, 55)) - mk(ct(2026, 10, 16, 10)) == 22 * H + 55 * 60_000   # Fri 10:00 to Mon 09:55
    assert mk(ct(2026, 10, 16, 16)) == mk(ct(2026, 10, 17, 12)) == mk(ct(2026, 10, 18, 16, 59))   # shut all weekend
    assert mk(ct(2026, 3, 8, 18)) - mk(ct(2026, 3, 6, 15)) == 2 * H                    # DST starts that Sunday
    assert mk(ct(2026, 11, 1, 18)) - mk(ct(2026, 10, 30, 15)) == 2 * H                 # ...and ends
    assert mk(ct(2025, 12, 31, 9)) - mk(ct(2025, 12, 30, 9)) == 23 * H
    sessions.set_calendar(load_holidays(CONFIG))
    thu, fri = ct(2026, 11, 26, 11), ct(2026, 11, 27, 9)                               # Thanksgiving: halt 12:00 (CL 13:30)
    assert mk(fri, "equity") - mk(thu, "equity") == 1 * H + 16 * H
    assert mk(fri, "energy") - mk(thu, "energy") == 2.5 * H + 16 * H
    assert mk(fri) - mk(thu) == 5 * H + 16 * H                                         # no group: the weekly hours alone
    assert mk(ct(2026, 4, 5, 18), "equity") - mk(ct(2026, 4, 2, 15), "equity") == 2 * H   # Good Friday: no session
    assert mk(ct(2026, 11, 26, 12), "equity") == mk(ct(2026, 11, 26, 16, 30), "equity")


def test_trend_line_alert_fires_at_the_drawn_line_after_the_weekend(tmp_path):
    """The hunt's ES case: Fri 10:00 at 5000 to Fri 15:00 at 5010, extended right. Monday 09:55 the line is 22h55m of
    trading on (+45.83); the level was 5143.83 when the weekend counted, 98 points off the line."""
    d = trend(ct(2026, 10, 16, 10), 5000.0, ct(2026, 10, 16, 15), 5010.0, extendRight=True)
    params = drawings.alert_params(d)
    mon = ct(2026, 10, 19, 9, 55)
    assert drawings.line_price(params, mon, "equity") == pytest.approx(5045.8333333)
    store, fired = Store(tmp_path / "t.db"), []
    eng = AlertEngine(store, {"ES": ES}, None, fired.append)
    store.add_alert("ES", "line", params, False, "")
    eng.reload()
    for i, px in enumerate([5040.0, 5044.0, 5047.0]):
        eng.on_tick(Tick("ES", mon + i * 1000, px, 1))
    assert [e["message"] for e in fired] == ["ES crossed above trend line 5045.83 (last 5047.00)"]


@needs_node
@pytest.mark.parametrize("group,per", [("equity", 300_000), ("energy", 300_000), ("equity", H)])
def test_the_level_is_the_line_the_chart_draws_across_breaks_and_holidays(group, per):
    """Bars for every period the market trades, Thanksgiving week (the halt and the next day's early close, each group
    at its own hours) on the 5m chart, a plain week on the 1h chart: at each bar the drawn line and the server's level
    agree, extended right and left."""
    sessions.set_calendar(load_holidays(CONFIG))
    if per == H:
        start, end, a, b = ct(2026, 10, 13, 17), ct(2026, 10, 21, 16), ct(2026, 10, 15, 9), ct(2026, 10, 15, 14)
    else:
        start, end, a, b = ct(2026, 11, 23, 17), ct(2026, 12, 1, 16), ct(2026, 11, 25, 9), ct(2026, 11, 25, 11, 30)
    bars = open_bars(start, end, per, group)
    d = trend(a, 6800.0, b, 6812.5, extendLeft=True, extendRight=True)
    times = [x["t"] + k for x in bars[::7] for k in (0, per // 3)]
    out = run_js("out.drawn = IN.times.map((t) => level(IN.d, t));", bars=bars, per=per, d=d, times=times)
    params = drawings.alert_params(d)
    server = [drawings.line_price(params, t, group) for t in times]
    assert max(abs(x - y) for x, y in zip(out["drawn"], server)) < 1e-6
    wall = [6800 + 12.5 * (t - a) / (b - a) for t in times]          # what wall-clock time gave: far off by Monday
    assert max(abs(x - y) for x, y in zip(out["drawn"], wall)) > 20


@needs_node
def test_the_alerts_list_shows_the_servers_level():
    """drawingLinePrice (the Alerts list's "now") and marketMs give the server's numbers from hello's holidays, for
    each holiday group, around Good Friday, Thanksgiving, Christmas and DST changes; without holidays (a page before
    them) the weekly hours, as line_price without a group."""
    sessions.set_calendar(load_holidays(CONFIG))
    d = trend(ct(2026, 3, 30, 9), 5000.0, ct(2026, 3, 30, 12), 5003.0, extendRight=True)
    params = drawings.alert_params(d)
    times = [ct(2026, m, day, h, mi) for m, day in [(4, 2), (4, 3), (4, 6), (11, 2), (11, 26), (11, 27), (11, 30),
                                                   (12, 24), (12, 25), (12, 28)] for h, mi in [(8, 0), (12, 10), (13, 40), (16, 30), (17, 5)]]
    groups = ["equity", "energy", "metals"]
    out = run_js("""out.now = IN.groups.map((g) => IN.times.map((t) => ctx.linePrice(IN.p, t, IN.hol, g)));
out.plain = IN.times.map((t) => ctx.linePrice(IN.p, t));
out.mk = IN.groups.map((g) => IN.times.map((t) => ctx.marketMs(t, IN.hol, g)));""",
                 p=params, times=times, hol=hello_holidays(), groups=groups)
    for g, now, mk in zip(groups, out["now"], out["mk"]):
        assert now == pytest.approx([drawings.line_price(params, t, g) for t in times], abs=1e-9)
        assert [x - mk[0] for x in mk] == [sessions.market_ms(t, g) - sessions.market_ms(times[0], g) for t in times]
    assert out["plain"] == pytest.approx([drawings.line_price(params, t) for t in times], abs=1e-9)
    assert len({round(x, 6) for x in out["now"][0]}) > 10


@needs_node
def test_the_chart_goes_on_in_market_time_past_its_bars():
    """A 1m chart holds Monday morning only. A line anchored on Friday (before its first bar) is drawn where the
    bars would put it, so it meets the alert level; a point placed right of the last bar skips the 16:00-17:00
    break; and both round-trip. Without env.market (a free-data chart, an older page), and on a 4h chart whose
    bars don't fit the hour, the chart goes on at the bar length as before."""
    mon = ct(2026, 10, 19, 8)
    bars = [{"t": mon + i * 60_000} for i in range(120)]                     # 08:00 to 09:59
    d = trend(ct(2026, 10, 16, 10), 5000.0, ct(2026, 10, 16, 15), 5010.0, extendRight=True)
    t_last, fri = bars[-1]["t"], ct(2026, 10, 16, 10)
    out = run_js("""out.fri = D.timeToLogical(IN.fri);
out.back = D.logicalToTime(out.fri);
out.level = level(IN.d, IN.last);
out.ahead = D.logicalToTime(119 + 600);
out.aheadL = D.timeToLogical(out.ahead);
per = 4 * 3600e3; out.four = D.timeToLogical(IN.fri);
per = 60000; market = null; out.plain = D.timeToLogical(IN.fri);""",
                 bars=bars, per=60_000, market={"holidays": {}, "group": "equity"}, d=d, fri=fri, last=t_last)
    assert out["fri"] == -(6 + 15) * 60                       # Fri 10:00-16:00 and Sun 17:00 to Mon 08:00, in minutes
    assert out["back"] == fri
    assert out["level"] == pytest.approx(drawings.line_price(drawings.alert_params(d), t_last, "equity"))
    assert out["ahead"] == ct(2026, 10, 19, 20, 59) and out["aheadL"] == 719   # 09:59 + 600 minutes of trading
    assert out["four"] == (fri - mon) / (4 * H) and out["plain"] == (fri - mon) / 60_000


def test_lines_saved_before_keep_working():
    """Alerts stored by earlier versions carry the same params: levels and rays keep their price everywhere they are
    drawn, a line's ends bound it in market time, line_price without a group runs on the weekly hours, and a line
    whose two ends both fall in one closure is upright on the chart (no level)."""
    stored = {"t1": ct(2026, 10, 15, 9), "p1": 6800.0, "t2": ct(2026, 10, 15, 10), "p2": 6802.0, "left": False,
              "right": False, "type": "trend", "direction": "cross", "drawing": "t"}       # as 07e6afb saved it
    assert drawings.line_price(stored, ct(2026, 10, 15, 9, 30)) == 6801.0
    assert drawings.line_price(stored, ct(2026, 10, 15, 10, 1)) is None
    assert drawings.line_price(stored, ct(2026, 10, 15, 8, 59)) is None
    right = {**stored, "right": True}
    assert drawings.line_price(right, ct(2026, 10, 16, 9)) == pytest.approx(6800 + 2 * 23)   # a day on: 23 h of trading
    hline = {"t1": 0, "p1": 6801.25, "t2": 1, "p2": 6801.25, "left": True, "right": True, "type": "hline"}
    assert drawings.line_price(hline, ct(2026, 10, 18, 18), "equity") == 6801.25
    hray = {"t1": ct(2026, 10, 17, 10), "p1": 6801.25, "t2": ct(2026, 10, 17, 10) + 1, "p2": 6801.25, "left": False,
            "right": True, "type": "hray"}                                                # placed on a Saturday
    assert drawings.line_price(hray, ct(2026, 10, 18, 17, 0, 1)) == 6801.25
    assert drawings.line_price(hray, ct(2026, 10, 16, 15, 59)) is None
    upright = {**stored, "t1": ct(2026, 10, 17, 9), "t2": ct(2026, 10, 18, 9), "right": True}
    assert drawings.line_price(upright, ct(2026, 10, 19, 9)) is None
