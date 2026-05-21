#!/usr/bin/env bash
# smoke_multi_smart_seed1.sh
# 여러 후보가 하나의 SUMO simulation 안에서 동시에 smart control 대상이 되는지 검증
#
# 핵심:
#   - baseline과 multi-smart 각 1회씩 실행 (= 2회 simulation)
#   - multi-smart: 선택된 후보들이 "같은 simulation" 안에서 동시에 smart 작동
#   - 후보별 개별 smart 실행(N회)과는 다름 → 동시 설치 효과 측정 가능
#
# 작동 원리:
#   run_sampled10_group은 candidate CSV의 모든 행에 동시에 smart extension 적용.
#   선택 후보만 담은 임시 CSV 1개를 만들어 1번 호출하면 multi-smart가 구현됨.
#
# 사용법:
#   bash final/top7_sim/commands/smoke_multi_smart_seed1.sh --ids LINK_194891 NODE_10262 --force
#   bash final/top7_sim/commands/smoke_multi_smart_seed1.sh --ids 194891 10262 5831 --force
#   bash final/top7_sim/commands/smoke_multi_smart_seed1.sh --skip-invalid --min-success 1
#   bash final/top7_sim/commands/smoke_multi_smart_seed1.sh --no-preflight
#   bash final/top7_sim/commands/smoke_multi_smart_seed1.sh   # top7 전체 동시 설치

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
RUN_ROOT="$TOP7_SIM_ROOT/runs/multi_smart_smoke"
LOG_ROOT="$TOP7_SIM_ROOT/logs/multi_smart_smoke"
RESULTS_DIR="$TOP7_SIM_ROOT/results"
TMP_DIR="$TOP7_SIM_ROOT/tmp"
SINGLE_CSV_ROOT="$TOP7_SIM_ROOT/manifests/single_candidates"
NET_FILE="$NETS_DIR/generated_signal_7.net.xml"
SIM_DURATION=600
OUTPUT_PROFILE=light
export PYTHONPATH="$PROJECT_ROOT:${PYTHONPATH:-}"

# ── 옵션 파싱 ─────────────────────────────────────────────────────────────────
FORCE=false
NO_PREFLIGHT=false
SKIP_INVALID=false
SMART_ONLY=false
MIN_SUCCESS=1
RAW_IDS=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --force) FORCE=true; shift ;;
    --no-preflight) NO_PREFLIGHT=true; shift ;;
    --skip-invalid) SKIP_INVALID=true; shift ;;
    --smart-only) SMART_ONLY=true; shift ;;
    --min-success)
      shift
      MIN_SUCCESS="${1:-}"
      shift ;;
    --min-success=*)
      MIN_SUCCESS="${1#*=}"
      shift ;;
    --ids)
      shift
      while [[ $# -gt 0 && "$1" != --* ]]; do RAW_IDS+=("$1"); shift; done ;;
    *) echo "Unknown option: $1" >&2; exit 1 ;;
  esac
done

if ! [[ "$MIN_SUCCESS" =~ ^[0-9]+$ ]] || [ "$MIN_SUCCESS" -lt 1 ]; then
  echo "--min-success must be a positive integer" >&2
  exit 1
fi

ALL_TOP7=(
  "NODE_8369" "LINK_239754" "NODE_5831" "NODE_5846"
  "NODE_125895" "LINK_194891" "NODE_10262"
)

# ── ID 정규화 ─────────────────────────────────────────────────────────────────
resolve_id() {
  local tok="$1"
  if [[ -f "$SINGLE_CSV_ROOT/${tok}.csv" ]]; then echo "$tok"; return 0; fi
  if [[ "$tok" =~ ^[0-9]+$ ]]; then
    local matches=()
    while IFS= read -r -d '' f; do
      local base; base="$(basename "$f" .csv)"
      if [[ "$base" =~ ^(NODE|LINK)_([0-9]+)$ && "${BASH_REMATCH[2]}" == "$tok" ]]; then
        matches+=("$base")
      fi
    done < <(find "$SINGLE_CSV_ROOT" -name "*.csv" -print0)
    case "${#matches[@]}" in
      0) echo "unknown id: '$tok'" >&2; return 1 ;;
      1) echo "${matches[0]}"; return 0 ;;
      *) echo "ambiguous id: '$tok' → ${matches[*]}" >&2; return 1 ;;
    esac
  fi
  echo "unknown id: '$tok'" >&2; return 1
}

