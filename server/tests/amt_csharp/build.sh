#!/bin/sh
# Builds harness.exe for tests/amt_parity.py from the original NinjaTrader AMT Toolkit source.
#   tests/amt_csharp/build.sh <tatanka-indicators checkout> <workdir>
# Takes the NinjaTrader-free parts of TatankaAmtToolkit.cs v1.2.0 (Settings through Engine, LevelsDocument,
# LevelWatch/LevelAlertTracker); line numbers are for that version.
set -eu
SRC="$1/ninjatrader/TatankaAmtToolkit.cs"
OUT="$2"
grep -q 'Version = "1.2.0"' "$SRC" || { echo "expected AMT Toolkit v1.2.0 in $SRC" >&2; exit 1; }
mkdir -p "$OUT"
{
  printf 'using System;\nusing System.Collections.Generic;\nusing System.Globalization;\nusing System.Linq;\nusing System.Text.RegularExpressions;\n'
  sed -n '32,330p;338,1083p' "$SRC"
  printf 'namespace TatankaTrading.AmtToolkit\n{\n'
  sed -n '1163,1284p;1679,1714p' "$SRC"
  printf '}\n'
} | tr -d '\r' > "$OUT/Engine.cs"
cp "$(dirname "$0")/Harness.cs" "$OUT/"
mcs -langversion:Latest -optimize+ -r:System.Core.dll -out:"$OUT/harness.exe" "$OUT/Engine.cs" "$OUT/Harness.cs"
echo "built $OUT/harness.exe"
