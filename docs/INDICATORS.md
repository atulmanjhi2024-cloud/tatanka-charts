# Tatanka indicators

The NinjaTrader indicators, ported: their settings, the Plan tab and levels.

## Tatanka indicators: settings, Plan tab, levels

The Tatanka NinjaTrader indicators: AMT Toolkit, If/Then Bias Table, Level Lines, Gap Analysis and HTF EMA are on by
default; TPO Profile is there to turn on. Each is a row in Studies ▾.

- **Settings…** (Studies ▾, or the ⚙ on the Plan tab) opens the Indicators dialog, laid out like NinjaTrader's: the
  indicators on the left (the box shows or hides one), every property of the selected one on the right in its
  NinjaTrader groups, labels, choices, limits and descriptions, starting at NinjaTrader's defaults. Each group
  collapses (Expand all / Collapse all; the app remembers which are open), **Find a setting** searches, changed
  settings are marked, ↺ puts one back to NinjaTrader's default. Nothing changes until **Apply** or **OK**. Every value
  is checked as NinjaTrader checks it.
- **Each chart's own:** Apply and OK change the chart's open tab only (its chart's shared set for a linked tab; on a
  layout, the chart last clicked), on every device that opens the workspace. **Apply to all charts** puts the
  selected indicator's settings, as shown, on every tab of every chart, layout and workspace: they become the
  starting settings (what a new chart gets) and each chart's own for that indicator goes. The other indicators keep
  theirs. The AMT Toolkit's alert settings and Gap Analysis' alert switches are the exception: they go to the charts
  of the chart's symbol only (below).
- A few settings belong to one chart in NinjaTrader, so here they are per symbol (marked with the symbol in the
  dialog): the AMT Toolkit's Paste Levels Here, the If/Then block and instrument label, Level Lines' range and
  custom levels.
- First start: the recommended values that differ from NinjaTrader's defaults are seeded once from
  `config/nt_settings.json` (AMT level colors and offset, Gap table bottom right, If/Then wrap 25). Change them
  there before the first start, or in the dialog any time.
- **Plan tab** (side panel), for the symbol on the chart:
  - **Daily levels**, as TradingView takes them: `2026-01-15, 5000.00, 5020.00, 5040.00, 4980.00` (date, pivot,
    then any prices: above the pivot is resistance, below is support), or the Pivot Level / Resistance Levels /
    Support Levels block. The AMT Toolkit draws them with its zones and labels, the **Daily levels** study (Studies ▾)
    as plain lines, and level alerts watch them. Where the levels come from, in NinjaTrader's order: the **Levels
    File or Link** AMT setting (a file under Documents, OneDrive's included, or anywhere with the server run on
    Windows without Docker; or an http(s) link; `{root}` = the symbol, MES reads ES), else
    `Documents\Tatanka\levels\ES.txt` if it exists, else the pasted line. The card says which one is in use and any
    error. A file is re-read within 5 seconds of a change; a link is asked at once when the setting is set or
    changed, then every 2 minutes while the market is open, and not while it is shut. A bad file or link keeps the
    last good levels; a link that answers 401, 403, 404 or 410 says "The levels link was refused (HTTP n): check
    the link".
  - **If/Then**: the block (below), saved per symbol.
  - **Level Lines**: range top/bottom and custom lines `price|label|color|style|width`, e.g.
    `7845.75|VAH|Magenta|Dash|2, 7810|VAL`.
- API: `GET /api/indicators` (schema with defaults, changes, per-symbol values), `PUT /api/indicators/{key}` (the
  changes), `PUT /api/indicators/{key}/{symbol}` (per-symbol values); `GET/PUT /api/amt/levels/{symbol}` (levels
  source and the pasted line). `server/tools/gen_nt_schema.py` regenerates the schema from the C# sources.
