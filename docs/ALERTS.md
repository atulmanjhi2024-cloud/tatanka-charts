# Alerts and Discord

Price, level, drawing, indicator and news alerts, and where they go: app notifications, Discord, ntfy.

## Alerts

All alerts are evaluated on the server on every tick, so they fire with no chart open. Setting one works as on
TradingView:

- **Quick alert**: right-click the chart (or hover it and click the **+** just left of the price axis) and pick
  **Add alert at …**: price crossing that level, once. `+ Alert` in the header then a tap (Esc or a right-click
  backs out), or Alt+click, does the same. Phone: hold a finger on the chart until the crosshair shows, lift it, then tap the **+** (it stays 3 s; the crosshair
  ends when the finger lifts, so a drag after it always moves the chart).
- **Create alert…** (same menu, **Alt+A**, or **+ Create alert** in the Alerts tab) opens the dialog:
  - **Condition**: Price, Session VWAP, RTH VWAP, POC, VAH, VAL, Daily levels, or an indicator (the **Indicators**
    groups below them).
  - For price: Crossing, Crossing up, Crossing down, Greater than, Less than (fires while price is past it),
    Entering channel, Exiting channel (two prices).
  - For a VWAP: the line or a ±1/2/3σ band. For POC/VAH/VAL: the ETH or RTH profile, developing or previous (this
    app's own 70% session profiles, not the Tatanka Session Volume Profile).
    Studies and daily levels fire on a touch within *Touch within* ticks, then re-arm once price has moved 8 ticks
    beyond that band. The levels are the ones the chart draws, kept running on the server.
  - **Indicator alerts**, as TradingView's: any indicator with a line (EMA, SMA, RSI, MACD, Stochastics, Bollinger
    Bands, CCI, ADX and the rest), with its own settings and the line to watch (MACD or its signal line, a band). It
    runs on its own timeframe, 1m to 1D, whatever the chart shows (on regular hours when the chart is set to RTH).
    It is compared with the price, a value, or another of its lines: an oscillator starts at its overbought level
    (RSI 70), MACD and Stochastics at their signal line, the rest at the price. **Check**: **Bar close** (when each
    bar closes; TradingView's Once Per Bar Close) or **Intrabar** (the first trade that meets it; Once Per Bar). The
    server uses the chart's formulas (NinjaTrader's), so an alert fires on the value the chart shows, with no chart
    open.
  - **Trigger**: Only once (it stops after firing) or Every time (price alerts at most once a minute, indicator
    alerts at most once a bar).
  - **Expiration**: **Open-ended** (the default: it stays armed until you delete it) or **Until…** a date and time
    (the chart's time zone) when it switches itself off. **Message** is added to the notification.
- **On the chart**: each active price alert is a dashed line with a 🔔 label and an axis tag (a channel shows both
  edges, shaded). Drag a line to move the alert (it re-arms); click it to edit.
- **Sound** (bottom of the Alerts tab, this device): a chime here when an alert fires: two notes for price alerts
  and drawn lines, three rising for levels, studies, indicators, AMT and gaps, four for news warnings. Volume slider;
  **Test** plays each in turn. Browsers allow sound only after you have clicked or tapped the page once.
- **The Alerts tab** lists every alert: active ones first, with the condition, trigger, expiry, message and state
  (Active, Paused, Triggered, Expired). Click a row to edit it (saving restarts a stopped alert), Pause/Restart,
  right-click (long-press) for Edit, Pause and Delete, **Delete stopped** to clear out the ones that are done.
- **Drawn lines**: lines, rays and trend lines carry their own alert (see [Drawings](CHARTS.md#drawings)). Edit those on the line;
  deleting one here switches it off on the line. A trend line's level is where the line is drawn at that moment on
  the chart its alert was switched on from (shown as *now …*): on a minute or hour chart it moves on only while the
  market trades, as their bars do; on a W chart by the clock; on an M chart in its month bars; on a log scale in log
  price.
- **Indicators** (Condition → any indicator in Studies ▾ except cumulative delta and COT), as TradingView's: one of
  its lines on a timeframe you pick (1m to 1D, with its own settings: Bollinger's period and deviations and its upper,
  middle or lower band, ADX's +DI, a pivot's R1, ...) **crossing**, **crossing up**, **crossing down**, **greater
  than** or **less than** the price, a value (RSI 70, ADX 25) or another line (another of its own, as MACD against
  its signal, %K against %D, +DI against -DI; or an EMA or SMA of any length). **Check** on **bar close** (TradingView's Once Per Bar Close) or **intrabar** (the first
  trade that meets it, Once Per Bar). NinjaTrader 8's formulas, the same as the chart's, on bars the server builds
  from history, so it fires with no chart open. Every time = once a bar. After a restart, a gap fill or turning it
  on, an alert first works its line out over the timeframe's last 3000 bars, never firing on that first look; on long
  settings (Regression 1000, Ichimoku 500) that takes a second or two in a worker process, and the alert checks from
  then on, a bar that closed meanwhile too (intrabar: one of the same session, not the last one's at the next open).
- **While history is still coming**: when a symbol's stored data stops 2 minutes or more short of its live trades
  (NinjaTrader just started, an instrument just put back on Connections > Instruments), its study, indicator, AMT and
  gap alerts send nothing until that stretch is filled (any part of it asked again as 1-minute bars too), as their
  lines would be worked out from bars days old; then they load again with it and alert as usual, a new gap seen
  meanwhile too if its first minute is under 6 minutes old (with its fill if it filled meanwhile; when the stretch's
  history failed, only if the stretch has no regular-hours minute, overnight or over a weekend, as the gap is measured
  from the last RTH close before it). A shorter stretch (the add-on reconnecting after a server restart) holds
  nothing back.

Delivery, set under **Connections > Alerts and Discord** (or in `.env`):

- **App notifications**: the installed app on each iPhone, Android phone or PC that turned them on (see [Phone and
  PC app](PHONE.md)). The server encrypts each alert for the device; Apple's, Google's or Microsoft's push service only
  carries it. No other app or account.
- **Discord** (`CHART_DISCORD_WEBHOOK`): a **private** channel's webhook.
- **ntfy** (`CHART_NTFY_URL`): phone push through the ntfy app. Subscribe to a long random topic and put
  `https://ntfy.sh/<topic>` here. Alert text (symbol, level, price) passes through ntfy.sh; self-host ntfy if that
  matters.
- **Heartbeat** (`CHART_HEARTBEAT_URL`): optional dead-man's switch (e.g. a healthchecks.io check). Pinged every 30 s
  while all symbols are ticking, so you hear about it if the PC, power or internet dies.
- **Discord: chart posts** (`CHART_DISCORD_CHARTS_WEBHOOK`): where **Post to Discord** sends chart pictures.
  Its own webhook, separate from the alert channel; mentions in a caption never ping anyone. **Check all** shows its
  state without posting in the channel; **Test** posts one line there.

Each one's **Test** button sends a test message (`POST /api/notify-test` sends one to all). The server also alerts when a symbol stops ticking for
`CHART_STALE_AFTER_S` during Globex hours, and again when it recovers. Weekends, the daily break and exchange
holidays (below) never count as stale, and the heartbeat keeps pinging through them.

## Chart pictures to Discord

The camera button (or the chart's right-click menu) has **Post to Discord…**: the chart picture with a caption, to
the channel set in Connections > **Discord: chart posts** (with two or more, one menu item each). See
[Charts](CHARTS.md) and [Connections](CONNECTIONS.md).

