// TatankaChartFeed: NinjaTrader 8 add-on that forwards live trades to the Tatanka Charts server.
// The futures feed for Tatanka Charts. Read-only: it subscribes to market data, reads your fills
// (executions) and positions (for the chart markers and the journal) and writes lines to a local TCP socket. It places
// no orders and changes nothing in any account.
//
// Install: copy this file to  Documents\NinjaTrader 8\bin\Custom\AddOns\
//          then New > NinjaScript Editor > press F5 to compile. It starts on compile and with NinjaTrader.
// Status lines appear in New > NinjaScript Output. Market Replay and Simulated Data Feed trades are skipped.
//
// Wire format, one line per message, "\n" terminated, invariant culture:
//   HELLO|<token>|<version>                 first line after connecting; the server says when the add-on is older than it
//   C|<sym>|<contract>                       the contract now forwarded for <sym> ("ES 03-27"): on connect and on a roll
//   C|<sym>|<contract>|quote                 a watchlist future's quote rolled: the settlements known are the old one's
//   T|<sym>|<utc epoch ms>|<price>|<size>|<side>   side +1 at/above ask, -1 at/below bid, 0 between
//   Q|<sym>|<last>|<prior session close>|<day volume>   once a second per symbol, for the watchlist, while its
//                                            market data comes (none after 60 s without, nor while NinjaTrader's
//                                            live data is down or Playback or the Simulated Data Feed is connected)
//   S|<sym>|<settlement>                    the settlement its exchange published (NinjaTrader's Settlement), on a change
//                                            and on each connect: the watchlist's change is from it
//   H|<utc epoch ms>                         heartbeat every 5 s
//   X|<sym>|<utc epoch ms>|<price>|<size>    a trade from NinjaTrader's history (gap fill, no side)
//   Y|<sym>|<bar open utc ms>|<o>|<h>|<l>|<c>|<volume>   a 1-minute bar from history (no tick history)
//   Z|<sym>|<ticks|minutes|error|none>|<count>|<note>     history for <sym> is done
//   E|<account>|<root>|<contract>|<utc epoch ms>|<price>|<qty>|<side>|<point value>|<commission>|<execution id>|<order id>|<name>
//                                            one of your fills, side +1 bought, -1 sold; every account's, as they happen,
//                                            and the session's again on each connect (the server stores each id once)
//   P|<account>|<root>|<contract>|<utc epoch ms>|<position>|<average price>|<point value>
//                                            one of your positions as NinjaTrader has it, + long, - short, 0 flat: a
//                                            second after its last change (at most 5 s after its first, once 0.2 s pass
//                                            with no change; 10 s if changes never stop), and every open one on each
//                                            connect. Its time is now on the clock of the account's own fills (measured
//                                            on its live fills, less 2.25 s, the longest a read waits; until its first,
//                                            the PC's or NinjaTrader's market data time, whichever is earlier), never
//                                            before a fill sent for it
//   P|<account>|<utc epoch ms>               on each connect, after the account's open positions (at the same time):
//                                            its other contracts are flat. With these the journal puts right a fill the
//                                            server never got (a position opened before the add-on was installed)
// From the server: GAP|<sym>|<from utc ms>|<to utc ms>[|live|minutes|older], sent on connect where its stored data stops short (and
// for holes inside it, one at a time per symbol); the add-on fetches that stretch from NinjaTrader (trades, else
// 1-minute bars) and sends it back, once NinjaTrader's data connection is up (at NinjaTrader's start the add-on is
// running before it). live: up to the add-on's first live trade then. minutes: 1-minute bars, for a stretch the
// trades didn't cover (the provider keeps no tick history that far back). A stretch across contract rolls comes from
// each contract in turn, switching at NinjaTrader's roll dates, from the one the server charted last (R line) to the
// one forwarded now. older (20+): history from before anything the server has stored (charts scrolled back, the last
// year filled in the background), as 1-minute bars, each contract's by NinjaTrader's roll dates however old, none for
// a time before the first roll date NinjaTrader knows; answered with Y lines and Z as the others.
// BYE|<reason>: the server has another NinjaTrader's add-on connected (two NinjaTrader instances): this one hangs up,
// drops what it queued meanwhile (the other sent those trades) and tries again in 30 s; once let in, the gap fill
// covers the stretch from where the other stopped.
// W|<root>,<root>,...: the futures in the app's watchlists ("ZN,SR3,NG"); the add-on quotes each one's front month
// (Q lines only, no trades) and follows its rolls. Roots NinjaTrader does not know are skipped.
// R|<root>|<volume|dates|MM-yy>|<contract charted last>: how a chart future rolls (Connections > Instruments). volume
// (the default): to the next contract in the 16:00-17:00 CT break (or a weekend) after a session in which the next one
// traded more, or two days after NinjaTrader's roll date if the volumes never came; never back to an older contract.
// dates: NinjaTrader's own roll dates. MM-yy: that month, until changed in the app. Until a chart future's first R
// line, its trades and its C line are held back (it may be on the wrong contract); with a server that sends no R
// lines, they go 10 s after connecting. A built-in market the server's I line leaves out is quoted only.
#region Using declarations
using System;
using System.Collections.Concurrent;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Net.Sockets;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
using NinjaTrader.Cbi;
using NinjaTrader.Data;
#endregion

namespace NinjaTrader.NinjaScript.AddOns
{
	public class TatankaChartFeed : AddOnBase
	{
		// One contract per market, NinjaTrader naming ("ES 12-26" = December 2026). Any month NinjaTrader knows will do:
		// the add-on starts on the front month by NinjaTrader's rollover table (Tools > Instruments > the symbol > Contract
		// months), then rolls as the server says (R lines: by volume unless set otherwise), so no edits on rollover.
		private static readonly string[] Contracts = { "ES 12-26", "NQ 12-26", "CL 12-26", "GC 12-26", "MES 12-26", "MNQ 12-26" };
		private const string Host     = "127.0.0.1";
		private const int    Port     = 8801;
		private const string Token    = "";       // must equal CHART_NT_TOKEN if the server sets one
		private const int    MaxQueue = 200000;   // lines buffered while the server is down; the oldest are dropped
		private const long   TickHistoryMaxMs = 3L * 86400000;   // longer gaps come as 1-minute bars
		private const int    Version  = 20;       // raised with each change; the server's ADDON_VERSION
		private const long   QuoteLiveMs = 60000;   // a symbol's Q lines stop once its market data is silent this long
		private const long   ReplayMs = 20 * 60000; // a trade older than this when it comes is NinjaTrader's replay of its last
		private const int    ReplayRun = 3;         // ...unless more come in a row, each within ReplayRunMs of the one before:
		private const long   ReplayRunMs = 5000;    // then it is the PC's clock that is off (replays come one a reconnect)
		private const int    HistoryRetries = 10;                // 30 s apart, while NinjaTrader's connection comes up

