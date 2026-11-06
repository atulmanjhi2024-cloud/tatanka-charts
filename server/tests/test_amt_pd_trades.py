"""The AMT Toolkit's prior-day value (pdPOC, pdVAH, pdVAL) from the day's trades at one-tick rows, as a tick volume
profile and the SVP: a member's tick profile (Rancho Dinero) had NQ's Nov 5 POC at 26880.00 where the Toolkit, from
each minute's volume spread over its range in 1.25-point rows, had 26902.50. The trades are worked out once in a
worker and kept (vp_cache); until then, and for older days, the minutes are used as before."""
import asyncio
import json
from datetime import datetime, timedelta

from chartsrv import amt_hub, amt_runner, sessions
from chartsrv.amt_hub import pd_job
from chartsrv.models import Bar, Tick
from chartsrv.sessions import CT
from test_review_fixes import build_market

NOW = datetime(2026, 11, 5, 18, 0, tzinfo=CT).timestamp()   # Thu Nov 5, after the close: the Thursday rolled to Friday


def ms(d: datetime) -> int:
    return int(d.replace(tzinfo=CT).timestamp() * 1000)


RTH = (ms(datetime(2026, 11, 5, 8, 30)), ms(datetime(2026, 11, 5, 15, 0)))


def history(store):
    """Wed 17:00 to Thu 17:30 CT. In RTH each minute trades 50 at 7002, 30 at 7004, 20 at 7008 (its bar 7000-7010),
    except 14:59, which has its bar only (a gap-filled minute)."""
    ticks, bars = [], []
    t = datetime(2026, 11, 4, 17, 0)
    while t < datetime(2026, 11, 5, 17, 30):
        if not (datetime(2026, 11, 5, 16, 0) <= t < datetime(2026, 11, 5, 17, 0)):   # the daily halt
            m = ms(t)
            if RTH[0] <= m < RTH[1]:
                bars.append(("ES", Bar(t=m, o=7000, h=7010, l=7000, c=7005, v=100)))
                if t.time() != datetime(2026, 1, 1, 14, 59).time():
                    ticks += [Tick("ES", m + 1000, 7002.0, 50, 1), Tick("ES", m + 2000, 7004.0, 30, 1),
                              Tick("ES", m + 3000, 7008.0, 20, -1)]
            else:
                bars.append(("ES", Bar(t=m, o=7000, h=7000, l=7000, c=7000, v=10)))
        t += timedelta(minutes=1)
    store.write(ticks, bars)


def test_the_job_reads_trades_and_a_minute_without_them_from_its_bar(tmp_path, monkeypatch):
    m, _ = build_market(tmp_path, monkeypatch)
    history(m.store)
    rows = dict(pd_job(m.store.path, "ES", [RTH, (RTH[0] - 86_400_000, RTH[0] - 86_400_000 + 60_000)], 0.25)[RTH])
    assert abs(sum(rows.values()) - 39_000) < 1e-6                          # 389 minutes of trades and one bar
    assert abs(rows[7002.0] - (389 * 50 + 100 / 41)) < 1e-6                 # 14:59: 100 over its 41 ticks
    assert abs(rows[7000.25] - 100 / 41) < 1e-6 and abs(rows[7010.0] - 100 / 41) < 1e-6
    assert pd_job(m.store.path, "ES", [(1, 2)], 0.25) == {(1, 2): []}       # nothing stored: none


def test_the_prior_day_comes_from_its_trades_once_worked_out(tmp_path, monkeypatch):
    sessions.set_calendar({"days": []})
    monkeypatch.setattr(amt_hub.time, "time", lambda: NOW)
    monkeypatch.setattr(amt_runner.time, "time", lambda: NOW)

    async def go():
        m, _ = build_market(tmp_path, monkeypatch)
        history(m.store)
        e = await m.amt.entry("ES", 5)
        pd = e.runner.engine.prevDay
        assert e.runner.pd_missing == {RTH}                     # from its minutes for now: 7000-7010 spread evenly
        assert pd.Poc == 7000.0
        for _ in range(200):                                    # the job: worked out, the runner loads again
            await asyncio.sleep(0.05)
            if ("ES", 5) not in m.amt.entries and not m.amt.pd_busy:
                break
        # no NinjaTrader add-on: from the trades stored here, kept in memory only (a recent session: NinjaTrader is
        # asked for it again after a restart)
        assert RTH in m.amt.pd_mem["ES"] and not m.store.db.execute("SELECT 1 FROM vp_cache WHERE key LIKE 'amtpd%'").fetchone()
        e = await m.amt.entry("ES", 5)
        pd = e.runner.engine.prevDay
        assert (pd.Poc, pd.Vah, pd.Val) == (7002.0, 7004.0, 7002.0) and not e.runner.pd_missing
        m.amt.reload("ES")                                      # a gap fill: worked out again
        assert "ES" not in m.amt.pd_mem
    asyncio.run(go())


