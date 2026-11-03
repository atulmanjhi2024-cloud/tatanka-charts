"""The IBKR futures plugin's old contract names: it named CL, NG, RB, HO, MCL and BZ by the month of their last trading
day ("CL 11-26" for CLZ6) and now by their contract month ("CL 12-26"). The first start after the update renames the
stored old name once, without a roll; every real roll, then and later, is still one."""
import asyncio
import sys
import types
from datetime import datetime, timezone

from chartsrv import backup
from chartsrv import connections as conns
from chartsrv import market as market_mod
from chartsrv.feeds.sdk import FeedPlugin
from chartsrv.gapfill import ROLLED
from chartsrv.models import Bar, Tick
from chartsrv.plugins import BUILTIN_DIR, Plugins
from chartsrv.sessions import CT
from chartsrv.store import Store

from test_feed_plugins import _Event
from test_review_fixes import build_market

# IBKR's contract details for crude: conId -> (last trading day, contract month)
CLZ6, CLF7 = 1, 2
DETAILS = {CLZ6: ("20261120", "202612"), CLF7: ("20261218", "202701")}


def at(h, mi, s=0):
    return int(datetime(2026, 10, 20, h, mi, s, tzinfo=CT).timestamp() * 1000)   # a Tuesday


def fake_ibkr(monkeypatch, front: list):
    """A stand-in for ib_async: IBKR's front month for CL is front[0]; its 1-minute bars are 10:01-10:09 at 60."""
    fake, asked = types.ModuleType("ib_async"), []

    class Contract:
        def __init__(self, **kw):
            self.__dict__.update(kw)

    class IB:
        def __init__(self):
            self.disconnectedEvent, self.pendingTickersEvent = _Event(), _Event()

        async def connectAsync(self, *a, **kw):
            pass

        def reqMarketDataType(self, t):
            pass

        async def qualifyContractsAsync(self, c):
            return [Contract(conId=front[0], exchange=c.exchange)]

        async def reqContractDetailsAsync(self, c):
            last, month = DETAILS[c.conId]
            c.lastTradeDateOrContractMonth = last
            return [types.SimpleNamespace(contract=c, contractMonth=month)]

        def reqTickByTickData(self, c, kind):
            return types.SimpleNamespace(tickByTicks=[], contract=c)

        async def reqHistoricalDataAsync(self, c, end, *a, **kw):
            asked.append(c.conId)
            return [types.SimpleNamespace(date=datetime.fromtimestamp(t / 1000, timezone.utc), open=60.0, high=60.0,
                                          low=60.0, close=60.0, volume=10) for t in range(at(10, 1), at(10, 10), 60_000)]

        def disconnect(self):
            pass

    fake.IB, fake.Contract, fake.Future, fake.StartupFetch = IB, Contract, Contract, lambda v: v
    monkeypatch.setitem(sys.modules, "ib_async", fake)
    return asked


def old_install(tmp_path, contracts: dict, ibkr_symbols: str = "CL"):
    """The database of an install that ran the plugin before the update: its contract names, and an IBKR futures
    connection on for `ibkr_symbols`."""
    conns.register_plugins(Plugins([BUILTIN_DIR], conns.builtin_ids()))   # what the app does at start
    s = Store(tmp_path / "chart.db")
    s.set_setting("contracts", contracts)
    s.set_setting(conns.SETTING, {"ibkr_trades": {"enabled": True, "host": "gw", "port": 4002, "client_id": 77,
                                                  "symbols": ibkr_symbols}})
    s.write([Tick("CL", at(10, 0, 30), 60.0, 1, 1)], [("CL", Bar(t=at(10, 0), o=60, h=60, l=60, c=60, v=1, n=1))])
    s.close()


async def serve_once(m, feed):
    """The plugin started as the app starts it, until its gap is filled (or it settles), then stopped."""
    task = asyncio.create_task(feed.serve(m))
    for _ in range(80):
        await asyncio.sleep(0.05)
        if m.gapfill.health().get("CL", {}).get("state") not in (None, "asked"):
            break
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    await asyncio.sleep(0.05)   # a roll's message is sent in a task of its own


def ibkr(m):
    """The built-in plugin as the app makes it for that connection, supplying CL."""
    cls = Plugins([BUILTIN_DIR], set()).types["ibkr_trades"]
    return cls("ibkr_trades", {"host": "gw", "port": 4002, "client_id": 77}, ["CL"])


