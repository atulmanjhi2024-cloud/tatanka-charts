// Drives the add-on's journal side for tests/test_hunt5_journal.py: one Sim101 account trading ES (and NQ), its fill
// and position events raised as NinjaTrader raises them, and the send loop's once-a-second SendChanged called here, at
// set moments. No server: the add-on never starts its send loop, and its queue is read here. The PC's clock is the
// real one; NinjaTrader's are made from it: market data on the exchange's clock (`skew` ms ahead of the PC's), a
// broker's fills on the exchange's, the simulator's on the PC's.
// Prints each E and P line the add-on queued, after the PC time (ms) it was read from the queue at, and "# <what>
// <PC ms>" marks.
//   mono harness.exe <scenario> [<its numbers>]
using System;
using System.Collections.Concurrent;
using System.Reflection;
using System.Threading;
using NinjaTrader.Cbi;
using NinjaTrader.Data;
using NinjaTrader.NinjaScript.AddOns;

public static class Harness
{
	static TatankaChartFeed addon;
	static BlockingCollection<string> queue;
	static Account acct;
	static Instrument es, nq;
	static Position pos, nqPos;   // ES's, NQ's
	static int n;
	static int skew;   // the exchange's clock less the PC's (ms)

	static object Call(string name, params object[] args)
	{
		return typeof(TatankaChartFeed).GetMethod(name, BindingFlags.NonPublic | BindingFlags.Instance).Invoke(addon, args);
	}

	static long Now() { return DateTimeOffset.UtcNow.ToUnixTimeMilliseconds(); }

	static void Mark(string what) { Console.WriteLine("# " + what + " " + Now()); }

	static void Drain()
	{
		string line;
		while (queue.TryTake(out line))
			if (line.StartsWith("E|") || line.StartsWith("P|")) Console.WriteLine(Now() + " " + line);
	}

	// The send loop's once-a-second call, `wait` ms from now.
	static void Changed(int wait)
	{
		Thread.Sleep(wait);
		Call("SendChanged");
		Drain();
	}

	// The add-on with the account (flat, or holding `held` at `avg`), not yet watched.
	static void Start(int held, double avg)
	{
		MasterInstrument m = new MasterInstrument { Name = "ES", TickSize = 0.25, PointValue = 50 };
		es = new Instrument { FullName = "ES 12-26", MasterInstrument = m, Expiry = new DateTime(2026, 12, 1) };
		MasterInstrument mn = new MasterInstrument { Name = "NQ", TickSize = 0.25, PointValue = 20 };
		nq = new Instrument { FullName = "NQ 12-26", MasterInstrument = mn, Expiry = new DateTime(2026, 12, 1) };
		acct = new Account { Name = "Sim101" };
		pos = new Position { Account = acct, Instrument = es, MarketPosition = held > 0 ? MarketPosition.Long : MarketPosition.Flat,
			Quantity = held, AveragePrice = avg };
		nqPos = new Position { Account = acct, Instrument = nq, MarketPosition = MarketPosition.Flat };
		acct.Positions.Add(pos);
		acct.Positions.Add(nqPos);
		Account.All.Add(acct);
		addon = new TatankaChartFeed();
		queue = new BlockingCollection<string>(new ConcurrentQueue<string>(), 200000);
		typeof(TatankaChartFeed).GetField("queue", BindingFlags.NonPublic | BindingFlags.Instance).SetValue(addon, queue);
	}

	// A first sight of the account (the add-on's start, or the minute timer): its session's fills, then its positions.
	static void Watch()
	{
		Call("WatchAccounts");
		Drain();
	}

	// The send loop's connect to the server: changes from while it was away, the session's fills again, the positions
	// now.
	static void Connect()
	{
		Call("SendChanged");
		Call("SendFills", acct);
		Call("SendPositions", acct);
		Drain();
	}

	// A trade on the market data, stamped on the exchange's clock and seen `latency` ms after it.
	static void Tick(int latency)
	{
		Call("OnMarketData", null, new MarketDataEventArgs { MarketDataType = MarketDataType.Last, Instrument = es, Price = 5000,
			Volume = 1, Ask = 5000, Bid = 4999.75, Time = DateTime.Now.AddMilliseconds(skew - latency) });
		Drain();
	}

