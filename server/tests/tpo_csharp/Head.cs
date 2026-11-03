// Parity harness for chartsrv/tpo.py: the NinjaTrader-free parts of TatankaTpoProfile.cs v1.0.0 (pasted in
// by build.sh), compiled with stand-ins for the NinjaTrader members they read.
using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Text;
using NinjaTrader.Stubs;

namespace NinjaTrader.Stubs
{
    public class ATR { public double Value = double.NaN; public double this[int barsAgo] { get { return Value; } } }
    public class SessionIterator
    {
        public DateTime ActualSessionBegin, Next;
        public bool GetNextSession(DateTime time, bool includesEndTimeStamp) { ActualSessionBegin = Next; return true; }
    }
    public class BarsStub { public bool IsFirstBarOfSession; }
    public class Series<T> { public T Value; public T this[int barsAgo] { get { return Value; } } }
    public abstract class Indicator { protected virtual void OnBarUpdate() { } }

    // NinjaTrader 8's ATR indicator
    public class NtAtr
    {
        public readonly int Period; public readonly List<double> Values = new List<double>(); double close1;
        public NtAtr(int period) { Period = period; }
        public double Add(double high0, double low0, double close0)
        {
            int cb = Values.Count; double v;
            if (cb == 0) v = high0 - low0;
            else
            {
                double trueRange = Math.Max(Math.Abs(low0 - close1), Math.Max(high0 - low0, Math.Abs(high0 - close1)));
                v = ((Math.Min(cb + 1, Period) - 1) * Values[cb - 1] + trueRange) / Math.Min(cb + 1, Period);
            }
            Values.Add(v); close1 = close0; return v;
        }
    }
}

public class TatankaTpoProfile : Indicator
{
    // The NinjaTrader members the pasted code reads; Main sets them for each bar.
    int BarsInProgress = 0;
    int[] CurrentBars = new int[] { -1, -1 };
    Series<DateTime> Time = new Series<DateTime>();
    Series<double> High = new Series<double>(), Low = new Series<double>();
    double TickSize;
    BarsStub Bars = new BarsStub();
    public bool FilterByTime { get; set; }
    public string SessionTime { get; set; }
    public int BlockMinutes { get; set; }
    public bool AutoRowHeight { get; set; }
    public int TargetRows { get; set; }
    public int RowHeightTicks { get; set; }

    // ---- pasted from TatankaTpoProfile.cs ----
