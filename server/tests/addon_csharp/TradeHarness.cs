// Drives the add-on's trades for tests/test_addon_replay.py: its real OnMarketData and send loop, connected to the
// test's server (the add-on compiled with the test's port), with Last events as NinjaTrader raises them.
//   mono trades.exe <scenario> <root>
//     replay   the last trade replayed on subscribing (its own time 40 minutes back), then two live trades
//     skewed   six trades whose own time is 30 minutes behind the PC's clock (a PC clock that is off)
//     live     three live trades
//     reconnects  a live trade, then NinjaTrader's replay of it at 4 reconnects, hours apart (Friday 16:40 to Sunday 16:40)
//     lull     a PC clock 30 minutes off: a burst of four trades, a minute with none, then two more
//     zone     a thousand live trades, then one more after a minute: prints how often NinjaTrader's zone was read
using System;
using System.Collections.Concurrent;
using System.Reflection;
using System.Threading;
using NinjaTrader.Cbi;
using NinjaTrader.Data;
using NinjaTrader.NinjaScript.AddOns;

public static class TradeHarness
{
	static TatankaChartFeed addon;
	static Instrument inst;

	static object Call(string name, params object[] args)
	{
		return typeof(TatankaChartFeed).GetMethod(name, BindingFlags.NonPublic | BindingFlags.Instance).Invoke(addon, args);
	}

	static void Set(string name, object value)
	{
		typeof(TatankaChartFeed).GetField(name, BindingFlags.NonPublic | BindingFlags.Instance).SetValue(addon, value);
	}

	// As if the PC's clock moved on this far since the last trade that looked old (the add-on's own clock is the PC's).
	static void Later(string root, double minutes)
	{
		FieldInfo f = typeof(TatankaChartFeed).GetField("replayAt", BindingFlags.NonPublic | BindingFlags.Instance);
		if (f == null) return;   // an add-on before 19: no such clock
		((ConcurrentDictionary<string, long>)f.GetValue(addon))[root] -= (long)(minutes * 60000);
	}

	static void Trade(double price, double minutesBack)
	{
		Call("OnMarketData", null, new MarketDataEventArgs { MarketDataType = MarketDataType.Last, Instrument = inst, Price = price,
			Volume = 2, Time = DateTime.Now.AddMinutes(-minutesBack) });
		Thread.Sleep(50);
	}

	public static int Main(string[] args)
	{
		inst = new Instrument { FullName = args[1] + " 12-26", MasterInstrument = new MasterInstrument { Name = args[1], TickSize = 0.25 },
			Expiry = new DateTime(2026, 12, 1) };
		Connection.Connections.Add(new Connection { Status = ConnectionStatus.Connected, PriceStatus = ConnectionStatus.Connected });
		addon = new TatankaChartFeed();
		Set("queue", new BlockingCollection<string>(new ConcurrentQueue<string>(), 200000));
		Set("cts", new CancellationTokenSource());
		new Thread(() => Call("SendLoop")) { IsBackground = true }.Start();
		switch (args[0])
		{
			case "replay":
				Trade(6905.25, 40);
				Trade(6910.50, 0);
				Trade(6910.75, 0);
				break;
			case "skewed":
				for (int k = 0; k < 6; k++) Trade(6900 + k * 0.25, 30);
				break;
			case "live":
				for (int k = 0; k < 3; k++) Trade(6920 + k * 0.25, 0);
				break;
			case "reconnects":
				Trade(6930.00, 0);
				Trade(6930.25, 41);
				Later(args[1], 10 * 60 + 20);
				Trade(6930.50, 11 * 60);
				Later(args[1], 18 * 60);
				Trade(6930.75, 29 * 60);
				Later(args[1], 19 * 60 + 41);
				Trade(6931.00, 48 * 60 + 41);
				Trade(6931.25, 0);
				break;
			case "lull":
				for (int k = 0; k < 4; k++) Trade(6940 + k * 0.25, 30);
				Later(args[1], 1);
				Trade(6941.00, 31);
				Trade(6941.25, 31);
				break;
			case "zone":
				var stamp = typeof(TatankaChartFeed).GetField("ntZoneAt", BindingFlags.NonPublic | BindingFlags.Static);
				for (int k = 0; k < 1000; k++)
					Call("OnMarketData", null, new MarketDataEventArgs { MarketDataType = MarketDataType.Last, Instrument = inst,
						Price = 6950 + (k % 2) * 0.25, Volume = 1, Time = DateTime.Now });
				int once = NinjaTrader.Core.Globals.Reads;
				if (stamp != null) stamp.SetValue(null, (long)stamp.GetValue(null) - 60000);   // a minute on (none before 19)
				Trade(6951.00, 0);
				Console.WriteLine(once + " " + NinjaTrader.Core.Globals.Reads);
				break;
		}
		Thread.Sleep(1500);   // the send loop's last lines out
		return 0;
	}
}

// NinjaTrader's Tools > Options > General, which the add-on reads by name (NtZone): its time zone, the PC's here.
namespace NinjaTrader.Core
{
	public class Options { public TimeZoneInfo TimeZoneInfo { get { return TimeZoneInfo.Local; } } }

	public static class Globals
	{
		public static int Reads;   // how often the add-on read them
		static readonly Options options = new Options();
		public static Options GeneralOptions { get { Interlocked.Increment(ref Reads); return options; } }
	}
}
