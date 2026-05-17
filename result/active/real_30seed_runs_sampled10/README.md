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

1. Run the generated `command_to_run_30seed_*.sh` scripts locally.
2. Run `command_to_standardize_30seed_outputs.sh`.
3. Run `command_to_check_30seed_results.sh`.
4. Run `python3 -m smart_crosswalk_sumo.reporting.aggregate_30seed_results aggregate ...` if needed.
