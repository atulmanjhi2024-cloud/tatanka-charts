"""Older history (older.py): the bars before a chart's first one, for a chart scrolled or zoomed out past it, are the
bars one long chart would have there, request after request: minute and longer bars from the stored 1-minute bars,
seconds and tick bars from the stored trades (stopping where those do), volume, range and Renko bars from the trades,
or from 1-minute bars for a session without its trades."""
import random
from datetime import datetime

import pytest
from test_app import client  # noqa: F401  (the app on a database of the test's own)

from chartsrv import sessions
from chartsrv.bars import make_series, parse_spec
from chartsrv.market import build_history
from chartsrv.models import Bar, Tick
from chartsrv.older import build_older, minute_prints
from chartsrv.sessions import CT
from chartsrv.store import Store
from chartsrv.studies import RthClock

RTH = ("08:30", "15:00")
CLOCK = RthClock(RTH)
BIG = 2 ** 62
MIN = 60_000


def ms(*a) -> int:
    return int(datetime(*a, tzinfo=CT).timestamp() * 1000)


def walk(start, end, step, seed):
    rnd, px, t = random.Random(seed), 6800.0, start
    while t < end:
        if sessions.is_open(t):
            px = round(px + rnd.randint(-6, 6) * 0.25, 2)
            yield t, px
        t += step


