# Vehicle Global Flow and Stress Interpretation Check

## 1. Executive Summary

The current artifacts support a narrow but real claim:

- `main_realistic_stress` is a scenario-wide vehicle demand policy.
- It is encoded as `20,877 vph` and `3,480 vehicles / 600s`.
- Vehicle routes are generated with `randomTrips.py` on the full valid network, not from candidate-only local buffers.
- The checked validation run passes whole-network coverage thresholds, but it does not prove uniform traffic everywhere.
- The 12 road groups are preserved as policy metadata, not as exact route-generation buckets.
- Baseline and smart share the same vehicle route, trip, and net inputs in the checked artifact.

What this does not prove:

- It does not prove that traffic is evenly spread across every edge.
- It does not prove that `20,877 vph` is an externally verified official Jung-gu total.
- It does not prove road-group exact allocation at the edge-routing level.

## 2. `main_realistic_stress` Interpretation

Status: partial.

Evidence:
- [`smart_crosswalk_sumo/vehicle_demand_policy.py`](../smart_crosswalk_sumo/vehicle_demand_policy.py)
- [`result/vehicle_policy_smoke_1seed/outputs/demand_params.csv`](../result/vehicle_policy_smoke_1seed/outputs/demand_params.csv)
- [`result/vehicle_policy_smoke_1seed/outputs/road_group_allocation_summary.csv`](../result/vehicle_policy_smoke_1seed/outputs/road_group_allocation_summary.csv)

Observed:
- `VEHICLE_POLICY_SCENARIO = "main_realistic_stress"`.
- `VEHICLE_POLICY_TOTAL_FLOW_VPH = 20877.0`.
- `VEHICLE_POLICY_TOTAL_COUNT_600S = 3480`.
- `VEHICLE_POLICY_VEHICLE_TYPE = passenger`.
- `VEHICLE_POLICY_PASSENGER_RATIO = 1.0`.
- `VEHICLE_POLICY_ALLOCATION_BASIS = road_06_22_volume_share`.
- `demand_params.csv` preserves:
  - `scenario_name`
  - `total_vehicle_flow_vph`
  - `total_vehicle_count_600s`
  - `vehicle_type`
  - `passenger_ratio`
  - `allocation_basis`
- `demand_params.csv` also preserves `edge_group_mapping_status = full_network_fallback`.

Interpretation:
- The code treats `main_realistic_stress` as one scenario-wide vehicle flow policy, not a candidate-local vehicle flow.
- The artifact does not independently prove the real-world meaning of `20,877 vph`; it only proves that the pipeline encodes and uses that value as the scenario policy total.

## 3. `20,877 vph` to `3,480 vehicles` Calculation

Status: pass.

Evidence:
- [`smart_crosswalk_sumo/vehicle_demand_policy.py`](../smart_crosswalk_sumo/vehicle_demand_policy.py)
- [`result/vehicle_policy_smoke_1seed/outputs/demand_params.csv`](../result/vehicle_policy_smoke_1seed/outputs/demand_params.csv)
- [`result/vehicle_policy_smoke_1seed/outputs/vehicle_flow_policy_validation.csv`](../result/vehicle_policy_smoke_1seed/outputs/vehicle_flow_policy_validation.csv)

Observed:
- The policy constant is `20877.0 vph`.
- `600s` conversion is consistent with `20877 * 600 / 3600 = 3479.5`, which rounds to `3480`.
- `demand_params.csv` stores `total_vehicle_flow_vph = 20877.0` and `total_vehicle_count_600s = 3480`.
- `vehicle_flow_policy_validation.csv` reports:
  - `expected_count = 3480`
  - `generated_count = 3480`
  - `count_diff = 0`

Conclusion:
- The code and output agree on the 600-second conversion.

## 4. Whole-map Vehicle Coverage Evidence

Status: partial.

Evidence:
- [`result/vehicle_policy_smoke_1seed/outputs/vehicle_global_coverage_validation.csv`](../result/vehicle_policy_smoke_1seed/outputs/vehicle_global_coverage_validation.csv)
- [`result/vehicle_policy_smoke_1seed/outputs/vehicle_route_generation_audit.csv`](../result/vehicle_policy_smoke_1seed/outputs/vehicle_route_generation_audit.csv)
- [`result/vehicle_policy_smoke_1seed/outputs/demand_validation_report.md`](../result/vehicle_policy_smoke_1seed/outputs/demand_validation_report.md)

