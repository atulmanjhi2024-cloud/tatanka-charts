"""AMT Toolkit v1.2.2: every pd line, and the prior day's drawn profile, switch to the session just closed at its close
(15:00 CT), from its trades. Before, the profile stayed the minutes' until the 17:00 open (all weekend after a Friday),
and pdPOC/pdVAH/pdVAL with it. Also the ten older mismatches a hunt found: the OVN INVENTORY row, the prior month's value,
alerts on lines not drawn, Poor High/Low, Enable IB and the day type, GC's value rows, the level zones' ATR, a later
"RTH Session for VA", GC's bars on a 15- or 30-minute chart, CL's nPOC rows."""
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from chartsrv import amt
from chartsrv.amt import Bar, Engine, LevelAlertTracker, LevelWatch, Profile, ZoneItem
from chartsrv.amt_hub import AmtHub
from chartsrv.amt_runner import AmtRunner
from amt_parity import synth, trades_by_day

TICK = 0.25
PD = ("pdHigh", "pdLow", "pdOpen", "pdPOC", "pdVAH", "pdVAL")


def committed(settings: dict, ticks=True, minutes=5, days=8, root="NQ", tick=TICK, price=25000.0, seed=5):
    """Every committed frame (with its drawing lists) of `days` of synthetic minutes, the RTH trades as TradeValue."""
    bars = synth(days, seed, datetime(2026, 10, 16, 15, 30), tick, price)
    trades = trades_by_day(bars, tick) if ticks else {}
    r = AmtRunner(root, tick, minutes, amt.Settings(settings))
    r.commit_visuals = True
    r.set_levels(round(price / tick) * tick, [round(price * (1 + k / 400) / tick) * tick for k in (-4, 4)], None, None)
    out, last = [], None
    for m in bars:
        r.add_minute(m)
        if r.engine is not None:
            r.engine.TradeValue = lambda first, end: trades.get(first.strftime("%Y-%m-%d"))
        r.commit_ready()
        if r.prev_frame is not last:
            last = r.prev_frame
            out.append(last)
    return out, trades, r


def lines(f):
    return {x.Name: x for x in f.Lines if x.Name in PD}


@pytest.mark.parametrize("ticks", [True, False])
def test_every_pd_line_and_the_profile_switch_at_the_close(ticks):
    """The first bar after 15:00: pdHigh/pdLow/pdOpen the closed session's RTH, pdPOC/pdVAH/pdVAL its value (from its
    trades when there are), drawn from that bar; the closed session's profile is drawn from the same trades (its
    tallest row is pdPOC). The 17:00 open keeps the values and draws them from there."""
    fs, trades, _ = committed({"mvp_tf_mode": "Session", "pd_showOpen": True}, ticks)
    checked = 0
    for i in range(1, len(fs) - 1):
        f, before = fs[i], fs[i - 1]
        if (f.Bar.Start.hour, f.Bar.Start.minute) != (15, 0) or len(lines(before)) < 6:
            continue   # (pdOpen is drawn: it's off by default)
        day = f.Bar.Start.date()
        rth = [g for g in fs if g.Bar.Start.date() == day and 510 <= g.Bar.Start.hour * 60 + g.Bar.Start.minute < 900]
        ln = lines(f)
        assert (ln["pdHigh"].Price, ln["pdLow"].Price) == (max(g.Bar.High for g in rth), min(g.Bar.Low for g in rth))
        assert ln["pdOpen"].Price == rth[0].Bar.Open
        if ticks:
            v = Profile.from_prices(TICK, trades[day.strftime("%Y-%m-%d")]).Stats(.68)
            assert (ln["pdPOC"].Price, ln["pdVAH"].Price, ln["pdVAL"].Price) == (v.Poc, v.Vah, v.Val)
            p = f.Profiles[-1]
            assert p.Profile.Step == TICK and max(p.Profile.Rows, key=p.Profile.Rows.get) * TICK == v.Poc
        assert all(x.Start == f.Bar.Start for x in ln.values())          # drawn from the close
        assert ln["pdPOC"].Price != lines(before)["pdPOC"].Price or ln["pdHigh"].Price != lines(before)["pdHigh"].Price
        nxt = next((g for g in fs[i:] if g.Bar.Start.hour == 17), None)    # the next trade date's open
        if nxt is not None:
            assert {k: x.Price for k, x in lines(nxt).items()} == {k: x.Price for k, x in ln.items()}
            assert all(x.Start == nxt.Bar.Start for x in lines(nxt).values())
        checked += 1
    assert checked >= 4


