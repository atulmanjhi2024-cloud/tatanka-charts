"""Gap fill: where the stored data stops, what the server asks NinjaTrader for, and which of the history it keeps
(only the stretch between the last stored trade and the first live one, nothing counted twice)."""
import asyncio
from datetime import datetime

from chartsrv import gapfill as gapfill_mod

from chartsrv.feeds.ninjatrader import NinjaTraderFeed
from chartsrv.models import Bar, Tick
from chartsrv.sessions import CT

from test_review_fixes import build_market


def ms(h, mi, s=0):
    return int(datetime(2026, 10, 14, h, mi, s, tzinfo=CT).timestamp() * 1000)


def stored(m):
    m.store.write([Tick("ES", ms(10, 0, 30), 7000.5, 2, 1)], [("ES", Bar(t=ms(10, 0), o=7000, h=7001, l=6999, c=7000.5, v=10, n=4))])


def test_history_fills_only_the_gap(tmp_path, monkeypatch):
    m, _ = build_market(tmp_path, monkeypatch, feed="ninjatrader")
    changed = []
    monkeypatch.setattr(m, "history_changed", changed.append)
    stored(m)
    asked = m.gapfill.requests(ms(10, 20))
    assert asked[0] == f"GAP|ES|{ms(10, 0, 30)}|{ms(10, 20)}"   # from where its data stops
    two_weeks = ms(10, 20) - 14 * 86_400_000                     # the others have none: their last 14 days
    assert asked[1:] == [f"GAP|{s}|{two_weeks}|{ms(10, 20)}" for s in m.instruments if s != "ES"] and len(asked) == len(m.instruments)
    m.gapfill.live("ES", ms(10, 19, 40))            # the add-on's buffer starts here: history must stop before it
    for t, px, size in [(ms(10, 0, 20), 6990, 5),   # before the stored data: dropped
                        (ms(10, 0, 45), 7002.0, 3),  # same minute as the stored data: merged into its bar
                        (ms(10, 5, 0), 7003.0, 1), (ms(10, 5, 30), 7004.25, 2), (ms(10, 5, 59), 7002.5, 1),
                        (ms(10, 19, 30), 7010.0, 1),  # the live feed's minute: the trade is stored, its bar left alone
                        (ms(10, 19, 50), 7011.0, 9)]:  # after the first live trade: dropped
        m.gapfill.tick("ES", t, px, size)
    m.gapfill.done("ES", "ticks", 7, "ok")
    ticks = m.store.db.execute("SELECT ts, price, size FROM ticks WHERE symbol='ES' ORDER BY ts").fetchall()
    assert [tuple(t) for t in ticks] == [(ms(10, 0, 30), 7000.5, 2), (ms(10, 0, 45), 7002.0, 3), (ms(10, 5), 7003.0, 1),
                                         (ms(10, 5, 30), 7004.25, 2), (ms(10, 5, 59), 7002.5, 1), (ms(10, 19, 30), 7010.0, 1)]
    bars = {r[0]: tuple(r[1:]) for r in m.store.db.execute("SELECT t,o,h,l,c,v,n FROM bars_1m WHERE symbol='ES'")}
    assert bars[ms(10, 0)] == (7000, 7002.0, 6999, 7002.0, 13, 5)           # merged with what was stored
    assert bars[ms(10, 5)] == (7003.0, 7004.25, 7002.5, 7002.5, 4, 3)
    assert ms(10, 19) not in bars and changed == ["ES"]
    st = m.gapfill.health()["ES"]
    assert st["state"] == "done" and st["stored"] == 5 and st["minutes"] == 2


def test_minute_bars_when_there_is_no_tick_history(tmp_path, monkeypatch):
    m, _ = build_market(tmp_path, monkeypatch, feed="ninjatrader")
    monkeypatch.setattr(m, "history_changed", lambda s: None)
    stored(m)
    m.gapfill.requests(ms(10, 20))
    m.gapfill.live("ES", ms(10, 20))   # the first live trade
    for t in (ms(10, 0), ms(10, 1), ms(10, 2), ms(10, 19), ms(10, 20)):   # 10:00 is stored already; 10:20 runs past the gap
        m.gapfill.minute("ES", t, 7001, 7002, 7000, 7001.5, 7)
    m.gapfill.done("ES", "minutes", 4, "no tick history")
    bars = {r[0]: r[1] for r in m.store.db.execute("SELECT t, v FROM bars_1m WHERE symbol='ES'")}
    assert bars == {ms(10, 0): 10, ms(10, 1): 7, ms(10, 2): 7, ms(10, 19): 7}


