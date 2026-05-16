# Validation Pipeline Revision Plan

Source diagnosis:
- [`docs/pipeline_assumption_comparison.md`](/Users/junlee/Desktop/2026-1/js/docs/pipeline_assumption_comparison.md)
- [`docs/post_run_validation_diagnosis.md`](/Users/junlee/Desktop/2026-1/js/docs/post_run_validation_diagnosis.md)

Goal:
- Keep the current smoke pipeline and the vehicle-flow pipeline separated.
- Add validation in a way that matches the experiment assumptions:
  - vehicle flow is global background traffic
  - baseline and smart share the same vehicle route/seed contract
  - smart differs only by policy at installed crosswalks
  - pedestrian demand is input-driven
  - dong-level distribution can be inspected
  - baseline/smart comparisons use the same observation scope

Non-goals:
- No code changes in this document.
- No simulation runs.
- No manifest regeneration.
- No cleanup.

## 1. Proposed Implementation Units

### Unit 1: Smoke validation adapter

Purpose:
- Attach a lightweight post-run validation layer to the existing smoke output contract.

Scope:
- Validate pedestrian repeat count.
- Validate pedestrian spacing.
- Validate phase-aligned departure behavior.
- Validate pedestrian route/person count.

Inputs:
- `demand_pedestrian.rou.xml`
- `pedestrian_depart_plan.csv`
- `phase6_smoke_summary.csv`
- smoke debug / route-order artifacts

Outputs:
- `pedestrian_smoke_validation.csv`

Why this is a separate unit:
- The current smoke pipeline already produces the pedestrian artifacts needed for a local sanity check.
- It does not currently produce vehicle routes or tripinfo, so vehicle validation should not be forced into this layer.

Definition of done:
- A smoke run can be validated without requiring vehicle-flow artifacts.
- The validation output is readable as a standalone smoke report.

---

### Unit 2: Vehicle demand validation bundle

Purpose:
- Validate that vehicle demand is generated from the policy contract rather than from the smoke-only path.

Scope:
- Expected vehicle count vs generated vehicle count.
- Road group allocation.
- Baseline/smart same route file or same route hash.

Inputs:
- `demand_params.csv`
- `demand_vehicle.rou.xml`
- `demand_all.rou.xml`
- `.trips.xml`
- route generation audit files
- vehicle demand policy metadata

Outputs:
- `vehicle_demand_validation.csv`

Why this is a separate unit:
- The current smoke runner does not emit the required vehicle artifacts.
- Vehicle demand validation depends on the vehicle-flow pipeline or an equivalent route-generation command.

Definition of done:
- Vehicle validation can be executed only when vehicle-flow artifacts exist.
- Baseline/smart route equality can be checked at the route-file or route-hash level.

---

### Unit 3: Whole-map vehicle coverage validation bundle

Purpose:
- Verify that vehicle flow is distributed across the full network, not concentrated only near candidate crosswalks.

Scope:
- Network-wide edge coverage.
- Candidate-buffer concentration check.
- Optional coverage summaries by road group or spatial bucket.

Inputs:
- `demand_vehicle.rou.xml`
- `demand_all.rou.xml`
- `.trips.xml`
- `tripinfo*.xml`
- full net XML
- route coverage / audit metadata

Outputs:
- `vehicle_global_coverage_validation.csv`
- `vehicle_edge_coverage_summary.csv`

Why this is a separate unit:
- Coverage validation is a different contract from demand-count validation.
- It needs the whole vehicle route graph and should not be collapsed into smoke-level pedestrian checks.

Definition of done:
- A run can be flagged if vehicle demand is concentrated near installed crosswalks instead of spanning the map.

---

### Unit 4: Candidate metadata preservation

Purpose:
- Preserve `dong_name` and `admin_dong` from the source layers through the 30-seed planning layer.

Recommended approach:
- Keep the run manifest lean.
- Add a separate `candidate_metadata.csv` as the durable source of truth for candidate-level metadata.
- Let `run_manifest` reference the candidate key and the metadata file path rather than duplicating all source columns.

Columns that should survive at minimum:
- `crosswalk_id`
- `dong_name`
- `admin_dong`
- `net_group`
- `net_file`
- `source_csv`
- `source_rank` or equivalent provenance key
- `is_installed_candidate` / `is_smart_target` if available

Why separate metadata is preferable:
- It avoids inflating the run manifest.
- It keeps provenance stable across smoke and vehicle-flow runs.
- It makes dong-level inspection possible without coupling metadata to execution commands.

Definition of done:
- A 30-seed run can still be traced back to its dong/admin_dong source without needing the original top50 file.

---

### Unit 5: Baseline/smart observation scope policy

Purpose:
- Make baseline and smart comparisons fair by defining a single observation policy.