- Drawn as NinjaTrader draws them: NinjaTrader's colors, fonts and dash styles. An AMT Toolkit line set to
  **Dotted** does not show in NinjaTrader (the Toolkit's Direct2D dots have zero length and flat caps), so it does
  not show here either: pick Dashed.

## Level Lines

Studies ▾ → **Level Lines**. A port of the NinjaTrader Level Lines v1.1.0 OnRender: a range (top, bottom, optional
midline and shading) and any number of custom lines, labels on the right above each line with the price, moved
down instead of overlapping. Range and lines are per symbol, on the Plan tab.

## Tatanka AMT Toolkit

Studies ▾ → **Tatanka AMT Toolkit**. A line-for-line Python port of the NinjaTrader AMT Toolkit v1.2.2
from tatankatrading/tatanka-indicators (MPL 2.0; the ported files keep its license header). It runs on the
server, so it draws the same lines the NinjaTrader version does and can alert with no chart open.

- Draws: IB high/low and extensions, overnight high/low, prior day/week/month levels, naked POCs, poor
  highs/lows, single prints, the developing profile in the right margin, past profiles, the 80% rule fill,
  and the dashboard (session, day type, pivot, overnight inventory, open type, ATR, 80% rule).
- The daily levels (Plan tab) feed it as PIVOT/RES/SUPP lines (the Daily levels study's plain lines are hidden
  while it is on). Levels more than one trading day old show a STALE LEVELS warning, as in the original.
- Engine bars are 1-30 minutes: minute charts use their own size, longer time charts 30, tick/volume/range
  charts 5, made smaller where the instrument's regular hours don't open or close on them (GC's 07:20: 10-minute bars
  on a 30-minute or longer chart, 5 on a 15-minute one; a 20-minute ES chart 10), so the IB and the RTH open start
  where they should. It loads the last 90 days of stored 1-minute bars, so backfill first. The Year profile only
  sees those 90 days.
- **The prior day's value** (pdPOC, pdVAH, pdVAL and the 80% Rule) comes from that day's **trades** at one-tick
  rows, as a tick volume profile and the Session VP do. For the last 7 days, with the NinjaTrader add-on connected,
  the trades are **NinjaTrader's tick history** for the session (asked of the add-on as a gap fill is, not stored), so
  the values are worked out from the same trades as the NinjaTrader AMT Toolkit (the trades
  recorded here can differ from them by a few hundred contracts, a few ticks on a value area edge); a trade stamped on
  the minute counts in the minute it ends, as NinjaTrader's bars. Otherwise, the trades stored here (a minute with none:
  its 1-minute bar's volume spread evenly over its ticks); a recent day worked out that way is asked of NinjaTrader
  again after a restart. Each day is worked out once in the background and kept; until it is, and for days
  over 30 days old, it comes from the day's minutes as in the NinjaTrader original (each minute's volume spread over
  its range, in rows of the Profile Row Scale), which on a fast day can put the POC 20 or more points away (NQ, Nov 5:
  26902.50 from minutes, 26880.00 from trades). Its value area is **Value Area %** in *4. Previous Day Value & 80%
  Rule*, a fraction (0.68 by default; 0.70 to compare with a 70% profile). The one in *11. Volume Profile* is the
  developing profile's.
- With *11. Volume Profile → Profile Period* on Session (its Session Time on the same hours, 08:30-15:00 CT), the
  prior day's drawn profile comes from the same trades as its pd lines at one-tick rows (v1.2.1), so its tallest row
  is pdPOC and its shaded value area ends at pdVAH and pdVAL when the two Value Area % match. A Weekly or longer
  profile shades the whole period's value area.
- **The current profile** (Profile Period Session or Weekly, the default), its POC and value area and the Developing
  POC, VAH and VAL lines come from the period's **trades** at the Row Scale's rows (v1.2.2), as a tick volume profile:
  the stored ones from the start of the session or week as the Toolkit loads, then each live trade. A minute whose
  trades don't add up to its 1-minute bar's volume (gap-filled history is bars only) is spread over its range as
  before. Monthly and Yearly profiles and older weeks come from 1-minute bars.
