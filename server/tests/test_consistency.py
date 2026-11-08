"""Things a chart shows twice that must agree (found by a hunt after the AMT Toolkit's pd lines and its profile's value
area disagreed): Gap alerts and the Gap table, a PVP line's end and the bar that touched it (RTH charts; GC's and CL's
float levels), If/Then's ATR on an RTH chart, the info lines' hours and rows, Level Lines' labels and their lines."""
import json
import random
from datetime import datetime

from chartsrv import gaps, gaps_service, tpo_service, vp, vp_service
from chartsrv.models import Bar
from chartsrv.sessions import CT
from chartsrv.studies import RthClock
from test_hunt_web_chart import needs_node, run_app


def ms(d, h, mi=0):
    return int(datetime(2026, 3, d, h, mi, tzinfo=CT).timestamp() * 1000)


def gap_minutes():
    """Mon 2026-03-02's RTH closes at 6000. Overnight drifts to 6010 by Tue 08:00, 08:00-08:30 rallies to 6020 (the
    08:30 minute opens there), then trades back into the gap to 6012."""
    out, t = [], ms(1, 17)
    while t < ms(3, 16):
        lt = datetime.fromtimestamp(t / 1000, CT)
        hm = lt.hour * 60 + lt.minute
        if lt.hour != 16:
            if lt.day <= 2:
                p = 6000.0
            elif hm < 480:
                p = 6010.0
            elif hm < 510:
                p = round((6010.0 + (hm - 480) / 3) * 4) / 4
            elif hm < 600:
                p = 6020.0
            else:
                p = 6012.0
            out.append(Bar(t=t, o=p, h=p, l=p, c=p, v=10))
        t += 60_000
    return out


def test_gap_alerts_say_what_the_charts_table_does():
    """The alerts run on the bars of the chart they were switched on from: on a 60-minute all-hours ES chart the
    08:00 bar is the first RTH one, so the gap opens at 6010 (10 points), as the table has it, not at the 08:30 minute's
    6020. And the minimum gap size hides it from both."""
    for minutes, size in ((60, 0.0), (30, 0.0), (60, 15.0), (1, 0.0)):
        s = gaps.settings({"MinimumGapPoints": size})
        eng, opens = gaps_service.run(s, minutes, gap_minutes())
        table = [(g.OpenPrice, g.SettlePrice) for g in eng.History]
        w = gaps_service.GapWatch({**{k: s[k] for k in gaps.DEFAULTS}, "minutes": minutes}, ("08:30", "15:00"))
        alerts = []
        for b in gap_minutes():
            u = w.add(b)
            if u and u.NewGap is not None:
                alerts.append((u.NewGap.OpenPrice, u.NewGap.SettlePrice))
        assert alerts == table, (minutes, size, alerts, table)
        assert [g.FillEdge for g in w.engine.History] == [g.FillEdge for g in eng.History]
    assert table == [(6020.0, 6000.0)]                                   # 1-minute bars: the 08:30 minute
    old = gaps_service.GapWatch({k: gaps.DEFAULTS[k] for k in gaps.DEFAULTS}, ("08:30", "15:00"))
    assert old.bars.period == 60_000                                     # a config saved before: 1-minute bars


def test_bar_builder_makes_chart_bars_as_the_minutes_form():
    """Minutes coming again as they form, out of order now and then: the bars it ends with are chart_bars'."""
    rnd = random.Random(3)
    mins = [b for b in gap_minutes() if rnd.random() > 0.05]
    for minutes, rth in ((5, None), (60, None), (30, ("08:30", "15:00")), (1, None)):
        bb, last = tpo_service.BarBuilder(minutes, rth), {}
        prev = None
        for b in mins:
            if rnd.random() < 0.3:   # the minute seen while forming first
                bb.add(Bar(t=b.t, o=b.o, h=b.o, l=b.o, c=b.o, v=1))
            got = bb.add(b)
            if prev is not None and rnd.random() < 0.05:
                assert bb.add(prev) is None                                  # a minute already passed
            prev = b
            if got:
                last[got[0]] = got[1:]
        want = tpo_service.chart_bars(mins, minutes, rth)
        assert [(k, c, o, h, low, cl) for _, (k, c, o, h, low, cl) in sorted(last.items())] == \
            [(k, close, o, h, low, c) for k, close, h, low, _s, o, c in want], (minutes, rth)


def test_pvp_lines_end_where_a_bar_touched_them():
    """A level is a tick count times the tick size: 2600.1000000000004 on GC. A minute whose high is 2600.1 touches it.
    And an RTH chart's PVP takes its regular hours' minutes only: an overnight minute neither builds it nor touches."""
    level = 26001 * 0.1
    assert level != 2600.1
    st = vp_service.MinuteState(None)
    prof = type("P", (), {"last_chart": 0, "touch": {}})()
    st.watch, st.watch_p = [(level, prof, "vah")], [level]
    st._touch(2599.5, 2600.1, 60_000)
    assert prof.touch == {"vah": 60_000}
    hours = RthClock(("08:30", "15:00"))
    st = vp_service.MinuteState(vp.PeriodicSeries("Daily", 1, 2, 0.25, False, 1, 70.0, False), hours)
    st.feed([Bar(t=ms(3, 2), o=6000, h=6000, l=6000, c=6000, v=5), Bar(t=ms(3, 9), o=6010, h=6010, l=6010, c=6010, v=5)])
    assert st.pend_t == ms(3, 9)                                          # the 02:00 minute left out


def test_ifthen_takes_an_rth_charts_regular_hours_days(client, monkeypatch):  # noqa: F811
    from chartsrv import app as app_mod
    asked = []
    real = app_mod.M().tpo.daily_for
    monkeypatch.setattr(app_mod.M().tpo, "daily_for", lambda sym, cur, rth=False, replay=False: asked.append(rth) or real(sym, cur, rth, replay))
    assert client.get("/api/ifthen/ES?atr=14").status_code == 200
    assert client.get("/api/ifthen/ES?atr=14&rth=true").status_code == 200
    assert asked == [False, True]


@needs_node
def test_labels_and_info_lines_say_what_is_drawn():
    """Level Lines: a line between ticks labelled at its own price; on a tick as before. TPO: the newest profile's rows.
    SVP and Gap Analysis: the hours the server runs (the instrument's own: CL's 08:00-13:30)."""
    out = run_app(["llPrice", "ownHours", "renderTpoInfo"], """
const inst = () => ({ symbol: 'ES', tick_size: 0.25, decimals: 2, rth: ['08:30', '15:00'] }), bondTick = () => false;
const px = (p) => Number(p).toFixed(2), el = { textContent: '' }, $ = () => el, tpoSpecOk = () => true;
const S = { st: { tpo: true }, tpoSet: { SessionTime: '0830-1500' }, tpoError: '',
  tpo: { step: 0.75, minutes: 30, profiles: [{ step: 2.0 }, { step: 0.5 }] } };
/*APP*/
renderTpoInfo();
console.log(JSON.stringify([[5050, 5005.25, 5027.625, 7845.1, 7845.125, 7845].map(llPrice), el.textContent]));
""")
    assert out[0] == ["5050.00", "5005.25", "5027.625", "7845.10", "7845.125", "7845.00"]
    assert out[1].startswith("Rows 0.50 (2 ticks)")


from test_amt import client  # noqa: E402,F401  (the app on the simulator, as a fixture)
