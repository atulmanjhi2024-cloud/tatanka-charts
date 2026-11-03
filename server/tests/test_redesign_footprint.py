"""Footprint on tick, volume, range and Renko bars: a request's first bar can open in a millisecond whose first trades
went to the bars before it (one print or one sweep finishing several). Each such bar says how many contracts of its
opening millisecond the bars before it took (`pre`), and the chart sends that with its first bar, so the forming
bar's refresh, the refetch after a close and a view's left edge get each bar's own trades."""
import json
import random
from datetime import datetime, timedelta

from chartsrv import sessions
from chartsrv.bars import make_series, parse_spec
from chartsrv.models import Tick
from test_hunt_web_chart import FP_JS, app_client, ct_ms, needs_node, run_app  # noqa: F401  (app_client: the app, as a fixture)


def _morning() -> int:
    """09:00 CT on a weekday a day or more back: the chart reads no footprint for a span over 8 days to now."""
    day = datetime.now(sessions.CT).date() - timedelta(days=1)
    while day.weekday() >= 5:
        day -= timedelta(days=1)
    return ct_ms(day.year, day.month, day.day, 9, 0)


T0 = _morning()
# Each bar type with trades that make its bars open where the bar before ended: big prints overfilling volume bars,
# sweeps of one millisecond past a tick bar's count, jumps of several ticks breaking out of range and Renko bars.
CASES = {"1000v": (11, (1, 2, 5, 10, 20, 60, 1500), 2), "10t": (12, (1, 2, 3), 2), "6r": (13, (1, 2, 10), 3),
         "4k": (14, (1, 3), 3)}


def stream(seed: int, sizes: tuple, moves: int) -> list[Tick]:
    """Trades in bursts, each burst some fills in one millisecond, the price walking up to `moves` ticks a fill."""
    rnd = random.Random(seed)
    t, p, out = T0, 24000, []
    for _ in range(40):
        t += rnd.choice((1, 7, 400))
        for _ in range(rnd.choice((1, 1, 3, 8, 25))):
            p += rnd.randint(-moves, moves)
            out.append(Tick("ES", t, p * 0.25, rnd.choice(sizes), rnd.choice((1, -1))))
    return out


def chart(spec: str):
    """The trades of `spec`'s case and its bars as the chart gets them."""
    ticks = stream(*CASES[spec])
    s = make_series(parse_spec(spec), 0.25, 2, 5000, ("08:30", "15:00"))
    for tk in ticks:
        s.on_tick(tk)
    return ticks, [b.to_dict() for b in s.bars]


def taken_before(ticks: list[Tick], bars: list[dict]) -> list[int]:
    """What each bar's `pre` should be. Bars take the trades in order, so the contracts before bar i are the bars'
    volumes before it; those traded in its opening millisecond are that, less the ones traded before that millisecond."""
    out, before = [], 0
    for b in bars:
        out.append(before - sum(tk.size for tk in ticks if tk.ts < b["t"]))
        before += b["v"]
    return out


def totals(rows) -> tuple[int, int]:
    return sum(bid + ask for _, bid, ask in rows), sum(ask - bid for _, bid, ask in rows)


def test_trade_built_bars_say_what_their_opening_millisecond_gave_the_bars_before():
    for spec in CASES:
        ticks, bars = chart(spec)
        want = taken_before(ticks, bars)
        assert sum(p > 0 for p in want) >= 3, spec   # the case: bars opening where the one before ended
        assert [b.get("pre", 0) for b in bars] == want, spec
        assert all("pre" in b for b, p in zip(bars, want) if p) and not any("pre" in b for b, p in zip(bars, want) if not p)


def test_every_footprint_request_gets_each_bars_own_trades(app_client):
    """Requests starting at each bar, as the chart sends them (its first bar's `pre`, the bar after the last one along
    and not kept), whether the first bar opens where the one before ended or not."""
    for spec in CASES:
        ticks, bars = chart(spec)
        n = len(bars)
        with app_client(ticks) as c:
            for a in range(n):
                for b in sorted({a, min(a + 2, n - 1), n - 1}):
                    k = b + 1 if b + 1 < n else b
                    win = bars[a:k + 1]
                    r = c.post("/api/footprint", json={
                        "symbol": "ES", "starts": [x["t"] for x in win], "vols": [x["v"] for x in win],
                        "pre": win[0].get("pre", 0), "end": bars[k]["t"] + 1 if k > b else ticks[-1].ts + 1})
                    assert r.status_code == 200, (spec, a, b, r.text)
                    got = r.json()["bars"]
                    assert [totals(f["rows"]) for f in got[:b - a + 1]] == [(x["v"], x["d"]) for x in win[:b - a + 1]], (spec, a, b)


