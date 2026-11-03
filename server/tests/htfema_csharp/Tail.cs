    // ---- harness ----
    static readonly CultureInfo Inv = CultureInfo.InvariantCulture;
    public static void Main(string[] args)
    {
        var x = new TatankaHtfEma();
        var o = new StringBuilder();
        foreach (string line in File.ReadAllLines(args[0]))
        {
            string[] p = line.Split('|');
            switch (p[0])
            {
                case "S":
                    x.HtfPeriodType = (BarsPeriodType)Enum.Parse(typeof(BarsPeriodType), p[1]);
                    x.HtfPeriod = int.Parse(p[2]); x.EmaLength = int.Parse(p[3]);
                    x.Mode = (TatankaHtfEmaMode)Enum.Parse(typeof(TatankaHtfEmaMode), p[4]);
                    x.BarsPeriod.BarsPeriodType = (BarsPeriodType)Enum.Parse(typeof(BarsPeriodType), p[5]);
                    x.BarsPeriod.Value = int.Parse(p[6]);
                    // State.DataLoaded
                    x.alpha = 2.0 / (x.EmaLength + 1.0);
                    x.lastClosedHtfEma = double.NaN;
                    x.htfEmaReady = false;
                    x.htfEmaInd = new EMA(x.EmaLength);
                    x.scaledLen = x.GetScaledLength();
                    x.smoothEmaInd = new EMA(x.scaledLen);
                    o.Append("S|").Append(x.scaledLen).Append('\n');
                    break;
                case "P":
                    x.CurrentBars[0]++;
                    double c = double.Parse(p[1], Inv);
                    x.smoothEmaInd.Add(c);
                    x.Close.Value = c;
                    x.BarsInProgress = 0;
                    x.Values[0] = new Plot();
                    x.OnBarUpdate();
                    o.Append("V|").Append(x.Values[0].Set ? x.Values[0].Value.ToString("G17", Inv) : "-").Append('\n');
                    break;
                case "H":
                    x.CurrentBars[1]++;
                    x.htfEmaInd.Add(double.Parse(p[1], Inv));
                    x.BarsInProgress = 1;
                    x.OnBarUpdate();
                    break;
            }
        }
        File.WriteAllText(args[1], o.ToString());
    }
}
