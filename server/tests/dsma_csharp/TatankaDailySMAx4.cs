// Copyright (c) TatankaTrading.
// Daily Simple Moving Averages x4 -- NinjaTrader 8.
// Port of the supplied TradingView Pine Script v6 indicator.

#region Using declarations
using System;
using System.ComponentModel;
using System.ComponentModel.DataAnnotations;
using System.Windows.Media;
using System.Xml.Serialization;
using NinjaTrader.Data;
using NinjaTrader.Gui;
using NinjaTrader.Gui.Chart;
using NinjaTrader.Gui.Tools;
using NinjaTrader.NinjaScript;
#endregion

public enum TatankaDailySMALabelSize
{
    Tiny,
    Small,
    Normal,
    Large
}

namespace NinjaTrader.NinjaScript.Indicators
{
    public class TatankaDailySMAx4 : Indicator
    {
        private SMA[] dailyAverages;
        private int[] periods;
        private int dailySeries;
        private SimpleFont labelFont;

        protected override void OnStateChange()
        {
            if (State == State.SetDefaults)
            {
                Name = "Tatanka Daily SMA x4";
                Description = "Four daily simple moving averages on the chart timeframe, with current-value labels.";
                Calculate = Calculate.OnEachTick;
                IsOverlay = true;
                DrawOnPricePanel = true;
                DisplayInDataBox = true;
                PaintPriceMarkers = true;
                IsSuspendedWhileInactive = true;
                BarsRequiredToPlot = 0;
                MaximumBarsLookBack = MaximumBarsLookBack.Infinite;

                SMA1Length = 8;
                SMA2Length = 21;
                SMA3Length = 50;
                SMA4Length = 200;
                LineWidth = 2;
                ShowLabels = true;
                LabelOffset = 10;
                LabelSize = TatankaDailySMALabelSize.Normal;

                // Colors are editable in NinjaTrader's standard Plots section.
                AddPlot(new Stroke(Brushes.Yellow, 2), PlotStyle.Line, "DailySMA1");
                AddPlot(new Stroke(Brushes.Blue, 2), PlotStyle.Line, "DailySMA2");
                AddPlot(new Stroke(Brushes.Red, 2), PlotStyle.Line, "DailySMA3");
                AddPlot(new Stroke(Brushes.White, 2), PlotStyle.Line, "DailySMA4");
            }
            else if (State == State.Configure)
            {
                // Use actual daily bars, not an SMA of intraday closes.
                // Request extra daily history independently of the intraday chart.
                // Hardcoded arguments follow AddDataSeries requirements.
                AddDataSeries(null,
                    new BarsPeriod { BarsPeriodType = BarsPeriodType.Day, Value = 1 },
                    1000, null, null);

                foreach (Plot plot in Plots)
                    plot.Width = LineWidth;
            }
            else if (State == State.DataLoaded)
            {
                // A standard one-day primary chart already has the right input.
                // Using it also avoids a duplicate-series update delay.
                dailySeries = BarsArray[0].BarsPeriod.BarsPeriodType == BarsPeriodType.Day
                    && BarsArray[0].BarsPeriod.Value == 1 ? 0 : 1;

                periods = new[] { SMA1Length, SMA2Length, SMA3Length, SMA4Length };
                dailyAverages = new SMA[4];
                for (int i = 0; i < dailyAverages.Length; i++)
                    dailyAverages[i] = SMA(Closes[dailySeries], periods[i]);

                double fontSize = LabelSize == TatankaDailySMALabelSize.Tiny ? 10
                    : LabelSize == TatankaDailySMALabelSize.Small ? 12
                    : LabelSize == TatankaDailySMALabelSize.Large ? 18 : 14;
                labelFont = new SimpleFont("Arial", fontSize);
            }
        }

        protected override void OnBarUpdate()
        {
            if (dailyAverages == null || (BarsInProgress != 0 && BarsInProgress != dailySeries))
                return;

            // Access the hosted indicators on daily updates even before the
            // primary chart begins, so all loaded daily history is processed.
            if (BarsInProgress == dailySeries && CurrentBars[dailySeries] >= 0)
                for (int i = 0; i < dailyAverages.Length; i++)
                    dailyAverages[i].Update();

            if (CurrentBars[0] < 0)
                return;

            // Primary bars are processed first when historical timestamps match.
            // Allow that daily callback to refresh the matching primary plot point.
            // Never write a newer historical daily close onto an earlier chart bar.
            if (BarsInProgress != 0 && State != State.Realtime
                && Times[0][0] < Times[dailySeries][0])
                return;

            for (int i = 0; i < dailyAverages.Length; i++)
            {
                if (CurrentBars[dailySeries] < periods[i] - 1)
                {
                    // Pine's ta.sma waits for a complete window. NinjaTrader's
                    // built-in SMA alone would output partial-window averages.
                    Values[i].Reset();
                    continue;
                }

                // Values always belongs to the primary chart series, including
                // the equal-timestamp refresh from the secondary daily callback.
                Values[i][0] = dailyAverages[i][0];
            }
        }

