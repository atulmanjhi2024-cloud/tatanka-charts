"""Fixes from the fifth bug hunt of HTF EMA on seconds charts: the seed (seconds of CPU over millions of stored trades)
ran in a thread of the server, once per request even for the same chart; minutes without a trade count let one
request read weeks of trades; an RTH chart read every all-hours trade in its window; and the chart page asked the
same thing twice, and after a failed answer at every closed bar."""
import asyncio
import json
import pickle
import shutil
from dataclasses import replace
from datetime import datetime
from itertools import chain
from types import SimpleNamespace

import pytest

from chartsrv import htfema, sessions, store, workers
from chartsrv.amt_runner import epoch_ms
from chartsrv.bars import parse_spec
from chartsrv.studies import RthClock
from test_hunt_web_chart import run_app
from test_redesign_htf_seconds import S, build, trades

needs_node = pytest.mark.skipif(not shutil.which("node"), reason="needs node")
RTH = ("08:30", "15:00")


@pytest.fixture(scope="module")
def db(tmp_path_factory):
    """Three days of ES trades and the 1-minute bars built from them, stored as the live feed stores them."""
    sessions.set_calendar({"days": []})
    ticks = trades(3, 5)
    mins = build("1m", ticks)
    path = tmp_path_factory.mktemp("hunt5") / "chart.db"
    st = store.Store(path)
    st.write(ticks, [("ES", b) for b in mins])
    st.close()
    return path, ticks, mins


def _args(path, mins, spec, start, now, rth=None):
    """What /api/htfema hands the seconds job, without live minutes."""
    sp = parse_spec(spec)
    scaled = htfema.GetScaledLength(S, *htfema.chart_period_spec(sp))
    since = htfema.history_ms(S, None, scaled, start, now, sp.size, rth)
    return (path, "ES", S, since, now, [], scaled, start, now, "equity", sp.size, rth)


def _before(path, spec, start, now, rth=None):
    """The answer as the endpoint worked it out before: in this process, the trades read over the whole window."""
    _, sym, s, since, split, live, scaled, *_ = _args(path, [], spec, start, now, rth)
    return htfema.compute(s, chain(store.iter_bars_1m(path, sym, since, split), live), None, scaled, start, now, "equity",
                          None, parse_spec(spec).size, lambda lo, hi: store.iter_closes_1s(path, sym, lo, hi), rth)


# ---- the seed runs in a worker process, one computation for identical requests -----------------------------------------

class _Pool:
    """workers.run stood in: the jobs wait until the test lets them go, then run on pickled copies, as in a worker."""

    def __init__(self):
        self.jobs, self.go = [], asyncio.Event()

    async def run(self, kind, fn, *args):
        self.jobs.append((kind, fn.__name__))
        await self.go.wait()
        fn, args = pickle.loads(pickle.dumps((fn, args)))
        return pickle.loads(pickle.dumps(fn(*args)))


def test_the_seconds_seed_runs_in_a_worker_and_identical_requests_share_it(db, monkeypatch):
    """The seed reads up to 4 million stored trades, seconds of CPU, in a thread of the server, where it held the GIL
    from the live ticks, the store's writes and every other request; every frame, device and reload asking for the same
    chart worked it out again. It runs in a chart worker; identical requests (the same chart, first bar and settings)
    while one is worked out, or later while its HTF bar still forms, share its answer; the last few are kept, and a
    gap fill's new history drops them."""
    import chartsrv.app as A
    path, _, mins = db
    now = mins[-1].t + 60_000   # Friday 12:07 CT: the 12:00 hour forms, due at 13:00
    clock = [now]
    split = mins[-1].t + 60_000
    market = SimpleNamespace(store=SimpleNamespace(path=path), recent_minutes=lambda sym: (split, []),
                             instruments={"ES": SimpleNamespace(rth=RTH, calendar="equity", tick_size=0.25, decimals=2)})
    monkeypatch.setattr(A.app.state, "market", market, raising=False)
    monkeypatch.setattr(A, "now_ms", lambda: clock[0])
    pool = _Pool()
    monkeypatch.setattr(workers, "run", pool.run)
    htfema.forget("ES")
    start = mins[-200].t   # a 15s chart's first bar, 200 minutes back

    async def go():
        out = {}
        asks = [asyncio.ensure_future(A.htf_ema("ES", "15s", start)), asyncio.ensure_future(A.htf_ema("es", "15s", start))]
        await asyncio.sleep(0.05)
        out["running"] = list(pool.jobs)
        pool.go.set()
        out["first"] = await asyncio.gather(*asks)
        clock[0] = now + 600_000                            # 12:17: the same HTF bar still forms
        out["later"] = await A.htf_ema("ES", "15s", start)
        out["later_jobs"] = len(pool.jobs)
        await A.htf_ema("ES", "15s", start, Mode="ExactHTF")   # Exact: no seed, no worker
        await A.htf_ema("ES", "15s", start, rth=True)          # an RTH chart: its own
        await A.htf_ema("ES", "15s", start + 15_000)            # another first bar: its own
        out["others"] = len(pool.jobs)
        clock[0] = epoch_ms(datetime(2026, 10, 16, 13, 0))     # the 12:00 hour closed: worked out again
        out["closed"] = await A.htf_ema("ES", "15s", start)
        out["closed_jobs"] = len(pool.jobs)
        htfema.forget("ES")                                  # a gap fill: the stored history changed
        await A.htf_ema("ES", "15s", start)
        out["forgot"] = len(pool.jobs)
        for i in range(40):                                  # other charts: the last few are kept, not all
            await A.htf_ema("ES", "15s", start - i * 15_000)
        out["kept"] = len(htfema._answers)
        return out
    out = asyncio.run(go())
    htfema.forget("ES")
    assert out["running"] == [("chart", "seconds_job")]           # two requests, one job, in the chart workers
    a, b = out["first"]
    assert a == b == {**_before(path, "15s", start, now), "next": None}
    assert a["smooth"]["count"] >= a["scaled"] and a["due"] == epoch_ms(datetime(2026, 10, 16, 13, 0))
    assert out["later"] == {**_before(path, "15s", start, now + 600_000), "next": None} and out["later_jobs"] == 1
    assert out["others"] == 3
    assert out["closed"] == {**_before(path, "15s", start, clock[0]), "next": None} and out["closed_jobs"] == 4
    assert out["forgot"] == 5
    assert out["kept"] <= htfema.SHARED < 40


