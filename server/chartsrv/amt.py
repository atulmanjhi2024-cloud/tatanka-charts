# Copyright (c) 2026 Tatanka Trading LLC.
# This Source Code Form is subject to the Mozilla Public License, v. 2.0.
# If a copy of the MPL was not distributed with this file, obtain one at https://mozilla.org/MPL/2.0/.
"""Tatanka AMT Toolkit calculation engine, ported from the NinjaTrader 8 source
(tatankatrading/tatanka-indicators, ninjatrader/TatankaAmtToolkit.cs, v1.1.0).

The Engine mirrors the C# Engine statement for statement and keeps its names, so the two can be read side
by side: CT sessions, profile row rules, prior-day/month value, initial balance, overnight inventory and
trap, 80% Rule, day type, single prints, poor highs/lows, naked POCs, late spike, levels and dashboard.
NinjaTrader plumbing (data series, rendering, levels file/link, Discord) lives elsewhere in this app.
All times are naive datetimes in exchange time (CT), as in the C# engine."""
import math
import re
from decimal import ROUND_HALF_UP, Decimal
from bisect import bisect_left
from collections import deque
from datetime import datetime, timedelta
from functools import lru_cache

from . import sessions

VERSION = "1.1.0"
NAN = float("nan")
DMIN = datetime.min

# --- AmtPalette (ARGB) ---------------------------------------------------------------------------------
RED, GREEN, LIME, YELLOW, ORANGE, GRAY = 0xFFF23645, 0xFF4CAF50, 0xFF00E676, 0xFFFDD835, 0xFFFF9800, 0xFF787B86
BLUE, PURPLE, MAROON, AQUA, WHITE = 0xFF2196F3, 0xFF9C27B0, 0xFF880E4F, 0xFF00BCD4, 0xFFFFFFFF
ALERT_RED, PINK, GREEN_SOFT, RED_SOFT = 0xFFFF1744, 0xFFEA80FC, 0xB34CAF50, 0xB3F23645

# Every setting with the NinjaTrader defaults, generated from the C# Settings class.
DEFAULTS = {
    'tzguard_enable': True,
    'row_scale': "Auto",
    'tick_auto': True,
    'tick_manual_val': 0.25,
    'layer_top_pick': "Daily Levels (All)",
    'alertcap_enable': True,
    'alertcap_max': 30,
    'alertcap_window_min': 60,
    'lvl_text': "",
    'lvl_ip_color': 0xFFFDD835,
    'lvl_bull_color': 0xFFF23645,
    'lvl_bear_color': 0xFF4CAF50,
    'lvl_thickness': 1,
    'lvl_style_in': "Dotted",
    'lvl_zone_style_in': "Solid",
    'lvl_text_size_opt': "large",
    'lvl_x_offset': 40,
    'lvl_showLine': True,
    'lvl_showZones': True,
    'lvl_zone_mode': "ATR %",
    'lvl_zone_atr_pct': 0.02,
    'lvl_zoneWidthTicks': 8,
    'lvl_zoneOpacity': 85,
    'lvl_color_flip': True,
    'lvl_flip_atr': 0.05,
    'lvl_txt_c': 0x00FFFFFF,
    'lvl_alerts': False,
    'lvl_alert_ip': True,
    'lvl_alert_bull': True,
    'lvl_alert_bear': True,
    'mvp_master': True,
    'mvp_tf_mode': "Weekly",
    'mvp_sess_time': "0830-1500",
    'mvp_sess_dur': 6.5,
    'mvp_calc_tf': "1",
    'mvp_row_size': 0.25,
    'mvp_val_pct': 68.0,
    'mvp_profile_width': 50,
    'mvp_align_right': True,
    'mvp_offset': 50,
    'mvp_show_walls': False,
    'mvp_use_auto_scale': True,
    'mvp_scale_start': 2.0,
    'mvp_scale_end': 1.5,
    'mvp_extend_walls': True,
    'mvp_smooth_len': 2,
    'mvp_vol_mult': 2.0,   # a double in the C# (2), so fractions reach the engine
    'mvp_min_wall_dist': 40,
    'mvp_w_wall': 1,
    'mvp_c_wall_base': 0xFFE6B300,
    'mvp_wall_transp': 25,
    'mvp_c_poc': 0xFF78909C,
    'mvp_poc_width': 2,
    'mvp_c_levels': 0xFF787B86,
    'mvp_lvl_width': 2,
    'mvp_c_hist_va': 0xFF78909C,
    'mvp_hist_va_transp': 75,
    'mvp_c_hist_out': 0xFF787B86,
    'mvp_hist_out_transp': 90,
    'mvp_show_poc': True,
    'mvp_show_vah': True,
    'mvp_show_val': True,
    'mvp_poc_style': "Solid",
    'mvp_lvl_style': "Solid",
    'mvp_wall_style': "Solid",
    'mvp_lb_size': "Normal",
    'mvp_txt_c': 0x00FFFFFF,
    'mvp_use_cur_alerts': True,
    'mvp_alert_time': "0830-1500",
    'macro_show_month': True,
    'macro_show_poc': True,
    'macro_show_hl': True,
    'macro_alert_lvl': True,
    'macro_color': 0xFFFFFFFF,
    'macro_poc_color': 0xFFFFFFFF,
    'macro_width': 3,
    'macro_poc_width': 3,
    'macro_style': "Solid",
    'macro_poc_style': "Solid",
    'macro_offset': 2,
    'macro_text_size': "Normal",
    'macro_txt_c': 0x00FFFFFF,
    'ib_enable': True,
    'ib_alert_ibh': True,
    'ib_alert_ibl': True,
    'ib_session': "0830-0930",
    'ib_alert_time': "0830-1500",
    'ib_show_mid': False,
    'ib_show_labels': True,
    'ib_label_loc': "Right",
    'ib_label_size': "Normal",
    'ib_lbl_offset': 0,
    'ib_lvl_width': 1,
    'ib_col_hi': 0xFFFFCC80,
    'ib_col_lo': 0xFFFFCC80,
    'ib_col_mid': 0xFFFFCC80,
    'ib_extend': "None",
    'ib_limit_ext': True,
    'ib_stop_time': "0830-1500",   # 1515 in the NinjaTrader original, from before ES's RTH ended at 15:00
    'ib_style_main': "Solid",
    'ib_style_int': "Dotted",
    'ib_txt_c': 0x00FFFFFF,
    'vwap_enable': False,
    'vwap_col': 0xFFFFFFFF,
    'vwap_width': 2,
    'vwap_style': "Solid",
    'vwap_start_h': 8,
    'vwap_start_m': 30,
    'vwap_end_h': 15,
    'vwap_end_m': 0,
    'vwap_alert': False,
    'cc_showOpen': False,
    'cc_c_open': 0xFF2196F3,
    'cc_open_width': 1,
    'cc_open_offset': 3,
    'cc_open_style': "Solid",
    'cc_open_lb_size': "Normal",
    'wk_showOpen': True,
    'wk_c_open': 0xFFFFFFFF,
    'wk_open_width': 2,
    'wk_open_offset': 2,
    'wk_open_style': "Solid",
    'wk_open_lb_size': "Normal",
    'opens_txt_c': 0x00FFFFFF,
    'wk_alert_open': True,
    'amt_enable': True,
    'amt_sess': "1700-0830:123456",
    'amt_del_sess': "1700-1705",   # 1500-1505 in the NinjaTrader original: here the OVN lines stay to the 17:00 open
    'amt_showLb': True,
    'amt_width': 1,
    'amt_style_in': "Solid",
    'amt_c_hi': 0xFFFFFFFF,
    'amt_c_txt_hi': 0xFFFFFFFF,
    'amt_c_lo': 0xFFFFFFFF,
    'amt_c_txt_lo': 0xFFFFFFFF,
    'amt_lb_size_in': "Normal",
    'amt_offset': 2,
    'amt_alert_ovn': True,
    'amt_vauto': True,
    'amt_vres': False,
    'amt_res_val': 0.25,
    'pd_enable': True,
    'pd_use_rth': True,
    'pd_sess_time': "0830-1500",
    'pd_showH': True,
    'pd_showL': True,
    'pd_showOpen': False,
    'pd_high_c': 0xFFFFFFFF,
    'pd_low_c': 0xFFFFFFFF,
    'pd_open_c': 0xFF2196F3,
    'pd_high_width': 1,
    'pd_low_width': 1,
    'pd_open_width': 1,
    'pd_style_in': "Solid",
    'pd_lb_size_in': "Normal",
    'pd_offset_val': 2,
    'pd_txt_c': 0x00FFFFFF,
    'pd_alert_hl': True,
    'cc_enable': True,
    'cc_show_poc': True,
    'cc_show_vah': True,
    'cc_show_val': True,
    'cc_alert_va': False,
    'cc_alert_80': True,
    'pd_poc_c': 0xFFFFFFFF,
    'pd_vah_c': 0x809C27B0,
    'pd_val_c': 0x809C27B0,
    'cc_poc_width': 1,
    'cc_vah_width': 1,
    'cc_val_width': 1,
    'cc_poc_style_in': "Solid",
    'cc_lb_offset': 2,
    'cc_lb_size_in': "Normal",
    'cc_txt_c': 0x00FFFFFF,
    'cc_showFill': True,
    'cc_col_active': 0x129C27B0,
    'cc_col_conf': 0x12EA80FC,
    'cc_va_pct': 0.68,
    'cc_sess_def': "0830-1500:23456",
    'pw_enable': True,
    'pw_showH': True,
    'pw_showL': True,
    'pw_col': 0xFFFFFFFF,
    'pw_c_txt': 0xFFFFFFFF,
    'pw_width': 2,
    'pw_style_in': "Solid",
    'pw_offset': 2,
    'pw_lb_size_in': "Normal",
    'pw_alert_hl': True,
    'sp_rth_session': "0830-1500",
    'sp_tpo_minutes': 30,
    'sp_ticks_per_block': 1,
    'sp_color_hist': 0x19FF9800,
    'sp_border_c': 0xFFFF9800,
    'sp_border_w': 0,
    'sp_border_style': "Solid",
    'sp_max_days': 10,
    'sp_filter_tails': True,
    'sp_tail_max': 15,
    'sp_min_ticks': 1,
    'sp_show_poor': True,
    'sp_poor_width': 2,
    'sp_poor_style_in': "Dotted",
    'sp_col_poor_h': 0xFFF23645,
    'sp_col_poor_l': 0xFF4CAF50,
    'sp_poor_lb_size': "Small",
    'sp_poor_txt_c': 0x00FFFFFF,
    'showSpike': True,
    'spikeTimeRange': "1430-1500",
    'spike_width': 1,
    'spike_style': "Solid",
    'spike_c_sup': 0xFF4CAF50,
    'spike_c_res': 0xFFF23645,
    'spike_offset': 15,
    'spike_lbl_size': "Normal",
    'spike_txt_c': 0x00FFFFFF,
    'spike_alert': True,
    'inv_enable': True,
    'inv_show_line': True,
    'inv_start_h': 17,
    'inv_end_h': 8,
    'inv_end_m': 30,
    'inv_c_trap': 0xFF00FF00,
    'inv_c_settle': 0xFF787B86,
    'inv_txt_trap': "TRAP ACTIVE",
    'inv_width': 1,
    'inv_style_in': "Dashed",
    'inv_settle_width': 1,
    'inv_settle_style': "Dashed",
    'inv_lb_size_in': "Normal",
    'inv_offset': 40,
    'inv_txt_c': 0x00FFFFFF,
    'inv_alert': True,
    'npoc_enable': True,
    'npoc_c': 0xFFFFC0CB,
    'npoc_c_w': 0xFFFFC0CB,
    'npoc_c_m': 0xFFFFC0CB,
    'npoc_d_show': True,
    'npoc_w_show': True,
    'npoc_m_show': True,
    'npoc_style_in': "Solid",
    'npoc_w_d': 1,
    'npoc_w_w': 2,
    'npoc_w_m': 3,
    'npoc_lb_size': "Small",
    'npoc_lb_offset': 2,
    'npoc_txt_c': 0x00FFFFFF,
    'npoc_alert': True,
    'context_session': "0815-1500",
    'daily_atr_length': 14,
    'tbl_show': True,
    'tbl_session': "0830-1500",
    'tbl_pos': "Top Right",
    'tbl_size': "Normal",
    'HideAllLabels': False,
    'LabelsLevels': True,
    'LabelsProfiles': True,
    'LabelsPriorDay': True,
    'LabelsPriorValue': True,
    'LabelsOpens': True,
    'LabelsPriorWeek': True,
    'LabelsPriorMonth': True,
    'LabelsInventory': True,
    'LabelsSpike': True,
    'LabelsNakedPoc': True,
    'LabelsPoor': True,
    'ShowPriceTags': True,
    'LevelAlertMode': "Cross",
    'AlertZoneTicks': 4,
    'MaxProfileRows': 100000,
    'MaxStructures': 2000,
}


