# Copyright (c) 2026 Tatanka Trading LLC.
# This Source Code Form is subject to the Mozilla Public License, v. 2.0.
# If a copy of the MPL was not distributed with this file, obtain one at https://mozilla.org/MPL/2.0/.
"""Feeds the AMT engine the way the NinjaTrader indicator does (OnBarUpdate/Commit/Preview in
TatankaAmtToolkit.cs): N-minute chart bars (1-30) aligned to the 17:00 CT open, each with the 1-minute bars
inside it, completed 30-minute bars for the prior-month profile, and completed daily bars for ATR. A chart
bar is committed when the next one starts; the forming bar is previewed on an engine clone."""
import bisect
import time
from collections import deque
from datetime import datetime, timedelta

from .amt import (DMIN, Bar, DailyHistory, Engine, Frame, LevelAlertTracker, LineItem, Settings, levels_from_prices,
                  trading_date, valid, week)
from .models import Bar as MinuteBar
from . import sessions
from .sessions import CT, SessionClock

MACRO_MINUTES = 30
HISTORY_DAYS_KEPT = 20      # past days' IB / prior-day lines kept on the chart (as the C# Commit)
SAMPLE_DAYS_KEPT = 20
PD_TRADE_DAYS = 30          # prior-day value from trades for days this recent (older ones: past the lines kept)
TRADE_DAYS_KEPT = 8         # the current Session or Weekly profile's trades, per minute, kept this long


def ct(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000, CT).replace(tzinfo=None)


def epoch_ms(t: datetime) -> int:
    return int(t.replace(tzinfo=CT).timestamp() * 1000)


class _Buckets:
    """1-minute bars grouped into session-aligned N-minute buckets (chart bars or 30-minute macro bars)."""

    def __init__(self, minutes: int):
        self.period = minutes * 60_000
        self.clock = SessionClock()
        self.keys: list[int] = []
        self.bars: dict[int, list[MinuteBar]] = {}

    def key(self, t: int) -> int:
        s = self.clock.locate(t)
        return s + (t - s) // self.period * self.period

    def add(self, m: MinuteBar) -> int:
        k = self.key(m.t)
        lst = self.bars.get(k)
        if lst is None:
            lst = self.bars[k] = []
            if self.keys and k < self.keys[-1]:
                bisect.insort(self.keys, k)
            else:
                self.keys.append(k)
        if lst and lst[-1].t == m.t:
            lst[-1] = m
        elif not lst or m.t > lst[-1].t:
            lst.append(m)
        else:   # an older minute arriving late: keep order
            i = next(i for i, x in enumerate(lst) if x.t >= m.t)
            if lst[i].t == m.t:
                lst[i] = m
            else:
                lst.insert(i, m)
        return k

    def drop_before(self, k: int):
        i = bisect.bisect_left(self.keys, k)
        for old in self.keys[:i]:
            del self.bars[old]
        del self.keys[:i]


def _engine_bar(minutes: list[MinuteBar], start_ms: int, size: int, index: int = 0) -> Bar:
    start = ct(start_ms)
    return Bar(start, start + timedelta(minutes=size), minutes[0].o, max(m.h for m in minutes),
               min(m.l for m in minutes), minutes[-1].c, float(sum(m.v for m in minutes)), index)


def _sub(m: MinuteBar) -> Bar:
    s = ct(m.t)
    return Bar(s, s + timedelta(minutes=1), m.o, m.h, m.l, m.c, float(m.v))


