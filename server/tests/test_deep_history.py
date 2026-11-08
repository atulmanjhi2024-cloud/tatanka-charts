"""Older history from NinjaTrader (gapfill's older requests, add-on 20): from before anything stored, a month at a time,
in the background back to a year once a symbol's gap is filled, and at once for a chart scrolled back past its stored
minutes; one request at a time; an empty month is where NinjaTrader's history ends; charts aren't reloaded for it, the
studies once the year is in. The add-on asks each part of it from its contract by NinjaTrader's roll dates."""
import re
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from chartsrv import sessions
from chartsrv.feeds.ninjatrader import NinjaTraderFeed
from chartsrv.gapfill import DAY_MS, OLDER_LIMIT, OLDER_SPAN_MS
from chartsrv.models import Bar
from test_app import client  # noqa: F401  (the app on a database of the test's own)
from test_hunt10_hold import Writer
from test_review_fixes import build_market

MIN = 60_000
HARNESS = Path(__file__).resolve().parent / "addon_csharp"
ROOT = Path(__file__).resolve().parents[2]


def open_minutes(start: int, n: int) -> list[int]:
    """The first n minutes from `start` the market is open."""
    out, t = [], start // MIN * MIN
    while len(out) < n:
        if sessions.is_open(t, "equity"):
            out.append(t)
        t += MIN
    return out


def store(m, sym, ts):
    m.store.write([], [(sym, Bar(t=t, o=6000.0, h=6001.0, l=5999.0, c=6000.25, v=10)) for t in ts])


def connect(m, version=20):
    """The add-on says hello (what NinjaTraderFeed._handle does): every symbol's gap asked for, then answered empty."""
    nt = NinjaTraderFeed("127.0.0.1", 0, "t")
    nt.writer, nt.stats["version"], nt.stats["connected"] = Writer(), version, True
    m.feed_objs.append(nt)
    gaps = m.gapfill.requests(int(time.time() * 1000), live=True, minutes=True, contracts=True, older=nt.older_ok())
    nt.writer.lines.extend(gaps)
    for line in gaps:   # nothing new from NinjaTrader: each gap done
        nt._line(m, f"Z|{line.split('|')[1]}|minutes|0|ok\n".encode())
    return nt


def older_lines(nt):
    return [ln for ln in nt.writer.lines if ln.endswith("|older")]


def answer(m, nt, line, minutes):
    """The add-on's answer to an older request: those minutes (Y lines), then Z."""
    sym = line.split("|")[1]
    for t in minutes:
        nt._line(m, f"Y|{sym}|{t}|6100|6101|6099|6100.5|7\n".encode())
    nt._line(m, f"Z|{sym}|minutes|{len(minutes)}|ok\n".encode())


@pytest.fixture
def market(tmp_path, monkeypatch):
    m, _ = build_market(tmp_path, monkeypatch)
    calls = []
    real = m.history_changed
    m.history_changed = lambda sym, charts=True: (calls.append((sym, charts)), real(sym, charts))
    m.reloads = calls
    return m


