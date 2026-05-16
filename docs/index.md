# Docs Index

Current docs split into `current`, `reference`, `diagnosis`, `temporary`, `deprecated`, and `unclear`.

## Read First

1. [`README.md`](../README.md)
2. [`docs/current_status.md`](current_status.md)
3. [`docs/report_claims.md`](report_claims.md)
4. [`docs/final_scenario_implementation_check.md`](final_scenario_implementation_check.md)
5. [`docs/network_provenance_and_representativeness_check.md`](network_provenance_and_representativeness_check.md)
6. [`docs/vehicle_global_flow_and_stress_interpretation_check.md`](vehicle_global_flow_and_stress_interpretation_check.md)

## Status Legend

| Status | Meaning |
|---|---|
| `current` | Live source of truth for the current codebase and artifact state |
| `reference` | Useful background, but not the live status source |
| `diagnosis` | Inspection-only note, used to understand a gap or hypothesis |
| `temporary` | Draft, plan, work log, or helper artifact |
| `deprecated` | Legacy or conflicting wording; do not use as current truth |
| `unclear` | Not yet classified with confidence |

## Current

| File | Why |
|---|---|
| [`README.md`](../README.md) | Current project overview, commands, outputs, and limits |
| [`docs/current_status.md`](current_status.md) | Current pipeline status, risks, and pre-run checklist |
| [`docs/report_claims.md`](report_claims.md) | Safe report wording and banned claims |
| [`docs/candidate_metadata_contract.md`](candidate_metadata_contract.md) | Current 30seed candidate metadata contract |
| [`docs/final_scenario_implementation_check.md`](final_scenario_implementation_check.md) | Detailed evidence log for current implementation checks |
| [`docs/network_provenance_and_representativeness_check.md`](network_provenance_and_representativeness_check.md) | Current network provenance and representativeness evidence |
| [`docs/pedestrian_demand_implementation_validation_contract.md`](pedestrian_demand_implementation_validation_contract.md) | Current pedestrian demand implementation contract |
| [`docs/pedestrian_smoke_validation_contract.md`](pedestrian_smoke_validation_contract.md) | Current pedestrian smoke validation contract |
| [`docs/vehicle_global_flow_and_stress_interpretation_check.md`](vehicle_global_flow_and_stress_interpretation_check.md) | Current vehicle policy and global coverage interpretation |

## Reference

| File | Why |
|---|---|
| [`docs/deep-research-report.md`](deep-research-report.md) | Literature-backed distribution note |
| [`docs/phase6_junggu_traffic_demand_design.md`](phase6_junggu_traffic_demand_design.md) | Traffic demand design note for Phase 6 |
| [`docs/phase6_traffic_volume_mapping_schema.md`](phase6_traffic_volume_mapping_schema.md) | Mapping schema for traffic-volume sources |
| [`docs/phase6_vehicle_flow_implementation_notes.md`](phase6_vehicle_flow_implementation_notes.md) | Vehicle route smoke implementation notes |
| [`docs/sumo_gui_runtime_recovery.md`](sumo_gui_runtime_recovery.md) | SUMO GUI runtime recovery note |

## Diagnosis

| File | Why |
|---|---|
| [`docs/phase6_current_vehicle_flow_diagnosis.md`](phase6_current_vehicle_flow_diagnosis.md) | Vehicle-flow diagnosis note |
| [`docs/phase6_vehicle_flow_diagnosis_NODE_7240.md`](phase6_vehicle_flow_diagnosis_NODE_7240.md) | NODE_7240 smoke diagnosis |
| [`docs/pipeline_assumption_comparison.md`](pipeline_assumption_comparison.md) | Assumption-vs-pipeline mismatch diagnosis |
| [`docs/post_run_validation_diagnosis.md`](post_run_validation_diagnosis.md) | Post-run validation wiring diagnosis |

## Temporary

| File | Why |
|---|---|
| [`docs/files_to_stage_plan.md`](files_to_stage_plan.md) | Stage-plan snapshot |
| [`docs/final_simulation_report_draft.md`](final_simulation_report_draft.md) | Draft report, not current truth |
| [`docs/github_cleanup_and_push_plan.md`](github_cleanup_and_push_plan.md) | GitHub cleanup plan snapshot |
| [`docs/phase6_next_work_plan.md`](phase6_next_work_plan.md) | Work-plan snapshot |
| [`docs/phase6_vehicle_experiment_draft_design.md`](phase6_vehicle_experiment_draft_design.md) | Draft experiment design |
| [`docs/result_cleanup_plan.md`](result_cleanup_plan.md) | Cleanup plan snapshot |
| [`docs/taas_calibration_simulation_prompt.md`](taas_calibration_simulation_prompt.md) | Calibration prompt draft |
| [`docs/validation_pipeline_revision_plan.md`](validation_pipeline_revision_plan.md) | Validation revision plan |
| [`docs/result_cleanup_keep_list.md`](result_cleanup_keep_list.md) | Cleanup helper list, useful but not current truth |
| [`docs/result_cleanup_candidates.csv`](result_cleanup_candidates.csv) | Helper CSV for cleanup review |
| [`docs/result_cleanup_delete_commands.sh`](result_cleanup_delete_commands.sh) | Helper script for cleanup review |

## Deprecated

| File | Why |
|---|---|
| [`docs/(important)final_results_reading_guide.md`](%28important%29final_results_reading_guide.md) | Legacy results-reading guide |
| [`docs/readme_result.md`](readme_result.md) | Legacy result-reading guide for an older `integrated_short_test` flow |

## Unclear

| File | Why |
|---|---|
| [`docs/.DS_Store`](.DS_Store) | OS metadata file, ignore |

## Notes

- Current docs are the only ones that should be used for claims about the present pipeline.
- Temporary and diagnosis docs are still useful, but they are not the source of truth for the current state.
- Deprecated docs may still be readable for history, but they should not be cited as current status.
