"""Round 12 of the bug hunt, the round-11 gap alert fixes: a new gap in the trades the add-on buffered while the server
was down is sent with the next live trade; a stretch answered with trades for part of it holds the alerts until its
rest is in; a gap alert switched while the page reads the gap alerts again stays as switched; and a held gap whose
stretch failed is still sent when the stretch held no regular-hours minute it is measured from."""
import json
import shutil
import subprocess
import time
from datetime import timedelta

import pytest

from chartsrv.gapfill import filling
from test_hunt10_hold import add_on, at_open, ct_ms, events  # noqa: F401  (at_open: the fixture)
from test_hunt11_gapload import UP, held, opening, stored, trader
from test_hunt7_drawings import _client


# ---- k-gl12-1: a new gap in the add-on's buffer (the server down across the open) is sent with the next live trade -------

def buffered_open(tmp_path, monkeypatch, at_open, down_s):
    """ES's data stops at 08:29:57 (the day before closed at 6000); the server goes down at 08:29:58 and is back
    `down_s` seconds later, the add-on reconnecting 3 s after. Its buffer brings ES's open, 6050 at 08:30:01, then a
    trade every 10 s; then live trades. The alert events sent after the buffer and after the live trades."""
    day, shift = at_open
    opening(tmp_path, day, (8, 29, 57))
    shift[0] += down_s
    app, client = _client(tmp_path, monkeypatch)
    with client as c:
        m = app.state.market
        c.portal.call(m.gaps.watches["ES"].ready.wait)
        shift[0] += 3
        nt = add_on(m)
        hello = int(time.time() * 1000)
        c.portal.call(lambda: m.gapfill.requests(hello, live=True, minutes=True, contracts=True))
        stale = filling(m, "ES")
        now = int(time.time() * 1000)
        for k, ts in enumerate(range(ct_ms(day, 8, 30, 1), now - 10_000, 10_000)):   # all over 10 s old: not live
            c.portal.call(nt._line, m, f"T|ES|{ts}|{6050.25 if k else 6050.0}|1|1\n".encode())
        buffered = events(c)
        if stale:   # held while its stretch is asked for: the add-on finds nothing in it (the buffer starts the live data)
            c.portal.call(nt._line, m, b"Z|ES|minutes|0|ok\n")
            assert not filling(m, "ES")
        trade = trader(c, m, nt)
        trade(6051.0)
        trade(6051.25, 500)
        trade(6051.5, 1000)
        m.feed_objs.remove(nt)
        return stale, buffered, events(c)


@pytest.mark.parametrize("down_s", [22, 132])
def test_a_new_gap_in_the_add_ons_buffer_is_sent_with_the_next_live_trade(tmp_path, monkeypatch, at_open, down_s):
    """The server restarts at 08:29:58 with NinjaTrader running and is back at 08:30:20 (or, a Docker update, at
    08:32:10: a stretch that holds ES's alerts); the add-on sends ES's open from its buffer, too old to be live. The
    buffered trades send nothing; the first live trade sends the gap, once (before: never, the engine's new gap used
    up on the buffer)."""
    stale, buffered, sent = buffered_open(tmp_path, monkeypatch, at_open, down_s)
    assert stale == (down_s > 120) and buffered == [] and sent == [UP]


def test_a_new_gap_in_the_add_ons_buffer_over_6_minutes_old_is_not_sent(tmp_path, monkeypatch, at_open):
    """The same with the server back at 08:37:10: the gap's first minute is no longer live (LIVE_BAR_AGE_MS)."""
    stale, buffered, sent = buffered_open(tmp_path, monkeypatch, at_open, 432)
    assert stale and buffered == sent == []


# ---- k-gl12-2: a stretch answered with trades for part of it holds the alerts until its rest is in -----------------------

