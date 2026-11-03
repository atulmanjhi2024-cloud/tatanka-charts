// Parity harness for chartsrv/vp.py: drives the unchanged calculation cores of TatankaAutoAnchoredVolumeProfile 1.1.0,
// TatankaPeriodicVolumeProfile 1.0.0 and TatankaSessionVolumeProfile 1.0.0 (pasted in after this file by build.sh)
// the way each indicator's OnBarUpdate does, and prints every number of their snapshots as raw double bits.
//
// Input lines (stdin), times as epoch ms in one time zone (the harness treats chart and session time as the same):
//   A|anchor|tick|numberOfRows|rowSize|percent|atLeast           AAVP: then M lines, then E
//   P|period|multiple|keep|tick|numberOfRows|rowSize|percent|atLeast   PVP: then M lines, then E
//   M|tradingDay yyyy-MM-dd|sessionBegin|index|low|high|volume|up|time
//   V|start HH:mm|end HH:mm|tick|ticksPerRow|keep|percent|atLeast  SVP: then T lines, then E
//   T|index|price|volume|time
//   S   print the snapshot(s) now         E   print them and end the run
using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Text;

namespace VpHarness
{
    public static class Program
    {
        static readonly DateTime Epoch = new DateTime(1970, 1, 1);
        static DateTime T(string ms) { return Epoch.AddMilliseconds(long.Parse(ms, CultureInfo.InvariantCulture)); }
        static long Ms(DateTime t) { return (long)Math.Round((t - Epoch).TotalMilliseconds); }
        static double D(string s) { return double.Parse(s, CultureInfo.InvariantCulture); }
        static string B(double x) { return BitConverter.DoubleToInt64Bits(x).ToString(CultureInfo.InvariantCulture); }
        static StringBuilder Out = new StringBuilder();

        static void Minute(string tag, TatankaAavpCore.Profile p)
        {
            TatankaAavpCore.Snapshot s = p.GetSnapshot(true);
            MinuteSnap(tag, Ms(p.StartChart), Ms(p.LastChart), p.FirstLoadedProfile, p.Window.Start, s.Rows, s.TotalVolume,
                s.ValueAreaVolume, s.MaxVolume, s.MaxAbsDelta, s.Poc, s.Vah, s.Val, s.LowEdge, s.HighEdge, s.PocIndex, s.VahIndex,
                s.ValIndex, s.TicksPerRow, s.WasCapped);
        }

        static void Minute(string tag, TatankaPvpCore.Profile p)
        {
            TatankaPvpCore.Snapshot s = p.GetSnapshot(true);
            TatankaAavpCore.Row[] rows = new TatankaAavpCore.Row[s.Rows.Length];
            for (int i = 0; i < rows.Length; i++)
                rows[i] = new TatankaAavpCore.Row { LowTick = s.Rows[i].LowTick, HighTick = s.Rows[i].HighTick, Up = s.Rows[i].Up, Down = s.Rows[i].Down };
            MinuteSnap(tag, Ms(p.StartChart), Ms(p.LastChart), p.FirstLoadedProfile, p.Window.Start, rows, s.TotalVolume,
                s.ValueAreaVolume, s.MaxVolume, s.MaxAbsDelta, s.Poc, s.Vah, s.Val, s.LowEdge, s.HighEdge, s.PocIndex, s.VahIndex,
                s.ValIndex, s.TicksPerRow, s.WasCapped);
        }

        static void MinuteSnap(string tag, long start, long last, bool first, DateTime window, TatankaAavpCore.Row[] rows,
            double total, double va, double max, double maxDelta, double poc, double vah, double val, double lowEdge,
            double highEdge, int pocI, int vahI, int valI, long tpr, bool capped)
        {
            Out.Append(tag).Append("|").Append(window.ToString("yyyy-MM-dd")).Append("|").Append(start).Append("|").Append(last)
               .Append("|").Append(first ? 1 : 0).Append("|").Append(rows.Length).Append("\n");
            if (rows.Length == 0) return;
            Out.Append(string.Join("|", new[] { B(total), B(va), B(max), B(maxDelta), B(poc), B(vah), B(val), B(lowEdge), B(highEdge) }))
               .Append("|").Append(pocI).Append("|").Append(vahI).Append("|").Append(valI).Append("|").Append(tpr).Append("|").Append(capped ? 1 : 0).Append("\n");
            foreach (TatankaAavpCore.Row r in rows)
                Out.Append(r.LowTick).Append(",").Append(r.HighTick).Append(",").Append(B(r.Up)).Append(",").Append(B(r.Down)).Append("\n");
        }

        static void Session(string tag, TatankaVpCore.Session s, double percent, bool atLeast)
        {
            TatankaVpCore.Snapshot p = s.GetSnapshot(percent, atLeast, true);
            Out.Append(tag).Append("|").Append(Ms(p.StartChart)).Append("|").Append(Ms(p.EndChart)).Append("|").Append(Ms(p.LastChart))
               .Append("|").Append(p.Rows.Length).Append("\n");
            if (p.Rows.Length == 0) return;
            Out.Append(string.Join("|", new[] { B(p.RowSize), B(p.MaxVolume), B(p.TotalVolume), B(p.ValueAreaVolume), B(p.Poc), B(p.Vah),
                B(p.Val), B(p.LowEdge), B(p.HighEdge) })).Append("|").Append(p.PocKey).Append("|").Append(p.VahKey).Append("|").Append(p.ValKey).Append("\n");
            foreach (TatankaVpCore.Row r in p.Rows)
                Out.Append(r.Key).Append(",").Append(B(r.Up)).Append(",").Append(B(r.Down)).Append("\n");
        }

