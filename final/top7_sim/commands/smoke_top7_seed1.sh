#!/usr/bin/env bash
# smoke_top7_seed1.sh
# top7 후보 seed1 smoke test
#
# 사용법:
#   bash final/top7_sim/commands/smoke_top7_seed1.sh                          # top7 전체
#   bash final/top7_sim/commands/smoke_top7_seed1.sh --force                  # 전체 재실행
#   bash final/top7_sim/commands/smoke_top7_seed1.sh --no-preflight           # preflight 건너뛰기
#   bash final/top7_sim/commands/smoke_top7_seed1.sh --skip-invalid           # invalid 후보 제외 후 실행
#   bash final/top7_sim/commands/smoke_top7_seed1.sh --min-success 1          # smart 성공 최소 개수
#   bash final/top7_sim/commands/smoke_top7_seed1.sh --ids NODE_5846          # 단일 후보
#   bash final/top7_sim/commands/smoke_top7_seed1.sh --ids LINK_194891 NODE_10262  # 복수 후보
#   bash final/top7_sim/commands/smoke_top7_seed1.sh --ids LINK_194891 NODE_10262 --jobs 2
#   bash final/top7_sim/commands/smoke_top7_seed1.sh --ids 5846 194891        # 숫자만 입력
#   bash final/top7_sim/commands/smoke_top7_seed1.sh --ids 194891 --force     # 조합

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
TMP_DIR="$TOP7_SIM_ROOT/tmp"
RESULTS_DIR="$TOP7_SIM_ROOT/results"
NET_FILE="$NETS_DIR/generated_signal_7.net.xml"
SIM_DURATION=600
OUTPUT_PROFILE=light
export PYTHONPATH="$PROJECT_ROOT:${PYTHONPATH:-}"

# ── 옵션 파싱 ─────────────────────────────────────────────────────────────────
FORCE=false
NO_PREFLIGHT=false
SKIP_INVALID=false
MIN_SUCCESS=1
JOBS=1
RAW_IDS=()   # --ids 뒤에 받은 원시 토큰들

while [[ $# -gt 0 ]]; do
  case "$1" in
    --force)
      FORCE=true; shift ;;
    --no-preflight)
      NO_PREFLIGHT=true; shift ;;
    --skip-invalid)
      SKIP_INVALID=true; shift ;;
    --min-success)
      shift
      if [[ $# -eq 0 || "$1" == --* ]]; then
        echo "--min-success requires a value" >&2
        exit 1
      fi
      MIN_SUCCESS="${1:-}"
      shift ;;
    --min-success=*)
      MIN_SUCCESS="${1#*=}"
      shift ;;
    --jobs)
      shift
      if [[ $# -eq 0 || "$1" == --* ]]; then
        echo "--jobs requires a value" >&2
        exit 1
      fi
      JOBS="${1:-}"
      shift ;;
    --jobs=*)
      JOBS="${1#*=}"
      shift ;;
    --ids)
      shift
      while [[ $# -gt 0 && "$1" != --* ]]; do
        RAW_IDS+=("$1"); shift
      done ;;
    *)
      echo "Unknown option: $1" >&2; exit 1 ;;
  esac
done

if ! [[ "$MIN_SUCCESS" =~ ^[0-9]+$ ]] || [ "$MIN_SUCCESS" -lt 1 ]; then
  echo "--min-success must be a positive integer" >&2
  exit 1
fi
if ! [[ "$JOBS" =~ ^[0-9]+$ ]] || [ "$JOBS" -lt 1 ]; then
  echo "--jobs must be a positive integer" >&2
  exit 1
fi

# ── top7 마스터 목록 ──────────────────────────────────────────────────────────
ALL_TOP7=(
  "NODE_8369"
  "LINK_239754"
  "NODE_5831"
  "NODE_5846"
  "NODE_125895"
  "LINK_194891"
  "NODE_10262"
)

# ── ID 정규화 함수 ─────────────────────────────────────────────────────────────
# 입력 토큰을 SINGLE_CSV_ROOT 파일명 기준으로 정규화해 확정 ID를 에코한다.
# 실패 시 에러 메시지를 stderr에 출력하고 exit 1.
resolve_id() {
  local tok="$1"

  # 이미 정확히 일치하는 파일이 있으면 그대로
  if [[ -f "$SINGLE_CSV_ROOT/${tok}.csv" ]]; then
    echo "$tok"; return 0
  fi

  # 숫자만인 경우: suffix 매칭
  if [[ "$tok" =~ ^[0-9]+$ ]]; then
    local matches=()
    while IFS= read -r -d '' f; do
      local base
      base="$(basename "$f" .csv)"
      # NODE_xxx 또는 LINK_xxx 에서 숫자 suffix 추출
      if [[ "$base" =~ ^(NODE|LINK)_([0-9]+)$ && "${BASH_REMATCH[2]}" == "$tok" ]]; then
        matches+=("$base")
      fi
    done < <(find "$SINGLE_CSV_ROOT" -name "*.csv" -print0)

    case "${#matches[@]}" in
      0) echo "unknown id: '$tok'  (숫자 suffix로 매칭되는 후보 없음)" >&2; return 1 ;;
      1) echo "${matches[0]}"; return 0 ;;
      *) echo "ambiguous id: '$tok'  후보: ${matches[*]}" >&2; return 1 ;;
    esac
  fi

  echo "unknown id: '$tok'  (CSV 없음: $SINGLE_CSV_ROOT/${tok}.csv)" >&2
  return 1
}

