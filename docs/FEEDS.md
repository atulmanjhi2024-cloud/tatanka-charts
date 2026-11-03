# Data feeds: brokers, data vendors and your own

Tatanka Charts can take futures trades, watchlist quotes and data series from several sources at once, each set up in
**Connections**. A source is a *feed*. Some come built in; anyone can add one as a small Python file (a *feed
plugin*), or by pointing a program of their own at the app.

## How feeds fit together

- **Futures feed**: the trades every chart, profile, footprint and study is built from, for the instruments in
  Connections > App > Instruments.
  - **NinjaTrader** (the add-on) is the main one: it supplies every instrument no other feed supplies.
  - A **feed plugin** supplies the instruments you list in its **Futures** field (e.g. `RTY, YM`).
  - **Each instrument comes from one feed only**, so no trade is counted twice. Connections refuses a second feed
    for an instrument already taken.
  - The **simulator** (`CHART_FEED=sim`) runs alone: real and invented trades never mix, so plugin futures feeds
    stay off while it runs.
- **Quotes**: the watchlists' prices. Several run at once. For each symbol the best source wins (NinjaTrader, then
  IBKR, Alpaca, Coinbase and plugins, then Yahoo); a lower one fills in while a better one is silent. Of two alike,
  the one quoting a symbol keeps it until it is silent; IBKR's delayed data comes after the real-time ones.
- **Data**: series of values by date (FRED, EIA, and plugins: your own CSV) that chart as a line and show in the
  watchlists with their latest value. A plugin's series is `DATA:<symbol>` (e.g. `DATA:GOLDOIL`).
- More than one of a kind: the **+** on a list adds a second copy of a plugin (two accounts, two CSV series), each
  with its own name and fields.
