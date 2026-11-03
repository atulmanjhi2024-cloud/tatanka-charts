"""Round 14, sessions: CME's holidays. A closed one (Christmas, New Year's Day, Good Friday) is like the weekend for a
feed's replay of its last trade, and the one-time clean-up of such prints stored before runs after the start, in
slices on a worker thread, and not at all on the simulator. An RTH chart's last bar closes with a holiday's halt or
early close (its bar-close alerts), and a D chart's footprint and Yahoo's older days hold a halt's session in the next
day's bar, as the chart's own bars do."""
import asyncio
import shutil
import sqlite3
import threading
from datetime import date, datetime, timedelta

import pytest

from chartsrv import app as app_mod
from chartsrv import ind_alerts as ia
from chartsrv import market as market_mod
from chartsrv import sessions
from chartsrv import store as store_mod
from chartsrv.bars import make_series, parse_spec
from chartsrv.config import ROOT, load_holidays
from chartsrv.feeds.ninjatrader import NinjaTraderFeed
from chartsrv.feeds.sdk import FeedPlugin
from chartsrv.models import Bar, Tick
from chartsrv.sessions import CT
from chartsrv.store import Store

from test_hunt_web_chart import FP_JS, app_client, run_app  # noqa: F401  (app_client: the fixture)
from test_review_fixes import build_market

weekend = pytest.mark.weekend   # the feeds' rule and the start's clean-up on, whatever day the tests run


def ct(*a) -> int:
    return int(datetime(*a, tzinfo=CT).timestamp() * 1000)


def cme_holidays():
    sessions.set_calendar(load_holidays(ROOT / "config", today=date(2026, 11, 2)))