def test_the_year_fills_in_month_by_month_until_ninjatraders_history_ends(market):
    m = market
    now = int(time.time() * 1000)
    floor = open_minutes(now - 20 * DAY_MS, 1)[0]
    store(m, "ES", [floor, now // MIN * MIN - 10 * MIN])
    nt = connect(m)
    asked = older_lines(nt)
    assert asked == [f"GAP|ES|{floor - OLDER_SPAN_MS}|{floor}|older"]   # ES only (nothing else stored), once its gap is done
    got = open_minutes(floor - 25 * DAY_MS, 3)                          # NinjaTrader's month: 3 minutes of it, say
    answer(m, nt, asked[0], got)
    rows = m.store.db.execute("SELECT t, v FROM bars_1m WHERE symbol='ES' AND t<? ORDER BY t", (floor,)).fetchall()
    assert [tuple(r) for r in rows] == [(t, 7) for t in got]
    assert m.reloads == []   # charts not reloaded
    asked = older_lines(nt)
    assert asked[-1] == f"GAP|ES|{got[0] - OLDER_SPAN_MS}|{got[0]}|older"     # on back from the new first minute
    answer(m, nt, asked[-1], [])                                             # an empty month: NinjaTrader's history ends
    assert m.store.get_setting(OLDER_LIMIT)["ES"][0] == got[0]
    assert m.gapfill.status["ES"]["state"] == "limit" and len(older_lines(nt)) == 2
    assert m.gapfill.want_older("ES", 0) == "limit"
    assert m.reloads == [("ES", False)]   # its studies once, the year's months in; no chart


def test_a_chart_scrolled_back_goes_first_with_the_span_it_wants(market, monkeypatch):
    monkeypatch.setenv("CHART_HISTORY_PREFILL_DAYS", "0")   # nothing in the background
    m = market
    now = int(time.time() * 1000)
    es, nq = open_minutes(now - 5 * DAY_MS, 1)[0], open_minutes(now - 6 * DAY_MS, 1)[0]
    store(m, "ES", [es])
    store(m, "NQ", [nq])
    nt = connect(m)
    assert older_lines(nt) == []
    assert m.gapfill.want_older("NQ", 90 * DAY_MS) == "fetching"
    assert older_lines(nt) == [f"GAP|NQ|{nq - 90 * DAY_MS}|{nq}|older"]   # sent at once
    assert m.gapfill.want_older("ES", 0) == "fetching"                    # waits for NQ's
    assert m.gapfill.want_older("NQ", 10 * DAY_MS) == "fetching" and len(older_lines(nt)) == 1
    answer(m, nt, older_lines(nt)[0], open_minutes(nq - 40 * DAY_MS, 2))
    assert older_lines(nt)[1] == f"GAP|ES|{es - OLDER_SPAN_MS}|{es}|older"
    answer(m, nt, older_lines(nt)[1], open_minutes(es - 10 * DAY_MS, 2))
    first = m.gapfill.floor("NQ")
    assert older_lines(nt)[2] == f"GAP|NQ|{first - OLDER_SPAN_MS}|{first}|older"   # NQ's second ask, after
    assert m.reloads == [("ES", False)]   # ES's studies, nothing more asked for it; not NQ's: it is still being asked
    answer(m, nt, older_lines(nt)[2], open_minutes(first - 10 * DAY_MS, 2))
    assert m.reloads == [("ES", False), ("NQ", False)]
    m.gapfill.want_older("ES", 0)
    answer(m, nt, older_lines(nt)[3], open_minutes(m.gapfill.floor("ES") - 10 * DAY_MS, 2))
    assert m.reloads == [("ES", False), ("NQ", False)]   # not again within 5 minutes...
    m.gapfill.older_reloaded["ES"] -= 301
    m.gapfill.kick_older()
    assert m.reloads[-1] == ("ES", False)                # ...but after (the watchdog's pass)


def test_an_unanswered_request_is_dropped_and_asked_again_later(market):
    m = market
    now = int(time.time() * 1000)
    store(m, "ES", [open_minutes(now - 5 * DAY_MS, 1)[0]])
    nt = connect(m)
    assert len(older_lines(nt)) == 1
    gf = m.gapfill
    gf.older_busy = ("ES", time.monotonic() - 1000)   # asked long ago, never answered
    gf.kick_older()
    assert "ES" not in gf.windows and gf.status["ES"]["state"] == "dropped" and len(older_lines(nt)) == 1
    assert gf.want_older("ES", 0) == "wait"
    nt._line(m, b"Z|ES|minutes|5|ok\n")   # a late answer: no window, nothing kept
    assert m.store.db.execute("SELECT COUNT(*) FROM bars_1m WHERE symbol='ES'").fetchone()[0] == 1


def test_an_error_is_not_ninjatraders_limit_unless_it_keeps_failing(market):
    m = market
    now = int(time.time() * 1000)
    floor = open_minutes(now - 5 * DAY_MS, 1)[0]
    store(m, "ES", [floor])
    nt = connect(m)
    nt._line(m, b"Z|ES|error|0|no data connection\n")
    gf = m.gapfill
    assert m.store.get_setting(OLDER_LIMIT) is None and gf.want_older("ES", 0) == "wait"
    gf.older_wait.clear()
    gf.kick_older()
    nt._line(m, b"Z|ES|error|0|no data connection\n")   # twice
    gf.older_wait.clear()
    gf.kick_older()
    gf.older_busy = ("ES", time.monotonic() - 1000)        # and no answer: the third in a row
    gf.kick_older()
    assert m.store.get_setting(OLDER_LIMIT)["ES"][0] == floor and gf.want_older("ES", 0) == "limit"
    assert len(older_lines(nt)) == 3


def test_an_older_add_on_is_never_asked(market):
    m = market
    now = int(time.time() * 1000)
    store(m, "ES", [open_minutes(now - 5 * DAY_MS, 1)[0]])
    nt = connect(m, version=19)
    assert older_lines(nt) == [] and m.gapfill.want_older("ES", 0) == "off"


def test_the_api_says_fetching_and_asks_ninjatrader(client, tmp_path):
    from chartsrv.app import M
    from chartsrv.store import Store
    m = M()
    first = open_minutes(int(time.time() * 1000) - 40 * DAY_MS, 1)[0]
    Store(tmp_path / "chart.db").write([], [("ES", Bar(t=first, o=6000.0, h=6001.0, l=5999.0, c=6000.25, v=10))])
    nt = NinjaTraderFeed("127.0.0.1", 0, "t")
    nt.writer, nt.stats["version"], nt.stats["connected"] = Writer(), 20, True
    m.feed_objs.append(nt)
    m.gapfill.older_ok = True
    d = client.get(f"/api/older?symbol=ES&spec=30m&before={first}&count=100").json()
    assert d["bars"] == [] and d["more"] and d["fetching"]   # 100 30m bars: under a month, so a month
    assert older_lines(nt) == [f"GAP|ES|{first - OLDER_SPAN_MS}|{first}|older"]
    d = client.get(f"/api/older?symbol=ES&spec=500t&before={first}&count=100").json()   # tick bars: trades only
    assert not d["more"] and d["why"] == "stored"
    m.gapfill.older_ok = False   # no add-on that answers
    d = client.get(f"/api/older?symbol=ES&spec=30m&before={first}&count=100").json()
    assert not d["more"] and d["why"] == "stored"


@pytest.mark.skipif(not (shutil.which("mcs") and shutil.which("mono")), reason="needs mono")
def test_the_add_on_asks_each_older_month_from_its_contract(tmp_path):
    exe = tmp_path / "segs.exe"
    r = subprocess.run(["mcs", "-langversion:Latest", "-r:System.Core.dll", f"-out:{exe}", str(HARNESS / "Stubs.cs"),
                        str(ROOT / "ninjatrader" / "TatankaChartFeed.cs"), str(HARNESS / "SegmentsHarness.cs")],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
    out = {line.split(" ", 1)[0]: line for line in
           subprocess.run(["mono", str(exe)], capture_output=True, text=True, timeout=60).stdout.splitlines()}
    names = lambda s: re.findall(r"(ES \d\d-\d\d)@", s)   # noqa: E731
    assert names(out["gap"]) == ["ES 09-26", "ES 12-26"]                 # a gap: never older than the one charted last
    assert names(out["older"]) == ["ES 06-26", "ES 09-26", "ES 12-26"]   # older: each by the roll dates, none before the first
    assert names(out["before"]) == []                                         # all before the first roll date it knows: none