- Every pd line (pdHigh, pdLow, pdOpen, pdPOC, pdVAH, pdVAL) switches to the session just closed at its close
  (15:00 CT; CL's 13:30), and that session's profile is redrawn from its trades at the same moment (v1.2.2; before,
  they waited for the 17:00 open, all weekend after a Friday). Live, the close's trades are worked out in a few
  seconds: the lines and profile come from its minutes until then. From the close to the 17:00 open the new lines
  start at the close and the chart keeps the old ones over the day before it; today's 80% Rule fill, OPEN and day type
  stay measured against the value in force at today's open.
- **Alerts** run on the timeframe and own settings of the chart you switched them on from (as NinjaTrader alerts per
  chart), so they fire at the lines that chart draws. To move them to another chart's, switch them off and on there.
  Alerts switched on before this version run on a 5-minute engine with the symbol's settings until switched again.
- **Settings…** opens every Toolkit setting in the Indicators dialog (above): the same 19 groups, checked the way
  the indicator's own validation does; the Toolkit reloads on Apply. Each chart's own, like the other indicators
  (the server runs one Toolkit per distinct set of settings; charts on the same settings share it), except:
  - the **alert settings** (the Alerts group, every group's Alert switches and the level zones' width, which Zone
    Entry alerts use; marked with the symbol in the dialog), which are the **symbol's**: the same on every chart of
    it, as AMT alerts come from one run per symbol. Turning one off on an NQ chart turns it off on every NQ chart, tab
    and device, and leaves ES's as they are; Apply to all charts on an ES chart gives them to ES's charts only. Each
    instrument has its own (micros too: MES's are not ES's); one added later starts at NinjaTrader's defaults.
  - the **levels file**, which is the same on every chart and symbol (`{root}` makes it each one's file or link);
  - **Paste Levels Here**, which is the symbol's.

  A change to those on any chart, tab or device shows at once on every other showing them (each saves only what it
  changed). Presets (Presets ▾) are the chart's own settings only: loading one leaves those as they are. From a
  version before the alert settings were each symbol's, the first start gives every instrument (and each symbol whose
  AMT alerts are kept off Instruments) the ones every chart had, once, so nothing set is lost. Left out because they
  only mean something inside NinjaTrader: alert sound and the indicator's own Discord webhook (this app has its own
  alert channels). With the session-change warning on (the default), the chart says so when a session field differs
  from its default: all session fields are Central exchange time.
- **The alerts follow their Alert switches only.** The alert run works out every group whose Alert switch is on
  (Alert VWAP Cross computes the RTH VWAP, Alert IB High Cross the IB, Alert nPOC Touch every kind of nPOC...), whatever
  a chart shows: a group's Enable switch (Enable IB, Enable RTH VWAP, Show Late Spike...) is each chart's own and only
  shows or hides the group on that chart. To stop a group's alerts, untick its Alert switch (on a chart of that
  symbol). An Alert switch a chart's tab had turned off in its own settings before they were shared is turned off on
  every chart of that tab's symbol once (a linked tab's: its symbol's), the first time that chart opens (it only ever
  turns alerts off), and a message says so.
- **Each instrument's own hours.** The session fields are written for ES/NQ's regular hours (08:30-15:00 CT), as
  NinjaTrader's defaults; on another instrument they move with its own (config/instruments.json `rth`): a time nearer
  the open moves with the open, one nearer the close with the close, the overnight stays. CL (08:00-13:30): IB
  08:00-09:00, overnight 17:00-08:00, its settle the 13:30 close. GC (07:20-12:30): IB 07:20-08:20. ES and NQ as
  written. The same for TPO's Session Time, Gap Analysis' RTH start and end, and Session VP (CT zones).
- **Holiday halts** (MLK Day, Thanksgiving, ...: no settlement that day): the short session counts toward the next
  trade date, as CME's daily bars: Tuesday's prior day and settle after MLK Day are Friday's, and Monday's trading
  is Tuesday's overnight. IB Stop Time is 0830-1500 here (NinjaTrader's default is 0830-1515), and the OVN High/Low
  lines stay from the RTH open to the 17:00 open (Delete OVN Lines At 1700-1705; NinjaTrader's 1500-1505).
- Profile: Day, Week, Month or Year is also a quick switch in the menu.
- Alerts: "Alerts to Discord/phone" turns on the Toolkit's own level alerts for that symbol (on the chart's timeframe
  and own settings, with the symbol's alert settings; its storm cap applies). How levels alert is its Level Alert Mode setting (Cross by
  default). Off by default.
  Turned back on, they start afresh: nothing that happened while they were off is sent.
