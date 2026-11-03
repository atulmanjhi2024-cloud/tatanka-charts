import json
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402

from chartsrv import gapfill, sessions, workers  # noqa: E402


@pytest.fixture(autouse=True)
def no_holidays():
    """The app loads config/holidays.json into module state at start-up; every test begins without it."""
    sessions.set_calendar({"days": []})
    yield
    sessions.set_calendar({"days": []})


@pytest.fixture(autouse=True)
def any_day(request, monkeypatch):
    """Tests run any day of the week: on a weekend or a closed holiday the feeds' rule (sessions.weekend) would drop
    the trades a test stamps with today's clock (and a gap fill's in the 16:00 break, gapfill.shut), and the start's
    clean-ups would take out the history it just stored. Off unless the test is about it (@pytest.mark.weekend)."""
    if "weekend" in request.keywords:
        return
    from chartsrv.market import Market

    async def no_clean_up(self):
        pass
    monkeypatch.setattr(sessions, "weekend", lambda ts_ms: False)
    monkeypatch.setattr(gapfill, "shut", lambda ts, group: False)   # a gap fill's history in the break, likewise
    monkeypatch.setattr(Market, "_drop_weekend_prints", no_clean_up)
    monkeypatch.setattr(Market, "_drop_settlement_bars", no_clean_up)


def pytest_configure(config):
    config.addinivalue_line("markers", "weekend: the feeds' weekend and closed-market rules are on (the any_day fixture leaves them alone)")


@pytest.fixture(autouse=True)
def workers_open():
    """A server's stop shuts the worker processes for good, until its next start: every test begins with them open
    (a test's market is often one with no server around it)."""
    workers._closed = False


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    """No test reaches Nasdaq Trader, Coinbase, Yahoo or FRED unless it brings its own transport. The test client
    is not the PC itself (its host is "testserver"): a fresh test database would refuse it until a password is set."""
    monkeypatch.setenv("CHART_SYMBOLS_ONLINE", "0")
    monkeypatch.setenv("CHART_REMOTE_NEEDS_PASSWORD", "0")
    monkeypatch.setenv("CHART_ALLOWED_HOSTS", "testserver")   # the test client's address, which the app answers to
    monkeypatch.setenv("CHART_RESTART_EXEC", "0")   # a restart in a test: a plain stop, never this process run again
    monkeypatch.setenv("CHART_DB_WATCH", "0")       # a test writing the database beside the app: no restart


@pytest.fixture
def in_session(monkeypatch):
    """A clock inside a trading day: the latest weekday (not a CME holiday) at 10:30 CT, running on from there. Tests
    that chart the simulator's last minutes need it: at the real time of day those minutes can sit in the 16:00-17:00 CT
    break or a weekend, where its trades go to the next open and the history comes out a bar or two long. The server
    reads time.time(); a test reading the time should too (datetime.now() is not moved)."""
    cfg = Path(__file__).resolve().parents[2] / "config" / "holidays.json"
    off_days = {date.fromisoformat(d["date"]) for d in json.loads(cfg.read_text()).get("days", [])} if cfg.exists() else set()
    real = time.time
    now = real()
    day = datetime.fromtimestamp(now, sessions.CT).date()
    while True:
        at = datetime(day.year, day.month, day.day, 10, 30, tzinfo=sessions.CT).timestamp()
        if at <= now and day.weekday() < 5 and day not in off_days and day - timedelta(days=1) not in off_days:
            break
        day -= timedelta(days=1)
    monkeypatch.setattr(time, "time", lambda: real() + (at - now))
    return int(at * 1000)
