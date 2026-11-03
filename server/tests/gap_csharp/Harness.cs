// Parity harness for chartsrv/gaps.py: replays a recorded input file through the original C# GapEngine
// (pasted from TatankaGapAnalysis.cs between ENGINE_BEGIN and ENGINE_END by build.sh) and prints every result.
using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Text;
using NinjaTrader.NinjaScript.Indicators.TatankaGapInternal;

public static class Harness
{
    static readonly CultureInfo Inv = CultureInfo.InvariantCulture;
    static string D(double x) { return double.IsNaN(x) ? "NaN" : x.ToString("G17", Inv); }
    static DateTime PT(string s) { return DateTime.ParseExact(s, "yyyy-MM-ddTHH:mm:ss", Inv); }
    static double PD(string s) { return double.Parse(s, Inv); }

    static void G(StringBuilder o, string tag, GapRecord g)
    {
        o.Append(tag).Append('|').Append(g.Ordinal).Append('|').Append(g.StartBar).Append('|').Append(g.NextGapBar)
         .Append('|').Append(g.FillBar).Append('|').Append(g.Direction).Append('|').Append(D(g.OpenPrice)).Append('|')
         .Append(D(g.SettlePrice)).Append('|').Append(D(g.FillEdge)).Append('|').Append(D(g.Size)).Append('|')
         .Append(g.Filled ? 1 : 0).Append('|').Append(g.BarsToFill).Append('|').Append(D(g.FillPercent)).Append('\n');
    }

    public static void Main(string[] args)
    {
        GapEngine e = null;
        var o = new StringBuilder();
        foreach (string line in File.ReadAllLines(args[0]))
        {
            string[] p = line.Split('|');
            switch (p[0])
            {
                case "S":
                    TimeSpan a, b;
                    bool okA = GapEngine.TryParseTime(int.Parse(p[1]), out a), okB = GapEngine.TryParseTime(int.Parse(p[2]), out b);
                    o.Append("S|").Append(okA ? 1 : 0).Append('|').Append(okB ? 1 : 0).Append('\n');
                    e = new GapEngine(a, b, PD(p[3]));
                    break;
                case "K":
                    DateTime k = e.SessionKey(PT(p[1]));
                    o.Append("K|").Append(k == DateTime.MinValue ? "MIN" : k.ToString("yyyy-MM-dd", Inv)).Append('\n');
                    break;
                case "U":
                    GapUpdate u = e.Update(int.Parse(p[1]), PT(p[2]), PD(p[3]), PD(p[4]), PD(p[5]), PD(p[6]));
                    if (u.NewGap != null) G(o, "N", u.NewGap);
                    if (u.FilledGaps != null) foreach (GapRecord g in u.FilledGaps) G(o, "X", g);
                    break;
                case "V":
                    GapFrame f = e.Snapshot(int.Parse(p[1]), int.Parse(p[2]), p[3] == "1", p[4] == "1", int.Parse(p[5]));
                    o.Append("F|").Append(f.StoredCount).Append('|').Append(f.CurrentBar).Append('|').Append(f.Consecutive)
                     .Append('|').Append(f.VisibleGaps.Count).Append('\n');
                    if (f.Latest != null) G(o, "L", f.Latest);
                    foreach (GapRecord g in f.VisibleGaps) G(o, "G", g);
                    break;
                case "E":
                    foreach (GapRecord g in e.History) G(o, "H", g);
                    break;
            }
        }
        File.WriteAllText(args[1], o.ToString());
    }
}
