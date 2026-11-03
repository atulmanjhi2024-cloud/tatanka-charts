"""Fixes from the sixth bug hunt of indicator alerts: first looks a trade had no time for are made right after it on
the event loop, not one a trade (on a thin market the bars of the trades to come were stepped over unchecked) and not
sent to a worker with part of the time (a short one waited there behind long replays); an intrabar crossing caught up
after a worker's replay is not reported in the next session; an edit that keeps the condition keeps the alert."""
import asyncio
import json
import shutil
import subprocess
import time
from datetime import datetime

import pytest

from chartsrv import ind_alerts as ia
from chartsrv import sessions, ta, workers
from chartsrv.bars import make_series, parse_spec
from chartsrv.models import Tick
from test_hunt_web_chart import WEB, app_part
from test_hunt_web_ui import app_js
from test_redesign_ind_alert_state import KEY, LINREG, _alert, _engine, _Pool, _trade, _until, _watch

EMA = {"ind": "ema", "line": "v", "len": 21}   # the price ends under it (_watch's falling series)


def _passes(n=10):
    """The event loop going round n times, as between two trades: each pass's CPU time (it held the loop that long)."""
    async def go():
        held = []
        for _ in range(n):
            t = time.thread_time()
            await asyncio.sleep(0)
            held.append(time.thread_time() - t)
        return held
    return go()


def _recording(monkeypatch):
    """ia._State with each first look's alert params and time on the event loop noted down."""
    made = []

    class State(ia._State):
        def __init__(self, p, w, last_fired=None, ended=False, budget=None):
            made.append((p, budget))
            super().__init__(p, w, last_fired, ended, budget)
    monkeypatch.setattr(ia, "_State", State)
    return made


# ---- first looks put off by a trade's first are made right after it, each with the whole time -----------------------

def test_on_a_thin_market_the_first_looks_put_off_are_made_before_the_next_trade(tmp_path, monkeypatch):
    """A trade a minute (each opens a bar), four long alerts ahead of two short ones. The first trade after a restart
    makes the first long one's first look, and the others are made right after it, not one a trade: the crossing on
    the next trade fires intrabar, and its bar's close fires on the trade after. None is stepped over by a first look
    made trades later. The loop is held briefly each time."""
    pool = _Pool()   # the long ones' replays never come back here
    monkeypatch.setattr(workers, "run", pool.run)
    w = _watch()
    ind = _engine(tmp_path, monkeypatch, w)
    alerts = [_alert(ia.clean_params({**LINREG, "len": 300 + 50 * i}, {"ind": "price"}, "1m", "close", "above"), i=i + 1)
              for i in range(4)]
    alerts += [_alert(ia.clean_params({"ind": "price"}, EMA, "1m", on, "above"), i=i) for i, on in ((50, "bar"), (51, "close"))]
    ind.m.alerts.alerts = {a.id: a for a in alerts}
    t0 = w.series.bars[-1].t

    async def go():
        fired, held, made = [], [], None
        for k in range(4):
            tk = Tick("ES", t0 + 30_000 + k * 60_000, 6200.0 if k else 6100.0, 1, 1)   # up through the EMA on trade 1
            ind.on_tick(tk)
            t = time.thread_time()
            fired += [(k, a.id) for a in alerts if ind.check(a, tk)]
            held.append(time.thread_time() - t)
            held += await _passes()   # a minute to the next trade
            if made is None:
                made = [a.id for a in alerts if a.id in ind.states or a.id in ind.replaying]
        return fired, made, max(held)
    fired, made, held = asyncio.run(go())
    assert fired == [(1, 50), (2, 51)] and held < 0.2, (fired, held)
    assert made == [1, 2, 3, 4, 50, 51] and len(pool.jobs) == 4, (made, pool.jobs)


def test_each_first_look_on_the_loop_has_the_whole_time_and_no_short_one_waits_behind_a_replay(tmp_path, monkeypatch):
    """A long alert, then short ones at their defaults: each first look made on the event loop (on a trade or right
    after it) has the whole INLINE_S, so none is sent to the worker with part of a trade's time left, to wait there
    behind the long replay. The short ones are made in place."""
    pool = _Pool()
    monkeypatch.setattr(workers, "run", pool.run)
    made = _recording(monkeypatch)
    w = _watch()
    ind = _engine(tmp_path, monkeypatch, w)
    alerts = [_alert(ia.clean_params({**LINREG, "len": 1000}, {"ind": "price"}, "1m", "bar", "above"))]
    alerts += [_alert(ia.clean_params({"ind": "price"}, {"ind": k, "line": ta.IND[k]["lines"][0]}, "1m", "bar", "cross"), i=10 + j)
               for j, k in enumerate(("ema", "sma", "rsi", "macd", "cci"))]
    ind.m.alerts.alerts = {a.id: a for a in alerts}

    async def go():
        for k in range(3):
            tk = _trade(ind, w, 10_000 + k * 1000, 6100.0)
            assert [ind.check(a, tk) for a in alerts] == [None] * len(alerts)
            await _passes()
    asyncio.run(go())
    assert [b for _, b in made] == [ia.INLINE_S] * len(alerts), made
    assert pool.jobs == ["study"] and list(ind.replaying) == [1] and sorted(ind.states) == [10, 11, 12, 13, 14]