- API: `GET /api/amt` gives the settings schema, the defaults and the current changes: every chart's (`overrides`)
  and each symbol's alert settings (`symbols`, by symbol; their names in `per_symbol`). `PUT /api/amt/settings`
  takes the changes as a JSON object (same names as the NinjaTrader properties, e.g. `ib_session`), all of them;
  `PATCH /api/amt/settings` takes only those that change (one at its default goes back to it). With `?symbol=NQ` the
  alert settings in either are NQ's; with no symbol, every symbol's. Every open page hears each change
  (`amt_settings`, with every chart's and each symbol's).
  `server/tools/gen_amt_schema.py` regenerates the schema from a new version of the C# source.

Parity check against the original C# (needs mono), from `server/`:
`tests/amt_csharp/build.sh <tatanka-indicators checkout> <workdir>`, then
`python tests/amt_parity.py <workdir> [days] [minutes]`. It replays the same synthetic bars through both
engines and compares every value.

## Tatanka TPO Profile

Studies ▾ → **TPO Profile**. A port of the NinjaTrader TPO Profile v1.0.0 from
tatankatrading/tatanka-indicators (MPL 2.0). Letters for each 30-minute block of the session, coloured from
the open (blue) to the close (pink), drawn one bar apart from the session's first bar as on NinjaTrader; the
last 3 days get letters, the 15 before them a grey outline. POC in yellow, value area optional.

- Minute and hour charts only (it is built from the chart's bars). The server rebuilds those bars from
  stored 1-minute bars, so it draws all 18 days even when the chart holds fewer.
- Session Time is in CT, NinjaTrader's 0830-1500 (Settings…), at each instrument's own hours (CL 0800-1330, as the
  AMT Toolkit above). Untick "Filter by Time" for one profile per
  17:00-16:00 Globex day.
- Row height comes from the daily ATR(10) / 80 rows, as on NinjaTrader; the daily bars are built from your
  stored 1-minute bars, so it needs 11+ days of history (8-tick rows until then).
- On an RTH chart it runs on that chart's bars, as on a NinjaTrader chart with an RTH trading-hours template: the
  regular hours only, counted from the RTH open (a 60-minute chart's first letter is the 08:30-09:30 bar, with no
  pre-market in it), one session per regular session (with "Filter by Time" off too), and the ATR from daily bars
  of the regular hours.
- Like the original it only uses closed bars; "Developing" adds today's profile, updated as each bar closes.
- One deliberate difference: with "Session only" off, the NinjaTrader version re-prices a finished day's rows
  when the daily ATR changes the row height at the 17:00 open, so that day draws at the wrong prices. The
  port keeps the height the rows were built with. On an RTH chart the same happens with it on (a day is saved
  at the next RTH open, after the daily bar).

Parity check against the original C# (needs mono), from `server/`:
`tests/tpo_csharp/build.sh <tatanka-indicators checkout> <workdir>`, then
`python tests/tpo_parity.py <workdir> [days] [minutes] [seed] [ES|NQ|CL|GC] ['{"SessionTime": "0800-1330"}'] [rth]`
(`rth`: an RTH chart's bars; the deliberate difference above is undone for the comparison).

## Tatanka volume profiles: Auto Anchored, Periodic, Session

Studies ▾ → **Auto Anchored Volume Profile** (AAVP 1.1.0), **Periodic Volume Profile** (PVP 1.0.0) and **Session
Volume Profile** (SVP 1.0.0): ports of the three Tatanka NinjaTrader indicators, with their settings (Settings…, the
same on every device), defaults, colors, labels, price markers and on-chart messages.

- AAVP and PVP are built from 1-minute bars, each bar's volume spread evenly over its ticks (up volume when it
  closed at or above its open), exactly as the indicators do it. AAVP is one profile from the anchor (default the
  Year) to now; PVP one per day, week (default), month or year, the last 20 kept. They read the stored 1-minute bars
  as far back as the anchor or the oldest period needs; when the stored bars start later, the notice at the bottom
  says so (backfill to fill it in). An AAVP whose anchor is older than the chart's first bar (a Year on 30 minutes)
  holds the whole stretch but is drawn from that first bar, where NinjaTrader's (which loads only the chart's days)
  starts.
- SVP is built from every stored trade in each custom session (default 08:30-15:00 CT, at each instrument's own
  hours as the AMT Toolkit above; none on a holiday halt; the last 20), split into up
  and down volume by the tick rule. Trades are stored here only while the NinjaTrader add-on is connected (or from a
  Databento import); history filled in from NinjaTrader is 1-minute bars. A minute with no stored trades takes its
  1-minute bar instead (its volume spread over its range, up or down by its close), and a note at the bottom says
  how many sessions have such minutes. For a profile that matches NinjaTrader exactly, keep the add-on connected
  through the session.
