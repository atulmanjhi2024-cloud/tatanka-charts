"""Round 15 of the bug hunt, pictures, the tape and big trades: Save picture and Post to Discord take the chart's top
layer (drawings, alert lines, the studies in front of the bars); a Discord caption up to the server's 1800 characters,
or none; big trades in a bar replay are the replay's; the Tape keeps the row being read once it holds its 400 rows;
bubbles and fill arrows at the bar they printed in (none in an RTH chart's night, the forming bar on tick, volume,
range and Renko charts); a live sweep's price on the tick, as /api/trades gives it; a bar replay's first session's
VWAP from its open; and the anchored VWAP on an RTH chart, where bars end at the RTH close. The app.js parts (and
studies.js, drawings.js) run in node with the rest stubbed."""
import asyncio
import json
from dataclasses import replace

from chartsrv import replay, sessions
from chartsrv.bars import parse_spec
from chartsrv.models import Tick
from chartsrv.store import Store
from test_hunt14_studies import BIG, ES, built, minutes, ms, studies_part
from test_hunt_web_chart import WEB, app_client, app_part, ct_ms, needs_node, run_app  # noqa: F401  (app_client: the fixture)
from test_review_fixes import build_market


# ---- r15-tape-1: Save picture and Post to Discord take the panes' top layer -------------------------------------------

SHOT_JS = r"""
const shots = [];
const canvas = () => ({ width: 0, height: 0, getContext: () => new Proxy({ measureText: (s) => ({ width: s.length * 6 }) },
  { get: (t, k) => (k in t ? t[k] : () => {}), set: (t, k, v) => { t[k] = v; return true; } }) });
const document = { createElement: canvas };
const chart = { takeScreenshot: (...a) => { shots.push(a); return { width: 800, height: 400 }; } };
const css = () => '#000000', $ = () => ({ textContent: 'ES 5m', complete: false }), zs = () => 'CT';
const ax = { full: { format: () => 'Nov 2, 10:00' } }, brand = { powered: true };
/*APP*/
const out = chartImage();
console.log(JSON.stringify({ shots, size: [out.width, out.height] }));
"""


@needs_node
def test_the_chart_picture_has_the_top_layer_and_no_crosshair():
    """The library's takeScreenshot(addTopLayer = false, includeCrosshair = false) copies each pane's main canvas only
    unless asked for the top one, where the drawings, alert lines, fills, big trades and every study in front of the
    bars paint: the picture has them, and still no crosshair."""
    out = run_app(["chartImage"], SHOT_JS)
    assert len(out["shots"]) == 1 and out["shots"][0][0] is True and not any(out["shots"][0][1:])
    assert out["size"] == [800, 426]   # the title line above


# ---- r15-tape-2: Post to Discord's caption: up to the server's 1800 characters, or none ------------------------------

NOTE = ("ES rejected the overnight high at the VAH, back inside value; watching 6880 for the 80% rule to the VAL this "
        "afternoon")
ASK_JS = r"""
const els = {}, posted = [];
const el = () => ({ hidden: true, textContent: '', value: '', type: '', placeholder: '', maxLength: -1, children: [],
  classList: { toggle() {} }, setAttribute() {}, replaceChildren() {}, append() {}, focus() {}, select() {}, blur() {},
  contains: () => false });
const $ = (s) => (els[s] ||= el());
const document = { activeElement: null, createElement: () => el() };
let shareChannels = [];
const chartImage = () => ({ toDataURL: () => 'data:image/png;base64,' });
const ax = { md: { format: () => 'Nov 2, 10:00' } }, zs = () => 'CT', toast = () => {}, fail = (e) => { throw e; };
const api = async (method, path, body) => { posted.push(body.caption); return {}; };
const settle = async () => { for (let k = 0; k < 10; k++) await new Promise((r) => setImmediate(r)); };
const answer = async (v) => { $('#ask-in').value = v; $('#ask-form').onsubmit({ preventDefault() {} }); await settle(); };
/*APP*/
(async () => {
  $('#legend').textContent = 'ES 5m';
  const out = {};
  const post = async (caption) => {
    postDiscord(undefined);
    await settle();
    const box = { maxLength: $('#ask-in').maxLength, open: !$('#ask-dlg').hidden };
    await answer(caption);
    return { ...box, closed: $('#ask-dlg').hidden };
  };
  out.note = await post(NOTE);
  out.none = await post('');
  ask({ title: 'Save template', names: ['Mine'] });   // a template's name: as it was
  await settle();
  const name = { maxLength: $('#ask-in').maxLength };
  await answer('');
  out.name = { ...name, closed: $('#ask-dlg').hidden };
  out.posted = posted;
  console.log(JSON.stringify(out));
})();
"""


