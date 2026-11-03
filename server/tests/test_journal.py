"""Your NinjaTrader fills: parsing the add-on's E lines, storing them once, flat-to-flat trades and the journal."""
import pytest

from chartsrv import journal
from chartsrv.models import Bar
from test_review_fixes import build_market

T = 1_760_000_000_000


def fill(ts, side, qty, price, i, acct="Sim101", contract="ES 12-26", comm=2.04):
    return {"account": acct, "symbol": "ES", "contract": contract, "ts": ts, "price": price, "qty": qty, "side": side,
            "point_value": 50.0, "commission": comm, "exec_id": f"x{i}", "order_id": f"o{i}", "name": "Entry"}


def test_parse_line():
    f = journal.parse_line("E|Sim101|ES|ES 12-26|1760000000000|6900.25|2|1|50|4.08|abc123|ord9|Entry".split("|"))
    assert f["qty"] == 2 and f["side"] == 1 and f["point_value"] == 50.0 and f["exec_id"] == "abc123"
    named = journal.parse_line("E|Sim101|ES|ES 12-26|1|6900|1|1|50|0|ab|o1|Long Entry [1] <x>".split("|"))
    assert named["name"] == "Long Entry [1] _x_"   # any order name: kept, never the fill dropped
    for bad in ["E|a|ES|ES 12-26|1|0|1|1|50|0|x|o|n", "E|a|ES|ES 12-26|1|6900|1|2|50|0|x|o|n", "E|a|ES|ES 12-26|1|6900|1|1|50|0||o|n",
                "E|a|ES|ES 12-26|1|6900|1|1|50|0|x|o"]:
        with pytest.raises(ValueError):
            journal.parse_line(bad.split("|"))


def test_flat_to_flat_trades_with_scaling_and_a_reversal():
    fills = [fill(T, 1, 1, 6900.0, 1), fill(T + 60_000, 1, 1, 6898.0, 2),            # long 2, avg 6899
             fill(T + 120_000, -1, 1, 6905.0, 3), fill(T + 180_000, -1, 3, 6906.0, 4),  # out 1 @6905, out 1 @6906 + 2 short
             fill(T + 240_000, 1, 2, 6902.0, 5)]                                       # short covered @6902
    trs = journal.trades(fills)
    assert [t["side"] for t in trs] == ["long", "short"]
    lg, sh = trs
    assert lg["qty"] == 2 and lg["entry"] == 6899.0 and lg["exit"] == 6905.5 and lg["points"] == 6.5
    assert lg["gross"] == 6.5 * 50 * 2 and lg["fills"] == 4 and lg["exit_ts"] == T + 180_000
    assert abs(lg["commission"] - (2.04 * 2 + 2.04 + 2.04 * 1 / 3)) < 1e-9
    assert sh["entry"] == 6906.0 and sh["exit"] == 6902.0 and sh["gross"] == 4 * 50 * 2 and not sh["open"]
    st = journal.stats(trs)
    assert st["trades"] == 2 and st["wins"] == 2 and st["win_rate"] == 100 and st["days"][0]["trades"] == 2
    open_ = journal.trades([fill(T, -1, 1, 6900.0, 9)])
    assert open_[0]["open"] and open_[0]["net"] is None


