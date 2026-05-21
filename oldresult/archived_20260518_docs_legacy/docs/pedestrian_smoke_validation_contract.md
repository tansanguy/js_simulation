# Pedestrian Smoke Validation Contract

Purpose:
- Validate pedestrian smoke outputs using only files already produced by `run_phase6_recovery_smoke`.
- Keep vehicle-demand and whole-map vehicle coverage checks out of this lane.

Inputs:
- [`result/active/real_30seed_runs/run_manifest.csv`](/Users/junlee/Desktop/2026-1/js/result/active/real_30seed_runs/run_manifest.csv)
- [`result/active/real_30seed_runs/manifests/candidate_metadata.csv`](/Users/junlee/Desktop/2026-1/js/result/active/real_30seed_runs/manifests/candidate_metadata.csv)
- Per-run output folders under [`result/active/real_30seed_runs/runs/`](/Users/junlee/Desktop/2026-1/js/result/active/real_30seed_runs/runs/)

Required run outputs:
- `phase6_smoke_summary.csv`
- `demand_pedestrian.rou.xml`
- `pedestrian_depart_plan.csv`
- `pedestrian_route_diagnostics.csv` if available
- `phase6_smoke_*_candidate_validation.json` if available

Outputs:
- [`result/active/real_30seed_runs/validation/pedestrian_smoke_validation.csv`](/Users/junlee/Desktop/2026-1/js/result/active/real_30seed_runs/validation/pedestrian_smoke_validation.csv)
- [`result/active/real_30seed_runs/validation/pedestrian_smoke_validation_summary.csv`](/Users/junlee/Desktop/2026-1/js/result/active/real_30seed_runs/validation/pedestrian_smoke_validation_summary.csv)

Validation rules:
- `output_dir` must exist.
- `phase6_smoke_summary.csv` must exist.
- `demand_pedestrian.rou.xml` must exist.
- `pedestrian_depart_plan.csv` must exist.
- `expected_ped_count` is compared with the observed person count from smoke summary.
- `spacing_pass` checks that actual depart times follow the planned repeat spacing.
- `phase_aligned_pass` checks that the depart plan used phase-aligned logic when available.
- `pedestrian_crossing_pass` requires a positive pedestrian crossing count and, if present, a positive crossing validation entry in the JSON.
- `extension_pass` is `false` for baseline unless the smoke output reports zero extensions, and `true` for smart only when the smoke output reports the expected nonzero extension count.

Required columns in `pedestrian_smoke_validation.csv`:
- `run_id`
- `scenario`
- `seed`
- `net_group`
- `run_group`
- `crosswalk_id`
- `dong_name`
- `admin_dong`
- `output_dir`
- `expected_ped_count`
- `observed_ped_count`
- `ped_count_pass`
- `spacing_pass`
- `phase_aligned_pass`
- `pedestrian_crossing_pass`
- `extension_pass`
- `overall_pass`
- `reason`

Notes:
- This contract uses only existing smoke artifacts.
- It does not require `demand_params.csv`, vehicle route XML, tripinfo, or whole-map coverage files.
- It is intentionally separate from vehicle-flow validation so baseline/smart pedestrian sanity checks stay lightweight.

