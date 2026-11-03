# Copyright (c) 2026 Tatanka Trading LLC.
# This Source Code Form is subject to the Mozilla Public License, v. 2.0.
# If a copy of the MPL was not distributed with this file, obtain one at https://mozilla.org/MPL/2.0/.
"""Tatanka TPO Profile v1.0.0, ported from the NinjaTrader 8 source
(tatankatrading/tatanka-indicators, ninjatrader/TatankaTpoProfile.cs).

TpoProfile mirrors the C# OnBarUpdate, session, row and value-area code statement for statement and keeps
its names. NinjaTrader's series become fields the caller sets before each OnBarUpdate: the bar's close
time, high and low, CurrentBars, the daily ATR(10) value and the session flags. Bars are stamped with their
CLOSE time, as on NinjaTrader. Frame() is the part of OnRender that picks what to draw; the browser draws it.

One deliberate change: a session's rows are priced with the row height they were keyed with (liveStep).
The C# keys rows with the row height of each bar and prices them with the height current when the session
is saved, so with Filter by Time off a daily ATR change at the 17:00 open re-prices the finished session at
the wrong prices. With Filter by Time on (the default) the two are identical."""
import math
import re
import sys
from datetime import datetime, timedelta

VERSION = "1.0.0"
LETTERS = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
DMIN = datetime.min
ATR_PERIOD = 10

# The settings that change what is computed, with the NinjaTrader defaults and [Range] limits. Colors,
# box transparency and the bar offset are drawing settings and live in the browser.
DEFAULTS = {
    "FilterByTime": True,
    "SessionTime": "0830-1500",
    "BlockMinutes": 30,
    "AutoRowHeight": True,
    "TargetRows": 80,
    "RowHeightTicks": 8,
    "LetterDays": 3,
    "ShowPolyline": True,
    "PolylineDays": 15,
    "XOffsetBars": 0,
    "ShowLive": False,
    "ShowValueArea": False,
    "ExtendPoc": False,
}
RANGES = {"BlockMinutes": (5, 240), "TargetRows": (15, 100), "RowHeightTicks": (1, 100), "LetterDays": (1, 5),
          "PolylineDays": (1, 50), "XOffsetBars": (-500, 500)}


def settings(overrides: dict | None) -> dict:
    """Defaults plus overrides; raises ValueError on an unknown name, a wrong type or an out-of-range value."""
    s = dict(DEFAULTS)
    for k, v in (overrides or {}).items():
        if k not in DEFAULTS:
            raise ValueError(f"unknown TPO setting {k}")
        d = DEFAULTS[k]
        if isinstance(d, bool):
            if not isinstance(v, bool):
                raise ValueError(f"{k} must be true or false")
        elif isinstance(d, int):
            if isinstance(v, bool) or not isinstance(v, int):
                raise ValueError(f"{k} must be a whole number")
            lo, hi = RANGES[k]
            if not lo <= v <= hi:
                raise ValueError(f"{k} must be {lo}-{hi}")
        elif not isinstance(v, str) or len(v) > 40:
            raise ValueError(f"{k} must be text")
        s[k] = v
    return s


def round_away(x: float) -> float:
    """Math.Round(x, MidpointRounding.AwayFromZero) as .NET Framework does it: split off the fraction."""
    frac, whole = math.modf(x)
    if abs(frac) >= 0.5:
        whole += math.copysign(1.0, frac)
    return whole


_INT = re.compile(r"[\t\n\v\f\r ]*[+-]?[0-9]+[\t\n\v\f\r ]*\Z")


def _try_int(s: str):
    """int.TryParse with the default NumberStyles.Integer."""
    return int(s) if _INT.match(s) else None


def _total_ms(d: timedelta) -> float:
    return (d.days * 86_400_000_000 + d.seconds * 1_000_000 + d.microseconds) / 1000.0


class NtAtr:
    """NinjaTrader 8's ATR indicator: the first bar's range, then ((min(n, Period) - 1) * prev + TR) / min(n, Period)."""

    def __init__(self, period: int = ATR_PERIOD):
        self.Period = period
        self.values: list[float] = []
        self._close = 0.0

    def add(self, high: float, low: float, close: float) -> float:
        cb = len(self.values)
        if cb == 0:
            v = high - low
        else:
            close1 = self._close
            true_range = max(abs(low - close1), max(high - low, abs(high - close1)))
            n = min(cb + 1, self.Period)
            v = ((n - 1) * self.values[-1] + true_range) / n
        self.values.append(v)
        self._close = close
        return v


