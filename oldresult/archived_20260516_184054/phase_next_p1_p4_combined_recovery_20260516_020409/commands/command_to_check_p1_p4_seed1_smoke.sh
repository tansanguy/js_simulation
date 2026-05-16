#!/usr/bin/env bash
set -euo pipefail
cd "/Users/junlee/Desktop/2026-1/js"

python3 - <<'PY'
from pathlib import Path
import pandas as pd

out_dir = Path(r"/Users/junlee/Desktop/2026-1/js/result/phase_next_p1_p4_combined_recovery_20260516_020409")
cand = out_dir / "csv" / "p1_p4_recovery_candidate_table.csv"
smart = out_dir / "patches" / "p1_p4_seed1_smart_smoke" / "phase6_smoke_summary.csv"
base = out_dir / "patches" / "p1_p4_seed1_baseline_smoke" / "phase6_smoke_summary.csv"

def b(v):
    return str(v).strip().lower() in ("1", "true", "yes", "y")

def i(v, default=0):
    try:
        if pd.isna(v):
            return default
        return int(float(v))
    except Exception:
        return default

if not cand.exists():
    raise SystemExit(f"missing candidate table: {cand}")
if not smart.exists() or not base.exists():
    raise SystemExit("missing smoke summary")
print("smoke summaries present")
PY
