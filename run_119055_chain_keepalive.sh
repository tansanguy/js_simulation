#!/usr/bin/env bash
set -euo pipefail

cd "$HOME/Desktop/2026-1/js"

STAMP="$(date +%Y%m%d_%H%M%S)"
SMOKE_RUN="smoke_diag_119055_v4_${STAMP}"
PROBE_RUN="extension_probe_119055_7200s_5seeds_${STAMP}"

echo "============================================================"
echo "[START] SUMO 119055 chained simulation"
echo "[TIME]  $(date)"
echo "============================================================"

echo ""
echo "[1/4] py_compile check"
python3 -m py_compile \
  smart_crosswalk_sumo/main.py \
  smart_crosswalk_sumo/integrated_mode.py \
  smart_crosswalk_sumo/run_simulations.py \
  smart_crosswalk_sumo/reporting/final_result_package.py \
  smart_crosswalk_sumo/crossing_patch_v2.py \
  smart_crosswalk_sumo/synthetic_tls_injector.py

echo "[OK] py_compile passed"

run_sim() {
  local RUN_NAME="$1"
  shift

  mkdir -p "result/${RUN_NAME}"

  echo ""
  echo "============================================================"
  echo "[RUN] ${RUN_NAME}"
  echo "[TIME] $(date)"
  echo "============================================================"

  python3 smart_crosswalk_sumo/main.py "$@" --run_name "${RUN_NAME}" 2>&1 | tee "result/${RUN_NAME}/console.log"

  echo ""
  echo "[DONE] ${RUN_NAME}"
  echo "[TIME] $(date)"
}

summarize_run() {
  local RUN_NAME="$1"

  python3 - "$RUN_NAME" <<'PY'
import sys
from pathlib import Path
import pandas as pd

run = Path("result") / sys.argv[1]
print(f"\n[SUMMARY] {run}")

summary_seed = run / "outputs" / "network_simulation_summary_seed.csv"
summary_avg = run / "outputs" / "network_simulation_summary.csv"

print("summary_seed exists:", summary_seed.exists())
print("summary_avg exists:", summary_avg.exists())

summary = summary_seed if summary_seed.exists() else summary_avg
if not summary.exists():
    print("ERROR: no network summary found")
    raise SystemExit(2)

df = pd.read_csv(summary)

cols = [c for c in [
    "scenario",
    "seed",
    "extension_count",
    "total_extension_sec",
    "smart_extension_eval_count",
    "smart_extension_ped_green_count",
    "smart_extension_remaining_trigger_window_count",
    "smart_extension_detected_peds_count",
    "smart_extension_sensor_pass_count",
    "smart_extension_decision_true_count",
    "smart_extension_block_ped_signal_not_green_count",
    "smart_extension_block_remaining_time_sufficient_count",
    "smart_extension_block_no_pedestrians_detected_count",
    "risk_event_count",
    "senior_risk_event_count",
    "avg_vehicle_delay_sec",
    "avg_queue_length",
    "vehicle_delay_cost",
] if c in df.columns]

print("")
print(df[cols].to_string(index=False))

diag_files = sorted((run / "outputs").glob("smart_extension_diagnostics_*_seed*.csv"))
print("\nDiagnostic CSV files:")
if diag_files:
    for f in diag_files:
        print(" -", f)
else:
    print(" - none")

csv_dir = run / "csv"
print("\nClean csv dir exists:", csv_dir.exists())
if csv_dir.exists():
    for f in sorted(csv_dir.glob("*.csv")):
        print(" -", f)
PY
}

echo ""
echo "[2/4] Small smoke simulation: 119055, 900s, seed 42"
run_sim "${SMOKE_RUN}" \
  --simulation_mode integrated_selected \
  --smart_crosswalk_ids 119055 \
  --sim_duration 900 \
  --warmup 0 \
  --seeds 42 \
  --network_mode expanded \
  --buffer_m 1000

summarize_run "${SMOKE_RUN}"

echo ""
echo "[3/4] Larger extension probe: 119055, 7200s, seeds 41 42 43 44 45"
run_sim "${PROBE_RUN}" \
  --simulation_mode integrated_selected \
  --smart_crosswalk_ids 119055 \
  --sim_duration 7200 \
  --warmup 0 \
  --seeds 41 42 43 44 45 \
  --network_mode expanded \
  --buffer_m 1000

summarize_run "${PROBE_RUN}"

echo ""
echo "============================================================"
echo "[ALL DONE]"
echo "SMOKE_RUN=${SMOKE_RUN}"
echo "PROBE_RUN=${PROBE_RUN}"
echo "[TIME] $(date)"
echo "============================================================"