def test_the_days_80_rule_and_open_keep_their_reference_after_the_close():
    """After the close the pd lines are today's own; the 80% Rule fill, OPEN and 80% RULE rows stay measured against
    the value in force at today's open."""
    fs, _, _ = committed({}, True)
    seen = 0
    for i in range(1, len(fs)):
        f, before = fs[i], fs[i - 1]
        if (f.Bar.Start.hour, f.Bar.Start.minute) != (15, 0) or not before.Values:
            continue
        assert (f.VaHigh, f.VaLow) == (before.VaHigh, before.VaLow)
        assert f.RuleActive == before.RuleActive
        assert (f.Values[4], f.Values[7]) == (before.Values[4], before.Values[7]) or f.Values[4] == "---"
        seen += 1
    assert seen >= 4


def test_the_chart_keeps_each_line_as_it_stood_before_the_close():
    """History: the pd lines over the day up to 15:00 (kept at once), then the closed session's from 15:00 to 17:00."""
    fs, _, r = committed({}, True)
    closes = [f for f in fs if (f.Bar.Start.hour, f.Bar.Start.minute) == (15, 0)][:-1]
    for c in closes:
        before = next(f for f in reversed(fs) if f.Bar.Start < c.Bar.Start)
        for name, x in lines(before).items():
            kept = [h for h in r.history if h.Name == name and h.End == x.End]
            assert kept and kept[0].Price == x.Price and kept[0].Start == x.Start, (c.Bar.Start, name)
        last = next(f for f in reversed(fs) if f.Bar.Start.hour == 15 and f.Bar.Start.date() == c.Bar.Start.date())
        for name, x in lines(last).items():
            assert any(h.Name == name and h.Start == c.Bar.Start and h.Price == x.Price for h in r.history), name


def bar(t, o, h, l, c, v=100, i=0, minutes=5):
    return Bar(t, t + timedelta(minutes=minutes), o, h, l, c, v, i)


def drive(e, rows, minutes=5):
    """rows: (start, o, h, l, c); every bar processed with visuals; the frames."""
    out = []
    for i, (t, o, h, l, c) in enumerate(rows):
        out.append(e.Process(bar(t, o, h, l, c, i=i, minutes=minutes), None, None))
    return out


def test_ovn_inventory_is_the_count_at_its_last_overnight_bar():
    """A night all above the 6800 settle; the 08:20 bar trades down to it (the trap resolves). From 08:30 the row
    says what the count was then, not 08:15's TRAP LONG (100%)."""
    e = Engine(amt.Settings({}), "ES", .25, 6800, 5)
    t, rows = datetime(2026, 10, 12, 14, 0), []
    while t < datetime(2026, 10, 13, 9, 30):
        if t.hour != 16:
            if t < datetime(2026, 10, 12, 15, 0):
                rows.append((t, 6800, 6801, 6799, 6800))
            elif t == datetime(2026, 10, 13, 8, 20):
                rows.append((t, 6801, 6802, 6799.75, 6801))
            else:
                rows.append((t, 6802, 6803, 6801, 6802))
        t += timedelta(minutes=5)
    fs = drive(e, rows)
    at = {f.Bar.Start: f for f in fs}
    assert at[datetime(2026, 10, 13, 8, 15)].Values[3] == "TRAP LONG (100%)"
    after = at[datetime(2026, 10, 13, 8, 35)].Values[3]
    assert after.startswith("LONG (") and after == e.Inventory()


