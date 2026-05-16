#!/usr/bin/env bash
set -euo pipefail
CSV="/Users/junlee/Desktop/2026-1/js/result/phase_next_top50_max_installation_recovery_20260516_002620/signal_fix_candidate_table.csv"
NET="/Users/junlee/Desktop/2026-1/js/result/phase_next_top50_max_installation_recovery_20260516_002620/signal_fix_net_v1.net.xml"
ROWS=$(python3 - <<'PY'
import pandas as pd
from pathlib import Path
csv = Path(r"/Users/junlee/Desktop/2026-1/js/result/phase_next_top50_max_installation_recovery_20260516_002620/signal_fix_candidate_table.csv")
print(len(pd.read_csv(csv)) if csv.exists() else 0)
PY
)
if [[ "${ROWS}" -eq 0 ]]; then
  echo "No seed1 smoke candidates in $CSV"
  exit 0
fi
python3 -m smart_crosswalk_sumo.run_phase6_recovery_smoke \
  --candidate-csv "$CSV" \
  --net-file "$NET" \
  --scenario smart \
  --seed 1 \
  --sim-duration 600 \
  --phase-aligned-ped-depart \
  --output-dir "/Users/junlee/Desktop/2026-1/js/result/phase_next_top50_max_installation_recovery_20260516_002620/signal_fix_seed1_smart_smoke"
python3 -m smart_crosswalk_sumo.run_phase6_recovery_smoke \
  --candidate-csv "$CSV" \
  --net-file "$NET" \
  --scenario baseline \
  --seed 1 \
  --sim-duration 600 \
  --phase-aligned-ped-depart \
  --output-dir "/Users/junlee/Desktop/2026-1/js/result/phase_next_top50_max_installation_recovery_20260516_002620/signal_fix_seed1_baseline_smoke"
