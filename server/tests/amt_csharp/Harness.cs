// Replays a recorded input file through the original C# engine and prints every frame canonically.
using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Linq;
using System.Text;
using TatankaTrading.AmtToolkit;

public static class Harness
{
    static readonly CultureInfo Inv = CultureInfo.InvariantCulture;
    static string D(double x) { return double.IsNaN(x) ? "NaN" : x.ToString("G17", Inv); }
    static string T(DateTime t) { return t == DateTime.MinValue ? "MIN" : t.ToString("yyyy-MM-ddTHH:mm:ss", Inv); }
    static DateTime PT(string s) { return DateTime.ParseExact(s, "yyyy-MM-ddTHH:mm:ss", Inv); }
    static double PD(string s) { return double.Parse(s, Inv); }
    static Bar PB(string[] p, int o) { return new Bar { Start = PT(p[o]), End = PT(p[o + 1]), Open = PD(p[o + 2]), High = PD(p[o + 3]), Low = PD(p[o + 4]), Close = PD(p[o + 5]), Volume = PD(p[o + 6]) }; }
    public static void Main(string[] args)
    {
        var lines = File.ReadAllLines(args[0]);
        var outp = new StringBuilder();
        Settings s = new Settings();
        if (args.Length > 2) foreach (var kv in File.ReadAllLines(args[2])) { var p = kv.Split('='); var fld = typeof(Settings).GetField(p[0]); fld.SetValue(s, Convert.ChangeType(p[1], fld.FieldType, Inv)); }
        Engine e = null; DailyHistory daily = new DailyHistory(s.daily_atr_length);
        var tracker = new LevelAlertTracker(); Frame last = null;
        for (int i = 0; i < lines.Length; i++)
        {
            string[] p = lines[i].Split('|');
            switch (p[0])
            {
                case "E": e = new Engine(s, p[1], PD(p[2]), PD(p[3]), int.Parse(p[4])); break;
                case "L":
                    var lv = new List<Level>();
                    if (p.Length > 1 && p[1].Length > 0) foreach (var x in p[1].Split(';')) { var q = x.Split(','); lv.Add(new Level { Name = q[0], Price = PD(q[1]), Kind = int.Parse(q[2]) }); }
                    e.ReplaceLevels(lv); break;
                case "D": daily.Add(PT(p[1]), PD(p[2]), PD(p[3]), PD(p[4])); break;
                case "M": e.AddMacro(PB(p, 1)); break;
                case "O":
                    foreach (Notice n in tracker.Observe(last.Watches, PD(p[1]), p[2], e.Root, e.Tick)) outp.Append("O|").Append(n.Key).Append('|').Append(n.Text).Append('\n');
                    break;
                case "B":
                    Bar b = PB(p, 2); b.Index = int.Parse(p[1]); int ns = int.Parse(p[9]); bool clone = p[10] == "1";
                    var subs = new List<Bar>(); for (int k = 0; k < ns; k++) subs.Add(PB(lines[++i].Split('|'), 1));
                    Engine target = clone ? e.Clone() : e;
                    Frame f = target.Process(b, subs, daily.Before(MathEx.TradingDate(b.Start)));
                    last = f; Dump(outp, f);
                    break;
            }
        }
        File.WriteAllText(args[1], outp.ToString());
    }
    static void Dump(StringBuilder o, Frame f)
    {
        o.Append("F|").Append(f.Bar.Index).Append('\n');
        foreach (var n in f.Notices) o.Append("N|").Append(n.Key).Append('|').Append(n.Text).Append('\n');
        foreach (var w in f.Watches) o.Append("W|").Append(w.Name).Append('|').Append(D(w.Price)).Append('|').Append(D(w.HalfWidth)).Append('|').Append(w.Enabled ? 1 : 0).Append('|').Append(w.Moving ? 1 : 0).Append('\n');
        foreach (var l in f.Lines) o.Append("LN|").Append(l.Name).Append('|').Append(l.Group).Append('|').Append(D(l.Price)).Append('|').Append(T(l.Start)).Append('|').Append(T(l.End)).Append('|').Append(l.Color).Append('|').Append(l.TextColor).Append('|').Append(l.Width).Append('|').Append(l.Style).Append('|').Append(l.Offset).Append('|').Append(l.Size).Append('|').Append(l.Label ? 1 : 0).Append('|').Append(l.Extend).Append('\n');
        foreach (var z in f.Zones) o.Append("Z|").Append(D(z.Top)).Append('|').Append(D(z.Bottom)).Append('|').Append(T(z.Start)).Append('|').Append(T(z.Created)).Append('|').Append(z.Color).Append('|').Append(z.Live ? 1 : 0).Append('|').Append(z.BorderColor).Append('|').Append(z.BorderWidth).Append('|').Append(z.BorderStyle).Append('\n');
        foreach (var p in f.Profiles)
        {
            var keys = p.Profile.Rows.Keys.OrderBy(k => k).ToArray(); double sum = 0, wsum = 0;
            foreach (var k in keys) { sum += p.Profile.Rows[k]; wsum += k * p.Profile.Rows[k]; }
            o.Append("P|").Append(T(p.Start)).Append('|').Append(T(p.End)).Append('|').Append(p.EndIndex).Append('|').Append(D(p.Profile.Step)).Append('|').Append(D(p.Stats.Poc)).Append('|').Append(D(p.Stats.Vah)).Append('|').Append(D(p.Stats.Val)).Append('|').Append(D(p.Stats.Total)).Append('|').Append(keys.Length).Append('|').Append(keys.Length > 0 ? keys[0] : 0).Append('|').Append(D(sum)).Append('|').Append(D(wsum)).Append('\n');
        }
        if (f.Labels != null) o.Append("DB|").Append(string.Join(";", f.Labels)).Append('|').Append(string.Join(";", f.Values)).Append('|').Append(string.Join(";", f.Colors.Select(c => c.ToString()))).Append('\n');
        o.Append("X|").Append(D(f.RthVwap)).Append('|').Append(D(f.DailyAtr)).Append('|').Append(f.RuleActive ? 1 : 0).Append('|').Append(f.RuleConfirmed ? 1 : 0).Append('|').Append(D(f.VaHigh)).Append('|').Append(D(f.VaLow)).Append('|').Append(f.Warning).Append('\n');
    }
}
