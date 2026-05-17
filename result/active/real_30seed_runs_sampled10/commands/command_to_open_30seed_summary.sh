#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PIPELINE_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

if command -v open >/dev/null 2>&1; then
  OPEN_CMD=(open)
elif command -v xdg-open >/dev/null 2>&1; then
  OPEN_CMD=(xdg-open)
elif command -v python3 >/dev/null 2>&1; then
  OPEN_CMD=(python3 -m webbrowser)
else
  OPEN_CMD=()
fi

if [[ ${#OPEN_CMD[@]} -gt 0 ]]; then
  "${OPEN_CMD[@]}" "$PIPELINE_ROOT/README.md" || true
  "${OPEN_CMD[@]}" "$PIPELINE_ROOT/csv/seed30_group_summary.csv" || true
else
  echo "$PIPELINE_ROOT/README.md"
  echo "$PIPELINE_ROOT/csv/seed30_group_summary.csv"
fi
