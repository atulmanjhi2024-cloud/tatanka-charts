import json
import random
import zipfile
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pyarrow as pa
import pytest

from chartsrv import databento as dbi
from chartsrv.config import load_instruments
from chartsrv.models import Tick
from chartsrv.sessions import CT
from chartsrv.store import Store

CONFIG = Path(__file__).resolve().parents[2] / "config"
HEADER = "ts_recv,ts_event,rtype,publisher_id,instrument_id,action,side,depth,price,size,flags,ts_in_delta,sequence,symbol"


def ms(y, mo, d, h, mi=0, s=0, tz=CT):
    return int(datetime(y, mo, d, h, mi, s, tzinfo=tz).timestamp() * 1000)


def iso(t_ms, extra_ns=123):
    dt = datetime.fromtimestamp(t_ms / 1000, timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{t_ms % 1000:03d}000{extra_ns:03d}Z"


def trades(start, minutes, base, sym, every_s=20, size=2, seed=None):
    """A trade every `every_s` seconds for `minutes`, price walking a tick at a time (at random with a seed), sides
    alternating."""
    out, p, rnd = [], base, random.Random(seed)
    for i in range(minutes * 60 // every_s):
        t = start + i * every_s * 1000
        if seed is not None:
            p += rnd.choice((-0.25, 0, 0.25))
        else:
            p = p + 0.25 if (i // 3) % 2 == 0 else p - 0.25
        out.append((t, round(p, 2), size, "B" if i % 2 == 0 else "A", sym))
    return out


def pretty_csv(rows):
    lines = [HEADER] + [f"{iso(t)},{iso(t)},0,1,{abs(hash(s)) % 100000},T,{sd},0,{p:.9f},{sz},0,0,0,{s}" for t, p, sz, sd, s in rows]
    return "\n".join(lines) + "\n"


def write_zst(path: Path, text: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    with pa.output_stream(str(path), compression="zstd") as f:
        f.write(text.encode())


@pytest.fixture
def env(tmp_path):
    inst = load_instruments(CONFIG)
    store = Store(tmp_path / "chart.db")
    return tmp_path, inst, store


def run(store, inst, paths, last_tz="auto", **kw):
    imp = dbi.Importer(store, inst, **kw)
    srcs = dbi.unique_sources(dbi.find_sources(paths, set(inst)))
    for s in srcs:
        if s.kind != "last":
            imp.run_trades(s)
    imp.trades_done()
    imp.last_volumes([s for s in srcs if s.kind == "last"], last_tz)
    for s in srcs:
        if s.kind == "last":
            imp.run_last(s, last_tz)
    return imp


def q(store, sql):
    return [tuple(r) for r in store.db.execute(sql).fetchall()]


def count(store, table, sym):
    return store.db.execute(f"SELECT COUNT(*) FROM {table} WHERE symbol=?", (sym,)).fetchone()[0]


def test_pretty_csv_front_contract_sessions_and_idempotent(env):
    tmp, inst, store = env
    # A UTC day file: the end of the 17 Mar session (trade date Mon 17... Tue 18) and the start of the next one.
    a = trades(ms(2025, 3, 18, 9), 60, 5700.0, "ESM5") + trades(ms(2025, 3, 18, 9), 60, 5650.0, "ESH5", every_s=60, size=1)
    a += [(ms(2025, 3, 18, 9, 30), 50.25, 5, "B", "ESH5-ESM5")]
    b = trades(ms(2025, 3, 18, 17, 5), 30, 5710.0, "ESM5")
    rows = sorted(a + b)
    write_zst(tmp / "cme/ES_tick_data/glbx-mdp3-20250318.trades.csv.zst", pretty_csv(rows))
    plain = tmp / "cme/ES_tick_data/glbx-mdp3-20250318.trades.csv/glbx-mdp3-20250318.trades.csv"   # 7-Zip's folder
    plain.parent.mkdir(parents=True)
    plain.write_text(pretty_csv(rows))
    srcs = dbi.find_sources([tmp / "cme"], set(inst))
    assert len(srcs) == 2 and {s.root for s in srcs} == {"ES"}

    dry = run(store, inst, [tmp / "cme"], dry=True)
    assert count(store, "ticks", "ES") == 0 and dry.totals["ES"]["ticks"] > 0

    imp = run(store, inst, [tmp / "cme"])
    es = q(store, "SELECT ts, price, size, side FROM ticks WHERE symbol='ES' ORDER BY ts")
    assert len(es) == 180 + 90 and imp.totals["ES"]["days"] == 2          # ESM5 only, both sessions; the copy skipped
    assert {r[1] for r in es} <= {round(5700 + k * 0.25, 2) for k in range(-4, 5)} | {round(5710 + k * 0.25, 2) for k in range(-4, 5)}
    assert es[0][3] == 1 and es[1][3] == -1 and es[0][0] == ms(2025, 3, 18, 9)
    bars = q(store, "SELECT t, v, bv, sv, n FROM bars_1m WHERE symbol='ES' ORDER BY t")
    assert len(bars) == 90 and bars[0] == (ms(2025, 3, 18, 9), 6, 4, 2, 3)

    again = run(store, inst, [tmp / "cme"])   # nothing doubles
    assert count(store, "ticks", "ES") == 270 and again.totals["ES"]["skipped"] == 2


def test_contract_follows_stored_bars_and_split_fills_nt_minutes(env):
    tmp, inst, store = env
    t0 = ms(2025, 6, 12, 9)
    # Roll week: ESU5 trades more, but the stored NinjaTrader bars are still ESM5's.
    m5, u5 = trades(t0, 60, 6000.0, "ESM5", size=1), trades(t0, 60, 6050.0, "ESU5", size=3)
    write_zst(tmp / "cme/glbx-mdp3-20250612.trades.csv.zst", pretty_csv(sorted(m5 + u5)))
    nt = dbi.minute_bars([r[0] for r in m5], [r[1] for r in m5], [r[2] for r in m5], [0] * len(m5))
    store.insert_rows_if_missing("ES", [(b[0], *b[1:6], 0) for b in nt][:50])   # 50 of the 60 minutes, no split
    run(store, inst, [tmp / "cme"])
    prices = {r[0] for r in q(store, "SELECT price FROM ticks WHERE symbol='ES'")}
    assert max(prices) < 6010
    rows = q(store, "SELECT v, bv, sv FROM bars_1m WHERE symbol='ES' ORDER BY t")
    assert len(rows) == 60 and all(bv + sv == v for v, bv, sv in rows)


def test_raw_csv_with_symbology_in_a_zip_and_live_ticks_win(env):
    tmp, inst, store = env
    rows = sorted(trades(ms(2025, 4, 1, 10), 20, 19500.0, "NQM5") + trades(ms(2025, 4, 2, 10), 20, 19600.0, "NQM5"))
    ids = {"NQM5": 42}
    lines = ["ts_recv,ts_event,rtype,publisher_id,instrument_id,action,side,depth,price,size,flags,ts_in_delta,sequence"]
    lines += [f"{t * 10**6},{t * 10**6 + 5},0,1,{ids[s]},T,{sd},0,{int(round(p * 1e9))},{sz},0,0,0" for t, p, sz, sd, s in rows]
    symb = {"result": {"NQM5": [{"d0": "2025-04-01", "d1": "2025-04-03", "s": "42"}]}}
    z = tmp / "dl/GLBX-20261003-TEST.zip"
    z.parent.mkdir()
    with zipfile.ZipFile(z, "w") as f:
        f.writestr("symbology.json", json.dumps(symb))
        f.writestr("glbx-mdp3-20250401-20250402.trades.csv", "\n".join(lines) + "\n")
    store.write([Tick("NQ", ms(2025, 4, 2, 10, 5), 19600.0, 1, 1)], [])   # the live feed had 2 April
    imp = run(store, inst, [z])
    nq = q(store, "SELECT ts, price FROM ticks WHERE symbol='NQ' ORDER BY ts")
    assert len(nq) == 60 + 1 and nq[0] == (ms(2025, 4, 1, 10), 19500.25) and imp.totals["NQ"]["skipped"] == 1


def test_dbn_in_a_zip(env):
    dbn = pytest.importorskip("databento_dbn")
    from types import SimpleNamespace as NS
    tmp, inst, store = env
    rows = trades(ms(2025, 5, 6, 8), 10, 61.5, "CLM5", size=1)
    mp = [NS(raw_symbol="CLM5", intervals=[NS(start_date=datetime(2025, 5, 6).date(), end_date=datetime(2025, 5, 7).date(), symbol="77")])]
    md = dbn.Metadata(dataset="GLBX.MDP3", start=ms(2025, 5, 6, 0) * 10**6, stype_in=dbn.SType.RAW_SYMBOL,
                      stype_out=dbn.SType.INSTRUMENT_ID, schema=dbn.Schema.TRADES, symbols=["CLM5"], mappings=mp,
                      end=ms(2025, 5, 7, 0) * 10**6)
    side = {"B": dbn.Side.BID, "A": dbn.Side.ASK}
    blob = md.encode() + b"".join(bytes(dbn.TradeMsg(1, 77, t * 10**6, int(round(p * 1e9)), sz, dbn.Action.TRADE, side[sd], 0, t * 10**6))
                                  for t, p, sz, sd, _ in rows)
    raw = tmp / "x.dbn.zst"
    with pa.output_stream(str(raw), compression="zstd") as f:
        f.write(blob)
    z = tmp / "GLBX-DBN.zip"
    with zipfile.ZipFile(z, "w") as f:
        f.write(raw, "glbx-mdp3-20250506.trades.dbn.zst")
    run(store, inst, [z])
    cl = q(store, "SELECT price, side FROM ticks WHERE symbol='CL' ORDER BY ts")
    assert len(cl) == 30 and cl[0] == (61.75, 1) and cl[1][1] == -1


def test_ninjatrader_last_file_time_zone_found_from_stored_bars(env):
    tmp, inst, store = env
    rows = trades(ms(2024, 5, 7, 9), 120, 18000.0, "x", every_s=10, size=1, seed=7)
    nt = dbi.minute_bars([r[0] for r in rows], [r[1] for r in rows], [r[2] for r in rows], [0] * len(rows))
    store.insert_rows_if_missing("NQ", [(b[0], *b[1:6], 0) for b in nt])
    ny = ZoneInfo("America/New_York")   # the file is in New York time
    text = "".join(f"{datetime.fromtimestamp(t / 1000, ny):%Y%m%d %H%M%S} {t % 1000:03d}0000;{p};{p - 0.25};{p};{s}\n"
                   for t, p, s, _, _ in rows)
    f = tmp / "nt_export/NQ 06-24.Last.txt"
    f.parent.mkdir()
    f.write_text(text)
    imp = run(store, inst, [tmp / "nt_export"])
    got = q(store, "SELECT ts, side FROM ticks WHERE symbol='NQ' ORDER BY ts")
    assert len(got) == len(rows) and got[0] == (rows[0][0], 1) and imp.totals["NQ"]["days"] == 1
    assert imp.totals["NQ"]["bars"] == 0 and imp.totals["NQ"]["split"] == 120   # its minutes line up with the stored ones


def test_sources_by_member_day_and_folder(tmp_path):
    """A batch zip is named after the job date: each member keeps its own day. Two unzipped jobs side by side (ES and
    NQ for the same day) are two sources. A month file counts up to its last day for --since."""
    z = tmp_path / "GLBX-20261003-WEA4JKV5PF.zip"
    with zipfile.ZipFile(z, "w") as f:
        f.writestr("symbology.json", "{}")
        for d in ("20250401", "20250402", "20250403"):
            f.writestr(f"glbx-mdp3-{d}.trades.csv.zst", b"")
    found = dbi.find_sources([z], {"ES"})
    assert [s.day.isoformat() for s in dbi.unique_sources(found, date(2025, 4, 1), date(2025, 4, 3))] == \
        ["2025-04-01", "2025-04-02", "2025-04-03"]
    for job in ("GLBX-20250401-AAAA", "GLBX-20250401-BBBB"):
        p = tmp_path / "db" / job / "glbx-mdp3-20250318.trades.csv.zst"
        p.parent.mkdir(parents=True)
        p.write_bytes(b"")
    extracted = tmp_path / "db" / "GLBX-20250401-AAAA" / "glbx-mdp3-20250318.trades.csv" / "glbx-mdp3-20250318.trades.csv"
    extracted.parent.mkdir()
    extracted.write_bytes(b"")                       # 7-Zip's copy of the first: the same file
    assert len(dbi.unique_sources(dbi.find_sources([tmp_path / "db"], {"ES", "NQ"}))) == 2
    m = tmp_path / "month" / "glbx-mdp3-20250301-20250331.trades.csv.zst"
    m.parent.mkdir()
    m.write_bytes(b"")
    (s,) = dbi.find_sources([tmp_path / "month"], {"ES"})
    assert (s.day, s.end) == (date(2025, 3, 1), date(2025, 3, 31)) and dbi.unique_sources([s], since=date(2025, 3, 15))


def test_last_file_across_a_daylight_saving_switch(tmp_path):
    """A NinjaTrader export in New York time spanning Nov 3: both sides land at the right UTC time."""
    inst, store = load_instruments(CONFIG), Store(tmp_path / "c.db")
    d1 = trades(ms(2024, 10, 31, 8), 240, 20000.0, "x", every_s=5, size=1, seed=1)    # EDT
    d2 = trades(ms(2024, 11, 4, 9), 120, 20100.0, "x", every_s=10, size=1, seed=2)    # EST, after the switch
    for day in (d1, d2):
        nt = dbi.minute_bars([r[0] for r in day], [r[1] for r in day], [r[2] for r in day], [0] * len(day))
        store.insert_rows_if_missing("NQ", [(b[0], *b[1:6], 0) for b in nt])
    ny = ZoneInfo("America/New_York")
    f = tmp_path / "imp" / "NQ 12-24.Last.txt"
    f.parent.mkdir()
    f.write_text("".join(f"{datetime.fromtimestamp(t / 1000, ny):%Y%m%d %H%M%S} {t % 1000:03d}0000;{p};{s}\n"
                         for t, p, s, _, _ in d1 + d2) + "20241104 120000 0000000;20100.5;\n")   # a line cut short
    run(store, inst, [tmp_path / "imp"])
    got = [r[0] for r in store.db.execute("SELECT ts FROM ticks WHERE symbol='NQ' ORDER BY ts")]
    assert got == sorted(r[0] for r in d1 + d2)


def test_a_session_split_over_two_day_files_gets_one_contract(env):
    """Daily files cover UTC days, so the 13 Mar 2025 session (from 17:00 CT on the 12th) starts in the 12th's file and
    goes on in the 13th's. On a roll, the old contract still trades more in its first evening hours, the new one more
    over the whole session: the whole session is the new one's, not two contracts with a jump between."""
    tmp, inst, store = env
    head = trades(ms(2025, 3, 12, 17), 120, 5700.0, "ESH5", size=3) + trades(ms(2025, 3, 12, 17), 120, 5740.0, "ESM5", size=1)
    tail = trades(ms(2025, 3, 12, 19), 600, 5740.0, "ESM5", size=3) + trades(ms(2025, 3, 12, 19), 600, 5700.0, "ESH5", size=1)
    write_zst(tmp / "cme/glbx-mdp3-20250312.trades.csv.zst", pretty_csv(sorted(head)))
    write_zst(tmp / "cme/glbx-mdp3-20250313.trades.csv.zst", pretty_csv(sorted(tail)))
    imp = run(store, inst, [tmp / "cme"])
    prices = {r[0] for r in q(store, "SELECT price FROM ticks WHERE symbol='ES'")}
    assert min(prices) > 5730 and count(store, "ticks", "ES") == 360 + 1800 and imp.totals["ES"]["days"] == 1


def test_a_split_session_an_earlier_run_began_is_finished_when_the_next_day_file_arrives(env):
    """Day files imported as they arrive: the first run has only the 12th's file and stores the 13 Mar session's first
    evening hours. Run again once the 13th's file is in the folder, the rest of that session is stored too, not skipped
    because its first hours have ticks. Each day file's part is checked on its own."""
    tmp, inst, store = env
    head = trades(ms(2025, 3, 12, 17), 120, 5740.0, "ESM5", size=3)
    tail = trades(ms(2025, 3, 12, 19), 600, 5740.0, "ESM5", size=3)
    write_zst(tmp / "cme/glbx-mdp3-20250312.trades.csv.zst", pretty_csv(head))
    run(store, inst, [tmp / "cme"])
    assert count(store, "ticks", "ES") == 360
    write_zst(tmp / "cme/glbx-mdp3-20250313.trades.csv.zst", pretty_csv(tail))
    imp = run(store, inst, [tmp / "cme"])
    assert count(store, "ticks", "ES") == 360 + 1800 and count(store, "bars_1m", "ES") == 720
    assert imp.totals["ES"]["ticks"] == 1800 and imp.totals["ES"]["skipped"] == 1   # the stored part only
    again = run(store, inst, [tmp / "cme"])   # nothing doubles
    assert count(store, "ticks", "ES") == 2160 and again.totals["ES"]["skipped"] == 2 and not again.totals["ES"]["ticks"]


def test_a_split_session_the_live_feed_began_is_filled_after_the_outage(env):
    """The live feed recorded the 13 Mar session's first evening hours, then NinjaTrader was off: importing both day
    files fills the rest of the session and leaves the recorded hours as they are."""
    tmp, inst, store = env
    head = trades(ms(2025, 3, 12, 17), 120, 5740.0, "ESM5", size=3)
    tail = trades(ms(2025, 3, 12, 19), 600, 5740.0, "ESM5", size=3)
    store.write([Tick("ES", t, p, 1, 0) for t, p, _, _, _ in head], [])   # recorded live, without the side
    write_zst(tmp / "cme/glbx-mdp3-20250312.trades.csv.zst", pretty_csv(head))
    write_zst(tmp / "cme/glbx-mdp3-20250313.trades.csv.zst", pretty_csv(tail))
    run(store, inst, [tmp / "cme"])
    assert count(store, "ticks", "ES") == 360 + 1800
    assert q(store, f"SELECT SUM(size) FROM ticks WHERE symbol='ES' AND ts < {ms(2025, 3, 12, 19)}") == [(360,)]   # live


def test_overlapping_downloads_store_a_session_once(env):
    """Two downloads of one product overlap by a day: a week's file and the day file of its Friday. Friday's session, held
    from the week's file, is joined to the day file's part of it, which holds the same trades again: stored once."""
    tmp, inst, store = env
    fri = trades(ms(2025, 3, 20, 17), 23 * 60, 5700.0, "ESM5", every_s=60, seed=1)   # Thu 17:00 .. Fri 16:00 CT
    utc_fri = [r for r in fri if r[0] >= ms(2025, 3, 21, 0, tz=timezone.utc)]
    write_zst(tmp / "cme/ES/glbx-mdp3-20250317-20250321.trades.csv.zst", pretty_csv(fri))
    write_zst(tmp / "cme/ES/glbx-mdp3-20250321.trades.csv.zst", pretty_csv(utc_fri))
    run(store, inst, [tmp / "cme"])
    assert count(store, "ticks", "ES") == len(fri) and len(utc_fri) == len(fri) - 120
    assert q(store, "SELECT SUM(size) FROM ticks WHERE symbol='ES'") == [(sum(r[2] for r in fri),)]
    again = run(store, inst, [tmp / "cme"])   # nothing doubles
    assert count(store, "ticks", "ES") == len(fri) and not again.totals["ES"]["ticks"]


def test_last_files_overlapping_in_dates_keep_the_contract_of_the_stored_bars(env):
    """'NQ 03-24' sorts before 'NQ 12-23' and both exports hold 4 Dec 2023, when the stored 1-minute bars are 12-23's:
    that session is 12-23's, not the back month's 200 points higher; 03-24 still gets January."""
    tmp, inst, store = env
    dec_front = trades(ms(2023, 12, 4, 9), 120, 15800.0, "x", every_s=10, size=1, seed=3)
    dec_back = trades(ms(2023, 12, 4, 9), 60, 16000.0, "x", every_s=30, size=1, seed=4)
    jan = trades(ms(2024, 1, 8, 9), 120, 16500.0, "x", every_s=10, size=1, seed=5)
    for day in (dec_front, jan):
        nt = dbi.minute_bars([r[0] for r in day], [r[1] for r in day], [r[2] for r in day], [0] * len(day))
        store.insert_rows_if_missing("NQ", [(b[0], *b[1:6], 0) for b in nt])
    ny = ZoneInfo("America/New_York")
    for name, rows in (("NQ 03-24", dec_back + jan), ("NQ 12-23", dec_front)):
        f = tmp / "imp" / f"{name}.Last.txt"
        f.parent.mkdir(exist_ok=True)
        f.write_text("".join(f"{datetime.fromtimestamp(t / 1000, ny):%Y%m%d %H%M%S} {t % 1000:03d}0000;{p};{s}\n"
                             for t, p, s, _, _ in rows))
    run(store, inst, [tmp / "imp"])
    dec = q(store, f"SELECT ts, price FROM ticks WHERE symbol='NQ' AND ts < {ms(2024, 1, 1, 0)} ORDER BY ts")
    assert dec == [(t, p) for t, p, _, _, _ in dec_front]
    assert count(store, "ticks", "NQ") == len(dec_front) + len(jan)


def test_last_files_overlapping_in_dates_with_no_stored_bars_go_to_the_most_traded(env):
    """No 1-minute bars stored yet and --last-tz given: 'NQ 03-24' still sorts before 'NQ 12-23', but each session both
    exports hold goes to the one that traded the most there: 12-23 before the roll, 03-24 after it."""
    tmp, inst, store = env
    before_front = trades(ms(2023, 12, 4, 9), 120, 15800.0, "x", every_s=10, size=1, seed=3)
    before_back = trades(ms(2023, 12, 4, 9), 60, 16000.0, "x", every_s=30, size=1, seed=4)
    after_front = trades(ms(2023, 12, 12, 9), 60, 15900.0, "x", every_s=30, size=1, seed=5)
    after_back = trades(ms(2023, 12, 12, 9), 120, 16100.0, "x", every_s=10, size=1, seed=6)
    ny = ZoneInfo("America/New_York")
    for name, rows in (("NQ 03-24", before_back + after_back), ("NQ 12-23", before_front + after_front)):
        f = tmp / "imp" / f"{name}.Last.txt"
        f.parent.mkdir(exist_ok=True)
        f.write_text("".join(f"{datetime.fromtimestamp(t / 1000, ny):%Y%m%d %H%M%S} {t % 1000:03d}0000;{p};{s}\n"
                             for t, p, s, _, _ in rows))
    dry = run(store, inst, [tmp / "imp"], last_tz="America/New_York", dry=True)
    assert dry.totals["NQ"]["ticks"] == len(before_front) + len(after_back)
    run(store, inst, [tmp / "imp"], last_tz="America/New_York")
    got = q(store, "SELECT ts, price FROM ticks WHERE symbol='NQ' ORDER BY ts")
    assert got == [(t, p) for t, p, _, _, _ in before_front + after_back]


def test_imported_trades_drop_the_symbols_cached_session_profiles(env):
    """Profiles cached from what was stored before (the simulator's trades, deleted since) are built again from the
    imported ones."""
    from chartsrv.vp_service import SCHEMA_SQL
    tmp, inst, store = env
    store.db.execute(SCHEMA_SQL)
    store.db.executemany("INSERT INTO vp_cache VALUES (?, ?)", [("svp2|ES|1|2|0.25", "{}"), ("svp2|NQ|1|2|0.25", "{}")])
    store.db.commit()
    write_zst(tmp / "cme/glbx-mdp3-20250318.trades.csv.zst", pretty_csv(trades(ms(2025, 3, 18, 9), 60, 5700.0, "ESM5")))
    run(store, inst, [tmp / "cme"], dry=True)
    assert len(q(store, "SELECT key FROM vp_cache")) == 2   # a dry run writes nothing
    run(store, inst, [tmp / "cme"])
    assert q(store, "SELECT key FROM vp_cache") == [("svp2|NQ|1|2|0.25",)]