candidate_csv_exists() {
  local cw="$1"
  [[ -f "$SINGLE_CSV_ROOT/${cw}.csv" ]]
}

check_preflight_one() {
  local cw="$1"
  python3 - "$PROJECT_ROOT" "$SINGLE_CSV_ROOT/${cw}.csv" <<'PY'
import math
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pandas as pd
import sumolib

project_root = Path(sys.argv[1])
csv_path = Path(sys.argv[2])
cw = csv_path.stem
df = pd.read_csv(csv_path)
if df.empty:
    print("empty candidate csv", file=sys.stderr)
    sys.exit(1)
r = df.iloc[0]

net_path = project_root / "result/active/nets/generated_signal_7.net.xml"
net = sumolib.net.readNet(str(net_path), withInternal=True)
tree = ET.parse(str(net_path))
crossing_to_li = {}
for conn in tree.getroot().findall("connection"):
    to_e = conn.get("to", "")
    tl = conn.get("tl", "")
    li = conn.get("linkIndex", "")
    if tl and li and re.match(r"^:.+_c\d+$", to_e):
        crossing_to_li[to_e] = int(li)

crossing_edge_id = str(r.get("crossing_edge_id", ""))
ped_link_index = int(r.get("ped_link_index", -1))
from_e = str(r.get("route_from_edge", ""))
to_e = str(r.get("route_to_edge", ""))

if not net.getEdge(crossing_edge_id):
    print(f"crossing edge not found: {crossing_edge_id}", file=sys.stderr)
    sys.exit(1)
if ped_link_index not in crossing_to_li.values():
    print(f"ped_link_index {ped_link_index} not in TLS connections", file=sys.stderr)
    sys.exit(1)
fe = net.getEdge(from_e) if from_e else None
te = net.getEdge(to_e) if to_e else None
if not fe:
    print(f"route_from_edge not found: {from_e}", file=sys.stderr)
    sys.exit(1)
if not te:
    print(f"route_to_edge not found: {to_e}", file=sys.stderr)
    sys.exit(1)
m = re.match(r"^:(.+)_c\d+$", crossing_edge_id)
if not m:
    print("can't parse junction", file=sys.stderr)
    sys.exit(1)
junc = net.getNode(m.group(1))
if not junc:
    print("junction not found", file=sys.stderr)
    sys.exit(1)
jx, jy = junc.getCoord()
fn = fe.getToNode(); fx, fy = fn.getCoord()
tn = te.getFromNode(); tx, ty = tn.getCoord()
df = math.sqrt((fx - jx) ** 2 + (fy - jy) ** 2)
dt = math.sqrt((tx - jx) ** 2 + (ty - jy) ** 2)
if df > 150.0:
    print(f"route_from too far: {df:.1f}m", file=sys.stderr)
    sys.exit(1)
if dt > 150.0:
    print(f"route_to too far: {dt:.1f}m", file=sys.stderr)
    sys.exit(1)
print(f"ok ({df:.1f}m / {dt:.1f}m)")
PY
}

