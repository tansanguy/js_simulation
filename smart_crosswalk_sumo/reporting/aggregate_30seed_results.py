#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import shutil
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import numpy as np
import pandas as pd

from smart_crosswalk_sumo.reporting.phase_next_cleanup_audit import build_audit as build_cleanup_audit


SAFETY_KEYWORDS = [
    "accident",
    "crash",
    "collision",
    "risk",
    "conflict",
    "near_miss",
    "ttc",
    "pet",
    "safety",
    "hazard",
    "proximity",
]

RUNTIME_COLUMNS = {
    "run_name",
    "scenario",
    "seed",
    "run_group",
    "net_group",
    "run_start_time",
    "run_end_time",
    "elapsed_sec",
    "completed",
    "output_dir",
}

VEHICLE_COLUMNS = {
    "veh_delay_mean",
    "veh_delay_max",
    "vehicle_delay_mean",
    "vehicle_delay_max",
    "avg_vehicle_delay_sec",
    "network_avg_travel_time_sec",
    "network_arrived_vehicles",
}

PEDESTRIAN_COLUMNS = {
    "ped_crossing_person_count",
    "ped_crossing_presence_steps",
    "ped_wait_time_mean",
    "ped_wait_time_max",
    "elderly_incomplete_crossings",
    "crossing_attempt_count",
    "completed_crossing_count",
    "incomplete_crossing_count",
}

SUMMARY_METRIC_CANDIDATES = [
    "extension_count",
    "elapsed_sec",
    "ped_crossing_person_count",
    "ped_crossing_presence_steps",
    "ped_wait_time_mean",
    "ped_wait_time_max",
    "veh_delay_mean",
    "veh_delay_max",
    "vehicle_delay_mean",
    "vehicle_delay_max",
    "high_risk_event_count",
    "min_ttc",
    "mean_ttc",
    "min_pet",
    "mean_pet",
]


@dataclass(frozen=True)
class GroupSpec:
    run_group: str
    net_group: str
    baseline_net_file: Path
    full_candidate_csv: Path
    smart_candidates: pd.DataFrame
    baseline_placeholder_crosswalk_id: str


def _now_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")


def _ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def _write_csv(df: pd.DataFrame, path: Path) -> None:
    _ensure_dir(path.parent)
    df.to_csv(path, index=False, encoding="utf-8-sig")


def _write_text(path: Path, text: str) -> None:
    _ensure_dir(path.parent)
    path.write_text(text, encoding="utf-8")


def _read_csv(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame()
    for encoding in ("utf-8-sig", "utf-8"):
        try:
            return pd.read_csv(path, encoding=encoding)
        except Exception:
            continue
    return pd.DataFrame()


def _read_csv_meta(path: Path) -> tuple[int, int, list[str], str]:
    if not path.exists():
        return 0, 0, [], "missing_file"
    try:
        with path.open("r", encoding="utf-8-sig", newline="", errors="replace") as fh:
            reader = csv.reader(fh)
            header = next(reader, [])
            rows = sum(1 for _ in reader)
        return rows, len(header), list(header), ""
    except Exception as exc:
        return 0, 0, [], str(exc)


def _has_any(columns: Iterable[str], needles: Iterable[str]) -> bool:
    lowered = [c.lower() for c in columns]
    for needle in needles:
        needle_l = needle.lower()
        if any(needle_l in col for col in lowered):
            return True
    return False


def _safe_float(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, float) and np.isnan(value):
        return None
    try:
        return float(value)
    except Exception:
        return None


def _safe_bool(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, np.integer)):
        return bool(value)
    if isinstance(value, float) and np.isnan(value):
        return False
    text = str(value).strip().lower()
    if text in {"true", "1", "yes", "y"}:
        return True
    if text in {"false", "0", "no", "n", ""}:
        return False
    return False


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _root_readme_text(output_root: Path) -> str:
    return "\n".join(
        [
            "# 30 Seed Pipeline",
            "",
            f"Output root: `{output_root}`",
            "",
            "This folder prepares a 30-seed workflow for three separate network groups:",
            "",
            "- `current_main_12`",
            "- `signal_fix_9`",
            "- `generated_signal_7`",
            "",
            "Baseline runs are separate per network group. Smart runs are separate per candidate and seed.",
            "",
            "Important constraints:",
            "",
            "- Codex did not run the 30-seed SUMO jobs.",
            "- Baseline networks are not shared across groups.",
            "- CSV columns stay English-only.",
            "",
            "Main folders:",
            "",
            "- `csv/` standardized CSV outputs",
            "- `xml/` copied raw XML and SUMO config files",
            "- `logs/` run logs",
            "- `readme/` per-table notes",
            "- `commands/` executable shell scripts",
            "- `manifests/` run plans and single-candidate CSVs",
            "- `runs/` actual run output directories used by the generated commands",
            "",
            "Recommended flow:",
            "",
            "1. Run the generated `command_to_run_30seed_*.sh` scripts locally.",
            "2. Run `command_to_standardize_30seed_outputs.sh`.",
            "3. Run `command_to_check_30seed_results.sh`.",
            "4. Run `python3 -m smart_crosswalk_sumo.reporting.aggregate_30seed_results aggregate ...` if needed.",
            "",
        ]
    )


def _table_readme(title: str, path: str, df: pd.DataFrame, note: str) -> str:
    cols = df.columns.tolist()
    lines = [
        f"# {title}",
        "",
        f"File: `{path}`",
        "",
        note,
        "",
        f"- rows: `{len(df)}`",
        f"- columns: `{len(cols)}`",
        "",
        "## Columns",
    ]
    if cols:
        for col in cols:
            lines.append(f"- `{col}`")
    else:
        lines.append("- file has no columns")
    return "\n".join(lines) + "\n"


def _inventory_csvs(result_root: Path, exclude_root: Path | None = None) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    exclude_root_resolved = exclude_root.resolve() if exclude_root else None
    for path in sorted(result_root.rglob("*.csv")):
        if exclude_root_resolved is not None:
            try:
                path.resolve().relative_to(exclude_root_resolved)
                continue
            except Exception:
                pass
        row_count, column_count, columns, note = _read_csv_meta(path)
        rows.append(
            {
                "file_path": str(path.relative_to(result_root)),
                "row_count": int(row_count),
                "column_count": int(column_count),
                "columns": json.dumps(columns, ensure_ascii=False),
                "has_run_group": bool("run_group" in columns),
                "has_net_group": bool("net_group" in columns),
                "has_scenario": bool("scenario" in columns),
                "has_seed": bool("seed" in columns),
                "has_crosswalk_id": bool("crosswalk_id" in columns),
                "has_elapsed_sec": bool("elapsed_sec" in columns),
                "has_runtime_columns": bool(_has_any(columns, RUNTIME_COLUMNS)),
                "has_safety_columns": bool(_has_any(columns, SAFETY_KEYWORDS)),
                "has_vehicle_columns": bool(_has_any(columns, VEHICLE_COLUMNS)),
                "has_pedestrian_columns": bool(_has_any(columns, PEDESTRIAN_COLUMNS)),
                "note": note or ("empty_csv" if column_count == 0 else ""),
            }
        )
    return pd.DataFrame(rows)


def _presence_check_df(inventory_df: pd.DataFrame, result_root: Path) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for _, inv in inventory_df.iterrows():
        path = result_root / str(inv["file_path"])
        _, _, columns, note = _read_csv_meta(path)
        lowered = [c.lower() for c in columns]
        matches = [needle for needle in SAFETY_KEYWORDS if any(needle in col for col in lowered)]
        rows.append(
            {
                "file_path": str(inv["file_path"]),
                "row_count": int(inv["row_count"]),
                "column_count": int(inv["column_count"]),
                "has_accident": bool("accident" in lowered),
                "has_crash": bool("crash" in lowered),
                "has_collision": bool("collision" in lowered),
                "has_risk": bool("risk" in lowered),
                "has_conflict": bool("conflict" in lowered),
                "has_near_miss": bool("near_miss" in lowered),
                "has_ttc": bool("ttc" in lowered),
                "has_pet": bool("pet" in lowered),
                "has_safety": bool(matches),
                "has_hazard": bool("hazard" in lowered),
                "has_proximity": bool("proximity" in lowered),
                "matched_metric_columns": json.dumps(matches, ensure_ascii=False),
                "note": note or ("matches_safety_metrics" if matches else ""),
            }
        )
    return pd.DataFrame(rows)