def test_a_first_look_a_trade_made_before_its_turn_on_the_loop_is_not_made_again_nor_checked(tmp_path, monkeypatch):
    """The feed reads buffered trades in one go, with no pass of the loop between them: a trade after the one that put
    a first look off can make it first. The loop's pass then leaves it alone (queued once, made once, never checked
    there): the crossing a later trade makes is reported on that trade, not found in a pass that sends nothing."""
    pool = _Pool()
    monkeypatch.setattr(workers, "run", pool.run)
    made = _recording(monkeypatch)
    w = _watch()
    ind = _engine(tmp_path, monkeypatch, w)
    longs = [_alert(ia.clean_params({**LINREG, "len": 300 + 50 * i}, {"ind": "price"}, "1m", "bar", "above"), i=i + 1)
             for i in range(2)]
    ema = _alert(ia.clean_params({"ind": "price"}, EMA, "1m", "bar", "above"), i=50)
    alerts = longs + [ema]
    ind.m.alerts.alerts = {a.id: a for a in alerts}

    async def go():
        out, queued = [], []
        for dt, px in ((10_000, 6200.0), (10_001, 6200.0), (10_002, 6100.0), (10_003, 6100.0)):   # one read
            tk = _trade(ind, w, dt, px)
            out.append([ind.check(a, tk) for a in alerts])
            queued.append(list(ind._later))
        await _passes()
        tk = _trade(ind, w, 20_000, 6200.0)
        out.append([ind.check(a, tk) for a in alerts])
        return out, queued
    out, queued = asyncio.run(go())
    assert queued[:2] == [[2, 50], [2, 50]] and not ind._later, queued   # trade 1 made alert 2's, trade 2 the EMA's
    assert [p for p, _ in made].count(ema.params) == 1 and len(made) == 3, made
    assert out[:4] == [[None] * 3] * 4 and out[4][:2] == [None, None], out
    assert out[4][2] and "price 6200.00 crossed above EMA(21)" in out[4][2], out[4]


def test_a_first_look_put_off_for_an_alert_paused_or_deleted_meanwhile_is_not_made(tmp_path, monkeypatch):
    """Paused before the loop's pass came round: no state is made for it then, so restarted it starts afresh with a
    first look of its own, and a bar that closed past the line while it was off is not reported. Deleted: none."""
    pool = _Pool()
    monkeypatch.setattr(workers, "run", pool.run)
    w = _watch()
    ind = _engine(tmp_path, monkeypatch, w)
    long = _alert(ia.clean_params(LINREG, {"ind": "price"}, "1m", "close", "above"), i=1)
    ema = _alert(ia.clean_params({"ind": "price"}, EMA, "1m", "close", "above"), i=51)
    gone = _alert(ia.clean_params({"ind": "price"}, {**EMA, "ind": "sma"}, "1m", "close", "above"), i=52)
    ind.m.alerts.alerts = {a.id: a for a in (long, ema, gone)}
    t0 = w.series.bars[-1].t

    def trade(dt, px, alerts):
        tk = Tick("ES", t0 + dt, px, 1, 1)
        ind.on_tick(tk)
        return [ind.check(a, tk) for a in alerts]

    async def go():
        assert trade(10_000, 6100.0, [long, ema, gone]) == [None] * 3   # the EMA and SMA ones put off
        ema.enabled = False   # paused, and the other deleted, before the loop came round
        del ind.m.alerts.alerts[gone.id]
        ind.ensure()
        await _passes()
        kept = ema.id in ind.states or gone.id in ind.states
        trade(70_000, 6200.0, [long])    # while it is off, a bar above the EMA...
        trade(130_000, 6200.0, [long])   # ...closes there
        ema.enabled = True   # restarted
        ind.ensure()
        out = [trade(140_000, 6200.0, [long, ema]), trade(190_000, 6200.0, [long, ema])]
        return kept, out
    kept, out = asyncio.run(go())
    assert not kept and out == [[None, None]] * 2 and ema.id in ind.states, (kept, out)


