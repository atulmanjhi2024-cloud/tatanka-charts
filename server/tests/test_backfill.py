"""Backfill: parquet 1-minute bars (close-stamped, as NinjaTrader stamps them), Datetime CSVs (snapped to the tick),
and the first file to hold a minute keeping it."""
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from chartsrv.backfill import import_file
from chartsrv.store import Store

pa = pytest.importorskip("pyarrow")
import pyarrow.parquet as pq  # noqa: E402

CT = ZoneInfo("America/Chicago")


def ms(y, mo, d, h, mi):
    return int(datetime(y, mo, d, h, mi, tzinfo=CT).timestamp() * 1000)


def nt_parquet(path, **extra):
    times = [datetime(2026, 9, 21, 8, 31, tzinfo=CT), datetime(2026, 9, 21, 8, 32, tzinfo=CT)]
    t = pa.table({"time": pa.array(times, pa.timestamp("ns", tz="America/Chicago")),
                  "open": [6600.0, 6601.0], "high": [6602.0, 6603.25], "low": [6599.75, 6600.5],
                  "close": [6601.0, 6602.0], "volume": [1200, 900], **extra})
    pq.write_table(t, path)


def test_parquet_then_csv(tmp_path):
    store = Store(tmp_path / "t.db")
    nt_parquet(tmp_path / "ES_1m.parquet")
    r = import_file(store, "ES", tmp_path / "ES_1m.parquet", CT, 0.25, 2)
    assert r == {"read": 2, "blank": 0, "skipped_shift": 0, "inserted": 2, "existing": 0}
    rows = store.db.execute("SELECT t, o, h, l, c, v FROM bars_1m WHERE symbol='ES' ORDER BY t").fetchall()
    assert [tuple(x) for x in rows] == [(ms(2026, 9, 21, 8, 30), 6600.0, 6602.0, 6599.75, 6601.0, 1200),   # close 08:31 = open 08:30
                                       (ms(2026, 9, 21, 8, 31), 6601.0, 6603.25, 6600.5, 6602.0, 900)]
    (tmp_path / "ES_1min.csv").write_text("Datetime,Open,High,Low,Close,Volume\n"
                                          "2026-09-21 08:31:00,6699.0,6699.0,6699.0,6699.0,1\n"      # held by the parquet
                                          "2026-09-23 08:30:00,6610.1300001,6611.0,6609.87,6610.5,300\n")
    r = import_file(store, "ES", tmp_path / "ES_1min.csv", CT, 0.25, 2)
    assert r["inserted"] == 1 and r["existing"] == 1
    assert tuple(store.db.execute("SELECT o, l FROM bars_1m WHERE t=?", (ms(2026, 9, 23, 8, 30),)).fetchone()) == (6610.25, 6609.75)
    assert store.db.execute("SELECT o FROM bars_1m WHERE t=?", (ms(2026, 9, 21, 8, 31),)).fetchone()[0] == 6601.0


def test_imported_bars_drop_the_symbols_cached_session_profiles(tmp_path):
    """A finished session's profile is cached; bars imported into its empty minutes must show in it after the restart."""
    from chartsrv.vp_service import SCHEMA_SQL
    store = Store(tmp_path / "t.db")
    store.db.execute(SCHEMA_SQL)
    store.db.executemany("INSERT INTO vp_cache VALUES (?, ?)", [("svp2|ES|1|2|0.25", "{}"), ("svp2|NQ|1|2|0.25", "{}")])
    store.db.commit()
    nt_parquet(tmp_path / "ES_1m.parquet")
    import_file(store, "ES", tmp_path / "ES_1m.parquet", CT, 0.25, 2)
    assert [r[0] for r in store.db.execute("SELECT key FROM vp_cache")] == ["svp2|NQ|1|2|0.25"]