def partial(tmp_path, monkeypatch, at_open, open_after, hole, failed=False):
    """ES's data stops yesterday at 12:00 at 6000 (the PC off); its RTH close, 6045, is in the stretch. NinjaTrader
    starts at 08:29:58 (ES's alerts held) and answers with the trades it recorded itself, from 17:00 yesterday on
    (at 08:30:31, or at 08:29:59.5 when `open_after`); the server asks the rest as 1-minute bars. ES opens 6050 at
    08:30:01; the rest comes 20 s after the trades (`failed`: its request fails). `hole`: a hole in yesterday's
    morning is asked for after the rest.
    The alert events sent before the rest came, and in all."""
    day, shift = at_open
    prev = day - timedelta(days=1)
    gone = range(ct_ms(prev, 9, 0), ct_ms(prev, 9, 30), 60_000) if hole else range(0)
    last = stored(tmp_path, [(t, 6000.0) for t in range(ct_ms(prev, 8, 30) - 3 * 86_400_000, ct_ms(prev, 12, 0) + 1, 60_000)
                             if t not in gone])
    app, client = _client(tmp_path, monkeypatch)
    with client as c:
        m = app.state.market
        c.portal.call(m.gaps.watches["ES"].ready.wait)
        nt = add_on(m)
        hello = int(time.time() * 1000)
        c.portal.call(lambda: m.gapfill.requests(hello, live=True, minutes=True, contracts=True))
        assert filling(m, "ES")
        trade = trader(c, m, nt)

        def trades():
            for t in (ct_ms(prev, 17, 0, 5), ct_ms(prev, 20, 0), ct_ms(day, 8, 29, 50)):
                c.portal.call(nt._line, m, f"X|ES|{t}|6040|1\n".encode())
            c.portal.call(nt._line, m, b"Z|ES|ticks|3|ok\n")
            assert nt.writer.lines[-1] == f"GAP|ES|{last}|{ct_ms(prev, 17, 0)}|minutes"   # the rest
            assert filling(m, "ES")
            c.portal.call(m.gaps.watches["ES"].ready.wait)   # loaded again with the trades
        if open_after:
            shift[0] += 1.5   # 08:29:59.5
            trades()
        shift[0] += 1.5 if open_after else 3   # 08:30:01
        trade(6050.0)
        trade(6050.25, 300)
        if not open_after:
            shift[0] += 30   # 08:30:31
            trades()
        trade(6050.5)
        trade(6050.75, 500)
        before = events(c)
        shift[0] += 20
        ys = [f"Y|ES|{t}|6045|6045|6045|6045|1" for t in range(ct_ms(prev, 12, 1), ct_ms(prev, 16, 0), 60_000)]
        c.portal.call(lambda: [nt._line(m, y.encode()) for y in (["Z|ES|error|0|BarsRequest failed"] if failed else
                                                                  ys + [f"Z|ES|minutes|{len(ys)}|ok"])])
        assert not filling(m, "ES")
        assert m.gapfill.windows["ES"].hole if hole else "ES" not in m.gapfill.windows   # the hole asked for next
        c.portal.call(m.gaps.watches["ES"].ready.wait)
        trade(6050.5)
        trade(6050.75, 500)
        m.feed_objs.remove(nt)
        return before, events(c)


@pytest.mark.parametrize("open_after,hole", [(False, False), (True, False), (False, True)])
def test_a_stretch_answered_with_trades_for_part_of_it_holds_the_alerts_until_its_rest_is_in(
        tmp_path, monkeypatch, at_open, open_after, hole):
    """The gap is sent once the rest is in, measured from yesterday's RTH close in it (before: the hold lifted at the
    trades' answer, and the gap was sent measured from yesterday's noon price, 50 points; the right one never)."""
    before, sent = partial(tmp_path, monkeypatch, at_open, open_after, hole)
    assert before == [] and sent == ["[ES] GAP UP 5.00 pts | Open: 6050.00 | RTH close: 6045.00"]


def test_a_held_gap_is_not_sent_when_the_rest_of_its_stretch_failed(tmp_path, monkeypatch, at_open):
    """The same, the rest's request failing: the gap would be measured from yesterday's noon price, so it is not sent
    (before: sent so, 50 points)."""
    before, sent = partial(tmp_path, monkeypatch, at_open, False, False, failed=True)
    assert before == sent == []


# ---- k-gl12-3: a gap alert switched while the page reads the gap alerts again stays as switched -------------------------

