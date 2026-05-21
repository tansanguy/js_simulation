# Files To Stage Plan

## Stage Candidates

- `smart_crosswalk_sumo/run_phase6_recovery_smoke.py`
- `smart_crosswalk_sumo/run_manual_tls_retry_batch.py`
- `smart_crosswalk_sumo/run_max_recovery.py`
- `smart_crosswalk_sumo/run_red_only_phase_fix_batch.py`
- `docs/phase6_next_work_plan.md`
- `docs/phase6_vehicle_implementation_plan.md`
- `docs/github_cleanup_and_push_plan.md`
- `docs/result_cleanup_plan.md`
- `docs/files_to_stage_plan.md`
- `.gitignore`
- `AGENTS.md`

## Stage Later / User Decision

- `.claude/settings.local.json`
- `T1_accident_crosswalk.csv`
- `T2_crosswalk_features.csv`

## Keep Out By Default

- `.agents/`
- `.codex-plugins/`
- `caveman/`
- `result/`

## Exact Stage Rule

- Print the exact file list before staging.
- Use `git status --short` before and after staging.
- Add files one by one or in a small explicit list only.
- Never use `git add .`.
- Never use `git add -A`.

