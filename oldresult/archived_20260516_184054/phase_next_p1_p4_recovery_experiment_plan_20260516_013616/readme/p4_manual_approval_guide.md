# P4 manual approval guide

1. Open HTML map from `html/p4_manual_approval_review_map.html`.
2. Judge by real point first. current SUMO mapped point can be wrong.
3. generated point, if any, is only secondary hint.
4. Choose `selected_node_id` from nearby nodes near real point.
5. Choose `selected_tls_id` from nearest TLS or exact node TLS when applicable.
6. Choose `selected_from_edge` and `selected_to_edge` as actual road pair crossed by pedestrian crossing.
7. Write `selected_crossing_edges` as pair text, usually two edge ids separated by space.
8. `manual_strategy`:
   - `EXISTING_CROSSING_REMAP` for existing crossing re-anchor.
   - `MANUAL_CROSSING_ADD` for new crossing geometry.
   - `EXISTING_TLS_RETROFIT` for existing TLS phase fix.
   - `NEW_SIGNAL_INSTALLATION` only when new traffic light install is necessary.
   - `HOLD` for uncertain or location-too-far cases.
   - `REJECT` for out-of-scope cases.
9. `confidence`: `HIGH`, `MEDIUM`, `LOW`.
10. `review_status`: `APPROVED`, `NEEDS_MORE_REVIEW`, `HOLD`, `REJECT`.
11. `APPROVED` does not mean ready. Seed1 smoke must pass first.
12. Do not put P4 into 30-seed until seed1 smoke passes.

