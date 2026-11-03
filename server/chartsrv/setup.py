"""First run: a fresh install opens on the simulator and a setup guide (the password, where futures trades come
from, alerts); an install that already holds data or settings never sees it. Secure by default: on a fresh install,
while no password is set (none yet, or Login turned off later), only the PC itself may use the app (another device is
refused).

Leaving the simulator (the guide, or Connections > NinjaTrader): its invented trades and bars are deleted, a copy of
the database kept next to it (refused without room on the disk for it), and the server restarts on the real feeds
(NinjaTrader and plugins).

    docker compose exec chart python -m chartsrv.setup again    # show the guide again on the next page load"""
import argparse
import logging
import sqlite3
from dataclasses import replace
from urllib.parse import urlsplit

log = logging.getLogger(__name__)
SETTING, LEAVE_SIM = "setup", "leave_sim"   # {"done", "step", "fresh"}; "pending" -> "done" (or {"failed": why})
TS_NAMES = "ts_names"   # this PC's own tailscale serve names, newest last: learned by app._own_host
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


def init(store) -> dict:
    """At start, before anything is seeded: a database with nothing in it is a fresh install (the guide shows, and
    other devices wait for a password); any other is set up already."""
    st = store.get_setting(SETTING, None)
    if st is None:
        db = store.db
        empty = all(db.execute(f"SELECT 1 FROM {t} LIMIT 1").fetchone() is None
                    for t in ("ticks", "bars_1m", "settings", "alerts", "drawings"))
        st = {"done": not empty, "step": 0, "fresh": empty}
        store.set_setting(SETTING, st)
        log.info("first run: %s", "a fresh install, the setup guide shows" if empty else "an existing install, no setup guide")
    return st


def leave_sim(store, settings, out=log.info):
    """The settings to run with: once the simulator was left, .env's CHART_FEED=sim reads as NinjaTrader (and sim
    quotes as Yahoo). A pending leave deletes the invented market data first, after a backup. When that backup can't
    be made (no room for it on the disk), the server starts on the simulator with nothing deleted, and Connections >
    NinjaTrader says why: it is tried again when Leave the simulator is, not at every start."""
    flag = store.get_setting(LEAVE_SIM, None)
    if flag == "pending" and settings.feed != "sim":   # not on the simulator (a restore run without .env): its trades are real
        store.set_setting(LEAVE_SIM, None)
        flag = None
    if flag == "pending":
        from .wipe_market_data import wipe
        try:
            wipe(store.path, True, out)
        except (OSError, sqlite3.Error) as e:
            log.error("the simulator was not left (%s: %s): it runs on, its trades kept", type(e).__name__, e)
            store.set_setting(LEAVE_SIM, {"failed": f"{type(e).__name__}: {e}"[:200]})
            return settings
        store.set_setting(LEAVE_SIM, "done")
        flag = "done"
    if flag == "done" and settings.feed == "sim":
        return replace(settings, feed="ninjatrader", quotes_feed="yahoo" if settings.quotes_feed in ("sim", "") else settings.quotes_feed)
    return settings


def on_sim(store, settings) -> bool:
    """The simulator runs: .env's CHART_FEED=sim and not left yet. Leaving it deletes every stored trade and bar, so
    history imported now (backfill, Databento) would go too, after mixing with its invented trades in the charts."""
    return settings.feed == "sim" and store.get_setting(LEAVE_SIM, None) != "done"


NO_IMPORT = ("This server runs the simulator (CHART_FEED=sim): imported history would mix with its invented trades, "
             "and leaving it deletes every stored trade and bar, imported ones too. Leave the simulator first (the "
             "setup guide, or Connections > NinjaTrader), then import.")


def is_local(headers, client_host: str | None) -> bool:
    """The PC itself: the browser asked for localhost, not through a proxy (tailscale serve adds X-Forwarded-For).
    docker-compose.yml publishes the app on 127.0.0.1 only, so anything else arrives through such a proxy."""
    if headers.get("x-forwarded-for"):
        return False
    return (urlsplit("//" + (headers.get("host") or "")).hostname or "") in LOCAL_HOSTS


def locked(fresh: bool, auth_enabled: bool, settings) -> bool:
    """A fresh install with no password (none yet, or Login turned off): other devices wait (CHART_REMOTE_NEEDS_PASSWORD=0
    lets them in)."""
    return fresh and not auth_enabled and getattr(settings, "remote_needs_password", True)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("what", choices=["again"], help="again: the setup guide shows on the next page load")
    ap.parse_args()
    from .config import load_settings
    from .store import Store
    store = Store(load_settings().db_path)
    st = store.get_setting(SETTING, None) or {}
    store.set_setting(SETTING, {**st, "done": False, "step": 0})
    store.close()
    print("The setup guide shows on the next page load.")


if __name__ == "__main__":
    main()
