"""Exchange holidays: the calendar file, market hours around a halt, an early close and a closed day, holiday
sessions folded into the next trade date's daily bar, and a watchdog that stays quiet while CME is shut."""
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from chartsrv import market as market_mod
from chartsrv import sessions
from chartsrv.config import load_holidays
from chartsrv.models import Bar, Tick
from chartsrv.sessions import is_open, trade_key
from chartsrv.store import Store
from chartsrv.tpo_service import Daily, TpoService

from test_review_fixes import build_market, ms

CONFIG = Path(__file__).resolve().parents[2] / "config"


@pytest.fixture
def cal():
    sessions.set_calendar(load_holidays(CONFIG))


def test_calendar_file_loads_and_bad_entries_are_refused():
    days = load_holidays(CONFIG)["days"]
    assert len(days) == len({d["date"] for d in days}) >= 20
    sessions.set_calendar(load_holidays(CONFIG))
    assert sessions.holidays()[ms_date(2026, 11, 26)]["name"] == "Thanksgiving"
    for bad, msg in [({"date": "2026-11-26", "halt": "12:00", "close": "12:15"}, "one of"),
                     ({"date": "2026-11-26"}, "one of"), ({"date": "2026-11-26", "halt": "25:00"}, "bad time")]:
        with pytest.raises(ValueError, match=msg):
            sessions.set_calendar({"days": [bad]})
    from datetime import date
    none = load_holidays(Path("/nonexistent"), today=date(2026, 6, 1))["days"]   # no file: the usual pattern, 2000-2028
    assert {d["date"][:4] for d in none} == {str(y) for y in range(2000, 2029)}
    assert not [d for d in none if d["name"].startswith("Juneteenth") and d["date"] < "2022"]   # CME's first: 2022
    sessions.set_calendar({"days": none})
    assert sessions.is_open(ms(2026, 11, 27, 13, 30), "energy") and not sessions.is_open(ms(2026, 11, 27, 13, 30), "equity")
    assert not sessions.is_open(ms(2026, 12, 24, 13, 0), "metals") and sessions.is_open(ms(2026, 1, 19, 13, 0), "energy")


def ms_date(y, mo, d):
    return sessions.to_ct(ms(y, mo, d, 12)).date()


def test_hours_around_thanksgiving_and_christmas(cal):
    eq = "equity"
    assert is_open(ms(2026, 11, 26, 11, 59), eq) and not is_open(ms(2026, 11, 26, 12, 0), eq)     # Thanksgiving halt
    assert not is_open(ms(2026, 11, 26, 16, 59), eq) and is_open(ms(2026, 11, 26, 17, 0), eq)     # reopens 17:00
    assert is_open(ms(2026, 11, 27, 12, 14), eq) and not is_open(ms(2026, 11, 27, 12, 15), eq)    # early close
    assert is_open(ms(2026, 12, 24, 12, 14), eq) and not is_open(ms(2026, 12, 24, 12, 15), eq)    # Christmas Eve
    assert not is_open(ms(2026, 12, 24, 17, 30), eq) and not is_open(ms(2026, 12, 25, 10), eq)    # Christmas: shut
    assert is_open(ms(2026, 12, 27, 17, 0), eq)                                                    # Sunday open
    assert is_open(ms(2026, 11, 26, 13), None)                       # without a group: weekly hours only
    assert is_open(ms(2026, 10, 14, 10), eq)                         # an ordinary day


def test_group_specific_times():
    sessions.set_calendar({"days": [{"date": "2026-07-03", "halt": {"default": "12:00", "energy": "12:30"}}]})
    assert not is_open(ms(2026, 7, 3, 12, 15), "equity") and is_open(ms(2026, 7, 3, 12, 15), "energy")
    assert not is_open(ms(2026, 7, 3, 12, 30), "energy")


def test_trade_key_folds_a_halt_session_into_the_next_trade_date(cal):
    sun, mon, tue = ms(2027, 1, 17, 17), ms(2027, 1, 18, 17), ms(2027, 1, 19, 17)    # MLK Monday 2027-01-18
    assert [trade_key(s, "equity") for s in (sun, mon, tue)] == [sun, sun, tue]
    assert trade_key(ms(2026, 6, 21, 17), "equity") == ms(2026, 6, 18, 17)            # Juneteenth Friday halt
    assert trade_key(ms(2026, 4, 5, 17), "equity") == ms(2026, 4, 5, 17)              # Good Friday: closed, nothing to fold
    assert trade_key(ms(2026, 11, 26, 17), "equity") == ms(2026, 11, 25, 17)          # Thanksgiving into Friday
    sessions.set_calendar({"days": []})
    assert trade_key(mon, "equity") == mon


