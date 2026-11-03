"""Bug hunt round 5, the footprint (web/app.js fpFetch): a forming bar that closes while its request is out, and the
last bar shown in replay. The chart's own requests (fpFetch, applyBars and replay's load and step run in node) go to
the real /api/footprint, and every bar the chart keeps a footprint for must get its own trades."""
import json
import os

import pytest

from chartsrv.bars import make_series, parse_spec
from chartsrv.models import Tick
from chartsrv.store import Store
from test_hunt_web_chart import app_client, needs_node, run_app  # noqa: F401  (app_client: the app, as a fixture)
from test_redesign_footprint import T0, totals

# What applyBars calls besides the footprint, stubbed; each answer says which request (q) and which place in it (k).
BARS_JS = """
const S = { st: { fp: 'bidask', tpo: false, gap: false, ifthen: false }, ext: null, replay: null, symbol: 'ES', spec: SPEC,
  rth: false, kind: /[tvrk]$/.test(SPEC) ? 'volume' : 'time', fp: new Map(), bars: [], cs: {}, hovering: false };
const chart = { timeScale: () => ({ getVisibleLogicalRange: () => ({ from: 0, to: S.bars.length + 3 }), options: () => ({ barSpacing: 50 }) }) };
const FP_MIN_SPACING = 40, fpPrim = { update() {} }, isTime = () => S.kind === 'time';
const toPoints = () => [{}, {}], candles = { update() {} }, vol = { update() {} }, altSeries = null, BAR_CAP = 20000, anchorMs = () => null;
const [updateStudyBar, indsUpdate, dsmaTick, hemaTick, cmpTail, legend, header] = Array(7).fill(() => {});
const bodies = [];
const answer = (body) => { const q = bodies.push(body) - 1; return { bars: body.starts.map((t, k) => ({ t, q, k, rows: [] })) }; };
"""


def bars_of(spec: str, ticks: list[Tick]) -> list[dict]:
    s = make_series(parse_spec(spec), 0.25, 2)
    for tk in ticks:
        s.on_tick(tk)
    return [b.to_dict() for b in s.bars]


def check_kept(c, bodies: list[dict], kept: list, bars: list[dict]):
    """Every footprint the chart keeps, its request asked again of the real endpoint: the bar's own volume and delta."""
    by_seq = {b["seq"]: b for b in bars}
    for seq, q, k in kept:
        r = c.post("/api/footprint", json=bodies[q])
        assert r.status_code == 200, (bodies[q], r.text)
        assert totals(r.json()["bars"][k]["rows"]) == (by_seq[seq]["v"], by_seq[seq]["d"]), (seq, bodies[q])


# ---- fp-1: the forming bar closes while its footprint request is out ------------------------------------------------

def T(ms, px, size, side):
    return Tick("ES", T0 + ms, px, size, side)


RACE = {   # (the trades before, the ones closing the forming bar)
    "500v": ([T(1000 * k, 6000 + 0.25 * (k % 4), 100, 1 if k % 2 else -1) for k in range(8)],   # 500, then 300 forming
             [T(9000, 6001.0, 250, 1), T(9100, 6001.25, 40, 1)]),                             # 500 closed, 90 forming
    "1m": ([T(15000 * k, 6000 + 0.25 * (k % 4), 10, 1 if k % 2 else -1) for k in range(7)],     # 09:00 40, 09:01 30
           [T(115000, 6000.5, 15, 1), T(120050, 6001.0, 70, 1), T(120300, 6001.25, 5, -1)]),   # 09:01 45, 09:02 75
}


