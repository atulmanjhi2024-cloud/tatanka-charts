"""The journal with NinjaTrader's own positions (add-on 13's P lines) as known points: a fill the app never got (a
position opened before the add-on was installed, a fill made while NinjaTrader was off) no longer skews the trades
after it, however long the position was held and whatever trading came after. Add-on 12 and older send no P lines: the
journal falls back to the stored fills' 7-day walk, as before."""
import json
import shutil
import subprocess

import pytest

from chartsrv.feeds.ninjatrader import NinjaTraderFeed
from chartsrv.market import DAY_MS

T = 1_760_000_000_000
H = 3_600_000


def E(ts, side, qty, price, i, acct="Sim101", contract="ES 12-26"):
    """An add-on's E line: one of your fills."""
    return f"E|{acct}|ES|{contract}|{ts}|{price}|{qty}|{side}|50|2.04|x{i}|o{i}|Entry"


def P(ts, qty, avg=0.0, acct="Sim101", contract="ES 12-26"):
    """Add-on 13's P line: the position NinjaTrader has (+ long, - short, 0 flat)."""
    return f"P|{acct}|ES|{contract}|{ts}|{qty}|{avg}|50"


def FLAT(ts, acct="Sim101"):
    """Add-on 13's P line after a connect's open positions: the account's other contracts are flat."""
    return f"P|{acct}|{ts}"


