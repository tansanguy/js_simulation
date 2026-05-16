#!/usr/bin/env bash
set -euo pipefail
cd "/Users/junlee/Desktop/2026-1/js"
CSV="/Users/junlee/Desktop/2026-1/js/result/phase_next_p1_p4_recovery_experiment_plan_20260516_013616/csv/p4_manual_recovery_candidate_table.csv"
NET="/Users/junlee/Desktop/2026-1/js/result/phase_next_p1_p4_recovery_experiment_plan_20260516_013616/p4_manual_recovery_net.net.xml"
echo "[manual run only] seed1 smoke not executed by Codex."
echo "python3 -m smart_crosswalk_sumo.run_phase6_recovery_smoke --candidate-csv "$CSV" --net-file "$NET" --scenario smart --seed 1 --sim-duration 300 --phase-aligned-ped-depart --output-dir "/Users/junlee/Desktop/2026-1/js/result/phase_next_p1_p4_recovery_experiment_plan_20260516_013616/patches/p4_manual_seed1_smart_smoke""
echo "python3 -m smart_crosswalk_sumo.run_phase6_recovery_smoke --candidate-csv "$CSV" --net-file "$NET" --scenario baseline --seed 1 --sim-duration 300 --phase-aligned-ped-depart --output-dir "/Users/junlee/Desktop/2026-1/js/result/phase_next_p1_p4_recovery_experiment_plan_20260516_013616/patches/p4_manual_seed1_baseline_smoke""
