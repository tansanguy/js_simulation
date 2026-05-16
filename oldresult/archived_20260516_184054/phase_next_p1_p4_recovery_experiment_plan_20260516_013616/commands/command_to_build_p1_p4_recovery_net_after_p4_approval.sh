#!/usr/bin/env bash
set -euo pipefail
cd "/Users/junlee/Desktop/2026-1/js"
MANUAL_CSV="/Users/junlee/Desktop/2026-1/js/result/phase_next_p1_p4_recovery_experiment_plan_20260516_013616/csv/manual_edge_pair_selection.csv"
P1_CSV="/Users/junlee/Desktop/2026-1/js/result/phase_next_remaining22_p1_signal_fix_20260516_010953/remaining_p1_signal_fix_candidate_table.csv"
P1_NET="/Users/junlee/Desktop/2026-1/js/result/phase_next_remaining22_p1_signal_fix_20260516_010953/remaining_p1_signal_fix_net_v1.net.xml"
if [ ! -f "$MANUAL_CSV" ]; then
  echo "missing manual csv: $MANUAL_CSV" >&2
  exit 1
fi
if [ ! -f "$P1_CSV" ]; then
  echo "missing P1 candidate table: $P1_CSV" >&2
  exit 1
fi
if [ ! -f "$P1_NET" ]; then
  echo "missing P1 net: $P1_NET" >&2
  exit 1
fi
APPROVED_COUNT=$(python3 - <<'PY'
from pathlib import Path
import pandas as pd
csv_path = Path(r"/Users/junlee/Desktop/2026-1/js/result/phase_next_p1_p4_recovery_experiment_plan_20260516_013616/csv/manual_edge_pair_selection.csv")
df = pd.read_csv(csv_path)
print(int((df['review_status'].astype(str).str.upper() == 'APPROVED').sum()))
PY
)
if [ "$APPROVED_COUNT" -lt 1 ]; then
  echo "no APPROVED rows; stop" >&2
  exit 1
fi
BUILD_DIR="/Users/junlee/Desktop/2026-1/js/result/phase_next_p1_p4_recovery_experiment_plan_20260516_013616/patches/p1_p4_recovery_after_approval"
mkdir -p "$BUILD_DIR"
python3 -m smart_crosswalk_sumo.run_p4_manual_edge_pair_recovery   --output-dir "$BUILD_DIR"   --manual-selection-csv "$MANUAL_CSV"   --base-net "$P1_NET"   --p4-candidates "/Users/junlee/Desktop/2026-1/js/result/phase_next_remaining22_p1_signal_fix_20260516_010953/remaining_p4_manual_review_candidates.csv"   --mapping-summary "/Users/junlee/Desktop/2026-1/js/result/phase_next_top50_mapping_visual_audit_20260515_174626/03_top50_mapping_distance_summary.csv"   --generated-check "/Users/junlee/Desktop/2026-1/js/result/phase_next_bad22_generated_crossing_patch_20260515_181150/generated_crossing_location_check.csv"   --generated-patch "/Users/junlee/Desktop/2026-1/js/result/phase_next_bad22_generated_crossing_patch_20260515_181150/generated_crossing_patch_candidates.csv"
cp "$BUILD_DIR/p4_manual_recovery_net.net.xml" "/Users/junlee/Desktop/2026-1/js/result/phase_next_p1_p4_recovery_experiment_plan_20260516_013616/p1_p4_recovery_net.net.xml"
