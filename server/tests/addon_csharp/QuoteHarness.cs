// Drives the add-on's watchlist quotes for tests/test_hunt13_feeds.py: its real send loop, connected to the test's
// server (the add-on compiled with the test's port), and one watchlist future's market data raised as NinjaTrader
// raises it, 10 times a second, then stopped or turned to Market Replay as the scenario says. Prints "# <what> <PC
// ms>" marks; the test reads the Q lines the server got.
//   mono quotes.exe <scenario> <root>
//     silent     3 s of data, then none for 7 s (the data connection lost, a weekend), then 3 s of data again
//     playback   3 s of data, then Market Replay connected (its data keeps coming) for 4 s
//     dropped    3 s of data, then NinjaTrader's live data connection drops (nothing comes) for 4 s
using System;
using System.Collections.Concurrent;
using System.Reflection;
using System.Threading;
using NinjaTrader.Cbi;
using NinjaTrader.Data;
using NinjaTrader.NinjaScript.AddOns;

public static class QuoteHarness
{
	static TatankaChartFeed addon;
	static Instrument inst;
	static double price = 110.5;

	static object Call(string name, params object[] args)
	{
		return typeof(TatankaChartFeed).GetMethod(name, BindingFlags.NonPublic | BindingFlags.Instance).Invoke(addon, args);
	}

	static void Set(string name, object value)
	{
		typeof(TatankaChartFeed).GetField(name, BindingFlags.NonPublic | BindingFlags.Instance).SetValue(addon, value);
	}

	static void Mark(string what) { Console.WriteLine("# " + what + " " + DateTimeOffset.UtcNow.ToUnixTimeMilliseconds()); }

	static void Raise(MarketDataType type, double p, long volume)
	{
		Call("OnQuoteData", null, new MarketDataEventArgs { MarketDataType = type, Instrument = inst, Price = p, Volume = volume });
	}

	// `ms` of market data: the bid and ask move, now and then a trade.
	static void Data(int ms)
	{
		for (int k = 0; k < ms / 100; k++)
		{
			if (k % 5 == 0)
			{
				price += 1 / 64.0;
				Raise(MarketDataType.Last, price, 1);
				Raise(MarketDataType.DailyVolume, 0, 1000 + k);
			}
			Raise(MarketDataType.Bid, price - 1 / 64.0, 10);
			Raise(MarketDataType.Ask, price, 12);
			Thread.Sleep(100);
		}
	}

	public static int Main(string[] args)
	{
		inst = new Instrument { FullName = args[1] + " 12-26", MasterInstrument = new MasterInstrument { Name = args[1], TickSize = 1 / 64.0 },
			Expiry = new DateTime(2026, 12, 1) };
		Connection live = new Connection { Status = ConnectionStatus.Connected, PriceStatus = ConnectionStatus.Connected };
		Connection.Connections.Add(live);
		addon = new TatankaChartFeed();
		Set("queue", new BlockingCollection<string>(new ConcurrentQueue<string>(), 200000));
		Set("cts", new CancellationTokenSource());
		new Thread(() => Call("SendLoop")) { IsBackground = true }.Start();
		Raise(MarketDataType.LastClose, 110.25, 0);   // the prior session's close, as NinjaTrader gives it on subscribing
		Mark("live");
		Data(3000);
		switch (args[0])
		{
			case "silent":
				Mark("silent");
				Thread.Sleep(7000);
				Mark("again");
				Data(3000);
				break;
			case "playback":
				Mark("playback");
				Connection.PlaybackConnection = new Connection { Status = ConnectionStatus.Connected, PriceStatus = ConnectionStatus.Connected };
				Data(4000);
				break;
			case "dropped":
				Mark("dropped");
				live.PriceStatus = ConnectionStatus.Disconnected;
				Thread.Sleep(4000);
				break;
		}
		Mark("end");
		return 0;
	}
}