def minutes(start, end, seed=1):
    out = []
    for t, p in walk(start, end, MIN, seed):
        v = 10 + t // MIN % 7
        out.append(Bar(t=t, o=p, h=p + 0.5, l=p - 0.75, c=p + 0.25, v=v, bv=v // 2, sv=v // 3, n=4))
    return out


def ticks(start, end, every=10_000, seed=2):
    return [Tick("ES", t, p, 1 + t // every % 5, 1 if t // every % 2 else -1) for t, p in walk(start, end, every, seed)]


def series(spec, maxlen=10 ** 6, rth_only=False):
    return make_series(parse_spec(spec), 0.25, 2, maxlen, RTH, rth_only=rth_only, group="equity")


def chart(path, spec, split, now, maxlen, rth_only=False):
    s = series(spec, maxlen, rth_only)
    build_history(path, s, "ES", parse_spec(spec), BIG, split, now)
    return s


def older(path, spec, before, count, rth_only=False):
    return build_older(path, "ES", parse_spec(spec), 0.25, 2, RTH, rth_only, "equity", before, count, BIG)


def key(b):
    b = b if isinstance(b, dict) else b.to_dict()
    return (b["t"], b["o"], b["h"], b["l"], b["c"], b["v"], b["d"], tuple(b.get("st") or ()))


def from_minutes(spec, bars, rth_only=False):
    """One long chart over every stored minute."""
    s = series(spec, rth_only=rth_only)
    s.bulk = True
    for b in bars:
        s.on_bar(b)
    s.end_bulk()
    return [key(b) for b in s.bars]


def from_trades(spec, trades):
    s = series(spec)
    s.bulk = True
    for tk in trades:
        s.on_tick(tk)
    s.end_bulk()
    return [key(b) for b in s.bars]


@pytest.fixture
def three_weeks(tmp_path):
    store = Store(tmp_path / "t.db")
    m = minutes(ms(2026, 10, 4, 17), ms(2026, 10, 23, 16))   # Sunday Oct 4 to Friday Oct 23
    store.insert_bars_if_missing("ES", m)
    return store, m


@pytest.mark.parametrize("spec,rth_only", [("5m", False), ("30m", False), ("1h", False), ("30m", True), ("4h", False)])
def test_minute_bars_go_on_back_as_one_long_chart(three_weeks, spec, rth_only):
    store, m = three_weeks
    split = now = ms(2026, 10, 23, 16)
    c = chart(store.path, spec, split, now, 120, rth_only)
    whole = from_minutes(spec, m, rth_only)
    assert [key(b) for b in c.bars] == whole[-len(c.bars):]
    got, before = [], c.bars[0].t
    for _ in range(50):   # request after request, as the page scrolls left
        d = older(store.path, spec, before, 100, rth_only)
        assert len(d["bars"]) <= 100 and not d["approx"]
        got = d["bars"] + got
        if not d["more"]:
            break
        before = d["bars"][0]["t"]
    assert not d["more"]
    assert [key(b) for b in got] + [key(b) for b in c.bars] == whole[-len(got) - len(c.bars):]
    assert len(got) + len(c.bars) >= len(whole) - 1   # all the way back (a first partial bar aside)


def test_daily_bars_step_over_a_gap(tmp_path):
    store = Store(tmp_path / "t.db")
    m = minutes(ms(2026, 6, 1, 17), ms(2026, 6, 26, 16)) + minutes(ms(2026, 9, 13, 17), ms(2026, 10, 23, 16))
    store.insert_bars_if_missing("ES", m)
    split = now = ms(2026, 10, 23, 16)
    c = chart(store.path, "1D", split, now, 3000)
    assert c.bars[0].t == m[0].t   # 1D: 400 days back, all of it already
    c = chart(store.path, "1D", split, now, 10)      # a chart holding only ten
    got, before = [], c.bars[0].t
    while True:   # five at a time: one request steps over the summer's eleven weeks with nothing stored
        d = older(store.path, "1D", before, 5)
        assert 0 < len(d["bars"]) <= 5
        got = d["bars"] + got
        if not d["more"]:
            break
        before = d["bars"][0]["t"]
    assert [key(b) for b in got] + [key(b) for b in c.bars] == from_minutes("1D", m)


def test_seconds_bars_stop_where_the_trades_do(tmp_path):
    store = Store(tmp_path / "t.db")
    store.insert_bars_if_missing("ES", minutes(ms(2026, 10, 18, 17), ms(2026, 10, 23, 16)))
    tk = ticks(ms(2026, 10, 21, 9), ms(2026, 10, 23, 16))
    store.write(tk, [])
    now = ms(2026, 10, 23, 15, 59)
    c = chart(store.path, "15s", now // MIN * MIN, now, 300)
    whole = from_trades("15s", tk)
    got, before = [], c.bars[0].t
    while True:
        d = older(store.path, "15s", before, 500)
        got = d["bars"] + got
        if not d["more"]:
            break
        before = d["bars"][0]["t"]
    assert [key(b)[:7] for b in got] == [b[:7] for b in whole[:len(got)]]   # VWAP: from the minutes before the trades
    assert got[0]["t"] >= tk[0].ts and len(got) + len(c.bars) == len(whole)


@pytest.mark.parametrize("spec", ["500t", "2000v", "12r", "6k"])
def test_tick_built_bars_go_back_session_by_session(tmp_path, spec):
    store = Store(tmp_path / "t.db")
    tk = ticks(ms(2026, 10, 18, 17), ms(2026, 10, 23, 16))   # Sunday to Friday: five sessions of trades
    store.write(tk, [])
    store.insert_bars_if_missing("ES", minutes(ms(2026, 10, 18, 17), ms(2026, 10, 23, 16)))
    now = ms(2026, 10, 23, 15, 59)
    c = chart(store.path, spec, now // MIN * MIN, now, 3000)   # the session before and this one
    assert sessions.to_ct(c.bars[0].t).day == 21
    whole = from_trades(spec, tk)
    assert [key(b) for b in c.bars] == whole[-len(c.bars):]
    d = older(store.path, spec, c.bars[0].t, 50)   # whole sessions: at least 50 bars
    assert (len(d["bars"]) >= 50 or not d["more"]) and not d["approx"]
    got = d["bars"]
    while d["more"]:
        d = older(store.path, spec, got[0]["t"], 50)
        got = d["bars"] + got
    assert [key(b) for b in got] + [key(b) for b in c.bars] == whole


def test_volume_bars_fall_back_to_minutes_where_no_trades_are_stored(tmp_path):
    store = Store(tmp_path / "t.db")
    m = minutes(ms(2026, 10, 11, 17), ms(2026, 10, 23, 16))
    store.insert_bars_if_missing("ES", m)
    tk = ticks(ms(2026, 10, 20, 17), ms(2026, 10, 23, 16))   # trades from Wednesday's session only
    store.write(tk, [])
    now = ms(2026, 10, 23, 15, 59)
    c = chart(store.path, "2000v", now // MIN * MIN, now, 3000)
    d = older(store.path, "2000v", c.bars[0].t, 5000)
    assert d["approx"] and not d["more"]
    # the sessions before Wednesday's from their minutes' prints, Wednesday's from its trades
    split = ms(2026, 10, 20, 17)
    want = from_trades("2000v", [p for b in m if b.t < split for p in minute_prints("ES", b)] + tk)
    assert [key(b) for b in d["bars"]] + [key(b) for b in c.bars] == want
    v = sum(b["v"] for b in d["bars"] if b["t"] < split)
    assert v == sum(b.v for b in m if b.t < split)   # every contract, once
    # a tick chart has no such fallback: it stops where the trades do
    c = chart(store.path, "500t", now // MIN * MIN, now, 3000)
    d = older(store.path, "500t", c.bars[0].t, 5000)
    assert d["bars"] and d["bars"][0]["t"] >= split and not d["more"] and not d["approx"]


def test_minute_prints_keep_the_minutes_range_and_volume():
    b = Bar(t=ms(2026, 10, 20, 9), o=10.0, h=12.0, l=9.0, c=11.0, v=11, bv=6, sv=4)
    p = minute_prints("ES", b)
    assert [x.price for x in p][0] == 10.0 and p[-1].price == 11.0
    assert max(x.price for x in p) == 12.0 and min(x.price for x in p) == 9.0
    assert [x.price for x in p].index(9.0) < [x.price for x in p].index(12.0)   # an up minute: low first
    assert sum(x.size for x in p) == 11 and all(b.t <= x.ts < b.t + MIN for x in p)
    down = minute_prints("ES", Bar(t=b.t, o=11.0, h=12.0, l=9.0, c=10.0, v=3))
    assert [x.price for x in down].index(12.0) < [x.price for x in down].index(9.0)


def test_more_when_everything_stored_is_more_than_asked_for(tmp_path):
    store = Store(tmp_path / "t.db")
    m = minutes(ms(2026, 10, 11, 17), ms(2026, 10, 23, 16))
    store.insert_bars_if_missing("ES", m)
    whole = from_minutes("30m", m)
    d = older(store.path, "30m", ms(2026, 10, 23, 16), len(whole) - 10)   # every stored minute in one window
    assert len(d["bars"]) == len(whole) - 10 and d["more"]
    d = older(store.path, "30m", d["bars"][0]["t"], 100)
    assert [key(b) for b in d["bars"]] == whole[:10] and not d["more"]


def test_nothing_older(tmp_path):
    store = Store(tmp_path / "t.db")
    store.insert_bars_if_missing("ES", minutes(ms(2026, 10, 20, 17), ms(2026, 10, 21, 16)))
    for spec in ("5m", "1D", "15s", "500t", "2000v"):
        d = older(store.path, spec, ms(2026, 10, 20, 17), 100)
        assert d == {"bars": [], "more": False, "approx": False, "next": None}


def test_the_api(client, tmp_path):
    Store(tmp_path / "chart.db").insert_bars_if_missing("ES", minutes(ms(2026, 10, 19, 17), ms(2026, 10, 23, 16)))
    before = ms(2026, 10, 23, 8, 30)
    r = client.get(f"/api/older?symbol=es&spec=30m&before={before}&count=20")
    assert r.status_code == 200
    d = r.json()
    assert len(d["bars"]) == 20 and d["more"] and d["bars"][-1]["t"] == before - 30 * MIN
    assert client.get(f"/api/older?symbol=ZZ&spec=30m&before={before}").status_code == 404
    assert client.get(f"/api/older?symbol=ES&spec=7x&before={before}").status_code == 400
    assert client.get(f"/api/older?symbol=ES&spec=5m&before={before}&count=0").status_code == 400


# ---- the chart page: older bars in front of the first, numbered on down, the view where it was ----------------------

PAGE_JS = """
(async () => {
let range = { from: 2, to: 40 }, answers = [], asked = [], toasts = [];
const chart = { timeScale: () => ({ getVisibleLogicalRange: () => range, setVisibleLogicalRange: (r) => { range = r; } }) };
const S = { ext: null, replay: null, liveWait: false, symbol: 'ES', spec: '30m', rth: false, instruments: { ES: {} },
  bars: [1, 2, 3, 4, 5].map((i) => ({ t: 1000 * i, seq: i })), seqTime: new Map(), lastBar: null, loadNote: '' };
const api = (m, url) => { asked.push(Object.fromEntries(new URLSearchParams(url.split('?')[1]))); return answers.shift()(); };
const toast = (m) => toasts.push(m), loadNote = (n) => { S.loadNote = n; }, calSpec = () => false, specLabel = (s) => s;
const toPoints = () => [{}, {}], candles = { setData() {} }, vol = { setData() {} }, cmpLines = [], drawings = { update() {} };
const [altSetData, setStudyData, indsUpdate, hemaDraw, dsmaDraw, cmpAlign, hemaRequest, dsmaRequest, fillsSchedule, drawAvwap, legend]
  = Array(11).fill(() => {});
const anchorMs = () => null;
function barIndexTo(bars, ms) { let i = -1; bars.forEach((b, k) => { if (b.t <= ms) i = k; }); return i; }
/*APP*/
const tick = () => new Promise((r) => setTimeout(r, 0));
const bars = (from, to) => { const o = []; for (let t = from; t <= to; t += 100) o.push({ t, seq: 7 }); return o; };
const out = {};
olderReset();
answers.push(() => ({ bars: bars(100, 900), more: true, approx: false, next: null }));
answers.push(() => ({ bars: [], more: true, approx: false, next: 50 }));   // a stretch with nothing stored
answers.push(() => ({ bars: [{ t: 10, seq: 7 }, { t: 20, seq: 7 }], more: false, approx: true, next: null }));
olderCheck();
for (let i = 0; i < 20; i++) await tick();
out.seqs = S.bars.map((b) => b.seq);
out.times = S.bars.map((b) => b.t);
out.range = range;
out.asked = asked.map((q) => [q.before, q.count]);
out.toasts = toasts;
out.more = S.older.more;
// an answer for bars since replaced (another timeframe) is dropped
range = { from: 1, to: 30 };
olderReset();
S.bars = [{ t: 5000, seq: 1 }];
let release;
answers.push(() => new Promise((r) => { release = r; }));
olderCheck();
await tick();
S.bars = [{ t: 7000, seq: 1 }];
olderReset();
release({ bars: bars(100, 900), more: true, approx: false, next: null });
for (let i = 0; i < 5; i++) await tick();
out.dropped = S.bars.length;
// go to a date: loads until the first bar is at or before it
range = { from: 100, to: 140 };
S.bars = [{ t: 9000, seq: 1 }];
olderReset();
answers.push(() => ({ bars: bars(5000, 8900), more: true, approx: false, next: null }));
answers.push(() => ({ bars: bars(1000, 4900), more: true, approx: false, next: null }));
let landed = null;
olderUntil(() => S.bars[0].t <= 3000, () => { landed = S.bars[0].t; }, () => { landed = 'failed'; });
for (let i = 0; i < 20; i++) await tick();
out.landed = landed;
// past the stored minutes: NinjaTrader is asked; the page waits, then asks again when told they're in ('older')
range = { from: 1, to: 30 };
S.bars = [{ t: 9000, seq: 1 }];
olderReset();
asked.length = 0;
answers.push(() => ({ bars: [], more: true, fetching: true, approx: false, next: null }));
answers.push(() => ({ bars: bars(8000, 8900), more: false, approx: false, next: null, why: 'limit' }));
olderCheck();
for (let i = 0; i < 5; i++) await tick();
out.fetch = [S.loadNote, S.older.fetching, S.older.more, asked.length];
olderAgain();
for (let i = 0; i < 5; i++) await tick();
out.fetched = [S.bars.length, S.older.fetching, S.older.more, S.loadNote, toasts.at(-1)];
console.log(JSON.stringify(out));
})();
"""


def test_the_page_puts_older_bars_in_front():
    from test_hunt_web_chart import needs_node, run_app   # noqa: F401
    import shutil
    if not shutil.which("node"):
        pytest.skip("needs node")
    out = run_app(["BAR_CAP", "OLDER_EDGE", "OLDER_NOTE", "olderReset", "olderOn", "olderCheck", "olderLoad", "olderAgain",
                   "olderPrepend", "barsRedraw", "olderUntil", "olderUntilStep"], PAGE_JS)
    assert out["times"] == [10, 20] + list(range(100, 1000, 100)) + [1000, 2000, 3000, 4000, 5000]
    assert out["seqs"] == list(range(-10, 6))   # numbered on down from the first bar's 1
    assert out["range"] == {"from": 13, "to": 51}   # the same bars in view
    # the next ask from the first bar, then from where the empty answer got to; the visible 38 bars x 3
    assert out["asked"] == [["1000", "500"], ["100", "500"], ["50", "500"]]
    assert not out["more"] and any("1-minute" in t for t in out["toasts"]) and any("all the stored" in t for t in out["toasts"])
    assert out["dropped"] == 1
    assert out["landed"] == 1000
    assert out["fetch"] == ["loading older bars from NinjaTrader…", True, True, 1]
    assert out["fetched"][:4] == [11, False, False, ""] and "NinjaTrader's history" in out["fetched"][4]