        protected override void OnRender(ChartControl chartControl, ChartScale chartScale)
        {
            base.OnRender(chartControl, chartScale);

            if (!ShowLabels || ChartBars == null || RenderTarget == null
                || labelFont == null || periods == null)
                return;

            int lastBar = ChartBars.Bars.Count - 1;
            if (lastBar < 0 || ChartBars.ToIndex < lastBar)
                return;

            // Draw only the latest four labels. No historical drawing objects.
            // Use absolute series indexes because rendering runs independently
            // of the OnBarUpdate barsAgo context.
            float x = chartControl.GetXByBarIndex(ChartBars, lastBar)
                + (float)(LabelOffset * chartControl.Properties.BarDistance);
            float availableWidth = ChartPanel.X + ChartPanel.W - x;
            if (availableWidth <= 0)
                return;

            using (SharpDX.DirectWrite.TextFormat format = labelFont.ToDirectWriteTextFormat())
            {
                format.WordWrapping = SharpDX.DirectWrite.WordWrapping.NoWrap;
                for (int i = 0; i < 4; i++)
                {
                    if (!Values[i].IsValidDataPointAt(lastBar))
                        continue;

                    double price = Values[i].GetValueAt(lastBar);
                    if (double.IsNaN(price) || double.IsInfinity(price))
                        continue;

                    string text = periods[i] + " SMA - " + Instrument.MasterInstrument.FormatPrice(price);
                    using (SharpDX.DirectWrite.TextLayout layout = new SharpDX.DirectWrite.TextLayout(
                        NinjaTrader.Core.Globals.DirectWriteFactory, text, format, availableWidth, 60))
                    using (SharpDX.Direct2D1.Brush brush = Plots[i].Brush.ToDxBrush(RenderTarget))
                    {
                        float y = chartScale.GetYByValue(price) - layout.Metrics.Height / 2;
                        RenderTarget.DrawTextLayout(new SharpDX.Vector2(x, y), layout, brush,
                            SharpDX.Direct2D1.DrawTextOptions.Clip);
                    }
                }
            }
        }

        #region Properties
        [NinjaScriptProperty]
        [Range(1, int.MaxValue)]
        [Display(Name = "SMA #1 Length", Order = 1, GroupName = "SMA Lengths")]
        public int SMA1Length { get; set; }

        [NinjaScriptProperty]
        [Range(1, int.MaxValue)]
        [Display(Name = "SMA #2 Length", Order = 2, GroupName = "SMA Lengths")]
        public int SMA2Length { get; set; }

        [NinjaScriptProperty]
        [Range(1, int.MaxValue)]
        [Display(Name = "SMA #3 Length", Order = 3, GroupName = "SMA Lengths")]
        public int SMA3Length { get; set; }

        [NinjaScriptProperty]
        [Range(1, int.MaxValue)]
        [Display(Name = "SMA #4 Length", Order = 4, GroupName = "SMA Lengths")]
        public int SMA4Length { get; set; }

        [Range(1, 4)]
        [Display(Name = "Line Width", Order = 1, GroupName = "Style")]
        public int LineWidth { get; set; }

        [Display(Name = "Show Labels", Order = 1, GroupName = "Labels")]
        public bool ShowLabels { get; set; }

        [Range(1, 100)]
        [Display(Name = "Label Offset", Description = "Bar spaces to the right of the latest chart bar. Increase the chart's right side margin if labels are clipped.", Order = 2, GroupName = "Labels")]
        public int LabelOffset { get; set; }

        [Display(Name = "Label Size", Order = 3, GroupName = "Labels")]
        public TatankaDailySMALabelSize LabelSize { get; set; }

        [Browsable(false)]
        [XmlIgnore]
        public Series<double> DailySMA1 { get { return Values[0]; } }

        [Browsable(false)]
        [XmlIgnore]
        public Series<double> DailySMA2 { get { return Values[1]; } }

        [Browsable(false)]
        [XmlIgnore]
        public Series<double> DailySMA3 { get { return Values[2]; } }

        [Browsable(false)]
        [XmlIgnore]
        public Series<double> DailySMA4 { get { return Values[3]; } }
        #endregion
    }
}