# ---- an intrabar crossing caught up after a replay: in its own session only ------------------------------------------

def _ct(d, h, mi=0, s=0):
    return int(datetime(2025, 10, d, h, mi, s, tzinfo=sessions.CT).timestamp() * 1000)


def _watch_to(end):
    """A 1m watch whose 3000 bars are the open minutes up to `end`, the last one forming, price falling."""
    w = ia._Watch(make_series(parse_spec("1m"), 0.25, 2, 3000, studies=False))
    ts, mins = end, []
    while len(mins) < 3000:
        if sessions.is_open(ts):
            mins.append(ts)
        ts -= 60_000
    for k, t in enumerate(reversed(mins)):
        w.series.on_tick(Tick("ES", t, 7000 - (k / 100) ** 2, 1, 1))
    w.ready.set()
    w.version = 1
    return w


@pytest.mark.parametrize("day, nxt", [(17, (19, 17)), (14, (14, 17))], ids=["friday-to-sunday", "the-daily-break"])
def test_an_intrabar_crossing_in_a_sessions_last_bar_caught_up_after_the_close_is_not_reported_at_the_next_open(
        tmp_path, monkeypatch, day, nxt):
    """The first look two seconds before the 16:00 close goes to the worker, price crosses up the line on the last
    trade, and the replay comes back once the market is shut. No trade checks the bar then (the watchdog's pass is for
    bar-close alerts): the next session's first trade, at 17:00 or on Sunday, does not report it, an hour or two
    days late with that old bar's values, as before the catch-up. A crossing of its own in the new session does."""
    pool = _Pool()
    monkeypatch.setattr(workers, "run", pool.run)
    w = _watch_to(_ct(day, 15, 59))
    ind = _engine(tmp_path, monkeypatch, w)
    ind.watches = {KEY: w}
    a = _alert(ia.clean_params({"ind": "price"}, LINREG, "1m", "bar", "above"))
    ind.m.alerts.alerts = {a.id: a}

    def trade(ts, px):
        tk = Tick("ES", ts, px, 1, 1)
        ind.on_tick(tk)
        return ind.check(a, tk)

    async def go():
        out = [trade(_ct(day, 15, 59, 58), 6100.0), trade(_ct(day, 15, 59, 59), 7000.0)]   # the first look; the cross
        await asyncio.sleep(0)
        assert pool.jobs == ["study"] and a.id not in ind.states
        pool.go.set()   # back after 16:00
        await _until(lambda: a.id in ind.states)
        out += [trade(_ct(*nxt, 0, 0), 7000.0), trade(_ct(*nxt, 0, 1), 7000.0)]
        out += [trade(_ct(*nxt, 1, 0), 6000.0), trade(_ct(*nxt, 2, 0), 7000.0)]   # down through it, then up again
        return out
    out = asyncio.run(go())
    assert out[:5] == [None] * 5, out
    assert out[5] and "price 7000.00 crossed above Regression(300,2) Middle" in out[5], out


# ---- an edit that keeps an indicator alert's condition is a PATCH: it keeps what it last saw ---------------------------

SAVE_JS = r"""
const vm = require('vm'), fs = require('fs'), calls = [];
const els = {}, $ = (sel) => (els[sel] ||= { value: '', checked: false, disabled: false });
const ctx = { console, Intl, window: {}, $, toast: (m) => calls.push(`toast ${m}`), fail: (e) => calls.push(`fail ${e.message}`),
  api: async (method, path, body) => { calls.push({ method, path, body }); return { id: 99 }; },
  describe: () => ['x'], closeAlertDialog() {}, num: Number, ax: { input: { toMs: () => /*FUTURE*/ }, short: 'CT' },
  S: { rth: false, instruments: { ES: {} } }, dlg: {} };
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(/*TA*/, 'utf8'), ctx);
vm.runInContext(/*APP*/, ctx);
(async () => {
const out = [];
for (const c of /*CASES*/) {   // the dialog as indDlgFill left it for the alert, then edited
  Object.assign(ctx.dlg, { edit: c.alert, sym: 'ES', repeat: false, on: c.on });
  for (const [sel, v] of Object.entries({ '#al-src': `i:${c.kind}`, '#al-msg': 'a new note', '#al-iline': c.line, '#al-vs': c.vs,
    '#al-vsv': c.vsv ?? '', '#al-ispec': c.spec, '#al-op': c.op, ...c.params })) $(sel).value = v;
  $('#al-exp-on').checked = true;
  calls.length = 0;
  await $('#al-form').onsubmit({ preventDefault() {} });
  out.push(calls.slice());
}
console.log(JSON.stringify(out));
})().catch((e) => { console.error(e); process.exit(1); });
"""