def _app(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from chartsrv.app import app
    monkeypatch.setenv("CHART_DB", str(tmp_path / "chart.db"))
    monkeypatch.setenv("CHART_LEVELS_DIR", str(tmp_path))
    monkeypatch.setenv("CHART_FEED", "none")
    return TestClient(app), app


def _journal(tmp_path, monkeypatch, lines, *ranges):
    """The lines as the add-on sends them (through the feed, on the server's thread), then /api/journal per range."""
    c, app = _app(tmp_path, monkeypatch)
    feed = NinjaTraderFeed("127.0.0.1", 0, "")
    with c:
        m = app.state.market
        for line in lines:
            c.portal.call(feed._line, m, line.encode())
        out = [c.get(f"/api/journal?start={a}&end={b}").json() for a, b in ranges]
    assert feed.stats["bad_lines"] == 0
    return out


def _short(t):
    return (t["account"], t["side"], t["qty"], round(t["entry"], 2), t["exit"], t["open"], t["from_position"],
            t["unseen_exit"])


def test_a_false_flat_point_inside_the_week_no_longer_skews_later_trades(tmp_path, monkeypatch):
    """Installed while long 1 at 5000: its buy was never stored. Then +1 and -1 put the stored fills at zero before
    the range, so today's -1 (which really closed the position) opened a phantom short, and today's round trip showed
    as that short closed plus a new short left open. NinjaTrader's positions put it right: the long 2 closes today, at
    NinjaTrader's average price for the part never seen, and the round trip is its own trade."""
    lines = [P(T - 3 * DAY_MS, 1, 5000), FLAT(T - 3 * DAY_MS),                 # the add-on's first connect
             E(T - 2 * DAY_MS, 1, 1, 5010, 1), P(T - 2 * DAY_MS, 2, 5005),
             E(T - DAY_MS, -1, 1, 5020, 2), P(T - DAY_MS, 1, 5005),
             E(T + H, -1, 1, 5030, 3), P(T + H, 0),
             E(T + 2 * H, 1, 1, 5040, 4), P(T + 2 * H, 1, 5040), E(T + 3 * H, -1, 1, 5044, 5), P(T + 3 * H, 0)]
    (r,) = _journal(tmp_path, monkeypatch, lines, (T, T + DAY_MS))
    assert [_short(t) for t in r["trades"]] == [("Sim101", "long", 2, 5005.0, 5025.0, False, True, False),
                                                ("Sim101", "long", 1, 5040.0, 5044.0, False, False, False)]
    first = r["trades"][0]
    assert first["entry_ts"] == T - 3 * DAY_MS and first["fills"] == 3 and first["mae"] is None and first["mfe"] is None
    assert round(first["net"], 2) == 1993.88   # 20 points on 2, less the three fills' commissions (the unseen one's unknown)
    assert r["stats"]["trades"] == 2 and r["stats"]["open"] == 0 and r["open_since"] is None


def test_a_position_held_longer_than_a_week_keeps_its_own_opening_fill(tmp_path, monkeypatch):
    """Bought 10 days before the range and sold inside it: the 7-day walk read it as flat a week back, so the sell
    opened a phantom short. With the position NinjaTrader reported on each day's connect, its real buy counts."""
    lines = [E(T - 10 * DAY_MS, 1, 1, 5000, 1), P(T - 10 * DAY_MS, 1, 5000)]
    for d in range(9, 0, -1):   # NinjaTrader restarted each day
        lines += [P(T - d * DAY_MS, 1, 5000), FLAT(T - d * DAY_MS)]
    lines += [E(T + H, -1, 1, 5050, 2), P(T + H, 0)]
    (r,) = _journal(tmp_path, monkeypatch, lines, (T, T + DAY_MS))
    assert [_short(t) for t in r["trades"]] == [("Sim101", "long", 1, 5000.0, 5050.0, False, False, False)]
    assert r["trades"][0]["entry_ts"] == T - 10 * DAY_MS and round(r["trades"][0]["net"], 2) == 2495.92
    assert r["stats"]["open"] == 0


def test_a_position_closed_by_a_fill_never_received_ends_there_without_a_pnl(tmp_path, monkeypatch):
    """Bought 1, and NinjaTrader then said flat with no sell stored (it was made while the app wasn't listening). The
    long ends there, with no exit price, out of the stats; today's long is one trade, not added to the old one and left
    open."""
    lines = [E(T - 2 * H, 1, 1, 5000, 1), P(T - 2 * H, 1, 5000), P(T - H, 0),
             E(T + H, 1, 1, 5010, 2), P(T + H, 1, 5010), E(T + 2 * H, -1, 1, 5015, 3), P(T + 2 * H, 0)]
    today, wider = _journal(tmp_path, monkeypatch, lines, (T, T + DAY_MS), (T - 3 * H, T + DAY_MS))
    assert [_short(t) for t in today["trades"]] == [("Sim101", "long", 1, 5010.0, 5015.0, False, False, False)]
    assert today["stats"]["trades"] == 1 and today["stats"]["open"] == 0
    assert [_short(t) for t in wider["trades"]] == [("Sim101", "long", 1, 5000.0, None, False, False, True),
                                                    ("Sim101", "long", 1, 5010.0, 5015.0, False, False, False)]
    gone = wider["trades"][0]
    assert gone["exit_ts"] == T - H and gone["net"] is None and gone["points"] is None
    assert wider["stats"]["trades"] == 1 and wider["stats"]["open"] == 0 and round(wider["stats"]["net"], 2) == 245.92


def test_a_trade_whose_exit_was_never_received_has_no_mae_or_mfe(tmp_path, monkeypatch):
    """Long 1 at 5000, closed outside NinjaTrader minutes later; NinjaTrader next said flat two hours on. Its exit is
    as unknown as a from_position trade's entry, so the Journal tab's MAE/MFE are left out rather than read over the
    two hours of bars after it was already flat (a 100-point rally). A trade with both ends seen keeps its own."""
    t0 = T - T % 60_000
    px = [5000 if i < 10 else 5000 + (i - 10) if i < 110 else 5099 - (i - 110) * 2 for i in range(200)]
    lines = [E(t0, 1, 1, 5000, 1), P(t0 + 1000, 1, 5000), P(t0 + 2 * H, 0),
             E(t0 + 180 * 60_000, 1, 1, 4959, 2), E(t0 + 182 * 60_000, -1, 1, 4955, 3)]   # each in its minute's bar
    c, app = _app(tmp_path, monkeypatch)
    feed = NinjaTraderFeed("127.0.0.1", 0, "")
    with c:
        m = app.state.market
        c.portal.call(m.store.insert_rows_if_missing, "ES", [(t0 + i * 60_000, p, p + 1, p - 1, p, 10, 5) for i, p in enumerate(px)])
        for line in lines:
            c.portal.call(feed._line, m, line.encode())
        r = c.get(f"/api/journal?start={t0 - H}&end={t0 + DAY_MS}").json()
    assert feed.stats["bad_lines"] == 0
    gone, seen = r["trades"]
    assert _short(gone) == ("Sim101", "long", 1, 5000.0, None, False, False, True) and gone["exit_ts"] == t0 + 2 * H
    assert gone["mae"] is None and gone["mfe"] is None
    assert _short(seen) == ("Sim101", "long", 1, 4959.0, 4955.0, False, False, False)
    assert (seen["mae"], seen["mfe"]) == (5.0, 1.0)


def test_a_connect_with_nothing_open_ends_a_phantom_from_an_orphan_inside_the_week(tmp_path, monkeypatch):
    """An add-on 12 stored only the sell that closed a position from before it was installed. Add-on 13's first
    connect says the account holds nothing: the phantom short stops being open from then on."""
    lines = [E(T - 3 * DAY_MS, -1, 1, 5000, 1), FLAT(T - DAY_MS)]
    today, wider = _journal(tmp_path, monkeypatch, lines, (T, T + DAY_MS), (T - 4 * DAY_MS, T + DAY_MS))
    assert today["trades"] == [] and today["stats"]["open"] == 0 and today["open_since"] is None
    assert [_short(t) for t in wider["trades"]] == [("Sim101", "short", 1, 5000.0, None, False, False, True)]
    assert wider["trades"][0]["exit_ts"] == T - DAY_MS and wider["stats"]["open"] == 0


def test_contracts_without_positions_keep_the_seven_day_walk(tmp_path, monkeypatch):
    """Sim102's fills came from an add-on 12 (no P lines): its trade held for days still counts every fill back to
    where it was flat, next to Sim101's, which uses NinjaTrader's positions."""
    lines = [E(T - 10 * DAY_MS, 1, 1, 5000, 1), P(T - 10 * DAY_MS, 1, 5000), P(T - DAY_MS, 1, 5000), FLAT(T - DAY_MS),
             E(T + H, -1, 1, 5050, 2), P(T + H, 0),
             E(T - 5 * DAY_MS, 1, 2, 5000, 3, "Sim102"), E(T - DAY_MS, 1, 1, 5010, 4, "Sim102"),
             E(T + 2 * H, -1, 3, 5020, 5, "Sim102")]
    (r,) = _journal(tmp_path, monkeypatch, lines, (T, T + DAY_MS))
    assert [_short(t) for t in r["trades"]] == [("Sim101", "long", 1, 5000.0, 5050.0, False, False, False),
                                                ("Sim102", "long", 3, 5003.33, 5020.0, False, False, False)]


def test_p_lines_checked_stored_once_and_a_correction_tells_the_pages(tmp_path, monkeypatch):
    """Bad P lines are bad lines. A position that agrees with the fills stored (or the same snapshot again on the next
    connect) changes nothing and tells nobody; one that says otherwise, or a connect's flat for a contract the fills
    have open, has the open pages ask again ({"type": "fill"} with no fill)."""
    c, app = _app(tmp_path, monkeypatch)
    feed = NinjaTraderFeed("127.0.0.1", 0, "")
    with c:
        m = app.state.market
        sent = []
        m.broadcast = sent.append

        def feed_lines(*lines):   # what reaches the pages about fills ("fill+fill": with the fill)
            sent.clear()
            for line in lines:
                c.portal.call(feed._line, m, line.encode())
            return [s["type"] + ("+fill" if "fill" in s else "") for s in sent if s["type"] == "fill"]
        for bad in ["P|Sim101|ES|ES 12-26|x|1|5000|50", "P|Sim101|ES|ES 12-26|1|1|0|50", "P|Sim101|ES||1|1|5000|50",
                    "P|Sim101|ES|ES 12-26|1|1|nan|50", "P|Sim101|ES|ES 12-26|1|1.5|5000|50", "P|Sim101|x",
                    "P||ES|ES 12-26|1|1|5000|50", "P|Sim101|ES|ES 12-26|1|1|5000|-1"]:
            assert feed_lines(bad) == [], bad
        assert feed.stats["bad_lines"] == 8
        assert feed_lines(E(T, 1, 1, 5000, 1)) == ["fill+fill"]
        assert feed_lines(P(T, 1, 5000), FLAT(T)) == []                # agrees with the buy
        assert feed_lines(P(T, 1, 5000), FLAT(T)) == []                # the next connect's, the same
        assert feed_lines(P(T + H, 0)) == ["fill"]                     # flat with no sell stored
        assert feed_lines(FLAT(T + 2 * H)) == []                       # nothing open
        assert feed_lines(E(T + 3 * H, -1, 1, 5010, 2)) == ["fill+fill"]   # an orphan: the fills have a short
        assert feed_lines(FLAT(T + 4 * H)) == ["fill"]                 # NinjaTrader has nothing
        assert feed_lines(FLAT(T + 4 * H)) == []
        rows = c.portal.call(m.store.positions, 0, T * 2)
        assert [(p["ts"], p["qty"]) for p in rows] == [(T, 1), (T + H, 0), (T + 4 * H, 0)]
        assert rows[0] == {"account": "Sim101", "symbol": "ES", "contract": "ES 12-26", "ts": T, "qty": 1,
                           "avg_price": 5000.0, "point_value": 50.0}
        assert feed.stats["bad_lines"] == 8


def test_a_later_p_line_at_the_same_millisecond_replaces_the_earlier_one(tmp_path, monkeypatch):
    """The add-on stamps a P line no earlier than its contract's last line or the account's last connect, so the next
    one after a P line that came out late (a connect's snapshot, on a fills' clock the PC's had moved from) clamps to
    its millisecond: here a sale made 0.4 s before the snapshot's time, its own P line flat at the snapshot's.
    Positions are kept by account, contract and time, and the later replaces the earlier: one trade, not the sale's
    plus a 'Long 1' from the snapshot still open."""
    lines = [E(T, 1, 1, 5000, 1), P(T, 1, 5000), P(T + 5000, 1, 5000), FLAT(T + 5000),
             E(T + 4600, -1, 1, 5010, 2), P(T + 5000, 0)]
    (r,) = _journal(tmp_path, monkeypatch, lines, (T - H, T + H))
    assert [_short(t) for t in r["trades"]] == [("Sim101", "long", 1, 5000.0, 5010.0, False, False, False)]


def test_flat_true_reads_the_positions_inside_the_range_too(tmp_path, monkeypatch):
    """The chart's ask from where it knows every position was flat applies the positions after it as a full ask
    does: a short opened by a fill never received, then covered, is one trade at NinjaTrader's price either way."""
    lines = [E(T + H, 1, 1, 5000, 1), P(T + H, 1, 5000), E(T + 2 * H, -1, 1, 5004, 2), P(T + 2 * H, 0),
             P(T + 3 * H, -1, 5020), E(T + 4 * H, 1, 1, 5012, 3), P(T + 4 * H, 0)]
    c, app = _app(tmp_path, monkeypatch)
    feed = NinjaTraderFeed("127.0.0.1", 0, "")
    with c:
        for line in lines:
            c.portal.call(feed._line, app.state.market, line.encode())
        full = c.get(f"/api/journal?start={T}&end={T + DAY_MS}").json()
        part = c.get(f"/api/journal?start={T + 3 * H}&end={T + DAY_MS}&flat=true").json()
    assert [_short(t) for t in full["trades"]] == [("Sim101", "long", 1, 5000.0, 5004.0, False, False, False),
                                                   ("Sim101", "short", 1, 5020.0, 5012.0, False, True, False)]
    assert part["trades"] == full["trades"][1:]


def _add_on(port, wait_s=5):
    """The add-on's socket to the server, once its feed listens: the feed starts in a task after the app's start-up,
    so a connect straight after it can come first and be refused (about 1 in 15)."""
    import socket
    import time
    until = time.monotonic() + wait_s
    while True:
        try:
            return socket.create_connection(("127.0.0.1", port))
        except ConnectionRefusedError:
            if time.monotonic() > until:
                raise
            time.sleep(0.05)


def test_an_add_on_12_still_works_against_this_server(tmp_path, monkeypatch):
    """Add-on 12 over the real socket: no P lines, so its fills give the journal they gave before (the 7-day walk:
    the orphan sell inside the week pairs with today's buy, today's sell is a short left open) and its card says to
    update."""
    from test_feed_plugins import _card, _client, _wait
    app, client = _client(tmp_path, monkeypatch, feed="ninjatrader")
    with client as c:
        with _add_on(app.state.market.s.nt_port) as s:
            s.sendall(("\n".join(["HELLO||12", E(T - 3 * DAY_MS, -1, 1, 5000, 1), E(T + H, 1, 1, 5010, 2),
                                   E(T + 2 * H, -1, 1, 5015, 3)]) + "\n").encode())
            assert _wait(lambda: len(c.get(f"/api/fills?start=0&end={T * 2}").json()["fills"]) == 3)
            assert _card(c, "ninjatrader")["addon"]["old"] is True
        r = c.get(f"/api/journal?start={T}&end={T + DAY_MS}").json()
    assert [_short(t) for t in r["trades"]] == [("Sim101", "short", 1, 5000.0, 5010.0, False, False, False),
                                                ("Sim101", "short", 1, 5015.0, None, True, False, False)]
    assert r["stats"]["open"] == 1 and r["open_since"] == T + 2 * H


# ---- the chart: a new fill or position asks only for what it changes, and shows what a full ask would -----------------

needs_node = pytest.mark.skipif(not shutil.which("node"), reason="needs node")
STEPS_JS_LINE = "for (const f of group) onFill({ type: 'fill', fill: f });"


@needs_node
def test_the_chart_after_fills_and_positions_matches_a_full_ask(tmp_path, monkeypatch):
    """Each line the add-on sends goes through the feed; what the server broadcasts for it goes to the page's onFill.
    After each, the page's fills and finished trades are those a full ask from its first bar gets, through a position
    seeded before the first bar, one ended by a fill never received, one opened by one, and a connect's flat."""
    from test_hunt4_web_a import FILLS_PARTS, FILLS_REAL_JS
    from test_hunt_web_chart import app_part
    assert STEPS_JS_LINE in FILLS_REAL_JS
    js = FILLS_REAL_JS.replace(STEPS_JS_LINE, "const sent = []; for (const l of group) sent.push(...(await py({ add: l })).sent);\n"
                               "    for (const s of sent) onFill(s);")
    js = js.replace("for (const f of group) await py({ add: f });", "")
    js = js.replace("out.push({", "out.push({ final: S.fillTrades.map((t) => [t.account, t.side, t.entry, t.exit, t.from_position, t.unseen_exit]),")
    old = [P(T - 3 * DAY_MS, 1, 6800), FLAT(T - 3 * DAY_MS),                     # Sim101: long from before the install
           E(T - 2 * H, 1, 1, 6900, 1, "Live"), P(T - 2 * H, 1, 6900, "Live")]
    steps = [[E(T + H, -1, 1, 6905, 2, "Live"), P(T + H, 0, acct="Live")],     # closes a trade open before the first bar
             [P(T + 2 * H, 0)],                                                 # Sim101's long: ended by a fill not received
             [E(T + 3 * H, 1, 1, 6910, 3, "Live"), P(T + 3 * H, 1, 6910, "Live")],
             [E(T + 4 * H, -1, 1, 6915, 4, "Live"), P(T + 4 * H, 0, acct="Live")],
             [P(T + 5 * H, -1, 6920, "Live")],                                  # a short from a fill not received
             [E(T + 6 * H, 1, 1, 6912, 5, "Live"), P(T + 6 * H, 0, acct="Live")],   # ...covered
             [FLAT(T + 7 * H, "Live")],                                         # a connect: nothing open, nothing asked
             [E(T + 8 * H, -1, 1, 6900, 6), FLAT(T + 9 * H)]]                   # an orphan sell, then NinjaTrader's flat
    src = "\n".join(app_part(p) for p in FILLS_PARTS)
    js = js.replace("/*APP*/", src).replace("FIRST", str(T)).replace("STEPS", json.dumps(steps))
    c, app = _app(tmp_path, monkeypatch)
    feed = NinjaTraderFeed("127.0.0.1", 0, "")
    with c:
        m = app.state.market
        sent = []
        m.broadcast = sent.append
        for line in old:
            c.portal.call(feed._line, m, line.encode())
        p = subprocess.Popen(["node", "-e", js], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             text=True)
        out = None
        for line in p.stdout:
            msg = json.loads(line)
            if "out" in msg:
                out = msg["out"]
                break
            if "add" in msg:
                sent.clear()
                c.portal.call(feed._line, m, msg["add"].encode())
                reply = {"sent": [s for s in sent if s["type"] == "fill"]}
            else:
                reply = c.get(msg["ask"]).json()
            p.stdin.write(json.dumps(reply) + "\n")
            p.stdin.flush()
        p.stdin.close()
        assert p.wait() == 0 and out is not None, p.stderr.read()
    assert feed.stats["bad_lines"] == 0
    for i, s in enumerate(out):
        assert s["fills"] and s["trades"], (i, s)
    part = lambda ts: [f"/api/fills?start={ts}&symbol=ES", f"/api/journal?start={ts}&symbol=ES&excursions=false&flat=true"]
    assert out[3]["asks"] == part(T + 3 * H) and out[5]["asks"] == part(T + 5 * H) and out[6]["asks"] == []
    assert out[-1]["final"] == [["Sim101", "long", 6800, None, True, True], ["Live", "long", 6900, 6905, False, False],
                                ["Live", "long", 6910, 6915, False, False], ["Live", "short", 6920, 6912, True, False],
                                ["Sim101", "short", 6900, None, False, True]]


DRAW_JS = r"""
const calls = [];
const ctx = new Proxy({ measureText: (s) => ({ width: s.length * 6 }) }, {
  get: (t, k) => (k in t ? t[k] : (...a) => calls.push([k, ...a])), set: (t, k, v) => { t[k] = v; return true; } });
const S = { st: { fills: true }, bars: Array.from({ length: 30 }, (_, k) => ({ t: k * 60000 })), replay: null, fills: [], fillTrades: TRADES };
const TK_MULTI = false, C = { up: 'u', down: 'd' }, css = () => 'x', logicalX = (ts, l) => l, isTime = () => false;
const chart = { timeScale: () => ({ options: () => ({ barSpacing: 6 }) }) }, drawings = { timeToLogical: (t) => t / 60000 };
const candles = { priceToCoordinate: (p) => 1000 - p / 10 };   // as the chart's: null reads as a price of 0
/*APP*/
drawFills(ctx);
console.log(JSON.stringify(calls.filter((c) => c[0] === 'fillText' || c[0] === 'lineTo')));
"""


@needs_node
def test_the_chart_draws_no_line_for_a_trade_whose_exit_was_never_received():
    """A trade ended by a fill the app never got has no exit price: no dotted line to a price of 0 and no $0.00."""
    from test_hunt_web_chart import run_app
    trades = [{"entry_ts": 60_000, "exit_ts": 600_000, "entry": 5000, "exit": 5010, "net": 495.92},
              {"entry_ts": 900_000, "exit_ts": 1_200_000, "entry": 5000, "exit": None, "net": None, "unseen_exit": True}]
    out = run_app(["money", "tradeBar", "drawFills"], DRAW_JS.replace("TRADES", json.dumps(trades)))
    assert [c[0] for c in out] == ["lineTo", "fillText"] and out[1][1] == "+$495.92"