@needs_node
def test_a_discord_caption_takes_the_servers_length_and_can_be_left_empty():
    """'Caption (optional)': a trade note longer than a template's 60-character name fits (the server takes 1800), and
    a cleared caption posts the picture alone. A template's name still stops at 40 and cannot be empty."""
    out = run_app(["ask", "postDiscord"], ASK_JS.replace("NOTE", json.dumps(NOTE)))
    assert len(NOTE) == 118
    assert out["note"] == {"maxLength": 1800, "open": True, "closed": True}
    assert out["none"] == {"maxLength": 1800, "open": True, "closed": True}
    assert out["posted"] == [NOTE, ""]
    assert out["name"] == {"maxLength": 40, "closed": False}


# ---- r15-tape-3: big trades in a bar replay: the replay's, up to its clock, asked again as it plays ------------------

BIG_FETCH_JS = r"""
const asked = [], M5 = 300_000, DAY = 86_400_000;
const NOW = Math.floor(Date.now() / M5) * M5, AT = NOW - 6 * DAY;   // a replay six days back
const bars = (end, n) => Array.from({ length: n }, (_, k) => ({ t: end - (n - k) * M5 }));
const S = { symbol: 'ES', bars: [], big: [], bigKey: '', bigFrom: 0, bigTo: 0, replay: null };
const chart = { timeScale: () => ({ getVisibleLogicalRange: () => ({ from: 0, to: S.bars.length - 1 }) }) };
const bigOn = () => true, bigMin = () => 25, toast = (m) => { throw new Error(m); }, bigPrim = { update() {} };
const api = async (method, path) => {
  const q = new URLSearchParams(path.split('?')[1]), start = +q.get('start'), end = +q.get('end');
  asked.push([start, end]);
  return { trades: [[end - 1000, 6900, 30, 1, 1], [start, 6890, 40, -1, 1]] };   // newest first
};
/*APP*/
(async () => {
  S.bars = bars(NOW, 100);                    // live: the last 100 bars
  await bigFetch();
  S.big.push([NOW + 5000, 6950, 50, 1, 1]);   // and a trade from the stream
  S.replay = { at: AT };                      // replay: the bars before its clock
  S.bars = bars(AT, 100);
  await bigFetch();
  const first = S.big.map((t) => t[0]);
  S.bars.push({ t: AT });                     // a step: the next bar, the clock at its end
  S.replay.at = AT + M5;
  await bigFetch();
  console.log(JSON.stringify({ NOW, AT, asked, first, after: S.big.map((t) => t[0]) }))
})();
"""


@needs_node
def test_big_trades_in_a_replay_are_asked_for_up_to_the_replay_clock():
    """Replay always shows its latest bars, which are not the live edge: the trades asked for end at the replay clock
    (not the present's last 3 days, all of them after it), and each step past what was asked asks again."""
    out = run_app(["bigFetch"], BIG_FETCH_JS)
    m5, now, at = 300_000, out["NOW"], out["AT"]
    assert out["asked"][0][0] == now - 100 * m5 and out["asked"][0][1] > now   # live: to the present
    assert out["asked"][1:] == [[at - 100 * m5, at], [at - 100 * m5, at + m5]]
    assert out["first"] == [at - 100 * m5, at - 1000]   # the replay's, not the stream's
    assert out["after"] == [at - 100 * m5, at + m5 - 1000]