class Settings:
    """The C# Settings class: every default above, overridable by name."""

    def __init__(self, overrides: dict | None = None):
        self.__dict__.update(DEFAULTS)
        for k, v in (overrides or {}).items():
            if k not in DEFAULTS:
                raise KeyError(f"unknown AMT setting {k!r}")
            self.__dict__[k] = type(DEFAULTS[k])(v) if not isinstance(DEFAULTS[k], float) else float(v)


# --- MathEx ----------------------------------------------------------------------------------------------
def valid(x) -> bool:
    return x is not None and math.isfinite(x)


def bin_(x: float, step: float) -> int:
    """Pine rounds ties toward positive infinity, unlike banker's rounding."""
    return math.floor(x / step + .5)


def trading_date(ct: datetime) -> datetime:
    d = datetime(ct.year, ct.month, ct.day)
    return d + timedelta(days=1) if (ct.hour, ct.minute, ct.second, ct.microsecond) >= (17, 0, 0, 0) else d


def week(td: datetime) -> datetime:
    d = datetime(td.year, td.month, td.day)
    return d - timedelta(days=d.weekday())


def month(td: datetime) -> datetime:
    return datetime(td.year, td.month, 1)


def minute_of(t: datetime) -> int:
    return t.hour * 60 + t.minute


def _dow1(t: datetime) -> str:
    """.NET DayOfWeek + 1 as a digit: Sunday 1 ... Saturday 7."""
    return str((t.weekday() + 1) % 7 + 1)


@lru_cache(maxsize=None)
def _session(session: str) -> tuple[int, int, str | None]:
    parts = session.split(":")
    rng = parts[0].split("-")
    if len(rng) != 2 or len(rng[0]) != 4 or len(rng[1]) != 4:
        raise ValueError("Invalid CT session: " + session)
    a = int(rng[0][:2]) * 60 + int(rng[0][2:])
    b = int(rng[1][:2]) * 60 + int(rng[1][2:])
    if a > 1439 or b > 1440 or int(rng[0][2:]) > 59 or int(rng[1][2:]) > 59:
        raise ValueError("Invalid CT session: " + session)
    return a, b, parts[1] if len(parts) >= 2 else None


def in_session(t: datetime, session: str) -> bool:
    a, b, days = _session(session)
    m = minute_of(t)
    day = days is None or _dow1(t) in days
    return day and (a == b or (m >= a and m < b if a < b else m >= a or m < b))


def levels_root(root: str) -> str:
    return {"MES": "ES", "MNQ": "NQ", "MCL": "CL", "MGC": "GC"}.get((root or "").upper(), root)


def row_mult(scale: str, price: float, tick: float) -> int:
    """Auto keeps rows near 0.0035% of price, rounded to 1, 2 or 5 times a power of ten in ticks."""
    m = 1
    if scale != "Auto":
        try:
            m = int((scale or "1x").rstrip("x"))
        except ValueError:
            m = 1
    elif valid(price) and price > 0 and tick > 0:
        raw = price * 0.000035 / tick
        if raw >= math.sqrt(2):
            dec = 10 ** math.floor(math.log10(raw))
            f = raw / dec
            n = 1 if f < math.sqrt(2) else 2 if f < math.sqrt(10) else 5 if f < math.sqrt(50) else 10
            m = int(math.floor(n * dec + .5))
    return max(1, m)


def auto_tick(root: str, tick: float, close: float) -> float:
    r = root.upper()
    table = {"CL": .05, "MCL": .05, "NG": .005, "QG": .005, "GC": .5, "MGC": .5, "SI": .05, "SIL": .05,
             "HG": .005, "QC": .005, "ZB": .015625, "ZN": .015625, "6E": .0005, "M6E": .0005,
             "6J": .0000005, "M6J": .0000005}
    if r in table:
        return table[r]
    return .25 if close > 1000 and tick < .1 else tick


def price_str(price: float, root: str, tick: float) -> str:
    if not valid(price):
        return "---"
    snap = bin_(price, tick) * tick
    if root in ("ZN", "ZB", "ZF", "ZT", "UB"):
        whole = math.floor(snap)
        q = int(round((snap - whole) * 128))
        if q >= 128:
            whole, q = whole + 1, 0
        return f"{whole}'{q // 4:02d}{'0257'[q % 4]}"
    decimals, q = 0, tick
    while decimals < 8 and abs(q - round(q)) > 1e-8:
        q *= 10
        decimals += 1
    return f"{snap:.{decimals}f}"


# --- data types -----------------------------------------------------------------------------------------
class Bar:
    __slots__ = ("Start", "End", "Index", "Open", "High", "Low", "Close", "Volume")

    def __init__(self, Start, End, Open, High, Low, Close, Volume, Index=0):
        self.Start, self.End, self.Index = Start, End, Index
        self.Open, self.High, self.Low, self.Close, self.Volume = Open, High, Low, Close, Volume


class ProfileStats:
    __slots__ = ("Poc", "Vah", "Val", "Total")

    def __init__(self):
        self.Poc = self.Vah = self.Val = NAN
        self.Total = 0.0


class Profile:
    __slots__ = ("Step", "Limit", "Rows", "insertion", "PrunedRows")

    def __init__(self, step: float, limit: int):
        if step <= 0:
            raise ValueError("Profile row size must be positive")
        self.Step, self.Limit = step, max(1, limit)
        self.Rows: dict[int, float] = {}
        self.insertion: deque[int] = deque()
        self.PrunedRows = 0

    def Clone(self):
        p = Profile.__new__(Profile)
        p.Step, p.Limit, p.PrunedRows = self.Step, self.Limit, self.PrunedRows
        p.Rows, p.insertion = dict(self.Rows), deque(self.insertion)
        return p

    def Add(self, b: Bar, capped: bool):
        lo, hi = bin_(b.Low, self.Step), bin_(b.High, self.Step)
        count = max(1, hi - lo)
        if capped:
            count = min(200, count)
        per = max(0.0, b.Volume) / count
        skip = max(0, count - self.Limit)
        self.PrunedRows += skip
        rows = self.Rows
        for k in range(lo + skip, lo + count):
            v = rows.get(k)
            if v is None:
                while len(rows) >= self.Limit:
                    del rows[self.insertion.popleft()]
                    self.PrunedRows += 1
                self.insertion.append(k)
                v = 0.0
            rows[k] = v + per

    def Stats(self, pct: float) -> ProfileStats:
        s = ProfileStats()
        rows = self.Rows
        if not rows:
            return s
        mx, poc = -1.0, 0
        for k in self.insertion:
            v = rows[k]
            s.Total += v
            if v > mx:
                mx, poc = v, k
        if s.Total <= 0:
            return s
        keys = sorted(rows)
        up = dn = bisect_left(keys, poc)
        area, target, n = mx, s.Total * (pct / 100 if pct > 1 else pct), len(keys)
        while area < target and (up < n - 1 or dn > 0):
            vu = rows[keys[up + 1]] if up < n - 1 else 0
            vd = rows[keys[dn - 1]] if dn > 0 else 0
            if (vu >= vd and up < n - 1) or dn <= 0:
                up += 1
                area += rows[keys[up]]
            else:
                dn -= 1
                area += rows[keys[dn]]
        s.Poc, s.Vah, s.Val = poc * self.Step, keys[up] * self.Step, keys[dn] * self.Step
        return s


class Rma:
    __slots__ = ("Period", "count", "sum", "Value")

    def __init__(self, n: int):
        self.Period, self.count, self.sum, self.Value = max(1, n), 0, 0.0, NAN

    def Add(self, x: float) -> float:
        if not valid(x):
            return self.Value
        self.count += 1
        if self.count <= self.Period:
            self.sum += x
            if self.count == self.Period:
                self.Value = self.sum / self.Period
        else:
            self.Value = (self.Value * (self.Period - 1) + x) / self.Period
        return self.Value


