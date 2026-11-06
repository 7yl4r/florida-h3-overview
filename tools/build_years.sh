#!/usr/bin/env bash
# Supervisor for the multi-year build loop.
#
# Wraps `florida_h3.run` with a relaunch-and-cooldown loop: there is nothing
# to lose on a crash because every stage is resumable from disk caches, so a
# transient remote-read failure only costs a cooldown period. The loop exits
# when every campaign build year has a product manifest, or after
# MAX_RELAUNCHES consecutive relaunches (config-level errors such as a bad
# campaign file abort immediately, since they will not heal on their own).
#
# Usage: tools/build_years.sh [extra runner args...]
set -u

PY="${PY:-/home/tylar/miniconda3/bin/python}"
RUNNER="florida_h3.run"
LOG="${LOG:-data/florida/build.log}"
COOLDOWN="${COOLDOWN:-600}"          # seconds between relaunches
MAX_RELAUNCHES="${MAX_RELAUNCHES:-30}"
CAMPAIGN="${CAMPAIGN:-campaigns/florida-south.json}"
STAGES="${STAGES:-grid,stac,mosaic,h3,product}"

cd "$(dirname "$0")/.." || exit 1
mkdir -p "$(dirname "$LOG")"

count_years_done() {
  need=0; have=0
  for y in $(PYTHONPATH=src "$PY" - "$CAMPAIGN" <<'PYEOF'
import json, sys
with open(sys.argv[1]) as fh:
    c = json.load(fh)
ys = c.get("years", {})
build = ys.get("build") or list(range(ys["start"], ys["end"] + 1))
print(*build, sep="\n")
PYEOF
  ); do
    need=$((need + 1))
    if [ -f "data/florida/product/$y/manifest.json" ]; then
      have=$((have + 1))
    fi
  done
  echo "$have $need"
}

i=0
while :; do
  read -r have need <<<"$(count_years_done)"
  if [ "$have" -eq "$need" ]; then
    echo "[supervisor] all $have/$need years have product manifests; done" | tee -a "$LOG"
    exit 0
  fi
  if [ "$i" -ge "$MAX_RELAUNCHES" ]; then
    echo "[supervisor] giving up after $MAX_RELAUNCHES relaunches at $have/$need years" | tee -a "$LOG"
    exit 1
  fi
  i=$((i + 1))
  echo "[supervisor] relaunch $i at $(date -u '+%FT%TZ') ($have/$need years built)" | tee -a "$LOG"
  PYTHONPATH=src "$PY" -m "$RUNNER" --campaign "$CAMPAIGN" \
      --stage "$STAGES" --workers 12 "$@" >> "$LOG" 2>&1
  rc=$?
  echo "[supervisor] runner exited rc=$rc at $(date -u '+%FT%TZ')" | tee -a "$LOG"
  if [ "$rc" -eq 0 ]; then
    exit 0
  fi
  if tail -c 200000 "$LOG" | grep -Eq "CampaignError|no scene cache at|no mosaic in|no h3 cache"; then
    # Config or stage-sequencing errors will not heal themselves; any
    # "read failed" RuntimeError will. Give up instead of burning relaunches.
    echo "[supervisor] config/stage error detected; not relaunching" | tee -a "$LOG"
    exit "$rc"
  fi
  sleep "$COOLDOWN"
done