# ---- r15-tape-4: the Tape keeps the row being read when it holds its 400 rows ---------------------------------------

TAPE_JS = r"""
const H = 20, VIEW = 400;   // a row's height and the list's, in px
const list = {
  rows: [], top: 0,
  get childElementCount() { return this.rows.length; },
  get scrollHeight() { return Math.max(VIEW, this.rows.length * H); },
  get scrollTop() { return this.top; },
  set scrollTop(v) { this.top = Math.max(0, Math.min(v, this.scrollHeight - VIEW)); },   // as a browser keeps it
  prepend(...r) { this.rows.unshift(...r); },
  get lastElementChild() { return { remove: () => { this.rows.pop(); this.scrollTop = this.top; } }; },
};
const $ = () => list, tapeRow = (t) => ({ t: t[0] });
const topRow = () => list.rows[Math.floor(list.scrollTop / H)].t;
let n = 0;
const trades = (k) => Array.from({ length: k }, () => [++n, 6900, 1, 1, 1]);   // oldest first, as they come
/*APP*/
const out = {};
for (const count of [200, 400]) {
  list.rows = [];
  list.top = 0;
  tapeAdd(trades(count));
  list.scrollTop = 300;   // scrolled down to read
  const before = topRow();
  tapeAdd(trades(3));     // three new trades come in above
  out[count] = { before, after: topRow(), rows: list.childElementCount };
}
console.log(JSON.stringify(out));
"""


@needs_node
def test_new_trades_do_not_move_the_tape_being_read_once_it_holds_400_rows():
    """The view is kept by its distance to the list's end: put back before the oldest rows go, as taking rows out
    below the view does not move it (after, the end has moved up by as many rows as came in)."""
    out = run_app(["tapeAdd"], TAPE_JS)
    assert out["200"]["after"] == out["200"]["before"] and out["200"]["rows"] == 203
    assert out["400"]["after"] == out["400"]["before"] and out["400"]["rows"] == 400


# ---- r15-tape-5 and -7: bubbles and fill arrows at the bar they printed in ------------------------------------------

