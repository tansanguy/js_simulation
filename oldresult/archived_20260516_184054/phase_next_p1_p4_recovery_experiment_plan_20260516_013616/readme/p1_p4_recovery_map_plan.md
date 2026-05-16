# P1 + P4 recovery experiment map plan

- reason split: default 28 ready candidates are already split into `current_main_12`, `signal_fix_9`, `generated_signal_7`. Do not merge them into new map.
- P1 path: reuse existing `remaining_p1_signal_fix_net_v1.net.xml` and its candidate table. Do not mark ready before seed1 smoke.
- P4 path: manual only. Real point first. current SUMO point only reference. generated point only auxiliary.
- P4 approval gate: `review_status=APPROVED` + non-empty `selected_crossing_edges` + non-empty `selected_node_id` + non-empty `selected_tls_id` or explicit new-signal intent.
- P4 net build: plain XML patch + netconvert only. No direct net.xml editing.
- P4 after approval: build separate P4 manual recovery net first, then use approved P4 on top of P1 net for combined P1+P4 experiment.
- seed1 smoke: not ready until seed1 smoke command completes successfully.
- 30 seed: only after seed1 smoke and separate baseline built from P1+P4 recovery net.
- baseline: separate baseline per experiment net. Do not reuse baseline from `current_main_12`, `signal_fix_9`, or `generated_signal_7`.
- generated/new signal candidates: keep separate from main experiment.

## Inputs
- P1 candidate table rows: 5
- P4 candidate rows: 13
- base net: `/Users/junlee/Desktop/2026-1/js/result/phase_next_recovery_command_plan_20260514_200908/batch_03_run/recovery_tls_batch_network_v1.net.xml`
- P1 net: `/Users/junlee/Desktop/2026-1/js/result/phase_next_remaining22_p1_signal_fix_20260516_010953/remaining_p1_signal_fix_net_v1.net.xml`

## Outputs
- csv/manual_edge_pair_selection_template.csv
- csv/p4_nearby_edge_tls_candidates.csv
- html/p4_manual_approval_review_map.html
- readme/p1_p4_recovery_map_plan.md
- readme/p4_manual_approval_guide.md

