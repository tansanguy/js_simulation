#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

cd "$PROJECT_ROOT"
SUMO_PYTHONPATH=""
if [[ -n "${SUMO_HOME:-}" ]]; then
  for candidate in "$SUMO_HOME/share/sumo/tools" "$SUMO_HOME/tools"; do
    if [[ -d "$candidate" ]]; then
      if [[ -n "$SUMO_PYTHONPATH" ]]; then
        SUMO_PYTHONPATH="$SUMO_PYTHONPATH:$candidate"
      else
        SUMO_PYTHONPATH="$candidate"
      fi
    fi
  done
fi
if [[ -n "$SUMO_PYTHONPATH" ]]; then
  export PYTHONPATH="$SUMO_PYTHONPATH:$PROJECT_ROOT:${PYTHONPATH:-}"
else
  export PYTHONPATH="$PROJECT_ROOT:${PYTHONPATH:-}"
fi

python3 -m smart_crosswalk_sumo.reporting.simple_final_pipeline "$@"