@pytest.mark.skipif(not shutil.which("node"), reason="needs node")
def test_saving_an_indicator_alert_with_its_condition_as_it_was_patches_it_and_keeps_what_it_saw(tmp_path, monkeypatch):
    """The Alerts dialog's Save on an indicator alert whose condition is as it was (its message, trigger or expiry
    edited) PATCHes it, as for other alerts, instead of making a new one with nothing seen yet: it keeps its state and
    last fire, so the crossing it reported in this bar is not sent again. A series saved before there were more
    counts as the same; another timeframe is another alert (made new, the old one deleted). The server takes those
    fields for an indicator alert."""
    from fastapi.testclient import TestClient
    from chartsrv.app import app
    monkeypatch.setenv("CHART_DB", str(tmp_path / "chart.db"))
    monkeypatch.setenv("CHART_LEVELS_DIR", str(tmp_path))
    monkeypatch.setenv("CHART_FEED", "none")
    future = int(time.time() * 1000) + 86_400_000
    with TestClient(app) as c:
        def make(a, b, spec, on, direction):
            r = c.post("/api/alerts", json={"symbol": "ES", "kind": "ind", "a": a, "b": b, "spec": spec, "on": on,
                                             "direction": direction, "repeat": True, "note": "old"})
            assert r.status_code == 201, r.text
            return r.json()
        reg = make({"ind": "linreg", "line": "middle", "len": 1000, "width": 2}, {"ind": "price"}, "1m", "bar", "above")
        old = make({"ind": "ema", "len": 9}, {"ind": "ema", "len": 21}, "5m", "close", "cross")   # as saved before
        assert reg["params"]["a"]["width"] == 2.0 and old["params"]["a"] == {"ind": "ema", "len": 9}
        cases = [
            {"alert": reg, "kind": "linreg", "line": "middle", "params": {"#al-ip-len": "1000", "#al-ip-width": "2"},
             "vs": "price", "spec": "1m", "on": "bar", "op": "above"},
            {"alert": reg, "kind": "linreg", "line": "middle", "params": {"#al-ip-len": "1000", "#al-ip-width": "2"},
             "vs": "price", "spec": "5m", "on": "bar", "op": "above"},   # another timeframe
            {"alert": old, "kind": "ema", "line": "v", "params": {"#al-ip-len": "9"}, "vs": "ema", "vsv": "21",
             "spec": "5m", "on": "close", "op": "cross"},
        ]
        src = "\n".join([app_part("same"), app_part("indLineNew"), app_part("dlgBody"), app_part("indDlgBody"),
                         app_part("sameAlert"), app_js("$('#al-form').onsubmit = ")])
        js = (SAVE_JS.replace("/*TA*/", json.dumps(str(WEB / "ta.js"))).replace("/*APP*/", json.dumps(src))
              .replace("/*CASES*/", json.dumps(cases)).replace("/*FUTURE*/", str(future)))
        r = subprocess.run(["node", "-e", js], capture_output=True, text=True)
        assert r.returncode == 0, r.stderr
        same, other, saved_before = json.loads(r.stdout)
        edit = {"enabled": True, "repeat": False, "note": "a new note", "expires": future}
        assert same == [{"method": "PATCH", "path": f"/api/alerts/{reg['id']}", "body": edit}, "toast Alert saved"], same
        assert [x["method"] for x in other[:2]] == ["POST", "DELETE"] and other[0]["body"]["spec"] == "5m", other
        assert saved_before[0] == {"method": "PATCH", "path": f"/api/alerts/{old['id']}", "body": edit}, saved_before

        m = c.app.state.market   # the alert fired in this bar, and has its state: both kept by the PATCH
        c.portal.call(lambda: m.store.update_alert(reg["id"], last_fired=future - 86_400_000))   # on the server's thread
        st = m.ind_alerts.states[reg["id"]] = object()
        r = c.patch(f"/api/alerts/{reg['id']}", json=same[0]["body"])
        assert r.status_code == 200, r.text
        got = r.json()
        assert got["params"] == {**reg["params"], "expires": future} and got["note"] == "a new note" and got["repeat"] is False
        assert got["last_fired"] == future - 86_400_000 and m.ind_alerts.states[reg["id"]] is st
