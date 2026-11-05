# Charts

Chart types, controls, studies, drawings, replay, footprint, big trades and your fills.

## Charts

Timeframes: the dropdown next to the symbol lists seconds, minutes, hours (1-12), days (1D, 2D, 3D), weeks (1W, 2W),
months (1M, 3M, 6M, 12M), ticks (trades per bar), volume (contracts per bar), range (ticks high to low) and Renko
(brick size in ticks). **Custom** makes any size (e.g. 15 seconds, 7 minutes, 2 days, 750 ticks); it is kept in a
Custom group (right-click one, or long-press it on a phone, to remove it). Star a timeframe to pin it to the toolbar; favorites are remembered per device.
Short codes: `15s` `5m` `4h` `1D` `2D` `1W` `3M` `2000t` `5000v` `16r`. Seconds bars are built from stored trades, so
they reach back about as far as the chart holds (up to the previous session; their VWAP still counts from the session's
open). Several days, weeks or months group
whole sessions from a fixed start, so a 2D bar is the same two sessions on every device. A day is one 17:00-16:00 CT session, a week the sessions of one
Monday-Friday trade week, a month those whose trade date falls in it; they are built from stored 1-minute bars
(1D: 400 days back, 1W: 5 years, 1M: 20 years), so they reach as far back as your backfill or import does. All times are CT. Time bars align to the 17:00 CT
Globex open; tick, volume and range bars restart at each session open. Range bars close at the range limit
and the next bar opens one tick beyond; gaps are filled with zero-volume bars.


**Renko** (`8k` = 8-tick bricks), as NinjaTrader's: bricks with no wicks, another one in the trend's direction a
brick past the last close, a reversal two bricks away (a brick past the last one's open); the last bar is the brick
forming. Built from stored trades, so history reaches back as far as the stored ticks (the Databento import).

**ETH / RTH** (the button by the timeframe): all Globex hours, or the instrument's regular hours only (ES/NQ 08:30-15:00,
CL 08:00-13:30, GC 07:20-12:30 CT), with time bars counted from the RTH open, as a NinjaTrader chart on an RTH
trading-hours template. Per chart. The Tatanka TPO Profile, Gap Analysis, HTF EMA and Daily SMA x4 run on the same
regular-hours bars there, as NinjaTrader runs them on that template ([INDICATORS.md](INDICATORS.md)).

**Heikin Ashi** (⚙ → Bars): NinjaTrader's formula on the instrument's tick, drawn from the real bars; the price axis
shows the real last price and every study still uses the real bars.

**Treasury futures in 32nds** (ZT, ZF, ZN, TN, ZB, UB), as CME, NinjaTrader and TradingView quote them: ZB 115'16 =
115 + 16/32, ZN 110'165 = 110 + 16.5/32, ZF 108'162 = 16.25/32, ZT 104'057 = 5.75/32 (the last digit is eighths of a
32nd). The price axis, the labels, the watchlist and alert messages show them that way; a price box (an alert, a
drawing's points, the If/Then block, Level Lines' levels, Paste levels) takes 110'165, 110'16.5 or 110.515625.

**Workspaces and layouts** (**▦** in the toolbar), as TradingView's saved layouts:

- **Layout**: 1 chart, 2 side by side or stacked, 1 large and 2 small (the large one left or right), 3 side by side,
  2 × 2 or 3 × 2. Each chart has its own bar (symbol, timeframe, studies, drawing, replay, alert, settings) and its
  own studies and settings; a new chart starts as a copy of the first. The chart last clicked has a gold edge: the watchlist, the Plan tab and alerts
  from the watchlist act on it. A phone shows the first chart of the layout.
- **Link the charts**: Symbol (a new symbol on one goes to all), Timeframe, Crosshair (on by default: the crosshair
  moves on every chart at the same moment).
- **Workspaces**: the layout, every chart's symbol, timeframe, tabs, studies and settings, the links and the panel.
  Saved on the server, so every device can open them: Save, Save as new, Rename, Delete, and switch by clicking one.
  The open workspace saves itself a couple of seconds after a change (untick "Save changes automatically" to stop
  that). A device opening the app for the first time opens the first workspace; when another device changes the open
  one, ▦ shows a dot and clicking it loads the newer one. Indicator settings, alerts, drawings, watchlists and the
  Plan tab's levels are on the server already, the same everywhere.

**Chart tabs** (the strip under the toolbar), as on a NinjaTrader chart window: each tab is a symbol and a
timeframe. **+** opens a new tab and the symbol search (Esc keeps the copied symbol); change its symbol or timeframe
and the tab follows. As TradingView's tabs:

