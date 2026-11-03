
public class TatankaHtfEma : Indicator
{
    // The NinjaTrader members the pasted code reads; Main sets them for each bar.
    int[] CurrentBars = new int[] { -1, -1 };
    int BarsInProgress;
    Series<double> Close = new Series<double>();
    Plot[] Values = new Plot[] { new Plot() };
    BarsPeriodStub BarsPeriod = new BarsPeriodStub();
    public BarsPeriodType HtfPeriodType { get; set; }
    public int HtfPeriod { get; set; }
    public int EmaLength { get; set; }
    public TatankaHtfEmaMode Mode { get; set; }

    // ---- pasted from TatankaHtfEma.cs: fields, OnBarUpdate, GetScaledLength, GetPeriodSeconds ----