		private static readonly CultureInfo Inv = CultureInfo.InvariantCulture;
		private static readonly TimeZoneInfo Central = FindCentral();
		private readonly List<Feed> feeds = new List<Feed>();
		private readonly List<BarsRequest> requests = new List<BarsRequest>();
		private System.Threading.Timer rollTimer;
		private readonly ConcurrentDictionary<string, double> lastPrice = new ConcurrentDictionary<string, double>();
		private readonly ConcurrentDictionary<string, double> priorClose = new ConcurrentDictionary<string, double>();
		private readonly ConcurrentDictionary<string, long> dayVolume = new ConcurrentDictionary<string, long>();
		private readonly ConcurrentDictionary<string, long> heardAt = new ConcurrentDictionary<string, long>();   // PC ms of its last market data
		private readonly ConcurrentDictionary<string, int> replays = new ConcurrentDictionary<string, int>();   // trades left out as replays, in a row
		private readonly ConcurrentDictionary<string, long> replayAt = new ConcurrentDictionary<string, long>();   // PC ms of the last that looked old
		private readonly ConcurrentDictionary<string, long> nextVolume = new ConcurrentDictionary<string, long>();   // by contract
		private readonly ConcurrentDictionary<string, double> settlement = new ConcurrentDictionary<string, double>();
		private readonly ConcurrentDictionary<string, VolumeCheck> volumeChecks = new ConcurrentDictionary<string, VolumeCheck>();
		// Chart futures whose contract the server hasn't settled yet (its R line): no trades, no C line until it has.
		private readonly ConcurrentDictionary<string, bool> held = new ConcurrentDictionary<string, bool>();
		private volatile bool heardR;   // an R line on this connection: the server says how each one rolls
		private volatile int connection;   // counts connections to the server: history asked on an older one is dropped
		private volatile bool turnedAway;  // the server said BYE: another add-on has it
		// Per symbol, NinjaTrader's time (whole seconds) of the first trade queued since the last connection ended: a
		// GAP's open end stops there, on the same clock as the history (the server's cut is when trades arrive).
		private readonly ConcurrentDictionary<string, long> firstLive = new ConcurrentDictionary<string, long>();
		private HashSet<string> chartRoots = new HashSet<string>();   // the server's last I list (under `feeds`)
		private MarketDataEventArgs lastTrade;   // the same event delivered twice is sent once
		private readonly HashSet<string> unknownRoots = new HashSet<string>();   // reported once each
		private readonly List<Account> watchedAccounts = new List<Account>();   // their fills and positions go to the journal
		// By account, the latest time (UTC ms, NinjaTrader's clock) its E and P lines carried (sentAt) and its last
		// connect's (snapAt); by account and contract ("Sim101|ES 12-26"), the latest its E and P lines carried, and the
		// positions changed since their last P line.
		private readonly ConcurrentDictionary<string, long> sentAt = new ConcurrentDictionary<string, long>();
		private readonly ConcurrentDictionary<string, long> snapAt = new ConcurrentDictionary<string, long>();
		private readonly ConcurrentDictionary<string, long> sentAtC = new ConcurrentDictionary<string, long>();
		private readonly ConcurrentDictionary<string, Changed> changed = new ConcurrentDictionary<string, Changed>();
		private long clockOffset;   // NinjaTrader's market data time less the PC's (ms), from a live trade once a minute
		// By account, its fills' time less the PC's as they come (ms), from its last live fill: the clock its P lines carry.
		// The simulator stamps fills with the PC's clock, a broker with its own: neither need be the market data's.
		private readonly ConcurrentDictionary<string, long> fillOffset = new ConcurrentDictionary<string, long>();
		private long nextClock;
		private BlockingCollection<string> queue;
		private CancellationTokenSource cts;
		private Thread sender;
		private long dropped;

		protected override void OnStateChange()
		{
			if (State == State.SetDefaults)
			{
				Name        = "TatankaChartFeed";
				Description = "Forwards live trades to the Tatanka Charts server. Read-only.";
			}
			else if (State == State.Active)
			{
				queue  = new BlockingCollection<string>(new ConcurrentQueue<string>(), MaxQueue);
				cts    = new CancellationTokenSource();
				sender = new Thread(SendLoop) { IsBackground = true, Name = "TatankaChartFeed" };
				sender.Start();

				lock (feeds)
				{
					foreach (string name in Contracts)
					{
						Instrument seed = Instrument.GetInstrument(name);
						if (seed == null)
						{
							Print("TatankaChartFeed: instrument not found: " + name);
							continue;
						}
						Feed f = new Feed { Master = seed.MasterInstrument };
						feeds.Add(f);
						held[f.Master.Name] = true;
						Forward(f, FrontMonth(f.Master) ?? seed);
					}
				}
				WatchAccounts();
				rollTimer = new System.Threading.Timer(CheckRolls, null, 60000, 60000);   // roll check once a minute
			}
			else if (State == State.Terminated)
			{
				if (rollTimer != null) rollTimer.Dispose();
				lock (feeds)
				{
					foreach (Feed f in feeds)
					{
						Instrument i = f.Current;
						MarketData md = f.Data;
						EventHandler<MarketDataEventArgs> h = Handler(f);
						if (i != null && md != null) i.Dispatcher.InvokeAsync(() => md.Update -= h);
						f.Data = null;
						WatchNext(f, null);
					}
					feeds.Clear();
					held.Clear();
				}
				lock (requests)
				{
					foreach (BarsRequest r in requests) r.Dispose();
					requests.Clear();
				}
				lock (watchedAccounts)
				{
					foreach (Account a in watchedAccounts)
					{
						a.ExecutionUpdate -= OnExecutionUpdate;
						a.PositionUpdate -= OnPositionUpdate;
					}
					watchedAccounts.Clear();
				}
				if (cts != null) cts.Cancel();
				if (queue != null) queue.CompleteAdding();
			}
		}

		private sealed class Feed
		{
			public MasterInstrument Master;
			public Instrument Current;
			public MarketData Data;  // this add-on's own subscription to Current
			public bool QuoteOnly;   // a watchlist future: quotes, no trades, a C line only on a roll
			public string Mode = "volume";   // how it rolls (the server's R line): volume, dates, or a month "03-27"
			public Instrument Next;          // by volume: the contract after Current, watched for its day's volume
			public Instrument Charted;       // the contract the server charted last (R line): where its stored data stops
			public MarketData NextData;
		}

		// One of your positions that changed (a fill, or NinjaTrader's position update): its P line is due.
		private sealed class Changed
		{
			public Account Account;
			public Instrument Instrument;
			public DateTime First, At;   // its first change and its last: the P line goes a second after the last
		}

		// NinjaTrader's count of a market's contracts today against what this add-on sent since its first reading.
		private sealed class VolumeCheck
		{
			public long Base = -1, Sent;
			public bool Reported;
			public DateTime NextWarning;
		}

		private EventHandler<MarketDataEventArgs> Handler(Feed f)
		{
			return f.QuoteOnly ? (EventHandler<MarketDataEventArgs>)OnQuoteData : OnMarketData;
		}

		private static TimeZoneInfo FindCentral()
		{
			foreach (string id in new[] { "Central Standard Time", "America/Chicago" })
			{
				try { return TimeZoneInfo.FindSystemTimeZoneById(id); }
				catch (Exception) { }
			}
			return TimeZoneInfo.CreateCustomTimeZone("CT", TimeSpan.FromHours(-6), "CT", "CT");   // never: Windows has it
		}

		// NinjaTrader's front month for the next trade date: the contract whose roll date is the latest on or before it.
		// The trade date turns at the 16:00 CT close, so a roll lands in the break before the session it belongs to.
		private static Instrument FrontMonth(MasterInstrument master)
		{
			DateTime rolled;
			return FrontMonth(master, out rolled);
		}

		private static Instrument FrontMonth(MasterInstrument master, out DateTime rolled)
		{
			DateTime ct = TimeZoneInfo.ConvertTimeFromUtc(DateTime.UtcNow, Central);
			DateTime tradeDate = ct.TimeOfDay >= TimeSpan.FromHours(16) ? ct.Date.AddDays(1) : ct.Date;
			DateTime month = DateTime.MinValue;
			rolled = DateTime.MinValue;
			foreach (Rollover r in master.RolloverCollection)
			{
				if (r.Date.Date <= tradeDate && r.Date >= rolled)
				{
					rolled = r.Date;
					month = r.ContractMonth;
				}
			}
			return month == DateTime.MinValue ? null : Instrument.GetInstrument(master.Name + " " + month.ToString("MM-yy", Inv));
		}