def test_the_feed_asks_on_connect_and_takes_the_answer(tmp_path, monkeypatch):
    monkeypatch.setattr(gapfill_mod, "MAX_GAP_MS", 10 ** 13)   # the stored data's day (Oct 14) stays within reach
    async def go():
        m, _ = build_market(tmp_path, monkeypatch, feed="ninjatrader")
        stored(m)
        feed = NinjaTraderFeed("127.0.0.1", 0, "")
        server = await asyncio.start_server(lambda r, w: feed._handle(m, r, w), "127.0.0.1", 0)
        r, w = await asyncio.open_connection("127.0.0.1", server.sockets[0].getsockname()[1])
        w.write(b"HELLO||1\n")
        await w.drain()
        before = []   # the I and R lines first: the add-on needs each future's feed and contract to answer a GAP
        while not (line := (await asyncio.wait_for(r.readline(), 3)).decode().strip()).startswith("GAP|"):
            before.append(line)
        assert before[0].startswith("I|") and {x.split("|")[1] for x in before[1:]} == set(before[0][2:].split(","))
        gap = line.split("|")
        start = int(gap[2])
        w.write(f"X|ES|{start + 5000}|7005.5|2\nZ|ES|ticks|1|ok\n".encode())
        await w.drain()
        await asyncio.sleep(0.3)
        w.close()
        server.close()
        return gap, m
    gap, m = asyncio.run(go())
    assert gap[:2] == ["GAP", "ES"] and int(gap[2]) == ms(10, 0, 30)
    assert m.gapfill.health()["ES"]["stored"] == 1
    assert m.store.db.execute("SELECT COUNT(*) FROM ticks WHERE symbol='ES'").fetchone()[0] == 2


def test_the_end_stays_open_until_the_first_live_trade(tmp_path, monkeypatch):
    """NinjaTrader starts the add-on before its data connection: asked at hello, it answers once connected, up to then."""
    m, _ = build_market(tmp_path, monkeypatch, feed="ninjatrader")
    monkeypatch.setattr(m, "history_changed", lambda s: None)
    stored(m)
    asked = m.gapfill.requests(ms(10, 20), live=True)
    assert asked[0] == f"GAP|ES|{ms(10, 0, 30)}|{ms(10, 20)}|live"   # add-on 5: up to its own now
    m.gapfill.tick("ES", ms(10, 25), 7005.0, 2)        # after hello, before the data connection gave a live trade: kept
    m.gapfill.tick("ES", ms(10, 25, 30), 7006.0, 1)
    m.gapfill.live("ES", ms(10, 26, 10))               # the first live trade
    m.gapfill.tick("ES", ms(10, 26, 30), 7007.0, 9)    # after it: dropped
    m.gapfill.done("ES", "ticks", 3, "ok")
    assert [r[0] for r in m.store.db.execute("SELECT ts FROM ticks WHERE symbol='ES' ORDER BY ts")] == [ms(10, 0, 30), ms(10, 25), ms(10, 25, 30)]
    bars = {r[0]: tuple(r[1:]) for r in m.store.db.execute("SELECT t,o,h,l,c,v FROM bars_1m WHERE symbol='ES'")}
    assert bars[ms(10, 25)] == (7005.0, 7006.0, 7005.0, 7006.0, 3) and ms(10, 26) not in bars
    assert m.gapfill.health()["ES"]["to"] == ms(10, 26, 10)


def _bars(m, sym, minutes, price=7000.0):
    m.store.write([], [(sym, Bar(t=t, o=price, h=price + 1, l=price - 1, c=price + 0.5, v=10, bv=6, sv=4, n=3)) for t in minutes])


