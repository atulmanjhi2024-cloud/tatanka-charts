#!/bin/sh
# Builds harness.exe for tests/htfema_parity.py from the original NinjaTrader HTF EMA source.
#   tests/htfema_csharp/build.sh <tatanka-indicators checkout> <workdir>
# Pastes the mode enum, the fields and OnBarUpdate/GetScaledLength/GetPeriodSeconds of TatankaHtfEma.cs
# v1.0.0 between the stand-ins; line numbers are for that version.
set -eu
SRC="$1/ninjatrader/TatankaHtfEma.cs"
OUT="$2"
HERE="$(dirname "$0")"
grep -q 'Tatanka 1hr 21 EMA (HTF EMA) v1.0.0' "$SRC" || { echo "expected HTF EMA v1.0.0 in $SRC" >&2; exit 1; }
mkdir -p "$OUT"
{
  cat "$HERE/Head.cs"
  sed -n '19,24p' "$SRC"
  cat "$HERE/Mid.cs"
  sed -n '30,35p;78,145p' "$SRC"
  cat "$HERE/Tail.cs"
} | tr -d '\r' > "$OUT/HtfEma.cs"
mcs -langversion:Latest -optimize+ -out:"$OUT/harness.exe" "$OUT/HtfEma.cs"
echo "built $OUT/harness.exe"