- **Rename:** double-click it (or F2): Enter keeps the name, Esc cancels, an empty name goes back to symbol and
  timeframe. A named tab shows its name with the symbol and timeframe small beside it.
- **Move:** drag it along the strip (mouse), or Shift+← → on a focused tab, or **Move left / right** in its menu.
- **Menu:** right-click (a long press on a phone): Rename, Duplicate, Move left / right, Close, Close other tabs,
  Close tabs to the right.
- **Close:** **Close** in its menu (right-click, or a long press on a phone), a middle click, or Del on a focused tab.
  No ✕ on the tab, so a tap or click can't close one by mistake. The same goes everywhere: removing or deleting a
  watchlist symbol, a study, an indicator, a template, an alert, a drawing in the object tree, a custom timeframe or a
  signed-in device is in its right-click menu (a long press on a phone: hold still; a scroll doesn't count). The ✕
  left are the ones that only close a panel.

- **Indicators per tab:** each tab has its own studies, indicators, layer order, hidden studies and study settings (the
  AMT Toolkit's included), so a Yearly tab can run the AAVP on 1D while a Monthly tab runs a monthly PVP on 4h.
  Settings… on a tab edits that tab's settings only; nothing reaches another tab or chart unless you press **Apply
  to all charts** in the Indicators dialog. Right-click a tab → untick **Own indicators** to link it to its chart's
  shared set instead (⛓ on the tab): that chart's linked tabs show and edit the same indicators and settings. On a
  layout of several charts, Studies ▾ and the Indicators dialog edit the chart last clicked (the dialog says which).
  A new tab starts as a copy of the one it came from; tabs from before each started with their own copy of what they
  showed.
- **Drawings** are the symbol's: every tab of ES shows the ES lines. To keep one to a tab, tick **This tab only** in
  its settings bar; right-click a tab → **New drawings on this tab only** makes that the default there. The object
  tree lists other tabs' drawings at the bottom (⇄ shares one with every tab).
- **Chart settings per tab:** the ⚙ chart settings, price bars on/off, RTH/ETH, log/%, the footprint's settings and
  the AMT dashboard folded or not (see ⚙ Chart settings below).
- **View per tab**, as TradingView: each tab comes back zoomed and scrolled where you left it, with its price scale
  as it was (Auto, a range you dragged to, or 🔒 locked). Scrolled back in time, it returns to the same bars; at the
  latest bars, it stays on the latest. Also after the app reopens, a reconnect or a history fill (these used to jump
  to the latest bars). Kept on each device (a phone shows fewer bars than a desktop), not in the workspace.
  Replay always starts and ends on the latest bars.

Tabs are remembered per chart and saved in the workspace.

## Chart controls

- **Clock**, as TradingView's: left of % log auto at the end of the time axis, to the second, in the chart's time zone
  (on a phone without the zone, to fit).
- **News countdown**, left of the clock: the next release the Calendar shows (its impact and currency filters) within
  the hour, e.g. "CPI m/m in 12:41", red in its last 2 minutes, "now" for a minute after. Tap it for the Calendar.
  **Countdown on the chart** in the Calendar tab turns it off.
- **Go to date** (**Alt+G**, the calendar button among − + ‹ › ⟲, or the chart's right-click / long-press menu):
  a date and time on the chosen time zone's clock (on D/W/M charts a trade date, as their axis shows them); the chart
  centres there at the zoom you have, with the crosshair on that bar. Only within the bars loaded: a longer timeframe
  goes further back.
- **Compare** (**⊕ Compare** in the header, right of + Alert; ⊕ on a phone; or the chart's right-click / long-press menu > **Compare
  symbol…**): the symbol search opens; pick up to three other symbols (ES, RTY, YM, AAPL, DXY, …) and each becomes a
  colored % line over the chart, as TradingView's Compare. The price scale switches to %, so
  every line starts at 0% at the left edge of the view; the legend shows each one's change to the bar under the
  crosshair, and **×** removes it (the last one gone, the scale goes back to what it was). Live futures update
  live; free-data symbols every 30 s. Each line takes its symbol's close as of each of the chart's bars, so a stock
  over a 24-hour future runs flat while its market is closed instead of adding gaps. Kept per chart tab. Time charts.
- **Data window** (⚙ Chart settings, or the chart's menu): under the legend, the bar under the crosshair (else the
  latest): open, high, low, close, change from the bar before, range, volume and delta.
- **Time zone**: click (tap) the clock. **Local** (this device's zone, the default), **Exchange** (the chart's own
  exchange: Chicago for the futures, New York for US stocks, UTC for crypto, the exchange's zone for other markets),
  UTC or a city. Saved in the workspace, so every device and chart follows it (**Local** is still each device's own). Every time shown follows it: the time axis (the date at that zone's midnight), crosshair, clock,
  **Calendar**, **alerts** (expiration dates, entered and shown; the fired-alert log), tape, journal, bar replay
  (its date box too), drawings (vertical line labels, the object tree), the watchlist's quote times, AVWAP anchor,
  levels and If/Then timestamps, screenshots, and Connections (devices, backups, check report). Session hints
  (17:00 open, RTH hours) are shown converted.
  Stays in exchange time (CT), on purpose: indicator session **settings** (AMT, TPO, Gap, profiles: entered as
  NinjaTrader's), D/W/M bars (labelled by trade date), the "as of" date of daily series, and phone/Discord alert
  texts (written by the server; they carry no clock times).
- **Price tags** on the axis from drawn lines, alert lines and the studies (AAVP/PVP/session profile, AMT) stack
  instead of covering each other: tags that would overlap are spread evenly around their prices, in price order.
- **Price axis** (corner under it, as TradingView's): **%** percent scale (from the first bar in view), **log**,
  **auto** (fits the data on screen; with the last bar on screen and price near the bars' low or high, it adds room
  on that side so price sits about a fifth of the way in, and none once price is back toward the middle; the room
  follows the bars' range, not each tick), **🔒** lock price to bar ratio (zooming keeps the chart's proportions, so a 45°
  move stays 45°). What is on shows blue. % and log are each other's opposite; dragging the price axis turns auto off,
  and a double-click (double-tap) on the axis or **auto** turns it back on; the lock needs a regular scale and turns
  auto off, and unlocking leaves the view where it is. % and log are kept per chart.
- **🔒 locked** (price to bar ratio): drag up or down on the price axis with one finger (or the mouse, or the wheel
  over it) to zoom in or out; drag left or right on the time axis to zoom time. Both keep the proportions, so the chart
  never needs a pinch. A double-tap (double-click) on the price axis unlocks and fits the data again. On a phone the
  time axis also zooms with one finger when not locked.
- **Moving the chart up or down** (TradingView's): with auto on, a drag that goes clearly up or down (mouse or one
  finger) moves the chart and turns auto off; a sideways swipe's wobble doesn't, and neither does a long press (the
  crosshair) or a pinch. With auto off the chart moves freely; the price range stays where you put it while you
  scroll or new bars come. A new symbol or timeframe fits itself again (auto on), so a fixed range from another market
  never leaves the chart empty.
- **Time axis**: hover the chart for − + ‹ › ⟲ (zoom out/in around the latest bar, scroll, reset the view). **»**
  appears when the latest bar is out of view and jumps back to it. Wheel and drag work as before. Zoomed in, bars stop
  at 30 px apart (160 with the footprint on, for its numbers).
- **Price axis menu**: right-click the price axis for Auto, Lock price to bar ratio, Scale price chart only, and
  Regular / Percent / Logarithmic. The **mouse wheel over the price axis** scales the prices (auto goes off; a
  double-click on the axis brings it back).
- **Chart menu**: right-click the chart: alerts and a line at that price, copy the price, reset the view, hide or
  show drawings, Studies…, Chart settings…, Keyboard shortcuts.
- **Keys** (not while typing in a box; **?** lists them all): ← → scroll, ↑ ↓ or + − zoom, Home/End, **Alt+R** reset
  view, **Alt+S** picture, **Alt+A** alert, **/** find an indicator, **Shift+drag** measure. Type a timeframe or a symbol
  anywhere (`5` = 5m, `4h`, `D`, `W`, `M` = month, `2000t`, `NQ`) and press Enter to switch.
- **Legend** (top left), as TradingView's: the symbol and timeframe; no open/high/low/close line, so it stays one
  short line and the AMT table sits close under it; then a row per study on the chart with its values at the crosshair. Hover a row (tap it on a phone) for the **eye** and **⚙** its settings; double-click a row
  for its settings; right-click it (long-press on a phone) for Settings, Hide and **Remove from the chart**. The eye hides a study without removing it: its row stays (dimmed, struck through) with its
  settings, and it draws and computes nothing until the eye shows it again (remembered per chart). **▴** folds the rows away (**▾ N studies** brings them back); folded by default on a phone.
- **Measure**: Shift+drag on the chart, or the ruler in ✎ Draw (drag, or tap two points on a phone): the move in
  points, ticks and dollars per contract, the %, the bars and time between, and the volume. The next click clears it.
- **Bar close countdown** under the last price on time bars (to the bar's end, or the day's close: 16:00 CT, the RTH
  end on an RTH chart, CME's halt or early close on a holiday; 2D, 3D, weekly and monthly bars to the close of their
  last session, a holiday halt's short session being the next bar's).
- **⚙ Chart settings** (per tab: each chart tab keeps its own, so two ES tabs can differ; saved in the workspace;
  also **Chart settings ⚙** at the bottom of Studies ▾). A new or duplicated tab starts with the open tab's; the
  log/% price scale mode is per tab too. The settings:
  - **Bars:** candles, hollow candles, OHLC bars, a close line or Heikin Ashi; up/down/doji colors; volume.
  - **Session:** ETH (all Globex hours) or RTH (regular hours only), the same as the **ETH/RTH** chip in the header.
  - **Look:** themes (NinjaTrader, Black, Navy) set background, grid and text together; or pick each color
    (background, grid, axis text, crosshair); grid off, both, horizontal only or vertical only; axis text size;
    a faint symbol and timeframe watermark.
  - **Price scale:** scale on price only (studies never stretch it); last price line, in the bar's color or one you
    pick, solid, dotted, dashed or long dash, 1-4 px, 10-100% opacity; bar close countdown.
  - **Price bars** (this tab only): off hides the candles or bars, for analysis; studies, drawings, volume and
    the last price stay. Also the eye on the legend's first line, or right-click → Hide price bars. Not copied by
    Apply to all charts (nor RTH/ETH or the scale mode).
  - **Chart:** session-break lines at each 17:00 CT open (zoomed far out, one a week instead; none when even those
    would crowd the chart; also right-click → Hide session breaks); crosshair free or magnet (snaps to
    open/high/low/close); right margin in bars. **Reset chart settings** puts all of it back.
  - **Apply to all charts** (bottom of the menu): these settings on every tab of every chart: each chart of the
    layout, every chart position of the workspace, and every other saved workspace.
  - **◑ Chart templates ▾** (bottom of the menu): **Save these settings as…** keeps the look (bars, colors, grid, text,
    price line, scale, margin) under a name; click a name to apply it; right-click it (long-press) to delete it. Studies are not part of it.
- **📷**: **Save picture** (Alt+S), a PNG of the chart with its legend and the time, the logo and "Powered by Tatanka
  Charts" (Connections > Branding turns that line off); or **Post to Discord…**, the same picture with a caption you
  type, to the channel set in Connections > **Discord: chart posts** (also in the chart's right-click / long-press
  menu, so it works on the phone). **⛶** full screen (desktop).
- **Touch:** a long press shows the crosshair while the finger is down; lifting it ends it, so the next drag always
  moves the chart. A sideways drag that starts on an alert line pans the chart (up or down moves the alert; a tap
  edits it). The app never zooms as a page (a tap in a number box used to zoom an iPhone's page, which then took
  drags on the chart for itself).
- **On a phone**, the AMT table starts folded to its title, a small tab in the chart's top-right corner beside the
  legend (**Tatanka AMT v1.1.0 ▾**); tap it and the rows drop down from that corner (under the legend only when the
  legend is too wide, e.g. the studies list open), tap again to fold. Settings and other
  dialogs fill the screen below the status bar, so their ✕ and buttons can always be reached.
- **On a phone**, as TradingView's app: one page at a time, with a bar at the bottom: **Chart**, **Watchlist**,
  **Alerts**, **Plan** and **More** (Tape, Journal, Calendar). The watchlist's rows are TradingView's: the symbol over
  its name, the price over its change and change %; tap a row for its chart, full screen; long-press for its menu (sort
  is in **⋯**). The app opens on the chart.
- **⇥** folds the watchlist/alerts panel away; a tab opens it again. Drag the panel's edge to make it wider; a
  double-click on the edge puts it back. Kept per device.

## Studies

**Studies ▾** lists every study, one row each:

- **Search indicators** at the top: type part of a name (`vwap`, `profile`, `ema`); **Enter** turns the first one on
  and opens its settings.
- **On this chart** first, then the rest by group (Tatanka, Price and levels, Volume, Indicators). The box turns a
  study on or off; clicking its name opens its settings (the others stay folded).
- Defaults are the NinjaTrader chart set: Tatanka AMT Toolkit (with its dashboard), If/Then Bias Table, Level Lines,
  Gap Analysis, HTF EMA, the price line and your fills. Everything else starts off. **NinjaTrader chart** (Indicator
  templates, bottom of the menu) puts it back to exactly that; **All Tatanka settings…** opens the NinjaTrader-style dialog.
- **Layer order:** under **On this chart**, the list is the drawing order, front first, with a **Price bars** row.
  Drag a row's **⋮⋮** (mouse or finger; or click it and use ↑ ↓) to move a study in front of or behind the others
  and the bars. Two limits from the chart library: behind the bars, line studies (VWAP, EMA, SMA, HTF EMA) always
  draw in front of the shaded ones (profiles, AMT, TPO...); in front of the bars, a shaded study can't sit between
  two line studies. A move that breaks one snaps to where it actually draws, and says so. Drawings and alert lines
  always draw in front of everything. The price line, daily levels, volume and footprint go with the bars.
- Remembered per chart and saved in the workspace.
- **fx Indicator templates ▾** (bottom of the menu): **Save these indicators as…** keeps which studies and indicators
  are on and their layer order under a name; click a name to apply it to any chart (the symbol, timeframe, drawings
  and chart look stay); right-click (long-press) to delete one. **NinjaTrader chart** is built in. The chart's look is a separate **◑ chart
  template** (⚙ Chart). Chart templates saved before the two were split became one of each, with the same name.
- **Presets:** in the Tatanka settings dialog, **Presets ▾** saves that indicator's settings under a name and loads
  them back (NinjaTrader's "Save as template"); Apply or OK uses what was loaded. Only the chart's own: the settings
  charts share (the AMT Toolkit's alert settings and Gap Analysis' alert switches, the same on every chart of the
  symbol; the levels file, the same on every chart) are not saved in one and stay as they are when one loads.
- All three are saved on the server: every device has them.
- **Naming one** (any template or preset, drawings' too): the name box lists the saved ones (**▾** shows them all) and
  narrows as you type; ↑/↓ then Enter or Tab, or a tap, takes one. A name that is saved already turns **Save** into
  **Replace**, so that's the one question.

What each one is:

- **Daily levels**: the symbol's daily levels (the Plan tab's: the levels file or link, else the pasted line;
  [INDICATORS.md](INDICATORS.md)) as plain lines, P, R1... and S1..., captioned with whose they are and their date
  (a micro draws its parent's). The AMT Toolkit draws the same levels with zones, so the plain lines hide while it is
  on. Level alerts (**Create alert…** > Daily levels) watch them.
- **VWAP**: Session (resets at the 17:00 CT open; on an RTH chart too, the night's trades in it) and RTH (ES/NQ
  08:30-15:00, CL 08:00-13:30, GC 07:20-12:30 CT, set in `config/instruments.json`), with 1, 2 and 3 standard
  deviation bands. Computed on the server from every trade; history rebuilt from stored 1-minute bars uses each bar's
  typical price.
- **Anchored VWAP**: Set anchor, then tap a bar. One anchor per symbol: on a longer timeframe it starts at the bar
  that holds it (on an RTH chart, the next day's for one after the RTH close). Computed from the chart's bars
  (typical price).
- **Volume profile (right edge)**: against the price axis, facing left: the visible bars (on an RTH chart, their
  regular hours only), or a composite of the last 5 or 20 sessions. Ranges up to 3 days use every trade; longer ones,
  and minutes that only have backfilled 1-minute bars, spread each bar's volume across its high-low range
  (approximate). The session, auto-anchored and periodic profiles are the Tatanka ones (below).

- **Indicators**, grouped in Studies ▾; tick one, and its row's settings have **+ Add another** (up to 12):
  - **Moving averages**: EMA, SMA, WMA, HMA, VWMA, DEMA, TEMA, SMMA (Wilder's), on the price.
  - **Bands and channels**: Bollinger Bands, Keltner Channels, Donchian Channels, on the price.
  - **Oscillators**, each in a pane below the chart with its levels: RSI (Smooth line, 70/30), MACD (line, signal,
    histogram), Stochastics (%K, %D; 20/80), Stochastic RSI, Williams %R, CCI, Momentum, ROC.
  - **Trend and volatility**: ADX with +DI and -DI (pane), ATR (pane), Parabolic SAR (dots), SuperTrend (green up,
    red down), Ichimoku (conversion, base, lagging line, and the cloud drawn its 26 bars ahead).
  - **Volume**: OBV, MFI (panes), Volume MA (on the volume bars), relative volume at time (this bar's volume against
    the same bar of the last N days, on tick, volume, range and Renko charts the bars opening in the same minute;
    pane, 1 = normal), cumulative delta (below).
  - **Price and levels**: floor pivots (PP, R1-R3, S1-S3) and Camarilla (R1-R4, S1-S4), daily, weekly or monthly from
    the period before's high, low and close (a holiday halt's short session counts toward the next trade date, as on
    the 1D chart: Tuesday's after MLK Day are Friday's), a segment a period with each level's name and price at the
    right; the linear regression channel over the last N bars (line and width x the standard deviation from it).
  - **Other**: COT.

  Change a setting or the color in the menu; right-click its name (long-press) to remove it. The legend shows each
  line's value at the crosshair. Formulas are NinjaTrader 8's (its @Indicators source), so the warm-up bars match
  NinjaTrader rather than TradingView; after that the two agree. NinjaTrader has no SuperTrend, Ichimoku or relative
  volume, so those are TradingView's; SMMA's first bars and the regression channel's band follow the usual
  definition. They run in the browser on the chart's own bars, so they work on every bar type, and the forming bar
  updates on every tick. The server runs the same formulas for indicator alerts, and a test checks the two agree.
- **Cumulative delta**: each bar runs from the total before it to the total after it (aggressive buys minus sells),
  restarting at each 17:00 CT session open, or never. Backfilled 1-minute bars have no buy/sell split, so they add 0.

## Drawings

**✎ Draw** shows the drawing tools down the chart's left edge (along the bottom on a phone): horizontal line,
horizontal ray, trend line, rectangle, fib retracement, arrow, vertical line, parallel channel, trend-based fib
extension, long and short position, price range, date range, text, and **paste levels**. Drawings are saved on the
server per symbol, so the phone shows what you drew on the PC (and the other way round), and they keep their
place on every timeframe. Past the bars a seconds, minute or hour chart of a future holds (a point a week back on a 1m
chart, or one placed right of the last bar), a point goes on in trading hours (CME's, less the daily break, weekends
and holidays), so it sits where the bars will put it, a bar that a holiday halt or early close cuts short taking a
whole bar's room as on the chart (CL's 13:00 1h bar at a 13:30 halt); bars that don't fit the hour evenly (2h, 4h,
45m) and other charts go on by the clock.

- **Draw:** pick a tool, then click (lines, rays, text, positions), click-click / drag (trend, arrow, rectangle,
  fib, ranges), or three clicks (channel: the line, then its width; fib extension: start, end, retracement). On a
  phone, tap the points.
- **Long / short position** (TradingView's): click the entry; it starts 20 bars wide with the stop 5 % of the
  visible price range away and the target at 2R. Drag the entry, the target or the stop (the target and stop also
  set its right edge). It shows the target and stop in points, ticks and dollars for **qty** contracts, the risk to
  reward, and, from the entry bar on, whether the target or the stop was hit first (both in one bar counts as the
  stop) or the open P&L.
- **Price range / date range**: a box with the move in points, ticks, % and dollars, or the bars, time and volume.
- **Edit:** click (tap) a drawing to select it; drag it or its end points. A small bar sets color, width and line
  style; **⚙** or a **double-click** on the drawing opens all its settings (a sheet at the bottom on a phone): text,
  text size, fill and opacity, extensions, midline, fib levels (any list, e.g. `0, 0.5, 0.618, 1, 1.618`), reverse,
  price on the axis, the alert, and the **exact price of each point** (type 6905.25 rather than drag; positions:
  entry, target, stop). **Delete** removes it, **Esc** or a right-click on the chart deselects (and drops a drawing
  not finished or a tool picked; the next right-click opens the menu), **Ctrl+Z** undoes and **Ctrl+Y** (or
  Ctrl+Shift+Z) redoes (also ↶ ↷ in ✎ Draw). **⧉** clones it where it is; **Ctrl+C** then **Ctrl+V** pastes a copy, on any symbol
  and in any chart of a layout. **🔒** locks it: it still selects, but can't be moved by mistake.
- **Object tree** (the ☰ in ✎ Draw, or right-click the chart): every drawing on this symbol, newest first, with its
  color, prices and label. Click one to select it (the chart scrolls to it); its eye hides it, its lock stops it
  moving; right-click it (long-press) to delete it (Ctrl+Z brings it back); **Hide all** hides every drawing (and
  lets go of the one selected); a new drawing, or one clicked here, shows them all again.
- **Keys** (TradingView's): **Alt+H** a horizontal line at the pointer's price, **Alt+J** horizontal ray, **Alt+V**
  vertical line, **Alt+T** trend line, **Alt+F** fib retracement, **Alt+Shift+R** rectangle.
- On a computer the drawing tools are a column beside the chart (they cover none of it); on a phone, a row over its
  bottom.
- **Templates:** the settings panel's **Save this style as…** keeps the tool's style under a name; click a name to
  apply it, right-click (long-press) to delete it. Saved on the server, so every device has them (templates kept in a browser before are
  moved there on its first start). New drawings start with the tool's last style. **Magnet** snaps points to the
  bar's open, high, low or close.
- **Alerts:** lines, rays and trend lines have an **alert** box (cross, above or below). It goes to Discord and the
  phone like any alert, follows the line when you move it (and re-arms), and goes when the drawing does. A trend
  line alerts at the level the line is drawn at that moment on the chart its alert was switched on from. On a
  seconds, minute or hour chart (ETH) of a future it is straight in that chart's bars, so it moves on only while the
  market trades, not over the 16:00-17:00 CT break, a weekend or a CME holiday (the instrument's holiday group), and
  a bar that a holiday halt or early close cuts short counts whole, as on the chart. On a W chart it runs by the
  clock, as its bars do. On an M chart it runs in that chart's bars, however many days their months have, so it is
  the line the chart draws. An alert switched on from an M chart by an older version kept to the clock instead, which
  drifts from the line by up to about a tenth of how far it is extended when its points are a month apart: switch it
  off and on again there (the Alerts tab's "now" shows the level it alerts at). On a **log** price scale it is
  straight in log price, as the scale draws it (a line through 0 or below stays straight in price). Charts whose bars
  are spaced otherwise draw the line at another slope: tick, volume, range and Renko bars, RTH-only charts, bars that
  don't fit the hour evenly (2h, 4h: their last bar of the day is short) and D bars around holidays; an alert
  switched on there keeps to the minute charts' line. Other charts draw the same line a little elsewhere, and the
  other price scale far elsewhere: flipped to it, the line's ⏰ says which scale its alert keeps to (*log scale*,
  *regular scale*). Switch the alert off and on to keep it to the chart in view; the Alerts tab shows its level
  (now …).
- **Paste levels** (drawings you can move; the Level Lines indicator is on the Plan tab): `7845.75|VAH, 7830|POC, 7810|VAL`, separated by commas, semicolons
  or new lines, label optional; plus an optional range box (top/bottom) with a midline. Pasting again replaces the
  last pasted set.

## Bar replay

**⏪ Replay**, then tap the bar to start from, or pick a date and time (CT) and press **Go**. Everything after that
bar disappears; **▶** plays (½ to 10 bars a second), **⏭** or **Shift+→** steps one bar, **Space** plays or pauses,
**Jump** picks a new start, **✕** goes back to live.

- Works on every bar type. Changing timeframe or symbol keeps the replay clock: 5m at 10:35 → 1m shows bars up to 10:35.
- Any date with stored data: the server builds the bars around it (about 1,500 back and 1,500 ahead; tick, volume and
  range bars stop sooner at the end of the session) and fetches the next stretch when you play past it.
- What replays: candles, volume, VWAP and bands, anchored VWAP, indicators, footprint, HTF EMA, Daily SMA x4, If/Then, Level Lines, drawings.
  Daily levels, AMT, TPO, gaps and volume profiles switch off during replay because they would show later data, and come
  back on exit.
- Alerts keep running on the live market while you replay.

## Footprint

Studies ▾ → **Footprint**: **Bid × Ask**, **Delta** or **Volume** per price inside each bar, from the stored
trades and their aggressor side (sellers hitting the bid × buyers lifting the ask; a trade without a side takes
the tick rule). Zoom in until bars are about 40px wide to see the numbers; zoomed out it draws plain candles.

- Each row is shaded by its delta (or volume); the bar's POC is boxed in yellow; **diagonal imbalances** (ask
  against the bid one row down, and the reverse, at or above the Imbalance %, 300 by default) print bold green
  or red. The bar's delta and volume are under it.
- Rows are one tick, or grouped automatically so the text fits (or 1, 2, 4, 5, 10 ticks).
- The forming bar refreshes every second while it is in view. Works on every bar type; on an RTH chart only the
  regular hours' trades count.
- Its settings (rows, Imbalance %, POC) are each tab's.
- Needs stored trades: the live feed (the NinjaTrader add-on sends the aggressor side) and the Databento import have
  them; bars from the 1-minute backfill have none (they draw as candles).
- API: `POST /api/footprint` with `{"symbol": "ES", "starts": […], "end": …, "rth": false, "spec": "1D"}` (ms): the bars
  opening at `starts`, the last one up to `end`; at most 400 bars and 8 days. With a D chart's `spec` a bar holds its
  trade dates' sessions, as the chart's bars do: a holiday halt's (Thanksgiving's) is in the next day's bar, stamped at
  that one's open, after those trades. Tick, volume, range and Renko bars can open in one millisecond (one print or
  one sweep finishing several), so for them also send `vols` (each bar's volume) and `pre`, the first bar's `pre` from
  the chart's bars: the contracts in its opening millisecond that went to the bars before it (left out, 0 if the bar
  has none).

## Big trades and time & sales

- **Big trades** (Studies ▾): bubbles where large trades printed, at the bar they printed in (on an RTH chart none
  from the hours it leaves out), area by size, in the chart's up color for buys at the ask and down color for sells
  at the bid. Fills at the same millisecond on the same side count as one trade (an order sweeping the book), as ATAS
  and Bookmap count them. The smallest size is per symbol (ES 25, NQ 10, CL 20, GC 10 to start). From stored trades
  over the visible bars (up to 3 days), then live. During replay only those up to the replay time show.
- **Tape** tab (side panel): time and sales for the symbol on the chart (the active chart in a layout), newest on top,
  grouped the same way, with a smallest-size filter; trades of at least the Big trades size are gold. Scroll down to
  read and new trades come in above without moving the view.
- Both need stored trades: the live feed and the Databento import have them; 1-minute backfill does not.
- API: `GET /api/trades?symbol=ES&start=…&end=…&min=25` (ms; at most 3 days; `newest=true&limit=200` for the latest).

## Your fills and the journal

The NinjaTrader add-on also reads your fills (executions) and positions from every account, read-only: this app
never places orders. Re-copy `ninjatrader/TatankaChartFeed.cs` and press F5 once for this. Fills made while the server
was away arrive on the next connect (each is stored once), and with them the positions as NinjaTrader has them then.

- **On the chart** (Studies ▾ → Your fills, on by default): an arrow just left of the bar it was filled in (on an RTH
  chart none from the hours it leaves out), pointing at the fill price (off the candle, so it shows), in the up color
  for a buy and the down color for a sell, with the quantity; each finished trade a dotted line from its average entry
  to its average exit with its net P&L. Pick one account or all.
- **Journal** tab: Today, 7, 30, 90 days or a year; one account or all; this symbol only or every one. Net P&L, trades,
  win rate, average win and loss, profit factor; a bar per day; then each trade: side, size, contract, entry → exit,
  points, P&L after commissions, MAE and MFE (from the stored 1-minute bars: an open trade's so far, none for one held
  across the chart's contract roll, as those bars are not adjusted), account. Click one to see it on the chart.
- A trade runs from flat to flat on one account and contract: adding to it and scaling out stay one trade, and a
  reversal closes one and opens the next. P&L uses NinjaTrader's point value for the contract. A trade still open
  when the range starts counts its fills back to where NinjaTrader last had that account and contract flat, however
  long ago.
- **A fill the app never got** (a position opened before the add-on was installed, a fill made while NinjaTrader
  was off) is put right by NinjaTrader's own positions, which the add-on sends a second after each change and on
  each connect (add-on 13): where they say otherwise than the fills, the trade open there ends with **exit unseen**
  (no exit price, MAE or MFE; left out of P&L and win rate), and a position held goes on as a trade at NinjaTrader's average price
  (no commission for the fills never seen, no MAE or MFE; hover the row to see why). So it never skews the trades
  after it. Fills from an add-on before 13 (no positions) count back to where the fills were flat, up to a week; one
  open longer is read from a week back.
- API: `GET /api/fills?start=…&end=…&symbol=ES&account=…`, `GET /api/journal?start=…&end=…` (trades, stats and
  `open_since`, the oldest open trade's entry; `excursions=false` leaves out MAE and MFE, `flat=true` reads every
  position as flat at `start`). A trade's `from_position` is true when it began at one of NinjaTrader's positions,
  `unseen_exit` when a fill the app never got ended it. After a new fill for its symbol the chart asks only from where
  every position was flat before it.
