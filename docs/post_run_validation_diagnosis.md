# Post-run Validation Diagnosis

Scope: diagnose only. No code changes, no simulation runs, no manifest regeneration.

## Conclusion

The current 30-seed pipeline is **catalog-only** for validation:

- `result/active/real_30seed_runs/manifests/run_validation_manifest.csv` exists and is populated.
- `run_manifest.csv` has a `validation_manifest` column pointing at that catalog.
- The `command` field in `run_manifest.csv` still runs only `python3 -m smart_crosswalk_sumo.run_phase6_recovery_smoke ...`.
- There is **no post-run validation execution wired into the command itself**.

So the current state is:

- **Catalog attached:** yes.
- **Actual validation execution attached to run command:** no.

## What Is Already Implemented

There are existing validation implementations, but they are split across full-demand and vehicle-flow paths, not attached to the current smoke runner.

### Pedestrian demand validation

Existing code:

- [`smart_crosswalk_sumo/demand_validation.py`](/Users/junlee/Desktop/2026-1/js/smart_crosswalk_sumo/demand_validation.py#L839)
- [`scripts/validate_demand_policy.py`](/Users/junlee/Desktop/2026-1/js/scripts/validate_demand_policy.py#L12)
- [`smart_crosswalk_sumo/main.py`](/Users/junlee/Desktop/2026-1/js/smart_crosswalk_sumo/main.py#L95)

What it does:

- Validates generated pedestrian routes against `demand_params.csv`.
- Produces `pedestrian_flow_policy_validation.csv`.
- Checks pedestrian route XML, route order, count match, and summary metrics.

### Vehicle demand validation

Existing code:

- [`smart_crosswalk_sumo/demand_validation.py`](/Users/junlee/Desktop/2026-1/js/smart_crosswalk_sumo/demand_validation.py#L542)
- [`scripts/validate_demand_policy.py`](/Users/junlee/Desktop/2026-1/js/scripts/validate_demand_policy.py#L12)
- [`smart_crosswalk_sumo/vehicle_demand_policy.py`](/Users/junlee/Desktop/2026-1/js/smart_crosswalk_sumo/vehicle_demand_policy.py#L167)
- [`scripts/build_vehicle_flow_audit.py`](/Users/junlee/Desktop/2026-1/js/scripts/build_vehicle_flow_audit.py#L167)
- [`smart_crosswalk_sumo/run_phase6_vehicle_flow.py`](/Users/junlee/Desktop/2026-1/js/smart_crosswalk_sumo/run_phase6_vehicle_flow.py#L876)

What it does:

- Validates vehicle route count vs policy count.
- Validates route/trip XML consistency.
- Validates road-group allocation.
- Produces route coverage and vehicle flow audits.

### Whole-map vehicle flow / coverage validation

Existing code:

- [`smart_crosswalk_sumo/demand_validation.py`](/Users/junlee/Desktop/2026-1/js/smart_crosswalk_sumo/demand_validation.py#L774)
- [`smart_crosswalk_sumo/vehicle_demand_policy.py`](/Users/junlee/Desktop/2026-1/js/smart_crosswalk_sumo/vehicle_demand_policy.py#L535)
- [`scripts/build_vehicle_flow_audit.py`](/Users/junlee/Desktop/2026-1/js/scripts/build_vehicle_flow_audit.py#L167)
- [`smart_crosswalk_sumo/run_phase6_vehicle_flow.py`](/Users/junlee/Desktop/2026-1/js/smart_crosswalk_sumo/run_phase6_vehicle_flow.py#L1470)

What it does:

- Computes edge coverage ratio over the full driveable network.
- Computes bbox / grid coverage.
- Flags candidate-buffer concentration.
- Produces `vehicle_global_coverage_validation.csv` or equivalent audit output.

## Current Wiring Check

### `run_manifest.csv`

Connected:

- `validation_manifest` column exists and points to `manifests/run_validation_manifest.csv`.

Not connected:

- The `command` column does not call any validation runner.
- The command string only invokes `smart_crosswalk_sumo.run_phase6_recovery_smoke`.

Evidence in code:

- [`smart_crosswalk_sumo/reporting/aggregate_30seed_results.py`](/Users/junlee/Desktop/2026-1/js/smart_crosswalk_sumo/reporting/aggregate_30seed_results.py#L674)
- [`smart_crosswalk_sumo/reporting/aggregate_30seed_results.py`](/Users/junlee/Desktop/2026-1/js/smart_crosswalk_sumo/reporting/aggregate_30seed_results.py#L708)

### `run_validation_manifest.csv`

Connected:

- Yes, but only as a catalog of already-produced artifacts.

Not connected:

- It is not an execution plan.
- It records `status`, `row_count`, `source_artifact`, and `source_path`, but no runnable validation step.

Evidence in code:

- [`smart_crosswalk_sumo/reporting/aggregate_30seed_results.py`](/Users/junlee/Desktop/2026-1/js/smart_crosswalk_sumo/reporting/aggregate_30seed_results.py#L751)
- [`smart_crosswalk_sumo/reporting/aggregate_30seed_results.py`](/Users/junlee/Desktop/2026-1/js/smart_crosswalk_sumo/reporting/aggregate_30seed_results.py#L1365)

### `aggregate_30seed_results.py`

Connected:

- Builds the run manifest and validation catalog.
- Reads completed run outputs and summarizes them.

Not connected:

- Does not invoke `validate_demand_run`.
- Does not invoke `build_vehicle_flow_audit`.
- Does not run any post-run validation command automatically.

Evidence:

- [`smart_crosswalk_sumo/reporting/aggregate_30seed_results.py`](/Users/junlee/Desktop/2026-1/js/smart_crosswalk_sumo/reporting/aggregate_30seed_results.py#L751)
- [`smart_crosswalk_sumo/reporting/aggregate_30seed_results.py`](/Users/junlee/Desktop/2026-1/js/smart_crosswalk_sumo/reporting/aggregate_30seed_results.py#L1365)

### `run_phase6_recovery_smoke.py`

Connected:

- Produces smoke outputs only.
- Writes `phase6_smoke_summary.csv`, debug traces, route-order summaries, and candidate validation JSON.

Not connected:

- Does not call `validate_demand_run`.
- Does not emit demand validation CSVs.
- Does not emit vehicle validation or coverage validation artifacts.

Evidence:

- [`smart_crosswalk_sumo/run_phase6_recovery_smoke.py`](/Users/junlee/Desktop/2026-1/js/smart_crosswalk_sumo/run_phase6_recovery_smoke.py#L842)
- [`smart_crosswalk_sumo/run_phase6_recovery_smoke.py`](/Users/junlee/Desktop/2026-1/js/smart_crosswalk_sumo/run_phase6_recovery_smoke.py#L1354)
- [`smart_crosswalk_sumo/run_phase6_recovery_smoke.py`](/Users/junlee/Desktop/2026-1/js/smart_crosswalk_sumo/run_phase6_recovery_smoke.py#L1561)

### `main.py`

Connected:

- There is already a generic validation hook: `--validate_demand`.
- `_maybe_validate_demand(...)` is called at the end of the normal pipeline.

Not connected:

- `run_phase6_recovery_smoke.py` does not go through this path.
- The current 30-seed smoke command does not pass `--validate_demand`.

Evidence:

- [`smart_crosswalk_sumo/main.py`](/Users/junlee/Desktop/2026-1/js/smart_crosswalk_sumo/main.py#L95)
- [`smart_crosswalk_sumo/main.py`](/Users/junlee/Desktop/2026-1/js/smart_crosswalk_sumo/main.py#L267)
- [`smart_crosswalk_sumo/main.py`](/Users/junlee/Desktop/2026-1/js/smart_crosswalk_sumo/main.py#L381)
- [`smart_crosswalk_sumo/main.py`](/Users/junlee/Desktop/2026-1/js/smart_crosswalk_sumo/main.py#L493)

## Validation-by-Validation Diagnosis

### A. Pedestrian demand generated as expected

Existing code:

- [`smart_crosswalk_sumo/demand_validation.py`](/Users/junlee/Desktop/2026-1/js/smart_crosswalk_sumo/demand_validation.py#L542)
- [`smart_crosswalk_sumo/main.py`](/Users/junlee/Desktop/2026-1/js/smart_crosswalk_sumo/main.py#L95)
- [`scripts/validate_demand_policy.py`](/Users/junlee/Desktop/2026-1/js/scripts/validate_demand_policy.py#L12)

Required inputs:

- `demand_params.csv`
- `demand_pedestrian.rou.xml`
- `sumocfg` / route paths
- `pedestrian_depart_plan.csv` is helpful for diagnosis

Current connection status:

- **Not connected to the current smoke command.**
- Current smoke runs do produce `demand_pedestrian.rou.xml`, but they do **not** produce `demand_params.csv`, which `validate_demand_run()` expects.

Implementation difficulty:

- **Low to medium** for the full pipeline.
- **Medium** to adapt to the current smoke-only outputs because an adapter or an alternate input contract is needed.

Current run-folder evidence:

- Present: `demand_pedestrian.rou.xml`, `phase6_smoke_summary.csv`, `pedestrian_route_diagnostics.csv`, `pedestrian_depart_plan.csv`.
- Absent in current smoke outputs: `demand_params.csv`, `pedestrian_flow_policy_validation.csv`.

### B. Vehicle demand generated as expected

Existing code:

- [`smart_crosswalk_sumo/demand_validation.py`](/Users/junlee/Desktop/2026-1/js/smart_crosswalk_sumo/demand_validation.py#L542)
- [`smart_crosswalk_sumo/vehicle_demand_policy.py`](/Users/junlee/Desktop/2026-1/js/smart_crosswalk_sumo/vehicle_demand_policy.py#L167)
- [`scripts/validate_demand_policy.py`](/Users/junlee/Desktop/2026-1/js/scripts/validate_demand_policy.py#L12)
- [`scripts/build_vehicle_flow_audit.py`](/Users/junlee/Desktop/2026-1/js/scripts/build_vehicle_flow_audit.py#L167)
- [`smart_crosswalk_sumo/run_phase6_vehicle_flow.py`](/Users/junlee/Desktop/2026-1/js/smart_crosswalk_sumo/run_phase6_vehicle_flow.py#L876)

Required inputs:

- `demand_vehicle.rou.xml`
- `demand_all.rou.xml` or equivalent vehicle route XML
- `.trips.xml` if trip-route consistency is checked
- `road_group_allocation_summary.csv`
- `demand_params.csv`
- baseline/smart `sumocfg` pair if cross-run equality is checked

Current connection status:

- **Not connected to the current smoke command.**
- Current smoke outputs do not include vehicle route XML by default.
- Current smoke command does not pass `--include-vehicles`.

Implementation difficulty:

- **Medium** if the goal is full-demand validation on existing vehicle-flow outputs.
- **High** if the goal is to retrofit this into the smoke-only pipeline, because the required artifacts are not present there today.

Current run-folder evidence:

- Present: `phase6_smoke_summary.csv`, `phase6_smoke_*_candidate_validation.json`.
- Absent: `demand_vehicle.rou.xml`, `demand_all.rou.xml`, `.trips.xml`, `vehicle_flow_policy_validation.csv`.

### C. Vehicle covers whole map, not only one crosswalk area

Existing code:

- [`smart_crosswalk_sumo/demand_validation.py`](/Users/junlee/Desktop/2026-1/js/smart_crosswalk_sumo/demand_validation.py#L774)
- [`smart_crosswalk_sumo/vehicle_demand_policy.py`](/Users/junlee/Desktop/2026-1/js/smart_crosswalk_sumo/vehicle_demand_policy.py#L535)
- [`scripts/build_vehicle_flow_audit.py`](/Users/junlee/Desktop/2026-1/js/scripts/build_vehicle_flow_audit.py#L167)

Required inputs:

- Full vehicle route XML
- Full driveable net XML
- Optional trip XML
- Optional candidate-edge mapping metadata for concentration checks

Current connection status:

- **Not connected to the current smoke command.**
- The current smoke output tree has only pedestrian demand artifacts, so there is no route graph to measure whole-map vehicle coverage from.

Implementation difficulty:

- **Medium** if run against vehicle-flow outputs that already exist.
- **High** if forced into the smoke-only run path, because the route file is missing and the measurement is meaningless without it.

Current run-folder evidence:

- Present: `phase6_smoke_summary.csv` and route-order/debug CSVs.
- Absent: `vehicle_edge_coverage_summary.csv`, `vehicle_global_coverage_validation.csv`, `demand_vehicle.rou.xml`, `demand_all.rou.xml`.

## Current Result-Folder Inventory

Checked under `result/active/real_30seed_runs`:

Present in at least one `seed01` smoke output:

- `phase6_smoke_summary.csv`
- `phase6_smoke_*_results.csv`
- `phase6_smoke_*_debug_trace.csv`
- `phase6_smoke_*_candidate_validation.json`
- `route_generation_audit.csv`
- `pedestrian_depart_plan.csv`
- `demand_pedestrian.rou.xml`
- `signal_phase_audit.csv`
- `pedestrian_route_diagnostics.csv`

Present at the pipeline level:

- `run_manifest.csv`
- `manifests/run_validation_manifest.csv`
- `csv/run_validation_summary.csv`
- `csv/seed30_run_level_results.csv`

Not found in the current smoke result tree:

- `demand_vehicle.rou.xml`
- `demand_all.rou.xml`
- `.trips.xml`
- `tripinfo*.xml`
- `vehicle_flow_policy_validation.csv`
- `vehicle_global_coverage_validation.csv`
- `vehicle_edge_coverage_summary.csv`
- `demand_validation_summary.csv`
- `demand_params.csv`

Logs:

- `result/active/real_30seed_runs/logs/current_main_12/baseline/seed01.log` exists.
- `result/active/real_30seed_runs/logs/current_main_12/smart/*/seed01.log` exists for the completed smart candidate runs.
- No evidence was found for `seed02` logs or broader seed coverage in the current folder snapshot.

## Implementation Readiness

### Already implemented and callable

- Pedestrian validation engine: `demand_validation.py`
- Vehicle flow audit: `build_vehicle_flow_audit.py`
- Vehicle demand route coverage audit: `vehicle_demand_policy.py`
- Generic validation hook in `main.py`

### Needs new plumbing for the current pipeline

- A runner that actually calls the validation engine after each run or after each batch.
- An input contract for smoke runs that includes the data `demand_validation.py` expects, or a smoke-specific validation adapter.
- Vehicle artifacts in the smoke path, if the goal is to validate B and C from the same run folder.

## Recommendation

Recommended connection style: **2. separate validation command generation**

Reason:

- The current `run_phase6_recovery_smoke` command is intentionally smoke-only.
- The existing validation code already exists as a separate contract.
- Vehicle coverage validation needs vehicle artifacts that are not present in the smoke outputs.
- A separate validation command keeps the run step stable and makes the missing-input failure mode explicit.

Practical sequence:

1. Keep the run command focused on smoke execution.
2. Generate a separate validation command per run group or per completed run folder.
3. Optionally run an aggregate-stage batch validator after the run folders exist.

If you want a single fallback choice for existing data:

- **3. aggregate 결과 단계에서 일괄 검증** is the safest for already-finished folders.
- **1. run command 뒤에 자동 실행** is the least safe right now because the required vehicle artifacts are missing in the smoke path.

## Why Not Implement Directly Now

- The current `real_30seed_runs` folders do not contain the vehicle XMLs needed for B and C.
- `validate_demand_run()` expects `demand_params.csv`, which the smoke runner does not produce.
- Current smoke outputs are sufficient only for pedestrian smoke evidence, not for full demand validation.
- Mixing vehicle validation into the smoke command would blur the boundary between smoke evidence and policy validation.
- Batch-specific network separation is already encoded in the manifests, so validation needs to respect the per-group output contract rather than one generic global pass.

