"""Regressions from the third bug hunt, studies: an AMT alert runner rebuilt mid-bar does not send that bar's alerts
again; the same levels set again keep the flips history gave them; a holiday halt's own levels are not stale. The HTF
EMA's Day bars fold a holiday halt as the chart's 1D bars do, and its Smooth start on D, 2D and 3D charts comes from
the chart's own bars. Bar replay shows a 2D or 3D bar over a weekend once the next one opens, not on Saturday,
and the HTF EMA loads on a chart, or a replay, that starts with no bars once they come."""
import asyncio
import json
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from amt_parity import synth
from chartsrv import amt, amt_hub, htfema, sessions
from chartsrv.amt_runner import AmtRunner, ct, epoch_ms
from chartsrv.bars import make_series, parse_spec
from chartsrv.config import load_holidays
from chartsrv.models import Tick
from chartsrv.store import Store
from chartsrv.tpo_service import session_end
from test_hunt_web_chart import needs_node, run_app

CONFIG = Path(__file__).resolve().parents[2] / "config"


def test_amt_alert_runner_rebuilt_in_the_bar_does_not_send_its_notices_again(tmp_path, monkeypatch):
    """Inventory Trap Active goes out at 08:30. A gap fill (Market.history_changed -> reload) or a server restart in
    the same 5-minute bar builds the alert runner again from history: its first step does not send the notice again."""
    bars = synth(15, 7, datetime(2026, 10, 2, 12, 0))
    i = next(k for k, b in enumerate(bars) if ct(b.t) == datetime(2026, 10, 2, 8, 30))
    store = Store(tmp_path / "c.db")
    store.write([], [("ES", b) for b in bars[:i]])
    store.set_setting("amt_alerts", {"ES": {"on": True}})
    now = [bars[i - 1].t + 30_000]
    monkeypatch.setenv("CHART_WORKERS", "0")
    monkeypatch.setattr(amt_hub.time, "time", lambda: now[0] / 1000)
    inst = SimpleNamespace(tick_size=0.25, rth=("08:30", "15:00"), calendar="equity")
    m = SimpleNamespace(store=store, instruments={"ES": inst}, m1={"ES": SimpleNamespace(bars=[bars[i - 1]])},
                        last_tick={"ES": Tick("ES", now[0], bars[i - 1].c, 1)}, flush_for_read=lambda *a: [],
                        daily=SimpleNamespace(current=lambda sym: ([], None, "")))
    sent = []

    def hub():
        h = amt_hub.AmtHub(m)
        h._emit = lambda sym, t, price, text: sent.append(text)
        return h

    async def go():
        h = hub()
        await h.entry("ES", amt_hub.ALERT_MINUTES)
        h.step()
        assert sent == []
        now[0] = bars[i].t + 30_000                            # 08:30 live
        m.m1["ES"].bars.append(bars[i])
        m.last_tick["ES"] = Tick("ES", now[0], bars[i].c, 1)
        h.on_minute("ES", bars[i])
        h.step()
        assert len(sent) == 1 and sent[0].startswith("[ES] Inventory Trap Active @ ")
        h.reload("ES")                                         # a gap fill, still in the 08:30 bar
        await h.entry("ES", amt_hub.ALERT_MINUTES)
        h.step()
        restarted = hub()
        await restarted.entry("ES", amt_hub.ALERT_MINUTES)
        restarted.step()
        assert len(sent) == 1
    asyncio.run(go())


def test_amt_same_levels_set_again_keep_their_flips():
    """After every history load and every ML levels poll the hub sets the symbol's levels again, freshly parsed (none
    flipped). The same levels keep the flips history gave them, as NinjaTrader replaces them only when the file
    changes: resistance broken in history, with price still inside the flip band, stays FLIPPED."""
    bars = synth(25, 7, datetime(2026, 10, 2, 12, 0))
    last = bars[-1].c
    levels = lambda: amt.levels_from_prices(last - 100, [last - 4 + 0.25 * k for k in range(20)] + [last - 150])   # noqa: E731
    r = AmtRunner("ES", 0.25, 5, amt.Settings(), calendar="equity")
    r.set_level_list(levels(), None, None)
    for b in bars:
        r.add_minute(b)
    r.commit_ready()
    flipped = lambda: sorted(x.Name for x in r.preview()[0].Lines if x.Name.startswith("FLIPPED"))   # noqa: E731
    before = flipped()
    assert len(before) == 20
    r.set_level_list(levels(), None, None)
    assert flipped() == before
    r.set_level_list(amt.levels_from_prices(last - 100, [last - 4, last + 50, last - 150]), None, None)   # new ones
    assert len(flipped()) <= 1 and any(x.Name == "RES2" and x.Price == last + 50 for x in r.preview()[0].Lines)


