"""Your own program as a futures feed, in any language: it connects to this port and writes one line per trade, the
same lines the NinjaTrader add-on sends. docs/FEEDS.md has a complete example.

    HELLO|<token>|1                                  first, once (token: as set here; blank if none)
    T|<symbol>|<epoch ms>|<price>|<size>|<side>      a trade; side 1 buy, -1 sell, 0 unknown
    Q|<symbol>|<last>|<previous close>[|<volume>]    a watchlist quote (optional)
    C|<symbol>|<contract>                            the contract it now sends, e.g. "ES 03-27" (optional: rolls)

Symbols are the chart instruments listed in this feed's Futures field. Several programs may connect at once."""
import asyncio
import hmac
import logging

from chartsrv.feeds.sdk import FeedPlugin, number, secret

log = logging.getLogger(__name__)


class LinesFeed(FeedPlugin):
    ID = "lines"
    TITLE = "Your own program (TCP lines)"
    KIND = "futures"
    ABOUT = ("Trades from a program of your own, in any language (a broker's API, Sierra Chart, Rithmic, a recorded file): "
             "it connects to this port and writes one line per trade. docs/FEEDS.md shows how.")
    FIELDS = [number("port", "Port", "the port your program connects to (8802 is published in docker-compose.yml)",
                     default=8802, min=1024, max=65535),
              secret("token", "Token", "your program sends it in its HELLO line; blank: no token")]

    PORT = 8802

    async def run(self, market):
        port, host = int(self.values.get("port") or self.PORT), getattr(market.s, "nt_host", "127.0.0.1")
        self.clients, self.writers = 0, set()
        server = await asyncio.start_server(lambda r, w: self.client(market, r, w), host, port)
        self.waiting(f"Listening on port {port}: start your program")
        try:
            async with server:
                await server.serve_forever()
        finally:   # turned off or saved again: the programs connected to this one go too
            self.close()

    def close(self):
        """Stopped from Connections, or the server stops: its programs are hung up first, as Python 3.12's listener
        waits for them to go before it stops (3.13's hangs them up itself)."""
        for w in list(getattr(self, "writers", ())):
            w.close()

    async def client(self, market, reader, writer):
        peer, accepted = writer.get_extra_info("peername"), False
        self.writers.add(writer)
        try:
            hello = (await asyncio.wait_for(reader.readline(), 10)).decode("utf-8", "replace").strip().split("|")
            token = self.values.get("token") or ""
            if len(hello) < 2 or hello[0] != "HELLO" or not hmac.compare_digest(hello[1].encode(), token.encode()):
                log.warning("%s: refused %s (bad HELLO or token)", self.name, peer)
                return
            self.clients, accepted = self.clients + 1, True
            self.connected()
            while line := await asyncio.wait_for(reader.readline(), 300):
                self.line(market, line.decode("utf-8", "replace").rstrip("\r\n").split("|"))
        except (asyncio.TimeoutError, ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            if accepted:
                self.clients -= 1
                if not self.clients:
                    self.waiting(f"Your program disconnected; listening on port {self.values.get('port') or self.PORT}")
            self.writers.discard(writer)
            writer.close()

    def line(self, market, p: list[str]):
        if p[0] == "T" and len(p) == 6:
            try:
                self.trade(market, p[1], int(p[2]), float(p[3]), float(p[4]), int(p[5]))
            except ValueError:
                self.stats["dropped"] += 1
        elif p[0] == "Q" and len(p) in (4, 5):
            try:
                self.quote(market, p[1], float(p[2]), float(p[3]) if p[3] else None, float(p[4]) if len(p) == 5 and p[4] else None)
            except ValueError:
                self.stats["dropped"] += 1
        elif p[0] == "C" and len(p) == 3:
            self.contract(market, p[1], p[2])
        elif p[0] != "H":   # H: a heartbeat, nothing to do
            self.stats["dropped"] += 1

    @classmethod
    async def test(cls, values: dict) -> tuple[bool, str]:
        port = int(values.get("port") or cls.PORT)
        return True, f"Ready: turn it on, then connect your program to port {port} and send HELLO|<token>|1"


class LinesQuotes(LinesFeed):
    """The same, for watchlist quotes: Q|<symbol>|<last>|<previous close>[|<volume>] lines on port 8803."""
    ID = "lines_quotes"
    TITLE = "Your own program (quotes)"
    KIND = "quotes"
    ABOUT = ("Watchlist quotes from a program of your own, in any language: it connects to this port and writes "
             "Q|<symbol>|<last>|<previous close> lines. docs/FEEDS.md shows how.")
    PORT = 8803
    FIELDS = [number("port", "Port", "the port your program connects to (8803 is published in docker-compose.yml)",
                     default=8803, min=1024, max=65535),
              secret("token", "Token", "your program sends it in its HELLO line; blank: no token")]