class DailyValue:
    __slots__ = ("Date", "Close", "High", "Low", "Atr14", "Atr")


class DailyHistory:
    def __init__(self, atr: int):
        self.data: list[DailyValue] = []
        self.r14, self.ra, self.prev = Rma(14), Rma(atr), NAN

    def Add(self, date: datetime, h: float, l: float, c: float):
        date = datetime(date.year, date.month, date.day)
        if self.data and date <= self.data[-1].Date:
            return
        tr = max(h - l, abs(h - self.prev), abs(l - self.prev)) if valid(self.prev) else h - l
        d = DailyValue()
        d.Date, d.Close, d.High, d.Low, d.Atr14, d.Atr = date, c, h, l, self.r14.Add(tr), self.ra.Add(tr)
        self.data.append(d)
        self.prev = c

    def Before(self, date: datetime) -> DailyValue | None:
        date = datetime(date.year, date.month, date.day)
        for d in reversed(self.data):
            if d.Date < date:
                return d
        return None


class Level:
    __slots__ = ("Name", "Price", "Kind", "Flipped")   # Kind: 0 pivot, -1 resistance, 1 support

    def __init__(self, Name, Price, Kind, Flipped=False):
        self.Name, self.Price, self.Kind, self.Flipped = Name, Price, Kind, Flipped

    def Clone(self):
        return Level(self.Name, self.Price, self.Kind, self.Flipped)


class LineItem:
    __slots__ = ("Name", "Group", "Price", "Start", "End", "Color", "Width", "Offset", "Style", "Size", "Extend",
                 "Label", "TextColor")


class ZoneItem:
    __slots__ = ("Top", "Bottom", "Start", "Created", "Color", "Live", "BorderColor", "BorderWidth", "BorderStyle")

    def __init__(self, Top, Bottom, Start, Color, Created=DMIN, Live=False, BorderColor=0, BorderWidth=0,
                 BorderStyle="Solid"):
        self.Top, self.Bottom, self.Start, self.Created, self.Color, self.Live = Top, Bottom, Start, Created, Color, Live
        self.BorderColor, self.BorderWidth, self.BorderStyle = BorderColor, BorderWidth, BorderStyle


class Naked:
    __slots__ = ("Kind", "Price", "Start", "Width")

    def __init__(self, Kind, Price, Start, Width):
        self.Kind, self.Price, self.Start, self.Width = Kind, Price, Start, Width


class Poor:
    __slots__ = ("Price", "High", "Start", "Created")

    def __init__(self, Price, High, Start, Created):
        self.Price, self.High, self.Start, self.Created = Price, High, Start, Created


class TpoRow:
    __slots__ = ("Hits", "Last", "First")

    def __init__(self, Hits=0, Last=-1, First=DMIN):
        self.Hits, self.Last, self.First = Hits, Last, First

    def Clone(self):
        return TpoRow(self.Hits, self.Last, self.First)


class PastProfile:
    __slots__ = ("Profile", "Stats", "Start", "End", "EndIndex")

    def __init__(self, Profile, Stats, Start, End, EndIndex):
        self.Profile, self.Stats, self.Start, self.End, self.EndIndex = Profile, Stats, Start, End, EndIndex


class Notice:
    __slots__ = ("Key", "Text")

    def __init__(self, Key, Text):
        self.Key, self.Text = Key, Text


class LevelWatch:
    __slots__ = ("Name", "Price", "HalfWidth", "Enabled", "Moving")

    def __init__(self, Name, Price, HalfWidth, Enabled, Moving):
        self.Name, self.Price, self.HalfWidth, self.Enabled, self.Moving = Name, Price, HalfWidth, Enabled, Moving


class Frame:
    def __init__(self, bar: Bar):
        self.Bar = bar
        self.RthVwap = self.DailyAtr = NAN
        self.Watches: list[LevelWatch] = []
        self.Notices: list[Notice] = []
        self.Lines: list[LineItem] = []
        self.Zones: list[ZoneItem] = []
        self.Profiles: list[PastProfile] = []
        self.Labels: list[str] = []
        self.Values: list[str] = []
        self.Colors: list[int] = []
        self.Warning = ""
        self.RuleActive = self.RuleConfirmed = False
        self.VaHigh = self.VaLow = NAN


# --- levels document (the paste line and the labeled block) ---------------------------------------------
_PASTE_LINE = re.compile(r"^(\d{4}-\d{2}-\d{2})\s*,(.*)$")
_BAND_LINE = re.compile(r"^[UVND]\|")


def _to_price(s: str) -> float:
    s = s.strip()
    if not re.fullmatch(r"[+-]?(\d+(\.\d*)?|\.\d+)([eE][+-]?\d+)?", s):
        raise ValueError(s)
    v = float(s)
    if not valid(v):
        raise ValueError(s)
    return v


def levels_from_prices(pivot: float, prices) -> list[Level]:
    """ParsePaste: above the pivot is resistance (RES1 nearest), below is support (SUPP1 nearest)."""
    prices = list(prices)
    if any(abs(x - pivot) > abs(pivot) * .2 for x in prices):
        raise ValueError("A paste-line price is more than 20% from the pivot. Write prices without thousands separators.")
    if any(x == pivot for x in prices):
        raise ValueError("A paste-line price equals the pivot.")
    res = sorted(x for x in prices if x > pivot)
    sup = sorted((x for x in prices if x < pivot), reverse=True)
    if not res or not sup:
        raise ValueError("The paste line needs at least one price above and one below the pivot.")
    if len(res) > 50 or len(sup) > 50:
        raise ValueError("Maximum 50 prices on each side of the pivot.")
    out = [Level("PIVOT", pivot, 0)]
    out += [Level(f"RES{i + 1}", p, -1) for i, p in enumerate(res)]
    out += [Level(f"SUPP{i + 1}", p, 1) for i, p in enumerate(sup)]
    return out