- They run on the server, so every chart and device shows the same profiles; the chart asks again every 3 seconds
  and gets only what changed.
- PVP's "until touched" lines end at the first bar after their period that reaches the level. NinjaTrader checks
  the chart's own bars; here it is the first 1-minute bar, which is inside that chart bar, so the line ends on the
  same bar (unless a period ends partway through a chart bar).
- Where a profile starts before the chart's first loaded bar, minute and hour charts place it where those bars would
  be, as if more were loaded (NinjaTrader loads both from the same days, so it never has to).
- As on NinjaTrader: AAVP needs an intraday or 1 Day chart, PVP and SVP an intraday chart (seconds to hours, tick,
  volume, range, Renko); otherwise they say so on the chart. The colors can be None (NinjaTrader's Transparent).

Parity check against the unchanged C# in `server/tests/vp_csharp` (needs mono), from `server/`:
`tests/vp_csharp/build.sh <workdir>`, then `python tests/vp_parity.py <workdir>/harness.exe [seed]`. It compares
every number of 34 cases (every anchor and period, year ends, a bar too wide for 20,000 rows, overnight and odd
session times) as raw double bits.

## Tatanka Gap Analysis

Studies ▾ → **Gap Analysis**. A port of the NinjaTrader Gap Analysis v1.0.0 from
tatankatrading/tatanka-indicators (MPL 2.0). A gap is the RTH open against the last RTH bar's close the day
before (the chart's close, not the exchange settlement). Unfilled gaps extend right and shrink as price
trades into them; the table shows the latest gap, its fill, bars elapsed and the consecutive-gap count.

- Minute and hour charts. The server runs the engine over the last 90 days of the chart's bars (rebuilt from
  stored 1-minute bars, so old unfilled gaps show even when the chart holds fewer bars), up to the bar
  forming now; the chart then fills gaps from every live tick, as the indicator does on each price change.
  On an RTH chart those are its regular-hours bars from the RTH open, as on an RTH trading-hours template: the gap
  opens at the 08:30 bar's open, not at a pre-market price, and its bar counts are the chart's.
- RTH start and end are NinjaTrader's HHmmss settings, Chicago time (83000 and 150000), at each instrument's own
  hours (CL 80000 and 133000, as the AMT Toolkit above). Times that an instrument's hours move to the same minute
  or past each other (110000-120000 is 10:30-10:30 on CL and 09:50-09:30 on GC, a session through the night) are
  refused on that instrument's chart and for its alerts, naming it (fine on ES, they are ES's: CL's alerts keep
  theirs); a change for every symbol (the API with no symbol, below) is checked on every instrument's hours, and on
  those of one taken off Instruments whose gap alerts are kept on (its saved or catalog hours). Alerts stored with
  such times from before stay off, the Studies panel saying why. History (days scanned) is in the Studies menu.
