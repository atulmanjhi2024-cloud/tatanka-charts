// Parity harness for the Daily SMA x4 port (studies.js dailySmaValues fed by chartsrv/dsma.py): the fields and
// OnBarUpdate of TatankaDailySMAx4.cs (pasted in by build.sh, unchanged) with stand-ins for the NinjaTrader members
// they read.
using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Text;

public enum State { Historical, Realtime }
public class SimpleFont { }
public abstract class Indicator { protected virtual void OnBarUpdate() { } }

// NinjaTrader 8's SMA over the daily closes, as a running sum: priorSum + Input[0] - Input[Period]. Update() brings
// it to the daily series' current bar; the forming bar again on each tick, its close having moved.
public class SMA
{
    readonly List<double> input;
    readonly int Period;
    readonly List<double> values = new List<double>();
    double sum, priorSum;
    public SMA(List<double> input, int period) { this.input = input; Period = period; }
    public void Update()
    {
        if (values.Count == input.Count && values.Count > 0) { Calc(values.Count - 1, false); return; }
        while (values.Count < input.Count) { values.Add(0); Calc(values.Count - 1, true); }
    }
    void Calc(int CurrentBar, bool IsFirstTickOfBar)
    {
        if (IsFirstTickOfBar)
            priorSum = sum;
        sum = priorSum + input[CurrentBar] - (CurrentBar >= Period ? input[CurrentBar - Period] : 0);
        values[CurrentBar] = sum / (CurrentBar < Period ? CurrentBar + 1 : Period);
    }
    public double this[int barsAgo] { get { return values[values.Count - 1 - barsAgo]; } }
}
public class TimeStub { public long Value; public long this[int barsAgo] { get { return Value; } } }
// A plot of the primary series: [0] is its current bar, Reset() leaves that bar without a value
public class PlotStub
{
    readonly Dictionary<int, double> v = new Dictionary<int, double>();
    readonly Func<int> bar;
    public PlotStub(Func<int> bar) { this.bar = bar; }
    public void Reset() { v.Remove(bar()); }
    public double this[int barsAgo] { get { return v[bar() - barsAgo]; } set { v[bar() - barsAgo] = value; } }
    public bool Get(int i, out double x) { return v.TryGetValue(i, out x); }
}

public class TatankaDailySMAx4 : Indicator
{
    // The NinjaTrader members the pasted code reads; Main sets them for each event.
    int[] CurrentBars = new int[] { -1, -1 };
    int BarsInProgress;
    State State = State.Historical;
    TimeStub[] Times = new TimeStub[] { new TimeStub(), new TimeStub() };
    PlotStub[] Values;
    readonly List<double> dailyCloses = new List<double>();

    // ---- pasted from TatankaDailySMAx4.cs: the fields, OnBarUpdate ----