class FakeAddOn:
    """The NinjaTrader add-on as the hub sees it: GAP lines it is sent (answered by the test through gapfill)."""
    name = "ninjatrader"

    def __init__(self):
        self.lines = []

    def history_ok(self):
        return True

    def ask_history(self, line):
        self.lines.append(line)
        return True


async def answer(m, addon, trades, kind="ticks"):
    """Waits for the GAP line, then sends the trades (X lines) and Z, as the add-on would."""
    for _ in range(200):
        if addon.lines:
            break
        await asyncio.sleep(0.05)
    _, sym, a, b = addon.lines.pop(0).split("|")
    assert (int(a), int(b)) == (RTH[0] - 60_000, RTH[1] + 60_000)
    for ts, px, size in trades:
        m.gapfill.tick(sym, ts, px, size)
    m.gapfill.done(sym, kind, len(trades), "ok")
    m.gapfill.next(sym)


def test_the_prior_day_comes_from_ninjatraders_tick_history_when_the_add_on_can_send_it(tmp_path, monkeypatch):
    """NinjaTrader's history (what the NinjaTrader Toolkit downloads) rather than the trades recorded here: they can
    differ by a few hundred contracts, a few ticks on a value area edge. A trade on the minute counts in the minute it
    ends, as NinjaTrader's bars: 08:30:00.000 is 08:29's (left out), 15:00:00.000 is 14:59's (in)."""
    sessions.set_calendar({"days": []})
    monkeypatch.setattr(amt_hub.time, "time", lambda: NOW)
    monkeypatch.setattr(amt_runner.time, "time", lambda: NOW)

    async def go():
        m, _ = build_market(tmp_path, monkeypatch)
        history(m.store)
        addon = FakeAddOn()
        m.feed_objs.append(addon)
        await m.amt.entry("ES", 5)
        trades = [(RTH[0], 7000.0, 30_000),                     # 08:30:00.000: 08:29's, left out
                  (RTH[0] + 500, 7006.0, 25_000), (RTH[0] + 60_000 * 60, 7002.0, 10_000),
                  (RTH[1], 7008.0, 3_000)]                      # 15:00:00.000: 14:59's, in
        await answer(m, addon, trades)
        for _ in range(200):
            await asyncio.sleep(0.05)
            if ("ES", 5) not in m.amt.entries and not m.amt.pd_busy:
                break
        row = m.store.db.execute("SELECT data FROM vp_cache WHERE key=?", (f"amtpd2|ES|{RTH[0]}|{RTH[1]}",)).fetchone()
        assert row and json.loads(row[0]) == [[7002.0, 10_000], [7006.0, 25_000], [7008.0, 3_000]]
        pd = (await m.amt.entry("ES", 5)).runner.engine.prevDay
        assert pd.Poc == 7006.0                                 # NinjaTrader's, not the stored trades' 7002

        m.amt.reload("ES")                                      # no tick history to send: the stored trades
        await m.amt.entry("ES", 5)
        await answer(m, addon, [], kind="none")
        for _ in range(200):
            await asyncio.sleep(0.05)
            if ("ES", 5) not in m.amt.entries and not m.amt.pd_busy:
                break
        assert (await m.amt.entry("ES", 5)).runner.engine.prevDay.Poc == 7002.0
    asyncio.run(go())


def test_older_days_stay_on_their_minutes(monkeypatch):
    """Past PD_TRADE_DAYS (beyond the 20 days of lines kept) a day is never asked for."""
    r = amt_runner.AmtRunner("ES", 0.25, 5, amt_runner.Settings())
    monkeypatch.setattr(amt_runner.time, "time", lambda: NOW)
    old = datetime(2026, 9, 1, 8, 30)
    assert r.trade_value(old, old + timedelta(hours=6, minutes=30)) is None and not r.pd_missing
    recent = datetime(2026, 11, 5, 8, 30)
    assert r.trade_value(recent, recent + timedelta(hours=6, minutes=30)) is None and r.pd_missing
    r.pd_values[RTH] = []                                       # nothing stored that day: minutes, and not asked again
    r.pd_missing.clear()
    assert r.trade_value(recent, recent + timedelta(hours=6, minutes=30)) is None and not r.pd_missing