		// Moves a market's subscription to `instrument` and tells the server. Call with `feeds` locked.
		// The subscription is a MarketData of this add-on's own, as NinjaTrader's add-on framework has it. (A day forwarded
		// while it listened on the instrument's shared MarketData came out at twice NinjaTrader's own volume.)
		private void Forward(Feed f, Instrument instrument)
		{
			Instrument old = f.Current;
			MarketData oldData = f.Data;
			f.Current = instrument;
			f.Data = null;
			EventHandler<MarketDataEventArgs> h = Handler(f);
			if (old != null && oldData != null) old.Dispatcher.InvokeAsync(() => oldData.Update -= h);
			instrument.Dispatcher.InvokeAsync(() =>
			{
				lock (feeds)
				{
					if (f.Current != instrument || f.Data != null || !feeds.Contains(f)) return;   // rolled or removed meanwhile
					f.Data = new MarketData(instrument);
					f.Data.Update += h;
				}
			});
			if (f.QuoteOnly)
			{
				if (old != null)
				{
					Print("TatankaChartFeed: watchlist quote rolled " + old.FullName + " to " + instrument.FullName);
					Enqueue("C|" + f.Master.Name + "|" + instrument.FullName + "|quote");
				}
				return;
			}
			if (!held.ContainsKey(f.Master.Name)) Enqueue("C|" + f.Master.Name + "|" + instrument.FullName);
			Print("TatankaChartFeed: " + (old == null ? "forwarding " : "rolled " + old.FullName + " to ") + instrument.FullName);
		}

		// The server has settled a chart future's contract (or sends no R lines): its trades go from now on, and the
		// contract it is on. Call with `feeds` locked.
		private void Release(Feed f)
		{
			bool was;
			if (f.QuoteOnly || f.Current == null || !held.TryRemove(f.Master.Name, out was)) return;
			Enqueue("C|" + f.Master.Name + "|" + f.Current.FullName);
		}

		private static int Months(Instrument i)
		{
			return i.Expiry.Year * 12 + i.Expiry.Month;
		}

		private static bool IsMonth(string mode)
		{
			return mode != null && mode.Length == 5 && mode[2] == '-' && char.IsDigit(mode[0]) && char.IsDigit(mode[1])
				&& char.IsDigit(mode[3]) && char.IsDigit(mode[4]);
		}

		// CME Globex shut: the 16:00-17:00 CT break, or the weekend (Friday 16:00 to Sunday 17:00).
		private static bool Shut(DateTime ct)
		{
			TimeSpan t = ct.TimeOfDay;
			switch (ct.DayOfWeek)
			{
				case DayOfWeek.Saturday: return true;
				case DayOfWeek.Sunday: return t < TimeSpan.FromHours(17);
				case DayOfWeek.Friday: return t >= TimeSpan.FromHours(16);
				default: return t >= TimeSpan.FromHours(16) && t < TimeSpan.FromHours(17);
			}
		}

		// The contract after `current` in NinjaTrader's list of contract months, or null.
		private static Instrument NextContract(MasterInstrument master, Instrument current)
		{
			int cur = Months(current), best = int.MaxValue;
			DateTime month = DateTime.MinValue;
			foreach (Rollover r in master.RolloverCollection)
			{
				int m = r.ContractMonth.Year * 12 + r.ContractMonth.Month;
				if (m > cur && m < best)
				{
					best = m;
					month = r.ContractMonth;
				}
			}
			return month == DateTime.MinValue ? null : Instrument.GetInstrument(master.Name + " " + month.ToString("MM-yy", Inv));
		}

		// The contract a market should be on now, by its mode. By volume it only moves forward, one contract at a time,
		// only while CME is shut: once the next contract traded more in the session just ended, or two days after
		// NinjaTrader's roll date if the volumes never came. Call with `feeds` locked.
		private Instrument Target(Feed f, DateTime ct)
		{
			DateTime rollDate;
			Instrument byDates = FrontMonth(f.Master, out rollDate);
			if (f.QuoteOnly || f.Mode == "dates" || f.Current == null) return byDates;
			if (IsMonth(f.Mode))
			{
				Instrument pinned = Instrument.GetInstrument(f.Master.Name + " " + f.Mode);
				if (pinned != null) return pinned;   // until changed in the app; a month NinjaTrader doesn't list: by volume
			}
			Instrument next = NextContract(f.Master, f.Current);
			WatchNext(f, next);
			if (next == null || !Shut(ct)) return f.Current;
			DateTime tradeDate = ct.TimeOfDay >= TimeSpan.FromHours(16) ? ct.Date.AddDays(1) : ct.Date;
			long cur, nxt;
			bool traded = dayVolume.TryGetValue(f.Master.Name, out cur) && nextVolume.TryGetValue(next.FullName, out nxt) && nxt > cur;
			bool late = byDates != null && Months(byDates) > Months(f.Current) && tradeDate > rollDate.Date.AddDays(2);
			return traded || late ? next : f.Current;
		}

		// By volume: watch `next`'s day volume (instead of whatever was watched before; null: none). Call with `feeds` locked.
		private void WatchNext(Feed f, Instrument next)
		{
			if (f.Next == next || (f.Next != null && next != null && f.Next.FullName == next.FullName)) return;
			Instrument old = f.Next;
			MarketData oldData = f.NextData;
			f.Next = next;
			f.NextData = null;
			if (old != null && oldData != null) old.Dispatcher.InvokeAsync(() => oldData.Update -= OnNextData);
			if (next == null) return;
			next.Dispatcher.InvokeAsync(() =>
			{
				lock (feeds)
				{
					if (f.Next != next || f.NextData != null || !feeds.Contains(f)) return;   // changed or removed meanwhile
					f.NextData = new MarketData(next);
					f.NextData.Update += OnNextData;
				}
			});
		}

		// The next contract's day volume (instrument thread), for rolling by volume.
		private void OnNextData(object source, MarketDataEventArgs e)
		{
			if (e.MarketDataType == MarketDataType.DailyVolume) nextVolume[e.Instrument.FullName] = (long)e.Volume;
		}

		// The server's choice for a chart future (R line): volume, dates or a month; and the contract it charted last, so
		// rolling by volume never steps back to an older one after NinjaTrader restarts.
		private void SetMode(string root, string mode, string last)
		{
			if (mode != "volume" && mode != "dates" && !IsMonth(mode)) return;
			lock (feeds)
			{
				foreach (Feed f in feeds)
				{
					if (f.QuoteOnly || f.Master.Name != root || f.Current == null) continue;
					f.Mode = mode;
					Instrument had = last.Length > 0 ? Instrument.GetInstrument(last) : null;
					f.Charted = had != null && had.MasterInstrument.Name == root ? had : null;
					DateTime ct = TimeZoneInfo.ConvertTimeFromUtc(DateTime.UtcNow, Central), rollDate;
					Instrument byDates = FrontMonth(f.Master, out rollDate);
					DateTime tradeDate = ct.TimeOfDay >= TimeSpan.FromHours(16) ? ct.Date.AddDays(1) : ct.Date;
					// By volume it carries on from the contract the server charted last: newer than NinjaTrader's dates
					// front (it rolled first), or, just after a start, older (the volumes haven't crossed yet) until
					// the volume rule would have left it anyway, two days after NinjaTrader's roll date, so never an
					// expired one (Expiry carries the contract month, not its last day).
					if (mode == "volume" && had != null && had.MasterInstrument.Name == root && had.FullName != f.Current.FullName
						&& (Months(had) > Months(f.Current) || (held.ContainsKey(root) && Months(had) >= ct.Year * 12 + ct.Month
							&& (byDates == null || Months(byDates) <= Months(had) || tradeDate <= rollDate.Date.AddDays(2)))))
						Forward(f, had);
					Instrument want = Target(f, ct);
					if (want != null && want.FullName != f.Current.FullName) Forward(f, want);
					Release(f);
				}
				heardR = true;
			}
		}

		// Once a minute, on a timer thread.
		private void CheckRolls(object state)
		{
			try { WatchAccounts(); }
			catch (Exception ex) { Print("TatankaChartFeed: account check failed: " + ex.Message); }
			try
			{
				DateTime ct = TimeZoneInfo.ConvertTimeFromUtc(DateTime.UtcNow, Central);
				lock (feeds)
				{
					foreach (Feed f in feeds)
					{
						Instrument want = Target(f, ct);
						if (want != null && f.Current != null && want.FullName != f.Current.FullName) Forward(f, want);
					}
				}
			}
			catch (Exception ex)
			{
				Print("TatankaChartFeed: rollover check failed: " + ex.Message);
			}
		}

