#!/usr/bin/env bash
set -euo pipefail

PIPELINE_ROOT="/Users/junlee/Desktop/2026-1/js/result/phase_next_top50_ready33_status_update_20260516_011912"
READY_POOL="$PIPELINE_ROOT/ready33_candidate_pool.csv"
SUMMARY="$PIPELINE_ROOT/ready33_status_summary.md"

export PYTHONPATH="/Users/junlee/Desktop/2026-1/js:${PYTHONPATH:-}"

echo "ready33 pool: $READY_POOL"
echo "summary: $SUMMARY"
python3 - "$READY_POOL" <<'PY'
import sys
from pathlib import Path
import pandas as pd

p = Path(sys.argv[1])
df = pd.read_csv(p)
print(f"ready33_count= {len(df)}")
print(f"net_groups= {df['net_group'].value_counts(dropna=False).to_dict()}")
print(f"status_labels= {df['status_label'].value_counts(dropna=False).to_dict()}")
print("next step: integrate map plan only; do not run SUMO yet")
print("suggested future command template:")
print("  python3 -m smart_crosswalk_sumo.main --simulation_mode integrated_selected --smart_crosswalk_ids <33 ids> --skip_run")
PY