def test_the_seconds_job_runs_in_a_real_worker_process(db, monkeypatch):
    """The job and what it is given pickle, and the worker (spawned, as the server's) gives the same answer."""
    path, _, mins = db
    now = mins[-1].t + 60_000
    start = mins[-100].t
    monkeypatch.setenv("CHART_WORKERS", "1")
    workers.shutdown()   # workers of this test's own (an earlier test's may hold another holiday calendar)
    workers.start()
    try:
        got = asyncio.run(workers.run("chart", htfema.seconds_job, *_args(path, mins, "5s", start, now)))
        assert "chart" in workers._pools   # it ran in the pool, not in a thread after the pool broke
    finally:
        workers.shutdown()
    assert got == _before(path, "5s", start, now)


# ---- minutes without a trade count still bound the trades read ----------------------------------------------------------

def test_the_trade_limit_holds_where_the_minutes_have_no_trade_count(db, tmp_path, monkeypatch):
    """Backfill stores 1-minute bars without a trade count (n=0); a Databento import after it stores their trades and
    keeps those minutes. The window was sized by adding up n, so for them it grew until 4 lengths of chart bars were
    met: 4 x EmaLength HTF bars of trading (weeks of trades on a 4h or Day source). A minute without a count counts as
    its volume, never less than its trades."""
    path, ticks, mins = db
    copy = tmp_path / "imported.db"
    st = store.Store(copy)
    st.write(ticks, [("ES", replace(b, n=0)) for b in mins])   # every trade stored, no minute counting them
    st.close()
    monkeypatch.setattr(htfema, "SECONDS_TRADES", 20_000)
    start, now = mins[-100].t, mins[-1].t + 60_000
    asked = []

    def read(lo, hi):
        asked.append((lo, hi))
        return store.iter_closes_1s(copy, "ES", lo, hi)
    _, sym, s, since, split, live, scaled, *_ = _args(copy, mins, "15s", start, now)
    d = htfema.compute(s, store.iter_bars_1m(copy, sym, since, split), None, scaled, start, now, chart_seconds=15,
                       second_closes=read)
    (lo, hi), = asked
    read_n = sum(1 for t in ticks if lo <= t.ts < hi)
    assert hi == start and 0 < read_n <= 20_000, read_n
    assert d["smooth"]["count"] >= d["scaled"]


# ---- an RTH seconds chart reads only its regular hours' trades ----------------------------------------------------------

