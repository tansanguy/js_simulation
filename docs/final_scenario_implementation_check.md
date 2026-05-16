# Final Scenario Implementation Check

Current evidence log, not a claim of final completion. See `docs/current_status.md` for live status.

## 1. Executive Summary

The pipeline is partially aligned with the final experiment design.

- Top50 candidate selection is implemented and ordered correctly.
- Vehicle demand for `main_realistic_stress` is implemented and validation runs cleanly.
- Baseline/smart input equality is checked by parsing `sumocfg` files, and the current validation output says the vehicle route, trip, and net inputs match.
- Pedestrian demand source files for `daytime_high_1p2` exist, but the current smoke-style artifact is not a final pedestrian demand run; it is a vehicle-policy smoke artifact with pedestrian validation attached afterward.
- `scripts/validate_demand_policy.py` works as a validation-only entry point and does not rerun SUMO.

Main gap:
- The current checked smoke artifact does not prove the final pedestrian scenario end-to-end by itself. The pedestrian validation path is implemented, but the available run is not the final demand run.

## 2. Final Scenario Checklist

- Top50 selection rule: implemented and documented.
- Pedestrian demand scenario: source exists, but the checked run is not the final pedestrian demand run.
- Vehicle demand scenario: implemented.
- Baseline/smart fairness: implemented and checked from generated inputs.
- Validation execution: implemented and working as a post-run step.

## 3. Top50 Candidate Check

Status: pass.

Evidence:
- [`result/active/pedestrian_assumption/top50_candidate_selection_report.md`](../result/active/pedestrian_assumption/top50_candidate_selection_report.md)
- [`result/active/pedestrian_assumption/top50_mapped_candidates_daytime_high_1p2.csv`](../result/active/pedestrian_assumption/top50_mapped_candidates_daytime_high_1p2.csv)

Observed:
- Selection rule is `risk_score` descending, then `crosswalk_id` ascending.
- `ROAD_RANK` is not used for ranking.
- Selected rows: `50` from `674` total rows.
- Mapped candidate file has `50` rows.
- Sorted order check passed.
- Tie-break check on `crosswalk_id` ascending passed.
- `ped_source` missing: `0`.
- `scenario_name` missing in the pedestrian assumption CSV: `0`.
- `final_pedestrian_600s = round(base_pedestrian_600s * 1.2)` mismatches: `0`.
- `living_population_imputed` rows: `17`.
- `동화동` is present in the mapped selection output.

Partial note:
- `top50_mapped_candidates_daytime_high_1p2.csv` is a selection artifact. It does not itself carry the final demand columns; those live in the pedestrian assumption CSV.

## 4. Pedestrian Demand Implementation Check

Status: partial.

Evidence:
- [`result/active/pedestrian_assumption/crosswalk_pedestrian_assumptions_top50_daytime_high_1p2.csv`](../result/active/pedestrian_assumption/crosswalk_pedestrian_assumptions_top50_daytime_high_1p2.csv)
- [`result/active/pedestrian_assumption/crosswalk_pedestrian_assumptions_daytime_high_1p2.csv`](../result/active/pedestrian_assumption/crosswalk_pedestrian_assumptions_daytime_high_1p2.csv)
- [`result/active/real_30seed_runs/manifests/candidate_metadata.csv`](../result/active/real_30seed_runs/manifests/candidate_metadata.csv)
- [`result/vehicle_policy_smoke_1seed/outputs/demand_validation_summary.csv`](../result/vehicle_policy_smoke_1seed/outputs/demand_validation_summary.csv)

Observed:
- `daytime_high_1p2` is present in the pedestrian assumption CSV.
- The pedestrian source CSVs contain `dong_name`, `admin_dong`, `base_pedestrian_600s`, `final_pedestrian_600s`, `ped_source`, and `scenario_name`.
- `ped_source` missing: `0`.
- `scenario_name` missing: `0`.
- `final_pedestrian_600s = round(base_pedestrian_600s * 1.2)` mismatches: `0`.
- `candidate_metadata.csv` preserves `crosswalk_id -> dong_name/admin_dong` mapping for the 30-seed pipeline.
- `candidate_metadata.csv` has `34` smart-target rows and `34` unique `crosswalk_id`s.

Validation behavior:
- The validation wrapper uses XML person count and demand source count, not runtime crossing count, as the demand implementation check.
- The pedestrian validation note says the pedestrian trip XML is not produced by the pipeline, so that field is `not_applicable`.
- In the current `vehicle_policy_smoke_1seed` validation output, pedestrian validation status is `warning` with reason `pedestrian_trip_not_applicable`.

Why partial:
- The checked artifact is a vehicle-policy smoke run, not a full final pedestrian demand run for the final experiment scenario.

## 5. Vehicle Demand Implementation Check