		// The server's requests: GAP lines, read on their own thread until the connection closes.
		private void ReadLoop(NetworkStream stream, int gen)
		{
			try
			{
				using (StreamReader r = new StreamReader(stream, new UTF8Encoding(false), false, 4096, true))
				{
					string line;
					while ((line = r.ReadLine()) != null)
					{
						string[] p = line.Split('|');
						if ((p.Length == 4 || p.Length == 5) && p[0] == "GAP")
						{
							string sym = p[1];
							long from = long.Parse(p[2], Inv), to = long.Parse(p[3], Inv);
							bool live = p.Length == 5 && p[4] == "live", older = p.Length == 5 && p[4] == "older";
							bool minutes = older || (p.Length == 5 && p[4] == "minutes");
							new Thread(() => History(sym, from, to, live, !minutes, gen, older)) { IsBackground = true, Name = "TatankaChartFeed history" }.Start();
						}
						else if (p.Length == 2 && p[0] == "I")
							SetCharts(p[1].Split(new[] { ',' }, StringSplitOptions.RemoveEmptyEntries));
						else if (p.Length == 2 && p[0] == "W")
							SetWatch(p[1].Split(new[] { ',' }, StringSplitOptions.RemoveEmptyEntries));
						else if (p.Length >= 3 && p[0] == "R")
							SetMode(p[1], p[2], p.Length > 3 ? p[3] : "");
						else if (p.Length >= 2 && p[0] == "BYE")
						{
							turnedAway = true;   // the send loop hangs up
							Print("TatankaChartFeed: " + p[1] + "; trying again in 30 s");
						}
					}
				}
			}
			catch (Exception) { }   // the connection closed; the send loop reconnects
		}

		// The server's chart symbols (Connections > Instruments, sent on connect): forward each root's trades, as the
		// built-in Contracts are. A root the watchlists already quote moves from quotes only to trades. A built-in one
		// not in the list (taken off Instruments, or a plugin's) gets no R line and would stay held, quote and all: it
		// is quoted only from now, as a watchlist future (the W line that follows keeps it or drops it).
		private void SetCharts(string[] roots)
		{
			int added = 0;
			lock (feeds)
			{
				foreach (string root in roots)   // charted from now: its first live trade is still to come
					if (!chartRoots.Contains(root)) { long was; firstLive.TryRemove(root, out was); }
				chartRoots = new HashSet<string>(roots);
				for (int k = 0; k < feeds.Count; k++)
				{
					Feed f = feeds[k];
					if (f.QuoteOnly || f.Current == null || chartRoots.Contains(f.Master.Name) || !held.ContainsKey(f.Master.Name)) continue;
					Instrument i = f.Current;
					MarketData md = f.Data;
					if (md != null) i.Dispatcher.InvokeAsync(() => md.Update -= OnMarketData);
					f.Data = null;
					WatchNext(f, null);
					bool was;
					held.TryRemove(f.Master.Name, out was);
					// A new Feed, as from quotes to trades below: a trade subscription the instrument thread hasn't made
					// yet (NinjaTrader starting up) finds the old one gone, so it is never made on a quoted market.
					Feed q = new Feed { Master = f.Master, QuoteOnly = true };
					feeds[k] = q;
					Forward(q, FrontMonth(q.Master) ?? i);
				}
				foreach (string root in roots)
				{
					Feed have = null;
					foreach (Feed f in feeds)
						if (f.Master.Name == root) have = f;
					if (have != null && !have.QuoteOnly) continue;
					Instrument any = have != null ? have.Current : AnyContract(root);
					if (have != null)
					{
						Instrument qi = have.Current;
						MarketData qmd = have.Data;
						if (qi != null && qmd != null) qi.Dispatcher.InvokeAsync(() => qmd.Update -= OnQuoteData);
						have.Data = null;
						feeds.Remove(have);
					}
					if (any == null)
					{
						if (unknownRoots.Add(root)) Print("TatankaChartFeed: no NinjaTrader instrument for chart symbol " + root);
						continue;
					}
					Feed full = new Feed { Master = any.MasterInstrument };
					feeds.Add(full);
					held[root] = true;   // until its R line (right after this I line)
					Forward(full, FrontMonth(full.Master) ?? any);
					added++;
				}
			}
			if (added > 0) Print("TatankaChartFeed: forwarding " + added + " more chart symbols");
		}

		// The watchlists' futures: quote each root's front month, drop the ones no longer listed. Chart symbols are
		// already forwarded and quoted.
		private void SetWatch(string[] roots)
		{
			HashSet<string> want = new HashSet<string>(roots);
			int added = 0;
			lock (feeds)
			{
				for (int k = feeds.Count - 1; k >= 0; k--)
				{
					Feed f = feeds[k];
					if (!f.QuoteOnly || want.Contains(f.Master.Name)) continue;
					Instrument i = f.Current;
					MarketData md = f.Data;
					if (md != null) i.Dispatcher.InvokeAsync(() => md.Update -= OnQuoteData);
					f.Data = null;
					feeds.RemoveAt(k);
					double d;
					long v;
					lastPrice.TryRemove(f.Master.Name, out d);
					priorClose.TryRemove(f.Master.Name, out d);
					dayVolume.TryRemove(f.Master.Name, out v);
					heardAt.TryRemove(f.Master.Name, out v);
				}
				foreach (string root in want)
				{
					bool have = false;
					foreach (Feed f in feeds)
						if (f.Master.Name == root) have = true;
					if (have) continue;
					Instrument any = AnyContract(root);
					if (any == null)
					{
						if (unknownRoots.Add(root)) Print("TatankaChartFeed: no NinjaTrader instrument for watchlist future " + root);
						continue;
					}
					Feed q = new Feed { Master = any.MasterInstrument, QuoteOnly = true };
					feeds.Add(q);
					Forward(q, FrontMonth(q.Master) ?? any);
					added++;
				}
			}
			if (added > 0) Print("TatankaChartFeed: quoting " + added + " more watchlist futures");
		}

		// Some contract of `root` NinjaTrader knows, to reach its master instrument: the coming 15 months are tried.
		private static Instrument AnyContract(string root)
		{
			DateTime month = DateTime.UtcNow;
			for (int k = 0; k < 15; k++, month = month.AddMonths(1))
			{
				Instrument i = Instrument.GetInstrument(root + " " + month.ToString("MM-yy", Inv));
				if (i != null) return i;
			}
			return null;
		}

		// NinjaTrader returns history in the time zone of Tools > Options > General; read it by name so this compiles
		// on any version, and fall back to the PC's zone. Read again at most once a minute (every trade asks).
		private static volatile TimeZoneInfo ntZone;
		private static long ntZoneAt;
		private static TimeZoneInfo NtZone()
		{
			long now = NowMs();
			TimeZoneInfo z = ntZone;
			if (z != null && now - Interlocked.Read(ref ntZoneAt) < 60000) return z;
			ntZone = z = ReadNtZone();
			Interlocked.Exchange(ref ntZoneAt, now);
			return z;
		}

		private static TimeZoneInfo ReadNtZone()
		{
			try
			{
				foreach (System.Reflection.Assembly a in AppDomain.CurrentDomain.GetAssemblies())
				{
					Type g = a.GetType("NinjaTrader.Core.Globals", false);
					if (g == null) continue;
					System.Reflection.PropertyInfo op = g.GetProperty("GeneralOptions");
					object options = op != null ? op.GetValue(null, null) : null;
					if (options == null) break;
					System.Reflection.PropertyInfo zp = options.GetType().GetProperty("TimeZoneInfo");
					TimeZoneInfo z = zp != null ? zp.GetValue(options, null) as TimeZoneInfo : null;
					if (z != null) return z;
					break;
				}
			}
			catch (Exception) { }
			return TimeZoneInfo.Local;
		}

