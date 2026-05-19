# 30 Seed Pipeline

Output root: `result/active/real_30seed_runs_sampled10`

This folder prepares a 30-seed workflow for three separate network groups:

- `current_main_12`
- `signal_fix_9`
- `generated_signal_7`

Baseline runs are separate per network group. Smart runs are separate per candidate and seed.

Important constraints:

- Codex did not run the 30-seed SUMO jobs.
- Baseline networks are not shared across groups.
- CSV columns stay English-only.
- sampled10 본실험은 기본적으로 `--disable-ssm`를 쓴다. SSM은 final safety 검증에서만 다시 켠다.

Main folders:

- `csv/` standardized CSV outputs
- `xml/` copied raw XML and SUMO config files
- `logs/` run logs
- `readme/` per-table notes
- `commands/` executable shell scripts
- `manifests/` run plans and single-candidate CSVs
- `manifests/run_validation_manifest.csv` validation catalog
- `runs/` actual run output directories used by the generated commands

Recommended flow:

1. For `seed1` sequential-light validation, run the `command_to_run_seed1_*.sh` wrappers. `command_to_run_seed1_all_groups.sh` now covers all 4 groups.
2. For the preserved full/source-of-truth 600-second path, run the `command_to_run_30seed_*.sh` scripts.
3. Run `command_to_standardize_30seed_outputs.sh`.
4. Run `command_to_check_30seed_results.sh`.
5. Run `python3 -m smart_crosswalk_sumo.reporting.aggregate_30seed_results aggregate ...` if needed.

Execution policy update:

- `seed1` validation now uses the lightweight path: `--sim-duration 540` with `--output-profile light`.
- Do not use the pre-lightweight `seed1` commands that hardcode `--sim-duration 600` without `--output-profile light`.
- Keep the 600-second/full commands available for the 30-seed source-of-truth flow only.
