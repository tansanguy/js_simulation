#!/usr/bin/env bash
# run_top7_30seed.sh
# top7 후보 × 30seed baseline/smart 시뮬레이션 실행 스크립트
#
# 변경 사항 (기존 대비):
#   - sim-duration: 600 → 1800s
#   - warmup: 0 → 300s
#   - max_extensions: 1 → 3
#   - SSM: --disable-ssm 제거 (PET 관측 활성화)
#   - SMART_IDS: top7 확정 후보 7개
#
# 사용법:
#   bash final/top7_sim/commands/run_top7_30seed.sh
#   bash final/top7_sim/commands/run_top7_30seed.sh --jobs=4
#   bash final/top7_sim/commands/run_top7_30seed.sh --jobs=4 --seeds 1-10
#   bash final/top7_sim/commands/run_top7_30seed.sh --jobs=4 --seeds 1-10 --dry-run
#   bash final/top7_sim/commands/run_top7_30seed.sh --skip-if-done  # 완료된 seed 스킵
#
# 주의:
#   - SMART_IDS는 manifests/top7_baseline_candidates.csv의 top7 순서와 맞춤
#   - NODE_125895 / NODE_8369 은 인접 TLS 군집으로 결과 해석 시 cluster 맥락 참고

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
TOP7_SIM_ROOT="$(cd -- "$SCRIPT_DIR/.." && pwd)"
if PROJECT_ROOT="$(git -C "$SCRIPT_DIR" rev-parse --show-toplevel 2>/dev/null)"; then
  :
else
  PROJECT_ROOT="$(cd -- "$SCRIPT_DIR/../../.." && pwd)"
fi
NETS_DIR="$PROJECT_ROOT/result/active/nets"
FIGURES_DIR="$TOP7_SIM_ROOT/figures"
RUN_ROOT="$TOP7_SIM_ROOT/runs"
LOG_ROOT="$TOP7_SIM_ROOT/logs"
SINGLE_CSV_ROOT="$TOP7_SIM_ROOT/manifests/single_candidates"
BASELINE_CSV="$TOP7_SIM_ROOT/manifests/top7_baseline_candidates.csv"
NET_FILE="$NETS_DIR/generated_signal_7.net.xml"
export PYTHONPATH="$PROJECT_ROOT:${PYTHONPATH:-}"

# ── SUMO 환경 ─────────────────────────────────────────────────────────────────
if [[ -z "${SUMO_HOME:-}" ]]; then
  if ! command -v sumo >/dev/null 2>&1; then
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

# ── 파라미터 파싱 ─────────────────────────────────────────────────────────────
JOBS=1
SEEDS_SPEC="1-30"
SKIP_IF_DONE=false
DRY_RUN=false
POSITIONAL=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --jobs)
      shift
      if [[ $# -eq 0 || "$1" == --* ]]; then
        echo "--jobs requires a value" >&2
        exit 1
      fi
      JOBS="$1"
      shift ;;
    --jobs=*)     JOBS="${1#*=}";        shift ;;
    --seeds)
      shift
      if [[ $# -eq 0 || "$1" == --* ]]; then
        echo "--seeds requires a value" >&2
        exit 1
      fi
      SEEDS_SPEC="$1"
      shift ;;
    --seeds=*)    SEEDS_SPEC="${1#*=}";  shift ;;
    --skip-if-done) SKIP_IF_DONE=true;  shift ;;
    --dry-run)    DRY_RUN=true;         shift ;;
    *)            POSITIONAL+=("$1");    shift ;;
  esac
