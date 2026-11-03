// Parity harness for the HTF EMA port (studies.js htfEmaValues fed by chartsrv/htfema.py): the indicator code
// of TatankaHtfEma.cs v1.0.0 (pasted in by build.sh) with stand-ins for the NinjaTrader members it reads.
using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Text;

public enum BarsPeriodType { Tick, Volume, Range, Second, Minute, Day, Week, Month, Year }

// NinjaTrader 8's EMA indicator
public class EMA
{
    readonly double constant1, constant2;
    readonly List<double> values = new List<double>();
    public EMA(int period) { constant1 = 2.0 / (1 + period); constant2 = 1 - (2.0 / (1 + period)); }
    public void Add(double input)
    {
        values.Add(values.Count == 0 ? input : input * constant1 + constant2 * values[values.Count - 1]);
    }
    public double this[int barsAgo] { get { return values[values.Count - 1 - barsAgo]; } }
}
public class Series<T> { public T Value; public T this[int barsAgo] { get { return Value; } } }
public class Plot { public double Value = double.NaN; public bool Set; public double this[int barsAgo] { get { return Value; } set { Value = value; Set = true; } } }
public class BarsPeriodStub { public BarsPeriodType BarsPeriodType; public int Value; }
public abstract class Indicator { protected virtual void OnBarUpdate() { } }

// ---- pasted from TatankaHtfEma.cs: the mode enum ----
