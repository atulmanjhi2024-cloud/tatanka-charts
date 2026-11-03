"""Footprint: each bar's volume per price, split into trades that hit the bid (sellers) and lifted the ask
(buyers), computed on demand from stored trades for the bars a chart shows. A trade with no aggressor side
takes the tick rule: an uptick is a buy, a downtick a sell, an unchanged price the side before it."""
from bisect import bisect_left, bisect_right

MAX_BARS = 400


def compute(ticks, starts: list[int], end: int, tick_size: float, vols: list[float] | None = None,
            pre: int = 0, bucket=None) -> list[dict]:
    """ticks sorted by time. Bar i holds the trades in [starts[i], starts[i + 1]), the last one up to `end`; given
    `bucket` (a D chart's TimeSeries.bucket: the open of the bar a trade is in), the ones it puts at starts[i], also
    from before that open (a holiday halt's session, in the next day's bar).
    With `vols` (each bar's volume: tick, volume, range and Renko bars, where one print or one millisecond's sweep can
    finish several, so bars can open at the same time), a trade at the very time a bar opens is shared out in order:
    the bar before takes what it still lacks of its volume, then each bar opening then, the last of them the rest.
    `pre`: the first bar's (Bar.pre), the contracts at its open time that went to bars before the ones asked for,
    which are left out. Rows are [price index (price / tick size), bid volume, ask volume], low to high."""
    rows: list[dict[int, list[int]]] = [{} for _ in starts]
    room = list(vols) if vols else None
    last_px, last_side = None, 1
    skip = pre
    minute = at = None

    def add(i, price, side, size):
        r = rows[i].setdefault(round(price / tick_size), [0, 0])
        r[0 if side < 0 else 1] += size

    for tk in ticks:
        side = tk.side or (1 if last_px is None or tk.price > last_px else -1 if tk.price < last_px else last_side)
        last_px, last_side = tk.price, side
        if bucket is None:
            at = tk.ts
        elif tk.ts // 60_000 != minute:   # the bar a trade is in changes on a minute at most
            minute, at = tk.ts // 60_000, bucket(tk.ts)
        i = bisect_right(starts, at) - 1
        if i < 0 or tk.ts >= end or (bucket is not None and starts[i] != at):
            continue
        left = tk.size
        if skip and tk.ts == starts[0]:   # the first ones at the first bar's open: an earlier bar's
            n = min(skip, left)
            skip -= n
            left -= n
        if room is not None:
            if starts[i] == tk.ts:
                for j in range(max(bisect_left(starts, tk.ts) - 1, 0), i):
                    take = min(left, max(room[j], 0))
                    if take:
                        add(j, tk.price, side, take)
                        room[j] -= take
                        left -= take
            room[i] -= left
        if left:
            add(i, tk.price, side, left)
    return [{"t": t, "rows": [[k, v[0], v[1]] for k, v in sorted(r.items())]} for t, r in zip(starts, rows)]
