"""Tatanka studies in bar replay: TPO, Gap Analysis, the session, anchored and periodic volume profiles and the
right-side profile, built as they stood at the replay clock (`until`). Each must be what the live chart showed at that
moment: the same study built live on a database that ends there, with the clock there."""
import asyncio
import random
import time
from datetime import datetime

import pytest
from test_amt import client  # noqa: F401  (the app on the simulator, as a fixture)
from test_review_fixes import build_market

from chartsrv import gaps, tpo
from chartsrv.gaps_service import own_hours
from chartsrv.models import Bar, Tick
from chartsrv.sessions import CT

MIN = 60_000


def ct(d, h, mi=0):
    return int(datetime(2026, 10, d, h, mi, tzinfo=CT).timestamp() * 1000)


NOW = ct(17, 12)                     # a Saturday: the week is over
T1, T2 = ct(15, 10), ct(15, 11, 30)  # Thursday morning, then later that morning
T0 = ct(14, 13)                      # Wednesday afternoon: replay jumped back


def week():
    """Sunday 17:00 to Friday 16:00 CT, the daily halts out: a random walk of minutes, four trades each (open, high,
    low, close), but every 37th minute has its bar only (a gap-filled one)."""
    rnd = random.Random(7)
    ticks, bars = [], []
    px = 6800.0
    t = ct(11, 17)
    while t < ct(16, 16):
        hour = datetime.fromtimestamp(t / 1000, CT).hour
        if hour != 16:
            o = px
            c = round((px + rnd.choice((-1.5, -0.75, -0.25, 0, 0.25, 0.75, 1.5))) * 4) / 4
            h = max(o, c) + rnd.choice((0, 0.25, 0.5))
            lo = min(o, c) - rnd.choice((0, 0.25, 0.5))
            sizes = [rnd.randint(1, 30) for _ in range(4)]
            bars.append(("ES", Bar(t=t, o=o, h=h, l=lo, c=c, v=sum(sizes))))
            if (t // MIN) % 37:
                for k, (p, sz) in enumerate(zip((o, h, lo, c), sizes)):
                    ticks.append(Tick("ES", t + 5_000 + k * 15_000, p, sz, 1 if k % 2 else -1))
            px = c
        t += MIN
    return ticks, bars


def write(m, ticks, bars, before=None):
    if before is not None:
        ticks = [x for x in ticks if x.ts < before]
        bars = [x for x in bars if x[1].t < before]
    m.store.write(ticks, bars)


@pytest.fixture
def clock(monkeypatch):
    now = {"t": NOW}
    monkeypatch.setattr(time, "time", lambda: now["t"] / 1000)
    monkeypatch.setenv("CHART_WORKERS", "0")
    return now


def markets(tmp_path, monkeypatch, cut):
    """The replaying server (the whole week stored) and the one that was live at `cut` (stored to there)."""
    data = week()
    (tmp_path / "a").mkdir(exist_ok=True)
    (tmp_path / "b").mkdir(exist_ok=True)
    replaying, _ = build_market(tmp_path / "a", monkeypatch)
    write(replaying, *data)
    live, _ = build_market(tmp_path / "b", monkeypatch)
    write(live, *data, before=cut)
    return replaying, live


def same(a: dict, b: dict) -> bool:
    drop = ("key",)   # a build's name: the replay's are its own
    return {k: v for k, v in a.items() if k not in drop} == {k: v for k, v in b.items() if k not in drop}


@pytest.mark.parametrize("cut", [T1, T0])
def test_tpo_and_gaps_in_replay_are_what_the_live_chart_showed_then(tmp_path, monkeypatch, clock, cut):
    async def go():
        replaying, live = markets(tmp_path, monkeypatch, cut)
        s = tpo.settings({})
        g = own_hours(gaps.settings({}), replaying.instruments["ES"].rth)
        got_tpo = await replaying.tpo.get("ES", 30, s, False, cut)
        got_gaps = await replaying.gaps.get("ES", 30, g, 90, False, cut)
        clock["t"] = cut
        want_tpo = await live.tpo.get("ES", 30, s, False)
        want_gaps = await live.gaps.get("ES", 30, g, 90, False)
        assert got_tpo["profiles"] and got_tpo == want_tpo
        assert got_gaps["gaps"] and got_gaps == want_gaps
        clock["t"] = NOW
        assert (await replaying.tpo.get("ES", 30, s))["through"] > got_tpo["through"]   # live: the whole week
    asyncio.run(go())


@pytest.mark.parametrize("kind", ["prof", "aavp", "pvp"])
def test_volume_profiles_in_replay_play_forward_and_jump_back(tmp_path, monkeypatch, clock, kind):
    """One replay build, carried from T1 to T2 as it plays, then built again when it jumps back to T0: at each, what
    a server live then showed."""
    async def go():
        data = week()
        replaying, _ = build_market(tmp_path, monkeypatch)
        write(replaying, *data)
        got = []
        for cut in (T1, T2, T0):
            clock["t"] = NOW
            got.append(await replaying.vp.get(kind, "ES", until=cut))
        for i, cut in enumerate((T1, T2, T0)):
            (tmp_path / str(i)).mkdir()
            clock["t"] = NOW
            live, _ = build_market(tmp_path / str(i), monkeypatch)
            write(live, *data, before=cut)
            clock["t"] = cut
            want = await live.vp.get(kind, "ES")
            assert same(got[i], want), (kind, i)
        if kind == "pvp":   # went back: a new build (its finished profiles may start elsewhere)
            assert got[1]["profiles"] and got[0]["key"] == got[1]["key"] != got[2]["key"]
        elif kind == "prof":   # finished sessions are the same in replay and live: the build the page holds is too
            assert got[1]["sessions"] and got[0]["key"] == got[1]["key"] == got[2]["key"]
        else:
            assert got[1]["profile"]["rows"] != got[0]["profile"]["rows"]
        clock["t"] = NOW
        live_now = await replaying.vp.get(kind, "ES")
        assert not same(live_now, got[1])   # the live chart's own build: the whole week
    asyncio.run(go())


def test_the_right_side_profile_counts_nothing_after_the_replay_clock(clock, client):  # noqa: F811
    """A composite of the last sessions and a visible range reaching past the replay clock: nothing traded after it.
    A clock at or past the present is the live chart's."""
    now = int(time.time() * 1000)
    for q in ("sessions=5", f"start={now - 3 * 3_600_000}&end={now + 60_000}"):
        live = client.get(f"/api/profile?symbol=ES&{q}").json()
        cut = client.get(f"/api/profile?symbol=ES&{q}&until={now - 3_600_000}").json()
        later = client.get(f"/api/profile?symbol=ES&{q}&until={now + 3_600_000}").json()
        assert later == live and cut != live, q
    for path in ("/api/tpo?symbol=ES&spec=30m", "/api/gaps?symbol=ES&spec=30m", "/api/vp/prof?symbol=ES"):
        assert client.get(f"{path}&until={now - 3_600_000}").status_code == 200, path



class FakeClient:
    """A page's connection as the AMT hub sees it: what it is sent."""

    def __init__(self):
        self.amt_entries, self.amt_state, self.amt_asked, self.subs, self.loading = {}, {}, {}, {}, set()
        self.closed = False
        self.sent = []

    def put(self, msg):
        self.sent.append(msg)


def amt_frame(runner) -> dict:
    runner.commit_ready()
    runner.preview()
    return runner.payload(True, True, True, True)


def test_the_amt_toolkit_in_replay_plays_forward_and_jumps_back(tmp_path, monkeypatch, clock):
    """A replaying chart's runner, carried from T1 to T2 and loaded again when it jumps back to T0: at each, what the
    Toolkit showed live then. It never alerts, and the live runners are left alone."""
    async def go():
        data = week()
        replaying, _ = build_market(tmp_path, monkeypatch)
        write(replaying, *data)
        asked = []   # the prior days from minutes in both here (the trades' own test: below)
        monkeypatch.setattr(type(replaying.amt), "_want_pd", lambda self, sym, loaded=None: asked.append(sym))
        c = FakeClient()
        got = []
        for cut in (T1, T2, T0):
            clock["t"] = NOW
            await replaying.amt.subscribe(c, 7, "ES", 5, None, cut)
            got.append(c.sent[-1])
        assert [m["full"] for m in got] == [True, False, True] and all(m["data"]["ready"] for m in got)
        assert not replaying.amt.entries and (c, 7) in replaying.amt.replays
        for i, cut in enumerate((T1, T2, T0)):
            (tmp_path / str(i)).mkdir()
            live, _ = build_market(tmp_path / str(i), monkeypatch)
            write(live, *data, before=cut)
            clock["t"] = cut
            want = amt_frame((await live.amt.entry("ES", 5)).runner)
            assert want["lines"] and want["dash"]["values"]
            have = replaying.amt.replays[(c, 7)].runner if i == 2 else None
            data_i = got[i]["data"]
            for k in ("bar_start", "bar_end", "lines", "zones", "dash"):
                if k in data_i:
                    assert data_i[k] == want[k], (i, k)
            assert data_i["bar_end"] == cut, i
            if have is not None:
                assert amt_frame(have)["lines"] == want["lines"]
        replaying.amt.unsubscribe(c, 7)
        assert not replaying.amt.replays
        assert asked and set(asked) == {"ES"}   # it asked for the prior days' trades, as a live chart does
    asyncio.run(go())


def test_daily_levels_in_replay_are_that_days(tmp_path, monkeypatch, clock):
    """A pasted line (or a levels file or link) holds one day: the app keeps each day's as they come (with when each
    version was first seen), so a replay of that day has them after the line has moved on. A day none were kept for
    has none."""
    m, _ = build_market(tmp_path, monkeypatch)

    def paste(text, at):
        clock["t"] = at
        m.daily.set_paste("ES", text)
        m.levels_changed()   # as PUT /api/amt/levels does
    paste("2026-10-15, 6820.00, 6850.00, 6875.00, 6800.00, 6775.00", ct(15, 7))
    assert m.levels.as_of("ES", T1).pivot == 6820.0                          # Thursday the 15th: that day's
    assert m.levels.as_of("ES", T0) is None and m.daily.as_of("ES", T0)[0] == []   # Wednesday: none kept
    paste("2026-10-15, 6822.00, 6850.00, 6875.00, 6800.00, 6775.00", ct(15, 10, 45))   # changed that morning
    paste("2026-10-16, 6900.00, 6925.00, 6875.00", ct(16, 7))                 # Friday's: Thursday's are gone from it
    assert m.levels.as_of("ES", T1).pivot == 6820.0                          # 10:00: the version seen by then
    assert m.levels.as_of("ES", T2).pivot == 6822.0                          # 11:30: the newer one
    assert m.levels.as_of("ES", ct(15, 6)) is None                           # before any was seen: none (as then)
    assert m.levels.as_of("ES", ct(15, 20)).pivot == 6822.0                  # Friday's overnight: Thursday's, until
    assert m.levels.as_of("ES", ct(16, 9)).pivot == 6900.0                   # Friday's came at 07:00
    levels, date, src = m.daily.as_of("ES", T2)
    assert src.origin == "paste" and date.day == 15 and [x.Kind for x in levels] == [0, -1, -1, 1, 1]
    paste("2026-10-16, 6900.00, 6925.00, 6875.00", ct(16, 8))                 # the same again: not kept twice
    assert m.store.db.execute("SELECT COUNT(*) FROM levels_kept WHERE symbol='ES'").fetchone()[0] == 3
    paste("", ct(16, 12))                                                    # cleared at noon: none from then on
    assert m.levels.as_of("ES", ct(16, 11)).pivot == 6900.0 and m.levels.as_of("ES", ct(16, 13)) is None
    assert m.levels.as_of("ES", ct(20, 9)) is None                           # days later: none kept for it