force_reset_run_dir() {
  local out_dir="$1"
  local log_file="$2"
  $FORCE && rm -rf "$out_dir" "$log_file"
}

write_status_csv() {
  local status_csv="$RESULTS_DIR/smoke_run_status.csv"
  local selected_ids_csv="$1"
  local excluded_ids_csv="$2"
  local executed_ids_csv="$3"
  local failed_ids_csv="$4"
  local baseline_ok="$5"
  local smart_success_count="$6"
  local smoke_ok="$7"
  local failure_reason="$8"
  python3 - "$status_csv" "$selected_ids_csv" "$excluded_ids_csv" "$executed_ids_csv" \
    "$failed_ids_csv" "$MIN_SUCCESS" "$baseline_ok" "$smart_success_count" "$smoke_ok" \
    "$failure_reason" <<'PY'
import csv, sys
from pathlib import Path

out = Path(sys.argv[1])
row = {
    "selected_ids": sys.argv[2],
    "excluded_ids": sys.argv[3],
    "executed_ids": sys.argv[4],
    "failed_ids": sys.argv[5],
    "min_success": int(sys.argv[6]),
    "baseline_ok": sys.argv[7],
    "smart_success_count": int(sys.argv[8]),
    "smoke_ok": sys.argv[9],
    "failure_reason": sys.argv[10],
}
out.parent.mkdir(parents=True, exist_ok=True)
with out.open("w", newline="", encoding="utf-8-sig") as f:
    w = csv.DictWriter(f, fieldnames=list(row.keys()))
    w.writeheader()
    w.writerow(row)
print(out)
PY
}

