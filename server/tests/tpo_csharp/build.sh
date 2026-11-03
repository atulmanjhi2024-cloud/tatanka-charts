#!/bin/sh
# Builds harness.exe for tests/tpo_parity.py from the original NinjaTrader TPO Profile source.
#   tests/tpo_csharp/build.sh <tatanka-indicators checkout> <workdir>
# Pastes the NinjaTrader-free parts of TatankaTpoProfile.cs v1.0.0 (fields, OnBarUpdate, session/row math,
# save and value area, the row/profile classes) between Head.cs and Tail.cs; line numbers are for that version.
set -eu
SRC="$1/ninjatrader/TatankaTpoProfile.cs"
OUT="$2"
HERE="$(dirname "$0")"
grep -q 'Tatanka TPO Profile v1.0.0' "$SRC" || { echo "expected TPO Profile v1.0.0 in $SRC" >&2; exit 1; }
mkdir -p "$OUT"
{
  cat "$HERE/Head.cs"
  sed -n '27,41p;101,172p;175,283p;297,302p;306,413p;818,832p' "$SRC" | sed 's/^\t//'
  cat "$HERE/Tail.cs"
} | tr -d '\r' > "$OUT/Tpo.cs"
mcs -langversion:Latest -optimize+ -out:"$OUT/harness.exe" "$OUT/Tpo.cs"
echo "built $OUT/harness.exe"
