    // ---- harness ----
    static readonly CultureInfo Inv = CultureInfo.InvariantCulture;
    // S|len1|len2|len3|len4 (an intraday chart: the daily series is BarsInProgress 1), then in NinjaTrader's order
    // P|close ms (a historical chart bar), D|close ms|close (a daily bar), R (realtime from here), N|close ms (a new
    // realtime chart bar), T|price (a tick: the forming daily bar's close, then BarsInProgress 0 and 1).
    // Writes V|v1|v2|v3|v4 per chart bar ("-": no value).
    public static void Main(string[] args)
    {
        var x = new TatankaDailySMAx4();
        x.Values = new PlotStub[4];
        for (int i = 0; i < 4; i++) x.Values[i] = new PlotStub(() => x.CurrentBars[0]);
        foreach (string line in File.ReadAllLines(args[0]))
        {
            string[] p = line.Split('|');
            switch (p[0])
            {
                case "S":
                    x.dailySeries = 1;   // State.DataLoaded, the chart not 1 Day
                    x.periods = new[] { int.Parse(p[1]), int.Parse(p[2]), int.Parse(p[3]), int.Parse(p[4]) };
                    x.dailyAverages = new SMA[4];
                    for (int i = 0; i < 4; i++) x.dailyAverages[i] = new SMA(x.dailyCloses, x.periods[i]);
                    break;
                case "P":
                case "N":
                    x.CurrentBars[0]++;
                    x.Times[0].Value = long.Parse(p[1]);
                    if (p[0] == "P") { x.BarsInProgress = 0; x.OnBarUpdate(); }
                    break;
                case "D":
                    x.dailyCloses.Add(double.Parse(p[2], Inv));
                    x.CurrentBars[1]++;
                    x.Times[1].Value = long.Parse(p[1]);
                    x.BarsInProgress = 1;
                    x.OnBarUpdate();
                    break;
                case "R":
                    x.State = State.Realtime;
                    break;
                case "T":
                    x.dailyCloses[x.dailyCloses.Count - 1] = double.Parse(p[1], Inv);
                    x.BarsInProgress = 0; x.OnBarUpdate();
                    x.BarsInProgress = 1; x.OnBarUpdate();
                    break;
            }
        }
        var o = new StringBuilder();
        for (int b = 0; b <= x.CurrentBars[0]; b++)
        {
            o.Append('V');
            for (int i = 0; i < 4; i++) { double v; o.Append('|').Append(x.Values[i].Get(b, out v) ? v.ToString("G17", Inv) : "-"); }
            o.Append('\n');
        }
        File.WriteAllText(args[1], o.ToString());
    }
}
