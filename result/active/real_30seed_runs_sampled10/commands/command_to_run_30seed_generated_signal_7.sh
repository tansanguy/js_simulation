#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PIPELINE_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
PROJECT_ROOT="$(cd "$PIPELINE_ROOT/../../.." && pwd)"
RESULT_ROOT="$PROJECT_ROOT/result"
ACTIVE_ROOT="$RESULT_ROOT/active"
NETS_DIR="$ACTIVE_ROOT/nets"
FIGURES_DIR="$PIPELINE_ROOT/figures"
RUN_ROOT="$PIPELINE_ROOT/runs/generated_signal_7"
LOG_ROOT="$PIPELINE_ROOT/logs/generated_signal_7"
SINGLE_CSV_ROOT="$PIPELINE_ROOT/manifests/single_candidates/generated_signal_7"
BASELINE_CSV="$PIPELINE_ROOT/manifests/generated_signal_7_candidates.csv"
NET_FILE="$NETS_DIR/generated_signal_7.net.xml"
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

mkdir -p "$RUN_ROOT" "$LOG_ROOT" "$FIGURES_DIR"
if [[ ! -f "$NET_FILE" ]]; then
  echo "missing group net input: $NET_FILE" >&2
  exit 1
fi

verify_run_success() {
  local benchmark_json="$1/benchmark_timing.json"
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

verify_report_outputs() {
  local out_dir="$1"
  python3 - "$out_dir" <<'PY'
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
out = Path(sys.argv[1])
required = [
    out / "simulation_summary.csv",
    out / "baseline_smart_seed_results.csv",
    out / "baseline_smart_summary.csv",
    out / "local_tradeoff_summary.csv",
    out / "tradeoff_summary.csv",
    out / "demand_params.csv",
    out / "csv" / "results" / "demand_params.csv",
    out / "demand_vehicle.rou.xml",
    out / "demand_vehicle.trips.xml",
    out / "csv" / "report" / "preflight_tradeoff_summary.csv",
]
missing = [str(path) for path in required if not path.exists()]
peds = sorted(out.glob("peds_seed*.rou.xml"))
if not peds:
    missing.append(str(out / "peds_seed*.rou.xml"))
if missing:
    print("missing report outputs: " + ";".join(missing), file=sys.stderr)
    sys.exit(1)
def count_xml(path, tag):
    try:
        return sum(1 for _ in ET.parse(path).getroot().iter(tag))
    except Exception as exc:
        print(f"xml read failed: {path}: {exc}", file=sys.stderr)
        sys.exit(1)
route_count = count_xml(out / "demand_vehicle.rou.xml", "vehicle")
trip_count = count_xml(out / "demand_vehicle.trips.xml", "trip")
if route_count != trip_count:
    print(f"vehicle route/trip count mismatch: route={route_count}, trip={trip_count}", file=sys.stderr)
    sys.exit(1)
for ped in peds:
    person_count = count_xml(ped, "person")
    walk_count = count_xml(ped, "walk")
    if person_count <= 0 or walk_count <= 0 or person_count != walk_count:
        print(f"ped count mismatch: {ped}: person={person_count}, walk={walk_count}", file=sys.stderr)
        sys.exit(1)
PY
}

is_successful_run() {
  local out_dir="$1"
  [[ -f "$out_dir/simulation_summary.csv" ]] || return 1
  [[ -f "$out_dir/baseline_smart_seed_results.csv" ]] || return 1
  [[ -f "$out_dir/baseline_smart_summary.csv" ]] || return 1
  [[ -f "$out_dir/local_tradeoff_summary.csv" ]] || return 1
  [[ -f "$out_dir/tradeoff_summary.csv" ]] || return 1
  verify_run_success "$out_dir" >/dev/null 2>&1
  verify_report_outputs "$out_dir" >/dev/null 2>&1
}

run_sampled() {
  local candidate_csv="$1"
  local out_dir="$2"
  local log_file="$3"
  local seed="$4"
  local manifest_row_role="$5"
  local manifest_crosswalk_id="$6"
  mkdir -p "$out_dir" "$(dirname "$log_file")"
  if is_successful_run "$out_dir"; then
    echo "skip seed$seed $out_dir"
    return 0
  fi
  if verify_run_success "$out_dir" >/dev/null 2>&1; then
    echo "repair report seed$seed $out_dir"
    python3 -m smart_crosswalk_sumo.repair_sampled10_demand_params --output-dir "$out_dir" >>"$log_file" 2>&1
    python3 -m smart_crosswalk_sumo.generate_reports --figures_dir "$FIGURES_DIR" --output_dir "$out_dir" --candidates "$candidate_csv" --nets_dir "$NETS_DIR" >>"$log_file" 2>&1
    verify_report_outputs "$out_dir"
    return 0
  fi
  python3 -m smart_crosswalk_sumo.run_sampled10_group --candidate-csv "$candidate_csv" --net-file "$NET_FILE" --seed "$seed" --output-dir "$out_dir" --sim-duration 600 --warmup 0 --traci_step_length 0.1 --traffic_measure_radius_m 500.0 --extension_increment 5.0 --max_extensions 1 --metric-sample-interval 10 --vehicle-sample-interval 10 --progress-interval 60 --phase-aligned-ped-depart --ped-repeat-count 5 --ped-repeat-spacing-sec 2 --include-vehicles --manifest-row-role "$manifest_row_role" --manifest-crosswalk-id "$manifest_crosswalk_id" >>"$log_file" 2>&1
  verify_run_success "$out_dir"
  python3 -m smart_crosswalk_sumo.generate_reports --figures_dir "$FIGURES_DIR" --output_dir "$out_dir" --candidates "$candidate_csv" --nets_dir "$NETS_DIR" >>"$log_file" 2>&1
  verify_report_outputs "$out_dir"
}

echo "[generated_signal_7] baseline seed1-30 (sampled10)"
for seed in $(seq 1 30); do
  out_dir="$RUN_ROOT/baseline/seed$(printf '%02d' "$seed")"
  log_file="$LOG_ROOT/baseline/seed$(printf '%02d' "$seed").log"
  run_sampled "$BASELINE_CSV" "$out_dir" "$log_file" "$seed" "baseline_placeholder" "BASELINE_GENERATED_SIGNAL_7"
done

SMART_IDS=("LINK_212169" "NODE_8452" "NODE_6082" "NODE_6123" "NODE_8426" "NODE_74388" "NODE_10273")
echo "[generated_signal_7] smart seed1-30 per candidate (sampled10)"
for i in "${!SMART_IDS[@]}"; do
  crosswalk_id="${SMART_IDS[$i]}"
  candidate_csv="$SINGLE_CSV_ROOT/${crosswalk_id}.csv"
  for seed in $(seq 1 30); do
    out_dir="$RUN_ROOT/smart/${crosswalk_id}/seed$(printf '%02d' "$seed")"
    log_file="$LOG_ROOT/smart/${crosswalk_id}/seed$(printf '%02d' "$seed").log"
    run_sampled "$candidate_csv" "$out_dir" "$log_file" "$seed" "smart_candidate" "$crosswalk_id"
  done
done