def _load_group_specs(input_root: Path, output_root: Path) -> list[GroupSpec]:
    recovery_root = input_root
    main_df = _read_csv(recovery_root / "main_verified_current_net_12.csv")
    signal_df = _read_csv(recovery_root / "signal_fix_candidate_table.csv")
    generated_df = _read_csv(recovery_root / "generated_signal_candidate_table.csv")

    specs = [
        GroupSpec(
            run_group="current_main_12",
            net_group="current_main",
            baseline_net_file=(recovery_root.parent / "phase_next_recovery_command_plan_20260514_200908" / "batch_03_run" / "recovery_tls_batch_network_v1.net.xml").resolve(),
            full_candidate_csv=(output_root / "manifests" / "current_main_12_candidates.csv").resolve(),
            smart_candidates=main_df.copy(),
            baseline_placeholder_crosswalk_id="BASELINE_CURRENT_MAIN_12",
        ),
        GroupSpec(
            run_group="signal_fix_9",
            net_group="signal_fix",
            baseline_net_file=(recovery_root / "signal_fix_net_v1.net.xml").resolve(),
            full_candidate_csv=(output_root / "manifests" / "signal_fix_9_candidates.csv").resolve(),
            smart_candidates=signal_df.copy(),
            baseline_placeholder_crosswalk_id="BASELINE_SIGNAL_FIX_9",
        ),
        GroupSpec(
            run_group="generated_signal_7",
            net_group="generated_signal",
            baseline_net_file=(recovery_root / "generated_signal_net_v1.net.xml").resolve(),
            full_candidate_csv=(output_root / "manifests" / "generated_signal_7_candidates.csv").resolve(),
            smart_candidates=generated_df.copy(),
            baseline_placeholder_crosswalk_id="BASELINE_GENERATED_SIGNAL_7",
        ),
    ]
    return specs


def _build_single_candidate_csvs(spec: GroupSpec, single_root: Path) -> list[Path]:
    files: list[Path] = []
    if spec.smart_candidates.empty:
        return files
    _ensure_dir(single_root / spec.run_group)
    for _, row in spec.smart_candidates.iterrows():
        crosswalk_id = str(row.get("crosswalk_id", "")).strip()
        out = single_root / spec.run_group / f"{crosswalk_id}.csv"
        _write_csv(pd.DataFrame([row.to_dict()]), out)
        files.append(out)
    return files


def _build_command_lines(spec: GroupSpec, output_root: Path, single_csv_root: Path) -> dict[str, str]:
    base_csv = spec.full_candidate_csv.as_posix()
    net_file = spec.baseline_net_file.as_posix()
    run_root = (output_root / "runs" / spec.run_group).as_posix()
    log_root = (output_root / "logs" / spec.run_group).as_posix()
    single_root = (single_csv_root / spec.run_group).as_posix()

    baseline_cmd = (
        "python3 -m smart_crosswalk_sumo.run_phase6_recovery_smoke "
        f"--candidate-csv \"{base_csv}\" --net-file \"{net_file}\" --scenario baseline "
        f"--seed \"$seed\" --sim-duration 600 --warmup 0 --step-length 0.5 "
        f"--extension-sec 0 --phase-aligned-ped-depart --ped-repeat-count 5 "
        f"--ped-repeat-spacing-sec 2 --output-dir \"$out_dir\" >\"$log_file\" 2>&1"
    )
    smart_cmd = (
        "python3 -m smart_crosswalk_sumo.run_phase6_recovery_smoke "
        f"--candidate-csv \"$candidate_csv\" --net-file \"{net_file}\" --scenario smart "
        f"--seed \"$seed\" --sim-duration 600 --warmup 0 --step-length 0.5 "
        f"--extension-sec 5.0 --phase-aligned-ped-depart --ped-repeat-count 5 "
        f"--ped-repeat-spacing-sec 2 --output-dir \"$out_dir\" >\"$log_file\" 2>&1"
    )
    return {
        "base_csv": base_csv,
        "net_file": net_file,
        "run_root": run_root,
        "log_root": log_root,
        "single_root": single_root,
        "baseline_cmd": baseline_cmd,
        "smart_cmd": smart_cmd,
    }


def _build_group_script(spec: GroupSpec, output_root: Path, single_csv_root: Path) -> str:
    ctx = _build_command_lines(spec, output_root, single_csv_root)
    ids = [str(v).strip() for v in spec.smart_candidates.get("crosswalk_id", pd.Series(dtype=str)).tolist()]
    id_array = " ".join(f'"{v}"' for v in ids)
    smart_single_paths = " ".join(
        f'"{(single_csv_root / spec.run_group / (cid + ".csv")).as_posix()}"' for cid in ids
    )
    return "\n".join(
        [
            "#!/usr/bin/env bash",
            "set -euo pipefail",
            "",
            'export SUMO_HOME="/Library/Frameworks/EclipseSUMO.framework/Versions/1.26.0/EclipseSUMO"',
            'export PATH="$SUMO_HOME/bin:$PATH"',
            'export PROJ_LIB="/Library/Frameworks/EclipseSUMO.framework/Versions/1.26.0/EclipseSUMO/framework/EclipseSUMO.framework/Resources/proj"',
            'export PYTHONPATH="/Users/junlee/Desktop/2026-1/js:${PYTHONPATH:-}"',
            "",
            f'PIPELINE_ROOT="{output_root.as_posix()}"',
            f'BASELINE_CSV="{ctx["base_csv"]}"',
            f'NET_FILE="{ctx["net_file"]}"',
            f'RUN_ROOT="{ctx["run_root"]}"',
            f'LOG_ROOT="{ctx["log_root"]}"',
            f'SINGLE_CSV_ROOT="{ctx["single_root"]}"',
            "",
            "mkdir -p \"$RUN_ROOT\" \"$LOG_ROOT\"",
            "",
            "run_smoke() {",
            "  local scenario=\"$1\"",
            "  local seed=\"$2\"",
            "  local candidate_csv=\"$3\"",
            "  local out_dir=\"$4\"",
            "  local log_file=\"$5\"",
            "  local extension_sec=\"$6\"",
            "  mkdir -p \"$(dirname \"$out_dir\")\" \"$(dirname \"$log_file\")\"",
            "  if [[ -f \"$out_dir/phase6_smoke_summary.csv\" ]]; then",
            "    echo \"skip $scenario seed$seed $out_dir\"",
            "    return 0",
            "  fi",
            "  python3 -m smart_crosswalk_sumo.run_phase6_recovery_smoke \\",
            "    --candidate-csv \"$candidate_csv\" \\",
            "    --net-file \"$NET_FILE\" \\",
            "    --scenario \"$scenario\" \\",
            "    --seed \"$seed\" \\",
            "    --sim-duration 600 \\",
            "    --warmup 0 \\",
            "    --step-length 0.5 \\",
            "    --extension-sec \"$extension_sec\" \\",
            "    --phase-aligned-ped-depart \\",
            "    --ped-repeat-count 5 \\",
            "    --ped-repeat-spacing-sec 2 \\",
            "    --output-dir \"$out_dir\" >\"$log_file\" 2>&1",
            "}",
            "",
            f"echo \"[{spec.run_group}] baseline 30 seed\"",
            "for seed in $(seq 1 30); do",
            f"  out_dir=\"$RUN_ROOT/baseline/seed$(printf '%02d' \"$seed\")\"",
            f"  log_file=\"$LOG_ROOT/baseline/seed$(printf '%02d' \"$seed\").log\"",
            "  run_smoke baseline \"$seed\" \"$BASELINE_CSV\" \"$out_dir\" \"$log_file\" 0",
            "done",
            "",
            f"SMART_IDS=({id_array})",
            f"SMART_CSVS=({smart_single_paths})",
            f"echo \"[{spec.run_group}] smart 30 seed per candidate\"",
            "for i in \"${!SMART_IDS[@]}\"; do",
            "  crosswalk_id=\"${SMART_IDS[$i]}\"",
            "  candidate_csv=\"${SMART_CSVS[$i]}\"",
            "  for seed in $(seq 1 30); do",
            "    out_dir=\"$RUN_ROOT/smart/${crosswalk_id}/seed$(printf '%02d' \"$seed\")\"",
            "    log_file=\"$LOG_ROOT/smart/${crosswalk_id}/seed$(printf '%02d' \"$seed\").log\"",
            "    run_smoke smart \"$seed\" \"$candidate_csv\" \"$out_dir\" \"$log_file\" 5.0",
            "  done",
            "done",
            "",
        ]
    )


