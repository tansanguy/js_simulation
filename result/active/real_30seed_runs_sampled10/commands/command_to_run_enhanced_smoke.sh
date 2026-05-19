#!/usr/bin/env bash
# command_to_run_enhanced_smoke.sh
#
# Enhanced smoke pipeline — two modes:
#   random-1:  --seed -1  (random seed, all crosswalks, 1 run each)
#   full:      --seeds 1 2 3 ... (all seeds specified)
#
# Usage:
#   bash commands/command_to_run_enhanced_smoke.sh                      # random-1, current_main_12
#   bash commands/command_to_run_enhanced_smoke.sh --group signal_fix_9
#   bash commands/command_to_run_enhanced_smoke.sh --seeds 1 2 3       # full, 3 seeds
#   bash commands/command_to_run_enhanced_smoke.sh --seeds 1 2 3 --group generated_signal_7
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PIPELINE_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
PROJECT_ROOT="$(cd "$PIPELINE_ROOT/../../.." && pwd)"
ACTIVE_ROOT="$PROJECT_ROOT/result/active"
NETS_DIR="$ACTIVE_ROOT/nets"
export PYTHONPATH="$PROJECT_ROOT:${PYTHONPATH:-}"

# ── SUMO / venv setup ────────────────────────────────────────────────────────
if [[ -z "${SUMO_HOME:-}" ]]; then
  command -v sumo >/dev/null 2>&1 || { echo "SUMO_HOME not set and sumo not on PATH" >&2; exit 1; }
else
  export PATH="$SUMO_HOME/bin:$PATH"
  if [[ -z "${PROJ_LIB:-}" && -d "$SUMO_HOME/framework/EclipseSUMO.framework/Resources/proj" ]]; then
    export PROJ_LIB="$SUMO_HOME/framework/EclipseSUMO.framework/Resources/proj"
  fi
fi
[[ -d "$PROJECT_ROOT/.venv/bin" ]] && export PATH="$PROJECT_ROOT/.venv/bin:$PATH"

# ── defaults ──────────────────────────────────────────────────────────────────
GROUP="current_main_12"
SEED_ARGS=(--seed -1)     # random-1 by default
PED_COUNT=20
ELDERLY_RATIO=0.2
SIM_DURATION=120
CROSSWALK_ID=""

# ── argument parsing ──────────────────────────────────────────────────────────
POSITIONAL=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --group)         GROUP="$2";           shift 2 ;;
    --ped-count)     PED_COUNT="$2";       shift 2 ;;
    --elderly-ratio) ELDERLY_RATIO="$2";   shift 2 ;;
    --sim-duration)  SIM_DURATION="$2";    shift 2 ;;
    --crosswalk-id)  CROSSWALK_ID="$2";    shift 2 ;;
    --seeds)
      SEED_ARGS=(--seeds)
      shift
      while [[ $# -gt 0 && "$1" =~ ^[0-9]+$ ]]; do
        SEED_ARGS+=("$1")
        shift
      done
      ;;
    *) POSITIONAL+=("$1"); shift ;;
  esac
done

CANDIDATE_CSV="$PIPELINE_ROOT/manifests/${GROUP}_candidates.csv"
NET_FILE="$NETS_DIR/${GROUP}.net.xml"
OUTPUT_DIR="$PIPELINE_ROOT/runs_enhanced/${GROUP}"

if [[ ! -f "$CANDIDATE_CSV" ]]; then
  echo "candidate CSV not found: $CANDIDATE_CSV" >&2; exit 1
fi
if [[ ! -f "$NET_FILE" ]]; then
  echo "net file not found: $NET_FILE" >&2; exit 1
fi

CW_ARGS=()
[[ -n "$CROSSWALK_ID" ]] && CW_ARGS=(--crosswalk-id "$CROSSWALK_ID")

echo "[enhanced smoke] group=$GROUP  crosswalk=${CROSSWALK_ID:-all}  seed_args=${SEED_ARGS[*]}  ped_count=$PED_COUNT  elderly_ratio=$ELDERLY_RATIO"

python3 -m smart_crosswalk_sumo.run_enhanced_smoke_group \
  --candidate-csv "$CANDIDATE_CSV" \
  --net-file      "$NET_FILE" \
  --output-dir    "$OUTPUT_DIR" \
  --ped-count     "$PED_COUNT" \
  --elderly-ratio "$ELDERLY_RATIO" \
  --sim-duration  "$SIM_DURATION" \
  "${SEED_ARGS[@]}" \
  ${CW_ARGS[@]+"${CW_ARGS[@]}"}

echo "[enhanced smoke] done → $OUTPUT_DIR/enhanced_results.xlsx"
