#!/usr/bin/env bash
set -euo pipefail

cd /Users/junlee/Desktop/2026-1/js

echo "[WARNING] Run this only after integrity audit PASS and manual review of HIGH_RISK items."
echo "[WARNING] baseline must be run separately for current net, signal_fix_net_v1, and generated_signal_net_v1."
echo "[WARNING] generated_signal_net_v1 is NEW_SIGNAL_INSTALLATION expansion net; keep it separate from main default net."
echo

echo "To execute 30-seed command manually, run:"
echo "bash result/phase_next_top50_max_installation_recovery_20260516_002620/command_to_run_next_30seed_signal_and_generated.sh"