@needs_node
@pytest.mark.parametrize("spec", RACE)
def test_a_bar_closing_while_its_footprint_is_asked_for_is_asked_for_again(app_client, spec):
    """The once-a-second refresh of the forming bar goes out; the message closing that bar is handled before the answer
    comes, which counted the trades up to when the server answered: the next bar's first ones (or not its own last).
    It was kept after applyBars had dropped the bar, so the refetch never asked for it again."""
    early, late = RACE[spec]
    before, after = bars_of(spec, early), bars_of(spec, early + late)
    closing = [b for b in after if b["seq"] >= before[-1]["seq"]]
    out = run_app(["fpBusy", "fpSchedule", "fpFetch", "applyBars"], f"const SPEC = '{spec}';" + BARS_JS + f"""
let hold = null;   // run while the next request is out
const api = async (method, path, body) => {{ const h = hold; hold = null; if (h) {{ await null; h(); }} return answer(body); }};
/*APP*/
(async () => {{
  S.bars = {json.dumps(before)};
  await fpFetch();                                             // the history's fetch: every bar in view
  hold = () => applyBars({{ bars: {json.dumps(closing)} }});   // the message closing the forming bar comes first
  await fpFetch(true);                                         // the forming bar's refresh
  await new Promise((r) => setTimeout(r, 400));                // the close's refetch
  console.log(JSON.stringify({{ bodies, kept: [...S.fp].map(([seq, f]) => [seq, f.q, f.k]) }}));
}})();""")
    bodies, kept = out["bodies"], {seq: (q, k) for seq, q, k in out["kept"]}
    shut, nxt = after[len(before) - 1], after[len(before)]   # the bar that closed, and the one after it
    assert bodies[1]["starts"] == [shut["t"]] and len(kept) == len(after)   # the race: the then forming bar's refresh
    with app_client(early + late) as c:   # the server has every trade: the refresh's answer counted the next bar's
        check_kept(c, bodies, out["kept"], after)
    q, k = kept[shut["seq"]]
    assert q > 1 and bodies[q]["starts"][k + 1] == nxt["t"]   # asked again, with the bar after it


# ---- fp-2: replay, the last bar shown ends where it ends -------------------------------------------------------------

REPLAY_JS = BARS_JS + """
const REPLAY = __REPLAY__, fpSchedule = () => {}, replayUi = () => {}, replayMore = () => {}, toast = () => {};
const fail = (e) => { throw e; };
const api = async (method, path, body) => (method === 'GET' ? REPLAY[path] : answer(body));
function applyHistory(m) { S.kind = m.kind; S.bars = m.bars.slice(); S.fp.clear(); }   // its footprint lines
/*APP*/
const out = [];
const start = (at) => { S.replay = { at, data: [], idx: 0, kind: 'time', period: 0, nextAt: null, req: 0 }; return replayLoad(); };
const stage = async (label) => {   // the fetch after a load or a step: every bar in view
  await fpFetch();
  out.push({ label, shown: S.bars.length, kept: [...S.fp].map(([seq, f]) => [seq, f.q, f.k]) });
};
"""
REPLAY_PARTS = ["specSeconds", "daysSpec", "barEnd", "shownCount", "replayLoad", "replayStep", "fpBusy", "fpFetch", "applyBars"]


def replay_run(c, clocks: list[tuple[str, int]], driver: str) -> tuple[dict, dict]:
    """The server's /api/replay answers for these (spec, clock), and the driver run with them in node: its requests and
    stages, and the replay's bars by spec."""
    paths = [f"/api/replay?symbol=ES&spec={spec}&at={at}&rth=false" for spec, at in clocks]
    answers = {p: c.get(p).json() for p in paths}
    out = run_app(REPLAY_PARTS, f"const SPEC = '{clocks[0][0]}';" + REPLAY_JS.replace("__REPLAY__", json.dumps(answers))
                  + driver + "\nconsole.log(JSON.stringify({ bodies, out }));\n})();")
    return out, {spec: answers[p]["bars"] for (spec, _), p in zip(clocks, paths)}


SHARED = [(0, 6000.0, 300, 1), (1000, 6000.25, 900, 1), (1000, 6000.5, 50, -1), (2000, 6000.0, 700, -1),
          (2001, 6000.25, 10, 1), (3000, 6000.5, 600, 1), (4000, 6000.75, 200, -1), (5000, 6001.0, 400, 1)]


