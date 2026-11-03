#!/bin/sh
# Builds harness.exe for tests/dsma_parity.py from the NinjaTrader Daily SMA x4 source kept here unchanged.
#   tests/dsma_csharp/build.sh <workdir>
# Pastes its fields and OnBarUpdate between the stand-ins; line numbers are for that file.
set -eu
HERE="$(dirname "$0")"
SRC="$HERE/TatankaDailySMAx4.cs"
OUT="$1"
grep -q 'public class TatankaDailySMAx4 : Indicator' "$SRC" || { echo "expected TatankaDailySMAx4 in $SRC" >&2; exit 1; }
mkdir -p "$OUT"
{
  cat "$HERE/Head.cs"
  sed -n '30,33p;96,131p' "$SRC"
  cat "$HERE/Tail.cs"
} | tr -d '\r' > "$OUT/Dsma.cs"
mcs -langversion:Latest -optimize+ -out:"$OUT/harness.exe" "$OUT/Dsma.cs"
echo "built $OUT/harness.exe"
