#!/usr/bin/env bash
set -euo pipefail
cd "/Users/junlee/Desktop/2026-1/js"

export SUMO_HOME="/Library/Frameworks/EclipseSUMO.framework/Versions/1.26.0/EclipseSUMO"
export PATH="$SUMO_HOME/bin:$PATH"
export PROJ_LIB="/Library/Frameworks/EclipseSUMO.framework/Versions/1.26.0/EclipseSUMO/framework/EclipseSUMO.framework/Resources/proj"
export PYTHONPATH="/Users/junlee/Desktop/2026-1/js:${PYTHONPATH:-}"

CSV="/Users/junlee/Desktop/2026-1/js/result/phase_next_p1_p4_combined_recovery_20260516_020409/csv/p1_p4_recovery_candidate_table.csv"
NET="/Users/junlee/Desktop/2026-1/js/result/phase_next_p1_p4_combined_recovery_20260516_020409/p1_p4_recovery_net.net.xml"

python3 -m smart_crosswalk_sumo.run_phase6_recovery_smoke \
  --candidate-csv "$CSV" \
  --net-file "$NET" \
  --scenario smart \
  --seed 1 \
  --sim-duration 300 \
  --phase-aligned-ped-depart \
  --output-dir "/Users/junlee/Desktop/2026-1/js/result/phase_next_p1_p4_combined_recovery_20260516_020409/patches/p1_p4_seed1_smart_smoke"

python3 -m smart_crosswalk_sumo.run_phase6_recovery_smoke \
  --candidate-csv "$CSV" \
  --net-file "$NET" \
  --scenario baseline \
  --seed 1 \
  --sim-duration 300 \
  --phase-aligned-ped-depart \
  --output-dir "/Users/junlee/Desktop/2026-1/js/result/phase_next_p1_p4_combined_recovery_20260516_020409/patches/p1_p4_seed1_baseline_smoke"