def replay_taps(app_client, taps: list[int], steps: int):
    """500v bars opening in the millisecond the one before ends in, two of them in one; replay from a tap on each of
    `taps`, the last one stepped on `steps` times: every bar shown has its own trades. What the chart sent and kept."""
    ticks = [T(*x) for x in SHARED]
    live = bars_of("500v", ticks)
    assert [(b["t"] - T0, b["v"]) for b in live] == [(0, 500), (1000, 500), (1000, 500), (2000, 500), (3000, 500),
                                                    (3000, 500), (5000, 160)]
    clocks = [(i, live[i + 1]["t"]) for i in taps]   # the click handler's clock: barEnd, the next bar's open
    with app_client(ticks) as c:
        out, data = replay_run(c, [("500v", at) for _, at in clocks], f"""
(async () => {{
  for (const [i, at] of {json.dumps(clocks)}) {{
    await start(at); await stage('tap ' + i);
    if (i === {taps[-1]}) for (let k = 1; k <= {steps}; k++) {{ replayStep(); await stage('step ' + k); }}
  }}""")
        for o in out["out"]:
            assert len(o["kept"]) == o["shown"], o   # every bar shown has its footprint
            check_kept(c, out["bodies"], o["kept"], data["500v"])
    return out


@needs_node
def test_replay_on_trade_built_bars_sends_the_hidden_next_bar(app_client):
    """A tap's clock is the next bar's open, and the last bar shown asked up to it: refused when that is its own open
    too (two bars in one millisecond: the whole view drew candles), or short of its share of that millisecond's print.
    Stepped on to the session's last bar, and its end."""
    out = replay_taps(app_client, [0, 1, 2], 4)
    assert [o["shown"] for o in out["out"]] == [2, 2, 3, 4, 5, 6, 7]


@needs_node
def test_replay_past_the_bars_loaded_sends_the_next_stretchs_first_bar(app_client, monkeypatch):
    """The last bar loaded (the server stops at a bar opening in a later millisecond, next_at) can end in that
    millisecond: the next stretch's first bar goes along, at next_at."""
    from chartsrv import replay
    monkeypatch.setattr(replay, "AFTER", 2)
    out = replay_taps(app_client, [0], 1)
    assert [o["shown"] for o in out["out"]] == [2, 3]
    assert out["bodies"][-1]["starts"][-1] == T0 + 2000 and out["bodies"][-1]["vols"][-1] == 0


@needs_node
def test_replay_on_time_bars_ends_the_last_bar_shown_at_its_end(app_client):
    """A clock inside the next bar (a time picked and Go, or a timeframe change keeping it: 1m at 09:17 -> 5m): the
    last bar shown, 09:10, asked up to 09:17 and took the hidden 09:15 bar's trades to then."""
    ticks = [Tick("ES", T0 + s * 1000, 6000 + 0.25 * (s // 60), 5 + s // 60 % 3, 1 if s // 20 % 2 else -1)
             for s in range(10, 25 * 60, 20)]   # 09:00:10 to 09:24:50, a tick higher each minute
    at = T0 + 17 * 60_000
    s1 = make_series(parse_spec("1m"), 0.25, 2)
    for tk in ticks:
        s1.on_tick(tk)
    client = app_client(ticks)
    Store(os.environ["CHART_DB"]).write([], [("ES", b) for b in s1.bars])   # replay builds time bars from 1m bars
    with client as c:
        out, data = replay_run(c, [("1m", at), ("5m", at)], """
(async () => {
  await start(REPLAY_AT); await stage('1m, a tap on 09:16');
  S.spec = '5m'; await replayLoad(); await stage('5m, the clock kept');   // a timeframe change: subscribe() -> replayLoad()
  replayStep(); await stage('5m, a step');
""".replace("REPLAY_AT", str(at)))
        assert [o["shown"] for o in out["out"]] == [17, 3, 4]
        for o, spec in zip(out["out"], ["1m", "5m", "5m"]):
            assert len(o["kept"]) == o["shown"], o
            check_kept(c, out["bodies"], o["kept"], data[spec])