PLACE_JS = r"""
const vm = require('vm'), fs = require('fs');
const box = { localStorage: { getItem: () => null, setItem() {} }, window: { addEventListener() {} }, document: { addEventListener() {} } };
vm.runInNewContext(['press.js', 'studies.js', 'drawings.js'].map((f) => fs.readFileSync(WEB + '/' + f, 'utf8')).join(';') +
  ';this.create=createDrawings;this.barIndexTo=barIndexTo;', box);   // as index.html loads them
const barIndexTo = box.barIndexTo;
const S = { kind: 'time', spec: '5m', rth: true, bars: [], big: [], fills: [], fillTrades: [], st: { big: true, fills: true }, replay: null };
const inst = () => ({ rth: ['08:30', '15:00'] }), TZ_EXCHANGE = 'America/Chicago';
const q = () => ({ addEventListener() {}, querySelector: q, querySelectorAll: () => [], classList: { toggle() {} }, style: {} });
const chart = { timeScale: () => ({ getVisibleLogicalRange: () => null, options: () => ({ barSpacing: 10 }) }),
  priceScale: () => ({ width: () => 0 }), options: () => ({}), applyOptions() {} };
const drawings = box.create({ chart, series: { attachPrimitive() {} }, el: { addEventListener() {}, style: {} }, bars: () => S.bars,
  periodMs: () => (isTime() ? specSeconds(S.spec) * 1000 : 0), tick: () => 0.25, fmt: String, symbol: () => 'ES',
  api: async () => ({ drawings: [] }), toast() {}, alerts: () => [], ui: { bar: q(), edit: q(), paste: q() } });
const bigOn = () => true, bigMin = () => 25, TK_MULTI = false, C = { up: 'u', down: 'd' }, css = () => 'x', money = String;
const logicalX = (ts, l) => l * 10, candles = { priceToCoordinate: (p) => p };
const marks = [];   // what is drawn: a bubble's circle, a fill arrow's tip (6 px left of its bar: barSpacing 10)
const ctx = new Proxy({ measureText: (s) => ({ width: s.length * 6 }) }, {
  get: (t, k) => (k in t ? t[k] : k === 'arc' ? (x, y) => marks.push(['big', x / 10, y])
    : k === 'moveTo' ? (x, y) => marks.push(['fill', Math.round((x + 6) / 10), y]) : () => {}),
  set: (t, k, v) => { t[k] = v; return true; } });
/*APP*/
const ctWall = wallClock(TZ_EXCHANGE);
const ct = (s) => Date.parse(s + '-05:00'), M = 60_000;
const draw = () => { marks.length = 0; drawBig(ctx); drawFills(ctx); return marks.slice(); };
const out = {};
// an RTH 5m chart: Monday's and Tuesday's regular hours, 08:30 to 15:00
const day = (d) => Array.from({ length: 78 }, (_, k) => ({ t: ct(`${d}T08:30:00`) + k * 5 * M }));
S.bars = [...day('2026-10-26'), ...day('2026-10-27')];
S.big = [[ct('2026-10-26T10:02:00'), 6900, 30, 1], [ct('2026-10-26T15:30:00'), 6950, 30, 1],   // Monday 10:00, after the close
  [ct('2026-10-27T02:00:00'), 6986, 30, -1], [ct('2026-10-27T08:31:00'), 6901, 30, 1],          // the night, Tuesday's first bar
  [ct('2026-10-27T15:10:00'), 6960, 30, 1]];                                                     // after Tuesday's close
S.fills = [{ ts: ct('2026-10-27T03:00:00'), price: 6980, side: 1, qty: 1 }, { ts: ct('2026-10-27T09:00:30'), price: 6905, side: -1, qty: 2 }];
out.rth = draw();
// RTH 1h: Monday's 14:30 bar ends at the 15:00 close, not at 15:30
const hours = (d) => Array.from({ length: 7 }, (_, k) => ({ t: ct(`${d}T08:30:00`) + k * 60 * M }));
Object.assign(S, { spec: '1h', bars: [...hours('2026-10-26'), ...hours('2026-10-27')], fills: [] });
S.big = [[ct('2026-10-26T14:50:00'), 6900, 30, 1], [ct('2026-10-26T15:20:00'), 6950, 30, 1]];
out.rth1h = draw();
// an RTH 2000-tick chart: a bar a minute to Monday's close; the night's and the next pre-market's are in none
Object.assign(S, { kind: 'tick', spec: '2000t', bars: Array.from({ length: 120 }, (_, k) => ({ t: ct('2026-10-26T13:00:00') + k * M })) });
S.big = [[ct('2026-10-26T14:59:30'), 6900, 30, 1], [ct('2026-10-26T15:30:00'), 6950, 30, 1], [ct('2026-10-27T02:00:00'), 6986, 30, -1]];
out.rthTicks = draw();
// a 2000-tick chart: a bar a minute; the forming one opened at T and has run 2.5 times that
const T0 = ct('2026-10-27T09:00:00'), T = T0 + 59 * M;
Object.assign(S, { rth: false, kind: 'tick', spec: '2000t', bars: Array.from({ length: 60 }, (_, k) => ({ t: T0 + k * M })) });
S.big = [[T0 + 10 * M + 20_000, 6900, 30, 1], [T + 30_000, 6910, 30, 1], [T + 150_000, 6911, 30, -1]];
S.fills = [{ ts: T + 200_000, price: 6912, side: 1, qty: 1 }];
out.ticks = draw();
console.log(JSON.stringify(out));
"""


