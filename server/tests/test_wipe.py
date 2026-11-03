"""Wiping market data: a dry run changes nothing; --yes backs up first and keeps alerts, drawings and settings."""
import sqlite3

from chartsrv.models import Bar, Tick
from chartsrv.store import Store
from chartsrv.wipe_market_data import counts, wipe


def test_dry_run_then_wipe_keeps_everything_but_market_data(tmp_path):
    path = tmp_path / "chart.db"
    s = Store(path)
    s.write([Tick("ES", 1_000 + i, 7000.0, 1, 1) for i in range(5)], [("ES", Bar(t=60_000, o=1, h=2, l=0.5, c=1.5, v=3))])
    s.add_alert("ES", "price", {"price": 7000.0, "direction": "cross"}, False, "")
    s.put_drawing("ES", {"id": "d1", "type": "hline"})
    s.set_setting("amt_settings", {"vwap_enable": True})
    s.close()
    lines = []
    assert wipe(path, False, lines.append) is None
    assert "Nothing deleted" in lines[-1] and counts(sqlite3.connect(path))["ticks"] == 5
    backup = wipe(path, True, lines.append)
    after = counts(sqlite3.connect(path))
    assert after["ticks"] == after["bars_1m"] == 0 and after["alerts"] == after["drawings"] == 1 and after["settings"] >= 1
    assert counts(sqlite3.connect(backup))["ticks"] == 5        # the copy has everything


def test_database_from_an_older_version(tmp_path):
    """No drawings or settings tables yet: the counts skip them and the wipe still runs."""
    path = tmp_path / "old.db"
    db = sqlite3.connect(path)
    db.executescript("CREATE TABLE ticks (symbol TEXT, ts INTEGER, price REAL, size INTEGER, side INTEGER);"
                     "CREATE TABLE bars_1m (symbol TEXT, t INTEGER, o REAL, h REAL, l REAL, c REAL, v INTEGER, bv INTEGER, sv INTEGER, n INTEGER);"
                     "CREATE TABLE alerts (id INTEGER PRIMARY KEY, symbol TEXT);"
                     "INSERT INTO ticks VALUES ('ES', 1, 7000, 1, 1); INSERT INTO alerts (symbol) VALUES ('ES');")
    db.commit()
    db.close()
    lines = []
    wipe(path, False, lines.append)
    assert "before: {'ticks': 1, 'bars_1m': 0, 'alerts': 1}" in lines
    wipe(path, True, lines.append)
    assert counts(sqlite3.connect(path)) == {"ticks": 0, "bars_1m": 0, "alerts": 1}


def test_wipe_drops_the_session_profiles_built_from_those_trades(tmp_path):
    """vp_cache holds finished SVP sessions built from the deleted trades: they go too, or once real trades are
    imported for those days the Session Volume Profile would still be the simulator's."""
    from chartsrv.vp_service import SCHEMA_SQL
    path = tmp_path / "chart.db"
    s = Store(path)
    s.write([Tick("ES", 1_000, 7000.0, 1, 1)], [])
    s.db.execute(SCHEMA_SQL)
    s.db.execute("""INSERT INTO vp_cache VALUES ('svp2|ES|0|82800000|0.25', '{"approx": 0}')""")
    s.db.commit()
    s.close()
    wipe(path, True, [].append)
    assert sqlite3.connect(path).execute("SELECT COUNT(*) FROM vp_cache").fetchone()[0] == 0


def test_wipe_drops_the_settlements_worked_out_from_those_trades(tmp_path, monkeypatch):
    """A charted future's settlement worked out from the simulator's trades is saved in the settings: after the wipe
    NinjaTrader's published one counts, not the simulator's (which would stay, its window's own from the trades)."""
    from chartsrv.settlement import Settlements
    from test_settlement import market, ms
    m = market(tmp_path, monkeypatch)
    m.store.write([Tick("ES", ms(20, 14, 59, 40), 6500.00, 10, 1)], [])   # the simulator's trade in the window
    assert m.settlements.reference("ES", ms(21, 10, 0))["price"] == 6500.00
    m.settlements.on_feed("ZN", 112.5, ms(21, 9, 0))   # one NinjaTrader published stays
    m.store.close()
    wipe(m.store.path, True, [].append)
    m.store = Store(m.store.path)
    st = m.settlements = Settlements(m, tmp_path / "cfg")   # the restart on NinjaTrader
    st.on_feed("ES", 6800.00, ms(21, 10, 0))
    assert st.reference("ES", ms(21, 10, 0))["price"] == 6800.00
    assert st.reference("ZN", ms(21, 10, 0))["price"] == 112.5