def test_the_forming_bar_alone_gets_its_own_trades(app_client):
    """The hunt's case: 300 lots, then a 400-lot print finishing that bar (200) and opening the next (200), then 10.
    The forming bar's once-a-second refresh sends it alone: it drew 410 contracts against its 210."""
    t1 = T0 + 1000
    ticks = [Tick("ES", T0, 6000.0, 300, 1), Tick("ES", t1, 6000.25, 400, 1), Tick("ES", t1, 6000.5, 10, -1)]
    s = make_series(parse_spec("500v"), 0.25, 2)
    for tk in ticks:
        s.on_tick(tk)
    bars = [b.to_dict() for b in s.bars]
    assert [(b["t"], b["v"], b.get("pre", 0)) for b in bars] == [(T0, 500, 0), (t1, 210, 200)]
    with app_client(ticks) as c:
        r = c.post("/api/footprint", json={"symbol": "ES", "starts": [t1], "vols": [210], "pre": bars[1]["pre"], "end": t1 + 1})
        assert r.status_code == 200, r.text
        assert r.json()["bars"] == [{"t": t1, "rows": [[24001, 0, 200], [24002, 10, 0]]}]


@needs_node
def test_the_chart_sends_its_first_bars_pre(app_client):
    """The chart's own requests (fpFetch run in node) to the real endpoint: the forming bar's refresh, the refetch after
    a close and a view whose left edge is a bar opening where the one before ended."""
    ticks, bars = chart("1000v")
    n = len(bars)
    pre = taken_before(ticks, bars)
    a = next(i for i in range(n - 6) if pre[i])   # a view starting at such a bar
    every = [b["seq"] for b in bars]
    views = [(True, n - 3, every), (False, n - 3, every[:-3]), (False, a + 1, [])]   # (only the forming bar, from, cached)
    out = run_app(["fpBusy", "fpFetch"], FP_JS + f"""
const B = {json.dumps(bars)}, out = [];
(async () => {{
  S.spec = '1000v'; S.rth = false;
  for (const [onlyLast, from, cached] of {json.dumps(views)}) {{
    S.bars = B.slice(); S.fp = new Map(cached.map((seq) => [seq, {{}}])); range = {{ from, to: from + 4 }}; bodies.length = 0;
    await fpFetch(onlyLast);
    out.push({{ body: bodies[0], kept: [...S.fp].filter(([seq, f]) => f.k != null).map(([seq, f]) => [seq, f.k]) }});
  }}
  console.log(JSON.stringify(out));
}})();""")
    assert [o["body"]["starts"][0] for o in out] == [bars[-1]["t"], bars[-3]["t"], bars[a]["t"]]
    assert [o["body"].get("pre", 0) for o in out] == [pre[-1], pre[-3], pre[a]]
    by_seq = {b["seq"]: b for b in bars}
    with app_client(ticks) as c:
        for o in out:
            body = {**o["body"], "end": min(o["body"]["end"], ticks[-1].ts + 1)}   # the forming bar's: an hour ahead
            r = c.post("/api/footprint", json=body)
            assert r.status_code == 200, (body, r.text)
            got = r.json()["bars"]
            assert o["kept"]
            for seq, k in o["kept"]:
                assert totals(got[k]["rows"]) == (by_seq[seq]["v"], by_seq[seq]["d"]), (o, seq)


def test_older_requests_and_time_bars_as_before(app_client):
    """An open page from before sends no `pre`: answered as before (as 0). Time bars never carry one, so their
    messages are as they were; a `pre` below 0 is refused."""
    s = make_series(parse_spec("1m"), 0.25, 2)
    ticks = [Tick("ES", T0 + k * 20_000, 6000.0 + k * 0.25, 2, 1) for k in range(9)]
    for tk in ticks:
        s.on_tick(tk)
    bars = [b.to_dict() for b in s.bars]
    assert len(bars) == 3 and not any("pre" in b for b in bars)
    body = {"symbol": "ES", "starts": [b["t"] for b in bars], "end": ticks[-1].ts + 1}
    with app_client(ticks) as c:
        old = c.post("/api/footprint", json=body)
        assert old.status_code == 200 and old.json() == c.post("/api/footprint", json={**body, "pre": 0}).json()
        assert [totals(f["rows"]) for f in old.json()["bars"]] == [(b["v"], b["d"]) for b in bars]
        assert c.post("/api/footprint", json={**body, "pre": -1}).status_code == 422