@pytest.mark.parametrize("cal, levels, at, stale", [
    ("equity", "2026-01-19", datetime(2026, 1, 18, 17, 30), False),    # MLK Day's halt session: Monday's levels
    ("equity", "2026-01-19", datetime(2026, 1, 19, 11, 30), False),
    ("equity", "2026-01-19", datetime(2026, 1, 19, 18, 0), True),      # Tuesday's session, still Monday's levels
    ("equity", "2026-11-26", datetime(2026, 11, 25, 17, 30), False),   # Thanksgiving
    ("energy", "2026-06-19", datetime(2026, 6, 19, 9, 0), False),      # CL on Juneteenth
    ("equity", "2026-10-20", datetime(2026, 10, 21, 9, 0), True),      # an ordinary day
    ("equity", "2026-10-21", datetime(2026, 10, 20, 18, 0), False),
])
def test_amt_stale_levels_on_a_holiday_halt_compares_calendar_trading_dates(cal, levels, at, stale):
    """Levels are dated by the calendar trading day (levels file or link, paste lines), so STALE LEVELS compares that with
    the bar's calendar trading date, as the original: a halt's short session folds into the next trade date for the
    profile and daily bars, but its own levels are not a day old."""
    sessions.set_calendar(load_holidays(CONFIG))
    r = AmtRunner("ES", 0.25, 30, amt.Settings(), calendar=cal)
    r.set_level_list(amt.levels_from_prices(6900.0, [6920.0, 6880.0]), datetime.fromisoformat(levels), None)
    w = r.warning(SimpleNamespace(Bar=SimpleNamespace(Start=at), Warning=""))
    assert w.startswith("STALE LEVELS") == stale


def test_htf_ema_day_bars_fold_a_holiday_halt_as_the_charts_daily_bars():
    """Source timeframe Day: MLK Day's short session is in Tuesday's bar, as on the chart's 1D bars, so on a 1D chart
    the HTF bars are the chart's own (as NinjaTrader's Day series is there). Until Tuesday's close that bar is forming."""
    sessions.set_calendar(load_holidays(CONFIG))
    m1 = [b for b in synth(40, 5, datetime(2026, 1, 30, 15, 59)) if sessions.is_open(b.t, "equity")]
    series = make_series(parse_spec("1D"), 0.25, 2, 5000, group="equity")
    for b in m1:
        series.on_bar(b)
    s = htfema.settings({"HtfPeriodType": "Day", "HtfPeriod": 1, "Mode": "ExactHTF"})
    d = htfema.compute(s, m1, 1440, 21, series.bars[0].t, m1[-1].t + 30 * 60_000, "equity")
    ema = htfema.NtEma(21)
    assert d["htf"] == [[session_end(b.t), v if i >= 20 else None]
                        for i, (b, v) in enumerate((b, ema.add(b.c)) for b in series.bars)]
    after_halt = epoch_ms(datetime(2026, 1, 19, 16, 30))   # MLK Day, after the 12:00 halt
    d = htfema.compute(s, [b for b in m1 if b.t < after_halt], 1440, 21, series.bars[0].t, after_halt, "equity")
    assert d["htf"][-1][0] == epoch_ms(datetime(2026, 1, 16, 16, 0)) and d["due"] == epoch_ms(datetime(2026, 1, 20, 16, 0))


@pytest.mark.parametrize("spec", ["1D", "2D", "3D"])
def test_htf_ema_smooth_start_on_daily_charts_comes_from_the_charts_own_bars(spec):
    """Smooth's chart EMA before the first bar shown runs over the chart's bars: 2D and 3D bars of several sessions,
    1D bars with a holiday halt's session in the next day's (Thanksgiving here), not one bar per session."""
    sessions.set_calendar(load_holidays(CONFIG))
    m1 = [b for b in synth(75, 3, datetime(2026, 2, 6, 12, 7)) if sessions.is_open(b.t, "equity")]
    sp = parse_spec(spec)
    series = make_series(sp, 0.25, 2, 5000, group="equity")
    for b in m1:
        series.on_bar(b)
    bars = list(series.bars)
    k = len(bars) // 2
    s = htfema.settings({"HtfPeriodType": "Day", "HtfPeriod": 1, "Mode": "SmoothScaled"})
    scaled = htfema.GetScaledLength(s, *htfema.chart_period_spec(sp))   # 21, 10, 7
    d = htfema.compute(s, m1, sp.size // 60, scaled, bars[k].t, m1[-1].t + 60_000, "equity",
                       make_series(sp, 0.25, 2, 1, group="equity").bucket)
    ema = htfema.NtEma(scaled)
    for b in bars[:k]:
        ema.add(b.c)
    assert d["smooth"] == {"seed": ema.values[-1], "count": k}