def test_holes_inside_the_stored_data_are_asked_for_once(tmp_path, monkeypatch):
    m, _ = build_market(tmp_path, monkeypatch, feed="ninjatrader")
    changed = []
    monkeypatch.setattr(m, "history_changed", changed.append)
    _bars(m, "ES", [ms(9, k) for k in range(0, 10)] + [ms(9, k) for k in range(30, 60)] + [ms(10, k) for k in range(0, 36)])
    m.store.write([Tick("ES", ms(9, 9, 40), 7000.25, 1, 1), Tick("ES", ms(9, 30, 15), 7001.0, 2, -1)], [])
    _bars(m, "ES", [ms(15, 58), ms(15, 59), ms(17, 0), ms(17, 1)])   # the daily break: not a hole
    now = ms(17, 2, 20)   # 20 s after the stored data
    holes = m.gapfill.holes("ES", now)
    assert holes == [(ms(9, 9, 40), ms(9, 30)), (ms(10, 35) + 59_999, ms(15, 58))]   # its last trade to the next minute
    assert not any(x.startswith("GAP|ES|") for x in m.gapfill.requests(now))   # an add-on before 5: no holes
    line = next(x for x in m.gapfill.requests(now, live=True) if x.startswith("GAP|ES|"))
    assert line == f"GAP|ES|{ms(17, 2)}|{now}|live"   # the end first, however short (open until the first live trade)
    m.gapfill.done("ES", "ticks", 0, "ok")
    line = m.gapfill.next("ES")
    assert line == f"GAP|ES|{ms(9, 9, 40) - gapfill_mod.PROBE_MS}|{ms(9, 30)}"   # then its holes, one at a time
    for t, px, size in [(ms(9, 9, 50), 7002.0, 3),   # the hole's first minute, after the stored trade: merged
                        (ms(9, 15), 7003.0, 1), (ms(9, 15, 30), 7004.0, 2),
                        (ms(9, 30, 5), 6999.0, 4)]:  # the next stored minute: left as stored
        m.gapfill.tick("ES", t, px, size)
    m.gapfill.done("ES", "ticks", 4, "ok")
    bars = {r[0]: tuple(r[1:]) for r in m.store.db.execute("SELECT t,o,h,l,c,v,bv,sv,n FROM bars_1m WHERE symbol='ES'")}
    assert bars[ms(9, 9)] == (7000.0, 7002.0, 6999.0, 7002.0, 13, 6, 4, 4)     # stored first; its buy/sell split kept
    assert bars[ms(9, 15)] == (7003.0, 7004.0, 7003.0, 7004.0, 3, 0, 0, 2)
    assert bars[ms(9, 30)] == (7000.0, 7001.0, 6999.0, 7000.5, 10, 6, 4, 3)
    assert changed == []   # charts reload once its holes are done
    assert m.gapfill.next("ES") == f"GAP|ES|{ms(10, 35) + 59_999 - gapfill_mod.PROBE_MS}|{ms(15, 58)}"   # the next, once done
    m.gapfill.done("ES", "minutes", 0, "ok")
    assert changed == ["ES"]
    assert m.gapfill.holes("ES", now) == [] and m.gapfill.next("ES") is None   # each asked once
    assert [x for x in m.gapfill.requests(now, live=True) if x.startswith("GAP|ES|")] == [f"GAP|ES|{ms(17, 2)}|{now}|live"]


def test_a_hole_after_a_fill_cut_short_counts_nothing_twice(tmp_path, monkeypatch):
    m, _ = build_market(tmp_path, monkeypatch, feed="ninjatrader")
    monkeypatch.setattr(m, "history_changed", lambda s: None)
    _bars(m, "ES", [ms(9, k) for k in range(0, 10)] + [ms(9, k) for k in range(30, 40)])
    m.store.write([Tick("ES", ms(9, 12), 7001.0, 1, 0), Tick("ES", ms(9, 14, 30), 7002.0, 1, 0)], [])   # written, no bars
    (start, end), = m.gapfill.holes("ES", ms(9, 40))
    assert (start, end) == (ms(9, 14, 30), ms(9, 30))
    m.gapfill.open_hole("ES", start, end)
    m.gapfill.tick("ES", ms(9, 14, 30), 7002.0, 1)   # the same trade again: dropped
    m.gapfill.tick("ES", ms(9, 20), 7003.0, 5)
    m.gapfill.done("ES", "ticks", 2, "ok")
    assert m.store.db.execute("SELECT COUNT(*) FROM ticks WHERE symbol='ES'").fetchone()[0] == 3
    bars = {r[0]: r[1] for r in m.store.db.execute("SELECT t, v FROM bars_1m WHERE symbol='ES' AND t>? AND t<?", (ms(9, 9), ms(9, 30)))}
    assert bars == {ms(9, 12): 1, ms(9, 14): 1, ms(9, 20): 5}   # every trade stored has its bar