# ── 선택 ID 결정 ──────────────────────────────────────────────────────────────
if [[ ${#RAW_IDS[@]} -gt 0 ]]; then
  SELECTED_IDS=()
  for tok in "${RAW_IDS[@]}"; do
    resolved="$(resolve_id "$tok")" || exit 1
    # top7에 속하지 않는 경우 경고 (실행은 막지 않음)
    in_top7=false
    for t7 in "${ALL_TOP7[@]}"; do
      [[ "$t7" == "$resolved" ]] && { in_top7=true; break; }
    done
    $in_top7 || echo "  ⚠ '$resolved' 는 top7 목록에 없지만 단일 CSV가 있어 실행합니다"
    SELECTED_IDS+=("$resolved")
  done
  MODE="selected"
else
  SELECTED_IDS=("${ALL_TOP7[@]}")
  MODE="all"
fi

REQUESTED_IDS=("${SELECTED_IDS[@]}")

# ── SUMO 환경 ─────────────────────────────────────────────────────────────────
if [[ -z "${SUMO_HOME:-}" ]]; then
  command -v sumo >/dev/null 2>&1 || { echo "SUMO_HOME not set" >&2; exit 1; }
else
  export PATH="$SUMO_HOME/bin:$PATH"
  [[ -z "${PROJ_LIB:-}" && -d "$SUMO_HOME/framework/EclipseSUMO.framework/Resources/proj" ]] && \
    export PROJ_LIB="$SUMO_HOME/framework/EclipseSUMO.framework/Resources/proj"
fi
[[ -d "$PROJECT_ROOT/.venv/bin" ]] && export PATH="$PROJECT_ROOT/.venv/bin:$PATH"

mkdir -p "$RUN_ROOT" "$LOG_ROOT" "$FIGURES_DIR" "$TMP_DIR" "$RESULTS_DIR"
[[ -f "$NET_FILE"     ]] || { echo "net.xml 없음: $NET_FILE" >&2; exit 1; }
[[ -f "$BASELINE_CSV" ]] || {
  echo "baseline CSV 없음 → python3 analysis/make_top7_sim_candidates.py 먼저 실행" >&2
  exit 1
}

# ── 선택 로그 출력 ────────────────────────────────────────────────────────────
echo "[top7_sim smoke] selected_ids=${SELECTED_IDS[*]}"
echo "[top7_sim smoke] mode=${MODE}  force=${FORCE}  jobs=${JOBS}"

# ── preflight / selection 정리 ─────────────────────────────────────────────────
EXCLUDED_IDS=()
VALIDATED_IDS=()
PRECHECK_FAILS=0

if [[ "$NO_PREFLIGHT" == true ]]; then
  for cw in "${SELECTED_IDS[@]}"; do
    if ! candidate_csv_exists "$cw"; then
      echo "  ✗ candidate CSV 없음: $SINGLE_CSV_ROOT/${cw}.csv" >&2
      write_status_csv "${REQUESTED_IDS[*]}" "" "" "$cw" false 0 false "missing candidate csv: $cw" >/dev/null
      exit 1
    fi
    VALIDATED_IDS+=("$cw")
  done
else
  for cw in "${SELECTED_IDS[@]}"; do
    if ! candidate_csv_exists "$cw"; then
      echo "  ✗ candidate CSV 없음: $SINGLE_CSV_ROOT/${cw}.csv" >&2
      PRECHECK_FAILS=$((PRECHECK_FAILS + 1))
      EXCLUDED_IDS+=("$cw")
      continue
    fi
    if msg="$(check_preflight_one "$cw" 2>&1)"; then
      VALIDATED_IDS+=("$cw")
    else
      PRECHECK_FAILS=$((PRECHECK_FAILS + 1))
      EXCLUDED_IDS+=("$cw")
      echo "  ✗ preflight failed: $cw  ($msg)" >&2
    fi
  done

  if [[ "$SKIP_INVALID" == true ]]; then
    SELECTED_IDS=("${VALIDATED_IDS[@]}")
    echo "[top7_sim smoke] excluded_by_preflight=${EXCLUDED_IDS[*]:-}"
    echo "[top7_sim smoke] selected_after_preflight=${SELECTED_IDS[*]:-}"
    if [[ ${#SELECTED_IDS[@]} -lt 1 ]]; then
      echo "❌ skip-invalid 후 남은 후보가 없음" >&2
      write_status_csv "${REQUESTED_IDS[*]}" "${EXCLUDED_IDS[*]:-}" "" "${EXCLUDED_IDS[*]:-}" false 0 false "no valid candidates after preflight" >/dev/null
      exit 1
    fi
  elif [[ "$PRECHECK_FAILS" -gt 0 ]]; then
    write_status_csv "${REQUESTED_IDS[*]}" "${EXCLUDED_IDS[*]:-}" "" "${EXCLUDED_IDS[*]:-}" false 0 false "preflight failed" >/dev/null
    echo "❌ preflight 실패 후보 존재 — --skip-invalid 또는 --no-preflight 사용" >&2
    exit 1
  else
    SELECTED_IDS=("${VALIDATED_IDS[@]}")
  fi
fi

REQUESTED_SELECTED_IDS=("${SELECTED_IDS[@]}")

# ── baseline CSV 준비 (선택 ID 필터) ──────────────────────────────────────────
# 선택 ID만 포함하는 임시 baseline CSV 생성
ACTIVE_BASELINE_CSV="$(
  python3 - "$BASELINE_CSV" "$TMP_DIR" "${SELECTED_IDS[@]}" <<'PY'
import sys, pandas as pd
from pathlib import Path
src   = Path(sys.argv[1])
tmpd  = Path(sys.argv[2])
ids   = set(sys.argv[3:])
df    = pd.read_csv(src)
filt  = df[df["crosswalk_id"].astype(str).isin(ids)]
if filt.empty:
    # 선택 ID가 baseline에 없는 경우(단일 CSV는 있지만 top7에서 빠진 후보 등)
    # 경고만 내고 원본 전체 사용
    import sys as _sys
    print(f"warn: none of {ids} found in baseline CSV — using full baseline", file=_sys.stderr)
    print(str(src))
else:
    out = tmpd / "smoke_selected_candidates.csv"
    filt.to_csv(out, index=False, encoding="utf-8-sig")
    print(str(out))
PY
)"

echo "[top7_sim smoke] selected_ids=${SELECTED_IDS[*]}"
echo "[top7_sim smoke] mode=${MODE}  force=${FORCE}  jobs=${JOBS}  no_preflight=${NO_PREFLIGHT}  skip_invalid=${SKIP_INVALID}  min_success=${MIN_SUCCESS}"
echo "[top7_sim smoke] baseline_csv=$ACTIVE_BASELINE_CSV"
echo "[top7_sim smoke] baseline_rows=$(python3 -c "import pandas as pd; print(len(pd.read_csv('$ACTIVE_BASELINE_CSV')))")"

# ── 검증 함수 ─────────────────────────────────────────────────────────────────
verify_run_success() {
  local bench="$1/benchmark_timing.json"
  python3 - "$bench" <<'PY'
import json, sys
from pathlib import Path
path = Path(sys.argv[1])
if not path.exists():
    print(f"missing: {path}", file=sys.stderr); sys.exit(1)
d = json.loads(path.read_text(encoding="utf-8"))
def i(k):
    try: return int(d.get(k, -1))
    except: return -1
ok = (d.get("run_success") is True
      and i("failed_cases_count") == 0
      and i("baseline_result_rows") >= 1
      and i("smart_result_rows") >= 1)
if not ok:
    print(f"failed: {d.get('failure_reason', 'run_success=false')}", file=sys.stderr)
    sys.exit(1)
PY
}

is_done() {
  local out="$1"
  $FORCE && return 1
  [[ -f "$out/simulation_summary.csv" ]]     || return 1
  [[ -f "$out/baseline_smart_summary.csv" ]] || return 1
  verify_run_success "$out" >/dev/null 2>&1
}

# ── 실행 함수 ─────────────────────────────────────────────────────────────────
run_smoke() {
  local candidate_csv="$1" out_dir="$2" log_file="$3"
  local manifest_row_role="$4" manifest_crosswalk_id="$5"
  mkdir -p "$out_dir" "$(dirname "$log_file")"

  # --force 시 로그 초기화
  force_reset_run_dir "$out_dir" "$log_file"
  mkdir -p "$out_dir" "$(dirname "$log_file")"

  if is_done "$out_dir"; then
    echo "  ✓ skip (already done): $manifest_crosswalk_id"; return 0
  fi

  echo "  → running: $manifest_crosswalk_id (seed=1, ${SIM_DURATION}s)"
  if ! python3 -m smart_crosswalk_sumo.run_sampled10_group \
    --candidate-csv   "$candidate_csv" \
    --net-file        "$NET_FILE" \
    --seed            1 \
    --output-dir      "$out_dir" \
    --sim-duration    "$SIM_DURATION" \
    --warmup          0 \
    --traci_step_length 0.1 \
    --traffic_measure_radius_m 500.0 \
    --extension_increment 5.0 \
    --max_extensions  3 \
    --metric-sample-interval  10 \
    --vehicle-sample-interval 10 \
    --progress-interval 30 \
    --phase-aligned-ped-depart \
    --ped-repeat-count 5 \
    --ped-repeat-spacing-sec 2 \
    --include-vehicles \
    --output-profile  "$OUTPUT_PROFILE" \
    --manifest-row-role     "$manifest_row_role" \
    --manifest-crosswalk-id "$manifest_crosswalk_id" \
    >>"$log_file" 2>&1; then
    echo "  ✗ FAILED: $manifest_crosswalk_id  (log: $log_file)" >&2
    tail -5 "$log_file" >&2
    return 1
  fi

  verify_run_success "$out_dir" || { echo "  ✗ verify failed: $manifest_crosswalk_id" >&2; return 1; }

  if ! python3 -m smart_crosswalk_sumo.generate_reports \
    --figures_dir "$FIGURES_DIR" --output_dir "$out_dir" \
    --candidates  "$candidate_csv" --nets_dir "$NETS_DIR" \
    >>"$log_file" 2>&1; then
    echo "  ⚠ report gen failed (run ok): $manifest_crosswalk_id"
  fi
}

# ── 병렬 실행 제어 ────────────────────────────────────────────────────────────
pids=()
wait_for_slot() {
  while true; do
    local active
    active=$(jobs -p | wc -l | tr -d '[:space:]')
    if [ "${active:-0}" -lt "$JOBS" ]; then break; fi
    sleep 0.5
  done
}
wait_for_all_jobs() {
  local status=0
  local pid
  for pid in "${pids[@]:-}"; do
    wait "$pid" || status=1
  done
  pids=()
  return "$status"
}

run_smart_async() {
  local cw="$1" csv="$2" out_dir="$3" log_file="$4" status_file="$5"
  wait_for_slot
  (
    if run_smoke "$csv" "$out_dir" "$log_file" "smart_candidate" "$cw"; then
      printf 'ok\n' > "$status_file"
    else
      printf 'failed\n' > "$status_file"
    fi
  ) &
  pids+=("$!")
}

# ── smoke 결과 요약 ───────────────────────────────────────────────────────────
smoke_summary() {
  local run_root="$1"
  shift
  local selected_ids=("$@")
  python3 - "$run_root" "${selected_ids[@]}" <<'PY'
import json, pandas as pd, sys
from pathlib import Path

RUN_ROOT     = Path(sys.argv[1])
SELECTED_IDS = sys.argv[2:]

print("\n=== smoke 결과 요약 ===")
b = RUN_ROOT / "baseline/seed01/simulation_result.csv"
if b.exists():
    df    = pd.read_csv(b)
    n_cw  = df['crosswalk_id'].nunique()
    net_d = df['network_avg_delay_sec'].mean() if 'network_avg_delay_sec' in df.columns else float('nan')
    print(f"  baseline: crosswalk={n_cw}  network_avg_delay={net_d:.3f}s")
else:
    print("  baseline: 결과 없음")

print()
for cw in SELECTED_IDS:
    f = RUN_ROOT / f"smart/{cw}/seed01/simulation_result.csv"
    if not f.exists():
        print(f"  {'—':1s} {cw:15s}: 결과 없음"); continue
    df    = pd.read_csv(f)
    smart = df[df['scenario'] == 'smart'] if 'scenario' in df.columns else df
    base  = df[df['scenario'] == 'baseline'] if 'scenario' in df.columns else pd.DataFrame()
    ext   = smart['extension_count'].sum()        if 'extension_count'        in smart.columns else '?'
    ext_s = smart['total_extension_sec'].sum()    if 'total_extension_sec'    in smart.columns else '?'
    pet   = smart['pet_event_count'].sum()        if 'pet_event_count'        in smart.columns else '?'
    net_d = smart['network_avg_delay_sec'].mean() if 'network_avg_delay_sec'  in smart.columns else float('nan')
    b_net = (base['network_avg_delay_sec'].mean()
             if not base.empty and 'network_avg_delay_sec' in base.columns
             else float('nan'))
    delta = net_d - b_net if net_d == net_d and b_net == b_net else float('nan')
    bench   = RUN_ROOT / f"smart/{cw}/seed01/benchmark_timing.json"
    success = '✓' if (bench.exists()
                      and json.loads(bench.read_text(encoding='utf-8')).get('run_success')) else '✗'
    print(f"  {success} {cw:15s}  ext={ext}({ext_s}s)  pet={pet}  "
          f"net_delay={net_d:.2f}s(Δ{delta:+.2f}s)")
PY
}

# ── baseline 실행 ─────────────────────────────────────────────────────────────
echo "[top7_sim smoke] baseline seed1 시작"
echo "  baseline_csv: $ACTIVE_BASELINE_CSV"
BASELINE_OK=false
if run_smoke "$ACTIVE_BASELINE_CSV" \
  "$RUN_ROOT/baseline/seed01" \
  "$LOG_ROOT/baseline/seed01.log" \
  "baseline_placeholder" "BASELINE_TOP7_SIM"; then
  BASELINE_OK=true
  echo "  ✓ baseline 완료"
else
  echo "  ✗ baseline 실패" >&2
fi

# ── smart 실행 (선택 ID만) ────────────────────────────────────────────────────
echo ""
echo "[top7_sim smoke] smart seed1 × ${#SELECTED_IDS[@]}후보 시작 (jobs=${JOBS})"
EXECUTED_IDS=()
FAILED_IDS=()
SMART_RUN_IDS=()
SMART_STATUS_DIR="$TMP_DIR/smoke_smart_status_$$"
mkdir -p "$SMART_STATUS_DIR"
for cw in "${SELECTED_IDS[@]}"; do
  csv="$SINGLE_CSV_ROOT/${cw}.csv"
  if [[ ! -f "$csv" ]]; then
    echo "  ✗ CSV 없음: $csv" >&2
    FAILED_IDS+=("$cw")
    continue
  fi
  SMART_RUN_IDS+=("$cw")
  status_file="$SMART_STATUS_DIR/${cw}.status"
  if [[ "$JOBS" -eq 1 ]]; then
    if run_smoke "$csv" \
      "$RUN_ROOT/smart/${cw}/seed01" \
      "$LOG_ROOT/smart/${cw}/seed01.log" \
      "smart_candidate" "$cw"; then
      printf 'ok\n' > "$status_file"
    else
      printf 'failed\n' > "$status_file"
    fi
  else
    run_smart_async "$cw" "$csv" \
      "$RUN_ROOT/smart/${cw}/seed01" \
      "$LOG_ROOT/smart/${cw}/seed01.log" \
      "$status_file"
  fi
done
wait_for_all_jobs || true

for cw in "${SMART_RUN_IDS[@]}"; do
  status_file="$SMART_STATUS_DIR/${cw}.status"
  if [[ -f "$status_file" && "$(cat "$status_file")" == "ok" ]]; then
    EXECUTED_IDS+=("$cw")
  else
    FAILED_IDS+=("$cw")
  fi
done

smoke_summary "$RUN_ROOT" "${SELECTED_IDS[@]}"

SMART_SUCCESS_COUNT="${#EXECUTED_IDS[@]}"
EXCLUDED_JOINED="${EXCLUDED_IDS[*]:-}"
EXECUTED_JOINED="${EXECUTED_IDS[*]:-}"
FAILED_JOINED="${FAILED_IDS[*]:-}"
SELECTED_JOINED="${REQUESTED_IDS[*]:-}"

SMOKE_OK=false
FAILURE_REASON="ok"
if [[ "$BASELINE_OK" != true ]]; then
  FAILURE_REASON="baseline failed"
elif [[ "$SMART_SUCCESS_COUNT" -lt "$MIN_SUCCESS" ]]; then
  FAILURE_REASON="smart_success_count=${SMART_SUCCESS_COUNT} < min_success=${MIN_SUCCESS}"
else
  SMOKE_OK=true
fi

write_status_csv "$SELECTED_JOINED" "$EXCLUDED_JOINED" "$EXECUTED_JOINED" "$FAILED_JOINED" \
  "$BASELINE_OK" "$SMART_SUCCESS_COUNT" "$SMOKE_OK" "$FAILURE_REASON" >/dev/null

echo ""
echo "[top7_sim smoke] excluded_ids=${EXCLUDED_JOINED:-}"
echo "[top7_sim smoke] executed_ids=${EXECUTED_JOINED:-}"
echo "[top7_sim smoke] failed_ids=${FAILED_JOINED:-}"
echo "[top7_sim smoke] min_success=${MIN_SUCCESS} smart_success_count=${SMART_SUCCESS_COUNT}"
echo "[top7_sim smoke] status_csv=$RESULTS_DIR/smoke_run_status.csv"

if [[ "$SMOKE_OK" == true ]]; then
  if [[ "$MODE" == "all" ]]; then
    echo "✅ smoke 전체 통과 — run_top7_30seed.sh 실행 가능"
  else
    echo "✅ smoke 통과 (${EXECUTED_JOINED:-})"
  fi
  exit 0
fi

echo "❌ smoke 실패: $FAILURE_REASON" >&2
exit 1
