# Copyright (c) 2026 Tatanka Trading LLC.
# This Source Code Form is subject to the Mozilla Public License, v. 2.0.
# If a copy of the MPL was not distributed with this file, obtain one at https://mozilla.org/MPL/2.0/.
"""Tatanka Gap Analysis v1.0.0, ported from the NinjaTrader 8 source
(tatankatrading/tatanka-indicators, ninjatrader/TatankaGapAnalysis.cs, the ENGINE_BEGIN..ENGINE_END part).

GapEngine mirrors the C# GapEngine statement for statement and keeps its names: RTH opening gaps against
the last RTH chart-bar close, partial fills, consecutive same-direction gaps. Update() takes each chart bar
by index with its END time in Chicago, as NinjaTrader stamps it; calling it again for the same bar as the
bar grows (Calculate.OnPriceChange) is what the C# does in real time. Drawing and alerts live elsewhere."""
from datetime import datetime, timedelta

VERSION = "1.0.0"
DMIN = datetime.min
DAY = timedelta(days=1)
ONE_TICK = timedelta(microseconds=1)    # DateTime.AddTicks(-1): any step below a second gives the same answer

# The settings that change what is computed, with the NinjaTrader defaults and [Range] limits.
DEFAULTS = {"RthStart": 83000, "RthEnd": 150000, "MinimumGapPoints": 0.0}


def settings(overrides: dict | None) -> dict:
    s = dict(DEFAULTS)
    for k, v in (overrides or {}).items():
        if k not in DEFAULTS:
            raise ValueError(f"unknown gap setting {k}")
        if k == "MinimumGapPoints":
            if isinstance(v, bool) or not isinstance(v, (int, float)) or not 0 <= v < 1e9:
                raise ValueError("MinimumGapPoints must be 0 or more")
            v = float(v)
        elif isinstance(v, bool) or not isinstance(v, int) or not 0 <= v <= 235959:
            raise ValueError(f"{k} must be HHmmss, 0-235959")
        s[k] = v
    ok_a, a = GapEngine.TryParseTime(s["RthStart"])
    ok_b, b = GapEngine.TryParseTime(s["RthEnd"])
    if not ok_a or not ok_b or a == b:
        raise ValueError("Enter different, valid RTH start/end times in HHmmss format.")
    return s


def engine_for(s: dict) -> "GapEngine":
    return GapEngine(GapEngine.TryParseTime(s["RthStart"])[1], GapEngine.TryParseTime(s["RthEnd"])[1],
                     s["MinimumGapPoints"])


class GapRecord:
    __slots__ = ("Ordinal", "StartBar", "NextGapBar", "FillBar", "Direction", "OpenPrice", "SettlePrice", "FillEdge")

    def __init__(self, Ordinal=0, StartBar=0, Direction=0, OpenPrice=0.0, SettlePrice=0.0, FillEdge=0.0):
        self.Ordinal = Ordinal
        self.StartBar = StartBar
        self.NextGapBar = -1
        self.FillBar = -1
        self.Direction = Direction
        self.OpenPrice = OpenPrice
        self.SettlePrice = SettlePrice
        self.FillEdge = FillEdge

    @property
    def Size(self) -> float:
        return abs(self.OpenPrice - self.SettlePrice)

    @property
    def Filled(self) -> bool:
        return self.FillBar >= 0

    @property
    def BarsToFill(self) -> int:
        return self.FillBar - self.StartBar if self.Filled else 0

    @property
    def FillPercent(self) -> float:
        return 100.0 if self.Filled else min(100.0, abs(self.OpenPrice - self.FillEdge) / self.Size * 100)

    def Copy(self) -> "GapRecord":
        c = GapRecord.__new__(GapRecord)
        for k in GapRecord.__slots__:
            setattr(c, k, getattr(self, k))
        return c

    def DrawingEndBar(self, currentBar: int, extend: bool) -> int:
        end = self.FillBar if self.Filled else currentBar
        if not extend and self.NextGapBar >= 0:
            end = min(end, self.NextGapBar - 1)
        return max(self.StartBar, end)


class GapUpdate:
    __slots__ = ("NewGap", "FilledGaps")

    def __init__(self):
        self.NewGap: GapRecord | None = None
        self.FilledGaps: list[GapRecord] | None = None


class GapFrame:
    def __init__(self):
        self.StoredCount = 0
        self.CurrentBar = 0
        self.Consecutive = 0
        self.Latest: GapRecord | None = None
        self.VisibleGaps: list[GapRecord] = []