@needs_node
def test_big_trades_and_fills_are_drawn_at_the_bar_they_printed_in():
    """An RTH chart has no bar for the night, the pre-market or after the close: those trades and fills are not drawn
    (not on the next day's first bar or the day's last, at prices they never traded, nor right of the last bar). On a
    tick chart the forming bar holds every trade since it opened, however much longer than the average bar it has run."""
    run = ["isTime", "specSeconds", "daysSpec", "barEnd", "wallClock", "tradeBar", "drawBig", "drawFills"]
    out = run_app(run, PLACE_JS.replace("WEB +", f"{json.dumps(str(WEB))} +"))
    assert out["rth"] == [["big", 18, 6900], ["big", 78, 6901], ["fill", 84, 6905]]   # Mon 10:00, Tue 08:30 and 09:00
    assert out["rth1h"] == [["big", 6, 6900]] and out["rthTicks"] == [["big", 119, 6900]]
    assert out["ticks"] == [["big", 10, 6900], ["big", 59, 6910], ["big", 59, 6911], ["fill", 59, 6912]]


# ---- r15-tape-6: a live sweep's price on the tick, as /api/trades (and a reload of the Tape) gives it ----------------

def test_a_live_sweep_is_on_the_tick_as_the_stored_trades_give_it(tmp_path, monkeypatch, app_client):
    t = ct_ms(2026, 10, 30, 9, 0)
    ticks = [Tick("ES", t, 6900.0, 6, 1), Tick("ES", t, 6900.25, 10, 1),        # a buyer sweeping two prices
             Tick("ES", t + 7, 6899.75, 1, -1), Tick("ES", t + 7, 6900.0, 24, -1)]
    m, _ = build_market(tmp_path, monkeypatch)
    a = m.connect()
    m.set_tape(a, "ES", 1)
    for tk in ticks:
        m.on_tick(replace(tk), live=False)

    async def one_flush():
        task = asyncio.create_task(m._client_loop())
        await asyncio.sleep(0.2)
        task.cancel()
    asyncio.run(one_flush())
    live = []
    while not a.outbox.empty():
        msg = a.outbox.get_nowait()
        if msg["type"] == "trades":
            live += msg["trades"]
    with app_client(ticks) as c:
        stored = c.get(f"/api/trades?symbol=ES&start={t}&end={t + 10}").json()["trades"]
    assert live == stored == [[t, 6900.25, 16, 1, 2], [t + 7, 6900.0, 25, -1, 2]]


# ---- r15-tape-8 (r15-review-3): a bar replay's first session's VWAP from its open -------------------------------------

def test_a_bar_replays_first_session_vwap_counts_from_its_open(tmp_path):
    """A minute chart's replay starts its look-back inside a session (1650 minutes back on 1m, 8250 on 5m): that
    session's VWAP, bands and RTH VWAP are the chart history's, from its 17:00 open, not from the window's start."""
    store = Store(tmp_path / "t.db")
    now, at = ms(2026, 10, 30, 14), ms(2026, 10, 30, 13)   # Friday; sessions stored from Sunday the 18th
    store.insert_bars_if_missing("ES", minutes(ms(2026, 10, 18, 17), now))
    for spec, rth_only in (("1m", False), ("5m", False), ("1m", True)):
        whole = {b.t: b.st for b in built(store.path, spec, 0, now, now, 10 ** 6, rth_only=rth_only).bars}
        bars, _, _ = replay.build(store.path, "ES", ES, parse_spec(spec), at, now, BIG, rth_only=rth_only)
        assert len(bars) > 500 and bars[0].t > sessions.session_start(bars[0].t) + 3_600_000, spec   # mid-session
        assert [b.st for b in bars] == [whole[b.t] for b in bars], (spec, rth_only)


# ---- r15-review-2: the anchored VWAP on an RTH chart, whose bars end at the RTH close -------------------------------

