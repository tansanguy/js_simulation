#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PIPELINE_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
PROJECT_ROOT="$(cd "$PIPELINE_ROOT/../../.." && pwd)"
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

python3 -m smart_crosswalk_sumo.reporting.aggregate_30seed_results aggregate --pipeline-root "$PIPELINE_ROOT" --copy-xml --copy-logs
