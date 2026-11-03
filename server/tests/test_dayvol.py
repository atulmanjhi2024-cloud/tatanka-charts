"""chartsrv.dayvol: stored 1-minute bars summed per CME trading day (17:00 CT the evening before to 16:00 CT)."""
import sqlite3
from datetime import date, datetime

from chartsrv import dayvol
from chartsrv.sessions import CT
from chartsrv.store import SCHEMA


def ms(*a) -> int:
    return int(datetime(*a, tzinfo=CT).timestamp() * 1000)


def test_days_split_at_the_evening_open():
    db = sqlite3.connect(":memory:")
    db.executescript(SCHEMA)
    rows = [(ms(2026, 10, 15, 15, 59), 100.0, 3),   # Thursday's last minute
            (ms(2026, 10, 15, 17, 0), 101.0, 5),    # the Friday session opens Thursday evening
            (ms(2026, 10, 16, 9, 30), 99.0, 7)]
    db.executemany("INSERT INTO bars_1m (symbol, t, o, h, l, c, v) VALUES ('NQ', ?, ?, ?, ?, ?, ?)",
                   [(t, p, p + 1, p - 1, p, v) for t, p, v in rows])
    got = dayvol.daily(db, "NQ", 5, ms(2026, 10, 16, 10))
    assert got == [(date(2026, 10, 15), 3, 1, 101.0, 99.0), (date(2026, 10, 16), 12, 2, 102.0, 98.0)]
