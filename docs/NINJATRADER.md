# NinjaTrader

The NinjaTrader 8 add-on that sends every trade (and your fills) to the app. Other feeds: [FEEDS.md](FEEDS.md).

## Updating the add-on

`update.cmd` copies it in with the rest (README, Updating). By hand: **Connections > NinjaTrader > Download the
add-on** (it has the server's Token in it, when one is set), put it in `Documents\NinjaTrader 8\bin\Custom\AddOns\`
(replace the old one), then New > NinjaScript Editor, F5. When the add-on running in NinjaTrader is older than the
server, its card turns yellow and says so.

## The NinjaTrader feed

`ninjatrader/TatankaChartFeed.cs` is a NinjaTrader 8 add-on that forwards every live trade (price, size,
buy/sell side from bid/ask) to this server over `127.0.0.1:8801`, and your fills and positions for the journal
([CHARTS.md](CHARTS.md#your-fills-and-the-journal)). Read-only: it places no orders and changes nothing in any account.

Setting it up:

1. Off the simulator: **Leave the simulator**, in the setup guide on the first run or on Connections > NinjaTrader
   while the simulator runs (README, First run). It deletes the simulator's invented trades and restarts on
   NinjaTrader.
2. Copy `ninjatrader\TatankaChartFeed.cs` to `Documents\NinjaTrader 8\bin\Custom\AddOns\` (or **Download the
   add-on** on Connections > NinjaTrader, which has the server's Token in it).
3. In NinjaTrader, connected to your data feed: New > NinjaScript Editor, press F5. The bottom of the editor must
   show no errors. It starts right away and with NinjaTrader from then on.
4. New > NinjaScript Output should show `TatankaChartFeed: forwarding ES DEC26` (one line per market) and
   `TatankaChartFeed: connected to 127.0.0.1:8801`.
5. Check http://127.0.0.1:8800/api/health: `feed_status.ninjatrader.connected` is true and `ticks` climbs while the
   market trades; the chart's last price matches NinjaTrader's.
6. History: the add-on fills the last 90 days by itself (below), the year before that in the background, and anything
   older as a chart is scrolled back (add-on 20, Older history below), as far as your data provider's history goes.
   A backfill or a Databento import ([DATA.md](DATA.md)) is for what it doesn't have.

Over a weekend (Friday 17:00 to Sunday 16:00 CT) and on a closed CME holiday (Christmas, New Year's Day, Good Friday
in `config/holidays.json`: from 17:00 the evening before) no future trades, so a trade line stamped then is left out:
NinjaTrader replays its last trade when it connects, and the add-on stamps it with the PC's clock, which made a
Saturday (or Christmas) bar of one price on the daily charts. Such prints stored before this was in are taken out
once, in the background after the next start (feed plugins' trades the same; not the simulator's, which leaving it
deletes anyway). Add-on 18 leaves that replay out itself, by NinjaTrader's own time for the trade (over 20 minutes
old when it comes), so a reconnect in the 16:00-17:00 break no longer opens the next session at the last one's close;
when several trades in a row look that old, seconds apart, it is the PC's clock that is off, and they go (add-on 19:
replays at separate reconnects, hours apart over a weekend or a holiday, never count as that).

One add-on at a time: a second NinjaTrader running it (another Windows user on the PC) is turned away while the first
is connected (`another NinjaTrader add-on is connected to the server; trying again in 30 s`) and takes over, gap fill
and all, once the first closes. One whose `Token` is not the server's (`CHART_NT_TOKEN`, or the Token field in
Connections > NinjaTrader) is turned away the same way (`wrong token: ...`), and the card says so.

**Your fills and positions** (for the journal): every account's fills as they happen, and the session's again on
each connect; NinjaTrader's position for a contract a second after its last change (at most 5 s after its first,
once 0.2 s pass with no change, so both events of a fill are in; 10 s if changes never stop), and every open one on
each connect. A position's time is on the clock of that account's own fills (add-on 14), measured on its live fills:
the simulator stamps a Sim account's fills with the PC's clock and a broker with its own, and neither need be the
market data's, so a PC clock seconds off the exchange no longer splits a trade into "exit unseen" rows. It is put
2.25 s early, the longest a position waits to be read, but never before a fill of that contract already sent or the
account's last connect (add-on 15): a fill made before a position was read but reported a second or two later (a fast
market) still comes after it, also when the account filled another contract in between (add-on 16; 15 put the
position at that other fill's time, after the one it did not count). Until the account's first fill since the add-on
started, the PC's clock or the market data's, whichever is earlier.

**Saved going forward**: every trade the add-on sends is stored (with its buy/sell side) along with the 1-minute
bars (roughly 100-150 MB a day for six markets). Trades are kept 5 years and 1-minute bars always (Connections >
Backups, Keep each trade). Gaps fill themselves: when the
add-on connects (NinjaTrader started, the server restarted, the PC back on), the server tells it where each
symbol's stored data stops, and the add-on fetches that stretch from NinjaTrader's own history: trades (side
unknown, so footprints use the tick rule there), or 1-minute bars when the gap is over 3 days or the data provider
keeps no tick history. Only the missing stretch is kept, so nothing is counted twice; open charts reload with it.
History stamped while the market is shut (the 16:00 break, a weekend, a holiday) is left out: NinjaTrader's 1-minute
history has a bar after the close holding the session's whole volume at the settlement price, which would put a day's
volume in one minute on every chart and volume profile. Such bars stored before this was in are taken out once, at
the next start.
NinjaScript Output shows `filling ES DEC26 ... from NinjaTrader's trades` and `sent N trades of ES history`;
`/api/health` shows `gap_fill` per symbol. It reaches back 90 days (a 2-month absence comes back whole), and a
future with nothing stored yet (one just added in Instruments) gets its last 90 days, as 1-minute bars. A stretch
across contract rolls comes from each contract in turn, switching at NinjaTrader's roll dates (Tools > Instruments >
the symbol > Contract months): before a roll the old contract's prices, as charted then. Anything older than 90 days
comes from the backfill instead.

History comes from your data provider, not only from what NinjaTrader recorded itself while it ran (add-on 7);
where the provider has no trades for a stretch (tick history often reaches back only a few days), it is asked again
as 1-minute bars. If NinjaTrader's own 1-minute chart has no bars there either, the provider has none: import that
stretch from Databento instead (DATA.md).

NinjaTrader starts the add-on before its data connection is up, so the add-on (version 8) waits for the connection
before it asks for any history (`history for ES waits for NinjaTrader's data connection`), then asks right up to its
first live trade: a NinjaTrader restart leaves no hole, however long the connection takes. Holes inside the stored
data of the last 7 days (5 or more minutes with no bar while the market was open; 30 for a quiet market) are found on
each connect, and when a market ticks again after a Feed stale alert with the add-on still connected (NinjaTrader's
data connection lost and back), and filled the same way, one at a time, each once: if NinjaTrader has no history for
one, it stays as it is. Never from before the market's last contract roll (the history would be the other
contract's); and each is asked from 3 minutes before it, so if NinjaTrader's prices there don't match the stored ones
(a roll before this version kept roll times), the answer is dropped.

**Older history** (add-on 20): the 1-minute bars from before anything stored, also from NinjaTrader, nothing to run.
Once a future's gap and holes are filled, the server asks for the month before its first stored minute, then the
month before that, until it has a year (`CHART_HISTORY_PREFILL_DAYS` in `.env`, 365; 0 turns it off), one request at a
time for all futures, after any other history. A chart scrolled back past the stored minutes asks at once, as many
months as it needs (up to 6 a time), ahead of that ("loading older bars from NinjaTrader…"), so it keeps going as far
back as the data provider's history does. Each part comes from its own contract by NinjaTrader's roll dates (Tools >
Instruments > the symbol > Contract months), however old, none from before the first roll date NinjaTrader lists. A
month that comes back empty is where the provider's history ends: the chart says so, and it isn't asked again for a
week. Not answered in 6 minutes, or answered with an error (no data connection): asked again 10 minutes later; three
times in a row, and that is taken as where the history ends too.
Charts don't reload for it (they load the older bars as they are scrolled back); a future's studies (AMT Toolkit,
Daily SMA x4, TPO's days...) load again once its older months are in, if any are from the last 400 days. About 50-70
MB per future per year. NinjaScript Output shows `filling ES 06-26 ... from NinjaTrader's 1-minute bars`;
`/api/health` shows it under `gap_fill` (`"older": true`). An add-on before 20 isn't asked: the stored history is
all there is.

Which futures: the server's list (**Connections > App > Instruments**, [CONNECTIONS.md](CONNECTIONS.md#instruments)). On connect the server tells the add-on
which roots it charts and the add-on forwards each one's trades; its own `Contracts` list (ES, NQ, CL, GC, MES, MNQ)
is forwarded either way.
