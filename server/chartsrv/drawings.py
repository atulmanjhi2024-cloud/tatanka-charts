"""Chart drawings (horizontal and vertical lines, rays, trend lines, arrows, parallel channels, rectangles, fib
retracements and trend-based extensions, long and short positions, price and date ranges, text), stored per symbol so
every device shows the same ones. Points are (epoch ms, price), so a drawing sits at the same place on every
timeframe. Lines, rays and trend lines can carry an alert: a `line` alert in the alert engine, kept in step
with the drawing (moved with it, removed with it), at the level the line is drawn at on the chart it was switched on
from."""
import math
import re
import time
from datetime import timedelta

from . import sessions
from .bars import calendar_bucket

TYPES = {"hline": 1, "hray": 1, "trend": 2, "rect": 2, "fib": 2, "text": 1,   # type -> number of points
         "vline": 1, "arrow": 2, "channel": 3, "fibext": 3, "long": 3, "short": 3, "prange": 2, "drange": 2}
ALERTABLE = ("hline", "hray", "trend")
DIRECTIONS = ("cross", "above", "below")
DASHES = ("solid", "dashed", "dotted")
MAX_PER_SYMBOL = 1000
ID = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
COLOR = re.compile(r"^#[0-9a-fA-F]{6}$")

# style key -> check
_STYLE = {
    "color": lambda v: isinstance(v, str) and COLOR.match(v),
    "textColor": lambda v: isinstance(v, str) and COLOR.match(v),
    "fill": lambda v: isinstance(v, str) and COLOR.match(v),
    "width": lambda v: _int(v, 1, 8),
    "fontSize": lambda v: _int(v, 8, 48),
    "qty": lambda v: _int(v, 1, 100_000),
    "fillOpacity": lambda v: _int(v, 0, 100),
    "dash": lambda v: v in DASHES,
    "text": lambda v: isinstance(v, str) and len(v) <= 500,
    "group": lambda v: isinstance(v, str) and len(v) <= 20,
    "extendLeft": lambda v: isinstance(v, bool),
    "extendRight": lambda v: isinstance(v, bool),
    "mid": lambda v: isinstance(v, bool),
    "showPrice": lambda v: isinstance(v, bool),
    "reverse": lambda v: isinstance(v, bool),
    "background": lambda v: isinstance(v, bool),
    "locked": lambda v: isinstance(v, bool),
    "hidden": lambda v: isinstance(v, bool),
    "tab": lambda v: isinstance(v, str) and ID.match(v),   # shown on that chart tab only
    "levels": lambda v: isinstance(v, list) and len(v) <= 24 and all(_num(x) and -10 <= x <= 10 for x in v),
}


# How the chart a trend line's alert was switched on from lays the line out, if not as the minute charts do (drawings.js
# lineLayout): months, an nM chart's n (its bars a calendar month or group of months each); clock, a W chart's bars,
# even on the clock (an M chart's too, before months came in); per, a seconds, minute or hour chart's bar length (one
# that fits the hour evenly); log, a logarithmic price scale.
LAYOUT = {
    "months": lambda v: _int(v, 1, 12),
    "clock": lambda v: isinstance(v, bool),
    "per": lambda v: _int(v, 1, 3_600_000) and 3_600_000 % v == 0,
    "log": lambda v: isinstance(v, bool),
}


def _int(v, lo, hi):
    return isinstance(v, int) and not isinstance(v, bool) and lo <= v <= hi


def _num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def clean(d: dict, drawing_id: str) -> dict:
    """The drawing as stored; raises ValueError on anything malformed."""
    if not ID.fullmatch(drawing_id):
        raise ValueError("bad drawing id")
    if not isinstance(d, dict):
        raise ValueError("a drawing is an object")
    kind = d.get("type")
    if not isinstance(kind, str) or kind not in TYPES:
        raise ValueError(f"type must be one of {', '.join(TYPES)}")
    pts = d.get("pts")
    if not isinstance(pts, list) or len(pts) != TYPES[kind]:
        raise ValueError(f"{kind} needs {TYPES[kind]} point(s)")
    clean_pts = []
    for p in pts:
        if not isinstance(p, dict) or not _num(p.get("t")) or not _num(p.get("p")) or not -4e12 < p["t"] < 4e12:
            raise ValueError("points are {t: epoch ms, p: price}")
        t = p["t"]   # hundredths of a ms tell apart range or Renko bars opened in the same ms (drawings.js)
        clean_pts.append({"t": int(t) if t == int(t) else round(float(t), 4), "p": float(p["p"])})
    style = d.get("style") or {}
    if not isinstance(style, dict):
        raise ValueError("style must be an object")
    for k, v in style.items():
        check = _STYLE.get(k)
        if check is None or not check(v):
            raise ValueError(f"bad style {k}")
    alert = d.get("alert") or {}
    if not isinstance(alert, dict) or set(alert) - {"on", "direction", "repeat", *LAYOUT}:
        raise ValueError("alert is {on, direction, repeat} and how the chart it was switched on from lays the line out")
    for k, check in LAYOUT.items():
        if k in alert and not check(alert[k]):
            raise ValueError(f"bad alert {k}")
    alert = {"on": bool(alert.get("on")), "direction": alert.get("direction", "cross"), "repeat": bool(alert.get("repeat")),
             **{k: alert[k] for k in LAYOUT if alert.get(k)}}
    if alert["direction"] not in DIRECTIONS:
        raise ValueError(f"alert direction must be one of {', '.join(DIRECTIONS)}")
    if alert["on"] and kind not in ALERTABLE:
        raise ValueError("alerts go on lines, rays and trend lines")
    return {"id": drawing_id, "type": kind, "pts": clean_pts, "style": style, "alert": alert,
            "updated": int(time.time() * 1000)}