Recommended policy:
- Use candidate-level comparison for smoke validation.
- Use network-level comparison for vehicle-flow validation.
- Do not mix the two into one score.

Inputs:
- baseline and smart manifests
- candidate metadata
- run-level summaries
- vehicle coverage summaries

Outputs:
- A documented observation-scope contract
- Comparison summaries that clearly label the scope

Definition of done:
- Every metric explicitly states whether it is candidate-level or network-level.
- Baseline and smart are compared on the same scope within a given validation lane.

---

### Unit 6: Validation command orchestration

Purpose:
- Create an executable validation path without attaching it directly to the current smoke command.

Scope:
- Validation manifest generation for executable checks.
- Optional aggregator step that collects smoke + vehicle outputs into one report.
- Keep smoke and vehicle-flow execution paths separate.

Recommended output grouping:
- Smoke lane:
  - `pedestrian_smoke_validation.csv`
- Vehicle lane:
  - `vehicle_demand_validation.csv`
  - `vehicle_global_coverage_validation.csv`

Why this is a separate unit:
- The current `run_manifest.command` is smoke-only.
- Forcing all checks into that command would blur failure modes and make debugging harder.

Definition of done:
- A run can be scheduled with smoke-only, vehicle-only, or both validations.
- The command contract remains explicit.

## 2. Recommended Dependency Order

1. Candidate metadata preservation.
2. Smoke validation adapter.
3. Vehicle demand validation bundle.
4. Whole-map vehicle coverage validation bundle.
5. Observation scope policy.
6. Validation command orchestration and reporting glue.

Reasoning:
- Metadata is the shared substrate for both smoke and vehicle lanes.
- Smoke validation can be implemented independently from vehicle routes.
- Vehicle validation depends on a route-generating pipeline that the smoke path does not provide.
- Scope policy should be finalized after the two validation lanes are explicit.

## 3. File / Data Contract by Unit

### Smoke lane

Required artifacts:
- `demand_pedestrian.rou.xml`
- `pedestrian_depart_plan.csv`
- `phase6_smoke_summary.csv`

New report:
- `pedestrian_smoke_validation.csv`

### Vehicle-demand lane

Required artifacts:
- `demand_params.csv`
- `demand_vehicle.rou.xml`
- `demand_all.rou.xml`
- `.trips.xml`
- route audit files

New report:
- `vehicle_demand_validation.csv`

### Vehicle-coverage lane

Required artifacts:
- full vehicle route / trip outputs
- `tripinfo*.xml`
- full network XML
- coverage audit metadata

New report:
- `vehicle_global_coverage_validation.csv`
- `vehicle_edge_coverage_summary.csv`

### Metadata lane

Required artifacts:
- top50 source CSV
- 30-seed candidate manifest
- run manifest

New artifact:
- `candidate_metadata.csv`

## 4. Baseline vs Smart Scope Decision

### Option A: Candidate-level comparison

What it means:
- Compare a baseline candidate and a smart candidate around the same installed crosswalk.

Pros:
- Strong for smoke validation.
- Clear local interpretation.
- Easy to keep aligned with pedestrian-only checks.

Cons:
- Does not answer whether vehicle traffic is truly global.
- Can hide network-wide traffic distortions.

### Option B: Network-level comparison

What it means:
- Compare baseline and smart over the full network with identical vehicle background traffic.

Pros:
- Matches the vehicle-flow research assumption.
- Best for checking global traffic fairness and coverage.

Cons:
- Requires full vehicle route / trip artifacts.
- Cannot be derived from smoke-only outputs.

### Recommended split

- Use candidate-level comparison for smoke.
- Use network-level comparison for vehicle-flow.
- Treat them as two different validation lanes, not one blended metric.

## 5. Practical Safest Revision

The safest structure for this project is:

1. Keep the current smoke pipeline as a pedestrian sanity-check lane.
2. Keep vehicle-flow generation and whole-map coverage as a separate lane.
3. Preserve candidate metadata independently so dong/admin_dong analysis stays possible.
4. Use a shared manifest key structure, but do not force all validations into one execution command.
5. Make baseline/smart comparison scope explicit in every report.

This preserves existing runs and logs, minimizes reruns, and prevents smoke artifacts from being mistaken for vehicle-flow validation.

## 6. Minimal Next Implementation Units

If the next step is to implement in small chunks, the order should be:

1. `candidate_metadata.csv` contract and propagation.
2. `pedestrian_smoke_validation.csv` adapter.
3. `vehicle_demand_validation.csv` bundle.
4. `vehicle_global_coverage_validation.csv` bundle.
5. Validation orchestration / manifest wiring.
6. Comparison reporting that labels candidate-level vs network-level scope.

