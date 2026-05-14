# Repository Guardrails

- Do not run `git add .`.
- Do not run `git add -A`.
- Do not stage `result/` recursively.
- Do not stage generated SUMO network files by default.
- Only stage files explicitly listed by the user.
- Before staging, print the exact file list.
- Use `git status --short` before and after staging.
- Do not run simulations unless the user explicitly asks.
- Do not delete files unless the user explicitly asks.
- Do not use destructive git commands such as `git reset --hard` or `git clean -fd`.

# Phase 6 Work Rules

- Treat batch03 mini results as execution-valid smoke evidence only.
- Do not describe current mini results as policy-effect validation.
- Keep vehicle route work separate from current pedestrian-only smoke work.
- Keep batch-specific network files separated by batch.
- Keep `phase6_mini_experiment_candidates_batch03_only.csv` as the batch03 source of truth.
- Do not swap in the generic candidates CSV.
- Prefer small, explicit stages over broad staging.