done
set -- "${POSITIONAL[@]:-}"
if [[ ${#POSITIONAL[@]} -gt 0 ]]; then
  echo "Unknown option(s): ${POSITIONAL[*]}" >&2
  exit 1
fi
if ! [[ "$JOBS" =~ ^[0-9]+$ ]] || [ "$JOBS" -le 0 ]; then
  echo "--jobs must be a positive integer" >&2; exit 1
fi

parse_seeds() {
  local spec="$1"
  python3 - "$spec" <<'PY'
import sys

spec = sys.argv[1].strip()
if not spec:
    raise SystemExit("--seeds must not be empty")
seeds = []
for part in spec.split(","):
    part = part.strip()
    if not part:
        continue
    if "-" in part:
        a, b = part.split("-", 1)
        start, end = int(a), int(b)
        step = 1 if end >= start else -1
        seeds.extend(range(start, end + step, step))
    else:
        seeds.append(int(part))
if not seeds or any(seed <= 0 for seed in seeds):
    raise SystemExit("--seeds must contain positive integers")
seen = set()
ordered = []
for seed in seeds:
    if seed not in seen:
        seen.add(seed)
        ordered.append(seed)
print(" ".join(str(seed) for seed in ordered))
PY
}
read -r -a SEEDS <<<"$(parse_seeds "$SEEDS_SPEC")"
if [[ ${#SEEDS[@]} -lt 1 ]]; then
  echo "--seeds produced no seeds" >&2; exit 1
fi

# ── 병렬 실행 제어 ────────────────────────────────────────────────────────────
pids=()
wait_for_slot() {
  while true; do
    local active
    active=$(jobs -pr | wc -l | tr -d '[:space:]')
    if [ "${active:-0}" -lt "$JOBS" ]; then break; fi
    sleep 0.5
  done
}
wait_for_all_jobs() {
  local status=0
  local pid
  for pid in "${pids[@]:-}"; do wait "$pid" || status=1; done
  pids=()
  if [ "$status" -ne 0 ]; then echo "one or more jobs failed" >&2; return 1; fi
}

# ── 실행 검증 함수 ────────────────────────────────────────────────────────────
verify_run_success() {
  local benchmark_json="$1/benchmark_timing.json"
  python3 - "$benchmark_json" <<'PY'
import json, sys
from pathlib import Path
path = Path(sys.argv[1])
if not path.exists():
    print(f"missing benchmark_timing.json: {path}", file=sys.stderr); sys.exit(1)
payload = json.loads(path.read_text(encoding="utf-8"))
def as_int(name):
    try: return int(payload.get(name, -1))
    except: return -1
ok = (
    payload.get("run_success") is True
    and as_int("failed_cases_count") == 0
    and as_int("baseline_result_rows") >= 1
    and as_int("smart_result_rows") >= 1
)
if not ok:
    reason = payload.get("failure_reason") or "run_success=false"
    print(f"run failed: {reason}", file=sys.stderr); sys.exit(1)
PY
}

is_successful_run() {
  local out_dir="$1"
  [[ -f "$out_dir/simulation_summary.csv" ]]          || return 1
  [[ -f "$out_dir/baseline_smart_seed_results.csv" ]] || return 1
  [[ -f "$out_dir/baseline_smart_summary.csv" ]]      || return 1
  verify_run_success "$out_dir" >/dev/null 2>&1
}

print_run_summary() {
  local out_dir="$1" log_file="$2" status="$3"
  python3 - "$out_dir" "$log_file" "$status" <<'PY'
import json, sys
from pathlib import Path
out = Path(sys.argv[1]); log = Path(sys.argv[2]); status = sys.argv[3]
payload = {}
bench = out / "benchmark_timing.json"
if bench.exists():
    try: payload = json.loads(bench.read_text(encoding="utf-8"))
    except Exception as e: payload = {"last_error": str(e)}
last_log = ""
if log.exists():
    for line in reversed([l.strip() for l in log.read_text(errors="replace").splitlines() if l.strip()]):
        if any(t in line for t in ("Fatal","Error","Exception","failed","WARN")):
            last_log = line[-200:]; break
print(
    f"[top7_sim][{status}] out={out} "
    f"success={payload.get('run_success','')} "
    f"rows={payload.get('result_rows','')} "
    f"reason={payload.get('failure_reason','') or 'ok'} "
    f"{last_log}"
)
PY
}

# ── 메인 실행 함수 ────────────────────────────────────────────────────────────
run_sampled() {
  local candidate_csv="$1"
  local out_dir="$2"
  local log_file="$3"
  local seed="$4"
  local manifest_row_role="$5"
  local manifest_crosswalk_id="$6"

  mkdir -p "$out_dir" "$(dirname "$log_file")"
  echo "[top7_sim][start] seed=$seed role=$manifest_row_role crosswalk=$manifest_crosswalk_id"

  if $SKIP_IF_DONE && is_successful_run "$out_dir"; then
    print_run_summary "$out_dir" "$log_file" "skip"
    return 0
  fi

  if ! python3 -m smart_crosswalk_sumo.run_sampled10_group \
    --candidate-csv   "$candidate_csv" \
    --net-file        "$NET_FILE" \
    --seed            "$seed" \
    --output-dir      "$out_dir" \
    --sim-duration    1800 \
    --warmup          300 \
    --traci_step_length 0.1 \
    --traffic_measure_radius_m 500.0 \
    --extension_increment 5.0 \
    --max_extensions  3 \
    --metric-sample-interval  10 \
    --vehicle-sample-interval 10 \
    --progress-interval 60 \
    --phase-aligned-ped-depart \
    --ped-repeat-count 5 \
    --ped-repeat-spacing-sec 2 \
    --include-vehicles \
    --manifest-row-role      "$manifest_row_role" \
    --manifest-crosswalk-id  "$manifest_crosswalk_id" \
    >>"$log_file" 2>&1; then
    print_run_summary "$out_dir" "$log_file" "run_failed"
    return 1
  fi

  if ! verify_run_success "$out_dir"; then
    print_run_summary "$out_dir" "$log_file" "verify_failed"
    return 1
  fi

  if ! python3 -m smart_crosswalk_sumo.generate_reports \
    --figures_dir "$FIGURES_DIR" \
    --output_dir  "$out_dir" \
    --candidates  "$candidate_csv" \
    --nets_dir    "$NETS_DIR" \
    >>"$log_file" 2>&1; then
    print_run_summary "$out_dir" "$log_file" "report_failed"
    return 1
  fi

  print_run_summary "$out_dir" "$log_file" "done"
}

run_sampled_async() {
  wait_for_slot
  run_sampled "$1" "$2" "$3" "$4" "$5" "$6" &
  pids+=("$!")
}

# ── 사전 확인 ─────────────────────────────────────────────────────────────────
mkdir -p "$RUN_ROOT" "$LOG_ROOT" "$FIGURES_DIR"
if [[ ! -f "$NET_FILE" ]]; then
  echo "net.xml 없음: $NET_FILE" >&2; exit 1
fi
if [[ ! -f "$BASELINE_CSV" ]]; then
  echo "baseline CSV 없음: $BASELINE_CSV" >&2
  echo "먼저 실행: python3 analysis/make_top7_sim_candidates.py" >&2; exit 1
fi

# ── top7 SMART_IDS ─────────────────────────────────────────────────────────────
# 현재 manifests/top7_baseline_candidates.csv 기준 top7
# 주의: NODE_125895 / NODE_8369 → 신당동 인접 TLS 군집
SMART_IDS=(
  "NODE_8369"
  "LINK_239754"
  "NODE_5831"
  "NODE_5846"
  "NODE_125895"
  "LINK_194891"
  "NODE_10262"
)

if [[ "$DRY_RUN" == true ]]; then
  echo "[top7_sim][dry-run] jobs=${JOBS} seeds=${SEEDS[*]} skip_if_done=${SKIP_IF_DONE}"
  echo "[top7_sim][dry-run] baseline_runs=${#SEEDS[@]}"
  echo "[top7_sim][dry-run] smart_ids=${SMART_IDS[*]}"
  echo "[top7_sim][dry-run] smart_runs=$((${#SMART_IDS[@]} * ${#SEEDS[@]}))"
  echo "[top7_sim][dry-run] total_runs=$((${#SEEDS[@]} + ${#SMART_IDS[@]} * ${#SEEDS[@]}))"
  exit 0
fi

# ── baseline seeds ─────────────────────────────────────────────────────────────
echo "[top7_sim] baseline seeds=${SEEDS[*]} 시작 (jobs=${JOBS}, sim=1800s warmup=300s SSM=on)"
for seed in "${SEEDS[@]}"; do
  out_dir="$RUN_ROOT/baseline/seed$(printf '%02d' "$seed")"
  log_file="$LOG_ROOT/baseline/seed$(printf '%02d' "$seed").log"
  run_sampled_async "$BASELINE_CSV" "$out_dir" "$log_file" \
    "$seed" "baseline_placeholder" "BASELINE_TOP7_SIM"
done
wait_for_all_jobs
echo "[top7_sim] baseline 완료"

# ── smart 7개 × seeds ─────────────────────────────────────────────────────────
echo "[top7_sim] smart seeds=${SEEDS[*]} × ${#SMART_IDS[@]}후보 시작 (jobs=${JOBS})"
for crosswalk_id in "${SMART_IDS[@]}"; do
  candidate_csv="$SINGLE_CSV_ROOT/${crosswalk_id}.csv"
  if [[ ! -f "$candidate_csv" ]]; then
    echo "⚠ candidate CSV 없음: $candidate_csv (스킵)" >&2
    continue
  fi
  for seed in "${SEEDS[@]}"; do
    out_dir="$RUN_ROOT/smart/${crosswalk_id}/seed$(printf '%02d' "$seed")"
    log_file="$LOG_ROOT/smart/${crosswalk_id}/seed$(printf '%02d' "$seed").log"
    run_sampled_async "$candidate_csv" "$out_dir" "$log_file" \
      "$seed" "smart_candidate" "$crosswalk_id"
  done
done
wait_for_all_jobs
echo "[top7_sim] 전체 완료"