		private static long ToUtcMs(DateTime t, TimeZoneInfo zone)
		{
			return new DateTimeOffset(TimeZoneInfo.ConvertTimeToUtc(DateTime.SpecifyKind(t, DateTimeKind.Unspecified), zone)).ToUnixTimeMilliseconds();
		}

		// Waits (on a history thread) until NinjaTrader's data connection is up (at NinjaTrader's start the add-on runs
		// before it), then 5 s more; false if the server connection it was asked on has closed meanwhile.
		private bool WaitDataUp(int gen, string sym)
		{
			bool waited = false;
			while (gen == connection && !DataConnected())
			{
				if (!waited) Print("TatankaChartFeed: history for " + sym + " waits for NinjaTrader's data connection");
				waited = true;
				Thread.Sleep(5000);
			}
			if (waited) Thread.Sleep(5000);
			return gen == connection;
		}

		// A history line (X, Y, Z), sent only on the connection it was asked on: one that closed meanwhile has its
		// requests asked again by the server, and a stray Z there would close the wrong window.
		private void EnqueueHistory(int gen, string line)
		{
			Enqueue("\u0001" + gen.ToString(Inv) + "\u0001" + line);
		}

		// Writes a queued line, unless it is a history line asked on another connection.
		private static void Send(StreamWriter w, string line, int gen)
		{
			if (line.Length > 0 && line[0] == '\u0001')
			{
				int end = line.IndexOf('\u0001', 1);
				if (end < 0 || line.Substring(1, end - 1) != gen.ToString(Inv)) return;
				line = line.Substring(end + 1);
			}
			w.WriteLine(line);
		}

		// From the data provider, not only what NinjaTrader recorded itself while it ran (a stretch it was closed for is
		// not in its own database); of this very contract, not merged with the one before it (by the global Merge policy
		// a contract after a by-volume roll would come back as the older one, back-adjusted). Set by name: it compiles
		// whichever namespace the enums are in.
		private static void HistoryPolicies(BarsRequest req)
		{
			SetEnum(req, "LookupPolicy", "Provider");
			SetEnum(req, "MergePolicy", "DoNotMerge");
		}

		private static void SetEnum(object target, string property, string value)
		{
			try
			{
				System.Reflection.PropertyInfo p = target.GetType().GetProperty(property);
				if (p != null && p.CanWrite && p.PropertyType.IsEnum) p.SetValue(target, Enum.Parse(p.PropertyType, value), null);
			}
			catch (Exception) { }   // a NinjaTrader without it: its own setting applies
		}

		// A live market-data feed is connected: a connection's price feed (PriceStatus; Status is its order side), not
		// Playback, not the Simulated Data Feed.
		private static bool DataConnected()
		{
			Connection playback = Connection.PlaybackConnection;
			if (playback != null && playback.Status == ConnectionStatus.Connected) return false;
			lock (Connection.Connections)
			{
				foreach (Connection c in Connection.Connections)
				{
					string provider = c.Options != null ? c.Options.Provider.ToString() : "";
					if (c.PriceStatus == ConnectionStatus.Connected && provider != "Simulator" && provider != "Playback")
						return true;
				}
			}
			return false;
		}

		// One GAP answered, on its own thread: [fromMs, toMs] of each contract in turn (Segments), as trades, or as 1-minute
		// bars when the stretch is long, the provider keeps no tick history, or the server asks for minutes; then Z.
		// older: from before the server's stored data, each contract by NinjaTrader's roll dates alone.
		private void History(string sym, long fromMs, long toMs, bool live, bool ticks, int gen, bool older = false)
		{
			try
			{
				if (!WaitDataUp(gen, sym)) return;
				long cut;
				if (live) toMs = firstLive.TryGetValue(sym, out cut) ? cut : Math.Max(toMs, NowMs());
				if (toMs <= fromMs)
				{
					EnqueueHistory(gen, "Z|" + sym + "|none|0|nothing before the first live trade");
					return;
				}
				Instrument current = null, charted = null;
				lock (feeds)
					foreach (Feed f in feeds)
						if (f.Master.Name == sym && !f.QuoteOnly) { current = f.Current; charted = f.Charted; }
				if (current == null)
				{
					EnqueueHistory(gen, "Z|" + sym + "|none|0|not forwarded");
					return;
				}
				if (ticks && toMs - fromMs > TickHistoryMaxMs) ticks = false;
				List<KeyValuePair<Instrument, long>> segs = Segments(older ? null : charted, current, fromMs, toMs, older);
				if (segs.Count == 0)
				{
					EnqueueHistory(gen, "Z|" + sym + "|minutes|0|ok");   // before the first roll date NinjaTrader knows
					return;
				}
				while (true)
				{
					int sent = 0;
					string error = null;
					for (int k = 0; k < segs.Count && error == null; k++)
					{
						long a = Math.Max(fromMs, segs[k].Value), b = k + 1 < segs.Count ? segs[k + 1].Value : toMs;
						int got = Fetch(sym, segs[k].Key, a, b, ticks, gen, out error);
						if (got < 0) return;   // its connection closed: the server asks again
						sent += got;
					}
					if (ticks && (sent == 0 || error != null))
					{
						ticks = false;   // no tick history here: 1-minute bars
						continue;
					}
					if (!RoomFor(gen)) return;
					if (error != null)
					{
						EnqueueHistory(gen, "Z|" + sym + "|error|0|" + error);
						Print("TatankaChartFeed: no history for " + sym + ": " + error);
					}
					else
					{
						EnqueueHistory(gen, string.Format(Inv, "Z|{0}|{1}|{2}|ok", sym, ticks ? "ticks" : "minutes", sent));
						Print("TatankaChartFeed: sent " + sent + " " + (ticks ? "trades" : "1-minute bars") + " of " + sym + " history");
					}
					return;
				}
			}
			catch (Exception ex)
			{
				EnqueueHistory(gen, "Z|" + sym + "|error|0|" + ex.Message);
			}
		}

		// The contract charted at each time of [fromMs, toMs], oldest first, each with the time it starts: NinjaTrader's
		// front month by its roll dates (each from the 17:00 CT open of its roll date's session), never older than the
		// one the server charted last (null: unknown) nor newer than the one forwarded now. older: none for a time before
		// the first roll date it knows (an empty list when all of it is), rather than a guess.
		private static List<KeyValuePair<Instrument, long>> Segments(Instrument charted, Instrument current, long fromMs, long toMs, bool older = false)
		{
			int cur = Months(current), oldest = charted != null ? Months(charted) : int.MinValue;
			List<KeyValuePair<int, long>> sched = new List<KeyValuePair<int, long>>();   // contract month, its start
			foreach (Rollover r in current.MasterInstrument.RolloverCollection)
			{
				DateTime eve = r.Date.Date.AddDays(-1);
				long start = new DateTimeOffset(TimeZoneInfo.ConvertTimeToUtc(
					new DateTime(eve.Year, eve.Month, eve.Day, 17, 0, 0, DateTimeKind.Unspecified), Central)).ToUnixTimeMilliseconds();
				sched.Add(new KeyValuePair<int, long>(r.ContractMonth.Year * 12 + r.ContractMonth.Month, start));
			}
			sched.Sort((x, y) => x.Value.CompareTo(y.Value));
			List<long> points = new List<long> { fromMs };
			foreach (KeyValuePair<int, long> e in sched)
				if (e.Value > fromMs && e.Value < toMs) points.Add(e.Value);
			List<KeyValuePair<Instrument, long>> segs = new List<KeyValuePair<Instrument, long>>();
			int had = int.MinValue;
			foreach (long t in points)
			{
				int m = int.MinValue;
				foreach (KeyValuePair<int, long> e in sched)
					if (e.Value <= t) m = e.Key;
				if (m == int.MinValue && older) continue;   // before any roll it knows: no contract to ask
				if (m == int.MinValue) m = oldest != int.MinValue ? oldest : cur;   // before any roll it knows
				m = Math.Min(Math.Max(m, oldest), cur);
				if (m == had) continue;
				Instrument i = m == cur ? current : m == oldest ? charted
					: Instrument.GetInstrument(current.MasterInstrument.Name + " " + (((m - 1) % 12) + 1).ToString("00", Inv) + "-" + (((m - 1) / 12) % 100).ToString("00", Inv));
				if (i == null) continue;   // a month NinjaTrader doesn't know: the one before goes on
				segs.Add(new KeyValuePair<Instrument, long>(i, t));
				had = m;
			}
			if (segs.Count == 0 && !older) segs.Add(new KeyValuePair<Instrument, long>(current, fromMs));
			return segs;
		}