def _build_all_groups_script(output_root: Path) -> str:
    return "\n".join(
        [
            "#!/usr/bin/env bash",
            "set -euo pipefail",
            "",
            f'PIPELINE_ROOT="{output_root.as_posix()}"',
            "",
            'bash "$PIPELINE_ROOT/commands/command_to_run_30seed_current_main_12.sh"',
            'bash "$PIPELINE_ROOT/commands/command_to_run_30seed_signal_fix_9.sh"',
            'bash "$PIPELINE_ROOT/commands/command_to_run_30seed_generated_signal_7.sh"',
            "",
        ]
    )


def _build_standardize_script(output_root: Path) -> str:
    return "\n".join(
        [
            "#!/usr/bin/env bash",
            "set -euo pipefail",
            "",
            'export SUMO_HOME="/Library/Frameworks/EclipseSUMO.framework/Versions/1.26.0/EclipseSUMO"',
            'export PATH="$SUMO_HOME/bin:$PATH"',
            'export PROJ_LIB="/Library/Frameworks/EclipseSUMO.framework/Versions/1.26.0/EclipseSUMO/framework/EclipseSUMO.framework/Resources/proj"',
            'export PYTHONPATH="/Users/junlee/Desktop/2026-1/js:${PYTHONPATH:-}"',
            "",
            f'PIPELINE_ROOT="{output_root.as_posix()}"',
            "",
            'python3 -m smart_crosswalk_sumo.reporting.aggregate_30seed_results aggregate --pipeline-root "$PIPELINE_ROOT" --copy-xml --copy-logs',
            "",
        ]
    )


def _build_check_script(output_root: Path) -> str:
    return "\n".join(
        [
            "#!/usr/bin/env bash",
            "set -euo pipefail",
            "",
            'export PYTHONPATH="/Users/junlee/Desktop/2026-1/js:${PYTHONPATH:-}"',
            "",
            f'PIPELINE_ROOT="{output_root.as_posix()}"',
            "",
            'python3 -m smart_crosswalk_sumo.reporting.aggregate_30seed_results status --pipeline-root "$PIPELINE_ROOT"',
            "",
        ]
    )


def _build_open_script(output_root: Path) -> str:
    return "\n".join(
        [
            "#!/usr/bin/env bash",
            "set -euo pipefail",
            "",
            f'PIPELINE_ROOT="{output_root.as_posix()}"',
            "open \"$PIPELINE_ROOT/README.md\"",
            "open \"$PIPELINE_ROOT/csv/seed30_group_summary.csv\" || true",
            "",
        ]
    )


def _build_cleanup_scripts(output_root: Path, result_root: Path) -> tuple[str, str]:
    dry_run = "\n".join(
        [
            "#!/usr/bin/env bash",
            "set -euo pipefail",
            "",
            'export PYTHONPATH="/Users/junlee/Desktop/2026-1/js:${PYTHONPATH:-}"',
            "",
            f'PIPELINE_ROOT="{output_root.as_posix()}"',
            f'RESULT_ROOT="{result_root.as_posix()}"',
            "",
            'python3 -m smart_crosswalk_sumo.reporting.aggregate_30seed_results cleanup --result-root "$RESULT_ROOT" --pipeline-root "$PIPELINE_ROOT"',
            "",
        ]
    )
    apply_script = "\n".join(
        [
            "#!/usr/bin/env bash",
            "set -euo pipefail",
            "",
            'export PYTHONPATH="/Users/junlee/Desktop/2026-1/js:${PYTHONPATH:-}"',
            "",
            f'PIPELINE_ROOT="{output_root.as_posix()}"',
            f'RESULT_ROOT="{result_root.as_posix()}"',
            "",
            'if [[ "${APPLY:-0}" != "1" ]]; then',
            '  echo "Dry-run only. Set APPLY=1 to archive safe files."',
            '  exit 0',
            'fi',
            "",
            'python3 -m smart_crosswalk_sumo.reporting.aggregate_30seed_results cleanup-apply --result-root "$RESULT_ROOT" --pipeline-root "$PIPELINE_ROOT"',
            "",
        ]
    )
    return dry_run, apply_script


def _build_plan_by_group(specs: list[GroupSpec], output_root: Path) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for spec in specs:
        candidate_count = int(len(spec.smart_candidates))
        rows.append(
            {
                "run_group": spec.run_group,
                "net_group": spec.net_group,
                "baseline_runs": 30,
                "smart_runs": candidate_count * 30,
                "total_runs": 30 + candidate_count * 30,
                "candidate_count": candidate_count,
                "baseline_candidate_csv": str(spec.full_candidate_csv),
                "baseline_net_file": str(spec.baseline_net_file),
                "output_root": str(output_root),
            }
        )
    return pd.DataFrame(rows)


def _build_run_manifest(specs: list[GroupSpec], output_root: Path) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    baseline_rows: list[dict[str, Any]] = []
    smart_rows: list[dict[str, Any]] = []
    single_csv_root = output_root / "manifests" / "single_candidates"
    for spec in specs:
        _ensure_dir(single_csv_root / spec.run_group)
        full_csv = spec.full_candidate_csv.as_posix()
        net_file = spec.baseline_net_file.as_posix()
        candidate_df = spec.smart_candidates.copy()
        if candidate_df.empty:
            continue
        candidate_df = candidate_df.reset_index(drop=True)
        _write_csv(candidate_df, spec.full_candidate_csv)
        for _, row in candidate_df.iterrows():
            crosswalk_id = str(row.get("crosswalk_id", "")).strip()
            _write_csv(pd.DataFrame([row.to_dict()]), single_csv_root / spec.run_group / f"{crosswalk_id}.csv")
            for seed in range(1, 31):
                smart_out = output_root / "runs" / spec.run_group / "smart" / crosswalk_id / f"seed{seed:02d}"
                smart_log = output_root / "logs" / spec.run_group / "smart" / crosswalk_id / f"seed{seed:02d}.log"
                smart_rows.append(
                    {
                        "run_id": f"{spec.run_group}_smart_{crosswalk_id}_seed{seed:02d}",
                        "run_group": spec.run_group,
                        "net_group": spec.net_group,
                        "scenario": "smart",
                        "seed": seed,
                        "crosswalk_id": crosswalk_id,
                        "candidate_csv": str(single_csv_root / spec.run_group / f"{crosswalk_id}.csv"),
                        "net_file": net_file,
                        "output_dir": str(smart_out),
                        "log_file": str(smart_log),
                        "expected_summary_csv": str(smart_out / "phase6_smoke_summary.csv"),
                        "skip_if_exists": True,
                        "command": (
                            "python3 -m smart_crosswalk_sumo.run_phase6_recovery_smoke "
                            f"--candidate-csv \"{single_csv_root / spec.run_group / f'{crosswalk_id}.csv'}\" "
                            f"--net-file \"{net_file}\" --scenario smart --seed {seed} "
                            "--sim-duration 600 --warmup 0 --step-length 0.5 --extension-sec 5.0 "
                            "--phase-aligned-ped-depart --ped-repeat-count 5 --ped-repeat-spacing-sec 2 "
                            f"--output-dir \"{smart_out}\" >\"{smart_log}\" 2>&1"
                        ),
                    }
                )
        for seed in range(1, 31):
            base_out = output_root / "runs" / spec.run_group / "baseline" / f"seed{seed:02d}"
            base_log = output_root / "logs" / spec.run_group / "baseline" / f"seed{seed:02d}.log"
            baseline_rows.append(
                {
                    "run_id": f"{spec.run_group}_baseline_seed{seed:02d}",
                    "run_group": spec.run_group,
                    "net_group": spec.net_group,
                    "scenario": "baseline",
                    "seed": seed,
                    "crosswalk_id": spec.baseline_placeholder_crosswalk_id,
                    "candidate_csv": full_csv,
                    "net_file": net_file,
                    "output_dir": str(base_out),
                    "log_file": str(base_log),
                    "expected_summary_csv": str(base_out / "phase6_smoke_summary.csv"),
                    "skip_if_exists": True,
                    "command": (
                        "python3 -m smart_crosswalk_sumo.run_phase6_recovery_smoke "
                        f"--candidate-csv \"{full_csv}\" --net-file \"{net_file}\" --scenario baseline --seed {seed} "
                        "--sim-duration 600 --warmup 0 --step-length 0.5 --extension-sec 0 "
                        "--phase-aligned-ped-depart --ped-repeat-count 5 --ped-repeat-spacing-sec 2 "
                        f"--output-dir \"{base_out}\" >\"{base_log}\" 2>&1"
                    ),
                }
            )
    baseline_df = pd.DataFrame(baseline_rows)
    smart_df = pd.DataFrame(smart_rows)
    run_manifest_df = pd.concat([baseline_df, smart_df], ignore_index=True)
    return run_manifest_df, baseline_df, smart_df


