#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../../../.." && pwd)"
RESULT_ROOT="$PROJECT_ROOT/result"
ACTIVE_ROOT="$RESULT_ROOT/active"
PIPELINE_ROOT="$ACTIVE_ROOT/real_30seed_runs_sampled10"

bash "$PIPELINE_ROOT/commands/command_to_run_30seed_current_main_12.sh"
bash "$PIPELINE_ROOT/commands/command_to_run_30seed_signal_fix_9.sh"
bash "$PIPELINE_ROOT/commands/command_to_run_30seed_generated_signal_7.sh"
bash "$PIPELINE_ROOT/commands/command_to_run_30seed_p1_p4_recovery_6.sh"