REPLAY_JS = """
const S = { symbol: 'ES', spec: SPEC, rth: false, replay: null };
const D = DATA, out = [];
const api = async () => D.d, applyHistory = () => {}, applyBars = () => {}, toast = () => {}, replayUi = () => {}, replayStudies = () => {};
const fail = (e) => { throw e; }, replayMore = () => {};
/*APP*/
(async () => {
  S.replay = { at: D.at, data: [], idx: 0, kind: 'time', period: 0, nextAt: null, req: 0, busy: false };
  await replayLoad();
  const r = S.replay;
  out.push(r.idx);
  replayStep();
  out.push(r.at);
  r.data = r.data.slice(0, r.idx); r.nextAt = D.next;   // the last bar of a stretch, the next one's open known
  r.idx -= 1;
  replayStep();
  out.push(r.at);
  console.log(JSON.stringify(out));
})();
"""


@needs_node
@pytest.mark.parametrize("spec", ["2D", "3D"])
def test_bar_replay_shows_a_bar_over_a_weekend_once_the_next_one_opens(spec):
    """A 2D or 3D bar holding Friday and Monday ends with Monday's close, not two or three days after it opened
    (Saturday 17:00): at a replay clock on Monday it is not shown yet, and stepping onto it moves the clock past
    Monday, to the next bar's open (the next stretch's first, when it is the last of its stretch)."""
    m1 = synth(21, 4, datetime(2026, 10, 22, 12, 0))
    series = make_series(parse_spec(spec), 0.25, 2, 5000)
    last = {}
    for m in m1:
        series.on_bar(m)
        last[series.bars[-1].t] = m.t
    bars = [{"t": b.t, "o": b.o, "h": b.h, "l": b.l, "c": b.c, "v": b.v, "seq": b.seq} for b in series.bars]
    days = int(spec[0]) * 86_400_000
    i = next(k for k, b in enumerate(bars[:-1]) if last[b["t"]] > b["t"] + days)   # trades on after its days
    assert ct(last[bars[i]["t"]]).weekday() == 0
    d = {"bars": bars, "kind": "time", "spec": spec, "next_at": None}
    js = REPLAY_JS.replace("SPEC", json.dumps(spec)).replace(
        "DATA", json.dumps({"d": d, "at": last[bars[i]["t"]] - 5 * 3_600_000, "next": bars[i + 1]["t"]}))   # Monday
    shown, at, at_last = run_app(["specSeconds", "daysSpec", "barEnd", "shownCount", "replayLoad", "replayStep"], js)
    assert shown == i and at == at_last == bars[i + 1]["t"] > last[bars[i]["t"]]


@needs_node
@pytest.mark.parametrize("replay", [True, False])
def test_htf_ema_is_asked_for_when_the_first_bars_come(replay):
    """A replay started before the chart's first bar (or a chart whose history came empty) has no bars to ask the HTF
    EMA for: it is asked for when the first one comes, not never (replay asks nothing more) or once it closes."""
    out = run_app(["hemaBusy", "hemaRequest", "hemaTick"], """
const asked = [];
const S = { st: { hema: true }, symbol: 'ES', spec: '5m', bars: [], instruments: { ES: {} }, replay: REPLAY,
  hemaSet: { HtfPeriodType: 'Minute', HtfPeriod: 60, EmaLength: 21, Mode: 'SmoothScaled' }, hema: null };
const indReady = () => true, hemaDraw = () => {}, hemaLive = () => {};
const api = async (method, path) => { asked.push(path); return { mode: 'SmoothScaled', htf: [] }; };
/*APP*/
(async () => {
  await hemaRequest();   // the history: no bars yet
  const before = asked.length;
  S.bars.push({ t: 1.8e12, c: 1 });
  hemaTick(false);   // the first bar
  await new Promise((r) => setTimeout(r, 10));
  hemaTick(false);
  await new Promise((r) => setTimeout(r, 10));
  console.log(JSON.stringify({ before, after: asked.length, drawn: !!S.hema }));
})();""".replace("REPLAY", "{}" if replay else "null"))
    assert out == {"before": 0, "after": 1, "drawn": True}