def _build_run_completion_summary(run_manifest_df: pd.DataFrame, output_root: Path, completed_df: pd.DataFrame | None = None) -> pd.DataFrame:
    total = int(len(run_manifest_df))
    baseline_total = int((run_manifest_df["scenario"] == "baseline").sum()) if not run_manifest_df.empty else 0
    smart_total = int((run_manifest_df["scenario"] == "smart").sum()) if not run_manifest_df.empty else 0
    completed = int(len(completed_df)) if completed_df is not None else 0
    return pd.DataFrame(
        [
            {
                "output_root": str(output_root),
                "planned_total_runs": total,
                "planned_baseline_runs": baseline_total,
                "planned_smart_runs": smart_total,
                "completed_runs": completed,
                "missing_runs": max(total - completed, 0),
                "created_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            }
        ]
    )


def _load_run_summary(path: Path, manifest_row: pd.Series) -> pd.DataFrame:
    summary = _read_csv(path)
    if summary.empty:
        return pd.DataFrame(
            [
                {
                    "run_group": manifest_row["run_group"],
                    "net_group": manifest_row["net_group"],
                    "run_name": manifest_row["run_id"],
                    "scenario": manifest_row["scenario"],
                    "seed": int(manifest_row["seed"]),
                    "crosswalk_id": manifest_row["crosswalk_id"],
                    "sim_duration": 600,
                    "warmup": 0,
                    "step_length": 0.5,
                    "run_start_time": "",
                    "run_end_time": "",
                    "elapsed_sec": np.nan,
                    "completed": False,
                    "output_dir": manifest_row["output_dir"],
                    "note": "missing_phase6_smoke_summary.csv",
                }
            ]
        )
    if "run_group" not in summary.columns:
        summary["run_group"] = manifest_row["run_group"]
    if "net_group" not in summary.columns:
        summary["net_group"] = manifest_row["net_group"]
    if "run_name" not in summary.columns:
        summary["run_name"] = manifest_row["run_id"]
    if "output_dir" not in summary.columns:
        summary["output_dir"] = manifest_row["output_dir"]
    if "crosswalk_id" not in summary.columns and "target_crosswalk_id" in summary.columns:
        summary["crosswalk_id"] = summary["target_crosswalk_id"]
    for col, value in {
        "run_group": manifest_row["run_group"],
        "net_group": manifest_row["net_group"],
        "scenario": manifest_row["scenario"],
        "seed": int(manifest_row["seed"]),
        "sim_duration": 600,
        "warmup": 0,
        "step_length": 0.5,
        "output_dir": manifest_row["output_dir"],
    }.items():
        if col not in summary.columns:
            summary[col] = value
    return summary


def _build_run_level_results(run_manifest_df: pd.DataFrame) -> pd.DataFrame:
    rows: list[pd.DataFrame] = []
    for _, manifest_row in run_manifest_df.iterrows():
        summary_path = Path(str(manifest_row["expected_summary_csv"]))
        summary_df = _load_run_summary(summary_path, manifest_row)
        rows.append(summary_df)
    if not rows:
        return pd.DataFrame()
    merged = pd.concat(rows, ignore_index=True)
    for col in [
        "run_group",
        "net_group",
        "run_name",
        "scenario",
        "seed",
        "crosswalk_id",
        "sim_duration",
        "warmup",
        "step_length",
        "run_start_time",
        "run_end_time",
        "elapsed_sec",
        "completed",
        "output_dir",
    ]:
        if col not in merged.columns:
            merged[col] = np.nan
    return merged


def _comparison_rows(run_level_df: pd.DataFrame, run_manifest_df: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    if run_level_df.empty:
        empty = pd.DataFrame(
            columns=[
                "run_group",
                "net_group",
                "crosswalk_id",
                "seed",
                "baseline_output_dir",
                "smart_output_dir",
                "baseline_completed",
                "smart_completed",
                "baseline_extension_count",
                "smart_extension_count",
                "baseline_elapsed_sec",
                "smart_elapsed_sec",
                "baseline_ped_count",
                "smart_ped_count",
                "baseline_vehicle_delay_mean",
                "smart_vehicle_delay_mean",
                "baseline_high_risk_event_count",
                "smart_high_risk_event_count",
                "comparison_status",
            ]
        )
        return empty, empty

    baseline_map: dict[tuple[str, str, int, str], pd.Series] = {}
    smart_map: dict[tuple[str, str, int, str], pd.Series] = {}
    for _, row in run_level_df.iterrows():
        key = (
            str(row.get("run_group", "")),
            str(row.get("net_group", "")),
            int(_safe_float(row.get("seed")) or 0),
            str(row.get("crosswalk_id", "")),
        )
        if str(row.get("scenario", "")).lower() == "baseline":
            baseline_map[key] = row
        elif str(row.get("scenario", "")).lower() == "smart":
            smart_map[key] = row

    smart_manifest = run_manifest_df[run_manifest_df["scenario"] == "smart"].copy()
    comparison_rows: list[dict[str, Any]] = []
    for _, smart_row in smart_manifest.iterrows():
        smart_key = (
            str(smart_row["run_group"]),
            str(smart_row["net_group"]),
            int(smart_row["seed"]),
            str(smart_row["crosswalk_id"]),
        )
        baseline_key = (
            str(smart_row["run_group"]),
            str(smart_row["net_group"]),
            int(smart_row["seed"]),
            str(smart_row["crosswalk_id"]),
        )
        baseline_row = baseline_map.get(baseline_key)
        smart_level_row = smart_map.get(smart_key)
        if smart_level_row is None:
            comparison_status = "missing_smart_result"
        elif baseline_row is None:
            comparison_status = "missing_baseline_result"
        else:
            baseline_completed = _safe_bool(baseline_row.get("completed"))
            smart_completed = _safe_bool(smart_level_row.get("completed"))
            baseline_ext = _safe_float(baseline_row.get("extension_count"))
            smart_ext = _safe_float(smart_level_row.get("extension_count"))
            if not baseline_completed or not smart_completed:
                comparison_status = "incomplete_run"
            elif baseline_ext is not None and baseline_ext > 0:
                comparison_status = "baseline_extension_violation"
            elif smart_ext is not None and smart_ext <= 0:
                comparison_status = "smart_extension_violation"
            else:
                comparison_status = "ready"
        comparison_rows.append(
            {
                "run_group": smart_row["run_group"],
                "net_group": smart_row["net_group"],
                "crosswalk_id": smart_row["crosswalk_id"],
                "seed": int(smart_row["seed"]),
                "baseline_output_dir": str(baseline_row["output_dir"]) if baseline_row is not None and "output_dir" in baseline_row.index else "",
                "smart_output_dir": str(smart_level_row["output_dir"]) if smart_level_row is not None and "output_dir" in smart_level_row.index else "",
                "baseline_completed": _safe_bool(baseline_row.get("completed")) if baseline_row is not None else False,
                "smart_completed": _safe_bool(smart_level_row.get("completed")) if smart_level_row is not None else False,
                "baseline_extension_count": _safe_float(baseline_row.get("extension_count")) if baseline_row is not None else np.nan,
                "smart_extension_count": _safe_float(smart_level_row.get("extension_count")) if smart_level_row is not None else np.nan,
                "baseline_elapsed_sec": _safe_float(baseline_row.get("elapsed_sec")) if baseline_row is not None else np.nan,
                "smart_elapsed_sec": _safe_float(smart_level_row.get("elapsed_sec")) if smart_level_row is not None else np.nan,
                "baseline_ped_count": _safe_float(baseline_row.get("ped_crossing_person_count")) if baseline_row is not None else np.nan,
                "smart_ped_count": _safe_float(smart_level_row.get("ped_crossing_person_count")) if smart_level_row is not None else np.nan,
                "baseline_vehicle_delay_mean": _safe_float(baseline_row.get("veh_delay_mean")) if baseline_row is not None else np.nan,
                "smart_vehicle_delay_mean": _safe_float(smart_level_row.get("veh_delay_mean")) if smart_level_row is not None else np.nan,
                "baseline_high_risk_event_count": _safe_float(baseline_row.get("high_risk_event_count")) if baseline_row is not None else np.nan,
                "smart_high_risk_event_count": _safe_float(smart_level_row.get("high_risk_event_count")) if smart_level_row is not None else np.nan,
                "comparison_status": comparison_status,
            }
        )
    comparison_df = pd.DataFrame(comparison_rows)

    metric_rows: list[dict[str, Any]] = []
    if not comparison_df.empty:
        for (run_group, net_group, crosswalk_id), sub in comparison_df.groupby(["run_group", "net_group", "crosswalk_id"], dropna=False):
            for metric in SUMMARY_METRIC_CANDIDATES:
                base_col = f"baseline_{metric}"
                smart_col = f"smart_{metric}"
                if base_col not in sub.columns or smart_col not in sub.columns:
                    continue
                base_vals = pd.to_numeric(sub[base_col], errors="coerce").dropna()
                smart_vals = pd.to_numeric(sub[smart_col], errors="coerce").dropna()
                n = int(min(len(base_vals), len(smart_vals)))
                if n == 0:
                    continue
                baseline_mean = float(base_vals.mean())
                smart_mean = float(smart_vals.mean())
                baseline_std = float(base_vals.std(ddof=1)) if len(base_vals) > 1 else 0.0
                smart_std = float(smart_vals.std(ddof=1)) if len(smart_vals) > 1 else 0.0
                difference = smart_mean - baseline_mean
                percent_change = (difference / baseline_mean * 100.0) if abs(baseline_mean) > 1e-12 else np.nan
                if metric in {"veh_delay_mean", "veh_delay_max", "vehicle_delay_mean", "vehicle_delay_max", "elapsed_sec"}:
                    interpretation = "lower is better"
                elif metric in {"extension_count"}:
                    interpretation = "baseline should remain 0; smart should be > 0"
                elif metric in {"ped_crossing_person_count", "ped_crossing_presence_steps"}:
                    interpretation = "compare traffic exposure / demand consistency"
                else:
                    interpretation = "compare with context"
                metric_rows.append(
                    {
                        "run_group": run_group,
                        "net_group": net_group,
                        "crosswalk_id": crosswalk_id,
                        "metric": metric,
                        "baseline_mean": baseline_mean,
                        "smart_mean": smart_mean,
                        "difference": difference,
                        "percent_change": percent_change,
                        "baseline_std": baseline_std,
                        "smart_std": smart_std,
                        "n_seeds": n,
                        "interpretation": interpretation,
                    }
                )
    metric_df = pd.DataFrame(
        metric_rows,
        columns=[
            "run_group",
            "net_group",
            "crosswalk_id",
            "metric",
            "baseline_mean",
            "smart_mean",
            "difference",
            "percent_change",
            "baseline_std",
            "smart_std",
            "n_seeds",
            "interpretation",
        ],
    )
    return comparison_df, metric_df


def _group_summary_df(run_level_df: pd.DataFrame, comparison_df: pd.DataFrame, specs: list[GroupSpec]) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for spec in specs:
        group_runs = run_level_df[run_level_df["run_group"] == spec.run_group].copy()
        comp = comparison_df[comparison_df["run_group"] == spec.run_group].copy() if not comparison_df.empty else pd.DataFrame()
        rows.append(
            {
                "run_group": spec.run_group,
                "net_group": spec.net_group,
                "candidate_count": int(len(spec.smart_candidates)),
                "expected_baseline_runs": 30,
                "expected_smart_runs": int(len(spec.smart_candidates)) * 30,
                "expected_total_runs": 30 + int(len(spec.smart_candidates)) * 30,
                "completed_runs": int(group_runs["completed"].fillna(False).astype(bool).sum()) if not group_runs.empty and "completed" in group_runs.columns else 0,
                "comparison_ready_runs": int((comp["comparison_status"] == "ready").sum()) if not comp.empty and "comparison_status" in comp.columns else 0,
                "baseline_mean_elapsed_sec": float(pd.to_numeric(group_runs[group_runs["scenario"] == "baseline"]["elapsed_sec"], errors="coerce").dropna().mean()) if not group_runs.empty else np.nan,
                "smart_mean_elapsed_sec": float(pd.to_numeric(group_runs[group_runs["scenario"] == "smart"]["elapsed_sec"], errors="coerce").dropna().mean()) if not group_runs.empty else np.nan,
            }
        )
    return pd.DataFrame(rows)


def _run_completion_summary_df(run_manifest_df: pd.DataFrame, run_level_df: pd.DataFrame, output_root: Path) -> pd.DataFrame:
    completed_outputs = 0
    if not run_level_df.empty and "output_dir" in run_level_df.columns and "completed" in run_level_df.columns:
        completed_outputs = int(run_level_df["completed"].fillna(False).astype(bool).sum())
    return pd.DataFrame(
        [
            {
                "output_root": str(output_root),
                "planned_total_runs": int(len(run_manifest_df)),
                "planned_baseline_runs": int((run_manifest_df["scenario"] == "baseline").sum()) if not run_manifest_df.empty else 0,
                "planned_smart_runs": int((run_manifest_df["scenario"] == "smart").sum()) if not run_manifest_df.empty else 0,
                "completed_runs": completed_outputs,
                "missing_runs": max(int(len(run_manifest_df)) - completed_outputs, 0),
                "completed_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            }
        ]
    )


def _safety_surrogate_summary(run_level_df: pd.DataFrame) -> tuple[pd.DataFrame | None, pd.DataFrame]:
    needed = {"high_risk_event_count", "min_ttc", "mean_ttc", "min_pet", "mean_pet"}
    if run_level_df.empty or not needed.issubset(set(run_level_df.columns)):
        missing = pd.DataFrame(
            [
                {
                    "status": "missing",
                    "reason": "No completed run-level CSV exposes full surrogate columns: high_risk_event_count, min_ttc, mean_ttc, min_pet, mean_pet.",
                    "available_safety_columns": ", ".join(sorted([c for c in run_level_df.columns if _has_any([c], SAFETY_KEYWORDS)])),
                }
            ]
        )
        return None, missing
    rows: list[dict[str, Any]] = []
    for _, row in run_level_df.iterrows():
        rows.append(
            {
                "run_group": row.get("run_group", ""),
                "net_group": row.get("net_group", ""),
                "scenario": row.get("scenario", ""),
                "seed": row.get("seed", np.nan),
                "crosswalk_id": row.get("crosswalk_id", ""),
                "ped_crossing_person_count": _safe_float(row.get("ped_crossing_person_count")),
                "ped_crossing_presence_steps": _safe_float(row.get("ped_crossing_presence_steps")),
                "extension_count": _safe_float(row.get("extension_count")),
                "ped_vehicle_conflict_count": _safe_float(row.get("ped_vehicle_conflict_count")),
                "high_risk_event_count": _safe_float(row.get("high_risk_event_count")),
                "near_miss_count": _safe_float(row.get("near_miss_count")),
                "min_ttc": _safe_float(row.get("min_ttc")),
                "mean_ttc": _safe_float(row.get("mean_ttc")),
                "min_pet": _safe_float(row.get("min_pet")),
                "mean_pet": _safe_float(row.get("mean_pet")),
                "risk_event_per_pedestrian": np.nan,
                "risk_event_per_crossing_presence_step": np.nan,
                "elapsed_sec": _safe_float(row.get("elapsed_sec")),
            }
        )
    out = pd.DataFrame(rows)
    return out, pd.DataFrame()


def _readme_files(output_root: Path, tables: dict[str, pd.DataFrame], notes: dict[str, str]) -> None:
    readme_dir = output_root / "readme"
    _ensure_dir(readme_dir)
    for filename, df in tables.items():
        note = notes.get(filename, "")
        _write_text(readme_dir / f"{Path(filename).stem}_README.md", _table_readme(filename, filename, df, note))


def _write_prepare_outputs(
    input_root: Path,
    output_root: Path,
    result_root: Path,
    specs: list[GroupSpec],
) -> dict[str, pd.DataFrame]:
    _ensure_dir(output_root)
    for rel in ["csv", "xml", "logs", "readme", "commands", "manifests", "figures", "runs"]:
        _ensure_dir(output_root / rel)

    single_csv_root = output_root / "manifests" / "single_candidates"
    for spec in specs:
        _build_single_candidate_csvs(spec, single_csv_root)

    run_manifest_df, baseline_df, smart_df = _build_run_manifest(specs, output_root)
    plan_df = _build_plan_by_group(specs, output_root)
    run_level_summary = _build_run_level_results(run_manifest_df)
    comparison_df, metric_df = _comparison_rows(run_level_summary, run_manifest_df)
    group_summary_df = _group_summary_df(run_level_summary, comparison_df, specs)
    completion_summary_df = _run_completion_summary_df(run_manifest_df, run_level_summary, output_root)

    inventory_df = _inventory_csvs(result_root, exclude_root=output_root)
    presence_df = _presence_check_df(inventory_df, result_root) if not inventory_df.empty else pd.DataFrame()
    safety_summary_df, safety_missing_df = _safety_surrogate_summary(run_level_summary)
    cleanup_scan_tmp = Path(tempfile.mkdtemp(prefix="cleanup_scan_"))
    cleanup_audit_df = build_cleanup_audit(result_root, cleanup_scan_tmp) if result_root.exists() else pd.DataFrame()
    if not cleanup_audit_df.empty:
        cleanup_audit_df = cleanup_audit_df.rename(
            columns={
                "path": "file_path",
                "size_bytes": "size_bytes",
                "size_mb": "size_mb",
                "category": "cleanup_status",
                "reason": "reason",
                "suggested_action": "suggested_action",
            }
        )
        try:
            pipeline_rel = output_root.relative_to(result_root).as_posix()
            cleanup_audit_df = cleanup_audit_df[~cleanup_audit_df["file_path"].astype(str).str.startswith(pipeline_rel)].copy()
        except Exception:
            pass
        cleanup_audit_df["safe_to_delete"] = cleanup_audit_df["cleanup_status"].isin({"safe_to_archive_after_backup", "safe_to_delete", "safe_to_archive", "duplicate_old_version", "generated_raw_heavy", "log_only"})
    else:
        cleanup_audit_df = pd.DataFrame(columns=["file_path", "type", "size_bytes", "size_mb", "cleanup_status", "reason", "suggested_action", "safe_to_delete"])

    # Cleanup inventory split.
    if not cleanup_audit_df.empty:
        protected_df = cleanup_audit_df[cleanup_audit_df["cleanup_status"] == "protected"].copy()
        review_df = cleanup_audit_df[cleanup_audit_df["cleanup_status"] == "review_needed"].copy()
        safe_df = cleanup_audit_df[cleanup_audit_df["safe_to_delete"] == True].copy()  # noqa: E712
    else:
        protected_df = cleanup_audit_df.copy()
        review_df = cleanup_audit_df.copy()
        safe_df = cleanup_audit_df.copy()

    # Main CSV outputs.
    _write_csv(run_manifest_df, output_root / "run_manifest.csv")
    _write_csv(run_manifest_df, output_root / "manifests" / "run_manifest.csv")
    _write_csv(baseline_df, output_root / "manifests" / "baseline_run_manifest.csv")
    _write_csv(smart_df, output_root / "manifests" / "smart_run_manifest.csv")
    _write_csv(plan_df, output_root / "manifests" / "run_plan_by_group.csv")
    _write_csv(run_level_summary, output_root / "csv" / "seed30_run_level_results.csv")
    _write_csv(comparison_df, output_root / "csv" / "seed30_baseline_smart_comparison.csv")
    _write_csv(metric_df, output_root / "csv" / "seed30_metric_summary.csv")
    _write_csv(group_summary_df, output_root / "csv" / "seed30_group_summary.csv")
    _write_csv(completion_summary_df, output_root / "run_completion_summary.csv")
    _write_csv(completion_summary_df, output_root / "csv" / "run_completion_summary.csv")
    _write_csv(inventory_df, output_root / "csv" / "csv_output_inventory.csv")
    audit_summary_df = pd.DataFrame(
        [
            {
                "result_root": str(result_root),
                "output_root": str(output_root),
                "csv_file_count": int(len(inventory_df)),
                "csv_with_runtime_columns": int(inventory_df["has_runtime_columns"].sum()) if not inventory_df.empty else 0,
                "csv_with_safety_columns": int(inventory_df["has_safety_columns"].sum()) if not inventory_df.empty else 0,
                "csv_with_vehicle_columns": int(inventory_df["has_vehicle_columns"].sum()) if not inventory_df.empty else 0,
                "csv_with_pedestrian_columns": int(inventory_df["has_pedestrian_columns"].sum()) if not inventory_df.empty else 0,
                "missing_run_group": int((~inventory_df["has_run_group"]).sum()) if not inventory_df.empty else 0,
                "missing_net_group": int((~inventory_df["has_net_group"]).sum()) if not inventory_df.empty else 0,
            }
        ]
    )
    _write_csv(audit_summary_df, output_root / "csv" / "csv_pipeline_audit_summary.csv")
    _write_csv(presence_df, output_root / "csv" / "safety_metric_presence_check.csv")
    if safety_summary_df is not None and not safety_summary_df.empty:
        _write_csv(safety_summary_df, output_root / "csv" / "safety_surrogate_summary.csv")
    else:
        _write_csv(safety_missing_df, output_root / "csv" / "safety_surrogate_missing_reason.csv")
    _write_csv(cleanup_audit_df, output_root / "csv" / "result_cleanup_inventory.csv")
    _write_csv(safe_df, output_root / "csv" / "result_cleanup_safe_to_delete.csv")
    _write_csv(review_df, output_root / "csv" / "result_cleanup_review_needed.csv")
    _write_csv(protected_df, output_root / "csv" / "result_cleanup_protected_files.csv")
    _write_text(
        output_root / "readme" / "result_cleanup_summary.md",
        "\n".join(
            [
                "# Result Cleanup Summary",
                "",
                f"Result root: `{result_root}`",
                f"Pipeline root: `{output_root}`",
                "",
                "This inventory is conservative. Safe rows are archive candidates, not auto-delete targets.",
                "",
                f"- total rows: `{len(cleanup_audit_df)}`",
                f"- safe_to_delete rows: `{len(safe_df)}`",
                f"- review_needed rows: `{len(review_df)}`",
                f"- protected rows: `{len(protected_df)}`",
                "",
            ]
        ),
    )

    # README files.
    _write_text(output_root / "README.md", _root_readme_text(output_root))
    _readme_files(
        output_root,
        {
            "run_manifest.csv": run_manifest_df,
            "run_completion_summary.csv": completion_summary_df,
            "seed30_run_level_results.csv": run_level_summary,
            "seed30_baseline_smart_comparison.csv": comparison_df,
            "seed30_metric_summary.csv": metric_df,
            "seed30_group_summary.csv": group_summary_df,
            "csv_output_inventory.csv": inventory_df,
            "csv_pipeline_audit_summary.csv": audit_summary_df,
            "safety_metric_presence_check.csv": presence_df,
            "safety_surrogate_summary.csv": safety_summary_df if safety_summary_df is not None else pd.DataFrame(),
            "safety_surrogate_missing_reason.csv": safety_missing_df,
            "result_cleanup_inventory.csv": cleanup_audit_df,
            "result_cleanup_safe_to_delete.csv": safe_df,
            "result_cleanup_review_needed.csv": review_df,
            "result_cleanup_protected_files.csv": protected_df,
        },
        {
            "run_manifest.csv": "Planned run manifest for 30-seed execution. Baseline rows are one per seed; smart rows are one per candidate per seed.",
            "run_completion_summary.csv": "Planned run completion summary for the pipeline root.",
            "seed30_run_level_results.csv": "One row per run-level output row from phase6_smoke_summary.csv.",
            "seed30_baseline_smart_comparison.csv": "Baseline vs smart comparison matched by run_group, net_group, seed, and crosswalk_id.",
            "seed30_metric_summary.csv": "Grouped metric summary across seeds.",
            "seed30_group_summary.csv": "High-level per-group completion summary.",
            "csv_output_inventory.csv": "Inventory of CSV files under the result tree. Intended to catch missing metadata columns.",
            "csv_pipeline_audit_summary.csv": "Audit summary of metadata coverage.",
            "safety_metric_presence_check.csv": "Keyword-based safety metric presence scan.",
            "safety_surrogate_summary.csv": "Safety surrogate summary when complete surrogate metrics are present.",
            "safety_surrogate_missing_reason.csv": "Fallback when full safety surrogate metrics are absent.",
            "result_cleanup_inventory.csv": "Dry-run cleanup inventory. Default action is review, not delete.",
            "result_cleanup_safe_to_delete.csv": "Conservative subset of cleanup inventory that can be archived after user review.",
            "result_cleanup_review_needed.csv": "Items that need manual review.",
            "result_cleanup_protected_files.csv": "Protected files that must not be deleted.",
        },
    )

    # Command files.
    _write_text(output_root / "commands" / "command_to_run_30seed_current_main_12.sh", _build_group_script(specs[0], output_root, single_csv_root))
    _write_text(output_root / "commands" / "command_to_run_30seed_signal_fix_9.sh", _build_group_script(specs[1], output_root, single_csv_root))
    _write_text(output_root / "commands" / "command_to_run_30seed_generated_signal_7.sh", _build_group_script(specs[2], output_root, single_csv_root))
    _write_text(output_root / "commands" / "command_to_run_30seed_all_groups.sh", _build_all_groups_script(output_root))
    _write_text(output_root / "commands" / "command_to_standardize_30seed_outputs.sh", _build_standardize_script(output_root))
    _write_text(output_root / "commands" / "command_to_check_30seed_results.sh", _build_check_script(output_root))
    _write_text(output_root / "commands" / "command_to_open_30seed_summary.sh", _build_open_script(output_root))
    dry_run_script, apply_script = _build_cleanup_scripts(output_root, result_root)
    _write_text(output_root / "commands" / "command_to_cleanup_result_dry_run.sh", dry_run_script)
    _write_text(output_root / "commands" / "command_to_cleanup_result_apply.sh", apply_script)

    shutil.rmtree(cleanup_scan_tmp, ignore_errors=True)

    for script in (output_root / "commands").glob("*.sh"):
        script.chmod(0o755)

    return {
        "run_manifest": run_manifest_df,
        "baseline_manifest": baseline_df,
        "smart_manifest": smart_df,
        "run_plan_by_group": plan_df,
        "run_level_results": run_level_summary,
        "baseline_smart_comparison": comparison_df,
        "metric_summary": metric_df,
        "group_summary": group_summary_df,
        "completion_summary": completion_summary_df,
        "csv_output_inventory": inventory_df,
        "csv_pipeline_audit_summary": audit_summary_df,
        "safety_metric_presence_check": presence_df,
        "safety_surrogate_summary": safety_summary_df if safety_summary_df is not None else pd.DataFrame(),
        "safety_surrogate_missing_reason": safety_missing_df,
        "result_cleanup_inventory": cleanup_audit_df,
        "result_cleanup_safe_to_delete": safe_df,
        "result_cleanup_review_needed": review_df,
        "result_cleanup_protected_files": protected_df,
    }


def _copy_xml_like(result_root: Path, output_root: Path, copy_logs: bool = False) -> None:
    target_dir = output_root / "xml"
    _ensure_dir(target_dir)
    for path in sorted(result_root.rglob("*")):
        if not path.is_file():
            continue
        if path.suffix.lower() not in {".xml", ".sumocfg"} and not path.name.endswith(".rou.xml"):
            continue
        try:
            path.resolve().relative_to(output_root.resolve())
            continue
        except Exception:
            pass
        rel = path.relative_to(result_root).as_posix().replace("/", "__")
        dst = target_dir / rel
        _ensure_dir(dst.parent)
        shutil.copy2(path, dst)
    if copy_logs:
        log_dir = output_root / "logs"
        _ensure_dir(log_dir)
        for path in sorted(result_root.rglob("*.log")):
            try:
                path.resolve().relative_to(output_root.resolve())
                continue
            except Exception:
                pass
            rel = path.relative_to(result_root).as_posix().replace("/", "__")
            dst = log_dir / rel
            _ensure_dir(dst.parent)
            shutil.copy2(path, dst)


def _status_report(pipeline_root: Path) -> dict[str, Any]:
    run_manifest = _read_csv(pipeline_root / "run_manifest.csv")
    run_level = _read_csv(pipeline_root / "csv" / "seed30_run_level_results.csv")
    completed = int(run_level["completed"].fillna(False).astype(bool).sum()) if not run_level.empty and "completed" in run_level.columns else 0
    missing = max(int(len(run_manifest)) - completed, 0)
    group_counts = {}
    if not run_level.empty and {"run_group", "completed"}.issubset(run_level.columns):
        for group, sub in run_level.groupby("run_group", dropna=False):
            group_counts[str(group)] = int(sub["completed"].fillna(False).astype(bool).sum())
    baseline_ok = True
    smart_ok = True
    if not run_level.empty:
        baseline_rows = run_level[run_level["scenario"] == "baseline"]
        smart_rows = run_level[run_level["scenario"] == "smart"]
        if not baseline_rows.empty and "extension_count" in baseline_rows.columns:
            baseline_ok = bool((pd.to_numeric(baseline_rows["extension_count"], errors="coerce").fillna(0) == 0).all())
        if not smart_rows.empty and "extension_count" in smart_rows.columns:
            smart_ok = bool((pd.to_numeric(smart_rows["extension_count"], errors="coerce").fillna(0) > 0).all())
    return {
        "expected_total_runs": int(len(run_manifest)),
        "completed_runs": completed,
        "missing_runs": missing,
        "group_completed_count": group_counts,
        "baseline_extension_ok": baseline_ok,
        "smart_extension_ok": smart_ok,
        "csv_exists": (pipeline_root / "csv").is_dir(),
        "xml_exists": (pipeline_root / "xml").is_dir(),
        "logs_exists": (pipeline_root / "logs").is_dir(),
        "csv_generated": (pipeline_root / "csv" / "seed30_run_level_results.csv").is_file(),
    }


def _cleanup_apply(result_root: Path, pipeline_root: Path) -> int:
    df = _read_csv(pipeline_root / "csv" / "result_cleanup_safe_to_delete.csv")
    if df.empty:
        return 0
    archive_root = result_root / f"_archive_unused_{_now_stamp()}"
    _ensure_dir(archive_root)
    moved = 0
    for _, row in df.iterrows():
        rel = str(row.get("file_path", "")).strip()
        if not rel:
            continue
        src = result_root / rel
        if not src.exists():
            continue
        dst = archive_root / rel
        _ensure_dir(dst.parent)
        shutil.move(str(src), str(dst))
        moved += 1
    _write_csv(df, archive_root / "quarantine_manifest.csv")
    return moved


def cmd_prepare(args: argparse.Namespace) -> None:
    input_root = Path(args.input_root).expanduser().resolve()
    result_root = Path(args.result_root).expanduser().resolve()
    output_root = Path(args.output_root).expanduser().resolve() if args.output_root else result_root / f"phase_next_30seed_28_ready_pipeline_{_now_stamp()}"
    specs = _load_group_specs(input_root, output_root)
    _write_prepare_outputs(input_root, output_root, result_root, specs)
    print(json.dumps({"output_root": str(output_root), "planned_total_runs": 930, "group_count": 3}, ensure_ascii=False, indent=2))


def cmd_aggregate(args: argparse.Namespace) -> None:
    pipeline_root = Path(args.pipeline_root).expanduser().resolve()
    run_manifest = _read_csv(pipeline_root / "run_manifest.csv")
    if run_manifest.empty:
        raise FileNotFoundError(pipeline_root / "run_manifest.csv")
    result_root = pipeline_root.parent
    run_level_df = _build_run_level_results(run_manifest)
    comparison_df, metric_df = _comparison_rows(run_level_df, run_manifest)
    specs = _load_group_specs(Path(args.input_root).expanduser().resolve() if args.input_root else pipeline_root.parent / "phase_next_top50_max_installation_recovery_20260516_002620", pipeline_root)
    group_summary_df = _group_summary_df(run_level_df, comparison_df, specs)
    completion_summary_df = _run_completion_summary_df(run_manifest, run_level_df, pipeline_root)
    _write_csv(run_level_df, pipeline_root / "csv" / "seed30_run_level_results.csv")
    _write_csv(comparison_df, pipeline_root / "csv" / "seed30_baseline_smart_comparison.csv")
    _write_csv(metric_df, pipeline_root / "csv" / "seed30_metric_summary.csv")
    _write_csv(group_summary_df, pipeline_root / "csv" / "seed30_group_summary.csv")
    _write_csv(completion_summary_df, pipeline_root / "csv" / "run_completion_summary.csv")
    _write_csv(completion_summary_df, pipeline_root / "run_completion_summary.csv")
    inventory_df = _inventory_csvs(result_root, exclude_root=pipeline_root)
    presence_df = _presence_check_df(inventory_df, result_root) if not inventory_df.empty else pd.DataFrame()
    cleanup_scan_tmp = Path(tempfile.mkdtemp(prefix="cleanup_scan_"))
    cleanup_audit_df = build_cleanup_audit(result_root, cleanup_scan_tmp) if result_root.exists() else pd.DataFrame()
    if not cleanup_audit_df.empty:
        cleanup_audit_df = cleanup_audit_df.rename(columns={"path": "file_path", "category": "cleanup_status"})
        try:
            pipeline_rel = pipeline_root.relative_to(result_root).as_posix()
            cleanup_audit_df = cleanup_audit_df[~cleanup_audit_df["file_path"].astype(str).str.startswith(pipeline_rel)].copy()
        except Exception:
            pass
        cleanup_audit_df["safe_to_delete"] = cleanup_audit_df["cleanup_status"].isin({"safe_to_archive", "duplicate_old_version", "generated_raw_heavy", "log_only"})
    else:
        cleanup_audit_df = pd.DataFrame(columns=["file_path", "type", "size_bytes", "size_mb", "cleanup_status", "reason", "suggested_action", "safe_to_delete"])
    safe_df = cleanup_audit_df[cleanup_audit_df["safe_to_delete"] == True].copy() if not cleanup_audit_df.empty else cleanup_audit_df.copy()  # noqa: E712
    review_df = cleanup_audit_df[cleanup_audit_df["cleanup_status"] == "review_needed"].copy() if not cleanup_audit_df.empty else cleanup_audit_df.copy()
    protected_df = cleanup_audit_df[cleanup_audit_df["cleanup_status"] == "protected"].copy() if not cleanup_audit_df.empty else cleanup_audit_df.copy()
    audit_summary_df = pd.DataFrame(
        [
            {
                "result_root": str(result_root),
                "output_root": str(pipeline_root),
                "csv_file_count": int(len(inventory_df)),
                "csv_with_runtime_columns": int(inventory_df["has_runtime_columns"].sum()) if not inventory_df.empty else 0,
                "csv_with_safety_columns": int(inventory_df["has_safety_columns"].sum()) if not inventory_df.empty else 0,
                "csv_with_vehicle_columns": int(inventory_df["has_vehicle_columns"].sum()) if not inventory_df.empty else 0,
                "csv_with_pedestrian_columns": int(inventory_df["has_pedestrian_columns"].sum()) if not inventory_df.empty else 0,
                "missing_run_group": int((~inventory_df["has_run_group"]).sum()) if not inventory_df.empty else 0,
                "missing_net_group": int((~inventory_df["has_net_group"]).sum()) if not inventory_df.empty else 0,
            }
        ]
    )
    safety_summary_df, safety_missing_df = _safety_surrogate_summary(run_level_df)
    _write_csv(inventory_df, pipeline_root / "csv" / "csv_output_inventory.csv")
    _write_csv(audit_summary_df, pipeline_root / "csv" / "csv_pipeline_audit_summary.csv")
    _write_csv(presence_df, pipeline_root / "csv" / "safety_metric_presence_check.csv")
    if safety_summary_df is not None and not safety_summary_df.empty:
        _write_csv(safety_summary_df, pipeline_root / "csv" / "safety_surrogate_summary.csv")
    else:
        _write_csv(safety_missing_df, pipeline_root / "csv" / "safety_surrogate_missing_reason.csv")
    _write_csv(cleanup_audit_df, pipeline_root / "csv" / "result_cleanup_inventory.csv")
    _write_csv(safe_df, pipeline_root / "csv" / "result_cleanup_safe_to_delete.csv")
    _write_csv(review_df, pipeline_root / "csv" / "result_cleanup_review_needed.csv")
    _write_csv(protected_df, pipeline_root / "csv" / "result_cleanup_protected_files.csv")
    _write_text(
        pipeline_root / "readme" / "result_cleanup_summary.md",
        "\n".join(
            [
                "# Result Cleanup Summary",
                "",
                f"Result root: `{result_root}`",
                f"Pipeline root: `{pipeline_root}`",
                "",
                "This inventory is conservative. Safe rows are archive candidates, not auto-delete targets.",
                "",
                f"- total rows: `{len(cleanup_audit_df)}`",
                f"- safe_to_delete rows: `{len(safe_df)}`",
                f"- review_needed rows: `{len(review_df)}`",
                f"- protected rows: `{len(protected_df)}`",
                "",
            ]
        ),
    )
    shutil.rmtree(cleanup_scan_tmp, ignore_errors=True)
    if args.copy_xml:
        _copy_xml_like(pipeline_root / "runs", pipeline_root, copy_logs=args.copy_logs)
    print(json.dumps({"output_root": str(pipeline_root), "completed_runs": int(run_level_df["completed"].fillna(False).astype(bool).sum()) if not run_level_df.empty and "completed" in run_level_df.columns else 0}, ensure_ascii=False, indent=2))


def cmd_status(args: argparse.Namespace) -> None:
    pipeline_root = Path(args.pipeline_root).expanduser().resolve()
    print(json.dumps(_status_report(pipeline_root), ensure_ascii=False, indent=2))


def cmd_cleanup(args: argparse.Namespace) -> None:
    result_root = Path(args.result_root).expanduser().resolve()
    pipeline_root = Path(args.pipeline_root).expanduser().resolve()
    output_dir = Path(tempfile.mkdtemp(prefix="cleanup_refresh_"))
    cleanup_df = build_cleanup_audit(result_root, output_dir)
    # Normalize names into pipeline root csv folder.
    cleanup_df = cleanup_df.rename(columns={"path": "file_path", "category": "cleanup_status"})
    try:
        pipeline_rel = pipeline_root.relative_to(result_root).as_posix()
        cleanup_df = cleanup_df[~cleanup_df["file_path"].astype(str).str.startswith(pipeline_rel)].copy()
    except Exception:
        pass
    cleanup_df["safe_to_delete"] = cleanup_df["cleanup_status"].isin({"safe_to_archive", "duplicate_old_version", "generated_raw_heavy", "log_only"})
    _write_csv(cleanup_df, pipeline_root / "csv" / "result_cleanup_inventory.csv")
    _write_csv(cleanup_df[cleanup_df["safe_to_delete"] == True].copy(), pipeline_root / "csv" / "result_cleanup_safe_to_delete.csv")  # noqa: E712
    _write_csv(cleanup_df[cleanup_df["cleanup_status"] == "review_needed"].copy(), pipeline_root / "csv" / "result_cleanup_review_needed.csv")
    _write_csv(cleanup_df[cleanup_df["cleanup_status"] == "protected"].copy(), pipeline_root / "csv" / "result_cleanup_protected_files.csv")
    _write_text(
        pipeline_root / "readme" / "result_cleanup_summary.md",
        "\n".join(
            [
                "# Result Cleanup Summary",
                "",
                f"Result root: `{result_root}`",
                f"Pipeline root: `{pipeline_root}`",
                "",
                "This is a dry-run inventory only.",
                "Safe items are archive candidates, not auto-delete targets.",
                "",
            ]
        ),
    )
    shutil.rmtree(output_dir, ignore_errors=True)
    print(json.dumps({"output_root": str(pipeline_root), "cleanup_rows": int(len(cleanup_df))}, ensure_ascii=False, indent=2))


def cmd_cleanup_apply(args: argparse.Namespace) -> None:
    result_root = Path(args.result_root).expanduser().resolve()
    pipeline_root = Path(args.pipeline_root).expanduser().resolve()
    moved = _cleanup_apply(result_root, pipeline_root)
    print(json.dumps({"output_root": str(pipeline_root), "moved": moved}, ensure_ascii=False, indent=2))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Prepare and aggregate the 30-seed pipeline.")
    sub = parser.add_subparsers(dest="cmd", required=True)

    prepare = sub.add_parser("prepare", help="build manifests, cleanup inventory, command scripts, and README files")
    prepare.add_argument("--input-root", default="result/phase_next_top50_max_installation_recovery_20260516_002620")
    prepare.add_argument("--result-root", default="result")
    prepare.add_argument("--output-root", default=None)
    prepare.set_defaults(func=cmd_prepare)

    aggregate = sub.add_parser("aggregate", help="aggregate completed 30-seed run outputs")
    aggregate.add_argument("--pipeline-root", required=True)
    aggregate.add_argument("--input-root", default=None)
    aggregate.add_argument("--copy-xml", action="store_true")
    aggregate.add_argument("--copy-logs", action="store_true")
    aggregate.set_defaults(func=cmd_aggregate)

    status = sub.add_parser("status", help="print pipeline completion status")
    status.add_argument("--pipeline-root", required=True)
    status.set_defaults(func=cmd_status)

    cleanup = sub.add_parser("cleanup", help="refresh cleanup inventories")
    cleanup.add_argument("--result-root", default="result")
    cleanup.add_argument("--pipeline-root", required=True)
    cleanup.set_defaults(func=cmd_cleanup)

    cleanup_apply = sub.add_parser("cleanup-apply", help="archive safe cleanup candidates")
    cleanup_apply.add_argument("--result-root", default="result")
    cleanup_apply.add_argument("--pipeline-root", required=True)
    cleanup_apply.set_defaults(func=cmd_cleanup_apply)

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