		// [a, b] of one contract from NinjaTrader, its lines queued as they come (waiting for room). The count sent; -1 when
		// its connection closed; `error` after the retries (30 s apart, while NinjaTrader's connection comes up) failed.
		private int Fetch(string sym, Instrument instrument, long a, long b, bool ticks, int gen, out string error)
		{
			error = null;
			for (int attempt = 0; ; attempt++)
			{
				if (!WaitDataUp(gen, sym)) return -1;
				TimeZoneInfo zone = NtZone();
				DateTime from = TimeZoneInfo.ConvertTimeFromUtc(DateTimeOffset.FromUnixTimeMilliseconds(a).UtcDateTime, zone);
				DateTime to = TimeZoneInfo.ConvertTimeFromUtc(DateTimeOffset.FromUnixTimeMilliseconds(b).UtcDateTime, zone);
				BarsRequest req = new BarsRequest(instrument, from, to);
				req.BarsPeriod = new BarsPeriod { BarsPeriodType = ticks ? BarsPeriodType.Tick : BarsPeriodType.Minute, Value = 1 };
				req.TradingHours = instrument.MasterInstrument.TradingHours;
				HistoryPolicies(req);
				lock (requests) requests.Add(req);
				Print("TatankaChartFeed: filling " + instrument.FullName + " " + from.ToString("MM-dd HH:mm", Inv) + " to "
					+ to.ToString("MM-dd HH:mm", Inv) + " (" + zone.Id + ") from NinjaTrader's " + (ticks ? "trades" : "1-minute bars"));
				try
				{
					ManualResetEventSlim answered = new ManualResetEventSlim(false);
					BarsRequest got = null;
					ErrorCode code = ErrorCode.NoError;
					string message = "";
					req.Request((bars, e, m) => { got = bars; code = e; message = m; answered.Set(); });
					bool came = answered.Wait(TimeSpan.FromMinutes(5));
					if (gen != connection) return -1;
					if (came && code == ErrorCode.NoError && got != null)
					{
						int sent = 0;
						for (int i = 0; i < got.Bars.Count; i++)
						{
							long t = ToUtcMs(got.Bars.GetTime(i), zone), v = Convert.ToInt64(got.Bars.GetVolume(i));
							if (ticks)
							{
								if (t <= a || t >= b || v <= 0) continue;
								if (!RoomFor(gen)) return -1;
								EnqueueHistory(gen, string.Format(Inv, "X|{0}|{1}|{2}|{3}", sym, t, got.Bars.GetClose(i), v));
							}
							else
							{
								long open = t - 60000;   // NinjaTrader stamps a minute bar at its close
								if (open < a - 60000 || t > b) continue;
								if (!RoomFor(gen)) return -1;
								EnqueueHistory(gen, string.Format(Inv, "Y|{0}|{1}|{2}|{3}|{4}|{5}|{6}", sym, open, got.Bars.GetOpen(i),
									got.Bars.GetHigh(i), got.Bars.GetLow(i), got.Bars.GetClose(i), v));
							}
							sent++;
						}
						return sent;
					}
					if (attempt >= HistoryRetries)
					{
						error = came ? code + " " + message : "no answer from NinjaTrader";
						return 0;
					}
				}
				finally
				{
					lock (requests) requests.Remove(req);
					req.Dispose();
				}
				Thread.Sleep(30000);
			}
		}

		private bool RoomFor(int gen)
		{
			while (queue.Count >= MaxQueue / 2 && gen == connection) Thread.Sleep(10);
			return gen == connection;
		}

		// Your fills and positions, read-only: every account NinjaTrader has (accounts that connect later are picked up
		// within a minute), with the fills it already has for this session and its open positions.
		private void WatchAccounts()
		{
			List<Account> added = new List<Account>();
			lock (Account.All)
				lock (watchedAccounts)
					foreach (Account a in Account.All)
						if (!watchedAccounts.Contains(a) && !IsReplayAccount(a))
						{
							watchedAccounts.Add(a);
							a.ExecutionUpdate += OnExecutionUpdate;
							a.PositionUpdate += OnPositionUpdate;
							added.Add(a);
						}
			foreach (Account a in added)
			{
				SendFills(a);
				SendPositions(a);
			}
		}

		// Market Replay (Playback101) and Strategy Analyzer (Backtest) fills are not trades: they stay out of the journal.
		private static bool IsReplayAccount(Account a)
		{
			string n = a.Name ?? "";
			return n.StartsWith("Playback", StringComparison.OrdinalIgnoreCase) || n.StartsWith("Backtest", StringComparison.OrdinalIgnoreCase);
		}

		private void SendFills(Account a)
		{
			lock (a.Executions)
				foreach (Execution x in a.Executions) EnqueueFill(x);
		}

		private void OnExecutionUpdate(object sender, ExecutionEventArgs e)
		{
			long now = NowMs(), at = EnqueueFill(e.Execution);
			if (e.Execution == null) return;
			Account a = e.Execution.Account ?? sender as Account;
			// A live fill (not the session's again, from SendFills) times the account's fills' clock. One over a minute
			// off is not live (an execution NinjaTrader restored) and is left out.
			if (a != null && at != 0 && Math.Abs(at - now) <= 60000) fillOffset[Clean(a.Name)] = at - now;
			MarkChanged(a, e.Execution.Instrument);
		}

		private void OnPositionUpdate(object sender, PositionEventArgs e)
		{
			if (e.Position != null) MarkChanged(e.Position.Account ?? sender as Account, e.Position.Instrument);
		}

		private static string Key(string account, Instrument i)
		{
			return Clean(account) + "|" + Clean(i.FullName);
		}

		// Each change (a new entry, not the one SendChanged may be reading) puts the P line off to a second after it, so
		// both events of each fill are in by then, in either order.
		private void MarkChanged(Account a, Instrument i)
		{
			if (a == null || i == null) return;
			DateTime now = DateTime.UtcNow;
			changed.AddOrUpdate(Key(a.Name, i), k => new Changed { Account = a, Instrument = i, First = now, At = now },
				(k, was) => new Changed { Account = a, Instrument = i, First = was.First, At = now });
		}

		// Now on the account's fills' clock, which a P line carries: after every fill it counts and before any to come,
		// however far off the PC's clock is. A position's own P line (key: its account and contract) is never before
		// that contract's last line or the account's last connect; a connect's (key null), never before the account's
		// last line. From its last live fill, less 2.25 s, the longest a position waits to be read (a second with no
		// change, then the send loop's next round): a P line read after a fill comes out at that fill's millisecond
		// (its contract's last line), so a fill made before the read but reported after it sorts after the P line,
		// however much slower its report was. Not at the account's last line: a fill of another contract in between
		// would put it after the fill it does not count. Early is safe: it is never before its last line, and the
		// server keeps one P line per account, contract and millisecond, so the next one clamped to the same
		// millisecond replaces it (which also puts right one that came out late, after a fill it did not count). Until
		// the account's first live fill, the PC's clock or the market data's, whichever is earlier (until NinjaTrader's
		// first live trade, the PC's).
		private long FillClock(Account a, string key)
		{
			long now = NowMs(), off, last;
			long at = fillOffset.TryGetValue(Clean(a.Name), out off) ? now + off - 2250 : now + Math.Min(0, Interlocked.Read(ref clockOffset));
			if (key == null) return sentAt.TryGetValue(Clean(a.Name), out last) ? Math.Max(at, last) : at;
			if (sentAtC.TryGetValue(key, out last)) at = Math.Max(at, last);
			return snapAt.TryGetValue(Clean(a.Name), out last) ? Math.Max(at, last) : at;
		}