Status: pass.

Evidence:
- [`result/vehicle_policy_smoke_1seed/run_metadata.json`](../result/vehicle_policy_smoke_1seed/run_metadata.json)
- [`result/vehicle_policy_smoke_1seed/outputs/demand_params.csv`](../result/vehicle_policy_smoke_1seed/outputs/demand_params.csv)
- [`result/vehicle_policy_smoke_1seed/outputs/road_group_allocation_summary.csv`](../result/vehicle_policy_smoke_1seed/outputs/road_group_allocation_summary.csv)
- [`result/vehicle_policy_smoke_1seed/outputs/vehicle_route_generation_audit.csv`](../result/vehicle_policy_smoke_1seed/outputs/vehicle_route_generation_audit.csv)
- [`result/vehicle_policy_smoke_1seed/sumo_nets/cw_NODE_5837/routes_seed42.rou.xml`](../result/vehicle_policy_smoke_1seed/sumo_nets/cw_NODE_5837/routes_seed42.rou.xml)
- [`result/vehicle_policy_smoke_1seed/sumo_nets/cw_NODE_5837/routes_seed42.trips.xml`](../result/vehicle_policy_smoke_1seed/sumo_nets/cw_NODE_5837/routes_seed42.trips.xml)

Observed:
- `scenario_name = main_realistic_stress`.
- `total_vehicle_flow_vph = 20877`.
- `total_vehicle_count_600s = 3480`.
- `vehicle_type = passenger`.
- `passenger_ratio = 1.0`.
- `allocation_basis = road_06_22_volume_share`.
- `veh_source = policy_fixed_road_06_22_share`.
- `run_metadata.json` says `skip_run = true` and `validate_demand = false`, so this is validation evidence only, not a new simulation.
- `routes_seed42.rou.xml` has `3480` trip/vehicle entries, all `passenger`.
- `routes_seed42.trips.xml` has `3480` trip entries, all `passenger`.
- `road_group_allocation_summary.csv` has `12` road-group rows.
- Road-group allocation is preserved as policy metadata, while actual route generation is full-network `randomTrips.py` output.

## 6. Baseline/Smart Fairness Check

Status: pass.

Evidence:
- [`smart_crosswalk_sumo/vehicle_demand_policy.py`](../smart_crosswalk_sumo/vehicle_demand_policy.py)
- [`smart_crosswalk_sumo/demand_validation.py`](../smart_crosswalk_sumo/demand_validation.py)
- [`result/vehicle_policy_smoke_1seed/outputs/demand_validation_summary.csv`](../result/vehicle_policy_smoke_1seed/outputs/demand_validation_summary.csv)
- [`result/vehicle_policy_smoke_1seed/sumo_nets/cw_NODE_5837/baseline_seed42.sumocfg`](../result/vehicle_policy_smoke_1seed/sumo_nets/cw_NODE_5837/baseline_seed42.sumocfg)
- [`result/vehicle_policy_smoke_1seed/sumo_nets/cw_NODE_5837/smart_seed42.sumocfg`](../result/vehicle_policy_smoke_1seed/sumo_nets/cw_NODE_5837/smart_seed42.sumocfg)

Observed:
- `compare_sumocfg_vehicle_inputs(...)` parses the generated `sumocfg` files.
- It returns `true`, `false`, or `not_checked`; it is not hardcoded to `true`.
- Current validation output says:
  - baseline/smart same route file: `true`
  - baseline/smart same trip file: `true`
  - baseline/smart same net file: `true`
- Route and trip inputs in the checked artifact point to the same generated files, and the same `network.net.xml`.

## 7. Demand Validation Execution Check

Status: pass.

Evidence:
- [`scripts/validate_demand_policy.py`](../scripts/validate_demand_policy.py)
- [`result/vehicle_policy_smoke_1seed/outputs/demand_validation_summary.csv`](../result/vehicle_policy_smoke_1seed/outputs/demand_validation_summary.csv)
- [`result/vehicle_policy_smoke_1seed/outputs/pedestrian_flow_policy_validation.csv`](../result/vehicle_policy_smoke_1seed/outputs/pedestrian_flow_policy_validation.csv)
- [`result/vehicle_policy_smoke_1seed/outputs/vehicle_flow_policy_validation.csv`](../result/vehicle_policy_smoke_1seed/outputs/vehicle_flow_policy_validation.csv)
- [`result/vehicle_policy_smoke_1seed/outputs/vehicle_global_coverage_validation.csv`](../result/vehicle_policy_smoke_1seed/outputs/vehicle_global_coverage_validation.csv)
- [`result/vehicle_policy_smoke_1seed/outputs/demand_validation_report.md`](../result/vehicle_policy_smoke_1seed/outputs/demand_validation_report.md)

