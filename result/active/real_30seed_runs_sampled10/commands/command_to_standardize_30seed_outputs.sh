#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../../../.." && pwd)"
RESULT_ROOT="$PROJECT_ROOT/result"
ACTIVE_ROOT="$RESULT_ROOT/active"
PIPELINE_ROOT="$ACTIVE_ROOT/real_30seed_runs_sampled10"
export PYTHONPATH="$PROJECT_ROOT:${PYTHONPATH:-}"
if [[ -z "${SUMO_HOME:-}" ]]; then
  if command -v sumo >/dev/null 2>&1; then
    :
  elif [[ -d "/Library/Frameworks/EclipseSUMO.framework/Versions/1.26.0/EclipseSUMO" ]]; then
    export SUMO_HOME="/Library/Frameworks/EclipseSUMO.framework/Versions/1.26.0/EclipseSUMO"
    export PATH="$PROJECT_ROOT/.venv/bin:$SUMO_HOME/bin:$PATH"
    export PROJ_LIB="$SUMO_HOME/framework/EclipseSUMO.framework/Resources/proj"
  else
    echo "SUMO_HOME is not set and sumo is not on PATH" >&2
    exit 1
  fi
else
  export PATH="$PROJECT_ROOT/.venv/bin:$SUMO_HOME/bin:$PATH"
  if [[ -z "${PROJ_LIB:-}" && -d "$SUMO_HOME/framework/EclipseSUMO.framework/Resources/proj" ]]; then
    export PROJ_LIB="$SUMO_HOME/framework/EclipseSUMO.framework/Resources/proj"
  fi
fi

python3 -m smart_crosswalk_sumo.reporting.aggregate_30seed_results aggregate --pipeline-root "$PIPELINE_ROOT" --copy-xml --copy-logs