		// Once a second, from the send loop: a P line for each position whose last change was a second ago or more (by
		// then the fills that changed it are in), or whose first was 5 s ago (changes that keep coming) and its last
		// 0.2 s ago (both events of a fill are in), or whose first was 10 s ago (changes that never stop), as NinjaTrader
		// has it now.
		private void SendChanged()
		{
			DateTime now = DateTime.UtcNow, due = now.AddSeconds(-1), cap = now.AddSeconds(-5), quiet = now.AddMilliseconds(-200),
				stop = now.AddSeconds(-10);
			foreach (KeyValuePair<string, Changed> kv in changed)
			{
				Changed c = kv.Value;   // removed only if it is still this one: changed again meanwhile, it waits
				if ((c.At > due && (c.First > cap || c.At > quiet) && c.First > stop) ||
					!((ICollection<KeyValuePair<string, Changed>>)changed).Remove(kv)) continue;
				long at = FillClock(c.Account, kv.Key);
				Enqueue(PositionLine(c.Account, c.Instrument, at));
				Sent(c.Account.Name, kv.Key, at);
			}
		}

		// On each connect, and for an account first seen: every open position, then P|<account>|<ms> (the others are
		// flat), all at one time.
		private void SendPositions(Account a)
		{
			long at = FillClock(a, null);
			List<Instrument> open = new List<Instrument>();
			lock (a.Positions)
				foreach (Position p in a.Positions)
					if (p.Instrument != null && p.MarketPosition != MarketPosition.Flat && p.Quantity > 0) open.Add(p.Instrument);
			foreach (Instrument i in open) Enqueue(PositionLine(a, i, at));
			Enqueue(string.Format(Inv, "P|{0}|{1}", Clean(a.Name), at));
			Sent(a.Name, null, at);
			snapAt.AddOrUpdate(Clean(a.Name), at, (k, was) => Math.Max(was, at));
		}

		// A line's time, for its account and its contract (key, if one).
		private void Sent(string account, string key, long at)
		{
			sentAt.AddOrUpdate(Clean(account), at, (k, was) => Math.Max(was, at));
			if (key != null) sentAtC.AddOrUpdate(key, at, (k, was) => Math.Max(was, at));
		}

		private static string PositionLine(Account a, Instrument i, long at)
		{
			int qty = 0;
			double avg = 0;
			lock (a.Positions)
				foreach (Position p in a.Positions)
					if (p.Instrument != null && p.Instrument.FullName == i.FullName && p.MarketPosition != MarketPosition.Flat)
					{
						qty = p.MarketPosition == MarketPosition.Long ? p.Quantity : -p.Quantity;
						avg = p.AveragePrice;
					}
			MasterInstrument m = i.MasterInstrument;
			return string.Format(Inv, "P|{0}|{1}|{2}|{3}|{4}|{5}|{6}", Clean(a.Name), Clean(m.Name), Clean(i.FullName), at, qty,
				avg, m.PointValue);
		}

		private static string Clean(string s)
		{
			return (s ?? "").Replace('|', '/').Replace('\r', ' ').Replace('\n', ' ');
		}

		// Its time (UTC ms), 0 when there is no fill to send.
		private long EnqueueFill(Execution x)
		{
			if (x == null || x.Instrument == null || x.Quantity <= 0) return 0;
			MasterInstrument m = x.Instrument.MasterInstrument;
			string account = x.Account != null ? x.Account.Name : "?";
			long at = ToUtcMs(x.Time, NtZone());
			Sent(account, Key(account, x.Instrument), at);
			Enqueue(string.Format(Inv, "E|{0}|{1}|{2}|{3}|{4}|{5}|{6}|{7}|{8}|{9}|{10}|{11}",
				Clean(account), Clean(m.Name), Clean(x.Instrument.FullName), at,
				x.Price, x.Quantity, x.MarketPosition == MarketPosition.Long ? 1 : -1, m.PointValue, x.Commission,
				Clean(x.ExecutionId), Clean(x.OrderId), Clean(x.Name)));
			return at;
		}

		// Runs on NinjaTrader's instrument thread: only format and enqueue, never touch the network here.
		private void OnMarketData(object source, MarketDataEventArgs e)
		{
			string sym = e.Instrument.MasterInstrument.Name;
			heardAt[sym] = NowMs();   // its data still comes: its Q lines go on
			if (e.MarketDataType == MarketDataType.Last)
			{
				if (e.Volume <= 0 || held.ContainsKey(sym) || !IsLiveData()) return;   // held: maybe the wrong contract
				if (ReferenceEquals(Interlocked.Exchange(ref lastTrade, e), e)) return;   // the very same event again
				// NinjaTrader replays the last trade when it subscribes or reconnects: in the 16:00 break (it became the next
				// session's first print, at the last one's close), over a weekend. It keeps its own time: one older than
				// ReplayMs is left out, unless more than ReplayRun come in a row, each within ReplayRunMs of the one before
				// (the PC's clock is off, and every trade looks old: from then on they go). Replays, one a reconnect, come
				// minutes or hours apart, so they never add up to a run.
				long now = NowMs(), at = ToUtcMs(e.Time, NtZone()), prev;
				if (now - at > ReplayMs)
				{
					bool near = replayAt.TryGetValue(sym, out prev) && now - prev <= ReplayRunMs;
					replayAt[sym] = now;
					if (replays.AddOrUpdate(sym, 1, (k, n) => near || n > ReplayRun ? Math.Min(n, ReplayRun) + 1 : 1) <= ReplayRun) return;
				}
				else replays[sym] = 0;
				double price = e.Instrument.MasterInstrument.RoundToTickSize(e.Price);
				VolumeCheck vc = volumeChecks.GetOrAdd(sym, k => new VolumeCheck());
				lock (vc) vc.Sent += (long)e.Volume;
				int side = (e.Ask > 0 && price >= e.Ask) ? 1 : (e.Bid > 0 && price <= e.Bid) ? -1 : 0;
				lastPrice[sym] = price;
				Enqueue(string.Format(Inv, "T|{0}|{1}|{2}|{3}|{4}", sym, now, price, e.Volume, side));
				if (now >= Interlocked.Read(ref nextClock))   // against the PC's: P lines' clock until an account's first fill
				{
					Interlocked.Exchange(ref nextClock, now + 60000);
					Interlocked.Exchange(ref clockOffset, at - now);
				}
				if (!firstLive.ContainsKey(sym)) firstLive.TryAdd(sym, at / 1000 * 1000);
			}
			else if (e.MarketDataType == MarketDataType.LastClose && e.Price > 0)
			{
				priorClose[sym] = e.Price;
			}
			else if (e.MarketDataType == MarketDataType.Settlement) Settled(sym, e.Price);
			else if (e.MarketDataType == MarketDataType.DailyVolume)
			{
				dayVolume[sym] = e.Volume;
				CheckVolume(sym, (long)e.Volume);
			}
		}

