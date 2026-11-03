import logging

from .sim import SimFeed, SimQuotes

log = logging.getLogger(__name__)
TRADE_FEEDS = ("sim", "ninjatrader", "none")
QUOTE_FEEDS = ("ibkr", "alpaca", "coinbase", "yahoo", "sim", "none")


def quote_feed_names(settings) -> list[str]:
    """The watchlist quote feeds asked for, in order: any of ibkr, alpaca, coinbase, yahoo (e.g. "ibkr,yahoo": IBKR
    first, Yahoo for what it lacks or while it is down; Market.on_quote ranks the sources), or sim, or none."""
    names = [x.strip().lower() for x in (settings.quotes_feed or "").split(",") if x.strip()]
    bad = [n for n in names if n not in QUOTE_FEEDS]
    if bad:
        raise ValueError(f"unknown CHART_QUOTES_FEED {settings.quotes_feed!r}: it takes {', '.join(QUOTE_FEEDS[:-2])}, sim "
                         "or none, comma-separated (e.g. ibkr,yahoo). The trade feed (sim, ninjatrader) is CHART_FEED")
    return [n for n in dict.fromkeys(names) if n != "none"]


def feed_names(settings) -> list[str]:
    """The names of the feeds these settings run (each feed object's .name)."""
    if settings.feed not in TRADE_FEEDS:
        raise ValueError(f"unknown CHART_FEED {settings.feed!r}")
    out = [settings.feed] if settings.feed != "none" else []
    for name in quote_feed_names(settings):
        if name == "sim":
            if settings.feed == "sim":
                out.append("sim-quotes")
            else:   # never mix invented quotes with a real feed
                log.warning("CHART_QUOTES_FEED=sim only runs with CHART_FEED=sim; no sim quotes (CHART_FEED=%s)", settings.feed)
        else:
            out.append(name)
    if getattr(settings, "fred", True):   # FRED and EIA symbols in the watchlists (idle without any)
        out.append("fred")
    return out


def make_feed(name: str, settings, instruments, levels):
    """levels: the daily levels (levels.Levels), where the simulator starts each symbol."""
    if name == "sim":
        return SimFeed(instruments, levels, settings.sim_warmup_min)
    if name == "sim-quotes":
        return SimQuotes()
    if name == "ninjatrader":
        from .ninjatrader import NinjaTraderFeed
        return NinjaTraderFeed(settings.nt_host, settings.nt_port, settings.nt_token)
    if name == "ibkr":
        from .ibkr import IBKRQuotes
        return IBKRQuotes(settings)
    if name == "alpaca":
        from .alpaca import AlpacaQuotes
        return AlpacaQuotes(settings)
    if name == "coinbase":
        from .coinbase import CoinbaseQuotes
        return CoinbaseQuotes()
    if name == "yahoo":
        from .yahoo import YahooQuotes
        return YahooQuotes(settings)
    if name == "fred":
        from .fred import FredQuotes
        return FredQuotes()
    raise ValueError(f"unknown feed {name!r}")


def make_feeds(settings, instruments, levels) -> list:
    """The trade feed and the watchlist quote feeds; quote feeds follow market.watch_keys as the lists change."""
    return [make_feed(n, settings, instruments, levels) for n in feed_names(settings)]
