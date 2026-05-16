# Candidate Metadata Contract

Purpose:
- Preserve candidate provenance separately from the execution manifest.
- Keep `dong_name` and `admin_dong` available for the 30-seed pipeline without expanding `run_manifest.csv`.

Source of truth:
- [`smart_crosswalk_sumo/data/crosswalk_stepwise_result_50m.csv`](/Users/junlee/Desktop/2026-1/js/smart_crosswalk_sumo/data/crosswalk_stepwise_result_50m.csv)
- [`result/active/real_30seed_runs/manifests/*_candidates.csv`](/Users/junlee/Desktop/2026-1/js/result/active/real_30seed_runs/manifests/current_main_12_candidates.csv)
- [`result/active/real_30seed_runs/run_manifest.csv`](/Users/junlee/Desktop/2026-1/js/result/active/real_30seed_runs/run_manifest.csv)

Output:
- [`result/active/real_30seed_runs/manifests/candidate_metadata.csv`](/Users/junlee/Desktop/2026-1/js/result/active/real_30seed_runs/manifests/candidate_metadata.csv)

Contract shape:
- One row per smart candidate.
- Only candidates that participate in the 30-seed smart lane are included.
- `is_smart_target` is `true` for every row in the current contract.

Required columns:
- `crosswalk_id`
- `dong_name`
- `admin_dong`
- `net_group`
- `run_group`
- `net_file`
- `source_csv`
- `source_rank`
- `risk_rank`
- `risk_score`
- `phase6_status_label`
- `is_smart_target`

Column rules:
- `crosswalk_id`: join key for the candidate and the source metadata.
- `dong_name` / `admin_dong`: copied from `crosswalk_stepwise_result_50m.csv` when available.
- `net_group`: network family used for the candidate.
- `run_group`: 30-seed execution group.
- `net_file`: network file for that group.
- `source_csv`: canonical source file used for metadata enrichment.
- `source_rank`: original row order in the source CSV after loading.
- `risk_rank`: descending rank by `risk_score` within the source CSV.
- `risk_score`: numeric source score used for candidate ordering.
- `phase6_status_label`: propagated from source metadata; if a future source uses `final_tier`, that may be used as a fallback.
- `is_smart_target`: boolean flag for smart-lane membership.

Generation rule:
- Join the smart candidate list to the source metadata on `crosswalk_id`.
- Do not overwrite `run_manifest.csv`.
- Keep the metadata file separate so execution and provenance can evolve independently.

Validation rules:
- All 34 smart candidates must appear exactly once in `candidate_metadata.csv`.
- Every row must keep `dong_name` and `admin_dong` if the source has them.
- The set of `crosswalk_id` values in `candidate_metadata.csv` must match the smart `crosswalk_id` set in `run_manifest.csv`.
- No simulation output is required to build this file.

Operational note:
- This contract is for provenance only.
- It is not a post-run validation report and should not be used as a simulation result summary.