		// Trades sent against NinjaTrader's own count of the day's contracts, from the first count this add-on saw: once
		// 5,000 contracts in, NinjaScript Output says whether they agree (each trade sent once) and warns if not.
		private void CheckVolume(string sym, long daily)
		{
			if (daily <= 0 || !IsLiveData()) return;
			VolumeCheck vc = volumeChecks.GetOrAdd(sym, k => new VolumeCheck());
			lock (vc)
			{
				if (vc.Base < 0 || daily < vc.Base) { vc.Base = daily; vc.Sent = 0; return; }   // first count, or a new day
				long counted = daily - vc.Base;
				if (counted < 5000) return;
				double ratio = vc.Sent / (double)counted;
				bool ok = ratio > 0.8 && ratio < 1.25;
				if (ok && !vc.Reported)
				{
					vc.Reported = true;
					Print(string.Format(Inv, "TatankaChartFeed: {0} volume check OK: sent {1:N0}, NinjaTrader counted {2:N0}", sym, vc.Sent, counted));
				}
				else if (!ok && DateTime.UtcNow >= vc.NextWarning)
				{
					vc.NextWarning = DateTime.UtcNow.AddMinutes(30);
					Print(string.Format(Inv, "TatankaChartFeed: {0} sent {1:N0} contracts while NinjaTrader counted {2:N0} ({3:0.00}x): trades are not reaching the server one for one", sym, vc.Sent, counted, ratio));
				}
				if (counted >= 500000) { vc.Base = daily; vc.Sent = 0; }   // a fresh window now and then
			}
		}

		// A watchlist future's quote (instrument thread): remember the numbers; the send loop writes Q lines.
		private void OnQuoteData(object source, MarketDataEventArgs e)
		{
			string sym = e.Instrument.MasterInstrument.Name;
			heardAt[sym] = NowMs();
			if (e.MarketDataType == MarketDataType.Last && e.Price > 0)
			{
				if (IsLiveData()) lastPrice[sym] = e.Price;
			}
			else if (e.MarketDataType == MarketDataType.LastClose && e.Price > 0) priorClose[sym] = e.Price;
			else if (e.MarketDataType == MarketDataType.DailyVolume) dayVolume[sym] = e.Volume;
			else if (e.MarketDataType == MarketDataType.Settlement) Settled(sym, e.Price);
		}

		// A settlement from NinjaTrader's data: sent when it changes (the exchange published the day's).
		private void Settled(string sym, double price)
		{
			double was;
			if (price <= 0 || (settlement.TryGetValue(sym, out was) && was == price)) return;
			settlement[sym] = price;
			Enqueue(string.Format(Inv, "S|{0}|{1}", sym, price));
		}

		// Market Replay (Playback) and the Simulated Data Feed must never reach the live charts or alerts. The
		// provider is compared by name so this compiles whatever NinjaTrader calls its other providers.
		private static bool IsLiveData()
		{
			Connection playback = Connection.PlaybackConnection;
			if (playback != null && playback.Status == ConnectionStatus.Connected) return false;
			lock (Connection.Connections)
			{
				foreach (Connection c in Connection.Connections)
					if (c.Status == ConnectionStatus.Connected && c.Options != null && c.Options.Provider.ToString() == "Simulator")
						return false;
			}
			return true;
		}

		private void Enqueue(string line)
		{
			if (queue == null) return;
			try
			{
				while (!queue.TryAdd(line))
				{
					string old;
					if (queue.TryTake(out old))   // full: drop oldest, keep newest
					{
						Interlocked.Increment(ref dropped);
						firstLive.Clear();   // a first trade may be the one dropped: taken again from one still queued
					}
				}
			}
			catch (InvalidOperationException) { } // adding completed: shutting down
		}

		private static long NowMs() { return DateTimeOffset.UtcNow.ToUnixTimeMilliseconds(); }

		// The watchlist's Q lines (the send loop's, once a second): each symbol's last price, only while its market data
		// comes and NinjaTrader's live data is connected. A price that stopped (the data connection lost, Market Replay
		// or the Simulated Data Feed connected, the market shut for the weekend) is not sent again as a fresh one: the
		// server's next source (IBKR, Yahoo) takes over and says how current it is.
		private void SendQuotes(StreamWriter w)
		{
			if (!DataConnected() || !IsLiveData()) return;
			long now = NowMs();
			foreach (KeyValuePair<string, double> kv in lastPrice)
			{
				double prev;
				long vol, at;
				if (!priorClose.TryGetValue(kv.Key, out prev) || !heardAt.TryGetValue(kv.Key, out at) || now - at > QuoteLiveMs) continue;
				if (dayVolume.TryGetValue(kv.Key, out vol))
					w.WriteLine(string.Format(Inv, "Q|{0}|{1}|{2}|{3}", kv.Key, kv.Value, prev, vol));
				else w.WriteLine(string.Format(Inv, "Q|{0}|{1}|{2}", kv.Key, kv.Value, prev));
			}
		}

		private void SendLoop()
		{
			CancellationToken ct = cts.Token;
			while (!ct.IsCancellationRequested)
			{
				if (turnedAway)   // what this one queued meanwhile the other add-on sent; once let in, the gap fill has the rest
				{
					string drop;
					while (queue.TryTake(out drop)) { }
					firstLive.Clear();
					turnedAway = false;
				}
				try
				{
					using (TcpClient client = new TcpClient())
					{
						client.NoDelay = true;
						client.Connect(Host, Port);
						using (StreamWriter w = new StreamWriter(client.GetStream(), new UTF8Encoding(false)))
						{
							w.NewLine = "\n";
							w.WriteLine("HELLO|" + Token + "|" + Version);
							heardR = false;
							lock (feeds)
								foreach (Feed f in feeds)
									if (f.Current != null && !f.QuoteOnly && !held.ContainsKey(f.Master.Name))
										w.WriteLine("C|" + f.Master.Name + "|" + f.Current.FullName);
							w.Flush();
							DateTime releaseAt = DateTime.UtcNow.AddSeconds(10);   // a server that sends no R lines
							foreach (KeyValuePair<string, double> kv in settlement)   // the settlements known, again
								w.WriteLine(string.Format(Inv, "S|{0}|{1}", kv.Key, kv.Value));
							SendChanged();   // changes from while it was away first: a connect's positions come after them
							lock (watchedAccounts)   // fills made while the server was away come again (it keeps each once), then
								foreach (Account a in watchedAccounts)   // the positions as they are now
								{
									SendFills(a);
									SendPositions(a);
								}
							Print("TatankaChartFeed: connected to " + Host + ":" + Port);
							NetworkStream stream = client.GetStream();
							int gen = ++connection;
							try
							{
								new Thread(() => ReadLoop(stream, gen)) { IsBackground = true, Name = "TatankaChartFeed reader" }.Start();
								DateTime nextBeat = DateTime.MinValue, nextQuotes = DateTime.MinValue;
								while (!ct.IsCancellationRequested && !turnedAway)
								{
									string line;
									if (queue.TryTake(out line, 250, ct))
									{
										Send(w, line, gen);
										while (queue.TryTake(out line)) Send(w, line, gen);
									}
									DateTime now = DateTime.UtcNow;
									if (!heardR && !held.IsEmpty && now >= releaseAt)
									{
										lock (feeds)
											foreach (Feed f in feeds) Release(f);
										releaseAt = DateTime.MaxValue;
									}
									if (now >= nextQuotes)
									{
										SendQuotes(w);
										nextQuotes = now.AddSeconds(1);
										SendChanged();
									}
									if (now >= nextBeat)
									{
										w.WriteLine("H|" + NowMs().ToString(Inv));
										nextBeat = now.AddSeconds(5);
										long d = Interlocked.Exchange(ref dropped, 0);
										if (d > 0) Print("TatankaChartFeed: dropped " + d + " lines while the server was unreachable");
									}
									w.Flush();
								}
							}
							finally
							{
								connection++;   // what is still asked on this connection is dropped; the server asks again
								firstLive.Clear();
							}
						}
					}
				}
				catch (OperationCanceledException) { break; }
				catch (Exception ex)
				{
					Print("TatankaChartFeed: " + ex.Message + " (retrying in 5 s)");
				}
				ct.WaitHandle.WaitOne(turnedAway ? 30000 : 5000);
			}
		}
	}
}
