# Watchlists and symbols

Watchlists, symbol search, and the free data sets (FRED, EIA, COT, the economic calendar).

## Watchlist

Works like TradingView's. The first run loads a starter list (config/watchlists.json). After that the lists live in
the database and every open app sees edits at once. Two devices editing at the same moment both keep their
edits: each save of the lists has a version, and a save made on an older one (the other device saved first) is merged
into the newer lists and saved again. Where both changed the same thing (a list's name, a symbol's flag) the later
save's stands; a list deleted on one device and edited on the other stays; in a list both changed, the later save's
order, with the symbols the other added or removed, each in its own section.

- **Click** a symbol to chart it: the live futures (blue: ES, NQ, CL, GC, MES, MNQ) from NinjaTrader, anything else
  from free data (see "Symbol search and free data"). **↑ / ↓** walk the list and switch the chart; **Del** removes;
  letters still open the quick symbol switch.
- **+** searches the symbol database: type a ticker or a name (`apple`, `crude`, `10-year`), filter by type
  (Futures, Stocks, ETFs, Indices, Forex, Crypto, Economy), click a result or press Enter for the highlighted one. It
  goes below the selected symbol. Several at once with commas (`AAPL, ZN1!, EURUSD`) adds them as typed. TradingView
  names work: `ES1!` is ES's front month, `CME_MINI:ES1!` too, and formulas like `AAPL+MSFT`, `SKEW/VIX` or
  `FRED:DGS10-FRED:DGS2` are computed from their parts. A future and the stock whose ticker is its root are two
  symbols, each with its own quote, chart and details: `ZS1!` soybeans and `ZS` Zscaler, `ZM1!` soybean meal and `ZM`
  Zoom, `HE1!` lean hogs and `HE` Hawaiian Electric. A future charted live here owns its root alone, as the chart does: `ES`
  and `CL` are the E-mini and crude while those run. A root no stock has for a ticker (`6E`, `SR3`, `ZB`, `YM`: none in
  the stock listings the symbol search holds) is written `6E1!`, typed or in an older list, and its chart has the
  drawings of both names, live or on free data (those drawn on an older list's bare `6E` show on `6E1!`); a formula
  saved before that would be longer than 120 characters written so keeps its roots as they are.
- **Live or not**, the mark after each symbol: a green dot live (NinjaTrader, IBKR, Alpaca's IEX, Coinbase), **D**
  delayed (IBKR delayed data, or a Yahoo price minutes old: its futures and indexes run 10-15 min behind), a half moon
  for a market that is shut (its last trade over an hour old), **E** end-of-day data (FRED, EIA), **S** simulated.
  Hover it, or select the symbol, for the source and how old the price is.
- **Sections**: click a header to fold it; **⋯** on it to rename, add a symbol there, or remove it. **⋯** at the top
  adds a section.
- **Reorder**: drag a symbol or a whole section (mouse). Phone: **⋯ > Edit list**, then drag by **⋮⋮**. **Remove**: right-click a symbol (long-press on a phone), or Del on the selected one.
- **Sort**: click Last, Chg, Chg% or Vol (again to reverse, a third time for the list's own order). Sorting is
  within each section.
- **Right-click** a symbol: open chart, alert, color flag, add a section above, copy, remove.
- **Flags**: a color marks the symbol in every list. The five dots by the list name are the flagged lists, as on
  TradingView: click one for that color's symbols from all your lists (a quick list of what you're watching today),
  again to go back. Remove there takes the flag off; **Make a copy** (list menu) turns it into a list of its own.
- **Lists**: click the list name to switch, create, rename, copy, delete, **import a TradingView export** (in
  TradingView: the watchlist's **⋯ > Export list**, a .txt) or export one back.
- **Details** (bottom): price, change, previous close (a future's settlement, with when it was set), volume, quote time; today's range with the price on it and the
  open; the 52-week range; performance over 1W, 1M, 3M, YTD and 1Y (Yahoo's daily bars); for a live future its
  contract and how it rolls; for a stock its next earnings; the flag.
  Opening, alerts and removing are a click, a right-click and Del away. Click its header to fold it.
- Prices flash green or red as they tick, at most about once a second each: on a symbol that ticks several times a
  second the flash fades between them (the price and the change follow every tick). Columns: **⋯** shows or hides
  Chg, Chg% and Vol.
- API: `GET /api/watchlists` gives `{lists, flags, names, version}` (each save's version is higher, across restarts
  and restores too); `PUT /api/watchlists` takes the whole document and `base`, the version it was edited on: a save
  since (another device's) makes it a 409, nothing saved: read the lists again and make the edit there. Without
  `base`, last write wins (as before versions). Every open app hears each save, its version with it.

Quotes, best source first (Details shows which one a quote came from):

1. **NinjaTrader** for futures: the add-on quotes the front month of every `XX1!` in your lists, if your NinjaTrader
   data connection carries it (CME, CBOT, NYMEX, COMEX; not Eurex/ICE/HKEX/OSE). Re-copy `TatankaChartFeed.cs`
   and press F5 once for this. Its quotes stop when a future's market data does (a minute with none, the data
   connection lost, Market Replay or the Simulated Data Feed connected; add-on 17): IBKR or Yahoo takes over.
2. **IBKR** (Connections, or `CHART_QUOTES_FEED=ibkr,yahoo`; TWS or IB Gateway running): stocks, ETFs, FX, US indices,
   crypto, futures; also the charts' history (below). A future only as `XX1!`: `BZ1!` is never quoted as Kanzhun's
   stock `BZ`, nor `BZ` as the future. **Alpaca** (free account; Connections): US stocks and ETFs in real time from IEX. **Coinbase** (no
   account; Connections): crypto in real time; its change is from the day's open at 00:00 UTC, as TradingView counts a
   crypto day. When two of these have a symbol, the one quoting it keeps it while it quotes; IBKR's delayed data comes
   after Alpaca's and Coinbase's.
3. **Yahoo Finance**, the backup and the default until IBKR is set up (`CHART_QUOTES_FEED=yahoo`, no account): stocks,
   ETFs, indices (SPX = ^GSPC, DXY = DX-Y.NYB, KOSPI = ^KS11, ...), yields (US10Y = ^TNX), FX (EURUSD=X), crypto
   (BTC-USD) and US futures (ES=F). Its quotes can lag 10-20 minutes, so it only fills a symbol that NinjaTrader or
   IBKR has not quoted in the last 2 minutes. Every 15 s (`CHART_YAHOO_POLL_S`). Its previous close is the close of the
   trading day before the latest price's (worked out from its daily bars). Futures with no Yahoo twin (FDAX, NCF) show
   **—**, FTUK1! the FTSE 100 index and HSI1! the Hang Seng index; `config/yahoo.json` maps any symbol to a Yahoo ticker
   you choose (a future as `FDAX1!`, or by its root, `FDAX`, as before). Yahoo's free endpoints are unofficial and
   can change; `/api/health` shows its last error.

**A stock's, ETF's or index's change is from its previous close**, the quoting source's, for the quote's trading day:
a US stock's runs from midnight New York (its pre-market, session and after-hours are one day), so yesterday's
reference is never carried into today's. IBKR sends no previous close for some tickers (a price and no Chg); such a
quote takes the one another source gave for that day (Yahoo or Alpaca before IBKR took the symbol over), else the
server asks Yahoo's daily bars for it (the close before the quote's day), once per symbol a day, up to 20 symbols a
request, in the background, whether or not Yahoo's quotes are on; the price stays the source's. A currency pair or
crypto (days from the 17:00 New York roll and 00:00 UTC) takes one another source gave that day, and is not asked.
`/api/health` shows `close_fill` (asked, filled, errors).
**A future's change is from its latest settlement**, as its exchange sets it, counted from the market's next open
(CME's 17:00 CT Globex open; Friday's from Sunday's), as TradingView: after the 15:00 settlement ES still shows the
day's change until 17:00, then the new session's. Each market's settlement window is in
`config/settlements.json`, in the exchange's own time (E-mini S&P: the volume-weighted average of the trades from
14:59:30 to 15:00:00 CT, to the tick). A future charted here (its trades stored) gets it worked out from its own trades
the exchange's way a minute after the window (again when a gap fill brings more of them); one only quoted gets the
settlement its exchange published, through NinjaTrader (add-on 9). On a CME holiday: none on a halt or closed day; on
an early-close day (the day after Thanksgiving, Christmas Eve) at the time CME sets (12:00 CT for equity index,
Treasuries, FX and grains; energy and metals as usual the day after Thanksgiving, earlier on Christmas Eve). Without
one, or with one older than the settlement before the latest (no trades stored in the window, no word from
NinjaTrader), the source's previous close. After a contract roll the old contract's no longer counts: the new one's
from NinjaTrader, else the source's previous close, until the new contract's own trades settle a window. A micro takes
its E-mini's while both are on the same contract month; each rolls by its own volume, so between the two rolls the
micro has its own.

| Markets | Settlement window |
|---|---|
| ES, NQ, RTY, YM, EMD, NKD, NIY (micros take their E-mini's) | 14:59:30-15:00:00 CT |
| ZT, ZF, ZN, TN, ZB, UB; 6E, 6J, 6B, 6A, 6C, 6S, 6M, 6N, M6E | 13:59:30-14:00:00 CT |
| SR3 | 13:59-14:00 CT |
| BTC, ETH (MBT, MET take theirs) | 14:59-15:00 CT |
| ZC, ZS, ZW, ZL, ZM | 13:14-13:15 CT |
| LE, HE, GF (counted from their 08:30 CT open) | 12:59:30-13:00:00 CT |
| CL, NG, RB, HO, BZ (MCL and QM take CL's) | 14:28-14:30 ET |
| GC 13:29-13:30, SI 13:24-13:25, PL 13:03-13:05, HG 12:59-13:00, PA 12:58-13:00 (MGC, SIL, MHG take GC's, SI's, HG's) | ET |
| FDAX | 17:29-17:30 Frankfurt |
| FTUK (ICE FTSE 100), NCF (ICE Newcastle coal) | 16:28-16:30, 16:05-16:15 London |

4. **FRED** and **EIA** series (`FRED:DGS10`, `EIA:PET.WCESTUS1.W`): the latest value and its change from the one
   before, with the date it is for. They publish at most daily (EIA weekly); the app asks again every few hours.

## Symbol search and free data

The **symbol button** (top left), or typing on the chart, opens the symbol search; the watchlist's **+** uses the same.

It searches a database the server keeps:

- **Every US-listed stock and ETF**, from Nasdaq Trader's symbol directory, refreshed daily (about 10,000).
- **Futures** roots (`ES1!` … `ZN1!`, `6E1!`, `KC1!`), **indices** (`SPX`, `VIX`, `DXY`, `US10Y`, `DE40`),
  **forex** pairs, **crypto** (built in, plus Coinbase's list daily) and about 90 popular **FRED** series.
- **The feeds you connect** add theirs, daily and at once when the connection is saved: Alpaca's full asset list
  (with its keys, OTC stocks too), Finnhub's US symbol list, your own `DATA:` series, and any feed plugin that lists
  symbols. A feed turned off takes its symbols with it.
- A search that finds little asks **Yahoo Finance** (international stocks, funds, more indices) and keeps what it
  finds, so the database grows as you use it. With a free **FRED API key** (`FRED_API_KEY` in `.env`; get one at
  fred.stlouisfed.org > My Account > API Keys) the search finds all 800,000+ FRED series the same way.

**Connections > Data > Symbol list** shows how many of each type it holds and every source with its count and
when it was last updated; **Update now** asks them all again.

More free data, no account:

- **Economic calendar** (the **Calendar** tab): this week's and next week's releases (CPI, payrolls, FOMC, PMIs,
  claims, …) with impact, forecast, previous and actual, from ForexFactory's weekly feed. Filter by impact and
  currency. **Warn me** sends a warning to your alert channels a few minutes before the events you pick (by default
  high-impact USD). **Studies > Economic events** marks them on the chart as dashed lines.
- **Major earnings** (same tab, blue dots): the next two weeks' reports from Nasdaq's public earnings calendar,
  read every 6 hours. Shown: companies at or over a market cap (**Earnings ≥ $200B** by default; 5 to 10,000) plus
  any tickers you type in the box next to it (e.g. `PLTR HOOD`). Each row: **BMO** (before the open) or **AMC**
  (after the close), ticker, company, EPS estimate, market cap, fiscal quarter. Shown with USD or All. Nasdaq gives
  no exact time, so the chart's line sits at 06:00 CT for BMO and 15:05 CT for AMC (reports at the same time share
  one line). If Nasdaq turns the server away, even for some of the days, the tab says so and keeps the last list.
  The **Earnings** button next to High / Medium / Low shows or hides them (list and chart, per device).
- **Commitments of Traders** (Indicators > **COT**): the CFTC's weekly net positions of commercials, large
  speculators and small traders, in a pane under the chart, for ES, NQ, RTY, YM, CL, NG, GC, SI, HG, the Treasury
  futures, currency futures and grains (none on a stock's chart: `ZS` is Zscaler's, `ZS1!` soybeans'). Each week
  appears at its Friday 3:30 pm ET release, never before.
  `config/cot.json` adds a market (`{"codes": {"ROOT": "<CFTC code>"}}`).
- **EIA** energy data (`EIA:PET.WCESTUS1.W` crude stocks, Cushing, gasoline, distillates, production, refinery runs,
  natural gas storage; search "EIA"): watchlist values and weekly charts. Needs EIA's free key (Connections > EIA).

Each free-data chart runs on its own exchange's clock: US stocks on New York's (regular hours 09:30-16:00 ET),
crypto on UTC around the clock, currencies on the 17:00 New York roll, other markets on their exchange's zone (from
Yahoo). Session breaks fall where that market's day starts (crypto at 00:00 UTC, not 17:00 CT), and **Exchange** in
the clock's time zone menu means that exchange. On a stock chart the **ETH/RTH** chip works as on the futures: ETH adds
the pre- and post-market (4:00-20:00 ET; Alpaca and Yahoo), RTH shows regular hours only.

Charts for symbols with no live feed here come from the best source that has them, and the legend says which. Every
one takes any minute or hour timeframe up to 24h (1m, 3m, 7m, 2h, 4h, …) and N days, weeks or months (2D, 2W, 3M),
built from the source's nearest shorter bars: minutes and hours counted from the regular open (9:30 ET for a US stock
or index, a stock's from 4:00 with ETH), or where the market's day starts (17:00 New York for FX, 00:00 UTC for
crypto, 17:00 CT for futures), as on TradingView; crypto's N days are calendar days, as it trades every day. While a
chart is open its symbol is quoted like a listed one (no need to add it to a list), so the last bar follows the live
price between polls (not the simulator's invented one, which stays in the watchlist); with RTH on, a stock's pre- and
post-market trades stay off its bars.

1. **IBKR** (TWS or IB Gateway connected; Connections > Interactive Brokers): stocks, ETFs, US indices (SPX, VIX,
   NDX, RUT), FX, crypto, and futures not charted live here (as `ZN1!`). Its history and real-time quotes need the
   symbol's market data subscription at IBKR (US stocks and ETFs: the US securities bundle; SPX, VIX and the
   yields US05Y, US10Y, US30Y, from Cboe's FVX, TNX, TYX: Cboe Streaming Market Indexes; Cboe's are ten times the
   yield, TNX 42.5 for 4.25%, so IBKR's quotes and bars of them, and of IRX, are divided by 10: in percent, as
   Yahoo's ^TNX); without one it says nothing and the next source charts it, IBKR asked again 10 minutes later.
   Legend: **IBKR real time** (**IBKR delayed** with `IBKR_MARKET_DATA_TYPE=3`). New bars every 15 s.
2. **Alpaca** (US stocks and ETFs, intraday, its free keys set; Connections > Alpaca): history is the consolidated
   tape (every exchange's trades and volume) up to 15 minutes ago, which the free plan allows; the last 15 minutes
   are IEX's trades, live, so their volume is a small slice. With Alpaca's paid plan (Data: SIP) all of it is the
   consolidated tape. Legend: **real time**. Up to 6.5h bars; longer ones and daily bars come from the next source.
3. **Yahoo Finance**, the backup: stocks, ETFs, indices, forex, crypto and other futures, delayed (15-20 min for
   most). It keeps 1-minute bars 7 days, 2- to 30-minute bars 60 days and hourly bars 2 years, so a 3m chart (from
   1m) reaches back 7 days, a 10m one (from 5m) 60 days, a 2h one (from 1h) 2 years. A US stock's 1h and longer hours
   with ETH come from 30 minutes (its hourly bars with the pre-market don't meet 4:00), so 60 days. New bars every
   30 s (2 min on daily bars). Legend: **delayed**.
- **FRED series** (`FRED:DGS10`, `FRED:CPIAUCSL`, `FRED:UNRATE`, `FRED:T10Y2Y`, `FRED:WALCL`, …): a line, in 1D, 1W
  and 1M, from FRED's public CSV; no key needed. Kept in the database, asked again every few hours.
- Your own indicators (EMA, RSI, MACD, …), drawings and chart settings work on them. The Tatanka studies, VWAP,
  profiles, footprint, big trades, fills and replay need this app's own futures trades, so they stay on the live
  futures charts, and so do alerts.

`CHART_SYMBOLS_ONLINE=0` keeps the server from reaching Nasdaq Trader, Coinbase and Yahoo's search (the built-in
symbols still search). `/api/symbols/status` shows how many symbols of each kind it has and when it last refreshed.
