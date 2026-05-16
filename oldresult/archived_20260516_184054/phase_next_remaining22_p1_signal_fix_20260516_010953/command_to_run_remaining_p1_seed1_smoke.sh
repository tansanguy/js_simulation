#!/usr/bin/env bash
set -euo pipefail

cd /Users/junlee/Desktop/2026-1/js

export SUMO_HOME="/Library/Frameworks/EclipseSUMO.framework/Versions/1.26.0/EclipseSUMO"
export PATH="$SUMO_HOME/bin:$PATH"
export PROJ_LIB="/Library/Frameworks/EclipseSUMO.framework/Versions/1.26.0/EclipseSUMO/framework/EclipseSUMO.framework/Resources/proj"
export PYTHONPATH="/Users/junlee/Desktop/2026-1/js:${PYTHONPATH:-}"

CSV="/Users/junlee/Desktop/2026-1/js/result/phase_next_remaining22_p1_signal_fix_20260516_010953/remaining_p1_signal_fix_candidate_table.csv"
NET="/Users/junlee/Desktop/2026-1/js/result/phase_next_remaining22_p1_signal_fix_20260516_010953/remaining_p1_signal_fix_net_v1.net.xml"

echo "[INFO] manual run only. this can take time."
python3 -m smart_crosswalk_sumo.run_phase6_recovery_smoke \
  --candidate-csv "$CSV" \
  --net-file "$NET" \
  --scenario smart \
  --seed 1 \
  --sim-duration 600 \
  --phase-aligned-ped-depart \
  --output-dir "/Users/junlee/Desktop/2026-1/js/result/phase_next_remaining22_p1_signal_fix_20260516_010953/remaining_p1_seed1_smart_smoke"

python3 -m smart_crosswalk_sumo.run_phase6_recovery_smoke \
  --candidate-csv "$CSV" \
  --net-file "$NET" \
  --scenario baseline \
  --seed 1 \
  --sim-duration 600 \
  --phase-aligned-ped-depart \
  --output-dir "/Users/junlee/Desktop/2026-1/js/result/phase_next_remaining22_p1_signal_fix_20260516_010953/remaining_p1_seed1_baseline_smoke"