def minute_bars(start, end, group="equity"):
    """One bar a minute while the market is open; the price climbs a point an hour so each bar is traceable."""
    out, t = [], start
    while t < end:
        if is_open(t, group):
            px = 7000 + (t - start) / 3_600_000
            out.append(Bar(t=t, o=px, h=px + 1, l=px - 1, c=px, v=1))
        t += 60_000
    return out


def test_daily_bars_merge_the_holiday_session_also_when_loaded_day_by_day(cal, tmp_path):
    thu, sun, mon, tue = ms(2027, 1, 14, 17), ms(2027, 1, 17, 17), ms(2027, 1, 18, 17), ms(2027, 1, 19, 17)
    bars = minute_bars(thu, tue)
    whole = Daily()
    whole.add_minutes(bars, tue)
    assert whole.keys == [thu, sun] and whole.before(mon) == 1 and whole.before(tue) == 2
    merged = [b for b in bars if b.t >= sun]
    assert whole.hlc[1] == [max(b.h for b in merged), min(b.l for b in merged), merged[-1].c]
    assert any(b.t < ms(2027, 1, 18, 12) for b in merged) and any(b.t > mon for b in merged)
    # The service loads to the current session, then adds each day as it completes: Monday evening it must hold
    # back the holiday session (today's trade date began Sunday), and Tuesday evening add both sessions as one.
    store = Store(tmp_path / "t.db")
    store.insert_bars_if_missing("ES", bars)
    svc = TpoService(SimpleNamespace(store=store, instruments={"ES": SimpleNamespace(calendar="equity")}))
    assert svc.daily_for("ES", mon).keys == [thu]
    d = svc.daily_for("ES", tue)
    assert d.keys == whole.keys and d.hlc == whole.hlc
    plain = Daily("equity")
    sessions.set_calendar({"days": []})
    plain.add_minutes(bars, tue)
    assert plain.keys == [thu, sun, mon]                                 # without the calendar: three days


def test_watchdog_stays_quiet_and_keeps_the_heartbeat_through_a_holiday(cal, tmp_path, monkeypatch):
    from test_review_fixes import FakeHttp
    http = FakeHttp()

    async def go():
        m, notifier = build_market(tmp_path, monkeypatch, feed="sim", http=http,
                                   CHART_HEARTBEAT_URL="https://hc-ping.example/uuid")
        m.started = 0
        for sym in m.instruments:
            m.last_tick[sym] = Tick(sym, ms(2026, 11, 26, 11, 59, 59), 1.0, 1, 0)
        monkeypatch.setattr(market_mod, "now_ms", lambda: ms(2026, 11, 26, 14))    # Thanksgiving afternoon
        await m._watchdog_pass()
        quiet, h = list(notifier.sent), m.health()
        monkeypatch.setattr(market_mod, "now_ms", lambda: ms(2026, 11, 26, 17, 10))   # reopened, still silent
        await m._watchdog_pass()
        return quiet, h, notifier.sent
    quiet, h, later = asyncio.run(go())
    assert quiet == [] and http.calls >= 1
    assert h["holiday"] == "Thanksgiving" and h["session_open"]["ES"] is False and h["session_open"]["CL"] is False
    assert sorted(msg.split()[1] for _, msg in later) == ["CL", "ES", "GC", "MES", "MNQ", "NQ"]
    json.dumps(h)


def test_the_calendar_never_runs_out():
    """Years the file doesn't list follow CME's usual pattern: it reproduces the years the file does list."""
    import json
    from datetime import date
    from chartsrv.config import ROOT, load_holidays, rule_holidays
    kind = lambda d: next(k for k in ("halt", "close", "closed") if k in d)   # noqa: E731
    listed = json.loads((ROOT / "config" / "holidays.json").read_text())["days"]
    for y in ("2026", "2027"):
        assert {(d["date"], kind(d), json.dumps(d.get(kind(d)))) for d in listed if d["date"].startswith(y)} == \
               {(d["date"], kind(d), json.dumps(d.get(kind(d)))) for d in rule_holidays(int(y))}
    cal = load_holidays(ROOT / "config", today=date(2029, 3, 1))
    days = {d["date"]: d for d in cal["days"]}
    assert days["2029-11-22"]["name"] == "Thanksgiving" and days["2030-04-19"]["closed"]   # Good Friday 2030
    assert days["2031-12-25"]["closed"] and "2028-01-01" not in days   # a Saturday New Year's Day moves nothing
    assert sum(d["date"].startswith("2027") for d in cal["days"]) == sum(d["date"].startswith("2027") for d in listed)   # listed: as is
    sessions.set_calendar(cal)   # and it loads