- **Gap fill**: when a futures feed connects, each of its instruments whose stored data stops short of now asks that
  feed for the missing stretch (if it can give history), then for the holes inside its stored data of the last 7
  days (since its last contract roll): `history()` is asked for stretches that ended days ago, not only up to now.
  One of its instruments put back on Connections > Instruments while it runs asks it for the stretch it was off
  (a contract roll it reported meanwhile is recorded then: only the new contract's sessions are filled). One handed
  back to NinjaTrader (the feed turned off, or the instrument taken out of its Futures) has the add-on asked for its
  stretch at once; a hole one feed had nothing for is asked again of the feed that supplies the instrument next.

## Using a built-in feed

In Connections, open **Futures feed**, press **+**, pick the feed. Then:

1. Fill its fields.
2. Put the instruments it should supply in **Futures**.
3. Tick **On**.
4. Press **Test**, then **Add**.

Its card then shows its state: green when connected (with the trades so far), yellow while connecting, red with the
reason when it fails.

### Interactive Brokers futures (untested)

Every trade of the chosen futures from IB Gateway or TWS (IBKR's tick-by-tick "Last" stream), on the front month
IBKR picks, followed through rolls. Gaps fill from IBKR's 1-minute bars.

1. IB Gateway or TWS running, with the API on: Configure > API > Settings, then **Enable ActiveX and Socket
   Clients** and **Read-Only API**.
2. **Host**:
   - `host.docker.internal` from Docker
   - `127.0.0.1` otherwise

   If TWS asks "Accept incoming connection attempt?" at every connect, it doesn't trust the app's address. Docker
   Desktop on Windows connects from 127.0.0.1: tick **Allow connections from localhost only** in Configure > API >
   Settings (or add 127.0.0.1 under **Trusted IPs**). Elsewhere, accept the prompt once, see the address with
   `Get-NetTCPConnection -LocalPort 7496 -State Established | Select-Object RemoteAddress` (PowerShell; 7496 is
   TWS's port, Gateway's 4001) or `ss -tn sport = :7496` (Linux), and add it under **Trusted IPs** (Create), with
   localhost only unticked. TWS keeps it across its daily restart.
3. **Port**: Gateway 4001 (paper 4002), TWS 7496 (paper 7497).
4. **Client ID**: any number no other API program uses. The IBKR quotes connection uses another one: 31 by default.
5. **Data**: IBKR's live CME data subscription for those futures. Delayed data has no tick-by-tick.
6. **Limits**: IBKR allows a few tick-by-tick streams at once, 3 on most accounts. Choose up to that many futures.

Contracts are named by their contract month, as NinjaTrader names them: `CL 12-26` for December crude. Its first
version named them by the month of their last trading day: CL, NG, RB, HO and MCL a month early (`CL 11-26`), SR3
three months late (`SR3 12-26` for September's). On the first start after the update, a contract stored that way is
renamed once, with no "Contract roll" message, if IBKR is still on that contract. That needs this card on, with the
future in its Futures field, at that start: if it is off then, its first report once turned on counts as a roll. A
roll that happened meanwhile is reported as usual, and so is every later one.

**Untested**: written and tested against a stand-in for IBKR's library, never yet run against a live Gateway.

### Databento (live, untested)

Every CME trade of the chosen futures from Databento's live feed (GLBX.MDP3, trades). **Contract** picks the front
month Databento follows: by volume (the usual), calendar or open interest.

1. A Databento account with live CME data (databento.com; Databento bills it).
2. The `databento` package: **Install databento** on its card (a minute or two).
3. **API key**: databento.com > API Keys. Test checks it.
4. **Fill gaps**: off by default. On, it fetches 1-minute bars for gaps, which Databento bills as historical data.

**Untested**: written against Databento's documented client and tested against a stand-in, never yet run with a
live key.

### Your own program (TCP lines)

Any program, in any language, can be a futures feed. It connects to the app's port 8802 and writes lines, the same
ones the NinjaTrader add-on sends. This is the way in for a broker or platform that has an API but no plugin yet:

- a Rithmic or Tradovate program
- Sierra Chart's DTC
- a Quantower or MotiveWave add-on
- a recorded file played back

```
HELLO|<token>|1                                  first, once (the Token field; empty if none)
T|<symbol>|<epoch ms>|<price>|<size>|<side>      one trade; side 1 buy, -1 sell, 0 unknown
Q|<symbol>|<last>|<previous close>[|<volume>]    a watchlist quote (optional)
C|<symbol>|<contract>                            the contract it now sends, e.g. "ES 03-27" (optional: rolls)
```

The symbol is the chart symbol (`ES`, `RTY`). Times are UTC epoch milliseconds. A Python sender, for example:

```python
import socket, time

with socket.create_connection(("127.0.0.1", 8802)) as s:
    s.sendall(b"HELLO|my-token|1\n")
    for price, size, side in my_broker_trades():          # yours: your broker's API
        line = f"T|RTY|{int(time.time() * 1000)}|{price}|{size}|{side}\n"
        s.sendall(line.encode())
```

Port 8802 is published in `docker-compose.yml`, on the PC only (127.0.0.1). Several programs may connect at once.

### Quotes: Finnhub (stocks, real time, untested)

Real-time quotes for the watchlists' US stocks and ETFs from Finnhub's websocket, with the change against the previous
close. A free key at finnhub.io (Dashboard); the free plan streams up to 50 symbols. Quotes > **+** > Finnhub, paste
the key, **Test**, tick **On**, **Add**. **Symbols** limits it to some (empty: every US stock and ETF in the lists).
**Untested**: written and tested against a stand-in for Finnhub, never yet run with a real key.

### Quotes: your own program

Quotes > **+** > **Your own program (quotes)**: a program connects to port 8803 (published in
`docker-compose.yml`) and sends `HELLO|<token>|1`, then `Q|<symbol>|<last>|<previous close>[|<volume>]` lines.
**Symbols** limits which it may quote. A future is `ZS1!` (`ZS` is Zscaler's stock); its root alone, `ZS`, is taken
for the future only while the lists hold the future and not the stock. A futures feed's (port 8802) `Q` lines are
futures: `Q|ZN|...` is `ZN1!`'s quote.

### Data: your own data (CSV link)

A series of your own: a Google Sheet, a spreadsheet export, any CSV with a date and a value on each row. Data >
**+** > **Your own data (CSV link)**:

1. **Symbol**: the name it charts under, e.g. `GOLDOIL` (then `DATA:GOLDOIL`).
2. **CSV link or file**: an `https://` link, or a `.csv` file name in the `feeds/` folder.
   - **Google Sheets:** File > Share > Publish to web > the sheet > Comma-separated values (.csv) > Publish, then copy
     the link.
3. **Name** (optional, shown on the chart) and **Column**: which column holds the value. The date is always the
   first column.
4. **Test** reads it and says how many rows, from when to when. Tick **On**, **Add**.

Then chart `DATA:GOLDOIL` (the symbol search, or type it on the chart) or add it to a watchlist. Dates like
`2026-10-17`, `10/17/2026`, `2026-10`, `20261017`; values like `1,234.5`, `$12.30`, `4.5%`. Header rows are skipped.
It is read again every 3 hours, and at once when its card is saved. One series per entry: **+** again adds another.

## Writing a feed plugin

A plugin is one `.py` file in the `feeds/` folder, next to `docker-compose.yml`:

1. Copy `feeds/my_feed.py.example` to `feeds/my_feed.py`.
2. Fill in `run()`.
3. Save. The server loads it within a few seconds, no restart; each later save loads it again and restarts the
   connections using it.
4. It shows under the **+** of its kind. If it fails to load, a red card in Futures feed says why.

`git pull` never touches that folder. A plugin runs with the server's rights, so only put code there that you
wrote or trust. Nothing can be uploaded through the app.

```python
import asyncio
from chartsrv.feeds.sdk import FeedPlugin, secret, text, number, choice

class MyBroker(FeedPlugin):
    TITLE = "My broker"            # its name in Connections
    KIND = "futures"               # "futures" or "quotes"
    ABOUT = "Trades from My broker's API. Needs an account with API access."
    FIELDS = [secret("api_key", "API key"), text("server", "Server", default="md.example.com")]
    REQUIRES = ["mybroker"]        # pip packages it imports (see Packages)

    async def run(self, market):
        async with mybroker.connect(self.values["server"], self.values["api_key"]) as conn:
            self.connected()
            async for t in conn.trades(self.symbols):
                self.trade(market, t.symbol, t.epoch_ms, t.price, t.size, t.side)
```

### What a plugin gets

| | |
|---|---|
| `self.values` | the fields as typed in Connections, by key (secrets too; `""` when not set) |
| `self.symbols` | futures: the instruments it supplies (its Futures field); quotes: its Symbols field |
| `self.name` | its connection id (`my_feed`, `my_feed-2` for a second copy) |
| `market.instruments` | the chart instruments: `.tick_size`, `.point_value`, `.rth` |
| `market.watch_keys` | every symbol in the watchlists (quotes); a future as its front month, `ZS1!` (`ZS` is the stock) |

### What it calls

| | |
|---|---|
| `self.trade(market, symbol, ms, price, size, side=0)` | one trade. Only its own instruments; rounded to the tick; a bad value is counted and dropped |
| `self.quote(market, symbol, last, prev_close, volume, at)` | one watchlist quote, `symbol` as `wanted()` gives it (a futures feed's root, `ES`, is its future's) |
| `self.contract(market, symbol, "ES 03-27")` | the contract it now sends; a change is a roll, sent to the alert channels. Optional `was=`: the name an earlier version of the plugin gave this same contract. A stored one is renamed without a roll, but only at the first report after the update that changed the naming (so far only the IBKR futures plugin's) |
| `self.connected()` / `self.connected(False, "why")` | its state on the card |
| `self.waiting("Market closed")` | not connected, not an error |
| `self.wanted(market)` | quotes: the watchlist symbols it may quote; a root in its Symbols field (`ZS`) covers the future (`ZS1!`) too |
| `await stopping.pause(s)` | (`from chartsrv import stopping`) wait between polls instead of `asyncio.sleep`: an httpx request can swallow the stop that comes as it connects, and this ends the loop then, so a restart doesn't wait on it |

### What it may define

| | |
|---|---|
| `async run(market)` | required. Connect, then send until cancelled. Raise when the connection drops (a return counts as one): it runs again 10 s later, the error on its card |
| `async history(symbol, start_ms, end_ms)` | futures, optional: an async generator of `("trade", ms, price, size)` or `("bar", minute_ms, o, h, l, c, volume)` for a gap, oldest first |
| `fills()` | futures, optional: whether `history()` asks for anything (default: when it is defined). False while a gap fill that is a choice is turned off (Databento's Fill gaps): the holes inside the stored data are then left for a feed that asks, not taken for answered |
| `@classmethod async test(values)` | optional: Connections > Test, with the fields as typed; returns `(ok, message)` |
| `watch_changed(market)` | quotes, optional: the watchlists changed |
| `ID`, `QUOTE_RANK` | optional: its connection id (default: the file name); a quote feed's rank (default 2, beside IBKR) |

A **data** plugin (`KIND = "data"`) runs nothing; the app asks it when a chart or a watchlist needs a series:

| | |
|---|---|
| `async series(http, code)` | required: `([("YYYY-MM-DD", value), ...], decimals)` for `DATA:<code>`, oldest first; `ValueError` for a code it doesn't have. Kept and asked again after 3 hours |
| `serves(code)` | optional: whether `DATA:<code>` is its (default: any) |
| `title(code)`, `describe()` | optional: the series' name on the chart; a word for its card |

Any **quotes** or **data** plugin may also list its symbols for the search box:

| | |
|---|---|
| `async listing(http)` | optional: `[(symbol, name, type, exchange), ...]`, type one of stock, etf, fund, index, forex, crypto, economic, data. Asked daily and when the plugin is saved; its symbols leave the search when it is turned off |

### Rules

1. **Read-only.** A feed reads market data; it never places an order.
2. **Never block.** Use async libraries, or `await asyncio.to_thread(...)` around a blocking call. A blocking call
   freezes every chart.
3. **Times are UTC epoch milliseconds; prices in the instrument's own units.**
4. **Keys stay in `self.values`.** The app takes a feed's secrets out of the errors it shows and logs (in a URL too),
   but never put one in a log line or a message on purpose.
5. **CME Globex hours.** Instruments run on the 17:00-16:00 CT session (Connections > App > Instruments).

### Packages

A plugin that imports a package the app doesn't have lists it in `REQUIRES`: the pip name, with a version or the
import name when they differ (`"databento"`, `"mybroker>=2"`, `"python-dateutil:dateutil"`). Until it is installed
its card says which one is missing, with an **Install** button: pip installs it on the server (in Docker into the data
volume, `/data/pylib`, so it stays through rebuilds and updates) and the connection starts by itself. A plugin that
imports it at the top of the file shows in **Feed plugins that did not load**, with the same button. Only names a
plugin in the server's folders lists can be installed.

Or, built into the image: `CHART_EXTRA_PIP=databento mybroker` in `.env`, then `docker compose up -d --build`.

## Brokers and vendors: what is possible

| Source | How | Notes |
|---|---|---|
| NinjaTrader (any of its connections: Rithmic, Tradovate, CQG, Kinetick) | built in, the add-on | the usual way for prop firm accounts |
| Interactive Brokers | built-in plugin (untested) | live CME data subscription; 3 tick-by-tick streams on most accounts |
| Databento | built-in plugin (untested) | paid by use; live CME licence |
| Tradovate | a plugin (not written yet) or your own program | API access is a paid add-on, and CME data over the API is billed separately; prop accounts often exclude it |
| Rithmic | your own program, or through NinjaTrader | its API (R\|Protocol) needs Rithmic's approval and a conformance test |
| CQG, Sierra Chart (DTC), Quantower, MotiveWave | your own program | send the TCP lines above |

Each person runs their own copy with their own accounts and data. Nothing is redistributed.