def alert_params(d: dict) -> dict | None:
    """The `line` alert a drawing asks for, or None."""
    if not d["alert"]["on"] or d["type"] not in ALERTABLE:
        return None
    a = d["pts"][0]
    if d["type"] == "hline":
        line = {"t1": 0, "p1": a["p"], "t2": 1, "p2": a["p"], "left": True, "right": True}
    elif d["type"] == "hray":
        line = {"t1": a["t"], "p1": a["p"], "t2": a["t"] + 1, "p2": a["p"], "left": False, "right": True}
    else:
        b = d["pts"][1]
        a, b = (b, a) if b["t"] < a["t"] else (a, b)     # extendLeft/Right are screen sides: earlier, later
        s = d["style"]
        line = {"t1": a["t"], "p1": a["p"], "t2": b["t"], "p2": b["p"], "left": bool(s.get("extendLeft")),
                "right": bool(s.get("extendRight")), **{k: v for k, v in d["alert"].items() if k in LAYOUT}}
    return {**line, "type": d["type"], "direction": d["alert"]["direction"], "drawing": d["id"]}


def month_bars(ts: float, n: int) -> float:
    """Where an nM chart puts ts, in bars (drawings.js monthBars): the index of the bar holding it, in groups of n months
    from January of year 0 (bars.calendar_bucket), and how far into that bar, as its time scale goes (timeToLogical: at
    n x 31 days a bar, so at most 1)."""
    trade = (sessions.to_ct(ts) + timedelta(hours=7)).date()   # a 17:00 open trades the next day
    into = (ts - calendar_bucket(ts, "M", n)) / (n * 31 * 86_400_000)
    return (trade.year * 12 + trade.month - 1) // n + min(1.0, into)


def line_price(params: dict, ts: int, group: str | None = None) -> float | None:
    """The line's price at time ts, or None where it is not drawn (before or after its ends, or vertical). The chart
    draws it straight in bars, which come only while the market trades, so it runs straight in market time
    (sessions.market_ms, with the holidays of `group`, the instrument's): over the daily break, a weekend or a
    holiday it stays where the bars leave it. An alert switched on from a chart that lays the line out otherwise
    (LAYOUT) keeps to that chart's line: in its bars, a bar a holiday closure cuts short counted whole (per); in an M
    chart's bars, each a calendar month or group of months (months); by the clock, as W bars run (clock); straight in
    log price, as a logarithmic scale draws it (log)."""
    t1, p1, t2, p2 = params["t1"], params["p1"], params["t2"], params["p2"]
    if t2 == t1:
        return None
    if params.get("clock"):
        x, x1, x2 = ts, t1, t2
    elif params.get("months"):
        x, x1, x2 = (month_bars(t, params["months"]) for t in (ts, t1, t2))
    else:
        x, x1, x2 = (sessions.market_ms(t, group, params.get("per")) for t in (ts, t1, t2))
    if x < x1 and not params.get("left") or x > x2 and not params.get("right"):
        return None
    if p1 == p2:   # a level line or ray: its price wherever it is drawn
        return p1
    if x2 == x1:   # both ends in one closure (a weekend): upright on the chart
        return None
    k = (x - x1) / (x2 - x1)
    if params.get("log") and p1 > 0 and p2 > 0:   # a price at or below 0 has no log: such a line keeps straight
        e = k * math.log(p2 / p1)
        return p1 * math.exp(e) if e < 700 else math.inf   # risen past a float: a level no price reaches
    return p1 + (p2 - p1) * k
