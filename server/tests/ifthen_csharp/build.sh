#!/bin/sh
# Builds harness.exe for tests/ifthen_parity.py from the original NinjaTrader If/Then Bias Table source.
#   tests/ifthen_csharp/build.sh <tatanka-indicators checkout> <workdir>
# Pastes TatankaBiasRow, TatankaBiasLogic and TatankaBiasDailyAtr of TatankaIfThenBiasTable.cs v1.0.0
# between Head.cs and Tail.cs; line numbers are for that version.
set -eu
SRC="$1/ninjatrader/TatankaIfThenBiasTable.cs"
OUT="$2"
HERE="$(dirname "$0")"
grep -q 'Tatanka If/Then Bias Table v1.0.0' "$SRC" || { echo "expected If/Then v1.0.0 in $SRC" >&2; exit 1; }
mkdir -p "$OUT"
{
  cat "$HERE/Head.cs"
  sed -n '353,359p;361,473p;475,501p' "$SRC"
  cat "$HERE/Tail.cs"
} | tr -d '\r' > "$OUT/IfThen.cs"
mcs -langversion:Latest -optimize+ -r:System.Core.dll -out:"$OUT/harness.exe" "$OUT/IfThen.cs"
echo "built $OUT/harness.exe"
