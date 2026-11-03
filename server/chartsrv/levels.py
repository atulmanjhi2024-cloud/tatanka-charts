"""Each chart symbol's daily levels as one set: what the Daily levels study draws, level alerts watch, /api/levels
and the hello and health payloads carry, and where the simulator starts. They are the AMT Toolkit's daily levels
(daily_levels.py: the levels file or link, else the symbol's pasted line), made again whenever those change
(Market.levels_changed)."""
from dataclasses import dataclass
from datetime import datetime

from .daily_levels import levels_root


@dataclass(frozen=True)
class LevelSet:
    symbol: str                       # whose levels they are (a micro draws its parent's: MES draws ES's)
    pivot: float
    resistance: tuple[float, ...]     # R1 first
    support: tuple[float, ...]        # S1 first
    date: str | None = None           # the trading day they are for, YYYY-MM-DD
    origin: str = ""                  # file, link or paste (daily_levels.Source)
    source: str = ""                  # the file or link they came from (a link without its private part)
    created: str = ""                 # when they were made, ISO 8601, if known (the chart shows it, else the date)
    price: float | None = None        # the market's price then, if known: the simulator starts there

    @property
    def label(self) -> str:
        """The chart's caption for them (the web app adds the time they were made, else their date)."""
        return f"Daily levels {self.symbol}"

    def lines(self) -> list[tuple[str, float]]:
        out = [("P", self.pivot)]
        out += [(f"R{i}", p) for i, p in enumerate(self.resistance, 1)]
        out += [(f"S{i}", p) for i, p in enumerate(self.support, 1)]
        return out

    def to_dict(self) -> dict:
        return {"symbol": self.symbol, "date": self.date, "origin": self.origin, "source": self.source,
                "created": self.created, "label": self.label, "lines": self.lines()}


def level_set(sym: str, levels: list, date: datetime | None, src) -> LevelSet | None:
    """The AMT Toolkit's levels (amt.Level: PIVOT, RES1..., SUPP1...) for a symbol as a set; None without a pivot."""
    pivot = next((x.Price for x in levels if x.Kind == 0), None)
    if pivot is None:
        return None
    return LevelSet(symbol=levels_root(sym), pivot=pivot, resistance=tuple(x.Price for x in levels if x.Kind == -1),
                    support=tuple(x.Price for x in levels if x.Kind == 1), date=date.strftime("%Y-%m-%d") if date else None,
                    origin=src.origin, source=src.where if src.origin in ("file", "link") else "")


class Levels:
    """The sets by chart symbol. `get` is all an alert's tick looks at; `poll` makes them again."""

    def __init__(self, market):
        self.m = market
        self._sets: dict[str, LevelSet] = {}

    def get(self, sym: str) -> LevelSet | None:
        return self._sets.get(sym)

    def make(self, sym: str) -> LevelSet | None:
        """The symbol's set as its daily levels are now."""
        try:
            levels, date, src = self.m.daily.current(sym)
        except ValueError:   # a pasted line the Toolkit rejects: none (the Plan tab says why)
            return None
        return level_set(sym, levels, date, src) if levels else None

    def poll(self) -> list[str]:
        """Makes every symbol's set again: the symbols whose set changed (taken off the list: gone)."""
        changed = []
        for sym in list(self.m.instruments):
            ls = self.make(sym)
            if ls != self._sets.get(sym):
                if ls:
                    self._sets[sym] = ls
                else:
                    self._sets.pop(sym, None)
                changed.append(sym)
        for sym in [s for s in self._sets if s not in self.m.instruments]:
            del self._sets[sym]
            changed.append(sym)
        return changed