if [[ ${#RAW_IDS[@]} -gt 0 ]]; then
  SELECTED_IDS=()
  for tok in "${RAW_IDS[@]}"; do
    resolved="$(resolve_id "$tok")" || exit 1
    SELECTED_IDS+=("$resolved")
  done
else
  SELECTED_IDS=("${ALL_TOP7[@]}")
fi

REQUESTED_IDS=("${SELECTED_IDS[@]}")

if [[ ${#SELECTED_IDS[@]} -lt 1 ]]; then
  echo "최소 1개 ID 필요" >&2; exit 1
fi

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

write_comparison_csv() {
  local baseline_dir="$1"
  local smart_dir="$2"
  local out_csv="$3"
  local selected_ids="$4"
  local excluded_ids="$5"
  local executed_ids="$6"
  local failed_ids="$7"
  local baseline_ok="$8"
  local smart_ok="$9"
  local failure_reason="${10}"

  python3 - "$baseline_dir" "$smart_dir" "$out_csv" "$selected_ids" "$excluded_ids" \
    "$executed_ids" "$failed_ids" "$MIN_SUCCESS" "$baseline_ok" "$smart_ok" \
    "$failure_reason" <<'PY'
import csv
import json
import sys
from pathlib import Path

import pandas as pd

baseline_dir = Path(sys.argv[1])
smart_dir = Path(sys.argv[2])
out_csv = Path(sys.argv[3])
selected_ids = sys.argv[4]
excluded_ids = sys.argv[5]
executed_ids = sys.argv[6]
failed_ids = sys.argv[7]
min_success = int(sys.argv[8])
baseline_ok = sys.argv[9]
smart_ok = sys.argv[10]
failure_reason = sys.argv[11]

out_csv.parent.mkdir(parents=True, exist_ok=True)

def load_bench(d):
    p = d / "benchmark_timing.json"
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}

def load_sim(d):
    for cand in ["simulation_result.csv", "csv/results/simulation_result.csv"]:
        p = d / cand
        if p.exists():
            return pd.read_csv(p)
    return pd.DataFrame()

bb = load_bench(baseline_dir)
sb = load_bench(smart_dir)
bdf = load_sim(baseline_dir)
sdf = load_sim(smart_dir)

def net_mean(df, col, scen):
    sub = df[df["scenario"] == scen] if "scenario" in df.columns else df
    return sub[col].mean() if col in sub.columns and not sub.empty else float("nan")

b_net = net_mean(bdf, "network_avg_delay_sec", "baseline")
s_net = net_mean(sdf, "network_avg_delay_sec", "smart")
b_arv = net_mean(bdf, "network_arrived_vehicles", "baseline")
s_arv = net_mean(sdf, "network_arrived_vehicles", "smart")
b_ttime = net_mean(bdf, "network_mean_travel_time", "baseline")
s_ttime = net_mean(sdf, "network_mean_travel_time", "smart")

ext_json = {}
if not sdf.empty and "extension_count" in sdf.columns:
    smart_rows = sdf[sdf["scenario"] == "smart"] if "scenario" in sdf.columns else sdf
    for cw in executed_ids.split():
        sub = smart_rows[smart_rows["crosswalk_id"] == cw]
        ext_json[cw] = int(sub["extension_count"].sum()) if not sub.empty else 0

total_ext_cnt = sum(ext_json.values())
total_ext_sec = 0.0
if not sdf.empty and "total_extension_sec" in sdf.columns:
    smart_rows = sdf[sdf["scenario"] == "smart"] if "scenario" in sdf.columns else sdf
    total_ext_sec = float(smart_rows["total_extension_sec"].sum())

b_done = bb.get("run_success") is True
s_done = sb.get("run_success") is True
smoke_ok = (baseline_ok == "true" and smart_ok == "true" and len(executed_ids.split()) >= min_success)

row = {
    "run_label": f"seed01_{executed_ids.replace(' ', '+') or 'none'}",
    "seed": 1,
    "selected_ids": selected_ids,
    "excluded_ids": excluded_ids,
    "executed_ids": executed_ids,
    "failed_ids": failed_ids,
    "min_success": min_success,
    "n_smart_crosswalks": len(executed_ids.split()) if executed_ids else 0,
    "baseline_completed": b_done,
    "smart_completed": s_done,
    "baseline_network_avg_delay_sec": round(b_net, 4) if b_net == b_net else None,
    "smart_network_avg_delay_sec": round(s_net, 4) if s_net == s_net else None,
    "delta_network_avg_delay_sec": round(s_net - b_net, 4) if (s_net == s_net and b_net == b_net) else None,
    "baseline_network_arrived_vehicles": round(b_arv, 1) if b_arv == b_arv else None,
    "smart_network_arrived_vehicles": round(s_arv, 1) if s_arv == s_arv else None,
    "delta_network_arrived_vehicles": round(s_arv - b_arv, 1) if (s_arv == s_arv and b_arv == b_arv) else None,
    "baseline_network_mean_travel_time": round(b_ttime, 4) if b_ttime == b_ttime else None,
    "smart_network_mean_travel_time": round(s_ttime, 4) if s_ttime == s_ttime else None,
    "delta_network_mean_travel_time": round(s_ttime - b_ttime, 4) if (s_ttime == s_ttime and b_ttime == b_ttime) else None,
    "total_extension_count": total_ext_cnt,
    "total_extension_sec": round(total_ext_sec, 1),
    "per_crosswalk_extension_count_json": json.dumps(ext_json, ensure_ascii=False),
    "baseline_ok": baseline_ok,
    "smart_ok": smart_ok,
    "smoke_ok": smoke_ok,
    "failure_reason": failure_reason,
}

pd.DataFrame([row]).to_csv(out_csv, index=False, encoding="utf-8-sig")
print(f"  saved: {out_csv}")
PY
}

# ── 환경 설정 ─────────────────────────────────────────────────────────────────
if [[ -z "${SUMO_HOME:-}" ]]; then
  command -v sumo >/dev/null 2>&1 || { echo "SUMO_HOME not set" >&2; exit 1; }
else
  export PATH="$SUMO_HOME/bin:$PATH"
  [[ -z "${PROJ_LIB:-}" && -d "$SUMO_HOME/framework/EclipseSUMO.framework/Resources/proj" ]] && \
    export PROJ_LIB="$SUMO_HOME/framework/EclipseSUMO.framework/Resources/proj"
fi
[[ -d "$PROJECT_ROOT/.venv/bin" ]] && export PATH="$PROJECT_ROOT/.venv/bin:$PATH"
mkdir -p "$RUN_ROOT" "$LOG_ROOT" "$FIGURES_DIR" "$TMP_DIR" "$RESULTS_DIR"
[[ -f "$NET_FILE" ]] || { echo "net.xml 없음: $NET_FILE" >&2; exit 1; }

# ── 선택 ID 출력 ──────────────────────────────────────────────────────────────
SELECTED_STR="${SELECTED_IDS[*]}"
echo "[multi_smart smoke] selected_ids=${SELECTED_STR}"
echo "[multi_smart smoke] n_smart_crosswalks=${#SELECTED_IDS[@]}  force=${FORCE}"

# ── preflight / selection 정리 ─────────────────────────────────────────────────
EXCLUDED_IDS=()
VALIDATED_IDS=()
PRECHECK_FAILS=0

if [[ "$NO_PREFLIGHT" == true ]]; then
  for cw in "${SELECTED_IDS[@]}"; do
    if ! candidate_csv_exists "$cw"; then
      echo "  ✗ candidate CSV 없음: $SINGLE_CSV_ROOT/${cw}.csv" >&2
      write_comparison_csv "$RUN_ROOT/baseline/seed01" "$RUN_ROOT/multi_smart/seed01" \
        "$RESULTS_DIR/multi_smart_smoke_comparison.csv" "${REQUESTED_IDS[*]}" \
        "" "" "$cw" false false "missing candidate csv: $cw" >/dev/null
      exit 1
    fi
    VALIDATED_IDS+=("$cw")
  done
else
  for cw in "${SELECTED_IDS[@]}"; do
    if ! candidate_csv_exists "$cw"; then
      echo "  ✗ candidate CSV 없음: $SINGLE_CSV_ROOT/${cw}.csv" >&2
      EXCLUDED_IDS+=("$cw")
      PRECHECK_FAILS=$((PRECHECK_FAILS + 1))
      continue
    fi
    if msg="$(check_preflight_one "$cw" 2>&1)"; then
      VALIDATED_IDS+=("$cw")
    else
      EXCLUDED_IDS+=("$cw")
      PRECHECK_FAILS=$((PRECHECK_FAILS + 1))
      echo "  ✗ preflight failed: $cw  ($msg)" >&2
    fi
  done

  if [[ "$SKIP_INVALID" == true ]]; then
    SELECTED_IDS=("${VALIDATED_IDS[@]}")
    echo "[multi_smart smoke] excluded_by_preflight=${EXCLUDED_IDS[*]:-}"
    echo "[multi_smart smoke] selected_after_preflight=${SELECTED_IDS[*]:-}"
    if [[ ${#SELECTED_IDS[@]} -lt 1 ]]; then
      write_comparison_csv "$RUN_ROOT/baseline/seed01" "$RUN_ROOT/multi_smart/seed01" \
        "$RESULTS_DIR/multi_smart_smoke_comparison.csv" "${REQUESTED_IDS[*]}" \
        "${EXCLUDED_IDS[*]:-}" "" "${EXCLUDED_IDS[*]:-}" false false \
        "no valid candidates after preflight" >/dev/null
      echo "❌ skip-invalid 후 남은 후보가 없음" >&2
      exit 1
    fi
  elif [[ "$PRECHECK_FAILS" -gt 0 ]]; then
    write_comparison_csv "$RUN_ROOT/baseline/seed01" "$RUN_ROOT/multi_smart/seed01" \
      "$RESULTS_DIR/multi_smart_smoke_comparison.csv" "${REQUESTED_IDS[*]}" \
      "${EXCLUDED_IDS[*]:-}" "" "${EXCLUDED_IDS[*]:-}" false false "preflight failed" >/dev/null
    echo "❌ preflight 실패" >&2
    exit 1
  else
    SELECTED_IDS=("${VALIDATED_IDS[@]}")
  fi
fi

MULTI_VALID_COUNT="${#SELECTED_IDS[@]}"
if [[ "$MULTI_VALID_COUNT" -lt "$MIN_SUCCESS" ]]; then
  write_comparison_csv "$RUN_ROOT/baseline/seed01" "$RUN_ROOT/multi_smart/seed01" \
    "$RESULTS_DIR/multi_smart_smoke_comparison.csv" "${REQUESTED_IDS[*]}" \
    "${EXCLUDED_IDS[*]:-}" "" "${EXCLUDED_IDS[*]:-}" false false \
    "valid candidate count ${MULTI_VALID_COUNT} < min_success ${MIN_SUCCESS}" >/dev/null
  echo "❌ min-success 미달: ${MULTI_VALID_COUNT} < ${MIN_SUCCESS}" >&2
  exit 1
fi

# ── multi-smart 후보 CSV 생성 (선택 ID만 포함) ───────────────────────────────
MULTI_CSV="$TMP_DIR/multi_smart_candidates_$(echo "${SELECTED_IDS[*]}" | tr ' ' '_').csv"
python3 - "$TMP_DIR" "${SELECTED_IDS[@]}" <<'PY'
import sys, pandas as pd
from pathlib import Path
tmp_dir      = Path(sys.argv[1])
selected_ids = sys.argv[2:]
top7_dir     = tmp_dir.parent / "manifests/single_candidates"
rows = []
for cw in selected_ids:
    p = top7_dir / f"{cw}.csv"
    if not p.exists():
        print(f"  ✗ CSV 없음: {p}", file=sys.stderr); sys.exit(1)
    rows.append(pd.read_csv(p))
combined = pd.concat(rows, ignore_index=True)
out = tmp_dir / f"multi_smart_candidates_{'_'.join(selected_ids)}.csv"
combined.to_csv(out, index=False, encoding="utf-8-sig")
print(str(out))
PY
MULTI_CSV="$TMP_DIR/multi_smart_candidates_$(echo "${SELECTED_IDS[*]}" | tr ' ' '_').csv"
[[ -f "$MULTI_CSV" ]] || {
  echo "❌ multi-smart CSV 생성 실패" >&2; exit 1
}

MULTI_ROWS=$(python3 -c "import pandas as pd; print(len(pd.read_csv('$MULTI_CSV')))") 
echo "[multi_smart smoke] multi_csv=$MULTI_CSV  rows=$MULTI_ROWS"

# ── 검증 함수 ─────────────────────────────────────────────────────────────────
verify_run_success() {
  local bench="$1/benchmark_timing.json"
  python3 - "$bench" <<'PY'
import json, sys
from pathlib import Path
p = Path(sys.argv[1])
if not p.exists(): print(f"missing: {p}", file=sys.stderr); sys.exit(1)
d = json.loads(p.read_text(encoding="utf-8"))
def i(k):
    try: return int(d.get(k,-1))
    except: return -1
ok = d.get("run_success") is True and i("failed_cases_count")==0 and i("baseline_result_rows")>=1 and i("smart_result_rows")>=1
if not ok: print(f"failed: {d.get('failure_reason','?')}", file=sys.stderr); sys.exit(1)
PY
}

is_done() {
  local out="$1"
  $FORCE && return 1
  [[ -f "$out/simulation_summary.csv" ]] && [[ -f "$out/baseline_smart_summary.csv" ]] || return 1
  verify_run_success "$out" >/dev/null 2>&1
}

run_sim() {
  local csv="$1" out="$2" log="$3" role="$4" label="$5"
  mkdir -p "$out" "$(dirname "$log")"
  force_reset_run_dir "$out" "$log"
  mkdir -p "$out" "$(dirname "$log")"
  if is_done "$out"; then echo "  ✓ skip: $label"; return 0; fi
  echo "  → $label (seed=1, ${SIM_DURATION}s)"
  if ! python3 -m smart_crosswalk_sumo.run_sampled10_group \
    --candidate-csv "$csv" --net-file "$NET_FILE" --seed 1 \
    --output-dir "$out" --sim-duration "$SIM_DURATION" --warmup 0 \
    --traci_step_length 0.1 --traffic_measure_radius_m 500.0 \
    --extension_increment 5.0 --max_extensions 3 \
    --metric-sample-interval 10 --vehicle-sample-interval 10 \
    --progress-interval 30 --phase-aligned-ped-depart \
    --ped-repeat-count 5 --ped-repeat-spacing-sec 2 \
    --include-vehicles --output-profile "$OUTPUT_PROFILE" \
    --manifest-row-role "$role" \
    --manifest-crosswalk-id "MULTI_SMART_$(echo "${SELECTED_IDS[*]}" | tr ' ' '+')" \
    >>"$log" 2>&1; then
    echo "  ✗ FAILED: $label  (log: $log)" >&2
    tail -5 "$log" >&2; return 1
  fi
  verify_run_success "$out" || { echo "  ✗ verify failed: $label" >&2; return 1; }
  python3 -m smart_crosswalk_sumo.generate_reports \
    --figures_dir "$FIGURES_DIR" --output_dir "$out" \
    --candidates "$csv" --nets_dir "$NETS_DIR" \
    >>"$log" 2>&1 || echo "  ⚠ report gen failed (run ok): $label"
}

# ── 실행 ──────────────────────────────────────────────────────────────────────
RUN_LABEL="seed01_$(echo "${SELECTED_IDS[*]}" | tr ' ' '+')"
BASELINE_OUT="$RUN_ROOT/baseline/$RUN_LABEL"
SMART_OUT="$RUN_ROOT/multi_smart/$RUN_LABEL"
BASELINE_LOG="$LOG_ROOT/baseline_${RUN_LABEL}.log"
SMART_LOG="$LOG_ROOT/multi_smart_${RUN_LABEL}.log"

echo ""
BASELINE_OK=false
if [[ "$SMART_ONLY" == true ]]; then
  echo "[multi_smart smoke] baseline 건너뜀 (--smart-only)"
  [[ -f "$BASELINE_OUT/benchmark_timing.json" ]] && BASELINE_OK=true
else
  echo "[multi_smart smoke] baseline 실행 (1/2)"
  if run_sim "$MULTI_CSV" "$BASELINE_OUT" "$BASELINE_LOG" "baseline_placeholder" "baseline"; then
    BASELINE_OK=true
  fi
fi

echo ""
echo "[multi_smart smoke] multi-smart 실행 (2/2)"
echo "  ※ ${#SELECTED_IDS[@]}개 crossing 동시 smart: ${SELECTED_IDS[*]}"
SMART_OK=false
if run_sim "$MULTI_CSV" "$SMART_OUT" "$SMART_LOG" "smart_candidate" "multi_smart"; then
  SMART_OK=true
fi

if [[ "$BASELINE_OK" != true || "$SMART_OK" != true ]]; then
  FAILED_IDS=("${SELECTED_IDS[@]}")
else
  FAILED_IDS=()
fi

# ── 비교 CSV 생성 ─────────────────────────────────────────────────────────────
echo ""
echo "[multi_smart smoke] 비교 CSV 생성 중..."
COMPARISON_CSV="$RESULTS_DIR/multi_smart_smoke_comparison.csv"
write_comparison_csv "$BASELINE_OUT" "$SMART_OUT" "$COMPARISON_CSV" \
  "${REQUESTED_IDS[*]}" "${EXCLUDED_IDS[*]:-}" "${SELECTED_IDS[*]}" \
  "${FAILED_IDS[*]:-}" "$BASELINE_OK" "$SMART_OK" \
  "$(
    if [[ "$BASELINE_OK" != true ]]; then
      echo "baseline failed"
    elif [[ "$SMART_OK" != true ]]; then
      echo "smart failed"
    else
      echo "ok"
    fi
  )"

echo ""
echo "✅ multi-smart smoke 완료"
echo "   결과: $COMPARISON_CSV"
echo "   baseline: $BASELINE_OUT"
echo "   multi-smart: $SMART_OUT"