def test_a_closed_holiday_is_not_a_session():
    """The sessions looked back over (profiles of N sessions, replay, tick history, the details' day) skip New Year's,
    Good Friday and Christmas: nothing traded."""
    from datetime import date
    sessions.set_calendar(load_holidays(CONFIG, today=date(2026, 10, 21)))
    assert sessions.previous_session_start(ms(2026, 4, 5, 17)) == ms(2026, 4, 1, 17)    # Monday's: Thursday's before it
    assert sessions.previous_session_start(ms(2026, 4, 4, 10)) == ms(2026, 4, 1, 17)    # on the Saturday
    assert sessions.previous_session_start(ms(2026, 10, 20, 17)) == ms(2026, 10, 19, 17)


def test_daily_bars_fold_a_holiday_halt_into_the_next_trade_date():
    """As CME and TradingView: MLK Day's short session is in Tuesday's 1D bar (stamped at Tuesday's 17:00 open, the
    evening before), the RTH 1D bar too, and the July 3 halt's in the next week's bar."""
    from datetime import date
    from chartsrv.bars import make_series, parse_spec
    sessions.set_calendar(load_holidays(CONFIG, today=date(2026, 10, 21)))

    def bars(spec, ticks, rth=False):
        s = make_series(parse_spec(spec), 0.25, 2, rth=("08:30", "15:00"), rth_only=rth, group="equity", studies=False)
        for t, p in ticks:
            s.on_tick(Tick("ES", t, p, 1, 1))
        return [(b.t, b.o, b.c) for b in s.bars]
    mlk = [(ms(2026, 1, 16, 10), 100), (ms(2026, 1, 18, 18), 101), (ms(2026, 1, 19, 10), 102), (ms(2026, 1, 19, 18), 103),
           (ms(2026, 1, 20, 10), 104), (ms(2026, 1, 21, 10), 105)]
    assert bars("1D", mlk) == [(ms(2026, 1, 15, 17), 100, 100), (ms(2026, 1, 19, 17), 101, 104), (ms(2026, 1, 20, 17), 105, 105)]
    assert bars("1D", mlk, rth=True) == [(ms(2026, 1, 16, 8, 30), 100, 100), (ms(2026, 1, 20, 8, 30), 102, 104),
                                         (ms(2026, 1, 21, 8, 30), 105, 105)]
    jul = [(ms(2026, 6, 29, 10), 1), (ms(2026, 7, 2, 18), 2), (ms(2026, 7, 3, 10), 3), (ms(2026, 7, 6, 10), 4)]
    assert bars("1W", jul) == [(ms(2026, 6, 28, 17), 1, 1), (ms(2026, 7, 5, 17), 2, 4)]


def test_hello_has_the_holidays_as_far_back_as_a_chart_goes(monkeypatch):
    """The chart's pivots fold a halt day's short session into the next trade date (web/ta.js setSessionHolidays): an
    hourly chart's bars go back months and a replay further, so hello's holiday hours reach back over all of it, not
    only the countdown's days ahead."""
    from datetime import date
    sessions.set_calendar(load_holidays(CONFIG, today=date(2026, 10, 26)))
    monkeypatch.setattr(market_mod, "now_ms", lambda: ms(2026, 10, 26, 10))
    got = market_mod.Market.holiday_hours(None)
    assert got["2026-01-19"] == {"halt": {"default": "12:00", "energy": "13:30", "metals": "13:30"}}   # MLK Day
    assert "2024-11-28" in got and "2026-11-26" in got and "2026-12-25" in got   # Thanksgiving 2024 and this year, Christmas
    assert max(got) == "2028-12-25"   # and ahead all the calendar holds, as the server's trend-line alerts count it