Observed:
- The wrapper only calls `validate_demand_run(...)`.
- The command is validation-only.
- It produced all expected outputs.
- Current summary status:
  - pedestrian flow validation: `warning`
  - vehicle flow validation: `pass`
  - vehicle global coverage validation: `pass`

Pedestrian warning reason:
- `pedestrian_trip_not_applicable`

## 8. Missing / Partial / Broken Items

- The checked smoke artifact is not the final pedestrian demand run for `daytime_high_1p2`.
- The pedestrian validation path is implemented, but the current artifact only proves demand validation on the smoke artifact and not the full final pedestrian pipeline.
- `top50_mapped_candidates_daytime_high_1p2.csv` is a selection artifact, not a final demand contract file.
- `run_metadata.json` shows `validate_demand = false`, so the original smoke run did not include demand validation inline.
- Pedestrian validation has no generated pedestrian trip XML in this pipeline, so that part stays `not_applicable`.

## 9. Exact Commands to Re-run Validation Only

```bash
python3 scripts/validate_demand_policy.py --run_dir result/vehicle_policy_smoke_1seed
```

This command is validation-only. It does not rerun SUMO.

## 10. Final Pass/Partial/Fail Table

| check_item | expected_policy | implementation_status | evidence_file | evidence_value | required_fix_or_next_action |
|---|---|---:|---|---|---|
| Top50 selection rule | `risk_score` desc, tie `crosswalk_id` asc; `ROAD_RANK` not used | pass | [`top50_candidate_selection_report.md`](../result/active/pedestrian_assumption/top50_candidate_selection_report.md) | `Selected rows: 50 from 674 total rows` | None |
| Top50 mapped file shape | 50 mapped candidates | pass | [`top50_mapped_candidates_daytime_high_1p2.csv`](../result/active/pedestrian_assumption/top50_mapped_candidates_daytime_high_1p2.csv) | `50 rows`; sort check passed | None |
| Ped source completeness | `daytime_high_1p2` present; `ped_source` / `scenario_name` preserved | pass | [`crosswalk_pedestrian_assumptions_top50_daytime_high_1p2.csv`](../result/active/pedestrian_assumption/crosswalk_pedestrian_assumptions_top50_daytime_high_1p2.csv) | `ped_source missing=0`, `scenario_name missing=0`, formula mismatches `0` | None |
| Dong traceability | `crosswalk_id -> dong_name/admin_dong` preserved in 30-seed metadata | pass | [`candidate_metadata.csv`](../result/active/real_30seed_runs/manifests/candidate_metadata.csv) | `34` smart targets; `34` unique IDs | None |
| Final pedestrian demand run | final `daytime_high_1p2` 600s demand checked end-to-end | partial | [`demand_validation_summary.csv`](../result/vehicle_policy_smoke_1seed/outputs/demand_validation_summary.csv) | pedestrian validation is `warning` and current artifact is not a final pedestrian run | Run the final pedestrian demand pipeline artifact, then re-run validation only |
| Vehicle demand policy | `main_realistic_stress`, `20877 vph`, `3480 / 600s`, passenger only, `road_06_22_volume_share` | pass | [`demand_params.csv`](../result/vehicle_policy_smoke_1seed/outputs/demand_params.csv) | `20877`, `3480`, `passenger`, `1.0`, `road_06_22_volume_share` | None |
| Vehicle route generation | passenger only, 3480 vehicles, no taxi/bus/truck | pass | [`routes_seed42.rou.xml`](../result/vehicle_policy_smoke_1seed/sumo_nets/cw_NODE_5837/routes_seed42.rou.xml) | `3480` entries, all `passenger` | None |
| Vehicle trip generation | same count as route file | pass | [`routes_seed42.trips.xml`](../result/vehicle_policy_smoke_1seed/sumo_nets/cw_NODE_5837/routes_seed42.trips.xml) | `3480` entries, all `passenger` | None |
| Road-group metadata | 12-group allocation preserved as metadata, route generation stays full-network | pass | [`road_group_allocation_summary.csv`](../result/vehicle_policy_smoke_1seed/outputs/road_group_allocation_summary.csv) | `12` road-group rows | None |
| Baseline/smart input equality | same route, trip, and net inputs for both scenarios | pass | [`demand_validation_summary.csv`](../result/vehicle_policy_smoke_1seed/outputs/demand_validation_summary.csv) | route/trip/net all `true` | None |
| Validation execution | validation-only command generates CSV/MD outputs | pass | [`scripts/validate_demand_policy.py`](../scripts/validate_demand_policy.py) | produced `demand_validation_summary.csv`, `pedestrian_flow_policy_validation.csv`, `vehicle_flow_policy_validation.csv`, `vehicle_global_coverage_validation.csv`, `demand_validation_report.md` | None |