Observed:
- `vehicle_global_coverage_validation.csv` status: `pass`.
- `reason` is empty.
- `coverage_ratio = 0.734694`.
- `total_driveable_edges = 24990`.
- `used_vehicle_edges = 18360`.
- `unique_depart_edges = 3236`.
- `unique_arrival_edges = 3226`.
- `unique_route_edges = 18360`.
- `candidate_buffer_vehicle_share = 0.000109`.
- `route_bbox_area_ratio = 0.851385`.
- `grid_cells_used = 16`.
- `grid_cells_total = 16`.
- `grid_coverage_ratio = 1.0`.

Pass logic:
- In code, global coverage warns when:
  - `coverage_ratio < 0.01`, or
  - `candidate_buffer_vehicle_share >= 0.5`, or
  - `grid_coverage_ratio < 0.25`.
- Current values are well above/below those thresholds, so the pass is real threshold-based coverage, not file existence only.

Interpretation:
- This is strong evidence for network-wide vehicle flow.
- It is not evidence for uniform traffic on every edge.
- It is broad background traffic, not candidate-local traffic.

## 5. `randomTrips` / `full_network_fallback` Interpretation

Status: pass.

Evidence:
- [`smart_crosswalk_sumo/generate_demand.py`](../smart_crosswalk_sumo/generate_demand.py)
- [`smart_crosswalk_sumo/vehicle_demand_policy.py`](../smart_crosswalk_sumo/vehicle_demand_policy.py)
- [`scripts/build_vehicle_flow_audit.py`](../scripts/build_vehicle_flow_audit.py)
- [`result/vehicle_policy_smoke_1seed/outputs/demand_validation_report.md`](../result/vehicle_policy_smoke_1seed/outputs/demand_validation_report.md)

Observed:
- `generate_demand.py` calls `randomTrips.py`.
- The call includes `--vehicle-class passenger`.
- The policy summary uses `edge_group_mapping_status = full_network_fallback`.
- The report text says the routes are generated from `randomTrips.py` on the full integrated network.
- The report also says the artifact is network-wide by generation method, but not dense enough to claim even distribution across all of Jung-gu.

Conclusion:
- It is safe to say the route generation is full-network passenger-only randomTrips-based background traffic.
- It is not safe to say it is evenly distributed everywhere.

## 6. Road-group Allocation: Metadata vs Actual Edge Routing

Status: pass.

Evidence:
- [`result/vehicle_policy_smoke_1seed/outputs/road_group_allocation_summary.csv`](../result/vehicle_policy_smoke_1seed/outputs/road_group_allocation_summary.csv)
- [`smart_crosswalk_sumo/vehicle_demand_policy.py`](../smart_crosswalk_sumo/vehicle_demand_policy.py)
- [`scripts/build_vehicle_flow_audit.py`](../scripts/build_vehicle_flow_audit.py)

Observed:
- `road_group_allocation_summary.csv` has `12` road-group rows.
- The row values are `A-10, A-17, A-13, A-11, A-09, A-16, A-18, A-12, A-21, A-19, A-23, A-24`.
- The code says road-group allocation is preserved as policy metadata.
- The code says actual route generation remains network-wide `randomTrips.py` output.
- The audit script says candidate-group-by-admin-dong coverage is not directly derivable from the current vehicle route artifact.

Conclusion:
- The 12 road-group values are metadata, not exact route-generation buckets.

## 7. Baseline/Smart Same Vehicle Input Check

Status: pass.

Evidence:
- [`result/vehicle_policy_smoke_1seed/sumo_nets/cw_NODE_5837/baseline_seed42.sumocfg`](../result/vehicle_policy_smoke_1seed/sumo_nets/cw_NODE_5837/baseline_seed42.sumocfg)
- [`result/vehicle_policy_smoke_1seed/sumo_nets/cw_NODE_5837/smart_seed42.sumocfg`](../result/vehicle_policy_smoke_1seed/sumo_nets/cw_NODE_5837/smart_seed42.sumocfg)
- [`result/vehicle_policy_smoke_1seed/outputs/vehicle_route_generation_audit.csv`](../result/vehicle_policy_smoke_1seed/outputs/vehicle_route_generation_audit.csv)
- [`result/vehicle_policy_smoke_1seed/outputs/demand_validation_summary.csv`](../result/vehicle_policy_smoke_1seed/outputs/demand_validation_summary.csv)

Observed:
- `compare_sumocfg_vehicle_inputs(...)` is real parsing code, not a hardcoded `true`.
- It returns `true`, `false`, or `not_checked`.
- Current validation output says:
  - baseline/smart same route file = `true`
  - baseline/smart same trip file = `true`
  - baseline/smart same net file = `true`
