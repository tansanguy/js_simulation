#!/usr/bin/env bash
set -euo pipefail
OUT="/Users/junlee/Desktop/2026-1/js/result/phase_next_top50_max_installation_recovery_20260516_002620"
run_batch() {
  local name="$1"
  local csv="$2"
  local net="$3"
  for seed in $(seq 1 30); do
    for scenario in smart baseline; do
      python3 -m smart_crosswalk_sumo.run_phase6_recovery_smoke         --candidate-csv "$csv"         --net-file "$net"         --scenario "$scenario"         --seed "$seed"         --sim-duration 600         --phase-aligned-ped-depart         --output-dir "$OUT/seed30_${name}/${scenario}_seed${seed}"
    done
  done
}
run_batch signal_fix "$OUT/signal_fix_candidate_table.csv" "$OUT/signal_fix_net_v1.net.xml"
run_batch generated_signal "$OUT/generated_signal_candidate_table.csv" "$OUT/generated_signal_net_v1.net.xml"