def test_an_rth_seconds_chart_reads_only_its_regular_hours_trades(db, monkeypatch):
    """With RTH only on, the window was sized from the regular-hours minutes but the trades were read over all of it,
    nights too (a third of ES's trades, more of CL's and GC's), and dropped afterwards; each second's RTH open was
    worked out again. The trades are read one RTH window at a time and the open once a day, for the same seed."""
    path, ticks, mins = db
    now = mins[-1].t + 60_000
    start = next(m.t for m in reversed(mins) if RthClock(RTH).day(m.t) is not None and m.t < now - 3600_000)
    _, sym, s, since, split, live, scaled, *_ = _args(path, mins, "1s", start, now, RTH)
    hours = RthClock(RTH)
    rth_mins = [m for m in store.iter_bars_1m(path, sym, since, split) if hours.day(m.t) is not None]
    # the seed as worked out before: every second of the window, the night's dropped, the RTH open of each one
    want = htfema.seconds_seed(rth_mins, 1, scaled, start, lambda lo, hi: (
        x for x in store.iter_closes_1s(path, sym, lo, hi) if hours.day(x[0]) is not None),
        lambda ts: hours.window(sessions.to_ct(ts))[0])
    spans, rows, opens = [], [0], [0]

    def read(lo, hi):
        spans.append((lo, hi))
        for x in store.iter_closes_1s(path, sym, lo, hi):
            rows[0] += 1
            yield x
    real_window = RthClock.window
    monkeypatch.setattr(RthClock, "window", lambda self, d: opens.__setitem__(0, opens[0] + 1) or real_window(self, d))
    d = htfema.compute(s, store.iter_bars_1m(path, sym, since, split), None, scaled, start, now, chart_seconds=1,
                       second_closes=read, rth=RTH)
    assert d["smooth"] == want and d["smooth"]["count"] > 0
    assert len(spans) >= 2   # the chart starts on Friday: Thursday's and Wednesday's regular hours at least
    for lo, hi in spans:     # each inside one day's regular hours
        a, b = sessions.to_ct(lo), sessions.to_ct(hi - 1)
        assert a.date() == b.date() and (8, 30) <= (a.hour, a.minute) and (b.hour, b.minute) < (15, 0), (a, b)
    lo = spans[0][0]
    assert rows[0] == len({t.ts // 1000 for t in ticks if lo <= t.ts < start and hours.day(t.ts) is not None})
    assert opens[0] < 30, opens   # once a day (and once per window found), not once a second


# ---- the chart page asks once for what it asks, and waits after a failed answer -----------------------------------------

HEMA_JS = """
let clock = 1.8e12;
Date.now = () => clock;
const asked = [];
const S = { st: { hema: true }, symbol: 'ES', spec: '1s', bars: [{ t: clock - 3000e3, c: 1 }], instruments: { ES: {} }, replay: null,
  hemaSet: { HtfPeriodType: 'Minute', HtfPeriod: 60, EmaLength: 21, Mode: 'SmoothScaled' }, hema: null };
const indReady = () => true, hemaDraw = () => {}, hemaLive = () => {};
let due = clock + 1800e3, fail = false, answer = null;
const api = (method, path) => { asked.push(path); return new Promise((ok, no) => {
  answer = () => (fail ? no(new Error('504 Gateway Time-out')) : ok({ mode: 'SmoothScaled', htf: [], due, next: null })); }); };
/*APP*/
const settle = () => new Promise((r) => setTimeout(r, 1));
const bar = async (t) => { clock = t; S.bars.push({ t, c: 1 }); hemaTick(true); await settle(); return asked.length; };
const land = async () => { const f = answer; answer = null; if (f) f(); await settle(); await settle(); return asked.length; };
(async () => {
  const t0 = clock, out = {};
  hemaRequest();                                            // the history (applyHistory)
  for (let i = 1; i <= 5; i++) await bar(t0 + i * 1000);    // a 1s chart: bars close while the server works on it
  out.open = await land();                                  // its answer: the same request not sent again
  due = t0 + 5400e3;
  out.htfClosed = await bar(t0 + 1800e3);                   // the forming HTF bar closed: asked again ...
  for (let i = 1; i <= 5; i++) await bar(t0 + 1800e3 + i * 1000);
  out.htfAnswer = await land();                             // ... once
  S.hema = null; hemaRequest();                             // new history (a reload, a timeframe change) ...
  hemaRequest();                                            // ... and the settings changed while it is asked
  await bar(t0 + 1810e3);
  out.again = [asked.length, await land(), await land()];   // asked once more when it answers, for the new ones
  fail = true;
  S.hema = null; hemaRequest();                             // the next history's ask fails (a proxy's 504)
  await land();
  const t1 = clock, failed = asked.length;
  for (let i = 1; i <= 180; i++) { await bar(t1 + i * 1000); if (answer) await land(); }
  out.failed = asked.length - failed;                       // three minutes of closed bars: asked again once a minute
  fail = false;
  for (let i = 181; i <= 250; i++) { await bar(t1 + i * 1000); if (answer) await land(); }
  out.recovered = [asked.length - failed, !!S.hema];
  console.log(JSON.stringify(out));
})();"""


@needs_node
def test_the_chart_page_asks_once_and_waits_a_minute_after_a_failed_answer():
    """On a seconds chart a bar closes every second; each one closing while the request was out (applyHistory had set
    the answer to none, or its due had passed) asked again, so the same request went out a second time when it
    answered: two of each, at the chart's open, every HTF close and every trim. After a failed answer every closed bar
    asked again at once. Now a closed bar doesn't ask while one is out (new history and settings still do, once it
    answers), and after a failed answer it asks again a minute later."""
    out = run_app(["hemaBusy", "hemaRequest", "hemaTick"], HEMA_JS)
    assert out == {"open": 1, "htfClosed": 2, "htfAnswer": 2, "again": [3, 4, 4], "failed": 2, "recovered": [3, True]}
