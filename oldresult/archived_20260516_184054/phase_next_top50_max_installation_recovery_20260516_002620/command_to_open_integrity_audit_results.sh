#!/usr/bin/env bash
set -euo pipefail

cd /Users/junlee/Desktop/2026-1/js

latest_dir="$(ls -dt result/phase_next_top50_recovery_integrity_audit_* 2>/dev/null | head -n 1 || true)"
if [[ -z "$latest_dir" ]]; then
  echo "[ERROR] no integrity audit result folder found"
  exit 1
fi

echo "[LATEST] $latest_dir"

summary_md="$latest_dir/signal_integrity_audit_summary.md"
decision_md="$latest_dir/default_network_promotion_decision.md"
roadmap_md="$latest_dir/remaining22_recovery_roadmap.md"

if [[ -f "$summary_md" ]]; then open "$summary_md"; fi
if [[ -f "$decision_md" ]]; then open "$decision_md"; fi
if [[ -f "$roadmap_md" ]]; then open "$roadmap_md"; fi

echo "[CSV] $latest_dir/signal_fix_seed1_smoke_validation_check.csv"
echo "[CSV] $latest_dir/generated_signal_seed1_smoke_validation_check.csv"
echo "[CSV] $latest_dir/network_integrity_comparison.csv"
echo "[CSV] $latest_dir/tls_diff_summary.csv"
echo "[CSV] $latest_dir/tls_changed_detail.csv"
echo "[CSV] $latest_dir/main12_preservation_on_signal_fix_net.csv"
echo "[CSV] $latest_dir/main12_preservation_on_generated_signal_net.csv"
echo "[CSV] $latest_dir/remaining22_recovery_diagnosis.csv"