def put(st: Store, sym: str, stamps: list[int]):
    """Trades stored at these times, with their 1-minute bars."""
    minutes = sorted({t // 60_000 * 60_000 for t in stamps})
    st.write([Tick(sym, t, 6905.25, 1, 0) for t in sorted(stamps)], [(sym, Bar(t, *[6905.25] * 4, 1, 0, 0, 1)) for t in minutes])


def stored(db, sym: str = "ES") -> list[int]:
    return [r[0] for r in db.execute("SELECT ts FROM ticks WHERE symbol=? ORDER BY ts", (sym,))]


# --- r14-sessions-1: closed holidays -------------------------------------------------------------------------------

@weekend
def test_a_closed_holidays_session_is_like_the_weekend():
    """Christmas 2026 is a Friday (Christmas Eve closes at 12:15), so is New Year's Day 2027 and Good Friday (Mar 26)."""
    cme_holidays()
    for t in [ct(2026, 12, 24, 17, 0), ct(2026, 12, 25, 10, 15), ct(2027, 1, 1, 9, 0), ct(2027, 3, 25, 18, 0),
              ct(2027, 3, 26, 10, 0),
              ct(2026, 12, 25, 16, 10), ct(2026, 12, 25, 16, 40)]:   # a closed Friday's 16:00 hour: no session either side
        assert sessions.weekend(t), sessions.to_ct(t)
    for t in [ct(2026, 12, 24, 12, 10), ct(2026, 12, 24, 16, 40), ct(2026, 12, 28, 9, 0), ct(2027, 3, 25, 15, 0)]:
        assert not sessions.weekend(t), sessions.to_ct(t)
    # the break before a closed day: back to the session closing, never on into the closed one's 17:00 open
    assert sessions.snap(ct(2026, 12, 24, 16, 40)) == ct(2026, 12, 24, 16, 0) - 1
    assert sessions.snap(ct(2026, 12, 31, 16, 45)) == ct(2026, 12, 31, 16, 0) - 1
    assert sessions.snap(ct(2026, 12, 23, 16, 40)) == ct(2026, 12, 23, 17, 0)   # a day before an ordinary one: as ever
    # Christmas 2025 is a Thursday: its 16:00 hour goes on to Friday's session, not back into its own
    assert sessions.snap(ct(2025, 12, 25, 16, 10)) == ct(2025, 12, 25, 17, 0) and not sessions.weekend(ct(2025, 12, 25, 16, 10))
    assert sessions.snap(ct(2025, 12, 24, 16, 40)) == ct(2025, 12, 24, 16, 0) - 1 and sessions.weekend(ct(2025, 12, 24, 17, 0))
    # the clean-up's spans: the weekends and the closed days' sessions
    assert sessions.weekends(ct(2026, 12, 21, 0, 0), ct(2027, 1, 4, 0, 0)) == [
        (ct(2026, 12, 24, 17, 0), ct(2026, 12, 25, 16, 0)), (ct(2026, 12, 25, 17, 0), ct(2026, 12, 27, 16, 0)),
        (ct(2026, 12, 31, 17, 0), ct(2027, 1, 1, 16, 0)), (ct(2027, 1, 1, 17, 0), ct(2027, 1, 3, 16, 0))]


@weekend
def test_the_sunday_before_a_closed_monday_has_no_session_to_move_a_print_into():
    """Christmas 2028 is a Monday: Sunday's pre-open hour would go to Sunday's 17:00 open, Christmas's session."""
    sessions.set_calendar({"days": [{"date": "2028-12-25", "name": "Christmas", "closed": True}]})
    assert sessions.weekend(ct(2028, 12, 24, 16, 40)) and sessions.weekend(ct(2028, 12, 24, 17, 0))
    assert sessions.weekend(ct(2028, 12, 25, 10, 0)) and not sessions.weekend(ct(2028, 12, 25, 16, 10))
    assert sessions.snap(ct(2028, 12, 24, 16, 40)) != ct(2028, 12, 24, 17, 0)
    assert sessions.snap(ct(2028, 12, 25, 16, 10)) == ct(2028, 12, 25, 17, 0)   # on to Tuesday's session
    sessions.set_calendar({"days": []})
    assert not sessions.weekend(ct(2028, 12, 25, 10, 0)) and sessions.snap(ct(2028, 12, 24, 16, 40)) == ct(2028, 12, 24, 17, 0)


@weekend
def test_ninjatraders_replay_on_christmas_and_new_years_day_is_not_stored(tmp_path, monkeypatch):
    """Before: the replay at Christmas's 10:15 made a one-price 1D bar for Dec 25 (and an RTH one); one in Christmas
    Eve's break went on to Thursday 17:00, Christmas's session."""
    cme_holidays()

    async def go():
        m, _ = build_market(tmp_path, monkeypatch, feed="ninjatrader")
        feed = NinjaTraderFeed("127.0.0.1", 0, "")
        for t in [ct(2026, 12, 24, 12, 10), ct(2026, 12, 24, 16, 40), ct(2026, 12, 24, 18, 0), ct(2026, 12, 25, 10, 15),
                  ct(2026, 12, 25, 16, 40), ct(2027, 1, 1, 9, 0), ct(2026, 12, 28, 8, 45)]:
            feed._line(m, f"T|ES|{t}|6905.25|3|0\n".encode())
        m.flush()
        return stored(m.store.db), feed.stats.get("dropped", 0)
    ticks, dropped = asyncio.run(go())
    assert ticks == [ct(2026, 12, 24, 12, 10), ct(2026, 12, 24, 16, 0) - 1, ct(2026, 12, 28, 8, 45)] and dropped == 4


@weekend
def test_a_plugins_trade_stamped_on_good_friday_is_dropped(tmp_path, monkeypatch):
    cme_holidays()

    async def go():
        m, _ = build_market(tmp_path, monkeypatch)
        p = FeedPlugin.__new__(FeedPlugin)
        p.name, p.stats = "lines", {"ticks": 0, "dropped": 0, "last_msg_ms": None}
        m.trade_owner = {"ES": "lines"}
        return p.trade(m, "ES", ct(2027, 3, 26, 10, 15), 6905.25, 1), p.stats["dropped"]
    monkeypatch.setattr("time.time", lambda: ct(2027, 3, 26, 10, 16) / 1000)
    assert asyncio.run(go()) == (False, 1)


@weekend
def test_closed_holiday_prints_stored_before_are_taken_out_once_more(tmp_path, monkeypatch):
    """An install that took out its weekend prints already (7cd478d's marker set) takes out the closed days' too, once:
    the replays stored in Christmas's session (one moved on from Christmas Eve's break), the Thursday and Monday trades
    around them kept."""
    cme_holidays()
    keep = [ct(2026, 12, 24, 12, 10), ct(2026, 12, 24, 16, 0) - 1, ct(2026, 12, 28, 8, 45)]
    drop = [ct(2026, 12, 24, 17, 0), ct(2026, 12, 25, 10, 15), ct(2026, 12, 25, 15, 59)]

    async def go():
        m, _ = build_market(tmp_path, monkeypatch, feed="ninjatrader")
        put(m.store, "ES", keep + drop)
        m.store.set_setting("weekend_prints_dropped", True)
        await m.start([])
        await m.cleaning
        after = stored(m.store.db), [r[0] for r in m.store.db.execute("SELECT t FROM bars_1m WHERE symbol='ES' ORDER BY t")]
        put(m.store, "ES", drop)   # (as if stored again: a second start leaves them)
        await m._drop_weekend_prints()
        again = stored(m.store.db)
        await m.stop()
        return after, again
    (ticks, bars), again = asyncio.run(go())
    assert ticks == keep and bars == sorted({t // 60_000 * 60_000 for t in keep})
    assert again == sorted(keep + drop)


# --- k-review-5: the clean-up off the start, and not on the simulator ----------------------------------------------

@weekend
def test_the_simulators_weekend_trades_are_left_and_the_clean_up_marked_done(tmp_path, monkeypatch):
    """The simulator trades all weekend (5M trades a weekend): taking them out held the start for seconds a weekend,
    while leaving the simulator deletes them anyway."""
    async def go():
        m, _ = build_market(tmp_path, monkeypatch, feed="sim")
        put(m.store, "ES", [ct(2026, 10, 31, 10, 0), ct(2026, 11, 2, 9, 0)])
        await m._drop_weekend_prints()
        return stored(m.store.db), m.store.get_setting(market_mod.WEEKEND_DROPPED, False)
    ticks, done = asyncio.run(go())
    assert ticks == [ct(2026, 10, 31, 10, 0), ct(2026, 11, 2, 9, 0)] and done is True


@weekend
def test_the_clean_up_runs_on_a_worker_thread_after_the_start(tmp_path, monkeypatch):
    """The start returns at once: the clean-up waits for nothing on the event loop, and the marker is set when it is done."""
    gate, threads = threading.Event(), []
    real = sessions.weekends

    def gated(*a):
        threads.append(threading.current_thread() is threading.main_thread())
        gate.wait(3)
        return real(*a)
    monkeypatch.setattr(sessions, "weekends", gated)

    async def go():
        m, _ = build_market(tmp_path, monkeypatch, feed="ninjatrader")
        put(m.store, "ES", [ct(2026, 10, 30, 15, 0), ct(2026, 10, 31, 10, 0)])
        await m.start([])
        pending = not m.cleaning.done() and not m.store.get_setting(market_mod.WEEKEND_DROPPED, False)
        gate.set()
        await m.cleaning
        out = pending, stored(m.store.db), m.store.get_setting(market_mod.WEEKEND_DROPPED, False)
        await m.stop()
        return out
    pending, ticks, done = asyncio.run(go())
    assert pending and threads and not any(threads)
    assert ticks == [ct(2026, 10, 30, 15, 0)] and done is True


@weekend
def test_the_clean_up_deletes_a_slice_at_a_time_and_lets_the_server_write_between(tmp_path):
    """Each slice its own transaction on a connection of its own, one per step: the server's writer (which here may
    not wait at all) writes between any two, and the write-ahead log is folded back in as it goes (one transaction
    of a weekend's 5M simulated trades grew it by 200 MB)."""
    st = Store(tmp_path / "chart.db")
    sat = ct(2026, 10, 31, 0, 0)
    put(st, "ES", [ct(2026, 10, 30, 15, 0)] + [sat + i * 7 for i in range(2_500)])
    st.write([], [("NQ", Bar(sat, 1, 1, 1, 1, 1, 0, 0, 1))])
    st.db.execute("PRAGMA busy_timeout=0")
    steps, out = 0, None
    for out in store_mod.drop_weekend_prints(st.path, batch=1_000):
        steps += 1
        st.write([Tick("GC", ct(2026, 11, 2, 9, 0) + steps, 2000.0, 1, 1)], [])
    assert out == {"ES": 2_501, "NQ": 1} and steps == 4   # ES's 2,500 weekend trades in 3 slices, NQ's bar in 1
    assert stored(st.db) == [ct(2026, 10, 30, 15, 0)] and len(stored(st.db, "GC")) == 4
    st.close()


def test_the_database_watch_leaves_the_clean_ups_own_writes(tmp_path, monkeypatch):
    """The clean-up writes on a connection of its own: the watch for another program's writes (an import) starts after
    it, or it would restart the server a minute later. A write by another program after it still counts."""
    st = Store(tmp_path / "chart.db")
    restarts = []
    monkeypatch.setattr(app_mod, "DB_QUIET_S", 0.05)
    monkeypatch.setattr(app_mod, "restart_server", lambda: restarts.append(1))

    def other_program():
        db = sqlite3.connect(st.path)
        with db:
            db.execute("INSERT OR REPLACE INTO settings VALUES ('x', '1')")
        db.close()

    async def go():
        async def clean_up():
            await asyncio.sleep(0.05)
            await asyncio.to_thread(other_program)   # (as the clean-up's slices commit)
        task = asyncio.create_task(clean_up())
        watch = asyncio.create_task(app_mod.watch_db(st, every=0.01, after=task))
        await asyncio.sleep(0.3)
        first = list(restarts)
        await asyncio.to_thread(other_program)
        await asyncio.sleep(0.3)
        watch.cancel()
        return first, list(restarts)
    first, later = asyncio.run(go())
    assert first == [] and later == [1]
    st.close()


# --- r14-sessions-2: an RTH chart's bar-close alerts on a holiday's early close or halt -----------------------------

def _series(spec: str, end: int, rth_only: bool = True):
    s = make_series(parse_spec(spec), 0.25, 2, 3000, ("08:30", "15:00"), studies=False, rth_only=rth_only, group="equity")
    t = ct(2026, 11, 23, 17, 0)
    while t < end:
        if sessions.is_open(t, "equity"):
            s.on_bar(Bar(t, 7000, 7001, 6999, 7000, 1))
        t += 60_000
    return s


def test_an_rth_charts_last_bar_closes_with_a_holidays_early_close_or_halt():
    """Black Friday closes at 12:15, Thanksgiving halts at 12:00: an RTH chart's last bar has closed then, as the ETH
    chart's has (before: only at the RTH close, 15:00, so its bar-close alerts came up to 3 hours late)."""
    cme_holidays()
    for spec in ("5m", "30m", "1D", "1W"):   # (2D: Friday's bar goes on into Monday)
        s = _series(spec, ct(2026, 11, 27, 12, 15))
        assert not ia._ended(s, ct(2026, 11, 27, 12, 14)), spec
        assert ia._ended(s, ct(2026, 11, 27, 12, 16)) and ia._ended(s, ct(2026, 11, 27, 14, 59)), spec
        assert ia._ended(_series(spec, ct(2026, 11, 27, 12, 15), rth_only=False), ct(2026, 11, 27, 12, 16)), spec
    s = _series("5m", ct(2026, 11, 26, 12, 0))
    assert ia._ended(s, ct(2026, 11, 26, 12, 1)) and not ia._ended(s, ct(2026, 11, 26, 11, 59))
    s = _series("5m", ct(2026, 11, 25, 14, 0))   # an ordinary day: as ever
    assert not ia._ended(s, ct(2026, 11, 25, 14, 59)) and ia._ended(s, ct(2026, 11, 25, 15, 1))


def test_an_rth_bar_close_alert_fires_at_black_fridays_early_close(tmp_path, monkeypatch):
    """End to end: the watchdog's pass while the market is shut (alerts.on_shut) fires the RTH 5m chart's alert on the
    12:10 bar at 12:16, with the ETH chart's (before: at 15:01)."""
    cme_holidays()
    m, _ = build_market(tmp_path, monkeypatch)
    inst = m.instruments["ES"]
    for rth in (True, False):
        w = m.ind_alerts.watches[("ES", "5m", rth)] = ia._Watch(make_series(
            parse_spec("5m"), inst.tick_size, inst.decimals, 3000, inst.rth, studies=False, rth_only=rth, group=inst.calendar))
        w.ready.set()
        w.version = 1
    ema = {"ind": "ema", "line": "v", "len": 3}
    ids = [m.store.add_alert("ES", "ind", ia.clean_params({"ind": "price"}, ema, "5m", "close", "above", rth), True, "")
           for rth in (True, False)]

    async def go():
        monkeypatch.setattr(market_mod, "now_ms", lambda: ct(2026, 11, 27, 8, 0))
        m.alerts_changed()
        t, px = ct(2026, 11, 25, 17, 0), 7500.0
        while t < ct(2026, 11, 27, 12, 10):   # falling through Wednesday night, Thanksgiving's session and Friday morning
            if sessions.is_open(t, inst.calendar):
                m.on_tick(Tick("ES", t, px, 1, 1))
                px -= 0.25
            t += 60_000
        for k in range(5):   # 12:10-12:14, far above: the last bar before the 12:15 close
            m.on_tick(Tick("ES", ct(2026, 11, 27, 12, 10, 30) + k * 60_000, 7600.0, 1, 1))
        seen = len(m.store.list_events(1000))
        now = ct(2026, 11, 27, 12, 16, 30)
        monkeypatch.setattr(market_mod, "now_ms", lambda: now)
        await m._watchdog_pass()
        events = m.store.list_events(1000)   # newest first
        return sorted(e["alert_id"] for e in events[: len(events) - seen])
    assert asyncio.run(go()) == sorted(ids)


# --- r14-sessions-3: a D chart's footprint around a holiday halt ----------------------------------------------------

def _trades(until: int) -> list[Tick]:
    out, t, k = [], ct(2026, 11, 23, 17, 0), 0
    while t < until:
        if sessions.is_open(t, "equity"):
            out.append(Tick("ES", t, 6800.0 + (k % 40) * 0.25, 1 + k % 3, 1 if k % 2 else -1))
            k += 1
        t += 15 * 60_000
    return out


def _bars(ticks: list[Tick], rth: bool, spec: str = "1D") -> list[Bar]:
    s = make_series(parse_spec(spec), 0.25, 2, 100, ("08:30", "15:00"), studies=False, rth_only=rth, group="equity")
    for tk in ticks:
        s.on_tick(Tick(tk.symbol, tk.ts, tk.price, tk.size, tk.side))
    return list(s.bars)


def test_a_d_charts_footprint_holds_a_holiday_halts_session_in_its_bar(app_client):
    """Thanksgiving's session (Wed 17:00 to its 12:00 halt) is in the 1D bar stamped Thursday 17:00, Friday's trade
    date (on the RTH chart, Thursday's RTH in Friday's 08:30 bar), as the chart's bars have it. Before, the footprint
    split trades by the bars' open times: Wednesday's bar took Thanksgiving's trades and the folded bar lacked them.
    Each bar's footprint now adds up to its volume, also with the folded bar first in the request or still forming."""
    cme_holidays()
    for now in (ct(2026, 11, 25, 20, 0), ct(2026, 11, 26, 10, 0), ct(2026, 11, 27, 13, 0)):
        ticks = _trades(now)
        with app_client(ticks) as c:
            for rth in (False, True):
                bars = _bars(ticks, rth)[-3:]
                if not rth:
                    assert sessions.to_ct(bars[-1].t).strftime("%a %H:%M") == "Thu 17:00"   # Friday's trade date
                end = max(now + 3_600_000, bars[-1].t + 1)   # as the chart asks for its forming bar
                for first in (0, len(bars) - 1):
                    body = {"symbol": "ES", "starts": [b.t for b in bars[first:]], "end": end, "rth": rth, "spec": "1D"}
                    r = c.post("/api/footprint", json=body)
                    assert r.status_code == 200, (now, rth, r.text)
                    got = [sum(x[1] + x[2] for x in b["rows"]) for b in r.json()["bars"]]
                    assert got == [b.v for b in bars[first:]], (sessions.to_ct(now), rth, first)


@pytest.mark.skipif(not shutil.which("node"), reason="needs node")
def test_the_chart_asks_for_a_forming_halt_day_bar_past_its_stamp():
    """The 1D bar forming in Thanksgiving's session is stamped Thursday 17:00, a day later: the request's end came
    before it (now + 1 h), and the server refused the whole request (400), every bar in view drawn as a candle."""
    now, wed, thu = ct(2026, 11, 25, 20, 0), ct(2026, 11, 24, 17, 0), ct(2026, 11, 26, 17, 0)
    out = run_app(["fpBusy", "fpFetch"], FP_JS.replace("spec: '5m'", "spec: '1D'").replace("rth: true", "rth: false") + f"""
Date.now = () => {now};
S.bars = [{{ t: {wed - 86_400_000}, seq: 1, v: 1 }}, {{ t: {wed}, seq: 2, v: 1 }}, {{ t: {thu}, seq: 3, v: 1 }}];
range = {{ from: 0, to: 2 }};
(async () => {{ await fpFetch(); console.log(JSON.stringify(bodies[0])); }})();""")
    assert out["starts"][-1] == thu and out["end"] > thu and out["spec"] == "1D"


# --- r14-sessions-4: the Yahoo-filled older days of a D or W chart fold a holiday halt as the app's own bars do -------

def test_yahoos_older_days_fold_a_holiday_halts_session_into_the_next_day(tmp_path, monkeypatch):
    """A future with under a year stored gets its older days from Yahoo's daily rows: Thanksgiving 2025, MLK Day and
    July 3 2026 (halts) were bars of their own there, and the 1W chart put July 3 in the week of June 29, while the
    bars this app makes (and DATA.md) have them in the next trade date's bar."""
    cme_holidays()
    now = ct(2026, 11, 2, 10, 0)
    monkeypatch.setattr(market_mod, "now_ms", lambda: now)
    hol, rows, d = sessions.holidays(), [], date(2025, 11, 20)
    while d < date(2026, 10, 20):   # Yahoo's rows, stamped at day_open(date): every weekday not closed
        if d.weekday() < 5 and not (hol.get(d) or {}).get("closed"):
            rows.append({"t": sessions.day_open(d), "o": 6000.0, "h": 6010.0, "l": 5990.0, "c": 6000.0 + d.toordinal() % 50, "v": 10})
        d += timedelta(days=1)

    async def go():
        m, _ = build_market(tmp_path, monkeypatch)

        async def yahoo(ticker, spec):
            return [dict(r) for r in rows], {}
        monkeypatch.setattr(m.ext, "_yahoo", yahoo)
        inst, out = m.instruments["ES"], {}
        for text in ("1D", "1W"):
            spec = parse_spec(text)
            sr = make_series(spec, inst.tick_size, inst.decimals, 1000, inst.rth, studies=False, group=inst.calendar)
            sr._bar(Bar(sessions.day_open(date(2026, 10, 21)) + 60_000, 6800, 6800, 6800, 6800, 1))   # stored from Oct 21
            await m._older_days("ES", sr, spec)
            out[text] = {sessions.to_ct(b.t).date(): b for b in sr.bars}
        return out
    out = asyncio.run(go())
    days = out["1D"]
    for halt in (date(2025, 11, 26), date(2026, 1, 18), date(2026, 7, 2)):   # the 17:00 opens of the halt days' sessions
        assert halt not in days, halt
    assert days[date(2025, 11, 27)].c == 6000.0 + date(2025, 11, 28).toordinal() % 50   # Thanksgiving's in Black Friday's
    week = out["1W"]
    assert week[date(2026, 6, 28)].c == 6000.0 + date(2026, 7, 2).toordinal() % 50     # the week of June 29 ends Jul 2
    assert (week[date(2026, 6, 28)].v, week[date(2026, 7, 5)].v) == (40, 60)            # and Jul 3 is the next week's


def test_a_real_good_friday_session_is_kept_by_the_clean_up_and_a_replay_on_christmas_is_not(tmp_path):
    """CME's calendar here closes every Good Friday, but ES and NQ traded a short session on 2023-04-07 (the jobs
    report). Its stored minutes, at many prices, are kept; a replay on Christmas Day (one price) is taken out."""
    cme_holidays()
    st = Store(tmp_path / "chart.db")
    gf = [ct(2023, 4, 6, 17, 0) + i * 60_000 for i in range(900)]   # Thu 17:00 to Fri 08:00, a real session
    st.write([Tick("ES", t, 4100 + (i % 40) * 0.25, 1, 0) for i, t in enumerate(gf)],
             [("ES", Bar(t, *[4100 + (i % 40) * 0.25] * 4, 1, 0, 0, 1)) for i, t in enumerate(gf)])
    put(st, "ES", [ct(2023, 12, 25, 10, 15), ct(2023, 12, 25, 11, 40)])   # two replays, one price
    for _ in store_mod.drop_weekend_prints(st.path):
        pass
    left = stored(st.db)
    assert len(left) == 900 and left[0] == gf[0] and ct(2023, 12, 25, 10, 15) not in left
    st.close()
