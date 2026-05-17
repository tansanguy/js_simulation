#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PIPELINE_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
PROJECT_ROOT="$(cd "$PIPELINE_ROOT/../../.." && pwd)"
RESULT_ROOT="$PROJECT_ROOT/result"
export PYTHONPATH="$PROJECT_ROOT:${PYTHONPATH:-}"

if [[ "${APPLY:-0}" != "1" ]]; then
  echo "Dry-run only. Set APPLY=1 to archive safe files."
  exit 0
fi

python3 -m smart_crosswalk_sumo.reporting.aggregate_30seed_results cleanup-apply --result-root "$RESULT_ROOT" --pipeline-root "$PIPELINE_ROOT"
