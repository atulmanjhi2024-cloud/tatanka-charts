from dataclasses import dataclass


@dataclass(slots=True)
class Tick:
    symbol: str
    ts: int          # epoch ms
    price: float
    size: int
    side: int = 0    # +1 buyer aggressor (lifted the ask), -1 seller (hit the bid), 0 unknown


@dataclass(slots=True)
class Bar:
    t: int           # open time, epoch ms
    o: float
    h: float
    l: float
    c: float
    v: int = 0
    bv: int = 0      # aggressive buy volume
    sv: int = 0      # aggressive sell volume
    n: int = 0       # trade count
    seq: int = 0     # position in its series; the x value for tick, range and volume charts
    st: list | None = None   # study values at the bar's last update: [vwap, sd, rth_vwap, rth_sd]
    pre: int = 0     # contracts traded in the millisecond it opened that went to the bars before it (tick, volume,
                     # range and Renko bars: one print or one sweep can finish several), so its footprint skips them

    def add(self, price: float, size: int, side: int) -> None:
        if price > self.h:
            self.h = price
        if price < self.l:
            self.l = price
        self.c = price
        self.v += size
        if side > 0:
            self.bv += size
        elif side < 0:
            self.sv += size
        self.n += 1

    def merge(self, b: "Bar") -> None:
        self.h = max(self.h, b.h)
        self.l = min(self.l, b.l)
        self.c = b.c
        self.v += b.v
        self.bv += b.bv
        self.sv += b.sv
        self.n += b.n

    def to_dict(self) -> dict:
        d = {"t": self.t, "o": self.o, "h": self.h, "l": self.l, "c": self.c,
             "v": self.v, "d": self.bv - self.sv, "seq": self.seq}
        if self.st is not None:
            d["st"] = self.st
        if self.pre:
            d["pre"] = self.pre
        return d