def test_old_history_keeps_its_holidays_whatever_the_year():
    """1-minute history is kept for ever (the NinjaTrader export starts in 2019): the usual pattern covers its years
    too, so a 2019 MLK halt folds into Tuesday's 1D bar (replay, long calendar charts), and the calendar of the years
    behind doesn't change when a new year starts."""
    from datetime import date
    from chartsrv.bars import make_series, parse_spec
    old = lambda today: [d for d in load_holidays(CONFIG, today=today)["days"] if d["date"] < "2021"]   # noqa: E731
    assert old(date(2026, 10, 26)) == old(date(2027, 1, 5)) and "2019-01-21" in {d["date"] for d in old(date(2026, 10, 26))}
    sessions.set_calendar(load_holidays(CONFIG, today=date(2026, 10, 26)))
    s = make_series(parse_spec("1D"), 0.25, 2, rth=("08:30", "15:00"), group="equity", studies=False)
    for t, p in [(ms(2019, 1, 18, 10), 100), (ms(2019, 1, 20, 18), 101), (ms(2019, 1, 21, 10), 102),
                 (ms(2019, 1, 21, 18), 103), (ms(2019, 1, 22, 10), 104)]:
        s.on_tick(Tick("ES", t, p, 1, 1))
    assert [(b.t, b.o, b.c) for b in s.bars] == [(ms(2019, 1, 17, 17), 100, 100), (ms(2019, 1, 21, 17), 101, 104)]


@pytest.mark.parametrize("edit", [lambda t: t.replace('"default": "12:00", "energy": "13:30"', '"default": "12.00", "energy": "13:30"', 1),
                                  lambda t: t.replace("}\n  ]", "},\n  ]", 1),   # a trailing comma
                                  lambda t: t.replace('{"date": "2026-11-26", ', '{', 1)])   # a line without its date
def test_a_bad_holidays_file_still_starts_the_server(tmp_path, monkeypatch, caplog, edit):
    """config/holidays.json is edited by hand before holidays (docs/DATA.md): a typo in it is logged and CME's usual
    pattern applies until it is fixed, as a bad settlements.json is logged; it doesn't stop the server from starting
    (charts, feeds and alerts all down, the container restarting over and over)."""
    import shutil
    from fastapi.testclient import TestClient
    cfg = tmp_path / "config"
    shutil.copytree(CONFIG, cfg)
    text = (cfg / "holidays.json").read_text()
    assert edit(text) != text
    (cfg / "holidays.json").write_text(edit(text))
    for k, v in {"CHART_CONFIG_DIR": cfg, "CHART_DB": tmp_path / "chart.db", "CHART_LEVELS_DIR": tmp_path,
                 "CHART_DATA_DIR": tmp_path, "CHART_FEED": "none"}.items():
        monkeypatch.setenv(k, str(v))
    from chartsrv.app import app
    with TestClient(app) as c:
        assert c.get("/api/health").status_code == 200
    assert "config/holidays.json not read" in caplog.text
    assert not is_open(ms(2026, 11, 26, 13), "equity") and not is_open(ms(2026, 12, 25, 10), "equity")   # the usual pattern


def test_a_year_corrected_as_data_md_says_keeps_its_other_holidays(tmp_path):
    """A year the file doesn't list is added whole (docs/DATA.md prints its lines to paste under "days": [): with one
    of its days corrected, the year keeps the others; one line alone would be the year's only holiday (a year in the
    file is used as it is)."""
    import re
    import subprocess
    import sys
    from datetime import date
    doc = (CONFIG.parent / "docs" / "DATA.md").read_text()
    cmd = re.search(r'docker compose exec chart python -c "(.+)"', doc).group(1)
    lines = subprocess.run([sys.executable, "-c", cmd], capture_output=True, text=True, check=True,
                           cwd=Path(__file__).resolve().parents[1]).stdout.replace('"12:00"', '"11:00"', 1)
    text = (CONFIG / "holidays.json").read_text()
    (tmp_path / "holidays.json").write_text(text.replace('  "days": [\n', '  "days": [\n' + lines, 1))
    sessions.set_calendar(cal := load_holidays(tmp_path, today=date(2028, 11, 1)))
    assert sum(d["date"].startswith("2028") for d in cal["days"]) == 11
    assert sessions.holidays()[date(2028, 1, 17)]["halt"]["default"] == "11:00"   # the corrected MLK Day
    assert not is_open(ms(2028, 12, 25, 10), "equity")   # and Christmas still closed