def test_alerts_only_for_lines_drawn():
    """OVN High/Low stop alerting with their lines (15:00 in the NinjaTrader defaults); a section switched off arms
    nothing (it armed PDH/PDL, pwHigh/pwLow, the Developing lines)."""
    s = {"amt_del_sess": "1500-1505", "amt_alert_ovn": True, "pd_alert_hl": True, "cc_alert_va": True,
         "mvp_use_cur_alerts": True}
    fs, _, _ = committed(s, False, days=4)
    for f in fs:
        w = {x.Name: x.Enabled for x in f.Watches}
        drawn = {x.Name for x in f.Lines}
        if "OVN High" in w:
            assert w["OVN High"] == ("OVN High" in drawn)
        for watch, line in (("PDH", "pdHigh"), ("pdPOC", "pdPOC")):
            if watch in w:
                assert w[watch] == (line in drawn), (f.Bar.Start, watch)
    off, _, _ = committed({**s, "pd_enable": False, "cc_enable": False, "mvp_master": False}, False, days=4)
    for f in off:
        w = {x.Name: x.Enabled for x in f.Watches}
        assert not any(w.get(k) for k in ("PDH", "PDL", "pdPOC", "pdVAH", "pdVAL", "Developing POC", "Developing VAH"))


def test_poor_high_and_low_are_the_sessions_high_and_low():
    """Two TPO periods at the same 6810.00 high and 6790.00 low: Poor High 6810.00 (it was 6810.25), Poor Low 6790.00.
    A trade one tick beyond repairs them."""
    e = Engine(amt.Settings({}), "ES", .25, 6800, 30)
    d = datetime(2026, 10, 13)
    drive(e, [(d + timedelta(hours=8, minutes=30), 6800, 6810, 6790, 6800), (d + timedelta(hours=9), 6800, 6810, 6790, 6800),
              (d + timedelta(hours=9, minutes=30), 6800, 6805, 6795, 6800)], 30)
    assert sorted((p.High, p.Price) for p in e.poor) == [(False, 6790.0), (True, 6810.0)]
    drive(e, [(d + timedelta(hours=10), 6800, 6810.25, 6795, 6800)], 30)
    assert [p.Price for p in e.poor] == [6790.0]


def test_a_single_print_fills_when_its_price_trades_from_below():
    e = Engine(amt.Settings({}), "ES", .25, 6800, 5)
    t = datetime(2026, 10, 13, 18)
    e.single = [ZoneItem(6800.25, 6800.0, t, 0, Created=t)]
    e.UpdateTpo(bar(t, 6799, 6800.0, 6798, 6799), False)                   # up to 6800.00, the single print's price
    assert e.single == []


@pytest.mark.parametrize("off", [{"ib_enable": False}, {"amt_enable": False}])
def test_switching_ib_or_ovn_lines_off_leaves_the_day_type(off):
    on, _, _ = committed({}, False, days=6)
    no, _, _ = committed(off, False, days=6)
    assert [f.Values[1] for f in on if f.Values] == [f.Values[1] for f in no if f.Values]
    assert not any(x.Name.startswith("IB " if "ib_enable" in off else "OVN") for f in no for x in f.Lines)


def test_gc_value_lines_are_on_its_ticks():
    """GC's prior day from 1-minute bars: rows at the drawn profile's 0.5 (0.25 put pdPOC at 3414.25, labeled 3414.3)."""
    fs, _, _ = committed({}, False, days=6, root="GC", tick=.1, price=3500.0)
    vals = [x.Price for f in fs for x in f.Lines if x.Name in ("pdPOC", "pdVAH", "pdVAL")]
    assert vals and all(abs(v * 2 - round(v * 2)) < 1e-9 for v in vals)


def test_level_zones_use_the_daily_atr_period_and_alert_their_width():
    fs, _, _ = committed({"daily_atr_length": 5, "lvl_zone_mode": "ATR %"}, False, days=40, root="ES", price=6800.0)
    f = fs[-1]
    hz = next(x.HalfWidth for x in f.Watches if x.Name == "PIVOT")
    assert hz == pytest.approx(f.DailyAtr * amt.Settings({}).lvl_zone_atr_pct)
    t = LevelAlertTracker()
    w = LevelWatch("PIVOT", 6700.0, 1.117, True, False)
    t.Observe([w], 6705, "Zone Entry", "ES", .25)
    assert [n.Text for n in t.Observe([w], 6700.5, "Zone Entry", "ES", .25)] == ["PIVOT Zone Entry @ 6700.00 (±1.12)"]


