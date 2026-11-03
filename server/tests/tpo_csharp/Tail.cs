    // ---- harness ----
    static readonly CultureInfo Inv = CultureInfo.InvariantCulture;
    static string D(double x) { return double.IsNaN(x) ? "NaN" : x.ToString("G17", Inv); }
    static string T(DateTime t) { return t == DateTime.MinValue ? "MIN" : t.ToString("yyyy-MM-ddTHH:mm:ss", Inv); }
    static DateTime PT(string s) { return DateTime.ParseExact(s, "yyyy-MM-ddTHH:mm:ss", Inv); }
    static double PD(string s) { return double.Parse(s, Inv); }

    void DumpProfile(StringBuilder o, string tag, SavedProfile p)
    {
        double vah, val;
        GetValueArea(p, out vah, out val);
        o.Append(tag).Append('|').Append(T(p.TimeStart)).Append('|').Append(T(p.TimeEnd)).Append('|').Append(D(p.TickStep))
         .Append('|').Append(D(p.PocPrice)).Append('|').Append(p.MaxCount).Append('|').Append(D(vah)).Append('|').Append(D(val))
         .Append('|').Append(p.Rows.Count).Append('\n');
        foreach (TpoRow r in p.Rows)
        {
            o.Append("R|").Append(D(r.Price)).Append('|');
            for (int j = 0; j < r.Letters.Count; j++) o.Append(j > 0 ? "," : "").Append(r.Letters[j]);
            o.Append('|');
            for (int j = 0; j < r.Letters.Count; j++) o.Append(GetLetter(r.Letters[j]));
            o.Append('\n');
        }
    }

    public static void Main(string[] args)
    {
        var x = new TatankaTpoProfile();
        x.dailyAtr = new ATR();
        x.sessionIterator = new SessionIterator();
        var atr = new NtAtr(10);
        var o = new StringBuilder();
        foreach (string line in File.ReadAllLines(args[0]))
        {
            string[] p = line.Split('|');
            switch (p[0])
            {
                case "S":
                    x.TickSize = PD(p[1]); x.FilterByTime = p[2] == "1"; x.SessionTime = p[3]; x.BlockMinutes = int.Parse(p[4]);
                    x.AutoRowHeight = p[5] == "1"; x.TargetRows = int.Parse(p[6]); x.RowHeightTicks = int.Parse(p[7]);
                    break;
                case "D":
                    x.dailyAtr.Value = atr.Add(PD(p[1]), PD(p[2]), PD(p[3]));
                    x.CurrentBars[1]++;
                    break;
                case "B":
                    x.CurrentBars[0]++;
                    x.Time.Value = PT(p[1]); x.High.Value = PD(p[2]); x.Low.Value = PD(p[3]);
                    x.Bars.IsFirstBarOfSession = p[4] == "1"; x.sessionIterator.Next = PT(p[5]);
                    int before = x.saved.Count;
                    x.OnBarUpdate();
                    o.Append("T|").Append(D(x.tickStep)).Append('|').Append(x.hasLive ? 1 : 0).Append('|').Append(x.liveInSession ? 1 : 0)
                     .Append('|').Append(x.liveRows.Count).Append('|').Append(T(x.sessionStartTime)).Append('|').Append(T(x.drawStartTime))
                     .Append('|').Append(T(x.lastSessBarTime)).Append('\n');
                    for (int i = before; i < x.saved.Count; i++) x.DumpProfile(o, "P", x.saved[i]);
                    break;
                case "E":
                    if (x.liveRows.Count > 0) x.DumpProfile(o, "LIVE", x.BuildProfileFromMap(x.liveRows, x.drawStartTime, x.Time[0]));
                    break;
            }
        }
        File.WriteAllText(args[1], o.ToString());
    }
}
