# Current Status

Current source of truth for live pipeline state.

Status key:
- `pass` = checked and consistent with current code or artifact state
- `partial` = some evidence exists, but the claim is not fully proven end-to-end
- `not_checked` = not verified in the current evidence set
- `deprecated` = legacy or conflicting wording, do not use as current truth

## Current Pipeline Status

| Axis | Status | Current evidence | What it means |
|---|---|---|---|
| Top50 candidate validation | pass | `risk_score` desc, tie `crosswalk_id` asc; `ROAD_RANK` not used | Top50 sorting rule is current |
| Candidate mapping / tier validation | pass | 34 smart targets are tracked in the 30seed lane | Current smart lane uses 34 candidates |
| Pedestrian demand implementation validation | partial | `daytime_high_1p2`, S-DoT first, imputed fallback, `final_pedestrian_600s = round(base * 1.2)` | Final end-to-end pedestrian demand still not fully proven |
| Pedestrian runtime observation validation | partial | `ped_crossing_person_count` is a runtime observation metric | Observation exists, but it does not prove demand implementation |
| Vehicle flow policy validation | pass | `main_realistic_stress`, `20,877 vph`, `3,480 / 600s`, passenger only | Current vehicle policy is implemented |
| Vehicle global coverage validation | pass | edge/grid coverage checks passed in checked artifact | Broad network coverage is validated, not uniform edge traffic |
| Baseline/smart same route/trip/net validation | pass | `sumocfg` parsing returns `true` / `false` / `not_checked` | Smart should differ only by signal policy |
| Sampled traffic report semantics | partial | `metric_sample_interval=10`, `vehicle_sample_interval=10` in the checked run | Sampled 10s is the official repeated experiment mode; exact `0/0` is audit-only for a few candidates |
| Trade-off scope definition | pass | `baseline_vs_smart_summary.csv` stays global-500m; `local_tradeoff_summary.csv` adds local approach / affected-route fields | Trade-off analysis must separate global dilution from local impact |
| Legacy `vehicle_count` label | deprecated | `vehicle_count` in legacy seed summaries is the toy queue arrival count, not route demand volume | Use `generated_vehicle_count`, `network_arrived_vehicles`, or `toy_queue_vehicle_arrivals` instead |
| Network provenance and representativeness validation | partial | Jung-gu boundary + about 1 km buffer experimental network | Current network is representative, not 1:1 identical to Jung-gu roads |
| Result/csv organization validation | pass | `result/active/real_30seed_runs/`, `result/active/pedestrian_assumption/`, `result/active/validation/` | Current artifact tree is organized |
| Runtime optimization rollout | partial | runtime optimization options added, benchmark pending | Step-local caches and reuse flags are in place, but the benchmark still needs fresh measurement |

## Not Checked

| Item | Status | Why it stays here |
|---|---|---|
| Full final pedestrian demand end-to-end run | not_checked | Current checked artifact is smoke-level, not the final demand run |
| Uniform traffic on every edge | not_checked | Current vehicle validation checks coverage, not equal distribution |
| Full source OSM chain from smoke-run JSON alone | not_checked | Smoke-run metadata does not fully embed source provenance |
| Final experiment-scale vehicle policy effect | not_checked | Current checked artifact proves policy implementation, not final-scale effect |
| Exact traffic/safety final report for sampled run | not_checked | The checked run used sampled vehicle and metric intervals, so the exact final report still needs a rerun |

## Current Risks

| Risk | Why it matters | Current status |
|---|---|---|
| Pedestrian end-to-end proof gap | Runtime counts can be mistaken for demand validation | partial |
| Network representativeness overstatement | The network is experimental, not a cadastral copy | partial |
| Vehicle coverage overstatement | Coverage pass is not uniform-all-edges proof | pass, but easy to overclaim |
| Baseline/smart fairness drift | Different vehicle inputs would invalidate comparisons | pass, but must stay strict |

## Pre-Run Checklist

1. Keep pedestrian and vehicle lanes separate.
2. Keep baseline/smart same seed, same route/trip/net.
3. Use `demand_pedestrian.rou.xml` person counts for demand implementation checks.
4. Use edge/grid coverage for vehicle flow checks, not uniformity language.
5. Keep `ROAD_RANK` out of candidate selection and vehicle allocation language.
6. Keep current claims inside `pass` or `partial` only, never promote `partial` to `pass` without new evidence.
7. Treat sampled 10s runs as the official repeated experiment mode; exact `0/0` is audit-only.
8. Use local approach / affected-route trade-off metrics when judging vehicle cost, not only global 500m averages.

## Evidence Files

| File | Role |
|---|---|
| [`docs/final_scenario_implementation_check.md`](final_scenario_implementation_check.md) | Detailed implementation evidence log |
| [`docs/network_provenance_and_representativeness_check.md`](network_provenance_and_representativeness_check.md) | Network provenance evidence |
| [`docs/pedestrian_demand_implementation_validation_contract.md`](pedestrian_demand_implementation_validation_contract.md) | Pedestrian demand validation contract |
| [`docs/pedestrian_smoke_validation_contract.md`](pedestrian_smoke_validation_contract.md) | Pedestrian smoke validation contract |
| [`docs/vehicle_global_flow_and_stress_interpretation_check.md`](vehicle_global_flow_and_stress_interpretation_check.md) | Vehicle flow interpretation evidence |