	// An order filled (ES's, or the contract of position `p`), at `stamp` on the fills' own clock: NinjaTrader's
	// position counts it. Its two events are raised by the caller (Position, Exec).
	static Execution Fill(int side, int qty, double price, DateTime stamp) { return Fill(pos, side, qty, price, stamp); }

	static Execution Fill(Position p, int side, int qty, double price, DateTime stamp)
	{
		n++;
		int was = p.MarketPosition == MarketPosition.Long ? p.Quantity : p.MarketPosition == MarketPosition.Short ? -p.Quantity : 0;
		int now = was + side * qty;
		if (now == 0) p.AveragePrice = 0;
		else if (was == 0 || Math.Sign(was) == side) p.AveragePrice = (p.AveragePrice * Math.Abs(was) + price * qty) / Math.Abs(now);
		else if (Math.Sign(now) != Math.Sign(was)) p.AveragePrice = price;
		p.Quantity = Math.Abs(now);
		p.MarketPosition = now > 0 ? MarketPosition.Long : now < 0 ? MarketPosition.Short : MarketPosition.Flat;
		Execution x = new Execution { Account = acct, Instrument = p.Instrument, Quantity = qty, Price = price, Time = stamp,
			MarketPosition = side > 0 ? MarketPosition.Long : MarketPosition.Short, ExecutionId = "x" + n, OrderId = "o" + n,
			Name = "Entry", Commission = 2.04 };
		acct.Executions.Add(x);
		return x;
	}

	static void Position() { Position(pos); }

	static void Position(Position p) { acct.Fire(p); Drain(); }

	static void Exec(Execution x) { acct.Fire(x); Drain(); }

	// The simulator's fill: stamped with the PC's clock as it fills, its position update first, its execution 2 ms on.
	static void SimFill(int side, int qty, double price)
	{
		Execution x = Fill(side, qty, price, DateTime.Now);
		Position();
		Thread.Sleep(2);
		Exec(x);
	}

	// A broker's fill: stamped on the exchange's clock when it filled, its report here `latency` ms later, position
	// update first, execution 2 ms on.
	static void BrokerFill(int side, int qty, double price, int latency) { BrokerFill(pos, side, qty, price, latency); }

	static void BrokerFill(Position p, int side, int qty, double price, int latency)
	{
		Execution x = Fill(p, side, qty, price, DateTime.Now.AddMilliseconds(skew - latency));
		Position(p);
		Thread.Sleep(2);
		Exec(x);
	}

