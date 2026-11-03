"""Your NinjaTrader fills (executions), as the TatankaChartFeed add-on reports them, read-only: markers on the chart
and a trade journal. A trade runs from flat to flat on one account and contract (scaling in and out stays one trade;
a reversal closes one and opens the next), with average entry and exit, P&L in points and dollars (NinjaTrader's
point value) less commissions, MAE and MFE from the stored 1-minute bars, and per-day totals.

Add-on 13 also sends NinjaTrader's own positions (P lines): known points where a fill the app never got (a position
opened before the add-on was installed, a fill made while NinjaTrader was off) is put right, so it can't skew the
trades after it."""
import math
import re
import time
from collections import defaultdict
from datetime import timedelta

from . import sessions
from .store import iter_bars_1m

FIELDS = ("account", "symbol", "contract", "ts", "price", "qty", "side", "point_value", "commission", "exec_id", "order_id", "name")
UNSAFE = re.compile(r"[^\w .:/+#@()&,\[\]=%'-]")


def parse_line(p: list[str]) -> dict:
    """E|account|root|contract|utc ms|price|qty|side|point value|commission|execution id|order id|name -> a fill."""
    if len(p) != 13:
        raise ValueError("E needs 12 fields")
    f = {"account": p[1], "symbol": p[2], "contract": p[3], "ts": int(p[4]), "price": float(p[5]), "qty": int(p[6]),
         "side": int(p[7]), "point_value": float(p[8]), "commission": float(p[9] or 0), "exec_id": p[10],
         "order_id": p[11], "name": p[12]}
    if f["side"] not in (-1, 1) or f["qty"] <= 0 or not f["price"] > 0 or not f["exec_id"] or f["point_value"] < 0:
        raise ValueError("bad fill")
    for k in ("account", "symbol", "contract", "exec_id", "order_id", "name"):   # any order name: kept, made plain
        f[k] = UNSAFE.sub("_", f[k])[:80]
    return f


def parse_position(p: list[str]) -> dict:
    """P|account|root|contract|utc ms|position (+ long, - short, 0 flat)|average price|point value -> a position."""
    if len(p) != 8:
        raise ValueError("P needs 7 fields")
    q = {"account": p[1], "symbol": p[2], "contract": p[3], "ts": int(p[4]), "qty": int(p[5]), "avg_price": float(p[6]),
         "point_value": float(p[7])}
    if q["ts"] <= 0 or not math.isfinite(q["point_value"]) or q["point_value"] < 0 or not math.isfinite(q["avg_price"]) \
            or (q["qty"] and not q["avg_price"] > 0):
        raise ValueError("bad position")
    for k in ("account", "symbol", "contract"):
        q[k] = UNSAFE.sub("_", q[k])[:80]
        if not q[k]:
            raise ValueError("bad position")
    if not q["qty"]:
        q["avg_price"] = 0.0
    return q


def parse_flat(p: list[str]) -> tuple[str, int]:
    """P|account|utc ms -> (account, ms): each of its contracts without a position at that moment is flat."""
    acct, ts = UNSAFE.sub("_", p[1])[:80], int(p[2])
    if not acct or ts <= 0:
        raise ValueError("bad flat")
    return acct, ts