AVWAP_JS = """
/*STUDIES*/
const ct = (s) => Date.parse(s + '-05:00'), H = 3600e3;
const sessionDay = (ms) => new Intl.DateTimeFormat('en-CA', { timeZone: 'America/Chicago', year: 'numeric', month: '2-digit', day: '2-digit' })
  .format(ms + 7 * H);   // ta.js's, on CME's clock: the 17:00 open is the next date's
const store = {}, local = { get: (k, d) => store[k] ?? d, set: (k, v) => { store[k] = v; } };
let drawn = null;
const avwap = { setData: (d) => { drawn = d; }, applyOptions() {} }, timeOf = (b) => b.t;
const ax = { full: { format: () => '' } }, zs = () => '', $ = () => ({});
const S = { symbol: 'ES', kind: 'time', rth: true, holidays: {}, instruments: { ES: { rth: ['08:30', '15:00'], calendar: 'equity' } } };
const inst = () => S.instruments[S.symbol], TZ_EXCHANGE = 'America/Chicago';
/*APP*/
const { toMs: ctInputToMs } = inputClock(TZ_EXCHANGE);
const bar = (s) => ({ t: ct(s), o: 6800, h: 6810, l: 6790, c: 6800, v: 1000 });
const show = (t) => new Date(t - 5 * H).toISOString().slice(5, 16);
const from = (spec, times, a, rth = true) => {
  Object.assign(S, { spec, rth, bars: times.map(bar) });
  store['anchor:ES'] = String(ct(a));
  drawAvwap();
  return drawn.length ? show(drawn[0].time) : null;
};
const d1 = ['2026-10-26T08:30:00', '2026-10-27T08:30:00', '2026-10-28T08:30:00'];
const h1 = [8, 9, 10, 11, 12, 13, 14].map((h) => `2026-10-27T${String(h).padStart(2, '0')}:30:00`).concat(['2026-10-28T08:30:00']);
const h4 = ['2026-10-27T08:30:00', '2026-10-27T12:30:00', '2026-10-28T08:30:00', '2026-10-28T12:30:00'];
console.log(JSON.stringify({
  d1Night: from('1D', d1, '2026-10-28T02:00:00'), d1Open: from('1D', d1, '2026-10-27T17:00:00'),
  d1Day: from('1D', d1, '2026-10-28T10:00:00'), d1Close: from('1D', d1, '2026-10-27T14:59:00'),
  h1After: from('1h', h1, '2026-10-27T15:15:00'), h1In: from('1h', h1, '2026-10-27T14:45:00'),
  h1Night: from('1h', h1, '2026-10-28T03:00:00'),
  h4After: from('4h', h4, '2026-10-27T15:30:00'), h4In: from('4h', h4, '2026-10-27T13:00:00'),
  d2Night: from('2D', ['2026-10-22T08:30:00', '2026-10-26T08:30:00'], '2026-10-23T20:00:00'),   // Thursday-Friday, Monday-Tuesday
  ethDay: from('1D', ['2026-10-26T17:00:00', '2026-10-27T17:00:00'], '2026-10-28T02:00:00', false),
}));
"""


@needs_node
def test_anchored_vwap_on_an_rth_chart_starts_at_the_next_bar_for_an_anchor_after_the_rth_close():
    """An RTH day bar stamped 08:30 ends at 15:00, not 24 hours on; an RTH 1h chart's 14:30 bar and 4h chart's 12:30 bar
    at 15:00 too: an anchor from the close to the next open (the night, the 17:00 open) starts at the next day's bar.
    One inside a bar's hours still starts at that bar; an ETH chart's day bar holds its whole session as before."""
    js = AVWAP_JS.replace("/*STUDIES*/", "\n".join(studies_part(n) for n in ("barIndexFrom", "barIndexTo", "anchoredVwap")))
    out = run_app(["isTime", "specSeconds", "daysSpec", "inputClock", "dayCloseCt", "dayHalts", "barCloseMs",
                   "anchorKey", "anchorMs", "drawAvwap"], js)
    assert out == {
        "d1Night": "10-28T08:30", "d1Open": "10-28T08:30", "d1Day": "10-28T08:30", "d1Close": "10-27T08:30",
        "h1After": "10-28T08:30", "h1In": "10-27T14:30", "h1Night": "10-28T08:30",
        "h4After": "10-28T08:30", "h4In": "10-27T12:30",
        "d2Night": "10-26T08:30", "ethDay": "10-27T17:00",
    }
