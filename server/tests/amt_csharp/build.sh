#!/bin/sh
# Builds harness.exe for tests/amt_parity.py from the original NinjaTrader AMT Toolkit source.
#   tests/amt_csharp/build.sh <tatanka-indicators checkout> <workdir>
# Takes the NinjaTrader-free parts of TatankaAmtToolkit.cs v1.2.2 (Settings through Engine, LevelsDocument,
# LevelWatch/LevelAlertTracker), found by their declarations.
set -eu
SRC="$1/ninjatrader/TatankaAmtToolkit.cs"
OUT="$2"
grep -q 'Version = "1.2.2"' "$SRC" || { echo "expected AMT Toolkit v1.2.2 in $SRC" >&2; exit 1; }
# the first two namespace blocks (Settings ... Engine), LevelsDocument, LevelWatch through LevelAlertTracker
RANGES=$(tr -d '\r' < "$SRC" | awk '
/^namespace TatankaTrading\.AmtToolkit/ { ns++; if (ns <= 2) s[ns] = NR; next }
/^}/ { for (i = 1; i <= 2; i++) if (s[i] && !e[i] && NR > s[i]) { e[i] = NR; break } }
/public sealed class LevelsDocument/ { ld = NR }
ld && !lde && /^    }/ && NR > ld { lde = NR }
/public sealed class LevelWatch/ { lw = NR }
lw && !lwe && /^}/ && NR > lw { lwe = NR - 1 }
END { printf "%d,%dp;%d,%dp %d,%dp;%d,%dp", s[1], e[1], s[2], e[2], ld, lde, lw, lwe }')
ENGINE=${RANGES% *}
LEVELS=${RANGES#* }
mkdir -p "$OUT"
{
  printf 'using System;\nusing System.Collections.Generic;\nusing System.Globalization;\nusing System.Linq;\nusing System.Text.RegularExpressions;\n'
  sed -n "$ENGINE" "$SRC"
  printf 'namespace TatankaTrading.AmtToolkit\n{\n'
  sed -n "$LEVELS" "$SRC"
  printf '}\n'
} | tr -d '\r' > "$OUT/Engine.cs"
cp "$(dirname "$0")/Harness.cs" "$OUT/"
mcs -langversion:Latest -optimize+ -r:System.Core.dll -out:"$OUT/harness.exe" "$OUT/Engine.cs" "$OUT/Harness.cs"
echo "built $OUT/harness.exe"