def test_a_thin_market_and_an_error_answer(tmp_path, monkeypatch):
    from dataclasses import replace
    m, _ = build_market(tmp_path, monkeypatch, feed="ninjatrader")
    monkeypatch.setattr(m, "history_changed", lambda s: None)
    _bars(m, "ES", [ms(9, 0), ms(9, 10)])
    m.instruments["ES"] = replace(m.instruments["ES"], quiet=True)   # thin: ten quiet minutes are normal there
    assert m.gapfill.holes("ES", ms(9, 20)) == []
    m.instruments["ES"] = replace(m.instruments["ES"], quiet=False)
    (start, end), = m.gapfill.holes("ES", ms(9, 20))
    m.gapfill.open_hole("ES", start, end)
    m.gapfill.done("ES", "error", 0, "no connection")   # asked again next time
    assert m.gapfill.holes("ES", ms(9, 20)) == [(start, end)]
    assert "ES" not in (m.store.get_setting(gapfill_mod.ASKED) or {})


def test_the_feed_sends_the_next_hole_when_one_is_done(tmp_path, monkeypatch, in_session):
    async def go():
        m, _ = build_market(tmp_path, monkeypatch, feed="ninjatrader")
        monkeypatch.setattr(m, "history_changed", lambda s: None)
        t0 = in_session // 60_000 * 60_000
        _bars(m, "ES", [t0 - k * 60_000 for k in range(1, 6)] + [t0 - k * 60_000 for k in range(20, 30)]
              + [t0 - k * 60_000 for k in range(45, 50)])   # two holes, nothing missing at the end
        feed = NinjaTraderFeed("127.0.0.1", 0, "")
        server = await asyncio.start_server(lambda r, w: feed._handle(m, r, w), "127.0.0.1", 0)
        r, w = await asyncio.open_connection("127.0.0.1", server.sockets[0].getsockname()[1])
        w.write(b"HELLO||6\n")
        await w.drain()
        lines = []
        while not (line := (await asyncio.wait_for(r.readline(), 3)).decode().strip()).startswith("W|"):
            lines.append(line)
        es = [x for x in lines if x.startswith("GAP|ES|")]
        others = [x for x in lines if x.startswith("GAP|") and not x.startswith("GAP|ES|")]
        nxt = []
        for _ in range(2):   # each Z brings the next
            w.write(b"Z|ES|ticks|0|ok\n")
            await w.drain()
            nxt.append((await asyncio.wait_for(r.readline(), 3)).decode().strip())
        w.close()
        server.close()
        return t0, es, others, nxt
    t0, es, others, nxt = asyncio.run(go())
    probe = gapfill_mod.PROBE_MS
    assert len(es) == 1 and es[0].startswith(f"GAP|ES|{t0}|") and es[0].endswith("|live")   # the end first, however short
    assert nxt == [f"GAP|ES|{t0 - 45 * 60_000 + 59_999 - probe}|{t0 - 29 * 60_000}",   # then the older hole
                   f"GAP|ES|{t0 - 20 * 60_000 + 59_999 - probe}|{t0 - 5 * 60_000}"]
    assert others and all(x.endswith("|live") for x in others)   # add-on 5: the end open, up to its own now


def test_no_hole_from_before_a_roll(tmp_path, monkeypatch):
    """History is the current contract's: a hole from before a roll would come at the other contract's prices."""
    async def go():
        m, _ = build_market(tmp_path, monkeypatch, feed="ninjatrader")
        monkeypatch.setattr(m, "history_changed", lambda s: None)
        _bars(m, "ES", [ms(9, 0), ms(9, 20), ms(9, 40), ms(10, 0)])
        assert len(m.gapfill.holes("ES", ms(10, 1))) == 3
        m.contracts["ES"] = "ES 12-26"
        start, end = m.gapfill.holes("ES", ms(10, 1))[0]
        m.gapfill.open_hole("ES", start, end)
        monkeypatch.setattr("chartsrv.market.now_ms", lambda: ms(9, 30))
        m.on_contract("ES", "ES 03-27")   # the roll, while that hole was asked
        m.gapfill.tick("ES", ms(9, 5), 7100.0, 1)
        m.gapfill.done("ES", "ticks", 1, "ok")
        return m
    m = asyncio.run(go())
    assert m.store.db.execute("SELECT COUNT(*) FROM ticks").fetchone()[0] == 0   # its answer: dropped
    assert m.gapfill.health()["ES"]["state"] == "dropped"
    assert m.gapfill.holes("ES", ms(10, 1)) == [(ms(9, 40) + 59_999, ms(10, 0))]   # only after the roll