def parse_levels(text: str) -> tuple[list[Level], datetime | None, str | None]:
    """LevelsDocument.Parse: returns (levels, date, symbol). Raises ValueError with the C# messages."""
    if not text or not text.strip():
        return [], None, None
    if len(text) > 1048576:
        raise ValueError("Levels input exceeds 1 MB.")
    found, labeled, header_date, paste, labeled_error, symbol = set(), [], None, None, None, None
    for raw in (x for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n") for x in line.split("||")):
        line = raw.strip().strip("﻿`* \t")
        if not line or line.startswith("#") or _BAND_LINE.match(line):
            continue
        if _PASTE_LINE.match(line):
            if paste is not None:
                raise ValueError("Use one paste line per symbol.")
            paste = line
            continue
        m = re.match(r"^(?:Date|Trading Date)\s*:\s*(.*)$", line, re.I)
        if m:
            try:
                parsed = datetime.strptime(m.group(1).strip(), "%Y-%m-%d")
            except ValueError:
                parsed = None
            if header_date is not None or parsed is None:
                raise ValueError("Use one Date: YYYY-MM-DD line.")
            header_date = parsed
            continue
        m = re.match(r"^Symbol\s*:\s*([A-Za-z0-9]+)\s*$", line, re.I)
        if m:
            if symbol is not None:
                raise ValueError("Use one Symbol: line.")
            symbol = m.group(1).upper()
            continue
        m = re.match(r"^(Pivot Level|Resistance Levels|Support Levels)\s*:\s*(.*)$", line, re.I)
        if not m or labeled_error is not None:
            continue
        head = m.group(1).lower()
        kind = 0 if head.startswith("pivot") else -1 if head.startswith("resistance") else 1
        if kind in found:
            labeled_error = "Duplicate levels section: " + m.group(1)
            continue
        found.add(kind)
        values = m.group(2).split(",")
        if kind == 0 and len(values) != 1:
            labeled_error = "Pivot requires one price without thousands separators."
            continue
        if kind != 0 and len(values) > 50:
            labeled_error = "Maximum 50 prices in each support/resistance section."
            continue
        try:
            for i, v in enumerate(values):
                labeled.append(Level("PIVOT" if kind == 0 else ("SUPP" if kind == 1 else "RES") + str(i + 1),
                                     _to_price(v), kind))
        except ValueError:
            labeled_error = f"Invalid price in {m.group(1)}. Use decimal prices separated by commas."
    if paste is not None:
        p = _PASTE_LINE.match(paste)
        try:
            date = datetime.strptime(p.group(1), "%Y-%m-%d")
        except ValueError:
            raise ValueError("The paste line must start with a YYYY-MM-DD date.")
        prices = []
        for item in p.group(2).split(","):
            try:
                prices.append(_to_price(item))
            except ValueError:
                raise ValueError(f"Invalid price in the paste line: '{item.strip()}'. Use decimal prices separated by commas.")
        levels = levels_from_prices(prices[0], prices[1:])
        if header_date is not None and header_date != date:
            raise ValueError("The Date: line and the paste-line date differ.")
    else:
        if labeled_error is not None:
            raise ValueError(labeled_error)
        if not found:
            raise ValueError("No levels found. Paste the levels line (2026-09-30, pivot, resistance..., support...) "
                             "or the Pivot Level / Resistance Levels / Support Levels block.")
        if len(found) != 3:
            raise ValueError("Include Pivot Level:, Resistance Levels:, and Support Levels:.")
        levels, date = labeled, header_date
    pivot = next(x for x in levels if x.Kind == 0)
    for lv in levels:
        if abs(lv.Price - pivot.Price) > abs(pivot.Price) * .2:
            raise ValueError(f"Price {lv.Price!r} is more than 20% from the pivot. Write prices without thousands separators.")
    return levels, date, symbol


# --- Engine ---------------------------------------------------------------------------------------------
def _max(a, b):
    return max(a, b) if valid(a) else b


def _min(a, b):
    return min(a, b) if valid(a) else b


class Engine:
    """A completed chart bar is committed once. Live previews run on Clone()."""

    def __init__(self, s: Settings, root: str, tick: float, initial_close: float, minutes: int):
        self.S, self.Root, self.Tick, self.Minutes = s, root, tick, minutes
        self.Step = auto_tick(root, tick, initial_close) if s.tick_auto else s.tick_manual_val
        self.rowMult = row_mult(s.row_scale, initial_close, self.Step)
        lim = s.MaxProfileRows
        self.main = Profile(self.RowStep(self.rowMult), lim)
        self.day = Profile(self.ValueStep(self.rowMult), lim)
        self.macro = Profile(self.ValueStep(self.rowMult), lim)
        self.nd = Profile(self.NakedStep(self.rowMult), lim)
        self.nw = Profile(self.NakedStep(self.rowMult), lim)
        self.nm = Profile(self.NakedStep(self.rowMult), lim)
        self.spStep = self.BlockStep(self.rowMult)
        self.Levels: list[Level] = parse_levels(s.lvl_text)[0]
        self.prevDay, self.prevMonth = ProfileStats(), ProfileStats()
        self.profiles: list[PastProfile] = []
        self.naked: list[Naked] = []
        self.single: list[ZoneItem] = []
        self.poor: list[Poor] = []
        self.tpo: dict[int, TpoRow] = {}
        self.tpoOrder: deque[int] = deque()
        self.rowsPruned = self.structuresPruned = False
        self.previous: Bar | None = None
        self.td = self.week = self.month = self.mainKey = self.mainStart = self.weekStart = self.monthStart = DMIN
        self.dayStart = self.rthStart = self.ibStart = self.settleStart = self.spikeStart = self.macroKey = DMIN
        for n in ("tradeH", "tradeL", "weekH", "weekL", "monthH", "monthL", "pdH", "pdL", "pdO", "pwH", "pwL",
                  "pmH", "pmL", "weekOpen", "rthH", "rthL", "rthO", "rthPreviousOpen", "ibH", "ibL", "ovnH", "ovnL",
                  "settle", "spike", "preH", "preL", "dayH", "dayL", "lastMacroClose"):
            setattr(self, n, NAN)
        self.ibWorkH = self.ibWorkL = self.frozenH = self.frozenL = self.tableH = self.tableL = 0.0
        self.vSrc = self.vVol = self.contextHlc = self.contextVol = 0.0
        self.above = self.below = self.total = self.otfUp = self.otfDn = 0
        self.tpoPeriod = -1
        for n in ("trap", "resolved", "trapAlert", "bullSpike", "auction", "isTrapDay", "outsideAbove",
                  "outsideBelow", "gapUp", "gapDown", "rule", "dayFull", "weekFull", "monthFull", "macroFull"):
            setattr(self, n, False)
        self.vaEntry: datetime | None = None
        # The prior day's value from its trades (a tick volume profile, as the SVP): TradeValue(start, end) gives the
        # RTH window's [(price, volume)] when they are worked out, else None and the day's minutes are used, as the
        # NinjaTrader original (each minute's volume spread over its rows). vaFirst/vaEnd: that window.
        self.TradeValue = None
        self.vaFirst = self.vaEnd = DMIN
        self.inventoryFrozen, self.dayType, self.pivotBias = "---", "DEVELOPING", "---"
        self.dayColor = GRAY

    # row sizes
    def RowStep(self, mult):
        return max(self.Step * mult, self.S.mvp_row_size)

    def ValueStep(self, mult):
        return max(self.S.mvp_row_size, self.Step * mult) if mult > 1 else self.S.mvp_row_size

    def NakedStep(self, mult):
        S = self.S
        return (self.Step if S.amt_vauto else S.amt_res_val if S.amt_vres else self.Step) * mult

    def BlockStep(self, mult):
        return self.Step * max(1, self.S.sp_ticks_per_block) * mult

    def DayValue(self) -> ProfileStats:
        """The day just ended: from its trades at one-tick rows when they are worked out (pdPOC, pdVAH, pdVAL then match
        a tick volume profile and the SVP), else from its minutes."""
        rows = self.TradeValue(self.vaFirst, self.vaEnd) if self.TradeValue and self.vaFirst != DMIN else None
        if not rows:
            return self.day.Stats(self.S.cc_va_pct)
        step = self.Tick if self.Tick > 0 else self.Step
        p = Profile(step, 1)
        for price, volume in sorted(rows):
            k = bin_(price, step)
            if k not in p.Rows:
                p.Rows[k] = 0.0
                p.insertion.append(k)
            p.Rows[k] += volume
        return p.Stats(self.S.cc_va_pct)

    def Clone(self) -> "Engine":
        e = Engine.__new__(Engine)
        e.__dict__.update(self.__dict__)
        for n in ("day", "main", "macro", "nd", "nw", "nm"):
            setattr(e, n, getattr(self, n).Clone())
        e.Levels = [x.Clone() for x in self.Levels]
        e.naked, e.single, e.poor, e.profiles = list(self.naked), list(self.single), list(self.poor), list(self.profiles)
        e.tpo = {k: v.Clone() for k, v in self.tpo.items()}
        e.tpoOrder = deque(self.tpoOrder)
        return e

    def ReplaceLevels(self, levels: list[Level]):
        self.Levels = [x.Clone() for x in levels]
        self.pivotBias = "---"

    def Was(self, session):
        return self.previous is not None and self.Ins(self.previous.Start, session)

    # The instrument's own calendar and regular hours (the runner sets them; the NinjaTrader original has neither:
    # its RTH is ES's 08:30-15:00 and a holiday's short session is a day of its own).
    Calendar: str | None = None
    RthSession = "0830-1500"

    def TradeDate(self, t: datetime) -> datetime:
        d = trading_date(t)
        if self.Calendar:
            f = sessions.fold_trade_date(d.date(), self.Calendar)
            if f != d.date():
                return datetime(f.year, f.month, f.day)
        return d

    def Halted(self, t: datetime) -> bool:
        """A holiday halt's short session (MLK Day before 12:00): no RTH, IB or settlement; it counts as overnight."""
        return bool(self.Calendar) and t.hour < 17 and sessions.holiday_halt(t.date(), self.Calendar)

    def Ins(self, t: datetime, session: str) -> bool:
        if self.Halted(t):
            return session == self.S.amt_sess
        return in_session(t, session)

    def Price(self, v):
        return price_str(v, self.Root, self.Tick)

    def Event(self, f: Frame, key, text):
        f.Notices.append(Notice(key, text))

    def Cross(self, f: Frame, name, p, enabled, moving, halfWidth=-1.0):
        if valid(p):
            f.Watches.append(LevelWatch(name, p, halfWidth if halfWidth >= 0 else self.S.AlertZoneTicks * self.Tick,
                                        enabled, moving))

    def Inventory(self):
        if not self.S.inv_enable or self.total == 0:
            return "---"
        l, s = 100.0 * self.above / self.total, 100.0 * self.below / self.total
        if l == 100:
            return "TRAP LONG (100%)"
        if s == 100:
            return "TRAP SHORT (100%)"
        if l > 60:
            return f"LONG ({_fmt0(l)}%)"
        if s > 60:
            return f"SHORT ({_fmt0(s)}%)"
        return "BALANCED"

    def NakedRoll(self, p: Profile, kind, complete, show, width, start):
        poc = p.Stats(.68).Poc
        if self.S.npoc_enable and complete and show and valid(poc):
            self.naked.append(Naked(kind, poc, start, width))

    def ProfileKey(self, b: Bar) -> datetime:
        d = self.TradeDate(b.Start)
        mode = self.S.mvp_tf_mode
        if mode == "Weekly":
            return week(d)
        if mode == "Monthly":
            return month(d)
        if mode == "Yearly":
            return datetime(d.year, 1, 1)
        return datetime(b.Start.year, b.Start.month, b.Start.day)

    @staticmethod
    def Aggregate(inp: list[Bar], size: int) -> list[Bar]:
        result, cur, bucket = [], None, DMIN
        for b in inp:
            k = datetime(b.Start.year, b.Start.month, b.Start.day) + timedelta(minutes=(minute_of(b.Start) // size) * size)
            if cur is None or k != bucket:
                cur = Bar(b.Start, b.End, b.Open, b.High, b.Low, b.Close, b.Volume)
                result.append(cur)
                bucket = k
            else:
                cur.High, cur.Low, cur.Close = max(cur.High, b.High), min(cur.Low, b.Low), b.Close
                cur.Volume += b.Volume
                cur.End = b.End
        return result

    def AddMacro(self, b: Bar):
        """Previous-month profile consumes independently completed 30/60-minute bars; never a future HTF close."""
        key = month(self.TradeDate(b.Start))
        self.RollMacro(key, self.lastMacroClose if valid(self.lastMacroClose) else b.Close)
        self.macro.Add(b, True)
        self.lastMacroClose = b.Close

    def RollMacro(self, key, lastPrice):
        if self.macroKey != key:
            if self.macroKey != DMIN and self.macroFull:
                self.prevMonth = self.macro.Stats(self.S.mvp_val_pct)
            self.macroFull = self.macroKey != DMIN
            self.macro = Profile(self.ValueStep(row_mult(self.S.row_scale, lastPrice, self.Step)), self.S.MaxProfileRows)
            self.macroKey = key

    def Process(self, b: Bar, subbars: list[Bar] | None, daily: DailyValue | None, visuals: bool = True) -> Frame:
        """visuals=False skips the developing-profile stats and the frame's drawing lists (history replay);
        the engine state it leaves behind is identical."""
        S = self.S
        f = Frame(b)
        d = self.TradeDate(b.Start)
        w, m = week(d), month(d)
        newDay, newWeek, newMonth = self.td != d, self.week != w, self.month != m
        lastPrice = self.previous.Close if self.previous is not None else b.Close
        if newMonth:
            self.rowMult = row_mult(S.row_scale, lastPrice, self.Step)
        self.RollMacro(m, lastPrice)
        rth = self.Ins(b.Start, self.RthSession)
        ib = self.Ins(b.Start, S.ib_session)
        ovn = self.Ins(b.Start, S.amt_sess)
        pdSession = self.Ins(b.Start, S.pd_sess_time)
        track = self.Ins(b.Start, S.context_session)
        table = self.Ins(b.Start, S.tbl_session)
        spikeWindow = self.Ins(b.Start, S.spikeTimeRange)
        spSession = self.Ins(b.Start, S.sp_rth_session)
        lim = S.MaxProfileRows
        if newDay:
            if self.td != DMIN and self.dayFull and self.day.Rows:
                self.prevDay = self.DayValue()
            if not S.pd_use_rth and self.td != DMIN:
                self.pdH, self.pdL = self.tradeH, self.tradeL
            self.NakedRoll(self.nd, "Daily", self.dayFull, S.npoc_d_show, S.npoc_w_d, b.Start)
            self.dayFull = self.td != DMIN or minute_of(b.Start) == 1020
            self.day = Profile(self.ValueStep(self.rowMult), lim)
            self.vaFirst = self.vaEnd = DMIN
            self.nd = Profile(self.NakedStep(self.rowMult), lim)
            self.td, self.dayStart, self.tradeH, self.tradeL = d, b.Start, b.High, b.Low
            self.rule, self.vaEntry, self.rthO = False, None, NAN
            self.contextHlc = self.contextVol = 0.0
            self.above = self.below = self.total = 0
            self.trapAlert = self.trap = self.resolved = False
        else:
            self.tradeH, self.tradeL = _max(self.tradeH, b.High), _min(self.tradeL, b.Low)
        if newWeek:
            if self.weekFull:
                self.pwH, self.pwL = self.weekH, self.weekL
            self.NakedRoll(self.nw, "Weekly", self.weekFull, S.npoc_w_show, S.npoc_w_w, b.Start)
            sunday_open = minute_of(b.Start) == 1020 and (b.Start.weekday() == 6 or bool(self.Calendar))   # a week's first open
            self.weekFull = self.week != DMIN or sunday_open
            self.week, self.weekStart, self.weekH, self.weekL = w, b.Start, b.High, b.Low
            self.nw = Profile(self.NakedStep(self.rowMult), lim)
            self.weekOpen = b.Open if sunday_open else NAN
        else:
            self.weekH, self.weekL = _max(self.weekH, b.High), _min(self.weekL, b.Low)
        if newMonth:
            if self.monthFull:
                self.pmH, self.pmL = self.monthH, self.monthL
            self.NakedRoll(self.nm, "Monthly", self.monthFull, S.npoc_m_show, S.npoc_w_m, b.Start)
            self.monthFull = self.month != DMIN
            self.month, self.monthStart, self.monthH, self.monthL = m, b.Start, b.High, b.Low
            self.nm = Profile(self.NakedStep(self.rowMult), lim)
        else:
            self.monthH, self.monthL = _max(self.monthH, b.High), _min(self.monthL, b.Low)
        # Naked POC profiles use the 1-minute bars inside this chart bar; the chart bar is the fallback.
        if S.npoc_enable:
            for one in (subbars if subbars else [b]):
                if S.npoc_d_show:
                    self.nd.Add(one, False)
                if S.npoc_w_show:
                    self.nw.Add(one, False)
                if S.npoc_m_show:
                    self.nm.Add(one, False)
        for i in range(len(self.naked) - 1, -1, -1):
            n = self.naked[i]
            if b.Low <= n.Price and b.High >= n.Price:
                if S.npoc_alert:
                    self.Event(f, "nPOC" + n.Kind + self.Price(n.Price), n.Kind + " nPOC Touch @ " + self.Price(n.Price))
                del self.naked[i]
        if ovn and S.amt_enable:
            if not self.Was(S.amt_sess) or newDay:
                self.ovnH, self.ovnL = b.High, b.Low
            else:
                self.ovnH, self.ovnL = _max(self.ovnH, b.High), _min(self.ovnL, b.Low)
        if pdSession:
            if not self.Was(S.pd_sess_time):
                self.rthH, self.rthL, self.rthPreviousOpen = b.High, b.Low, b.Open
            else:
                self.rthH, self.rthL = _max(self.rthH, b.High), _min(self.rthL, b.Low)
        if not pdSession and self.Was(S.pd_sess_time):
            if S.pd_use_rth:
                self.pdH, self.pdL = self.rthH, self.rthL
            self.pdO = self.rthPreviousOpen
        if daily is not None:
            if not valid(self.pdH):
                self.pdH = daily.High
            if not valid(self.pdL):
                self.pdL = daily.Low
        if not rth and self.Was(self.RthSession):
            self.settle, self.settleStart = self.previous.Close, b.Start
            self.trap = self.resolved = False
        mb = minute_of(b.Start)
        inv_end = S.inv_end_h * 60 + S.inv_end_m
        globex = mb >= S.inv_start_h * 60 or mb < inv_end or self.Halted(b.Start)
        if globex and valid(self.settle):
            if b.Low >= self.settle:
                self.above += 1
            elif b.High <= self.settle:
                self.below += 1
            self.total += 1
        imbalance = self.total > 0 and (self.above == self.total or self.below == self.total)
        # Latch only while overnight bars are being counted.
        if globex and imbalance and not self.resolved:
            self.trap = True
        if self.trap and b.Low <= self.settle and b.High >= self.settle:
            self.trap, self.resolved = False, True
        if S.inv_alert and S.inv_enable and self.trap and not self.trapAlert and inv_end <= mb < inv_end + 30:
            self.Event(f, "Inventory", "Inventory Trap Active @ " + self.Price(self.settle))
            self.trapAlert = True
        if ib:
            if not self.Was(S.ib_session) or newDay:
                self.ibH = self.ibL = NAN
                self.ibStart, self.ibWorkH, self.ibWorkL = b.Start, b.High, b.Low
            else:
                self.ibWorkH, self.ibWorkL = max(self.ibWorkH, b.High), min(self.ibWorkL, b.Low)
        if not ib and self.Was(S.ib_session) and S.ib_enable:
            self.ibH, self.ibL = self.ibWorkH, self.ibWorkL
        if track and (not self.Was(S.context_session) or newDay):
            self.inventoryFrozen = self.Inventory()
        if self.Ins(b.Start, S.cc_sess_def) and (not self.Was(S.cc_sess_def) or newDay):
            self.rthO = b.Open
        if rth and (not self.Was(self.RthSession) or newDay):
            self.rthStart, self.preH, self.preL, self.dayH, self.dayL = b.Start, NAN, NAN, b.High, b.Low
            self.auction, self.otfUp, self.otfDn = False, 0, 0
            openPrice = self.rthO if valid(self.rthO) else b.Open
            self.isTrapDay = S.inv_enable and imbalance
            self.gapUp = valid(self.pdH) and openPrice > self.pdH
            self.gapDown = valid(self.pdL) and openPrice < self.pdL
            self.outsideAbove = valid(self.prevDay.Vah) and openPrice > self.prevDay.Vah
            self.outsideBelow = valid(self.prevDay.Val) and openPrice < self.prevDay.Val
        if rth and not spikeWindow:
            self.preH, self.preL = _max(self.preH, b.High), _min(self.preL, b.Low)
        if spikeWindow and not self.Was(S.spikeTimeRange):
            self.frozenH, self.frozenL, self.spikeStart = self.preH, self.preL, b.Start
        if not spikeWindow and self.Was(S.spikeTimeRange):
            self.spike = NAN
            if S.showSpike and self.previous is not None:
                self.bullSpike = self.previous.Close > self.frozenH
                if self.bullSpike or self.previous.Close < self.frozenL:
                    self.spike = self.frozenH if self.bullSpike else self.frozenL
        if not spikeWindow and valid(self.spike) and b.Low <= self.spike and b.High >= self.spike:
            if S.spike_alert:
                self.Event(f, "Spike", "Spike Base Touch @ " + self.Price(self.spike))
            self.spike = NAN
        # All profile inputs are available sub-bars only; the upper-exclusive, 200-row cap is retained.
        raw = list(subbars) if subbars else [b]
        shared = 1
        try:
            precision = int(S.mvp_calc_tf)
        except ValueError:
            precision = 1
        if precision < 1:
            precision = 1
        calc = precision if precision != shared and self.Minutes > precision else shared
        pk = self.ProfileKey(b)
        mainWindow = S.mvp_tf_mode != "Session" or self.Ins(b.Start, S.mvp_sess_time)
        if mainWindow and self.mainKey != pk:
            if self.main.Rows:
                self.profiles.append(PastProfile(self.main, self.main.Stats(S.mvp_val_pct), self.mainStart, b.Start, b.Index))
                if len(self.profiles) > 12:
                    del self.profiles[0]
            self.main = Profile(self.RowStep(self.rowMult), lim)
            self.mainKey, self.mainStart = pk, b.Start
        # Prior-day value is built from the RTH session for VA only (08:30-15:00 CT by default).
        for q in self.Aggregate(raw, calc):
            if mainWindow:
                self.main.Add(q, True)
            if S.cc_enable and self.Ins(q.Start, S.cc_sess_def):
                self.day.Add(q, True)
                if self.vaFirst == DMIN:
                    self.vaFirst = q.Start
                self.vaEnd = q.End
        dev = self.main.Stats(S.mvp_val_pct) if visuals else ProfileStats()
        self.UpdateTpo(b, spSession)
        self.contextHlc += (b.High + b.Low + b.Close) / 3 * b.Volume
        self.contextVol += b.Volume
        dailyVwap = self.contextHlc / self.contextVol if self.contextVol > 0 else NAN
        vstart, vend = S.vwap_start_h * 60 + S.vwap_start_m, S.vwap_end_h * 60 + S.vwap_end_m
        vwin = vstart <= mb < vend and not self.Halted(b.Start)
        pm = -1 if self.previous is None else minute_of(self.previous.Start)
        pvwin = vstart <= pm < vend and not self.Halted(self.previous.Start)
        if vwin:
            if not pvwin or newDay:
                self.vSrc = self.vVol = 0.0
            self.vSrc += (b.High + b.Low + b.Close) / 3 * b.Volume
            self.vVol += b.Volume
        else:
            self.vSrc = self.vVol = 0.0
        f.RthVwap = self.vSrc / self.vVol if S.vwap_enable and vwin and self.vVol > 0 else NAN
        dtr = NAN if daily is None else max(self.tradeH - self.tradeL, abs(self.tradeH - daily.Close), abs(self.tradeL - daily.Close))
        L = S.daily_atr_length
        f.DailyAtr = (daily.Atr * (L - 1) + dtr) / L if daily is not None and valid(daily.Atr) else NAN
        zoneAtr = (daily.Atr14 * 13 + dtr) / 14 if daily is not None and valid(daily.Atr14) else NAN
        if rth:
            self.Classify(b, dailyVwap, f.DailyAtr)
        if track:
            pivot = next((x for x in self.Levels if x.Kind == 0), None)
            if pivot is not None:
                z = (f.DailyAtr if valid(f.DailyAtr) else 1) * .005
                self.pivotBias = "BULLISH" if b.Close > pivot.Price + z else "BEARISH" if b.Close < pivot.Price - z else "AT PIVOT"
        f.RuleActive = S.cc_enable and valid(self.rthO) and (self.rthO > self.prevDay.Vah or self.rthO < self.prevDay.Val)
        if f.RuleActive and pdSession:
            if self.prevDay.Val < b.Close < self.prevDay.Vah:
                if self.vaEntry is None:
                    self.vaEntry = b.Start
                if not self.rule and (b.Start - self.vaEntry).total_seconds() / 60 >= 60:
                    self.rule = True
                    if S.cc_alert_80:
                        self.Event(f, "Rule80", "80% Rule Confirmed")
            elif not self.rule:
                self.vaEntry = None
        if not pdSession and not self.rule:
            self.vaEntry = None
        f.RuleConfirmed, f.VaHigh, f.VaLow = self.rule, self.prevDay.Vah, self.prevDay.Val
        flip = zoneAtr * S.lvl_flip_atr if valid(zoneAtr) else 0
        for lv in self.Levels:
            if S.lvl_color_flip and lv.Kind != 0:
                if lv.Kind == -1:
                    if not lv.Flipped and b.Close > lv.Price + flip:
                        lv.Flipped = True
                    elif lv.Flipped and b.Close < lv.Price - flip:
                        lv.Flipped = False
                else:
                    if not lv.Flipped and b.Close < lv.Price - flip:
                        lv.Flipped = True
                    elif lv.Flipped and b.Close > lv.Price + flip:
                        lv.Flipped = False
            if visuals:
                self.Cross(f, lv.Name, lv.Price, S.lvl_alerts and (S.lvl_alert_ip if lv.Kind == 0 else S.lvl_alert_bull if lv.Kind == -1 else S.lvl_alert_bear),
                           False, self.LevelZoneHalfWidth(zoneAtr))
        if visuals:
            ib_alert = not ib and self.Ins(b.Start, S.ib_alert_time)
            self.Cross(f, "IBH", self.ibH, S.ib_enable and S.ib_alert_ibh and ib_alert, False)
            self.Cross(f, "IBL", self.ibL, S.ib_enable and S.ib_alert_ibl and ib_alert, False)
            self.Cross(f, "OVN High", self.ovnH, S.amt_enable and S.amt_alert_ovn and not ovn, False)
            self.Cross(f, "OVN Low", self.ovnL, S.amt_enable and S.amt_alert_ovn and not ovn, False)
            self.Cross(f, "RTH VWAP", f.RthVwap, S.vwap_alert and vwin and pvwin, True)
            self.Cross(f, "PDH", self.pdH, S.pd_alert_hl, False)
            self.Cross(f, "PDL", self.pdL, S.pd_alert_hl, False)
            self.Cross(f, "pwHigh", self.pwH, S.pw_alert_hl, False)
            self.Cross(f, "pwLow", self.pwL, S.pw_alert_hl, False)
            self.Cross(f, "Weekly Open", self.weekOpen, S.wk_alert_open, False)
            self.Cross(f, "pdPOC", self.prevDay.Poc, S.cc_alert_va, False)
            self.Cross(f, "pdVAH", self.prevDay.Vah, S.cc_alert_va, False)
            self.Cross(f, "pdVAL", self.prevDay.Val, S.cc_alert_va, False)
            da = S.mvp_use_cur_alerts and self.Ins(b.Start, S.mvp_alert_time)
            self.Cross(f, "Developing POC", dev.Poc, da, True)
            self.Cross(f, "Developing VAH", dev.Vah, da, True)
            self.Cross(f, "Developing VAL", dev.Val, da, True)
            self.Cross(f, "pmPOC", self.prevMonth.Poc, S.macro_alert_lvl, False)
            self.Cross(f, "pmVAH", self.prevMonth.Vah, S.macro_alert_lvl, False)
            self.Cross(f, "pmVAL", self.prevMonth.Val, S.macro_alert_lvl, False)
            self.Cross(f, "pmHigh", self.pmH, S.macro_alert_lvl, False)
            self.Cross(f, "pmLow", self.pmL, S.macro_alert_lvl, False)
        if table and not self.Was(S.tbl_session):
            self.tableH, self.tableL = b.High, b.Low
        elif table:
            self.tableH, self.tableL = max(self.tableH, b.High), min(self.tableL, b.Low)
        self.PruneStructures()
        self.rowsPruned |= any(p.PrunedRows > 0 for p in (self.day, self.main, self.macro, self.nd, self.nw, self.nm))
        if visuals:
            self.BuildVisuals(f, dev, zoneAtr, table, ovn, ib)
        else:
            self.DayLines(f)
        if self.rowsPruned:
            f.Warning += " Row limit reached: oldest rows pruned; profile/TPO values use retained rows."
        if self.structuresPruned:
            f.Warning += " Structure limit reached: oldest structures pruned."
        self.previous = b
        return f

    def UpdateTpo(self, b: Bar, active: bool):
        S = self.S
        was = self.Was(S.sp_rth_session)
        ended = not active and was
        if active and not was:
            self.tpo.clear()
            self.tpoOrder.clear()
            self.single = [x for x in self.single if not x.Live]
            self.tpoPeriod = -1
            self.spStep = self.BlockStep(self.rowMult)
        pid = minute_of(b.Start) // max(1, S.sp_tpo_minutes)
        newPeriod = active and pid != self.tpoPeriod
        if active:
            lo, hi = math.floor(b.Low / self.spStep), math.floor(b.High / self.spStep)
            if hi - lo + 1 > S.MaxProfileRows:
                lo = hi - S.MaxProfileRows + 1
                self.rowsPruned = True
            for k in range(lo, hi + 1):
                row = self.tpo.get(k)
                if row is None:
                    while len(self.tpo) >= S.MaxProfileRows:
                        del self.tpo[self.tpoOrder.popleft()]
                        self.rowsPruned = True
                    row = TpoRow(0, -1, b.Start)
                    self.tpo[k] = row
                    self.tpoOrder.append(k)
                if row.Last != pid:
                    row.Hits += 1
                    row.Last = pid
            self.tpoPeriod = pid
        if (newPeriod or ended) and self.tpo:
            self.single = [x for x in self.single if not x.Live]
            high, low = max(self.tpo), min(self.tpo)
            candidates = sorted(k for k, v in self.tpo.items() if v.Hits == 1)
            start = 0
            for i in range(len(candidates)):
                if i + 1 < len(candidates) and candidates[i + 1] <= candidates[i] + 2:
                    continue
                a, z = candidates[start], candidates[i]
                ht = (z - a) * self.spStep / self.Step
                tail = a <= low or z >= high
                if ht + 1e-8 >= S.sp_min_ticks and not (S.sp_filter_tails and tail and ht < S.sp_tail_max):
                    for j in range(start, i + 1):
                        c = candidates[j]
                        self.single.append(ZoneItem((c + 1) * self.spStep, c * self.spStep, self.tpo[c].First,
                                                    S.sp_color_hist, Created=b.Start, Live=not ended,
                                                    BorderColor=S.sp_border_c, BorderWidth=S.sp_border_w,
                                                    BorderStyle=S.sp_border_style))
                start = i + 1
            if S.sp_show_poor:
                hp, lp = (high + 1) * self.spStep, low * self.spStep
                if self.tpo[high].Hits >= 2 and not any(x.High and abs(x.Price - hp) < self.Step * 2 for x in self.poor):
                    self.poor.append(Poor(hp, True, self.tpo[high].First, b.Start))
                if self.tpo[low].Hits >= 2 and not any(not x.High and abs(x.Price - lp) < self.Step * 2 for x in self.poor):
                    self.poor.append(Poor(lp, False, self.tpo[low].First, b.Start))
        maxd = S.sp_max_days
        self.single = [x for x in self.single
                       if not ((not x.Live and (b.Start - x.Created).total_seconds() / 86400 > maxd)
                               or (x.Top > b.Low and x.Bottom < b.High))]
        self.poor = [x for x in self.poor
                     if not ((b.Start - x.Created).total_seconds() / 86400 > maxd
                             or (b.High > x.Price if x.High else b.Low < x.Price))]

    def Classify(self, b: Bar, vwap: float, dailyAtr: float):
        h = self.ibH if valid(self.ibH) else self.ovnH
        l = self.ibL if valid(self.ibL) else self.ovnL
        self.dayH, self.dayL = _max(self.dayH, b.High), _min(self.dayL, b.Low)
        if self.previous is not None:
            self.otfUp = self.otfUp + 1 if b.Low > self.previous.Low else 0
            self.otfDn = self.otfDn + 1 if b.High < self.previous.High else 0
        candidate, color, real = "DEVELOPING", GRAY, False
        if self.isTrapDay:
            candidate, color, real = "TRAP", ORANGE, True
        elif valid(h) and valid(l):
            rng, ibRange = self.dayH - self.dayL, h - l
            ext = rng / ibRange if ibRange > 0 else 0
            pos = (b.Close - self.dayL) / rng if rng > 0 else .5
            bu, bd = self.dayH > h, self.dayL < l
            cvh, cvl = b.Close > self.prevDay.Vah, b.Close < self.prevDay.Val
            iu = self.dayH > self.pdH or (self.dayH > self.ovnH and cvh)
            id_ = self.dayL < self.pdL or (self.dayL < self.ovnL and cvl)
            tu = bu and pos > .75 and vwap > (h + l) / 2 and (self.otfUp >= 5 or iu) and ext > 1.5
            tl = bd and pos < .25 and vwap < (h + l) / 2 and (self.otfDn >= 5 or id_) and ext > 1.5
            tu |= iu and pos > .8 and ext > 2
            tl |= id_ and pos < .2 and ext > 2
            real = True
            if tl:
                candidate, color = "Trend Day (Down)", RED
            elif tu:
                candidate, color = "Trend Day (Up)", GREEN
            elif bu and bd:
                candidate = "Neutral Extreme (Up)" if pos >= .65 else "Neutral Extreme (Down)" if pos <= .35 else "Neutral Day (Center)"
                color = GREEN_SOFT if pos >= .65 else RED_SOFT if pos <= .35 else GRAY
            elif dailyAtr > 0 and ibRange / dailyAtr > .5 and not bu and not bd:
                candidate = "Normal Day"
            elif bu:
                candidate = "Normal Variation (Up)" if iu or ext > 1.2 or cvh else "Normal Day (Balance)"
                color = LIME if "Up" in candidate else GRAY
            elif bd:
                candidate = "Normal Variation (Down)" if id_ or ext > 1.2 or cvl else "Normal Day (Balance)"
                color = MAROON if "Down" in candidate else GRAY
            elif not self.Ins(b.Start, self.S.ib_session) or valid(self.ibH):
                candidate = "Non-Trend Day"
            else:
                real = False
        if real:
            self.auction, self.dayType, self.dayColor = True, candidate, color
        elif not self.auction:
            self.dayType = "GAP UP" if self.gapUp else "GAP DOWN" if self.gapDown else "DEVELOPING"
            self.dayColor = LIME if self.gapUp else ALERT_RED if self.gapDown else GRAY

    def Line(self, f: Frame, name, price, start, color, width, style, offset, size, label, extend, text=0):
        if not valid(price):
            return
        li = LineItem()
        group = self.LabelGroup(name, extend)
        li.Name, li.Group, li.Price, li.Start, li.End = name, group, price, start, f.Bar.End
        li.Color, li.TextColor = color, color if (text >> 24) == 0 else text
        li.Width, li.Style, li.Offset, li.Size, li.Extend = width, style, offset, size, extend
        li.Label = label and self.ShowGroupLabel(group)
        f.Lines.append(li)

    def LevelZoneHalfWidth(self, zoneAtr):
        S = self.S
        return zoneAtr * S.lvl_zone_atr_pct if S.lvl_zone_mode == "ATR %" and valid(zoneAtr) else S.lvl_zoneWidthTicks * self.Step

    def PruneStructures(self):
        excess = len(self.naked) + len(self.poor) + len(self.single) - self.S.MaxStructures
        if excess <= 0:
            return
        self.structuresPruned = True
        items = ([(x.Start, 0, i, x) for i, x in enumerate(self.naked)] + [(x.Created, 1, i, x) for i, x in enumerate(self.poor)]
                 + [(x.Created, 2, i, x) for i, x in enumerate(self.single)])
        discard = {id(x) for _, _, _, x in sorted(items, key=lambda t: t[0])[:excess]}   # stable, as OrderBy
        self.naked = [x for x in self.naked if id(x) not in discard]
        self.poor = [x for x in self.poor if id(x) not in discard]
        self.single = [x for x in self.single if id(x) not in discard]

    @staticmethod
    def LabelGroup(name, extend):
        if extend == "Both":
            return "Levels"
        if name.startswith("IB "):
            return "IB"
        if name.startswith("OVN "):
            return "OVN"
        if name in ("pdHigh", "pdLow", "pdOpen"):
            return "PriorDay"
        if name.startswith("pd"):
            return "PriorValue"
        if name in ("dOpen", "wkOpen"):
            return "Opens"
        if name.startswith("pw"):
            return "PriorWeek"
        if name.startswith("pm"):
            return "PriorMonth"
        if name.startswith("Spike"):
            return "Spike"
        if "nPOC" in name:
            return "NakedPoc"
        if name.startswith("Poor"):
            return "Poor"
        if name.startswith("Developing") or name == "HVN":
            return "Profiles"
        return "Inventory"

    def ShowGroupLabel(self, group):
        S = self.S
        if S.HideAllLabels:
            return False
        return {"Levels": S.LabelsLevels, "Profiles": S.LabelsProfiles, "PriorDay": S.LabelsPriorDay,
                "PriorValue": S.LabelsPriorValue, "Opens": S.LabelsOpens, "PriorWeek": S.LabelsPriorWeek,
                "PriorMonth": S.LabelsPriorMonth, "Inventory": S.LabelsInventory, "Spike": S.LabelsSpike,
                "NakedPoc": S.LabelsNakedPoc, "Poor": S.LabelsPoor, "IB": S.ib_show_labels,
                "OVN": S.amt_showLb}.get(group, True)

    @staticmethod
    def StatusColor(s):
        if s.startswith("TRAP LONG") or s == "Below pdLow":
            return ALERT_RED
        if s.startswith("TRAP SHORT") or s == "Above pdHigh":
            return LIME
        if s.startswith("LONG") or s == "Above pdVAH":
            return GREEN_SOFT
        if s.startswith("SHORT") or s == "Below pdVAL":
            return RED_SOFT
        return GRAY

    def OpenLocation(self):
        if not valid(self.rthO) or not valid(self.pdH) or not valid(self.pdL):
            return "---"
        if self.rthO > self.pdH:
            return "Above pdHigh"
        if self.rthO < self.pdL:
            return "Below pdLow"
        if self.rthO > self.prevDay.Vah:
            return "Above pdVAH"
        if self.rthO < self.prevDay.Val:
            return "Below pdVAL"
        return "Inside Value" if valid(self.prevDay.Vah) else "---"

    def _ib_lines(self, f: Frame):
        S = self.S
        if S.ib_enable and valid(self.ibH):
            end = f.Bar.End
            if S.ib_limit_ext and not in_session(f.Bar.Start, S.ib_stop_time):
                t = S.ib_stop_time.split(":")[0].split("-")[1]
                end = datetime(self.ibStart.year, self.ibStart.month, self.ibStart.day) + timedelta(hours=int(t[:2]), minutes=int(t[2:]))
            n = len(f.Lines)
            self.Line(f, "IB High", self.ibH, self.ibStart, S.ib_col_hi, S.ib_lvl_width, S.ib_style_main, S.ib_lbl_offset, S.ib_label_size, S.ib_show_labels, S.ib_extend, S.ib_txt_c)
            self.Line(f, "IB Low", self.ibL, self.ibStart, S.ib_col_lo, S.ib_lvl_width, S.ib_style_main, S.ib_lbl_offset, S.ib_label_size, S.ib_show_labels, S.ib_extend, S.ib_txt_c)
            if S.ib_show_mid:
                self.Line(f, "IB Midpoint", (self.ibH + self.ibL) / 2, self.ibStart, S.ib_col_mid, S.ib_lvl_width, S.ib_style_int, S.ib_lbl_offset, S.ib_label_size, S.ib_show_labels, S.ib_extend, S.ib_txt_c)
            for li in f.Lines[n:]:
                li.End = end

    def _pd_lines(self, f: Frame):
        S = self.S
        if S.pd_enable:
            if S.pd_showH:
                self.Line(f, "pdHigh", self.pdH, self.dayStart, S.pd_high_c, S.pd_high_width, S.pd_style_in, S.pd_offset_val, S.pd_lb_size_in, True, "None", S.pd_txt_c)
            if S.pd_showL:
                self.Line(f, "pdLow", self.pdL, self.dayStart, S.pd_low_c, S.pd_low_width, S.pd_style_in, S.pd_offset_val, S.pd_lb_size_in, True, "None", S.pd_txt_c)
            if S.pd_showOpen:
                self.Line(f, "pdOpen", self.pdO, self.dayStart, S.pd_open_c, S.pd_open_width, S.pd_style_in, S.pd_offset_val, S.pd_lb_size_in, True, "None", S.pd_txt_c)
        if S.cc_enable:
            if S.cc_show_poc:
                self.Line(f, "pdPOC", self.prevDay.Poc, self.dayStart, S.pd_poc_c, S.cc_poc_width, S.cc_poc_style_in, S.cc_lb_offset, S.cc_lb_size_in, True, "None", S.cc_txt_c)
            if S.cc_show_vah:
                self.Line(f, "pdVAH", self.prevDay.Vah, self.dayStart, S.pd_vah_c, S.cc_vah_width, S.cc_poc_style_in, S.cc_lb_offset, S.cc_lb_size_in, True, "None", S.cc_txt_c)
            if S.cc_show_val:
                self.Line(f, "pdVAL", self.prevDay.Val, self.dayStart, S.pd_val_c, S.cc_val_width, S.cc_poc_style_in, S.cc_lb_offset, S.cc_lb_size_in, True, "None", S.cc_txt_c)
        if S.cc_showOpen:
            self.Line(f, "dOpen", self.rthO, self.rthStart, S.cc_c_open, S.cc_open_width, S.cc_open_style, S.cc_open_offset, S.cc_open_lb_size, True, "None", S.opens_txt_c)

    def DayLines(self, f: Frame):
        """The subset of BuildVisuals the chart keeps for past days (IB, prior-day lines, daily open)."""
        self._ib_lines(f)
        self._pd_lines(f)

    def BuildVisuals(self, f: Frame, dev: ProfileStats, zoneAtr, table, ovn, ib):
        S = self.S
        now = f.Bar.Start
        self._ib_lines(f)
        if S.amt_enable and not ovn and minute_of(now) < int(S.amt_del_sess[:2]) * 60 + int(S.amt_del_sess[2:4]):
            self.Line(f, "OVN High", self.ovnH, self.rthStart, S.amt_c_hi, S.amt_width, S.amt_style_in, S.amt_offset, S.amt_lb_size_in, S.amt_showLb, "None")
            self.Line(f, "OVN Low", self.ovnL, self.rthStart, S.amt_c_lo, S.amt_width, S.amt_style_in, S.amt_offset, S.amt_lb_size_in, S.amt_showLb, "None")
            for li in f.Lines:
                if li.Name.startswith("OVN"):
                    li.TextColor = S.amt_c_txt_hi if li.Name.endswith("High") else S.amt_c_txt_lo
        self._pd_lines(f)   # pd lines, prior-day value, daily open: the C# order
        if S.wk_showOpen:
            self.Line(f, "wkOpen", self.weekOpen, self.weekStart, S.wk_c_open, S.wk_open_width, S.wk_open_style, S.wk_open_offset, S.wk_open_lb_size, True, "None", S.opens_txt_c)
        if S.pw_enable:
            if S.pw_showH:
                self.Line(f, "pwHigh", self.pwH, self.weekStart, S.pw_col, S.pw_width, S.pw_style_in, S.pw_offset, S.pw_lb_size_in, True, "None")
            if S.pw_showL:
                self.Line(f, "pwLow", self.pwL, self.weekStart, S.pw_col, S.pw_width, S.pw_style_in, S.pw_offset, S.pw_lb_size_in, True, "None")
            for li in f.Lines:
                if li.Name.startswith("pw"):
                    li.TextColor = S.pw_c_txt
        if S.macro_show_month:
            self.Line(f, "pmVAH", self.prevMonth.Vah, self.monthStart, S.macro_color, S.macro_width, S.macro_style, S.macro_offset, S.macro_text_size, True, "None", S.macro_txt_c)
            self.Line(f, "pmVAL", self.prevMonth.Val, self.monthStart, S.macro_color, S.macro_width, S.macro_style, S.macro_offset, S.macro_text_size, True, "None", S.macro_txt_c)
        if S.macro_show_poc:
            self.Line(f, "pmPOC", self.prevMonth.Poc, self.monthStart, S.macro_poc_color, S.macro_poc_width, S.macro_poc_style, S.macro_offset, S.macro_text_size, True, "None", S.macro_txt_c)
        if S.macro_show_hl:
            self.Line(f, "pmHigh", self.pmH, self.monthStart, S.macro_color, S.macro_width, S.macro_style, S.macro_offset, S.macro_text_size, True, "None", S.macro_txt_c)
            self.Line(f, "pmLow", self.pmL, self.monthStart, S.macro_color, S.macro_width, S.macro_style, S.macro_offset, S.macro_text_size, True, "None", S.macro_txt_c)
        if S.inv_enable and S.inv_show_line:
            self.Line(f, S.inv_txt_trap if self.trap else "SETTLE", self.settle, self.settleStart,
                      S.inv_c_trap if self.trap else S.inv_c_settle, S.inv_width if self.trap else S.inv_settle_width,
                      S.inv_style_in if self.trap else S.inv_settle_style, S.inv_offset, S.inv_lb_size_in, True, "Right", S.inv_txt_c)
        if S.showSpike:
            self.Line(f, "Spike Base (Supp)" if self.bullSpike else "Spike Base (Res)", self.spike, self.spikeStart,
                      S.spike_c_sup if self.bullSpike else S.spike_c_res, S.spike_width, S.spike_style, S.spike_offset, S.spike_lbl_size, True, "None", S.spike_txt_c)
        for n in self.naked:
            self.Line(f, n.Kind + " nPOC", n.Price, n.Start, S.npoc_c_w if n.Kind == "Weekly" else S.npoc_c_m if n.Kind == "Monthly" else S.npoc_c,
                      n.Width, S.npoc_style_in, S.npoc_lb_offset, S.npoc_lb_size, True, "Right", S.npoc_txt_c)
        for p in self.poor:
            self.Line(f, "Poor High" if p.High else "Poor Low", p.Price, p.Start, S.sp_col_poor_h if p.High else S.sp_col_poor_l,
                      S.sp_poor_width, S.sp_poor_style_in, 2, S.sp_poor_lb_size, True, "None", S.sp_poor_txt_c)
        f.Zones.extend(self.single)
        hz = self.LevelZoneHalfWidth(zoneAtr)
        for lv in self.Levels:
            flipped = S.lvl_color_flip and lv.Flipped
            color = S.lvl_ip_color if lv.Kind == 0 else S.lvl_bear_color if ((lv.Kind == 1) ^ flipped) else S.lvl_bull_color
            name = ("FLIPPED " if flipped else "") + lv.Name
            self.Line(f, name, lv.Price, DMIN, color, S.lvl_thickness if S.lvl_showLine else 0, S.lvl_style_in, S.lvl_x_offset, S.lvl_text_size_opt, True, "Both", S.lvl_txt_c)
            if S.lvl_showZones:
                alpha = int(255 * (100 - S.lvl_zoneOpacity) / 100)
                f.Zones.append(ZoneItem(lv.Price + hz, lv.Price - hz, DMIN, (color & 0xFFFFFF) | (alpha << 24)))
                self.Line(f, "", lv.Price + hz, DMIN, color, 1, S.lvl_zone_style_in, 0, "Small", False, "Both")
                self.Line(f, "", lv.Price - hz, DMIN, color, 1, S.lvl_zone_style_in, 0, "Small", False, "Both")
        if S.mvp_master:
            f.Profiles.extend(x for x in self.profiles if f.Bar.Index - x.EndIndex < 4500)
            f.Profiles.append(PastProfile(self.main, dev, self.mainStart, f.Bar.End, f.Bar.Index))
            if S.mvp_show_poc:
                self.Line(f, "Developing POC", dev.Poc, self.mainStart, S.mvp_c_poc, S.mvp_poc_width, S.mvp_poc_style, S.mvp_offset, S.mvp_lb_size, True, "None", S.mvp_txt_c)
            if S.mvp_show_vah:
                self.Line(f, "Developing VAH", dev.Vah, self.mainStart, S.mvp_c_levels, S.mvp_lvl_width, S.mvp_lvl_style, S.mvp_offset, S.mvp_lb_size, True, "None", S.mvp_txt_c)
            if S.mvp_show_val:
                self.Line(f, "Developing VAL", dev.Val, self.mainStart, S.mvp_c_levels, S.mvp_lvl_width, S.mvp_lvl_style, S.mvp_offset, S.mvp_lb_size, True, "None", S.mvp_txt_c)
            if S.mvp_show_walls and len(self.main.Rows) > S.mvp_smooth_len + 1:
                rows = self.main.Rows
                keys = sorted(rows)
                avg = sum(rows.values()) / len(rows)
                mode = S.mvp_tf_mode
                duration = S.mvp_sess_dur / 24 if mode == "Session" else 7 if mode == "Weekly" else 365.25 if mode == "Yearly" else 30.44
                progress = min(1, max(0, (now - self.mainStart).total_seconds() / 86400 / max(duration, .001)))
                mult = S.mvp_scale_start + progress * (S.mvp_scale_end - S.mvp_scale_start) if S.mvp_use_auto_scale else S.mvp_vol_mult
                inside, walls = False, []
                half = S.mvp_smooth_len // 2
                for i in range(len(keys)):
                    js = range(max(0, i - half), min(len(keys) - 1, i + half) + 1)
                    structure = sum(rows[keys[j]] for j in js) / len(js) > avg * mult
                    if structure != inside:
                        p = keys[i if structure else max(0, i - 1)] * self.main.Step
                        if not any(abs(x - p) < S.mvp_min_wall_dist * self.main.Step for x in walls):
                            walls.append(p)
                            alpha = int(255 * (100 - S.mvp_wall_transp) / 100)
                            self.Line(f, "HVN", p, self.mainStart, (S.mvp_c_wall_base & 0xFFFFFF) | (alpha << 24), S.mvp_w_wall, S.mvp_wall_style, 0, "Small", False, "Right" if S.mvp_extend_walls else "None")
                        inside = structure
        loc = self.OpenLocation()
        rng = self.tableH - self.tableL if table else 0
        used = rng / f.DailyAtr * 100 if valid(f.DailyAtr) and f.DailyAtr > 0 else 0
        rule = ("CONFIRMED" if self.rule else "ACTIVE" if f.RuleActive else "SETUP") if table and S.cc_enable and (self.outsideAbove or self.outsideBelow) else "---"
        f.Labels = ["SESSION", "DAY TYPE", "PIVOT", "OVN INVENTORY", "OPEN", f"ATR({S.daily_atr_length})", "ATR USED", "80% RULE"]
        f.Values = ["RTH" if table else "OVN" if ovn else "CLOSED", self.dayType if table else "---",
                    self.pivotBias if table else "---", self.inventoryFrozen if table else self.Inventory(),
                    loc if table else "---", _fmt(f.DailyAtr, 2) if table and valid(f.DailyAtr) else "---",
                    f"{_fmt(rng, 2)} ({_fmt0(used)}%)" if table else "---", rule]
        v = f.Values
        f.Colors = [LIME if table else AQUA if ovn else GRAY, self.dayColor if table else GRAY,
                    GREEN if v[2] == "BULLISH" else RED if v[2] == "BEARISH" else YELLOW if v[2] == "AT PIVOT" else GRAY,
                    self.StatusColor(v[3]), self.StatusColor(v[4]), GRAY,
                    GRAY if not table else ALERT_RED if used >= 100 else ORANGE if used >= 75 else GRAY,
                    PINK if v[7] == "CONFIRMED" else ORANGE if v[7] == "ACTIVE" else YELLOW if v[7] == "SETUP" else GRAY]
        if not valid(f.DailyAtr):
            f.Warning = "Loading daily ATR: load at least 30 trading days."
        if not self.dayFull or not self.weekFull or not self.monthFull or not self.macroFull:
            f.Warning += " First partial profile period is warming up."


def _fmt(x: float, decimals: int) -> str:
    """.NET Framework ToString("0.00") / ("0"): the value is first taken to 15 significant digits, then
    rounded half away from zero (NinjaTrader runs on .NET Framework 4.8)."""
    q = Decimal(f"{x:.15g}").quantize(Decimal(1).scaleb(-decimals), rounding=ROUND_HALF_UP)
    return f"{q:.{decimals}f}".replace("-0", "0") if q == 0 else f"{q:.{decimals}f}"


def _fmt0(x: float) -> str:
    return _fmt(x, 0)


class LevelAlertTracker:
    """Live observations of the engine's watched prices: Cross, Zone Entry or Both."""

    def __init__(self):
        self.points: dict[str, tuple[float, float, float, bool]] = {}

    def Reset(self):
        self.points.clear()

    def Observe(self, levels: list[LevelWatch], close: float, mode: str, root: str, tick: float) -> list[Notice]:
        result, present = [], set()
        for l in levels:
            present.add(l.Name)
            p = self.points.get(l.Name)
            if p is not None and p[3] and l.Enabled and (l.Moving or abs(p[1] - l.Price) < tick * .001):
                pclose, pcenter, pwidth, _ = p
                if mode in ("Cross", "Both"):
                    if pclose <= pcenter and close > l.Price:
                        result.append(Notice(l.Name + "Up", f"{l.Name} Cross Up @ {price_str(l.Price, root, tick)}"))
                    if pclose >= pcenter and close < l.Price:
                        result.append(Notice(l.Name + "Down", f"{l.Name} Cross Down @ {price_str(l.Price, root, tick)}"))
                was_inside = abs(pclose - pcenter) <= pwidth
                inside = abs(close - l.Price) <= l.HalfWidth
                if mode in ("Zone Entry", "Both") and not was_inside and inside:
                    result.append(Notice(l.Name + "Zone", f"{l.Name} Zone Entry @ {price_str(l.Price, root, tick)} (±{price_str(l.HalfWidth, root, tick)})"))
            self.points[l.Name] = (close, l.Price, l.HalfWidth, l.Enabled)
        for k in [k for k in self.points if k not in present]:
            del self.points[k]
        return result
