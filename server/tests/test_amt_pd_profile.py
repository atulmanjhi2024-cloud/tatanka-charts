"""AMT Toolkit v1.2.1: the prior day's drawn profile (Session mode) comes from the same trades as its pdPOC, pdVAH and
pdVAL, so its shaded value area ends where the lines are. A member's NQ chart had the lines from the day's ticks
(v1.2.0) beside a profile still built from 1-minute bars spread over their range: the shading stopped a few points from
pdVAH and pdVAL, and the profile's peak sat at the minutes' POC (26902.50) beside the trades' pdPOC (26880.00)."""
from datetime import datetime

import pytest

from chartsrv import amt
from chartsrv.amt_runner import AmtRunner
from amt_parity import synth, trades_by_day

TICK = 0.25


def frames(settings: dict, ticks: bool, minutes=5):
    """Every 30th minute's frame of 6 days of NQ, the day's RTH trades given to the engine as its TradeValue."""
    bars = synth(6, 5, datetime(2026, 10, 16, 15, 30), TICK, 25000.0)
    trades = trades_by_day(bars, TICK) if ticks else {}
    r = AmtRunner("NQ", TICK, minutes, amt.Settings(settings))
    r.commit_visuals = True
    out = []
    for i, m in enumerate(bars):
        r.add_minute(m)
        if r.engine is not None:
            r.engine.TradeValue = lambda first, end: trades.get(first.strftime("%Y-%m-%d"))
        if i % 30 == 0:
            r.commit_ready()
            f, _ = r.preview()
            if f is not None:
                out.append(f)
    return out, trades


def pd_lines(f):
    return {x.Name: x.Price for x in f.Lines if x.Name in ("pdPOC", "pdVAH", "pdVAL")}


@pytest.mark.parametrize("pct", [68, 70])
def test_the_prior_days_shaded_value_area_ends_at_its_lines(pct):
    """Overnight (the prior day's profile is the newest drawn): its POC and value area are the lines', its rows the
    day's trades at one-tick rows, so its tallest row is pdPOC and the shaded rows end at pdVAH and pdVAL."""
    fs, trades = frames({"mvp_tf_mode": "Session", "mvp_val_pct": float(pct), "cc_va_pct": pct / 100}, ticks=True)
    overnight = [f for f in fs if (f.Bar.Start.hour >= 17 or f.Bar.Start.hour < 8) and len(pd_lines(f)) == 3]
    assert len(overnight) > 30
    for f in overnight:
        lines, p = pd_lines(f), f.Profiles[-1]
        assert (p.Stats.Poc, p.Stats.Vah, p.Stats.Val) == (lines["pdPOC"], lines["pdVAH"], lines["pdVAL"]), f.Bar.Start
        assert sum(p.Profile.Rows.values()) == sum(v for _, v in trades[p.Start.strftime("%Y-%m-%d")])   # its trades
        assert p.Profile.Step == TICK   # one-tick rows, whatever the Row Scale (NQ's Auto is 5 ticks)
        peak = max(p.Profile.Rows, key=lambda k: p.Profile.Rows[k]) * p.Profile.Step
        assert peak == lines["pdPOC"], f.Bar.Start                         # its tallest row is pdPOC
        shaded = [k * p.Profile.Step for k in p.Profile.Rows if lines["pdVAL"] <= k * p.Profile.Step <= lines["pdVAH"]]
        assert min(shaded) == lines["pdVAL"] and max(shaded) == lines["pdVAH"]   # the shading ends at the lines


def test_a_day_without_trades_and_a_weekly_profile_are_as_before():
    """No trades for the day (older than the sessions downloaded): lines and profile from the minutes, as before. A
    Weekly profile (the default) is a week, not the prior day: the same with or without the trades."""
    minutes, _ = frames({"mvp_tf_mode": "Session"}, ticks=False)
    f = next(f for f in reversed(minutes) if f.Bar.Start.hour >= 17 and len(pd_lines(f)) == 3)
    assert f.Profiles[-1].Stats.Total and (f.Profiles[-1].Stats.Vah, f.Profiles[-1].Stats.Val) != (None, None)
    weekly, _ = frames({}, ticks=True)
    weekly_minutes, _ = frames({}, ticks=False)
    assert [(p.Stats.Vah, p.Stats.Val, len(p.Profile.Rows)) for p in weekly[-1].Profiles] == \
        [(p.Stats.Vah, p.Stats.Val, len(p.Profile.Rows)) for p in weekly_minutes[-1].Profiles]


@pytest.mark.parametrize("ticks", [True, False])
def test_every_pd_line_is_the_same_days(ticks):
    """pdHigh, pdLow and pdOpen switched to the day just closed at 15:00, pdPOC, pdVAH and pdVAL at the 17:00 open: for
    two hours each afternoon pdPOC could sit below "pdLow" (a member's NQ chart: pdPOC 26880.00, pdLow 26988.50, today's
    low). They switch together now: the value is inside the high and low of its own day at every frame."""
    fs, _ = frames({}, ticks=ticks)
    checked = after_close = 0
    for f in fs:
        ln = {x.Name: x.Price for x in f.Lines if x.Name in ("pdHigh", "pdLow", "pdPOC", "pdVAH", "pdVAL")}
        if len(ln) < 5:
            continue
        checked += 1
        after_close += 15 <= f.Bar.Start.hour < 17
        assert ln["pdLow"] - TICK <= ln["pdVAL"] <= ln["pdPOC"] <= ln["pdVAH"] <= ln["pdHigh"] + TICK, (f.Bar.Start, ln)
    assert checked > 100 and after_close > 5
