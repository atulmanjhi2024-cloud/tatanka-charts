# Copyright (c) 2026 Tatanka Trading LLC.
# This Source Code Form is subject to the Mozilla Public License, v. 2.0.
# If a copy of the MPL was not distributed with this file, obtain one at https://mozilla.org/MPL/2.0/.
"""Tatanka If/Then Bias Table v1.0.0, ported from the NinjaTrader 8 source
(tatankatrading/tatanka-indicators, ninjatrader/TatankaIfThenBiasTable.cs).

The rows, the active band, labels, zones and table run in the browser (web/studies.js, the TatankaBiasLogic
and TatankaBiasPainter port) on every tick. The server keeps each symbol's pasted block, so every device
shows the same plan, and supplies the previous day's ATR (TatankaBiasDailyAtr) from stored 1-minute bars."""
import math

VERSION = "1.0.0"
MAX_BLOCK = 20_000


class TatankaBiasDailyAtr:
    """Pine ta.atr: RMA of the true range, seeded with an SMA after `length` days."""

    def __init__(self, length: int):
        self.length = max(1, length)
        self.closes: list[float] = []
        self.sums: list[float] = []
        self.values: list[float] = []
        self.current = -1

    @property
    def Previous(self) -> float:
        return self.values[self.current - 1] if self.current > 0 else math.nan

    @property
    def Current(self) -> float:
        return self.values[self.current] if self.current >= 0 else math.nan

    def Update(self, index: int, high: float, low: float, close: float):
        if index < 0:
            return
        if index > len(self.values):
            raise ValueError("Nonsequential daily ATR data.")
        if index == len(self.values):
            self.values.append(math.nan)
            self.closes.append(0.0)
            self.sums.append(0.0)
        tr = (high - low if index == 0
              else max(high - low, max(abs(high - self.closes[index - 1]), abs(low - self.closes[index - 1]))))
        self.sums[index] = (0 if index == 0 else self.sums[index - 1]) + tr
        # Pine ta.atr = RMA(TR): seed with an SMA only after length observations.
        n = self.length
        self.values[index] = (math.nan if index < n - 1 else self.sums[index] / n if index == n - 1
                              else (1.0 / n) * tr + (1.0 - 1.0 / n) * self.values[index - 1])
        self.closes[index] = close
        self.current = index


def previous_atr(hlc: list[list[float]], length: int, today_has_bars: bool) -> float | None:
    """dailyAtr.Previous as NinjaTrader shows it: the ATR of the day before the latest daily bar. While
    today's session trades, the latest bar is today's (not in `hlc`, which holds completed days); before
    its first trade (a weekend), it is the last completed day."""
    atr = TatankaBiasDailyAtr(length)
    for i, (h, l, c) in enumerate(hlc):
        atr.Update(i, h, l, c)
    v = atr.Current if today_has_bars else atr.Previous
    return None if math.isnan(v) else v