class AmtRunner:
    def __init__(self, symbol: str, tick: float, minutes: int, settings: Settings, record: list | None = None,
                 calendar: str | None = None, rth_session: str = "0830-1500"):
        """calendar, rth_session: the instrument's CME holiday group (a holiday halt's short session folds into the
        next trade date) and regular hours; None and ES's as the NinjaTrader original."""
        if not 1 <= minutes <= 30:
            raise ValueError("the AMT Toolkit runs on 1-30 minute bars")
        self.symbol, self.tick, self.minutes, self.S = symbol, tick, minutes, settings
        self.calendar, self.rth_session = calendar, rth_session
        self.engine: Engine | None = None
        self.daily = DailyHistory(settings.daily_atr_length)
        self.days: dict[datetime, list[float]] = {}      # trading date -> [high, low, close]
        self.day_keys: list[datetime] = []
        self.daily_through = DMIN
        self.chart = _Buckets(minutes)
        self.macro = _Buckets(MACRO_MINUTES)
        self.last_macro = -1
        self.next_commit = 0          # position in chart.keys of the next bar to commit
        self.index = -1
        self.prev_frame: Frame | None = None
        self.history: list = []       # past days' IB / prior-day lines, as LineItems
        self.history_rev = 0
        self.samples: deque = deque()  # (start, end, vah, val, confirmed) where the 80% Rule fill shows
        self.samples_total = 0
        self.samples_rev = 0          # bumps on every change, including a run growing
        self.vwap: deque = deque()    # (bar start, RTH VWAP) of committed bars, while the RTH VWAP is drawn
        self.vwap_total = 0           # points ever added: a client that knows n of them gets the rest
        self.frame: Frame | None = None
        self.past_rev = 0
        self._past_keys: tuple = ()
        self.levels = None            # list[Level] or None
        self.levels_date: datetime | None = None
        self.levels_error = ""
        self.tracker = LevelAlertTracker()
        self.sent: set[str] = set()
        self.record = record          # input log for the C# comparison test
        # the prior day's value from its trades: RTH window (ms) -> [(price, volume)], [] with nothing stored; the
        # windows asked for and not known yet (the hub works them out, then loads the runner again)
        self.pd_values: dict[tuple[int, int], list] = {}
        self.pd_missing: set[tuple[int, int]] = set()
        # the current Session or Weekly profile from its trades (v1.2.2): minute start (ms) -> price -> volume, stored
        # ones from the period's start (load_trades) then live (add_trade)
        self.minute_trades: dict[int, dict[float, float]] = {}
        self.commit_visuals = False   # tests: draw every committed frame too

    # --- inputs ----------------------------------------------------------------------------------------
    def set_levels(self, pivot: float | None, others: list[float], date: datetime | None, ref_price: float | None):
        """Daily levels in the paste-line form: pivot first, any prices after it."""
        self.levels_error = ""
        levels = None
        if pivot is not None:
            try:
                if ref_price and valid(ref_price) and abs(pivot / ref_price - 1) > .2:
                    raise ValueError(f"Pivot {pivot!r} is more than 20% from the chart price {ref_price!r}; "
                                     "these look like another symbol's levels.")
                levels = levels_from_prices(pivot, others)
            except ValueError as e:
                self.levels_error = str(e)
        self.levels, self.levels_date = levels, date
        if self.engine is not None:
            self.engine.ReplaceLevels(levels or [])
            if self.record is not None:
                self.record.append(("L", [(x.Name, x.Price, x.Kind) for x in levels or []]))

    def set_level_list(self, levels: list, date: datetime | None, ref_price: float | None):
        """Daily levels already parsed (daily_levels.py). One more than 20% from the chart price is another symbol's:
        the last valid levels stay, as NinjaTrader keeps them."""
        pivot = next((x for x in levels if x.Kind == 0), None)
        if pivot is not None and ref_price and valid(ref_price) and abs(pivot.Price / ref_price - 1) > .2:
            self.levels_error = (f"Pivot {pivot.Price!r} is more than 20% from the chart price {ref_price!r}; "
                                 "these look like another symbol's levels. Last valid levels retained.")
            return
        if levels or not self.levels_error.startswith("Pasted levels rejected"):
            self.levels_error = ""
        # the same levels again (after a load, a levels poll) keep the flips history gave them: NinjaTrader
        # replaces them only when the file changes
        same = [(x.Name, x.Price, x.Kind) for x in levels] == [(x.Name, x.Price, x.Kind) for x in self.levels or []]
        self.levels, self.levels_date = (levels or None), date
        if self.engine is not None and not same:
            self.engine.ReplaceLevels(levels or [])
            if self.record is not None:
                self.record.append(("L", [(x.Name, x.Price, x.Kind) for x in levels or []]))

    def add_minute(self, m: MinuteBar):
        self.chart.add(m)
        self.macro.add(m)
        td = self.trade_date(ct(m.t))
        d = self.days.get(td)
        if d is None:
            self.days[td] = [m.h, m.l, m.c]
            self.day_keys.append(td)
            if len(self.day_keys) > 1 and td < self.day_keys[-2]:
                self.day_keys.sort()
        else:
            d[0], d[1], d[2] = max(d[0], m.h), min(d[1], m.l), m.c

    def trade_date(self, t: datetime) -> datetime:
        d = trading_date(t)
        f = sessions.fold_trade_date(d.date(), self.calendar) if self.calendar else d.date()
        return d if f == d.date() else datetime(f.year, f.month, f.day)

    # --- trades: the current Session or Weekly profile -------------------------------------------------------
    def period_start(self, now_ms: int) -> int | None:
        """The first minute (ms) of the profile current at now_ms when it is drawn from trades (Session, Weekly), else
        None: the week's Sunday 17:00 open, or the Session Time window that began last."""
        S = self.S
        if not S.mvp_master or S.mvp_tf_mode not in ("Session", "Weekly"):
            return None
        now = ct(now_ms)
        if S.mvp_tf_mode == "Weekly":
            w = week(self.trade_date(now))
            return epoch_ms(w - timedelta(hours=7))
        rng = S.mvp_sess_time.split(":")[0].split("-")
        a = int(rng[0][:2]) * 60 + int(rng[0][2:4])
        start = datetime(now.year, now.month, now.day) + timedelta(minutes=a)
        if start > now:
            start -= timedelta(days=1)
        return epoch_ms(start)

    def wants_trades(self) -> bool:
        return self.S.mvp_master and self.S.mvp_tf_mode in ("Session", "Weekly")

    def add_trade(self, ts: int, price: float, size: float):
        """A trade (live, or stored: one minute's volume at a price)."""
        if size <= 0:
            return
        k = ts // 60_000 * 60_000
        m = self.minute_trades.get(k)
        if m is None:
            m = self.minute_trades[k] = {}
            if len(self.minute_trades) % 500 == 0:
                cut = k - TRADE_DAYS_KEPT * 86_400_000
                for old in [x for x in self.minute_trades if x < cut]:
                    del self.minute_trades[old]
        m[price] = m.get(price, 0.0) + size

    def _minute_rows(self, start: datetime, volume: float):
        """Engine.MinuteTrades: the minute's trades when they add up to its 1-minute bar's volume, else None."""
        m = self.minute_trades.get(epoch_ms(start))
        if not m or volume <= 0:
            return None
        total = sum(m.values())
        return sorted(m.items()) if volume * .99 <= total <= volume * 1.01 else None

    # --- driving the engine ------------------------------------------------------------------------------
    def _feed_daily(self, td: datetime):
        keys = self.day_keys   # sorted: start after the last day fed, not at the first (90 days, every bar)
        for i in range(bisect.bisect_right(keys, self.daily_through), len(keys)):
            k = keys[i]
            if k >= td:
                break
            if k > self.daily_through:
                h, l, c = self.days[k]
                self.daily.Add(k, h, l, c)
                self.daily_through = k
                if self.record is not None:
                    self.record.append(("D", k, h, l, c))

    def _drain_macro(self, through: datetime):
        through_ms = epoch_ms(through)
        keys = self.macro.keys   # sorted: start after the last bar fed
        for i in range(bisect.bisect_right(keys, self.last_macro), len(keys)):
            k = keys[i]
            if k + MACRO_MINUTES * 60_000 > through_ms:
                break
            if k > self.last_macro:
                mb = _engine_bar(self.macro.bars[k], k, MACRO_MINUTES)
                self.engine.AddMacro(mb)
                self.last_macro = k
                if self.record is not None:
                    self.record.append(("M", mb))

    def _process(self, engine: Engine, key: int, index: int, visuals: bool) -> Frame:
        mins = self.chart.bars[key]
        b = _engine_bar(mins, key, self.minutes, index)
        subs = [_sub(m) for m in mins]
        self._feed_daily(self.trade_date(b.Start))
        if self.record is not None:
            self.record.append(("B", b, subs, visuals))
        f = engine.Process(b, subs, self.daily.Before(self.trade_date(b.Start)), visuals)
        if not subs:
            f.Warning += " One-minute data unavailable; using chart-bar profile fallback."
        return f

    def _ensure_engine(self):
        if self.engine is None and self.chart.keys:
            first = self.chart.bars[self.chart.keys[0]]
            self.engine = Engine(self.S, self.symbol, self.tick, first[-1].c, self.minutes)
            self.engine.Calendar, self.engine.RthSession = self.calendar, self.rth_session
            self.engine.TradeValue = self.trade_value
            self.engine.MinuteTrades = self._minute_rows
            if self.record is not None:
                self.record.append(("E", self.symbol, self.tick, first[-1].c, self.minutes))
            if self.levels:
                self.engine.ReplaceLevels(self.levels)
                if self.record is not None:
                    self.record.append(("L", [(x.Name, x.Price, x.Kind) for x in self.levels]))

    def trade_value(self, start: datetime, end: datetime):
        """Engine.TradeValue: the window's trades if worked out; a recent one not yet is noted for the hub."""
        key = (epoch_ms(start), epoch_ms(end))
        rows = self.pd_values.get(key)
        if rows is None and key[1] > (time.time() - PD_TRADE_DAYS * 86_400) * 1000:
            self.pd_missing.add(key)
        return rows or None

    def commit_ready(self) -> list:
        """Commits every chart bar except the newest. Committed frames are never drawn (the preview is), so
        they skip the drawing lists; their notices are returned."""
        self._ensure_engine()
        notices = []
        keys = self.chart.keys
        while self.next_commit < len(keys) - 1:
            key = keys[self.next_commit]
            self.index += 1
            end = ct(key) + timedelta(minutes=self.minutes)
            self._drain_macro(end)
            f = self._process(self.engine, key, self.index, self.commit_visuals)
            self._keep_history(f)
            if f.RuleActive or f.RuleConfirmed:
                s0, e0 = epoch_ms(f.Bar.Start), epoch_ms(f.Bar.End)
                last = self.samples[-1] if self.samples else None
                if last and last[1] == s0 and last[2:] == (f.VaHigh, f.VaLow, f.RuleConfirmed):
                    self.samples[-1] = (last[0], e0) + last[2:]     # extend the run: one block, not one per bar
                else:
                    self.samples.append((s0, e0, f.VaHigh, f.VaLow, f.RuleConfirmed))
                self.samples_total += 1
                self.samples_rev += 1
            cutoff = epoch_ms(f.Bar.Start) - SAMPLE_DAYS_KEPT * 86_400_000
            while self.samples and self.samples[0][1] < cutoff:
                self.samples.popleft()
            if self.S.vwap_enable and valid(f.RthVwap):
                self.vwap.append((epoch_ms(f.Bar.Start), f.RthVwap))
                self.vwap_total += 1
            while self.vwap and self.vwap[0][0] < cutoff:
                self.vwap.popleft()
            self.prev_frame = f
            # as C# Commit: the ones this bar's previews already sent are not sent again, then `sent` starts over
            notices.extend((f.Bar.Index, n) for n in f.Notices if f"{f.Bar.Index}:{n.Key}" not in self.sent)
            self.next_commit += 1
            self.sent.clear()
        # free minutes no longer needed (keep the uncommitted bar and a day of macro context)
        if self.next_commit > 2:
            self.chart.drop_before(keys[self.next_commit - 1])
            self.next_commit = 1
        if self.macro.keys and self.last_macro > 0:
            self.macro.drop_before(self.last_macro)
        return notices

    def _keep_history(self, f: Frame):
        """C# Commit: keep each finished day's IB and prior-day lines. A pd line that switched at the close (it starts
        there from then on) is kept as it stood over the day before it, at once."""
        prev = self.prev_frame
        same_day = prev is not None and self.trade_date(prev.Bar.Start) == self.trade_date(f.Bar.Start)
        keep = []
        if same_day:
            for x in (l for l in prev.Lines if l.Name.startswith("pd")):
                y = next((l for l in f.Lines if l.Name == x.Name), None)
                if y is not None and y.Start != x.Start:
                    keep.append(x)
        elif prev is not None:
            keep = [x for x in prev.Lines if x.Name.startswith("IB ") or x.Name == "dOpen" or x.Name.startswith("pd")]
        for x in keep:   # unlabeled copies of name, price, span, color, width and style, as in C#
            c = LineItem()
            c.Name, c.Group, c.Price, c.Start, c.End, c.Color, c.Width, c.Style = x.Name, None, x.Price, x.Start, x.End, x.Color, x.Width, x.Style
            c.Offset, c.Size, c.Extend, c.Label, c.TextColor = 2, "Normal", "None", False, 0
            self.history.append(c)
        if keep:
            self.history = [x for x in self.history if (f.Bar.Start - x.End).total_seconds() / 86400 <= HISTORY_DAYS_KEPT]
            self.history_rev += 1

    def preview(self) -> tuple[Frame | None, list]:
        """The forming bar on an engine clone, plus its notices."""
        self._ensure_engine()
        if self.engine is None or not self.chart.keys:
            return None, []
        key = self.chart.keys[-1]
        self._drain_macro(ct(key))
        e = self.engine.Clone()
        f = self._process(e, key, self.index + 1, True)
        self.frame = f
        keys = tuple(p.EndIndex for p in f.Profiles[:-1])
        if keys != self._past_keys:
            self._past_keys = keys
            self.past_rev += 1
        return f, [(f.Bar.Index, n) for n in f.Notices if n.Key != "Rule80"]

    def observe(self, price: float, mode: str) -> list:
        """Every live price against the latest frame's watched levels (the C# LevelAlertTracker)."""
        if self.frame is None:
            return []
        return [(self.frame.Bar.Index, n) for n in
                self.tracker.Observe(self.frame.Watches, price, mode, self.symbol, self.tick)]

    def fresh(self, notices: list) -> list:
        """Drops notices already sent for the same bar (the C# `sent` set)."""
        out = []
        for idx, n in notices:
            k = f"{idx}:{n.Key}"
            if k not in self.sent:
                self.sent.add(k)
                out.append(n)
        return out

    # --- output for the browser -------------------------------------------------------------------------
    def warning(self, f: Frame) -> str:
        w = []
        # levels are dated by the calendar trading day, so not folded: a holiday halt's own levels are not stale
        td = trading_date(f.Bar.Start)
        if self.levels_date is not None and self.levels_date < td:
            w.append(f"STALE LEVELS: levels date {self.levels_date:%Y-%m-%d}; chart trading date {td:%Y-%m-%d}.")
        if self.levels_error:
            w.append(self.levels_error)
        w.append(f.Warning.strip())
        return " ".join(x for x in w if x)

    @staticmethod
    def _line(li) -> list:
        return [li.Name, li.Price, None if li.Start == DMIN else epoch_ms(li.Start), epoch_ms(li.End), li.Color,
                li.TextColor, li.Width, li.Style, li.Offset, li.Size, li.Label, li.Extend]

    @staticmethod
    def _profile(pp, current: bool) -> dict:
        rows = pp.Profile.Rows
        if not rows:
            return {}
        lo, hi = min(rows), max(rows)
        return {"key": pp.EndIndex, "end": epoch_ms(pp.End), "cur": current, "step": pp.Profile.Step,
                "p0": lo * pp.Profile.Step, "v": [round(rows.get(k, 0.0), 1) for k in range(lo, hi + 1)],
                "vah": pp.Stats.Vah if valid(pp.Stats.Vah) else None, "val": pp.Stats.Val if valid(pp.Stats.Val) else None}

    def payload(self, past: bool, history: bool, samples: bool, profile: bool = True, vwap_known: int | None = None) -> dict:
        f = self.frame
        if f is None:
            return {"ready": False}
        out = {
            "ready": True, "minutes": self.minutes, "bar_start": epoch_ms(f.Bar.Start), "bar_end": epoch_ms(f.Bar.End),
            "lines": [self._line(li) for li in f.Lines],
            "zones": [[z.Top, z.Bottom, None if z.Start == DMIN else epoch_ms(z.Start), z.Color, z.BorderColor,
                       z.BorderWidth, z.BorderStyle] for z in f.Zones],
            **({"profile": self._profile(f.Profiles[-1], True) if f.Profiles else None} if profile else {}),
            "dash": {"labels": f.Labels, "values": f.Values, "colors": f.Colors},
            "warning": self.warning(f), "past_rev": self.past_rev, "history_rev": self.history_rev,
        }
        if past:
            out["past"] = [self._profile(p, False) for p in f.Profiles[:-1]]
            S = self.S   # drawing settings the browser needs (sent with the full payload)
            out["style"] = {"profile_width": S.mvp_profile_width, "profile_offset": S.mvp_offset,
                            "align_right": S.mvp_align_right, "c_va": S.mvp_c_hist_va, "va_transp": S.mvp_hist_va_transp,
                            "c_out": S.mvp_c_hist_out, "out_transp": S.mvp_hist_out_transp, "show_fill": S.cc_showFill,
                            "fill_active": S.cc_col_active, "fill_conf": S.cc_col_conf, "zones": S.lvl_showZones,
                            "tbl_show": S.tbl_show, "tbl_pos": S.tbl_pos, "tbl_size": S.tbl_size,
                            "ib_label_left": S.ib_label_loc == "Left", "top_layer": S.layer_top_pick,
                            "vwap": [S.vwap_col, S.vwap_width, S.vwap_style] if S.vwap_enable else None,
                            "price_tags": S.ShowPriceTags and not S.HideAllLabels}
        if history:
            out["history"] = [self._line(li) for li in self.history]
        if samples:   # runs of bars where the 80% Rule fill shows; resent whole when they change (small)
            out["samples"] = [[s, e, vah, val, conf] for s, e, vah, val, conf in self.samples]
        # the forming bar's, as NinjaTrader draws the preview's sample (the dashboard says ACTIVE or CONFIRMED already)
        out["fill"] = ([epoch_ms(f.Bar.Start), epoch_ms(f.Bar.End), f.VaHigh, f.VaLow, f.RuleConfirmed]
                       if f.RuleActive or f.RuleConfirmed else None)
        if self.S.vwap_enable:   # one point a bar: only the ones the client lacks, all of them when it lacks too many
            new = None if vwap_known is None else self.vwap_total - vwap_known
            if new is None or not 0 <= new <= len(self.vwap):
                out["vwap"] = [list(x) for x in self.vwap]
            elif new:
                out["vwap_add"] = [list(x) for x in list(self.vwap)[len(self.vwap) - new:]]
            out["vwap_live"] = [epoch_ms(f.Bar.Start), f.RthVwap] if valid(f.RthVwap) else None
        return out