	public static int Main(string[] args)
	{
		switch (args[0])
		{
			case "sim_pc_behind":   // the simulator's fills, the PC 3 s behind the exchange: buy 1, add 1, sell 2
				skew = 3000;
				Start(0, 0);
				Watch();
				Tick(100);
				SimFill(1, 1, 5000);
				Changed(1100);
				Thread.Sleep(400);
				SimFill(1, 1, 5002);
				Changed(1100);
				SimFill(-1, 2, 5010);
				Changed(1100);
				break;
			case "position_first":   // the second buy's position update read before its execution update
				skew = 0;
				Start(0, 0);
				Watch();
				Tick(400);
				SimFill(1, 1, 5000);
				Thread.Sleep(1100);
				Execution x = Fill(1, 1, 5002, DateTime.Now);
				Position();
				Changed(0);
				Thread.Sleep(100);
				Exec(x);
				Changed(1100);
				SimFill(-1, 2, 5010);
				Changed(1100);
				break;
			case "design_note":   // a broker's fills, the PC 5 s ahead of the exchange, position updates first; long 1
				skew = -5000;     // from before, first seen just after a trade and sold 4.5 s later, then a buy just
				Start(1, 4990);   // after that position's read, an add and a sale
				Tick(20);
				Watch();
				Thread.Sleep(4500);
				BrokerFill(-1, 1, 5010, 50);
				Changed(1100);
				BrokerFill(1, 1, 5012, 50);
				Thread.Sleep(500);
				BrokerFill(1, 1, 5014, 50);
				Changed(1100);
				BrokerFill(-1, 2, 5020, 50);
				Changed(1100);
				break;
			case "keeps_changing":   // NinjaTrader's position updates every `every` ms after a buy, `times` of them, with no
				skew = 0;            // change
				{
					int every = int.Parse(args[1]), times = int.Parse(args[2]);
					Start(0, 0);
					Watch();
					Mark("first");
					SimFill(1, 1, 5000);
					for (int i = 0; i < times; i++)
					{
						Changed(every);
						Position();
					}
				}
				break;
			case "cap":   // the simulator's fills every 600 ms, each position update 20 ms before its execution update: the
				skew = 0; // read 5 s after the first change lands between the 9th fill's two updates; then sell 9
				{
					Start(0, 0);
					Watch();
					Tick(20);
					long t0 = Now();
					int k = 0;
					for (; k < 9; k++)
					{
						while (Now() - t0 < (k < 8 ? 600 * k : 5030)) Thread.Sleep(1);
						Execution b = Fill(1, 1, 5000 + k, DateTime.Now);
						Position();
						if (k == 8) Changed(0);
						Thread.Sleep(20);
						Exec(b);
						if (k < 8) Changed(280);
					}
					Changed(1100);
					Changed(1000);
					SimFill(-1, k, 5100);
					Changed(1100);
				}
				break;
			case "jitter":   // a broker's fills, the PC in sync: a buy reported 20 ms after it filled, its position read
				skew = 0;    // `wait` ms on, then an add reported just after that read but filled before it (its report
				{            // `late` ms late), read as long after; then sell 2
					int wait = int.Parse(args[1]), late = int.Parse(args[2]);
					Start(0, 0);
					Watch();
					Tick(20);
					BrokerFill(1, 1, 5000, 20);
					Changed(wait);
					BrokerFill(1, 1, 5002, late);
					Changed(wait);
					BrokerFill(-1, 2, 5010, 20);
					Changed(1100);
				}
				break;
			case "two_contracts":   // a broker's fills, the PC in sync, ES and NQ: buy 1 ES (reported 20 ms after it filled),
				skew = 0;           // buy 1 NQ `nqAt` ms on, the ES position read 1.1 s after its buy, then an ES add filled
				{                   // `add` ms after that buy but reported after the read (at 1.3 s), and another NQ buy
					int add = int.Parse(args[1]), nqAt = int.Parse(args[2]);   // at 1.5 s; then sell 2 ES and 2 NQ
					Start(0, 0);
					Watch();
					Tick(20);
					long t0 = Now();
					BrokerFill(1, 1, 5000, 20);
					while (Now() - t0 < nqAt + 20) Thread.Sleep(1);
					BrokerFill(nqPos, 1, 1, 20000, 20);
					Changed((int)Math.Max(0, 1100 - (Now() - t0)));
					while (Now() - t0 < 1300) Thread.Sleep(1);
					BrokerFill(1, 1, 5002, (int)(Now() - t0) - add);
					while (Now() - t0 < 1520) Thread.Sleep(1);
					BrokerFill(nqPos, 1, 1, 20005, 20);
					Changed(1100);
					BrokerFill(-1, 2, 5010, 20);
					Thread.Sleep(300);
					BrokerFill(nqPos, -1, 2, 20050, 20);
					Changed(1100);
				}
				break;
			case "step":   // a broker's fills, the PC in sync: a buy, then the PC's clock stepped `step` ms forward (a time
				skew = 0;  // sync), a connect to the server, and a sale `after` ms later
				{
					int step = int.Parse(args[1]), after = int.Parse(args[2]);
					Start(0, 0);
					Watch();
					Tick(20);
					BrokerFill(1, 1, 5000, 20);
					Changed(1100);
					Thread.Sleep(3000);   // nothing stamped while the clock steps
					skew = -step;         // the broker's clock now that far behind the PC's
					Connect();
					Thread.Sleep(after);
					BrokerFill(-1, 1, 5010, 20);
					Changed(1100);
				}
				break;
			case "restored":   // a buy, then a sell of the session's from 2 h ago raised again (as at a broker reconnect)
				skew = 0;
				Start(0, 0);
				Fill(1, 1, 4990, DateTime.Now.AddHours(-2));
				Execution sold = Fill(-1, 1, 4995, DateTime.Now.AddHours(-2).AddMinutes(1));   // flat since
				Watch();
				SimFill(1, 1, 5000);
				Exec(sold);
				Changed(4000);
				break;
			default:
				Console.Error.WriteLine("unknown scenario " + args[0]);
				return 2;
		}
		return 0;
	}
}