def test_a_short_restart_and_the_minute_it_is_now(tmp_path, monkeypatch):
    m, _ = build_market(tmp_path, monkeypatch, feed="ninjatrader")
    monkeypatch.setattr(m, "history_changed", lambda s: None)
    stored(m)
    assert not any(x.startswith("GAP|ES|") for x in m.gapfill.requests(ms(10, 1, 30)))   # 60 s: too short for an old add-on
    assert m.gapfill.requests(ms(10, 1, 30), live=True)[0] == f"GAP|ES|{ms(10, 0, 30)}|{ms(10, 1, 30)}|live"
    m.gapfill.tick("ES", ms(10, 1, 10), 7003.0, 2)
    m.gapfill.tick("ES", ms(10, 1, 40), 7004.0, 1)   # the minute it is now, no live trade yet
    monkeypatch.setattr("chartsrv.gapfill.time.time", lambda: ms(10, 1, 50) / 1000)
    m.gapfill.done("ES", "ticks", 2, "ok")
    assert tuple(m.store.db.execute("SELECT o, c, v FROM bars_1m WHERE symbol='ES' AND t=?", (ms(10, 1),)).fetchone()) == (7003.0, 7004.0, 3)
    assert m.m1["ES"].last.t == ms(10, 1) and m.m1["ES"].last.v == 3   # the live feed goes on with it


def test_lines_for_a_plugins_future_are_not_ninjatraders(tmp_path, monkeypatch):
    async def go():
        m, _ = build_market(tmp_path, monkeypatch, feed="ninjatrader")
        monkeypatch.setattr(m, "history_changed", lambda s: None)
        m.trade_owner = {"ES": "ibkr"}
        m.gapfill.open_hole("ES", ms(9, 0), ms(9, 30))   # the plugin's window
        feed = NinjaTraderFeed("127.0.0.1", 0, "")
        feed._line(m, f"X|ES|{ms(9, 10)}|7000.0|3\n".encode())
        feed._line(m, b"Z|ES|ticks|1|ok\n")
        return m, feed
    m, feed = asyncio.run(go())
    assert "ES" in m.gapfill.windows and m.gapfill.windows["ES"].count == 0 and feed.stats["bad_lines"] == 0


def test_a_hole_whose_history_is_another_contracts_is_dropped(tmp_path, monkeypatch):
    """No roll recorded (one before this version): the prices just before the hole tell the contracts apart."""
    m, _ = build_market(tmp_path, monkeypatch, feed="ninjatrader")
    monkeypatch.setattr(m, "history_changed", lambda s: None)
    _bars(m, "ES", [ms(9, k) for k in range(0, 10)] + [ms(9, 30)])   # closes at 7000.5
    for price, kept in ((7042.75, False), (7000.75, True)):   # the next contract 42 points over / the same one
        (start, end), = m.gapfill.holes("ES", ms(9, 31))
        asked = m.gapfill.open_hole("ES", start, end)
        assert asked == (start - gapfill_mod.PROBE_MS, end)
        m.gapfill.tick("ES", ms(9, 7, 30), price, 1)          # before the hole: compared, not kept
        m.gapfill.tick("ES", ms(9, 8, 30), price - 0.25, 1)
        m.gapfill.tick("ES", ms(9, 15), price + 1, 2)         # inside
        m.gapfill.done("ES", "ticks", 3, "ok")
        n = m.store.db.execute("SELECT COUNT(*) FROM ticks WHERE symbol='ES'").fetchone()[0]
        assert n == (1 if kept else 0) and m.gapfill.health()["ES"]["state"] == ("done" if kept else "dropped")
        if not kept:
            assert m.gapfill.holes("ES", ms(9, 31)) == []   # not asked again: NinjaTrader has only the new one's
            m.store.set_setting(gapfill_mod.ASKED, {})       # (for the second round)


