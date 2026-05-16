#!/usr/bin/env bash
set -euo pipefail

cd /Users/junlee/Desktop/2026-1/js

export SUMO_HOME="/Library/Frameworks/EclipseSUMO.framework/Versions/1.26.0/EclipseSUMO"
export PATH="$SUMO_HOME/bin:$PATH"
export PROJ_LIB="/Library/Frameworks/EclipseSUMO.framework/Versions/1.26.0/EclipseSUMO/framework/EclipseSUMO.framework/Resources/proj"
export PYTHONPATH="/Users/junlee/Desktop/2026-1/js:${PYTHONPATH:-}"

python3 -m smart_crosswalk_sumo.run_top50_recovery_integrity_audit

latest_dir="$(ls -dt result/phase_next_top50_recovery_integrity_audit_* 2>/dev/null | head -n 1 || true)"
if [[ -n "$latest_dir" ]]; then
  echo "[RESULT] $latest_dir"
else
  echo "[WARN] result folder not found"
fi