def test_the_first_start_after_the_update_renames_the_old_name_and_fills_the_restarts_gap(tmp_path, monkeypatch):
    """CL stored as "CL 11-26" by the plugin before the update. The update restarts the server at 10:00; the plugin
    reconnects at 10:10 on the same CLZ6, now "CL 12-26": no "Contract roll" message, the settlement kept, and the
    gap from 10:00:30 asked and filled. CLZ6 rolling to CLF7 later is a roll as ever, although CLF7's old name
    ("CL 12-26") is the name now stored."""
    front = [CLZ6]
    asked = fake_ibkr(monkeypatch, front)
    old_install(tmp_path, {"CL": "CL 11-26"})
    m, notifier = build_market(tmp_path, monkeypatch, feed="ninjatrader")
    assert m.trade_owner == {"CL": "ibkr_trades"}
    monkeypatch.setattr(m, "history_changed", lambda s: None)
    dropped = []
    monkeypatch.setattr(m.settlements, "rolled", dropped.append)
    monkeypatch.setattr("time.time", lambda: at(10, 10) / 1000)

    asyncio.run(serve_once(m, ibkr(m)))
    assert notifier.sent == [] and dropped == []
    assert m.contracts == {"CL": "CL 12-26"} and m.store.get_setting("contracts") == {"CL": "CL 12-26"}
    assert "CL" not in (m.store.get_setting(ROLLED) or {})
    assert m.gapfill.status["CL"]["from"] == at(10, 0, 30) and asked == [CLZ6]   # from where its data stopped
    assert m.store.db.execute("SELECT COUNT(*) FROM bars_1m WHERE symbol='CL'").fetchone()[0] == 10
    assert m.store.get_setting(market_mod.OLD_NAMES) == {}   # used up: once only

    front[0] = CLF7   # weeks later: IBKR rolls to January
    monkeypatch.setattr("time.time", lambda: at(10, 30) / 1000)
    asyncio.run(serve_once(m, ibkr(m)))
    assert notifier.sent == [("Contract roll", "CL is now CL 01-27 (was CL 12-26). Earlier bars are not adjusted for the roll.")]
    assert m.contracts == {"CL": "CL 01-27"} and dropped == ["CL"] and m.store.get_setting(ROLLED)["CL"] == at(10, 30)


def test_a_roll_while_the_server_was_updated_is_still_a_roll(tmp_path, monkeypatch):
    """Stored "CL 11-26" (CLZ6 by its last trading day), but IBKR rolled to CLF7 while the server was down: CLF7 is
    "CL 01-27", its old name "CL 12-26". Not the stored contract: a roll, told and handled as one."""
    fake_ibkr(monkeypatch, [CLF7])
    old_install(tmp_path, {"CL": "CL 11-26"})
    m, notifier = build_market(tmp_path, monkeypatch, feed="ninjatrader")
    monkeypatch.setattr(m, "history_changed", lambda s: None)
    monkeypatch.setattr("time.time", lambda: at(10, 10) / 1000)
    asyncio.run(serve_once(m, ibkr(m)))
    assert notifier.sent == [("Contract roll", "CL is now CL 01-27 (was CL 11-26). Earlier bars are not adjusted for the roll.")]
    assert m.contracts == {"CL": "CL 01-27"} and m.store.get_setting(ROLLED)["CL"] == at(10, 10)


def test_the_first_report_uses_the_old_name_up_even_when_nothing_changed(tmp_path, monkeypatch):
    """Stored "CL 12-26" for CLZ6 already (the plugin since 8de0ce8, or NinjaTrader's name): the first report after
    the update is the same, and the old naming is done with. The roll to CLF7 (old name "CL 12-26") is a roll."""
    front = [CLZ6]
    fake_ibkr(monkeypatch, front)
    old_install(tmp_path, {"CL": "CL 12-26"})
    m, notifier = build_market(tmp_path, monkeypatch, feed="ninjatrader")
    assert m.old_names == {"CL": "CL 12-26"}
    monkeypatch.setattr(m, "history_changed", lambda s: None)
    monkeypatch.setattr("time.time", lambda: at(10, 10) / 1000)
    asyncio.run(serve_once(m, ibkr(m)))
    assert notifier.sent == [] and m.old_names == {} and m.store.get_setting(market_mod.OLD_NAMES) == {}
    front[0] = CLF7
    asyncio.run(serve_once(m, ibkr(m)))
    assert [t for t, _ in notifier.sent] == ["Contract roll"] and m.contracts == {"CL": "CL 01-27"}