def trades(fills: list[dict], positions: list[dict] = ()) -> list[dict]:
    """Flat-to-flat trades from fills (any order), oldest first. positions: NinjaTrader's own (add-on 13), each its
    account and contract once the fills up to its time are in. Where the fills say otherwise (one the app never got),
    the trade open there ends there, with no exit price or P&L (unseen_exit), and a position goes on from there as
    NinjaTrader has it: a trade at its average price (from_position)."""
    out, open_ = [], {}
    for f in _in_order(fills, positions, open_):
        key = (f["account"], f["contract"])
        if "exec_id" not in f:   # one of NinjaTrader's positions
            _known(f, open_, out)
            continue
        t = open_.get(key)
        qty = f["qty"]
        while qty:
            if t is None:
                t = open_[key] = {"account": f["account"], "symbol": f["symbol"], "contract": f["contract"],
                                  "side": f["side"], "entry_ts": f["ts"], "pos": 0, "max_qty": 0, "in_qty": 0, "in_px": 0.0,
                                  "out_qty": 0, "out_px": 0.0, "commission": 0.0, "point_value": f["point_value"], "fills": [],
                                  "first_price": f["price"]}
            if f["side"] == t["side"]:   # adding
                t["pos"] += qty
                t["in_qty"] += qty
                t["in_px"] += qty * f["price"]
                t["max_qty"] = max(t["max_qty"], t["pos"])
                t["commission"] += f["commission"] * qty / f["qty"]
                t["fills"].append(f["exec_id"])
                qty = 0
            else:                        # reducing, maybe through flat
                take = min(qty, t["pos"])
                t["pos"] -= take
                t["out_qty"] += take
                t["out_px"] += take * f["price"]
                t["commission"] += f["commission"] * take / f["qty"]
                if f["exec_id"] not in t["fills"]:
                    t["fills"].append(f["exec_id"])
                qty -= take
                if t["pos"] == 0:
                    t["exit_ts"], t["last_price"] = f["ts"], f["price"]
                    out.append(_finish(t))
                    t = open_[key] = None
    for t in open_.values():
        if t is not None:
            out.append(_finish(t, open_=True))
    return sorted(out, key=lambda x: x["entry_ts"])


def _in_order(fills: list[dict], positions, open_: dict):
    """By time; fills in the same millisecond (a strategy's exit and new entry on one tick) close what is open
    before they open more, and a position comes after the fills of its millisecond. `open_` is the caller's open
    trades, as they are when each millisecond comes up."""
    fills = sorted(fills, key=lambda x: (x["ts"], x["exec_id"]))
    positions = sorted(positions, key=lambda x: x["ts"])
    i = k = 0
    while i < len(fills):
        while k < len(positions) and positions[k]["ts"] < fills[i]["ts"]:
            yield positions[k]
            k += 1
        j = i
        while j < len(fills) and fills[j]["ts"] == fills[i]["ts"]:
            j += 1
        group = fills[i:j]
        if len(group) > 1:
            def adds(f):
                t = open_.get((f["account"], f["contract"]))
                return t is None or t["side"] == f["side"]
            group.sort(key=adds)   # stable: exits first, then the rest by execution id
        yield from group
        i = j
    yield from positions[k:]


def _known(p: dict, open_: dict, out: list) -> None:
    """One of NinjaTrader's positions: nothing changes where the fills agree with it."""
    key = (p["account"], p["contract"])
    t = open_.get(key)
    if (t["side"] * t["pos"] if t else 0) == p["qty"]:
        return
    if t is not None:   # it ended (or changed) with a fill the app never got: no exit price
        t["exit_ts"] = p["ts"]
        out.append(_finish(t, unseen=True))
    open_[key] = None if not p["qty"] else {
        "account": p["account"], "symbol": p["symbol"], "contract": p["contract"], "side": 1 if p["qty"] > 0 else -1,
        "entry_ts": p["ts"], "pos": abs(p["qty"]), "max_qty": abs(p["qty"]), "in_qty": abs(p["qty"]),
        "in_px": abs(p["qty"]) * p["avg_price"], "out_qty": 0, "out_px": 0.0, "commission": 0.0,
        "point_value": p["point_value"], "fills": [], "first_price": p["avg_price"], "from_position": True}