- The route/trip/net hashes are present in `vehicle_route_generation_audit.csv`.
- The checked run uses the same `routes_seed42.rou.xml`, `routes_seed42.trips.xml`, and `network.net.xml` for baseline and smart.

Conclusion:
- Baseline and smart share the same vehicle background input in the checked artifact.

## 8. What Can Be Claimed in the Report

- `main_realistic_stress` is a scenario-wide vehicle policy with `20,877 vph` and `3,480 vehicles / 600s`.
- Vehicle demand is passenger-only.
- Vehicle routes are generated from `randomTrips.py` on the full passenger-valid network.
- Road-group allocation is preserved as policy metadata.
- Baseline and smart share the same vehicle route, trip, and net inputs.
- Whole-network coverage passed threshold-based validation.

Recommended sentence:

> The vehicle demand is a scenario-wide `main_realistic_stress` background traffic policy (`20,877 vph`, `3,480 vehicles / 600s`) generated with passenger-only `randomTrips.py` on the full valid SUMO network. The 12 road-group allocation values are preserved as policy metadata, while network coverage is verified with edge-coverage and grid-coverage checks rather than exact edge-level allocation.

## 9. What Must Not Be Claimed

- Do not claim every edge is used.
- Do not claim traffic is uniformly distributed across all of Jung-gu.
- Do not claim the 12 road groups are exact edge-routing buckets.
- Do not claim candidate-local or crosswalk-local vehicle flow.
- Do not claim the artifact proves an external official `20,877 vph` ground truth.
- Do not claim smart changes vehicle demand; smart changes only the policy side.

## 10. Final Pass/Partial/Fail Table

| check_item | expected_meaning | actual_evidence | status | evidence_file | report_sentence |
|---|---|---|---|---|---|
| Scenario total | `main_realistic_stress` is one scenario-wide vehicle policy | `scenario_name=main_realistic_stress`, `total_vehicle_flow_vph=20877.0`, `total_vehicle_count_600s=3480` | pass | [`demand_params.csv`](../result/vehicle_policy_smoke_1seed/outputs/demand_params.csv) | `main_realistic_stress` is a scenario-wide background traffic policy |
| 600s conversion | `20877 vph -> 3480 vehicles / 600s` | `20877 * 600 / 3600 = 3479.5`, rounded to `3480` | pass | [`vehicle_flow_policy_validation.csv`](../result/vehicle_policy_smoke_1seed/outputs/vehicle_flow_policy_validation.csv) | The 600-second vehicle count is consistent with the policy total |
| Vehicle coverage | network-wide flow, not candidate-only | `coverage_ratio=0.734694`, `used_vehicle_edges=18360/24990`, `grid_coverage_ratio=1.0` | pass | [`vehicle_global_coverage_validation.csv`](../result/vehicle_policy_smoke_1seed/outputs/vehicle_global_coverage_validation.csv) | The flow is network-wide by generation method |
| Uniform spread | evenly distributed everywhere | `coverage_ratio=0.734694` and `route_bbox_area_ratio=0.851385`, but not 100% edge use | partial | [`vehicle_global_coverage_validation.csv`](../result/vehicle_policy_smoke_1seed/outputs/vehicle_global_coverage_validation.csv) | The traffic is broad, but not uniformly everywhere |
| randomTrips mode | full-network passenger-only route generation | `randomTrips.py` call uses `--vehicle-class passenger`; policy says `full_network_fallback` | pass | [`generate_demand.py`](../smart_crosswalk_sumo/generate_demand.py) | Vehicle routes are passenger-only randomTrips output on the full network |
| Road-group role | road-group numbers are metadata only | `road_group_allocation_summary.csv` has 12 groups; code says actual route generation remains network-wide randomTrips output | pass | [`road_group_allocation_summary.csv`](../result/vehicle_policy_smoke_1seed/outputs/road_group_allocation_summary.csv) | Road-group values are preserved as policy metadata |
| Baseline/smart equality | same route, trip, and net inputs | validation says `true / true / true`; hashes are present | pass | [`vehicle_route_generation_audit.csv`](../result/vehicle_policy_smoke_1seed/outputs/vehicle_route_generation_audit.csv) | Baseline and smart share the same vehicle background input |
| Coverage pass meaning | pass must come from thresholds, not file existence | code warns only if coverage/grid/buffer thresholds fail | pass | [`demand_validation.py`](../smart_crosswalk_sumo/demand_validation.py) | Coverage pass means threshold-based coverage, not mere file presence |