def test_a_roll_and_the_answers_already_coming(tmp_path, monkeypatch):
    async def go():
        m, _ = build_market(tmp_path, monkeypatch, feed="ninjatrader")
        reloads = []
        monkeypatch.setattr(m, "history_changed", reloads.append)
        m.contracts["ES"] = "ES 12-26"
        _bars(m, "ES", [ms(9, 0), ms(9, 20), ms(9, 40)])
        (start, end), _ = m.gapfill.holes("ES", ms(9, 41))
        m.gapfill.open_hole("ES", start, end)
        m.gapfill.tick("ES", ms(9, 5), 7000.0, 1)   # its answer has begun: asked on the old contract, kept
        monkeypatch.setattr("chartsrv.market.now_ms", lambda: ms(9, 41))
        m.on_contract("ES", "ES 03-27")
        m.gapfill.done("ES", "ticks", 1, "ok")
        kept = m.store.db.execute("SELECT COUNT(*) FROM ticks WHERE symbol='ES'").fetchone()[0]
        # the gap at the end, a roll while it is asked: the new contract's history only from the next session open
        m.gapfill.requests(ms(16, 30), live=True)
        m.on_contract("ES", "ES 06-27")
        m.gapfill.tick("ES", ms(15, 0), 7100.0, 1)            # the old session: not merged into the old contract's
        m.gapfill.tick("ES", ms(16, 0) + 3_600_000, 7101.0, 1)  # 17:00, the new session
        m.gapfill.done("ES", "ticks", 2, "ok")
        return m, kept, reloads
    m, kept, reloads = asyncio.run(go())
    assert kept == 1
    assert [r[0] for r in m.store.db.execute("SELECT ts FROM ticks WHERE symbol='ES' ORDER BY ts")] == [ms(9, 5), ms(17, 0)]
    assert reloads == ["ES", "ES"]   # the gap at the end reloads at its own answer, not after the holes


def test_a_hole_with_only_trades_before_it_is_asked_again_for_minutes(tmp_path, monkeypatch):
    m, _ = build_market(tmp_path, monkeypatch, feed="ninjatrader")
    monkeypatch.setattr(m, "history_changed", lambda s: None)
    _bars(m, "ES", [ms(9, k) for k in range(0, 10)] + [ms(9, 30)])
    m.gapfill.queue["ES"] = m.gapfill.holes("ES", ms(9, 31))
    start, end = m.gapfill.queue["ES"][0]
    assert m.gapfill.next("ES") == f"GAP|ES|{start - gapfill_mod.PROBE_MS}|{end}"
    m.gapfill.tick("ES", ms(9, 8, 30), 7000.5, 1)   # NinjaTrader's own copy of the trades before the hole, none in it
    m.gapfill.done("ES", "ticks", 1, "ok")
    assert m.gapfill.next("ES") == f"GAP|ES|{start}|{end}"   # again, from its start: the add-on falls back to minutes
    m.gapfill.minute("ES", ms(9, 15), 7001, 7002, 7000, 7001.5, 7)
    m.gapfill.done("ES", "minutes", 1, "ok")
    assert m.store.db.execute("SELECT v FROM bars_1m WHERE symbol='ES' AND t=?", (ms(9, 15),)).fetchone()[0] == 7
    assert m.gapfill.holes("ES", ms(9, 31)) == []


def test_what_the_trades_left_out_is_asked_as_minutes(tmp_path, monkeypatch):
    """NinjaTrader had trades only from when it was back (its own recording): the rest comes as 1-minute bars."""
    m, _ = build_market(tmp_path, monkeypatch, feed="ninjatrader")
    monkeypatch.setattr(m, "history_changed", lambda s: None)
    _bars(m, "ES", [ms(4, k) for k in range(0, 46)] + [ms(8, 32), ms(8, 33)])
    now = ms(8, 34, 20)
    lines = [x for x in m.gapfill.requests(now, live=True, minutes=True) if x.startswith("GAP|ES|")]
    assert lines == [f"GAP|ES|{ms(8, 34)}|{now}|live"]   # the end, however short, first
    m.gapfill.done("ES", "ticks", 0, "ok")
    start = ms(4, 45) + 59_999
    assert m.gapfill.next("ES") == f"GAP|ES|{start - gapfill_mod.PROBE_MS}|{ms(8, 32)}"
    for k in range(0, 32):   # trades from 8:00 only
        m.gapfill.tick("ES", ms(8, k, 10), 7000.0 + k, 1)
    m.gapfill.done("ES", "ticks", 32, "ok")
    assert m.gapfill.next("ES") == f"GAP|ES|{start}|{ms(8, 0)}|minutes"   # 4:46 to 8:00 again, as minutes
    m.gapfill.minute("ES", ms(6, 0), 6990, 6995, 6985, 6992, 40)
    m.gapfill.done("ES", "minutes", 1, "ok")
    assert m.gapfill.next("ES") is None
    assert m.store.db.execute("SELECT v FROM bars_1m WHERE symbol='ES' AND t=?", (ms(6, 0),)).fetchone()[0] == 40
    # an add-on before 7 gets no minutes request: nothing more is asked
    m.gapfill.requests(now, live=True)
    assert not m.gapfill.minutes_ok