class GapEngine:
    def __init__(self, start: timedelta, end: timedelta, minimum: float):
        if start == end or start < timedelta(0) or end < timedelta(0) or start >= DAY or end >= DAY:
            raise ValueError("Invalid RTH session.")
        self.History: list[GapRecord] = []
        self.Active: list[GapRecord] = []
        self.rthStart = start
        self.rthEnd = end
        self.minimumGap = minimum
        self.trackedSession = DMIN
        self.trackedClose = 0.0
        self.currentBar = -1
        self.consecutive = 0

    @staticmethod
    def TryParseTime(hhmmss: int):
        hour = hhmmss // 10000
        minute = hhmmss // 100 % 100
        second = hhmmss % 100
        valid = hhmmss >= 0 and hour < 24 and minute < 60 and second < 60
        return valid, (timedelta(hours=hour, minutes=minute, seconds=second) if valid else timedelta(0))

    def SessionKey(self, barEndChicago: datetime) -> datetime:
        # 08:30 end-stamped minute bar is pre-RTH; 15:00 is the last RTH bar.
        instant = barEndChicago - ONE_TICK
        clock = instant - datetime(instant.year, instant.month, instant.day)
        date = datetime(instant.year, instant.month, instant.day)
        if self.rthStart < self.rthEnd:
            return date if self.rthStart <= clock < self.rthEnd else DMIN
        if clock >= self.rthStart:
            return date
        return date - DAY if clock < self.rthEnd else DMIN

    def Update(self, bar: int, barEndChicago: datetime, open_: float, high: float, low: float, close: float) -> GapUpdate:
        update = GapUpdate()
        self.currentBar = bar
        session = self.SessionKey(barEndChicago)
        if session != DMIN:
            if session != self.trackedSession:
                # The first loaded session warms up the RTH close. The next session
                # can create a gap, even on RTH-only charts with no overnight bars.
                if self.trackedSession != DMIN:
                    signedSize = open_ - self.trackedClose
                    if abs(signedSize) > 0 and abs(signedSize) >= self.minimumGap:
                        prior = None if len(self.History) == 0 else self.History[-1]
                        gap = GapRecord(Ordinal=len(self.History), StartBar=bar, Direction=1 if signedSize > 0 else -1,
                                        OpenPrice=open_, SettlePrice=self.trackedClose, FillEdge=open_)
                        if prior is not None:
                            prior.NextGapBar = bar
                        self.consecutive = self.consecutive + 1 if prior is not None and prior.Direction == gap.Direction else 1
                        self.History.append(gap)
                        self.Active.append(gap)
                        update.NewGap = gap
                self.trackedSession = session
            self.trackedClose = close

        # Filled history is never rescanned on a price update.
        for i in range(len(self.Active) - 1, -1, -1):
            gap = self.Active[i]
            gap.FillEdge = (max(gap.SettlePrice, min(gap.FillEdge, low)) if gap.Direction > 0
                            else min(gap.SettlePrice, max(gap.FillEdge, high)))
            isFilled = low <= gap.SettlePrice if gap.Direction > 0 else high >= gap.SettlePrice
            if isFilled:
                gap.FillEdge = gap.SettlePrice
                gap.FillBar = bar
                if update.FilledGaps is None:
                    update.FilledGaps = []
                update.FilledGaps.append(gap)
                del self.Active[i]
        return update

    def Snapshot(self, firstVisibleBar: int, lastVisibleBar: int, extend: bool, hideFilled: bool,
                 maximumVisible: int) -> GapFrame:
        frame = GapFrame()
        frame.StoredCount = len(self.History)
        frame.CurrentBar = self.currentBar
        frame.Consecutive = self.consecutive
        frame.Latest = None if len(self.History) == 0 else self.History[-1].Copy()
        candidates = self.Active if hideFilled else self.History
        for i in range(len(candidates) - 1, -1, -1):
            gap = candidates[i]
            if gap.StartBar > lastVisibleBar:
                continue
            reachesView = extend and not gap.Filled or gap.DrawingEndBar(self.currentBar, extend) >= firstVisibleBar
            if not reachesView:
                continue
            if maximumVisible > 0 and len(frame.VisibleGaps) >= maximumVisible:
                break
            frame.VisibleGaps.append(gap.Copy())
        frame.VisibleGaps.reverse()   # Newest zones are painted last.
        return frame