class TpoRow:
    __slots__ = ("Price", "Letters")

    def __init__(self):
        self.Price = 0.0
        self.Letters: list[int] = []


class SavedProfile:
    __slots__ = ("TimeStart", "TimeEnd", "PocPrice", "MaxCount", "TickStep", "Rows")

    def __init__(self):
        self.TimeStart = DMIN
        self.TimeEnd = DMIN
        self.PocPrice = 0.0
        self.MaxCount = 0
        self.TickStep = 0.0
        self.Rows: list[TpoRow] = []


class TpoProfile:
    def __init__(self, s: dict, tick_size: float):
        self.FilterByTime = s["FilterByTime"]
        self.SessionTime = s["SessionTime"]
        self.BlockMinutes = s["BlockMinutes"]
        self.AutoRowHeight = s["AutoRowHeight"]
        self.TargetRows = s["TargetRows"]
        self.RowHeightTicks = s["RowHeightTicks"]
        self.LetterDays = s["LetterDays"]
        self.ShowPolyline = s["ShowPolyline"]
        self.PolylineDays = s["PolylineDays"]
        self.XOffsetBars = s["XOffsetBars"]
        self.ShowLive = s["ShowLive"]
        self.ShowValueArea = s["ShowValueArea"]
        self.ExtendPoc = s["ExtendPoc"]
        self.TickSize = tick_size

        self.liveRows: dict[int, list[int]] = {}
        self.saved: list[SavedProfile] = []
        self.sessionStartTime = DMIN   # actual session open (letter math)
        self.drawStartTime = DMIN      # first in-session bar timestamp (render anchor)
        self.lastSessBarTime = DMIN    # last in-session bar timestamp
        self.hasLive = False
        self.liveInSession = False
        self.tickStep = 0.0
        self.liveStep = 0.0            # (port) the row height the live session's rows are keyed with

        # NinjaTrader series, set by the caller for each primary bar
        self.CurrentBars = [-1, -1]
        self.Time0 = DMIN              # Time[0]: the bar's close time
        self.High0 = 0.0
        self.Low0 = 0.0
        self.IsFirstBarOfSession = False
        self.TemplateSessionOpen = DMIN   # sessionIterator.ActualSessionBegin for Time[0]
        self.dailyAtr: float | None = None  # dailyAtr[0], None before the daily series has a bar

    def OnBarUpdate(self):
        if self.CurrentBars[0] < 1:
            return

        self.tickStep = self.GetTickStep()
        if self.tickStep <= 0:
            return

        # NinjaTrader stamps bars with their CLOSE time (TradingView/Pine uses OPEN time).
        # Session window is evaluated as (start, end] on the close stamp so the 08:30 bar
        # (pre-open) is excluded and the 15:00 bar (final RTH bar) is included.
        t = self.Time0

        if self.FilterByTime:
            timeOk = self.IsInSession(t)
            # Session-open change also catches RTH-only templates where no out-of-window bar exists.
            newSess = timeOk and (not self.hasLive or self.GetSessionOpen(t) != self.sessionStartTime)
        else:
            timeOk = True
            newSess = not self.hasLive or self.IsFirstBarOfSession

        if self.hasLive and (newSess or not timeOk):
            self.SaveProfile()
            self.liveRows.clear()
            self.hasLive = False

        if newSess:
            self.sessionStartTime = self.GetSessionOpen(t) if self.FilterByTime else self.TemplateSessionOpen
            self.drawStartTime = t
            self.hasLive = True
            self.liveStep = self.tickStep

        self.liveInSession = timeOk and self.hasLive

        if self.liveInSession:
            tIdx = self.GetLetterIndex(t)
            lo = self.PriceKey(self.Low0)
            hi = self.PriceKey(self.High0)
            if hi < lo:
                lo, hi = hi, lo

            for k in range(lo, hi + 1):
                letters = self.liveRows.get(k)
                if letters is None:
                    letters = []
                    self.liveRows[k] = letters
                if len(letters) == 0 or letters[-1] != tIdx:
                    letters.append(tIdx)
            self.lastSessBarTime = t

    # --- Session / bins -----------------------------------------------------------------------------------
    def IsInSession(self, t: datetime) -> bool:
        ok, startMin, endMin = self.ParseSession(self.SessionTime)
        if not ok:
            return True

        # (start, end] on bar close time
        now = t.hour * 60 + t.minute + (t.second + t.microsecond / 1e6) / 60.0
        if startMin <= endMin:
            return startMin < now <= endMin
        return now > startMin or now <= endMin

    # Actual open of the custom session window this close-stamped bar belongs to.
    def GetSessionOpen(self, t: datetime) -> datetime:
        _, startMin, endMin = self.ParseSession(self.SessionTime)
        now = t.hour * 60 + t.minute + (t.second + t.microsecond / 1e6) / 60.0
        day = datetime(t.year, t.month, t.day)
        if startMin > endMin and now <= endMin:   # overnight window, after-midnight part
            day = day - timedelta(days=1)
        return day + timedelta(minutes=startMin)

    @staticmethod
    def ParseSession(s: str):
        startMin = 8 * 60 + 30
        endMin = 15 * 60
        if not s:
            return False, startMin, endMin

        parts = s.split("-")
        if len(parts) != 2:
            return False, startMin, endMin

        a = TpoProfile.TryParseHhmm(parts[0])
        if a is None:
            return False, startMin, endMin
        b = TpoProfile.TryParseHhmm(parts[1])
        if b is None:
            return False, startMin, endMin

        return True, a, b

    @staticmethod
    def TryParseHhmm(raw: str):
        t = raw.strip()
        if len(t) == 3:
            t = "0" + t
        if len(t) != 4:
            return None
        hh = _try_int(t[0:2])
        mm = _try_int(t[2:4])
        if hh is None or mm is None:
            return None
        if hh < 0 or hh > 23 or mm < 0 or mm > 59:
            return None
        return hh * 60 + mm

    def GetLetterIndex(self, t: datetime) -> int:
        # -1ms: a bar closing exactly on a block boundary belongs to the block it ends
        ms = _total_ms(t - self.sessionStartTime) - 1.0
        blockMs = self.BlockMinutes * 60.0 * 1000.0
        if blockMs <= 0:
            return 0
        idx = int(math.floor(ms / blockMs))
        if idx < 0:
            idx = 0
        return idx

    def GetTickStep(self) -> float:
        tick = self.TickSize
        if tick <= 0:
            tick = 0.01

        mult = float(self.RowHeightTicks)
        if self.AutoRowHeight and len(self.CurrentBars) > 1 and self.CurrentBars[1] >= 10 and self.dailyAtr is not None:
            atr = self.dailyAtr
            if not math.isnan(atr) and atr > 0:
                mult = max(round_away(atr / self.TargetRows / tick), 1.0)
        return tick * max(mult, 1.0)

    def PriceKey(self, price: float) -> int:
        return int(round_away(price / self.liveStep))

    @staticmethod
    def KeyToPrice(key: int, step: float) -> float:
        return key * step

    @staticmethod
    def GetLetter(index: int) -> str:
        if 0 <= index < len(LETTERS):
            return LETTERS[index]
        return "?"

    # --- Save / VA ------------------------------------------------------------------------------------------
    def SaveProfile(self):
        if len(self.liveRows) == 0:
            return

        prof = self.BuildProfileFromMap(self.liveRows, self.drawStartTime, self.lastSessBarTime, self.liveStep)
        if prof is not None:
            self.saved.append(prof)

    def BuildProfileFromMap(self, m: dict, tStart: datetime, tEnd: datetime, step: float):
        if len(m) == 0:
            return None

        keys = sorted(m)

        p = SavedProfile()
        p.TimeStart = tStart
        p.TimeEnd = tEnd
        p.TickStep = step
        p.MaxCount = 0
        p.PocPrice = 0.0

        for k in keys:
            letters = m[k]
            if not letters:
                continue
            row = TpoRow()
            row.Price = self.KeyToPrice(k, step)
            row.Letters = list(letters)
            p.Rows.append(row)
            if len(letters) > p.MaxCount:
                p.MaxCount = len(letters)

        if len(p.Rows) == 0:
            return None

        # POC: the row with the most TPOs. On a tie, the tied row nearest the middle of the
        # profile's range (Market Profile convention); if two are equally close, the lower one.
        # Rows are in ascending price order, so a strict "<" keeps the lower row on an equal distance.
        mid = (p.Rows[0].Price + p.Rows[-1].Price) / 2.0
        bestDist = sys.float_info.max
        for r in p.Rows:
            if len(r.Letters) != p.MaxCount:
                continue
            d = abs(r.Price - mid)
            if d < bestDist - 1e-9:
                bestDist = d
                p.PocPrice = r.Price
        return p

    @staticmethod
    def GetValueArea(p: SavedProfile):
        vah = p.PocPrice
        val = p.PocPrice
        sz = len(p.Rows)
        if sz == 0:
            return vah, val

        total = 0
        pocIdx = 0
        for i in range(sz):
            total += len(p.Rows[i].Letters)
            if abs(p.Rows[i].Price - p.PocPrice) < 1e-10:
                pocIdx = i

        target = int(round_away(total * 0.70))
        cum = len(p.Rows[pocIdx].Letters)
        lowIdx = pocIdx
        highIdx = pocIdx

        while cum < target and (lowIdx > 0 or highIdx < sz - 1):
            nextLow = len(p.Rows[lowIdx - 1].Letters) if lowIdx > 0 else 0
            nextHigh = len(p.Rows[highIdx + 1].Letters) if highIdx < sz - 1 else 0
            if nextHigh > nextLow:
                highIdx += 1
                cum += nextHigh
            elif nextLow > 0:
                lowIdx -= 1
                cum += nextLow
            elif nextHigh > 0:
                highIdx += 1
                cum += nextHigh
            else:
                break

        return p.Rows[highIdx].Price, p.Rows[lowIdx].Price

    # --- what OnRender draws ---------------------------------------------------------------------------------
    def GetPocExtEnd(self, idx: int, total: int, colSeconds: float):
        if not self.ExtendPoc:
            return DMIN
        if idx + 1 < total:
            return self.saved[idx + 1].TimeEnd
        return self.Time0 + timedelta(seconds=colSeconds)

    def Frame(self, colSeconds: float) -> list[dict]:
        """The profiles OnRender draws, oldest first: older days as outlines ("P"), the last LetterDays with
        letters ("L"), then the developing session if ShowLive. Times are NinjaTrader close stamps or
        TimeStart plus whole columns; the caller maps them onto its own bars."""
        offsetSec = self.XOffsetBars * colSeconds
        total = len(self.saved)
        letterStart = max(total - self.LetterDays, 0)
        out = []

        if self.ShowPolyline and letterStart > 0:
            polyStart = max(letterStart - self.PolylineDays, 0)
            for i in range(polyStart, letterStart):
                prof = self.saved[i]
                if len(prof.Rows) < 2:
                    continue
                out.append(self._drawn(prof, "P", i, total, colSeconds, offsetSec, 3))

        for i in range(letterStart, total):
            prof = self.saved[i]
            if len(prof.Rows) == 0:
                continue
            out.append(self._drawn(prof, "L", i, total, colSeconds, offsetSec, 5))

        if self.ShowLive and self.liveInSession and len(self.liveRows) > 0:
            live = self.BuildProfileFromMap(self.liveRows, self.drawStartTime, self.Time0, self.liveStep)
            if live is not None:
                out.append(self._drawn(live, "L", total, total + 1, colSeconds, offsetSec, 5, True))
        return out

    def _drawn(self, prof: SavedProfile, mode: str, idx: int, total: int, colSeconds: float, offsetSec: float,
               pad: int, live: bool = False) -> dict:
        ext = self.GetPocExtEnd(idx, total, colSeconds)
        pocEnd = prof.TimeStart + timedelta(seconds=(prof.MaxCount + pad) * colSeconds + offsetSec)
        pocExt = ext != DMIN and ext > pocEnd
        d = {"mode": mode, "live": live, "profile": prof, "poc_cols": prof.MaxCount + pad,
             "poc_end": ext if pocExt else None, "va": None}
        if self.ShowValueArea:
            d["va"] = self.GetValueArea(prof)
        return d
