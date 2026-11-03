"""Each trading day's volume, high and low from the stored 1-minute bars, to check against another platform's daily
bars (a NinjaTrader Daily chart's Data Box). Read-only. Docker, from the Tatanka Charts folder:
    docker compose exec chart python -m chartsrv.dayvol NQ 15
Without Docker, from its server folder: python -m chartsrv.dayvol NQ [days]

A trading day is CME's: 17:00 CT the evening before to 16:00 CT. Minutes counts the 1-minute bars stored for it (a
full Globex day is about 1380); far fewer means data is missing there."""
import argparse
import time
from datetime import date, timedelta

from .amt_runner import ct
from .config import load_settings
from .store import connect

DAY_MS = 86_400_000


def daily(db, symbol: str, days: int, now_ms: int) -> list[tuple[date, int, int, float, float]]:
    """(trade date, volume, minutes, high, low) for the last `days` trading days with bars."""
    out: dict[date, list] = {}
    since = now_ms - int((days * 1.6 + 5) * DAY_MS)
    for t, h, l, v in db.execute("SELECT t, h, l, v FROM bars_1m WHERE symbol=? AND t>=? ORDER BY t", (symbol, since)):
        d = (ct(t) + timedelta(hours=7)).date()   # 17:00 CT opens the next trade date
        x = out.setdefault(d, [0, 0, h, l])
        x[0] += v or 0
        x[1] += 1
        x[2], x[3] = max(x[2], h), min(x[3], l)
    return [(d, *x) for d, x in sorted(out.items())][-days:]


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("symbol")
    ap.add_argument("days", nargs="?", type=int, default=15)
    a = ap.parse_args(argv)
    db = connect(load_settings().db_path, readonly=True)
    rows = daily(db, a.symbol.upper(), max(1, a.days), int(time.time() * 1000))
    if not rows:
        print(f"no 1-minute bars stored for {a.symbol.upper()}")
        return
    print(f"{a.symbol.upper()}  trade date   volume        minutes   high         low")
    for d, v, n, h, l in rows:
        print(f"    {d:%a %Y-%m-%d}  {v:>12,}  {n:>7}   {h:>11,.2f}  {l:>11,.2f}")


if __name__ == "__main__":
    main()