def test_add_on_8_reaches_back_90_days_across_rolls(tmp_path, monkeypatch):
    """It fills each contract of the stretch from its own: a 2-month absence comes back whole, as 1-minute bars."""
    m, _ = build_market(tmp_path, monkeypatch, feed="ninjatrader")
    monkeypatch.setattr(m, "history_changed", lambda s: None)
    _bars(m, "ES", [ms(9, 0)])
    now = ms(9, 0) + 60 * 86_400_000
    old = next(x for x in m.gapfill.requests(now, live=True, minutes=True) if x.startswith("GAP|ES|"))
    assert old.split("|")[2] == str(now - gapfill_mod.MAX_GAP_MS)   # add-on 7: 14 days
    line = next(x for x in m.gapfill.requests(now, live=True, minutes=True, contracts=True) if x.startswith("GAP|ES|"))
    assert line == f"GAP|ES|{ms(9, 1)}|{now}|live"   # add-on 8: from where the data stops, 60 days back
    m.contracts["ES"] = "ES SEP26"

    async def roll_then_answer():
        m.on_contract("ES", "ES DEC26")   # a roll at its start: the add-on's answer is already each contract's, all kept
        for k in range(1, 4):
            m.gapfill.minute("ES", ms(9, 0) + k * 86_400_000, 7000, 7001, 6999, 7000.5, 5)
        m.gapfill.done("ES", "minutes", 3, "ok")
    asyncio.run(roll_then_answer())
    assert m.store.db.execute("SELECT COUNT(*) FROM bars_1m WHERE symbol='ES'").fetchone()[0] == 4


def test_a_plugins_gap_across_a_roll_keeps_only_the_new_session(tmp_path, monkeypatch):
    """A futures plugin answers its gap from the contract it is on now (as ibkr_trades does): off across a roll, its
    gap keeps only what comes from the next session open, also when NinjaTrader's add-on 8+ (it fills each contract
    from its own) supplies the other futures."""
    from chartsrv.feeds.sdk import FeedPlugin

    def at(d, h, mi, s=0):
        return int(datetime(2026, 12, d, h, mi, s, tzinfo=CT).timestamp() * 1000)

    class P(FeedPlugin):
        KIND = "futures"

        async def run(self, market):
            self.contract(market, "ES", "ES 03-27")   # the front month now: it rolled while the feed was off
            self.connected()
            await asyncio.Event().wait()

        async def history(self, symbol, a, b):
            self.asked = a
            for t, px in ((at(10, 15, 30), 6900.5), (at(14, 8, 0), 6910.0)):   # 03-27's prices, in the old session too
                if a < t < b:
                    yield ("bar", t, px, px, px, px, 10)

    async def go():
        m, _ = build_market(tmp_path, monkeypatch, feed="ninjatrader")
        monkeypatch.setattr(m, "history_changed", lambda s: None)
        m.contracts["ES"] = "ES 12-26"
        m.store.write([Tick("ES", at(10, 15, 0, 30), 6850.0, 1, 1)], [("ES", Bar(t=at(10, 15, 0), o=6850, h=6850, l=6850, c=6850, v=1, n=1))])
        m.gapfill.contracts_ok = True   # an add-on 8+ said hello (it supplies the other futures)
        m.trade_owner = {"ES": "p"}
        feed = P("p", {}, ["ES"])
        monkeypatch.setattr("time.time", lambda: at(14, 9, 0) / 1000)
        task = asyncio.create_task(feed.serve(m))
        for _ in range(40):
            await asyncio.sleep(0.05)
            if m.gapfill.health().get("ES", {}).get("state") == "done":
                break
        task.cancel()
        return m, feed
    m, feed = asyncio.run(go())
    assert feed.asked == at(10, 17, 0) - 1   # from Thursday's 17:00 open, the first session on 03-27
    bars = m.store.db.execute("SELECT t, c FROM bars_1m WHERE symbol='ES' ORDER BY t").fetchall()
    assert [tuple(b) for b in bars] == [(at(10, 15, 0), 6850.0), (at(14, 8, 0), 6910.0)]


