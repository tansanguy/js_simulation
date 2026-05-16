#!/usr/bin/env bash
set -euo pipefail
python3 - <<'PY'
from pathlib import Path
import pandas as pd

csv_path = Path(r"/Users/junlee/Desktop/2026-1/js/result/phase_next_p1_p4_recovery_experiment_plan_20260516_013616/csv/manual_edge_pair_selection.csv")
if not csv_path.exists():
    raise SystemExit(f"missing manual csv: {csv_path}")
df = pd.read_csv(csv_path)
required = ['crosswalk_id','original_rank','real_lon','real_lat','mapping_distance_m','failure_category','recovery_strategy','manual_strategy','selected_node_id','selected_tls_id','selected_crossing_edges','selected_from_edge','selected_to_edge','needs_new_signal','confidence','manual_reason','review_status']
missing = [c for c in required if c not in df.columns]
if missing:
    raise SystemExit(f"missing columns: {missing}")
allowed_manual = {'EXISTING_CROSSING_REMAP','MANUAL_CROSSING_ADD','EXISTING_TLS_RETROFIT','NEW_SIGNAL_INSTALLATION','HOLD','REJECT'}
allowed_status = {'APPROVED','NEEDS_MORE_REVIEW','HOLD','REJECT','NEEDS_MANUAL_SELECTION'}
allowed_conf = {'HIGH','MEDIUM','LOW',''}
bad_manual = sorted(set(x for x in df['manual_strategy'].astype(str).str.upper().tolist() if x and x not in allowed_manual))
bad_status = sorted(set(x for x in df['review_status'].astype(str).str.upper().tolist() if x and x not in allowed_status))
bad_conf = sorted(set(x for x in df['confidence'].astype(str).str.upper().tolist() if x and x not in allowed_conf))
if bad_manual:
    raise SystemExit(f"invalid manual_strategy: {bad_manual}")
if bad_status:
    raise SystemExit(f"invalid review_status: {bad_status}")
if bad_conf:
    raise SystemExit(f"invalid confidence: {bad_conf}")
approved = df[df['review_status'].astype(str).str.upper() == 'APPROVED']
if not approved.empty:
    empty_edges = approved['selected_crossing_edges'].astype(str).str.strip().eq('')
    if bool(empty_edges.any()):
        raise SystemExit('APPROVED rows must have selected_crossing_edges')
print('manual_csv_ok', len(df), 'approved', len(approved))
PY
