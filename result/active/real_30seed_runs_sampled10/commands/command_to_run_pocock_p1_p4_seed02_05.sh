#!/usr/bin/env bash
set -euo pipefail

echo "DEPRECATED/DO_NOT_RUN: p1_p4_recovery_6 is cut after seed01; no seed02-05 Pocock expansion." >&2
exit 1

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PIPELINE_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
PROJECT_ROOT="$(cd "$PIPELINE_ROOT/../../.." && pwd)"
RESULT_ROOT="$PROJECT_ROOT/result"
ACTIVE_ROOT="$RESULT_ROOT/active"
NETS_DIR="$ACTIVE_ROOT/nets"
RUN_ROOT="$PIPELINE_ROOT/runs_pocock/p1_p4_recovery_6_selected"
LOG_ROOT="$PIPELINE_ROOT/logs_pocock/p1_p4_recovery_6_selected"
CANDIDATE_CSV="$PIPELINE_ROOT/manifests/pocock_selected/p1_p4_recovery_6_selected_seed01_top3.csv"
NET_FILE="$NETS_DIR/p1_p4_recovery_6.net.xml"
export PYTHONPATH="$PROJECT_ROOT:${PYTHONPATH:-}"

if [[ -z "${SUMO_HOME:-}" ]]; then
  if command -v sumo >/dev/null 2>&1; then
    :
  else
    echo "SUMO_HOME is not set and sumo is not on PATH" >&2
    exit 1
  fi
else
  export PATH="$SUMO_HOME/bin:$PATH"
  if [[ -z "${PROJ_LIB:-}" && -d "$SUMO_HOME/framework/EclipseSUMO.framework/Resources/proj" ]]; then
    export PROJ_LIB="$SUMO_HOME/framework/EclipseSUMO.framework/Resources/proj"
  fi
fi
if [[ -d "$PROJECT_ROOT/.venv/bin" ]]; then
  export PATH="$PROJECT_ROOT/.venv/bin:$PATH"
fi

mkdir -p "$RUN_ROOT" "$LOG_ROOT"
if [[ ! -f "$CANDIDATE_CSV" ]]; then
  echo "missing candidate csv: $CANDIDATE_CSV" >&2
  exit 1
fi
if [[ ! -f "$NET_FILE" ]]; then
  echo "missing group net input: $NET_FILE" >&2
  exit 1
fi

verify_run_success() {
  local out_dir="$1"
  local benchmark_json="$out_dir/benchmark_timing.json"
  python3 - "$benchmark_json" <<'PY'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
if not path.exists():
    print(f"missing benchmark_timing.json: {path}", file=sys.stderr)
    sys.exit(1)
payload = json.loads(path.read_text(encoding="utf-8"))

def as_int(name):
    try:
        return int(payload.get(name, -1))
    except Exception:
        return -1

ok = (
    payload.get("run_success") is True
    and as_int("failed_cases_count") == 0
    and as_int("baseline_result_rows") >= 1
    and as_int("smart_result_rows") >= 1
)
if not ok:
    reason = payload.get("failure_reason") or "run_success=false"
    print(f"sampled10 run failed: {reason}", file=sys.stderr)
    sys.exit(1)
PY
}

is_successful_seed() {
  local out_dir="$1"
  [[ -f "$out_dir/simulation_result.csv" ]] || return 1
  [[ -f "$out_dir/simulation_results_seed.csv" ]] || return 1
  verify_run_success "$out_dir" >/dev/null 2>&1
}

run_seed() {
  local seed="$1"
  local seed_label
  seed_label="$(printf 'seed%02d' "$seed")"
  local out_dir="$RUN_ROOT/$seed_label"
  local log_file="$LOG_ROOT/$seed_label.log"
  mkdir -p "$out_dir" "$(dirname "$log_file")"
  if is_successful_seed "$out_dir"; then
    echo "skip $seed_label $out_dir"
    return 0
  fi
  echo "run $seed_label $out_dir"
  python3 -m smart_crosswalk_sumo.run_sampled10_group \
    --candidate-csv "$CANDIDATE_CSV" \
    --net-file "$NET_FILE" \
    --seed "$seed" \
    --output-dir "$out_dir" \
    --sim-duration 540 \
    --warmup 0 \
    --traci_step_length 0.1 \
    --traffic_measure_radius_m 500.0 \
    --extension_increment 5.0 \
    --max_extensions 1 \
    --metric-sample-interval 10 \
    --vehicle-sample-interval 10 \
    --progress-interval 60 \
    --phase-aligned-ped-depart \
    --ped-repeat-count 5 \
    --ped-repeat-spacing-sec 2 \
    --include-vehicles \
    --output-profile light \
    --manifest-row-role baseline_placeholder \
    --manifest-crosswalk-id BASELINE_P1_P4_RECOVERY_6 \
    >>"$log_file" 2>&1
  verify_run_success "$out_dir"
}

echo "[pocock p1_p4_recovery_6_selected] group seed02-05 only"
for seed in 2 3 4 5; do
  run_seed "$seed"
done