def _finish(t: dict, open_: bool = False, unseen: bool = False) -> dict:
    entry = t["in_px"] / t["in_qty"]
    exit_ = t["out_px"] / t["out_qty"] if t["out_qty"] and not unseen else None
    pts = None if exit_ is None else (exit_ - entry) * t["side"]
    gross = None if pts is None else pts * t["point_value"] * t["out_qty"]
    return {"account": t["account"], "symbol": t["symbol"], "contract": t["contract"],
            "side": "long" if t["side"] > 0 else "short", "qty": t["max_qty"], "entry_ts": t["entry_ts"],
            "exit_ts": None if open_ else t["exit_ts"], "entry": entry, "exit": exit_, "points": pts,
            "gross": gross, "commission": round(t["commission"], 2),
            "net": None if gross is None else gross - t["commission"], "open": open_, "fills": len(t["fills"]),
            "first_price": t["first_price"], "last_price": t.get("last_price"),
            "from_position": t.get("from_position", False), "unseen_exit": unseen}


def excursions(db_path, tr: dict, tick: float, db=None, until: int | None = None, rolled: int = 0) -> None:
    """MAE and MFE (points, against and for the trade) from the stored 1-minute bars it spanned, one still open up to
    `until` (the range's end; now if None), read on db (a connection open on db_path, when given: one for all of a
    journal's trades). None for a trade from one of NinjaTrader's positions (it began before the app knew of it) or
    ended by a fill never received (its exit is unknown: the position NinjaTrader reported next may be hours later),
    and where the stored bars are another contract month (they are not adjusted for the roll): its first or last
    fill's price not in that minute's bar, or held across the chart's last contract roll (`rolled`, ms; 0: none)."""
    end = tr["exit_ts"] or until or int(time.time() * 1000)
    if tr.get("from_position") or tr.get("unseen_exit") or tr["entry_ts"] < rolled <= end:
        tr["mae"] = tr["mfe"] = None
        return
    start = tr["entry_ts"] - tr["entry_ts"] % 60_000
    last = tr["exit_ts"] - tr["exit_ts"] % 60_000 if tr["exit_ts"] else None
    hi = lo = None
    for b in iter_bars_1m(db_path, tr["symbol"], start, end + 1, db):
        if any(b.t == at and not b.l - 4 * tick <= px <= b.h + 4 * tick
               for at, px in ((start, tr["first_price"]), (last, tr["last_price"]))):
            hi = None   # the stored bars are another contract month there (the roll): no excursions from them
            break
        hi = b.h if hi is None else max(hi, b.h)
        lo = b.l if lo is None else min(lo, b.l)
    if hi is None:
        tr["mae"] = tr["mfe"] = None
        return
    up, down = hi - tr["entry"], tr["entry"] - lo
    fav, adv = (up, down) if tr["side"] == "long" else (down, up)
    tr["mfe"], tr["mae"] = round(max(0.0, fav) / tick) * tick, round(max(0.0, adv) / tick) * tick


def stats(trs: list[dict]) -> dict:
    done = [t for t in trs if not t["open"] and t["net"] is not None]
    wins, losses = [t["net"] for t in done if t["net"] > 0], [t["net"] for t in done if t["net"] <= 0]
    days = defaultdict(lambda: {"net": 0.0, "trades": 0})
    for t in done:
        d = (sessions.to_ct(t["exit_ts"]) + timedelta(hours=7)).date().isoformat()   # the trade date (17:00 CT open)
        days[d]["net"] += t["net"]
        days[d]["trades"] += 1
    return {"trades": len(done), "open": sum(1 for t in trs if t["open"]), "net": sum(t["net"] for t in done),
            "commission": sum(t["commission"] for t in done), "wins": len(wins), "losses": len(losses),
            "win_rate": len(wins) / len(done) * 100 if done else None,
            "avg_win": sum(wins) / len(wins) if wins else None, "avg_loss": sum(losses) / len(losses) if losses else None,
            "profit_factor": sum(wins) / -sum(losses) if losses and sum(losses) < 0 else None,
            "largest_win": max(wins) if wins else None, "largest_loss": min(losses) if losses else None,
            "days": [{"date": d, **v} for d, v in sorted(days.items())]}
