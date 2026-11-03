// Parity harness for the If/Then port (web/studies.js biasParse..biasLevelColor, chartsrv/ifthen.py
// TatankaBiasDailyAtr): TatankaBiasRow, TatankaBiasLogic and TatankaBiasDailyAtr from TatankaIfThenBiasTable.cs
// v1.0.0 (pasted in by build.sh), with stand-ins for SharpDX's Color4 and the style the logic reads.
using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Linq;
using System.Text;

namespace NinjaTrader.NinjaScript.Indicators
{
    public struct Color4
    {
        public float Red, Green, Blue, Alpha;
        public Color4(float red, float green, float blue, float alpha) { Red = red; Green = green; Blue = blue; Alpha = alpha; }
    }
    internal sealed class TatankaBiasStyle
    {
        internal string LineColorMode;
        internal double AtrPercent;
        internal Color4 Above, Below, Within;
    }

    // ---- pasted from TatankaIfThenBiasTable.cs ----
