# Pedestrian Demand Implementation Validation Contract

Purpose:
- Validate whether 10-minute pedestrian demand is actually implemented in `demand_pedestrian.rou.xml`.
- Keep runtime crossing observation separate.

Input files:
- [`result/active/real_30seed_runs/run_manifest.csv`](/Users/junlee/Desktop/2026-1/js/result/active/real_30seed_runs/run_manifest.csv)
- [`result/active/real_30seed_runs/manifests/candidate_metadata.csv`](/Users/junlee/Desktop/2026-1/js/result/active/real_30seed_runs/manifests/candidate_metadata.csv)
- Pedestrian demand source files:
  - [`result/active/pedestrian_assumption/crosswalk_pedestrian_assumptions_top50_daytime_high_1p2.csv`](/Users/junlee/Desktop/2026-1/js/result/active/pedestrian_assumption/crosswalk_pedestrian_assumptions_top50_daytime_high_1p2.csv)
  - [`result/active/pedestrian_assumption/crosswalk_pedestrian_assumptions_daytime_high_1p2.csv`](/Users/junlee/Desktop/2026-1/js/result/active/pedestrian_assumption/crosswalk_pedestrian_assumptions_daytime_high_1p2.csv)
- Per-run smoke outputs:
  - `phase6_smoke_summary.csv`
  - `demand_pedestrian.rou.xml`
  - `pedestrian_depart_plan.csv`
  - `pedestrian_route_diagnostics.csv` if present
  - `phase6_smoke_*_candidate_validation.json` if present

Outputs:
- [`result/active/real_30seed_runs/validation_seed01/pedestrian_demand_implementation_validation.csv`](/Users/junlee/Desktop/2026-1/js/result/active/real_30seed_runs/validation_seed01/pedestrian_demand_implementation_validation.csv)
- [`result/active/real_30seed_runs/validation_seed01/pedestrian_runtime_observation_validation.csv`](/Users/junlee/Desktop/2026-1/js/result/active/real_30seed_runs/validation_seed01/pedestrian_runtime_observation_validation.csv)
- [`result/active/real_30seed_runs/validation_seed01/pedestrian_validation_summary.csv`](/Users/junlee/Desktop/2026-1/js/result/active/real_30seed_runs/validation_seed01/pedestrian_validation_summary.csv)

Core rule:
- `generated_person_count_from_xml` is demand implementation truth.
- `ped_crossing_person_count` is runtime observation only.
- Do not use crossing count as demand implementation proof.

Validation logic:
- Smart:
  - `expected_generated_person_count` = source 10-minute demand for that candidate's `admin_dong`.
  - `demand_implementation_pass` requires XML person count to match that expected value.
- Baseline:
  - `baseline_group_expected_person_count` = sum of source 10-minute demand across all candidates in same `run_group`.
  - `baseline_group_generated_person_count` = total person count in baseline XML.
  - `demand_implementation_pass` requires those two to match.
- Runtime observation:
  - `runtime_observed_ped_count` = observed pedestrian crossing count from smoke summary.
  - `runtime_observation_pass` only checks `> 0`.
- Extension:
  - smart: `extension_count == 1`
  - baseline: `extension_count == 0`

Required columns in demand validation:
- `run_id`
- `scenario`
- `seed`
- `net_group`
- `run_group`
- `crosswalk_id`
- `dong_name`
- `admin_dong`
- `output_dir`
- `demand_source_file`
- `demand_key_type`
- `demand_10min`
- `expected_generated_person_count`
- `generated_person_count_from_xml`
- `baseline_group_expected_person_count`
- `baseline_group_generated_person_count`
- `demand_implementation_pass`
- `extension_pass`
- `overall_pass`
- `reason`

Required columns in runtime validation:
- `run_id`
- `scenario`
- `seed`
- `net_group`
- `run_group`
- `crosswalk_id`
- `dong_name`
- `admin_dong`
- `output_dir`
- `runtime_observed_ped_count`
- `runtime_observation_pass`
- `pedestrian_crossing_pass`
- `extension_pass`
- `overall_pass`
- `reason`

Important caveat:
- Current source files do not cover every current 30-seed candidate admin_dong.
- When source coverage is missing, validator must fail with explicit `missing_source_admin_dong` reason.
- This is correct behavior. Do not silently substitute runtime crossing count for missing demand source.

