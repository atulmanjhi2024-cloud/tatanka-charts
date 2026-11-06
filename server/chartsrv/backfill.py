"""Import 1-minute bars into the chart database. Never overwrites a minute already stored, so the first file
imported for a minute wins; live bars from the feed always replace imported ones.

One file, put in the folder DOCUMENTS_DIR in .env names (your Documents folder; compose mounts it read-only at
/import/documents):
    docker compose exec chart python -m chartsrv.backfill ES /import/documents/ES_1min.csv
Without Docker, from the server folder: python -m chartsrv.backfill ES <file>

Formats, told apart by the file:
  - Parquet with time, open, high, low, close, volume: `time` is the bar CLOSE (as NinjaTrader stamps its bars),
    time-zone aware.
  - Datetime,Open,High,Low,Close,Volume: naive times in --tz (America/Chicago by default), bar open.
  - date,time,open,high,low,close,volume (naive, --tz) or date,open,... with a UTC offset (IBKR export).
Prices are put on the instrument's tick (some files hold float32 prices). The running server
restarts by itself a minute after the import ends, so open charts and studies load the new history."""
import argparse
import csv
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from . import setup
from .config import load_instruments, load_settings
from .models import Bar
from .store import Store

CHUNK = 50_000
USAGE = "give SYMBOL FILE"


def read_csv(path: Path, tz: ZoneInfo) -> tuple[list[Bar], int]:
    """Returns the bars and the number of blank-date rows skipped (IBKR exports end with one)."""
    bars, skipped = [], 0
    for chunk, stats in _csv_chunks(path, tz, None):
        bars += [Bar(t=t, o=o, h=h, l=lo, c=c, v=v, n=n) for t, o, h, lo, c, v, n in chunk]
        skipped = stats["blank"]
    return bars, skipped


def _snap(tick, decimals):
    return (lambda p: p) if not tick else (lambda p: round(round(p / tick) * tick, decimals))


def _csv_chunks(path: Path, tz: ZoneInfo, snap, stats=None):
    stats = stats if stats is not None else {"read": 0, "blank": 0, "skipped_shift": 0}
    snap = snap or (lambda p: p)
    out = []
    with path.open(newline="") as f:
        for raw in csv.DictReader(f):
            r = {(k or "").strip().lower(): (v or "").strip() for k, v in raw.items()}
            stamp = r.get("datetime") or (f"{r.get('date', '')} {r['time']}" if r.get("time") else r.get("date", ""))
            if not stamp.strip():
                stats["blank"] += 1
                continue
            dt = datetime.fromisoformat(stamp.strip())
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=tz)
            out.append((int(dt.timestamp() * 1000), snap(float(r["open"])), snap(float(r["high"])), snap(float(r["low"])),
                        snap(float(r["close"])), int(float(r.get("volume") or 0)), int(float(r.get("barcount") or 0))))
            stats["read"] += 1
            if len(out) >= CHUNK:
                yield out, stats
                out = []
    yield out, stats


def _parquet_chunks(path: Path, snap, stats=None):
    """Parquet 1-minute bars: `time` is the bar's close (NinjaTrader's stamp), so the bar opened a minute earlier."""
    import pyarrow.compute as pc
    import pyarrow.parquet as pq
    stats = stats if stats is not None else {"read": 0, "blank": 0, "skipped_shift": 0}
    f = pq.ParquetFile(path)
    cols = ["time", "open", "high", "low", "close", "volume"]
    for batch in f.iter_batches(batch_size=CHUNK, columns=cols):
        ns = pc.cast(batch.column("time"), "int64").to_pylist()
        o, h, lo, c, v = (batch.column(k).to_pylist() for k in ("open", "high", "low", "close", "volume"))
        out = []
        for i in range(len(ns)):
            out.append((ns[i] // 1_000_000 - 60_000, snap(o[i]), snap(h[i]), snap(lo[i]), snap(c[i]), int(v[i] or 0), 0))
        stats["read"] += len(ns)
        yield out, stats


def import_file(store: Store, symbol: str, path: Path, tz: ZoneInfo, tick: float | None, decimals: int) -> dict:
    """Streams the file into bars_1m. Returns read / inserted / kept-existing / skipped counts."""
    snap = _snap(tick, decimals)
    chunks = _parquet_chunks(path, snap) if path.suffix.lower() == ".parquet" else _csv_chunks(path, tz, snap)
    inserted, stats = 0, {"read": 0, "blank": 0, "skipped_shift": 0}
    for rows, stats in chunks:
        if rows:
            inserted += store.insert_rows_if_missing(symbol, rows)
    db = store.db
    if inserted and db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='vp_cache'").fetchone():
        with db:
            db.execute("DELETE FROM vp_cache WHERE key LIKE ? OR key LIKE ?", (f"svp2|{symbol}|%", f"amtpd|{symbol}|%"))   # session profiles, AMT prior days: built again
    usable = stats["read"] - stats["skipped_shift"]
    return {**stats, "inserted": inserted, "existing": usable - inserted}


def _report(name: str, path: Path, symbol: str, r: dict) -> str:
    extra = f", {r['skipped_shift']:,} time-shifted bars left out" if r["skipped_shift"] else ""
    extra += f", {r['blank']} blank rows" if r["blank"] else ""
    return (f"{symbol}: {name} {path.name}: read {r['read']:,}, inserted {r['inserted']:,}, "
            f"already stored {r['existing']:,}{extra}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("symbol", nargs="?")
    ap.add_argument("file", nargs="?", type=Path)
    ap.add_argument("--tz", default="America/Chicago", help="timezone of naive timestamps")
    ap.add_argument("--db", type=Path, default=None)
    a = ap.parse_args()
    settings = load_settings()
    store = Store(a.db or settings.db_path)
    if setup.on_sim(store, settings):
        store.close()
        raise SystemExit(setup.NO_IMPORT)
    instruments = load_instruments(settings.config_dir, store)
    tz = ZoneInfo(a.tz)
    before = store.counts()["bars_1m"]
    if a.symbol and a.file:
        sym = a.symbol.upper()
        inst = instruments.get(sym)
        r = import_file(store, sym, a.file, tz, inst.tick_size if inst else None, inst.decimals if inst else 2)
        print(_report("file", a.file, sym, r))
    else:
        ap.error(USAGE)
    print(f"bars_1m rows {before:,} -> {store.counts()['bars_1m']:,}. The server restarts by itself in about a minute "
          f"so charts load it.")


if __name__ == "__main__":
    main()
