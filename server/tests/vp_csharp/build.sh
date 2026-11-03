#!/bin/sh
# Builds harness.exe for tests/vp_parity.py: Harness.cs plus the calculation cores of the three NinjaTrader
# indicators kept here unchanged (the parts between their BEGIN/END STANDALONE ... CORE markers).
#   tests/vp_csharp/build.sh <workdir>
set -eu
HERE="$(dirname "$0")"
OUT="$1"
mkdir -p "$OUT"
core() { sed -n "/^\/\/ BEGIN STANDALONE $2/,/^\/\/ END STANDALONE $2/p" "$HERE/$1"; }
{
  cat "$HERE/Harness.cs"
  core TatankaAutoAnchoredVolumeProfile.cs "AUTO ANCHORED PROFILE CORE"
  core TatankaPeriodicVolumeProfile.cs "PERIODIC PROFILE CORE"
  core TatankaSessionVolumeProfile.cs "PROFILE CORE"
} | tr -d '\r' > "$OUT/Vp.cs"
mcs -langversion:Latest -optimize+ -out:"$OUT/harness.exe" "$OUT/Vp.cs"
echo "built $OUT/harness.exe"
