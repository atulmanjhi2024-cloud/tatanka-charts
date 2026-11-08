// Drives the add-on's Segments for tests/test_deep_history.py: which contract each part of a stretch is asked from. ES
// rolls (NinjaTrader's table) on 2026-03-12 to 06-26, 2026-06-11 to 09-26, 2026-09-10 to 12-26; it is on 12-26 now
// and charted last on 09-26. Prints one line per case: "<case> <contract>@<utc ms> ...".
using System;
using System.Collections.Generic;
using System.Reflection;
using NinjaTrader.Cbi;
using NinjaTrader.NinjaScript.AddOns;

public static class SegmentsHarness
{
	static Instrument Contract(MasterInstrument mi, string month, int y, int m)
	{
		Instrument i = new Instrument { FullName = "ES " + month, MasterInstrument = mi, Expiry = new DateTime(y, m, 19) };
		Instrument.Known["ES " + month] = i;
		return i;
	}

	static long Ms(int y, int mo, int d) { return new DateTimeOffset(y, mo, d, 0, 0, 0, TimeSpan.Zero).ToUnixTimeMilliseconds(); }

	public static void Main()
	{
		MasterInstrument mi = new MasterInstrument { Name = "ES" };
		foreach (int[] r in new[] { new[] { 2026, 3, 12, 2026, 6 }, new[] { 2026, 6, 11, 2026, 9 }, new[] { 2026, 9, 10, 2026, 12 } })
			mi.RolloverCollection.Add(new Rollover { Date = new DateTime(r[0], r[1], r[2]), ContractMonth = new DateTime(r[3], r[4], 1) });
		Contract(mi, "03-26", 2026, 3);
		Contract(mi, "06-26", 2026, 6);
		Instrument sep = Contract(mi, "09-26", 2026, 9), dec = Contract(mi, "12-26", 2026, 12);
		MethodInfo seg = typeof(TatankaChartFeed).GetMethod("Segments", BindingFlags.NonPublic | BindingFlags.Static);
		var cases = new List<KeyValuePair<string, object[]>>
		{
			new KeyValuePair<string, object[]>("gap", new object[] { sep, dec, Ms(2026, 1, 5), Ms(2026, 11, 1), false }),
			new KeyValuePair<string, object[]>("older", new object[] { null, dec, Ms(2026, 1, 5), Ms(2026, 11, 1), true }),
			new KeyValuePair<string, object[]>("before", new object[] { null, dec, Ms(2025, 6, 1), Ms(2026, 2, 1), true }),
		};
		foreach (var c in cases)
		{
			var got = (List<KeyValuePair<Instrument, long>>)seg.Invoke(null, c.Value);
			Console.Write(c.Key);
			foreach (var s in got) Console.Write(" " + s.Key.FullName + "@" + s.Value);
			Console.WriteLine();
		}
	}
}
