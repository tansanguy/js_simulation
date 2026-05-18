#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PIPELINE_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

bash "$PIPELINE_ROOT/commands/command_to_run_30seed_current_main_12.sh"
bash "$PIPELINE_ROOT/commands/command_to_run_30seed_signal_fix_9.sh"
bash "$PIPELINE_ROOT/commands/command_to_run_30seed_generated_signal_7.sh"
echo "skip p1_p4_recovery_6 in all-groups run; use command_to_run_30seed_p1_p4_recovery_6.sh explicitly if needed"