def test_fills_stored_once_and_journal_api(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from chartsrv.app import app
    monkeypatch.setenv("CHART_DB", str(tmp_path / "chart.db"))
    monkeypatch.setenv("CHART_LEVELS_DIR", str(tmp_path))
    monkeypatch.setenv("CHART_FEED", "none")
    with TestClient(app) as c:
        m = app.state.market
        for f in [fill(T, 1, 1, 6900.0, 1), fill(T + 120_000, -1, 1, 6904.0, 2), fill(T + 120_000, -1, 1, 6904.0, 2)]:
            c.portal.call(m.on_fill, f)   # on the server's own thread, as the feed calls it
        c.portal.call(m.store.insert_rows_if_missing, "ES", [(T, 6900, 6906, 6898.5, 6903, 10, 5), (T + 60_000, 6903, 6904, 6899, 6904, 10, 5)])
        assert len(c.portal.call(m.store.fills, 0, T * 2)) == 2
        r = c.get(f"/api/journal?start={T - 1000}&end={T + 3_600_000}").json()
        t = r["trades"][0]
        assert r["accounts"] == ["Sim101"] and t["points"] == 4 and t["mfe"] == 6.0 and t["mae"] == 1.5
        assert c.get(f"/api/fills?start=0&end={T * 2}&symbol=es").json()["fills"][0]["exec_id"] == "x1"


def test_exit_and_new_entry_in_one_millisecond():
    """A strategy's exit and re-entry on one tick: two trades, whatever order their execution ids sort in."""
    fills = [fill(T, 1, 1, 6900.0, "c1"), fill(T + 60_000, -1, 1, 6910.0, "f9e2"), fill(T + 60_000, 1, 1, 6910.0, "0a7b"),
             fill(T + 120_000, -1, 1, 6905.0, "d4")]
    trs = journal.trades(fills)
    assert [(t["entry"], t["exit"], t["qty"]) for t in trs] == [(6900.0, 6910.0, 1), (6910.0, 6905.0, 1)]


def test_no_excursions_from_another_contract(tmp_path):
    """In the roll week the stored bars may be the other month: MAE and MFE are left out, not made up."""
    from chartsrv.store import Store
    st, t0 = Store(tmp_path / "c.db"), T - T % 60_000
    st.insert_bars_if_missing("ES", [Bar(t=t0 + i * 60_000, o=6950, h=6952, l=6948, c=6950, v=10) for i in range(3)])
    tr = journal.trades([fill(T, 1, 1, 6900.0, 1), fill(T + 120_000, -1, 1, 6901.0, 2)])[0]
    journal.excursions(tmp_path / "c.db", tr, 0.25)
    assert tr["mae"] is None and tr["mfe"] is None
    tr = journal.trades([fill(T, 1, 1, 6950.0, 1), fill(T + 120_000, -1, 1, 6951.0, 2)])[0]
    journal.excursions(tmp_path / "c.db", tr, 0.25)
    assert (tr["mae"], tr["mfe"]) == (2.0, 2.0)


def test_journal_finds_a_position_held_for_days(tmp_path, monkeypatch):
    """A trade open longer than a few days before the range starts: its earlier fills all count, back to where its
    account and contract were last flat, and another account's older fills don't."""
    from fastapi.testclient import TestClient
    from chartsrv.app import app
    from chartsrv.market import DAY_MS
    monkeypatch.setenv("CHART_DB", str(tmp_path / "chart.db"))
    monkeypatch.setenv("CHART_LEVELS_DIR", str(tmp_path))
    monkeypatch.setenv("CHART_FEED", "none")
    h = 3_600_000
    fills = [fill(T - 5 * DAY_MS, 1, 2, 5000.0, 1), fill(T - DAY_MS, 1, 1, 5010.0, 2), fill(T, -1, 3, 5020.0, 3),
             fill(T - 6 * DAY_MS, 1, 1, 5100.0, 4, "Sim102"), fill(T - 4 * DAY_MS, -1, 1, 5101.0, 5, "Sim102"),
             fill(T - 2 * h, 1, 2, 5005.0, 6, "Sim102"), fill(T - h // 2, -1, 2, 5007.0, 7, "Sim102")]
    with TestClient(app) as c:
        m = app.state.market
        for f in fills:
            c.portal.call(m.store.add_fill, f)
        r = c.get(f"/api/journal?start={T - 3 * h}&end={T + h}").json()
    got = [(t["account"], t["side"], t["qty"], round(t["entry"], 2), t["exit"], t["open"]) for t in r["trades"]]
    assert got == [("Sim101", "long", 3, 5003.33, 5020.0, False), ("Sim102", "long", 2, 5005.0, 5007.0, False)]
    assert r["stats"]["open"] == 0 and r["stats"]["trades"] == 2


def _journal(tmp_path, monkeypatch, fills, start, end):
    from fastapi.testclient import TestClient
    from chartsrv.app import app
    monkeypatch.setenv("CHART_DB", str(tmp_path / "chart.db"))
    monkeypatch.setenv("CHART_LEVELS_DIR", str(tmp_path))
    monkeypatch.setenv("CHART_FEED", "none")
    with TestClient(app) as c:
        m = app.state.market
        for f in fills:
            c.portal.call(m.store.add_fill, f)
        return c.get(f"/api/journal?start={start}&end={end}").json()


def test_journal_unseen_opening_fill_long_ago_does_not_reverse_later_trades(tmp_path, monkeypatch):
    """Installed while long 1: only the sell that closed it was stored, 20 days back. Today's clean long 2 is one
    trade, not a short closed by today's buy and a short left open by today's sell."""
    from chartsrv.market import DAY_MS
    h = 3_600_000
    fills = [fill(T - 20 * DAY_MS, -1, 1, 5000.0, 1), fill(T + h, 1, 2, 5010.0, 2), fill(T + 2 * h, -1, 2, 5020.0, 3)]
    r = _journal(tmp_path, monkeypatch, fills, T, T + DAY_MS)
    assert [(t["side"], t["qty"], t["entry"], t["exit"], t["open"]) for t in r["trades"]] == [("long", 2, 5010.0, 5020.0, False)]
    assert r["stats"]["open"] == 0 and round(r["stats"]["net"], 2) == 995.92


def test_journal_unseen_opening_fill_long_ago_does_not_hide_later_trades(tmp_path, monkeypatch):
    """Installed while long 2: only the sell 2 that closed it was stored, 10 days back, then a 1-lot round trip each
    day. Today shows today's trade, not nothing (all of them folded into one short open since the orphan)."""
    from chartsrv.market import DAY_MS
    h = 3_600_000
    fills = [fill(T - 10 * DAY_MS, -1, 2, 5000.0, 0)]
    for d in range(10, -1, -1):
        fills += [fill(T - d * DAY_MS + h, 1, 1, 5000.0, 100 + d), fill(T - d * DAY_MS + 2 * h, -1, 1, 5004.0, 200 + d)]
    r = _journal(tmp_path, monkeypatch, fills, T, T + DAY_MS)
    assert [(t["side"], t["qty"], t["points"], t["open"]) for t in r["trades"]] == [("long", 1, 4.0, False)]
    assert r["stats"]["trades"] == 1 and r["stats"]["open"] == 0