def test_only_what_the_plugin_supplied_at_the_update_and_only_once(tmp_path, monkeypatch):
    """The names are noted once, at the first start after the update, for the instruments an IBKR futures connection
    supplied then. CL came from NinjaTrader ("CL 12-26", CLZ6): moved to the plugin after IBKR rolled to CLF7, whose
    old name is "CL 12-26", it is a roll. A restart does not note them again; a fresh install notes none."""
    fake_ibkr(monkeypatch, [CLF7])
    old_install(tmp_path, {"CL": "CL 12-26", "ES": "ES 12-26", "GC": "GC 12-26"}, ibkr_symbols="ES")
    m, notifier = build_market(tmp_path, monkeypatch, feed="ninjatrader")
    assert m.old_names == {"ES": "ES 12-26"} and m.store.get_setting(market_mod.OLD_NAMES) == {"ES": "ES 12-26"}

    m.connections.put("ibkr_trades", {"symbols": "ES,CL"})   # CL moved to the plugin
    m.trade_owner = m.connections.owners()
    monkeypatch.setattr(m, "history_changed", lambda s: None)
    monkeypatch.setattr("time.time", lambda: at(10, 10) / 1000)
    asyncio.run(serve_once(m, ibkr(m)))
    assert notifier.sent == [("Contract roll", "CL is now CL 01-27 (was CL 12-26). Earlier bars are not adjusted for the roll.")]
    m.store.close()

    again, _ = build_market(tmp_path, monkeypatch, feed="ninjatrader")   # restarted: nothing noted again
    assert again.trade_owner == {"ES": "ibkr_trades", "CL": "ibkr_trades"} and again.old_names == {"ES": "ES 12-26"}
    again.store.close()

    (tmp_path / "fresh").mkdir()
    fresh, _ = build_market(tmp_path / "fresh", monkeypatch, feed="ninjatrader")
    assert fresh.old_names == {} and fresh.store.get_setting(market_mod.OLD_NAMES) == {}


def test_a_restore_keeps_the_old_names_as_they_are(tmp_path, monkeypatch):
    """The noted names describe the contracts stored in this database, which a restore keeps: a backup from before
    the update does not note them again over names already renamed."""
    m, _ = build_market(tmp_path, monkeypatch, CHART_BACKUP_DIR=str(tmp_path / "bk"))
    s = m.store
    s.db.execute("DELETE FROM settings WHERE key=?", (market_mod.OLD_NAMES,))
    s.db.commit()
    before = backup.make(s.path, tmp_path / "bk")   # made before the update: nothing noted in it
    s.set_setting(market_mod.OLD_NAMES, {"NQ": "NQ 12-26"})
    s.close()
    backup.restore(s.path, before)
    assert Store(s.path).get_setting(market_mod.OLD_NAMES) == {"NQ": "NQ 12-26"}


def test_plugins_that_pass_no_old_name_work_as_before(tmp_path, monkeypatch):
    """A plugin written to the SDK before `was` (or one passing the name itself) calls on_contract with two arguments,
    as a stand-in market or an older server takes them; against the app, a change is a roll as ever."""
    calls = []
    stand_in = types.SimpleNamespace(instruments={"CL": object()}, trade_owner={"CL": "p"},
                                     on_contract=lambda sym, name: calls.append((sym, name)))
    p = FeedPlugin("p", {}, ["CL"])
    p.contract(stand_in, "CL", "CL 12-26")
    p.contract(stand_in, "CL", " CL 12-26 ", was="CL 12-26")
    assert calls == [("CL", "CL 12-26"), ("CL", "CL 12-26")]

    old_install(tmp_path, {"CL": "CL 11-26"})
    m, notifier = build_market(tmp_path, monkeypatch, feed="ninjatrader")
    m.trade_owner = {"CL": "p"}

    async def go():
        p.contract(m, "CL", "CL 12-26")   # no old name given: the noted one is used up, and it is a roll
        await asyncio.sleep(0.01)
    asyncio.run(go())
    assert [t for t, _ in notifier.sent] == ["Contract roll"] and m.old_names == {}
