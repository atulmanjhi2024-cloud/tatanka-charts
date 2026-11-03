    // ---- harness ----
    public static class Harness
    {
        static readonly CultureInfo Inv = CultureInfo.InvariantCulture;
        static string D(double x) { return double.IsNaN(x) ? "NaN" : x.ToString("G17", Inv); }
        static string B64(string s) { return Convert.ToBase64String(Encoding.UTF8.GetBytes(s)); }
        static string U64(string s) { return Encoding.UTF8.GetString(Convert.FromBase64String(s)); }
        static double PD(string s) { return s == "NaN" ? double.NaN : double.Parse(s, Inv); }
        static string C(Color4 c)
        {
            return ((int)Math.Round(c.Red * 255)) + "," + ((int)Math.Round(c.Green * 255)) + "," + ((int)Math.Round(c.Blue * 255))
                + "," + Math.Round(c.Alpha, 3).ToString(Inv);
        }
        static Color4 Hex(string h)
        {
            return new Color4(Convert.ToInt32(h.Substring(1, 2), 16) / 255f, Convert.ToInt32(h.Substring(3, 2), 16) / 255f,
                Convert.ToInt32(h.Substring(5, 2), 16) / 255f, 1);
        }
        public static void Main(string[] args)
        {
            TatankaBiasRow[] rows = new TatankaBiasRow[0];
            TatankaBiasDailyAtr atr = null;
            var o = new StringBuilder();
            foreach (string line in File.ReadAllLines(args[0]))
            {
                string[] p = line.Split('|');
                switch (p[0])
                {
                    case "B":
                        rows = TatankaBiasLogic.Parse(U64(p[1]));
                        o.Append("R|").Append(rows.Length).Append('\n');
                        foreach (TatankaBiasRow r in rows)
                            o.Append("r|").Append(B64(r.Bias)).Append('|').Append(D(r.Low)).Append('|').Append(D(r.High)).Append('|')
                             .Append(B64(r.Text)).Append('|').Append(B64(TatankaBiasLogic.Range(r))).Append('\n');
                        break;
                    case "A": o.Append("a|").Append(TatankaBiasLogic.ActiveRow(rows, PD(p[1]))).Append('\n'); break;
                    case "F": o.Append("f|").Append(TatankaBiasLogic.Format(PD(p[1]))).Append('\n'); break;
                    case "W": o.Append("w|").Append(B64(TatankaBiasLogic.Wrap(U64(p[2]), int.Parse(p[1])))).Append('\n'); break;
                    case "C":
                        var s = new TatankaBiasStyle { LineColorMode = p[1], AtrPercent = PD(p[2]), Above = Hex(p[5]), Below = Hex(p[6]), Within = Hex(p[7]) };
                        o.Append("c|").Append(string.Join(";", rows.Select(r => C(TatankaBiasLogic.LevelColor(r, PD(p[3]), PD(p[4]), s))))).Append('\n');
                        break;
                    case "N": atr = new TatankaBiasDailyAtr(int.Parse(p[1])); break;
                    case "U":
                        atr.Update(int.Parse(p[1]), PD(p[2]), PD(p[3]), PD(p[4]));
                        o.Append("u|").Append(D(atr.Previous)).Append('|').Append(D(atr.Current)).Append('\n');
                        break;
                }
            }
            File.WriteAllText(args[1], o.ToString());
        }
    }
}
