#!/usr/bin/env bash
# verify_top7_runs.sh
# top7_sim 실행 완료 여부 점검
#
# 사용법:
#   cd /Users/junlee/Desktop/2026-1/js
#   bash final/top7_sim/commands/verify_top7_runs.sh
#   bash final/top7_sim/commands/verify_top7_runs.sh --summary  # 요약만 출력
#   bash final/top7_sim/commands/verify_top7_runs.sh --summary --seeds 1-10

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUN_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)/runs"

SUMMARY_ONLY=false
SEEDS_SPEC="1-30"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --summary)
      SUMMARY_ONLY=true
      shift ;;
    --seeds)
      shift
      if [[ $# -eq 0 || "$1" == --* ]]; then
        echo "--seeds requires a value" >&2
        exit 1
      fi
      SEEDS_SPEC="$1"
      shift ;;
    --seeds=*)
      SEEDS_SPEC="${1#*=}"
      shift ;;
    *)
      echo "Unknown option: $1" >&2
      exit 1 ;;
  esac
done

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

SMART_IDS=(
  "NODE_8369"
  "LINK_239754"
  "NODE_5831"
  "NODE_5846"
  "NODE_125895"
  "LINK_194891"
  "NODE_10262"
)

ok_total=0; fail_total=0; missing_total=0

check_seed() {
  local path="$1" label="$2"
  local bench="$path/benchmark_timing.json"
  if [[ ! -f "$bench" ]]; then
    $SUMMARY_ONLY || echo "  ✗ MISSING  $label"
    echo "missing"; return
  fi
  local success
  success=$(python3 -c "
import json, sys
d=json.load(open('$bench'))
print('ok' if d.get('run_success') and int(d.get('failed_cases_count',1))==0 else 'fail')
" 2>/dev/null || echo "fail")
  if [[ "$success" == "ok" ]]; then
    $SUMMARY_ONLY || echo "  ✓ OK       $label"
    echo "ok"
  else
    local reason
    reason=$(python3 -c "import json; d=json.load(open('$bench')); print(d.get('failure_reason','?'))" 2>/dev/null || echo "?")
    $SUMMARY_ONLY || echo "  ✗ FAILED   $label  ($reason)"
    echo "fail"
  fi
}

# ── baseline seeds ─────────────────────────────────────────────────────────────
expected_seed_count="${#SEEDS[@]}"
echo "=== baseline (${expected_seed_count} seed) ==="
b_ok=0; b_fail=0; b_miss=0
for raw_seed in "${SEEDS[@]}"; do
  seed="$(printf '%02d' "$raw_seed")"
  result=$(check_seed "$RUN_ROOT/baseline/seed${seed}" "baseline/seed${seed}")
  case "$result" in
    ok)      b_ok=$((b_ok+1));   ok_total=$((ok_total+1)) ;;
    fail)    b_fail=$((b_fail+1)); fail_total=$((fail_total+1)) ;;
    missing) b_miss=$((b_miss+1)); missing_total=$((missing_total+1)) ;;
  esac
done
echo "  → ✓${b_ok} / ✗${b_fail} / ?${b_miss}  (완료율: $((b_ok*100/expected_seed_count))%)"

# ── smart 7 × seeds ──────────────────────────────────────────────────────────
echo ""
echo "=== smart (${#SMART_IDS[@]}후보 × ${expected_seed_count}seed) ==="
for cw in "${SMART_IDS[@]}"; do
  s_ok=0; s_fail=0; s_miss=0
  for raw_seed in "${SEEDS[@]}"; do
    seed="$(printf '%02d' "$raw_seed")"
    result=$(check_seed "$RUN_ROOT/smart/${cw}/seed${seed}" "${cw}/seed${seed}")
    case "$result" in
      ok)      s_ok=$((s_ok+1));   ok_total=$((ok_total+1)) ;;
      fail)    s_fail=$((s_fail+1)); fail_total=$((fail_total+1)) ;;
      missing) s_miss=$((s_miss+1)); missing_total=$((missing_total+1)) ;;
    esac
  done
  rate=$((s_ok*100/expected_seed_count))
  echo "  ${cw}: ✓${s_ok} / ✗${s_fail} / ?${s_miss}  (완료율: ${rate}%)"
done

# ── 전체 요약 ─────────────────────────────────────────────────────────────────
total=$((expected_seed_count + ${#SMART_IDS[@]}*expected_seed_count))
echo ""
echo "=== 전체 요약 ==="
echo "  기대 총 run 수 : $total  (baseline ${expected_seed_count} + smart $((${#SMART_IDS[@]}*expected_seed_count)))"
echo "  ✓ 성공        : $ok_total"
echo "  ✗ 실패        : $fail_total"
echo "  ? 미실행      : $missing_total"
echo "  완료율        : $((ok_total*100/total))%"

# ── simulation_result.csv 빠른 확인 ──────────────────────────────────────────
echo ""
echo "=== benchmark_timing.json 수 ==="
find "$RUN_ROOT" -name benchmark_timing.json | wc -l | xargs echo "  파일 수:"
echo "  (기대: $total)"

echo ""
echo "=== 주요 후보 첫 번째 결과 샘플 ==="
for cw in "LINK_194891" "NODE_10262"; do
  f="$RUN_ROOT/smart/${cw}/seed01/simulation_result.csv"
  if [[ -f "$f" ]]; then
    python3 - "$f" "$cw" <<'PY'
import pandas as pd, sys
df = pd.read_csv(sys.argv[1])
cw = sys.argv[2]
cols = ['crosswalk_id','scenario','seed','extension_count','total_extension_sec',
        'network_avg_delay_sec','local_500m_avg_delay_sec','ped_wait_time_mean']
available = [c for c in cols if c in df.columns]
print(f"  {cw} seed01:")
print(df[available].head(4).to_string(index=False))
PY
  else
    echo "  $cw: seed01 결과 없음 (미실행)"
  fi
done