def test_a_later_rth_session_for_va_decides_the_gap_test_and_the_80_rule_together():
    """RTH Session for VA from 09:00. Monday's value about 6795-6805; Tuesday opens 6800 at 08:30 (inside) and 6780 at
    09:00 (below): OPEN, the fill and the 80% RULE row all go by 09:00's open (the row stayed "---" under the fill)."""
    e = Engine(amt.Settings({"cc_sess_def": "0900-1500:23456"}), "ES", .25, 6800, 30)
    t, rows = datetime(2026, 10, 11, 17), []
    while t < datetime(2026, 10, 13, 11):
        if t.hour != 16:
            if t >= datetime(2026, 10, 13, 9):
                rows.append((t, 6780, 6785, 6778, 6782) if t.hour == 9 and t.minute == 0 else (t, 6782, 6800, 6780, 6798))
            elif t >= datetime(2026, 10, 13, 8, 30):
                rows.append((t, 6800, 6801, 6799, 6800))
            else:
                rows.append((t, 6800, 6805, 6795, 6800))
        t += timedelta(minutes=30)
    fs = {f.Bar.Start: f for f in drive(e, rows, 30)}
    f = fs[datetime(2026, 10, 13, 10)]
    assert e.prevDay.Val > 6780 and f.RuleActive
    assert (f.Values[4], f.Values[7]) == ("Below pdLow", "ACTIVE")
    assert e.dayType != "GAP UP"


def test_the_prior_month_is_its_minutes_value_when_the_chart_has_all_of_it():
    """pmPOC/pmVAH/pmVAL are the drawn Monthly profile's (both from the month's minutes at its rows: NQ's August
    pmPOC sat 93.75 points off its profile's POC, from 30-minute bars), and its Monthly nPOC is that POC."""
    fs, _, r = committed({"mvp_tf_mode": "Monthly", "npoc_m_show": True}, False, days=75, root="NQ")
    e = r.engine
    assert e.pmMinutes != amt.DMIN
    p = next(x for x in e.profiles if amt.month(e.TradeDate(x.Start)) == e.pmMinutes)
    assert (p.Stats.Poc, p.Stats.Vah, p.Stats.Val) == (e.prevMonth.Poc, e.prevMonth.Vah, e.prevMonth.Val)
    assert e.NakedStep(e.rowMult) == p.Profile.Step


def test_cl_weekly_npoc_is_the_weekly_profiles_poc():
    fs, _, r = committed({}, False, days=30, root="CL", tick=.01, price=65.0, seed=4)
    e = r.engine
    weekly = [n for n in e.naked if n.Kind == "Weekly"]
    assert weekly
    for n in weekly:
        p = next(x for x in e.profiles if x.End == n.Start)
        assert n.Price == p.Stats.Poc


def test_gc_runs_on_bars_its_open_falls_on():
    m = SimpleNamespace(instruments={"GC": SimpleNamespace(rth=("07:20", "12:30")), "ES": SimpleNamespace(rth=("08:30", "15:00")),
                                     "CL": SimpleNamespace(rth=("08:00", "13:30"))})
    hub = AmtHub.__new__(AmtHub)
    hub.m = m
    got = {(s, n): hub.engine_minutes(s, n) for s in ("GC", "ES", "CL") for n in (1, 5, 7, 15, 20, 30)}
    assert got[("GC", 30)] == 10 and got[("GC", 15)] == 5 and got[("GC", 5)] == 5
    assert got[("ES", 30)] == 30 and got[("ES", 15)] == 15 and got[("ES", 20)] == 10 and got[("ES", 7)] == 1
    assert got[("CL", 30)] == 30 and got[("CL", 5)] == 5