RACE_JS = """
const els = {};
const $ = (q) => (els[q] ||= { textContent: '', checked: false, value: '' });
const gapSpecOk = () => true, indReady = () => true, local = { set() {} }, isExtSym = () => false, watch = { render() {} };
const WL_FUT = /^([A-Z0-9]{1,6})1!$/, TK_MULTI = false;
let gapDays = 90, appConfig = null;
const toasts = [], toast = (t) => toasts.push(t), fail = (e) => toasts.push('fail ' + e);
const server = { ES: { on: true, new: true, fill: true, rth: false } };
const pending = [];
const api = (method, url, body) => new Promise((res) => pending.push({ method, url, body, res }));
const take = (method) => pending.splice(pending.findIndex((p) => p.method === method), 1)[0];
const copy = () => ({ alerts: JSON.parse(JSON.stringify(server)), watches: {} });
const flush = async () => { for (let i = 0; i < 10; i++) await null; };
const S = { symbol: 'ES', rth: false, st: { gap: true }, gaps: { gaps: [] }, gapError: '', instruments: { ES: {} }, catalog: {},
  gapAlerts: { ES: { on: true, new: true, fill: true } },
  gapSet: { RthStart: 83000, RthEnd: 150000, MinimumGapPoints: 0, AlertOnNewGap: true, AlertOnGapFill: true, EnabledAnalysis: true, days: 90 } };
/*APP*/
(async () => {
  renderGapMenu();
  onMessage({ type: 'instruments', instruments: { ES: {}, RTY: {} }, catalog: {}, config: 'x' });   // the GET goes out
  $('#gap-alerts').checked = false;
  const change = $('#gap-alerts').onchange({ target: $('#gap-alerts') });   // the PUT, before the GET's answer
  const get = take('GET'), put = take('PUT'), old = copy();   // the server handles the GET first (alerts on)...
  server.ES = { ...server.ES, on: false };   // ...then the PUT
  if (PUT_FIRST) { put.res(server.ES); await flush(); get.res(old); } else { get.res(old); await flush(); put.res(server.ES); }
  await change;
  await flush();
  while (pending.length) { take('GET').res(copy()); await flush(); }   // asked again
  console.log(JSON.stringify({ checked: $('#gap-alerts').checked, page: S.gapAlerts.ES, info: $('#gap-info').textContent, toasts }));
})();
"""


@pytest.mark.skipif(not shutil.which("node"), reason="needs node")
@pytest.mark.parametrize("put_first", [False, True])
def test_a_gap_alert_switched_while_the_page_reads_them_again_stays_as_switched(put_first):
    """ES's gap alerts on; another device adds a future, so the page reads the gap alerts again, and before that GET's
    answer the user unticks Alerts (the server handles the GET first). Whichever answer comes back first, the box
    stays unticked and the Studies line says nothing of alerts, as the server has them off (before: ticked again, 'Alerts:
    new gap + fill.', the PUT's answer written into the alerts the GET's had replaced)."""
    from test_hunt_web_chart import app_lines, app_part
    parts = ["hhmm", "gapRth", "gapRthOk", "GAP_RTH_BAD", "gapAlertsPut", "renderGapInfo", "renderGapMenu", "loadGapAlerts",
             "putGapAlerts", "onMessage"]
    src = RACE_JS.replace("PUT_FIRST", json.dumps(put_first)).replace(
        "/*APP*/", "\n".join(app_part(p) for p in parts) + "\n" + app_lines("$('#gap-alerts').onchange", "};"))
    r = subprocess.run(["node", "-e", src], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout.strip().split("\n")[-1])
    assert not out["checked"] and not out["page"]["on"] and out["info"].endswith(" CT."), out
    assert out["toasts"] == ["Gap alerts off for ES"]


# ---- k-gl12-4: a held gap whose stretch failed is sent when the stretch held no RTH minute it is measured from ----------

@pytest.mark.parametrize("days_before", [1, 2])
def test_a_held_gap_is_sent_when_its_failed_stretch_held_no_rth_minute(tmp_path, monkeypatch, at_open, days_before):
    """ES's data runs through yesterday's RTH close (6000 at 14:59) to 16:00 (6040), when the PC was shut down; ES
    opens 6050 while its alerts are held, and the stretch's history request fails. The gap, measured from that close,
    does not depend on the stretch: it is sent (before: dropped). Stopping the day before yesterday at 16:00 instead,
    yesterday's RTH is in the stretch: the gap would be measured from the day before's close, so it is not sent."""
    day, _ = at_open
    prev = day - timedelta(days=days_before)
    rows = [(t, 6000.0) for t in range(ct_ms(prev, 8, 30) - 3 * 86_400_000, ct_ms(prev, 15, 0), 60_000)]
    rows += [(t, 6040.0) for t in range(ct_ms(prev, 15, 0), ct_ms(prev, 16, 0), 60_000)]
    during, sent = held(tmp_path, monkeypatch, at_open, rows, [(0, 6050.0), (500, 6050.25)], "Z|ES|error|0|BarsRequest failed\n")
    assert during == [] and sent == ([UP] if days_before == 1 else [])