- Alerts: "Alerts to Discord/phone" turns on new-gap and full-fill alerts for that symbol, with its current
  RTH and minimum size. They run on the server from a 1-minute engine, so "filled in N bars" counts minutes.
  Turned on from an RTH chart they watch only the regular hours' minutes, so a gap fills when it does on that chart
  (the Studies panel then says "Alerts: ... (RTH)"); from an ETH chart, all hours. One setting per symbol: the hours
  of the chart that last turned them on. Off by default; history never alerts, only live data (and none while the
  stretch up to the live trades is still being filled: see [Alerts](ALERTS.md#alerts)).
- **The alert settings are each symbol's**, as the AMT Toolkit's. The alert switches (Alert on new gap, Alert on
  full gap fill; marked with the symbol in the dialog) are the same on every chart of the symbol: the dialog shows
  the server's for the chart's symbol, and unticking one on an NQ chart turns it off on every NQ chart, tab and
  device at once and leaves ES's as they are. Saving Gap Analysis' settings on a chart gives its RTH and minimum size
  to that symbol's alerts only (a minimum is in the instrument's points, and the hours are each product's), which
  stay on or off at their own hours, whichever chart or device last switched them. Apply to all charts on an ES
  chart gives the alert switches to ES's charts only, and only ES's alerts take its RTH and minimum size (every chart
  starts from them as usual). Each instrument has its own (micros too: MES's are not ES's); one added later starts
  with both on. A preset leaves the switches as they are, and one a chart's tab had turned off in its own settings
  before they were shared is turned off on every chart of that tab's symbol once (as above).
- From a version where the switches were the same on every chart and symbol, the first start gives every instrument
  (and each symbol whose gap alerts are kept off Instruments) the ones it had, once, so nothing set is lost; each
  symbol's alerts keep their own RTH and minimum size. From one older still, the server works the switches out first
  from each symbol's alerts as saved (off where a symbol whose alerts are on had it off; with no alerts saved, as the
  starting settings have it), and the next page to open says which it turned off.
- API: `GET /api/gaps/alerts` gives each instrument's gap alerts (`alerts`: on, rth, RTH times, minimum size and
  switches) and its alert switches (`switches`, by symbol); `?symbol=NQ` gives NQ's only. `PUT
  /api/gaps/alerts?symbol=NQ` takes a chart's Gap settings for NQ (`new`, `fill`, `RthStart` with `RthEnd`,
  `MinimumGapPoints`; what is left out stays as it is); with no symbol, every symbol's, as before. `PUT
  /api/gaps/alerts/NQ` switches NQ's alerts on or off. Every open page hears each change (`gap_alerts`, with each
  symbol's).

Parity check against the original C# (needs mono), from `server/`:
`tests/gap_csharp/build.sh <tatanka-indicators checkout> <workdir>`, then
`python tests/gap_parity.py <workdir> [days] [minutes] [seed] [ES|NQ|CL|GC] ['{"RthStart": 80000, "RthEnd": 133000}']
[rth]` (`rth`: an RTH chart's bars).

## Tatanka HTF EMA

Studies ▾ → **HTF EMA**. A port of the NinjaTrader HTF EMA v1.0.0 from
tatankatrading/tatanka-indicators (MPL 2.0): a 21-period EMA of a higher timeframe (1 hour by default) on any
chart, including tick and range charts, updated on every price change. Three modes, as in the original:

- **Smooth** (default): an EMA of this chart's own bars with the length scaled to the higher timeframe
  (21 x 60/5 = 252 on a 5-minute chart, 1260 on a 1-minute chart, 5040 on a 15-second chart; tick and range charts
  use 21).
- **Exact**: the EMA of the last closed higher-timeframe bar, a step line.
- **Live**: the higher-timeframe EMA with the current price as the forming bar's close.

The line is left out of the price auto-scale: after a big move it can sit hundreds of points from price, and fitting
it squashed the candles flat. Far from price, it goes off screen.

The server computes the higher-timeframe EMA and, for seconds, minute and hour charts, the chart EMA before the
chart's first bar, from enough stored history (8 EMA lengths) that the line starts settled; the
browser runs the indicator's OnBarUpdate on the chart's bars. Source timeframe (Minute 1-1440 or Day; Day bars are
the 1D chart's, a holiday halt's short session in the next day's), length,
mode and the plot's color, dash style and width are in Settings…; the legend shows its value. On tick, volume and range charts a bar's close time is taken as the next
bar's open, so an hour that ends in the gap between two bars can land one bar later than on NinjaTrader.
On an RTH chart the higher timeframe follows the chart's hours, as NinjaTrader's added series follows an RTH
trading-hours template: hour bars 08:30-09:30 ... 14:30-15:00 (7 a day, not 23), Day bars of the regular hours, and
the Smooth line's start from the RTH bars before the chart's first.

A seconds chart's length is far more bars than the chart holds (75600 on a 1-second chart), so the server rebuilds
its last 4 lengths of bars from the stored trades, at most 4 million trades a request. Further back, and where only
1-minute bars are stored (bars imported, or a gap filled from a provider without tick history), each minute counts as
the seconds bars that open in it, all at its close and no more than its trades. With the trades stored the line is
NinjaTrader's to well under a tick. From 1-minute bars alone it is close on 15- and 30-second charts but can be a few
points off on 1- and 5-second ones: NinjaTrader's seconds bars exist only where a trade printed, and a minute bar
doesn't say how many seconds did.

Parity check against the original C# (needs mono and node), from `server/`:
`tests/htfema_csharp/build.sh <tatanka-indicators checkout> <workdir>`, then
`python tests/htfema_parity.py <workdir> [days] [5m|1D|t3] [seed] [ES|NQ|CL|GC] ['{"Mode": "ExactHTF"}'] [rth]`
(`rth`: an RTH chart's bars).

## Tatanka Daily SMA x4

Studies ▾ → **Daily SMA x4** (off until you turn it on). A port of the NinjaTrader Tatanka Daily SMA x4 (kept
unchanged in `server/tests/dsma_csharp/`): four simple moving averages of real daily closes, 8, 21, 50 and 200 days
by default, on any chart: tick, minute, hour, day, week or month.

- The daily bars come from the 1-minute bars stored here, as CME's daily bars are: one per trading day (17:00-16:00
  CT; a session cut short by a holiday halt joins the next day's, as it has no settlement). On an RTH chart, one per
  regular session, a halt's joining the next day's there too: the RTH 1D chart's bars. Like NinjaTrader's
  `AddDataSeries(Day, 1000 bars)`, at most the last 1000.
- A line only starts once a full window is stored (Pine's `ta.sma`): the 200 needs 200 trading days of 1-minute
  history. The Studies panel says when there are too few.
- Each chart bar shows the SMAs as of the last day that closed by that bar's close, as NinjaTrader runs it: today's
  bars show yesterday's values, and the last bar of a day (and a daily bar) its own. The forming bar shows the
  developing values with the live price; a bar that formed live keeps the values it had then, until the chart
  reloads (NinjaTrader's realtime bars do the same).
- Labels "200 SMA - 6897.25" in each line's color, Label Offset bars right of the last bar, only while the last bar
  is on screen; the chart's right margin grows to fit them. Price markers on the axis; the legend shows each value.
- Settings…: the four lengths (1-1000), Line Width, Show Labels, Label Offset, Label Size, and each plot's color and
  dash style (their width is Line Width, as in the original). Futures only, not on free-data charts.

Parity check against the original C# (needs mono and node), from `server/`:
`tests/dsma_csharp/build.sh <workdir>`, then
`python tests/dsma_parity.py <workdir> [days] [5m|30m|4h|t3] [seed] [ES|NQ|CL|GC] [8,21,50,200]`: history plus 90
realtime minutes, every value equal.

## Tatanka If/Then

Studies ▾ → **If/Then Bias Table**. A port of the NinjaTrader If/Then Bias Table v1.0.0 from
tatankatrading/tatanka-indicators (MPL 2.0). Paste an If/Then block on the **Plan tab**, one band per
line, top band first, and press **Save**:

```
BIAS|LO|HI|TEXT
U|7450|9999999|Accepts above 7450 → continuation, next target 7480
P|7405|7420|Pivot ~7412
D|0|7385|Below 7385 → range break
```

`0` = no bottom, `9999999` = no top, a literal `\n` breaks the text, `#` at the start of a line is a comment.
The block is stored on the server per symbol, so the phone shows what you pasted on the PC (open charts update
right away). A line and a label at each band's bottom; the band price is in now is highlighted, on every tick.
On a phone-width chart the labels are one line each (… where cut, at the chart's edge) and the table shows only the
band price is in, so the chart stays readable; the whole block is on the Plan tab. **Tap a label** (or the table) for
the full text, wrapped to fit; tap again for one line. The same switch is **Full text** in Studies > If/Then (this
device).
Color by **Bias** (U/V/D/P/N) or **Position** (above price = resistance, below = support, within 10% of the
previous day's ATR = neutral). The table and ATR zones are off by default, as in the original.

Labels sit at the chart's right edge, so they stay in the same spot when you zoom or scroll (NinjaTrader draws them
75 bars after the last candle, which moves them as the bars get wider or narrower). The chart keeps Label Offset bars
of room right of the last candle while labels are shown (75); lower it in Settings… to take that room back. The previous day's ATR comes from your stored 1-minute bars.

Parity check against the original C# (needs mono and node), from `server/`:
`tests/ifthen_csharp/build.sh <tatanka-indicators checkout> <workdir>`, then `python tests/ifthen_parity.py <workdir>`.