        public static void Main()
        {
            string line, mode = null;
            TatankaAavpCore.AnchorSeries aavp = null;
            List<TatankaPvpCore.Profile> profiles = new List<TatankaPvpCore.Profile>();
            TatankaPvpCore.Profile current = null;
            NinjaTrader.NinjaScript.TatankaPvpPeriod period = 0;
            int multiple = 1, keep = 20, rowSize = 0, tpr = 1;
            double tick = 0, percent = 0;
            bool numberOfRows = true, atLeast = false;
            List<TatankaVpCore.Session> sessions = new List<TatankaVpCore.Session>();
            TatankaVpCore.Session session = null;
            TatankaVpCore.TickCounter counter = new TatankaVpCore.TickCounter();
            TimeSpan start = TimeSpan.Zero, end = TimeSpan.Zero;
            while ((line = Console.ReadLine()) != null)
            {
                string[] f = line.Split('|');
                switch (f[0])
                {
                    case "A":
                        mode = "A";
                        aavp = new TatankaAavpCore.AnchorSeries((NinjaTrader.NinjaScript.TatankaAavpAnchor)Enum.Parse(typeof(NinjaTrader.NinjaScript.TatankaAavpAnchor), f[1]),
                            D(f[2]), f[3] == "1" ? NinjaTrader.NinjaScript.TatankaAavpRows.NumberOfRows : NinjaTrader.NinjaScript.TatankaAavpRows.TicksPerRow,
                            int.Parse(f[4]), D(f[5]), f[6] == "1");
                        break;
                    case "P":
                        mode = "P";
                        period = (NinjaTrader.NinjaScript.TatankaPvpPeriod)Enum.Parse(typeof(NinjaTrader.NinjaScript.TatankaPvpPeriod), f[1]);
                        multiple = int.Parse(f[2]); keep = int.Parse(f[3]); tick = D(f[4]); numberOfRows = f[5] == "1";
                        rowSize = int.Parse(f[6]); percent = D(f[7]); atLeast = f[8] == "1";
                        profiles.Clear(); current = null;
                        break;
                    case "V":
                        mode = "V";
                        start = TatankaVpCore.SessionClock.ParseTime(f[1]); end = TatankaVpCore.SessionClock.ParseTime(f[2]);
                        tick = D(f[3]); tpr = int.Parse(f[4]); keep = int.Parse(f[5]); percent = D(f[6]); atLeast = f[7] == "1";
                        sessions.Clear(); session = null; counter.Reset();
                        break;
                    case "M":
                    {
                        DateTime td = DateTime.ParseExact(f[1], "yyyy-MM-dd", CultureInfo.InvariantCulture);
                        DateTime begin = T(f[2]); int index = int.Parse(f[3]);
                        double low = D(f[4]), high = D(f[5]), vol = D(f[6]); bool up = f[7] == "1"; DateTime time = T(f[8]);
                        if (mode == "A") aavp.Upsert(td, begin, index, low, high, vol, up, time);
                        else
                        {   // TatankaPeriodicVolumeProfile.OnBarUpdate, BarsInProgress 1
                            TatankaPvpCore.Window window = TatankaPvpCore.PeriodClock.Get(td, period, multiple);
                            if (current == null || current.Window.Start != window.Start)
                            {
                                if (current != null && window.Start < current.Window.Start) break;
                                bool first = current == null;
                                if (current != null) current.Complete();
                                current = new TatankaPvpCore.Profile(window, begin, first, tick,
                                    numberOfRows ? NinjaTrader.NinjaScript.TatankaPvpRows.NumberOfRows : NinjaTrader.NinjaScript.TatankaPvpRows.TicksPerRow,
                                    rowSize, percent, atLeast);
                                profiles.Add(current);
                                while (profiles.Count > keep) profiles.RemoveAt(0);
                            }
                            current.Upsert(index, low, high, vol, up, time);
                        }
                        break;
                    }
                    case "T":
                    {   // TatankaSessionVolumeProfile.OnBarUpdate, BarsInProgress 1 (chart time = session time)
                        double volume = counter.Consume(int.Parse(f[1]), D(f[3]));
                        if (volume <= 0) break;
                        DateTime time = T(f[4]), ws, we;
                        if (TatankaVpCore.SessionClock.TryWindow(time, start, end, out ws, out we))
                        {
                            if (session == null || session.StartLocal != ws)
                            {
                                if (session != null && ws < session.StartLocal) break;
                                if (session != null) session.GetSnapshot(percent, atLeast, true);
                                session = new TatankaVpCore.Session(ws, ws, we, tick, tpr);
                                sessions.Add(session);
                                while (sessions.Count > keep) sessions.RemoveAt(0);
                            }
                            session.Add(D(f[2]), volume, time);
                        }
                        break;
                    }
                    case "S":
                    case "E":
                        if (mode == "A") { if (aavp.Current != null) Minute("AAVP", aavp.Current); else Out.Append("AAVP|none\n"); }
                        else if (mode == "P") { Out.Append("PVP|").Append(profiles.Count).Append("\n"); foreach (TatankaPvpCore.Profile p in profiles) Minute("PROFILE", p); }
                        else if (mode == "V") { Out.Append("SVP|").Append(sessions.Count).Append("\n"); foreach (TatankaVpCore.Session s in sessions) Session("SESSION", s, percent, atLeast); }
                        if (f[0] == "E") Out.Append("END\n");
                        break;
                }
            }
            Console.Out.Write(Out.ToString());
        }
    }
}

