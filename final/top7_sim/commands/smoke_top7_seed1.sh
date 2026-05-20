#!/usr/bin/env bash
# smoke_top7_seed1.sh
# top7 후보 seed1 smoke test
#
# 사용법:
#   bash final/top7_sim/commands/smoke_top7_seed1.sh                          # top7 전체
#   bash final/top7_sim/commands/smoke_top7_seed1.sh --force                  # 전체 재실행
#   bash final/top7_sim/commands/smoke_top7_seed1.sh --ids NODE_5846          # 단일 후보
#   bash final/top7_sim/commands/smoke_top7_seed1.sh --ids LINK_194891 NODE_10262  # 복수 후보
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
NET_FILE="$NETS_DIR/generated_signal_7.net.xml"
SIM_DURATION=600
OUTPUT_PROFILE=light
export PYTHONPATH="$PROJECT_ROOT:${PYTHONPATH:-}"

# ── 옵션 파싱 ─────────────────────────────────────────────────────────────────
FORCE=false
RAW_IDS=()   # --ids 뒤에 받은 원시 토큰들

while [[ $# -gt 0 ]]; do
  case "$1" in
    --force)
      FORCE=true; shift ;;
    --ids)
      shift
      while [[ $# -gt 0 && "$1" != --* ]]; do
        RAW_IDS+=("$1"); shift
      done ;;
    *)
      echo "Unknown option: $1" >&2; exit 1 ;;
  esac
done

# ── top7 마스터 목록 ──────────────────────────────────────────────────────────
ALL_TOP7=(
  "LINK_194891"
  "NODE_10262"
  "NODE_125895"
  "NODE_5831"
  "LINK_239754"
  "NODE_5846"
  "NODE_8369"
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

# ── SUMO 환경 ─────────────────────────────────────────────────────────────────
if [[ -z "${SUMO_HOME:-}" ]]; then
  command -v sumo >/dev/null 2>&1 || { echo "SUMO_HOME not set" >&2; exit 1; }
else
  export PATH="$SUMO_HOME/bin:$PATH"
  [[ -z "${PROJ_LIB:-}" && -d "$SUMO_HOME/framework/EclipseSUMO.framework/Resources/proj" ]] && \
    export PROJ_LIB="$SUMO_HOME/framework/EclipseSUMO.framework/Resources/proj"
fi
[[ -d "$PROJECT_ROOT/.venv/bin" ]] && export PATH="$PROJECT_ROOT/.venv/bin:$PATH"

mkdir -p "$RUN_ROOT" "$LOG_ROOT" "$FIGURES_DIR" "$TMP_DIR"
[[ -f "$NET_FILE"     ]] || { echo "net.xml 없음: $NET_FILE" >&2; exit 1; }
[[ -f "$BASELINE_CSV" ]] || {
  echo "baseline CSV 없음 → python3 analysis/make_top7_sim_candidates.py 먼저 실행" >&2
  exit 1
}

# ── 선택 로그 출력 ────────────────────────────────────────────────────────────
echo "[top7_sim smoke] selected_ids=${SELECTED_IDS[*]}"
echo "[top7_sim smoke] mode=${MODE}  force=${FORCE}"

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
  $FORCE && [[ -f "$log_file" ]] && : > "$log_file"

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

# ── preflight (선택 ID만) ─────────────────────────────────────────────────────
echo ""
echo "[top7_sim smoke] preflight 검증 중... (대상: ${SELECTED_IDS[*]})"
python3 - "$PROJECT_ROOT" "${SELECTED_IDS[@]}" <<'PY'
import sys
from pathlib import Path

PROJECT_ROOT = Path(sys.argv[1])
ids          = sys.argv[2:]
script       = PROJECT_ROOT / "analysis/make_top7_sim_candidates.py"

# verify-only 스크립트를 직접 임포트해서 선택 ID만 검사
import re, math
import xml.etree.ElementTree as ET
import pandas as pd
import sumolib

NET_PATH   = PROJECT_ROOT / "result/active/nets/generated_signal_7.net.xml"
SINGLE_DIR = PROJECT_ROOT / "final/top7_sim/manifests/single_candidates"
BASELINE_P = PROJECT_ROOT / "final/top7_sim/manifests/top7_baseline_candidates.csv"

net = sumolib.net.readNet(str(NET_PATH), withInternal=True)
tree = ET.parse(str(NET_PATH))
crossing_to_li = {}
for conn in tree.getroot().findall("connection"):
    to_e = conn.get("to",""); tl = conn.get("tl",""); li = conn.get("linkIndex","")
    if tl and li and re.match(r"^:.+_c\d+$", to_e):
        crossing_to_li[to_e] = int(li)

def check(cw_id, crossing_edge_id, ped_link_index, from_e, to_e, max_d=150.0):
    if not net.getEdge(crossing_edge_id):
        return False, f"crossing edge not found: {crossing_edge_id}"
    if ped_link_index not in crossing_to_li.values():
        return False, f"ped_link_index {ped_link_index} not in TLS connections"
    fe = net.getEdge(from_e) if from_e else None
    te = net.getEdge(to_e)   if to_e   else None
    if not fe: return False, f"route_from_edge not found: {from_e}"
    if not te: return False, f"route_to_edge not found: {to_e}"
    m = re.match(r"^:(.+)_c\d+$", crossing_edge_id)
    if not m: return False, "can't parse junction"
    junc = net.getNode(m.group(1))
    if not junc: return False, "junction not found"
    jx, jy = junc.getCoord()
    fn = fe.getToNode(); fx, fy = fn.getCoord()
    df = math.sqrt((fx-jx)**2+(fy-jy)**2)
    tn = te.getFromNode(); tx, ty = tn.getCoord()
    dt = math.sqrt((tx-jx)**2+(ty-jy)**2)
    if df > max_d: return False, f"route_from too far: {df:.1f}m"
    if dt > max_d: return False, f"route_to too far: {dt:.1f}m"
    return True, f"ok ({df:.1f}m / {dt:.1f}m)"

all_ok = True
for cw in ids:
    p = SINGLE_DIR / f"{cw}.csv"
    if not p.exists():
        print(f"  ✗ {cw}: CSV 없음"); all_ok = False; continue
    r = pd.read_csv(p).iloc[0]
    ok, msg = check(cw, str(r.get("crossing_edge_id","")),
                    int(r.get("ped_link_index",-1)),
                    str(r.get("route_from_edge","")),
                    str(r.get("route_to_edge","")))
    print(f"  {'✓' if ok else '✗'} {cw:15s}  {msg}")
    if not ok: all_ok = False

# baseline CSV 행 수 확인 (선택 ID 기준)
if BASELINE_P.exists():
    bdf  = pd.read_csv(BASELINE_P)
    have = set(bdf["crosswalk_id"].astype(str)) & set(ids)
    miss = set(ids) - set(bdf["crosswalk_id"].astype(str))
    if miss:
        print(f"  ⚠ baseline CSV에 없는 선택 ID: {miss}  (임시 CSV로 처리됨)")

sys.exit(0 if all_ok else 1)
PY
preflight_exit=$?
if [[ "$preflight_exit" -ne 0 ]]; then
  echo "❌ preflight 실패 — 재생성: python3 analysis/make_top7_sim_candidates.py" >&2
  exit 1
fi
echo ""

# ── baseline 실행 ─────────────────────────────────────────────────────────────
echo "[top7_sim smoke] baseline seed1 시작"
echo "  baseline_csv: $ACTIVE_BASELINE_CSV"
run_smoke "$ACTIVE_BASELINE_CSV" \
  "$RUN_ROOT/baseline/seed01" \
  "$LOG_ROOT/baseline/seed01.log" \
  "baseline_placeholder" "BASELINE_TOP7_SIM"
echo "  ✓ baseline 완료"

# ── smart 실행 (선택 ID만) ────────────────────────────────────────────────────
echo ""
echo "[top7_sim smoke] smart seed1 × ${#SELECTED_IDS[@]}후보 시작"
FAIL=0
for cw in "${SELECTED_IDS[@]}"; do
  csv="$SINGLE_CSV_ROOT/${cw}.csv"
  if [[ ! -f "$csv" ]]; then
    echo "  ✗ CSV 없음: $csv" >&2; FAIL=$((FAIL+1)); continue
  fi
  run_smoke "$csv" \
    "$RUN_ROOT/smart/${cw}/seed01" \
    "$LOG_ROOT/smart/${cw}/seed01.log" \
    "smart_candidate" "$cw" || FAIL=$((FAIL+1))
done

smoke_summary "$RUN_ROOT" "${SELECTED_IDS[@]}"

echo ""
if [[ "$FAIL" -eq 0 ]]; then
  if [[ "$MODE" == "all" ]]; then
    echo "✅ smoke 전체 통과 — run_top7_30seed.sh 실행 가능"
  else
    echo "✅ smoke 통과 (${SELECTED_IDS[*]})"
  fi
else
  echo "❌ ${FAIL}개 실패 — candidate CSV 및 로그 확인 필요"
  exit 1
fi