def test_a_gap_fill_cut_short_gets_its_bars_at_the_next_one(tmp_path, monkeypatch):
    """The connection drops after the first chunks of a gap's trades were written, before Z (NinjaTrader or the server
    restarted, the market shut so no live trade came after them): the next connect's fill starts at the last trade
    written and builds the bars of those trades too, the minute it was cut in from both fills' trades."""
    m, _ = build_market(tmp_path, monkeypatch, feed="ninjatrader")
    monkeypatch.setattr(m, "history_changed", lambda s: None)
    monkeypatch.setattr(gapfill_mod, "CHUNK", 50)
    stored(m)
    trades = [(ms(10, 1) + k * 5000, 7001.0 + k % 4, 1 + k % 3) for k in range(120)]   # 10:01:00 to 10:10:55
    m.gapfill.requests(ms(12, 0), live=True)
    for t, px, size in trades:
        m.gapfill.tick("ES", t, px, size)
        if t == ms(10, 9, 15):
            break   # cut: the first 100 written (two chunks), no bars
    line = next(x for x in m.gapfill.requests(ms(12, 2), live=True) if x.startswith("GAP|ES|"))
    assert line == f"GAP|ES|{ms(10, 9, 15)}|{ms(12, 2)}|live" and m.gapfill.queue["ES"] == []   # no hole seen
    for t, px, size in trades:   # the stretch again, from the last trade written
        m.gapfill.tick("ES", t, px, size)
    m.gapfill.live("ES", ms(12, 1, 30))
    m.gapfill.done("ES", "ticks", 120, "ok")
    vol = dict(m.store.db.execute("SELECT ts / 60000 * 60000, SUM(size) FROM ticks WHERE symbol='ES' AND ts>=? GROUP BY 1",
                                  (ms(10, 1),)).fetchall())
    bars = {r[0]: tuple(r[1:]) for r in m.store.db.execute("SELECT t, o, c, v, n FROM bars_1m WHERE symbol='ES' AND t>=?",
                                                            (ms(10, 1),))}
    assert len(vol) == 10 and {k: b[2] for k, b in bars.items()} == vol
    cut = [x for x in trades if ms(10, 9) <= x[0] < ms(10, 10)]
    assert bars[ms(10, 9)] == (cut[0][1], cut[-1][1], sum(x[2] for x in cut), 12)


def test_a_failed_chunk_write_is_tried_again_a_chunk_later(tmp_path, monkeypatch):
    """The database not writable while a gap's trades come (a full disk, an import's lock past the busy timeout):
    the connection goes on, and the chunk is kept and tried again a chunk later, not on every line after it (each try
    can block for the busy timeout); once the database is back, nothing is lost."""
    import sqlite3
    m, _ = build_market(tmp_path, monkeypatch, feed="ninjatrader")
    monkeypatch.setattr(m, "history_changed", lambda s: None)
    monkeypatch.setattr(gapfill_mod, "CHUNK", 50)
    stored(m)
    m.gapfill.requests(ms(12, 0), live=True)
    real, tries = m.store.write, []

    def write(ticks, bars):
        tries.append(len(ticks))
        if len(tries) == 1:
            raise sqlite3.OperationalError("database is locked")
        real(ticks, bars)
    monkeypatch.setattr(m.store, "write", write)
    feed = NinjaTraderFeed("127.0.0.1", 0, "")
    for k in range(120):
        feed._line(m, f"X|ES|{ms(10, 1) + k * 1000}|7001.0|1\n".encode())
    assert tries == [50, 100] and feed.stats["bad_lines"] == 0
    m.gapfill.live("ES", ms(11, 0))
    feed._line(m, b"Z|ES|ticks|120|ok\n")
    assert m.store.db.execute("SELECT COUNT(*) FROM ticks WHERE symbol='ES' AND ts>=?", (ms(10, 1),)).fetchone()[0] == 120
