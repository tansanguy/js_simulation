#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import shutil
import tempfile
import xml.etree.ElementTree as ET
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

SAMPLED10_METRIC_SAMPLE_INTERVAL = 10
SAMPLED10_VEHICLE_SAMPLE_INTERVAL = 10
SAMPLED10_PROGRESS_INTERVAL = 60
SAMPLED10_EXTENSION_SEC = 5.0
SAMPLED10_SIM_DURATION = 600
SAMPLED10_WARMUP = 0
SAMPLED10_STEP_LENGTH = 0.1
PROJECT_ROOT = Path(__file__).resolve().parents[2]


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


def _default_candidate_metadata_source() -> Path:
    return PROJECT_ROOT / "smart_crosswalk_sumo" / "data" / "crosswalk_stepwise_result_50m.csv"


def _portable_path(path: Path | str) -> str:
    p = Path(path)
    try:
        if p.is_absolute():
            return p.resolve().relative_to(PROJECT_ROOT).as_posix()
    except Exception:
        pass
    return p.as_posix()


def _portableize_path_columns(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    for column in ["source_file", "batch_network_file", "net_file", "candidate_csv"]:
        if column not in out.columns:
            continue
        out[column] = out[column].map(
            lambda value: _portable_path(str(value).strip())
            if str(value).strip() and str(value).strip().lower() not in {"nan", "none", "null"}
            else value
        )
    return out


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


def _as_int(value: Any, default: int = -1) -> int:
    try:
        if value is None:
            return default
        if isinstance(value, float) and np.isnan(value):
            return default
        return int(value)
    except Exception:
        return default


def _benchmark_outcome(summary_path: Path, manifest_row: pd.Series) -> dict[str, Any]:
    output_dir = Path(str(manifest_row.get("output_dir", summary_path.parent)))
    benchmark_path = output_dir / "benchmark_timing.json"
    payload = _read_json(benchmark_path)
    failed_cases_count = _as_int(payload.get("failed_cases_count"), -1)
    if failed_cases_count < 0:
        failed_cases_count = _read_csv_meta(output_dir / "failed_cases.csv")[0]
    result_rows = _as_int(payload.get("result_rows"), -1)
    baseline_result_rows = _as_int(payload.get("baseline_result_rows"), -1)
    smart_result_rows = _as_int(payload.get("smart_result_rows"), -1)
    run_success = payload.get("run_success") is True
    completed = bool(
        summary_path.exists()
        and benchmark_path.exists()
        and run_success
        and failed_cases_count == 0
        and baseline_result_rows >= 1
        and smart_result_rows >= 1
    )
    if not benchmark_path.exists():
        failure_reason = "benchmark_timing.json missing"
    else:
        failure_reason = str(payload.get("failure_reason") or "")
    return {
        "completed": completed,
        "benchmark_timing_path": str(benchmark_path),
        "benchmark_timing_exists": benchmark_path.exists(),
        "run_success": run_success,
        "failure_reason": failure_reason,
        "failed_cases_count": failed_cases_count,
        "result_rows": result_rows,
        "baseline_result_rows": baseline_result_rows,
        "smart_result_rows": smart_result_rows,
        "experiment_mode": payload.get("experiment_mode", ""),
        "metrics_exact": payload.get("metrics_exact", pd.NA),
    }


def _completed_manifest_count(run_level_df: pd.DataFrame) -> int:
    if run_level_df.empty or "completed" not in run_level_df.columns:
        return 0
    if "manifest_run_id" in run_level_df.columns:
        run_rows = run_level_df.drop_duplicates("manifest_run_id")
        return int(run_rows["completed"].fillna(False).astype(bool).sum())
    return int(run_level_df["completed"].fillna(False).astype(bool).sum())


def _safe_str(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and np.isnan(value):
        return ""
    if pd.isna(value):
        return ""
    return str(value)


def _root_readme_text(output_root: Path) -> str:
    return "\n".join(
        [
            "# 30 Seed Pipeline",
            "",
            f"Output root: `{_portable_path(output_root)}`",
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
            "- `manifests/run_validation_manifest.csv` validation catalog",
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


def _load_group_specs(input_root: Path, output_root: Path, active_root: Path | None = None) -> list[GroupSpec]:
    """Load group specs for all 4 active experiment groups.

    active_root: explicit path to result/active/ — the source of truth for nets and CSVs.
    When provided, CSV/nets are loaded directly from result/active/csv/ and result/active/nets/.
    Falls back to legacy input_root paths when active_root files are absent.

    p1_p4_recovery_6: NODE_5830 excluded (confirmed 2026-05-16).
    project_root fix: input_root is <project>/result/<subdir>, so parent.parent = project root.
    """
    recovery_root = input_root

    # Resolve active root explicitly (avoid recovery_root.parent path-depth bug)
    if active_root is not None:
        ar = active_root.resolve()
    else:
        # input_root is typically <project>/result/<something>
        # parent = <project>/result  ;  parent.parent = <project>
        ar = recovery_root.parent.parent / "result" / "active"

    active_nets = ar / "nets"
    active_csv  = ar / "csv"
    project_result = ar.parent  # <project>/result

    def _active_net(name: str, legacy: Path) -> Path:
        candidate = active_nets / name
        return candidate.resolve() if candidate.exists() else legacy.resolve()

    def _active_csv_file(name: str, fallback_dir: Path, fallback_name: str) -> Path:
        candidate = active_csv / name
        return candidate.resolve() if candidate.exists() else (fallback_dir / fallback_name).resolve()

    # --- current_main_12 ---
    main_net = _active_net(
        "current_main_12.net.xml",
        project_result / "phase_next_recovery_command_plan_20260514_200908" / "batch_03_run" / "recovery_tls_batch_network_v1.net.xml",
    )
    main_src_csv = _active_csv_file(
        "current_main_12_candidates.csv",
        recovery_root,
        "main_verified_current_net_12.csv",
    )
    main_df = _read_csv(main_src_csv)

    # --- signal_fix_9 ---
    signal_net = _active_net("signal_fix_9.net.xml", recovery_root / "signal_fix_net_v1.net.xml")
    signal_src_csv = _active_csv_file(
        "signal_fix_9_candidates.csv",
        recovery_root,
        "signal_fix_candidate_table.csv",
    )
    signal_df = _read_csv(signal_src_csv)

    # --- generated_signal_7 ---
    generated_net = _active_net("generated_signal_7.net.xml", recovery_root / "generated_signal_net_v1.net.xml")
    generated_src_csv = _active_csv_file(
        "generated_signal_7_candidates.csv",
        recovery_root,
        "generated_signal_candidate_table.csv",
    )
    generated_df = _read_csv(generated_src_csv)

    # --- p1_p4_recovery_6 (NODE_5830 hard-excluded) ---
    p1_p4_net = _active_net(
        "p1_p4_recovery_6.net.xml",
        project_result / "phase_next_p1_p4_combined_recovery_20260516_020409" / "p1_p4_recovery_net.net.xml",
    )
    p1_p4_src_csv = _active_csv_file(
        "p1_p4_recovery_6_candidates.csv",
        project_result / "phase_next_p1_p4_combined_recovery_20260516_020409" / "csv",
        "p1_p4_recovery_candidate_table.csv",
    )
    p1_p4_df_raw = _read_csv(p1_p4_src_csv)
    if not p1_p4_df_raw.empty and "crosswalk_id" in p1_p4_df_raw.columns:
        p1_p4_df = p1_p4_df_raw[p1_p4_df_raw["crosswalk_id"] != "NODE_5830"].copy().reset_index(drop=True)
    else:
        p1_p4_df = p1_p4_df_raw.copy()

    specs = [
        GroupSpec(
            run_group="current_main_12",
            net_group="current_main",
            baseline_net_file=main_net,
            full_candidate_csv=(output_root / "manifests" / "current_main_12_candidates.csv").resolve(),
            smart_candidates=main_df.copy(),
            baseline_placeholder_crosswalk_id="BASELINE_CURRENT_MAIN_12",
        ),
        GroupSpec(
            run_group="signal_fix_9",
            net_group="signal_fix",
            baseline_net_file=signal_net,
            full_candidate_csv=(output_root / "manifests" / "signal_fix_9_candidates.csv").resolve(),
            smart_candidates=signal_df.copy(),
            baseline_placeholder_crosswalk_id="BASELINE_SIGNAL_FIX_9",
        ),
        GroupSpec(
            run_group="generated_signal_7",
            net_group="generated_signal",
            baseline_net_file=generated_net,
            full_candidate_csv=(output_root / "manifests" / "generated_signal_7_candidates.csv").resolve(),
            smart_candidates=generated_df.copy(),
            baseline_placeholder_crosswalk_id="BASELINE_GENERATED_SIGNAL_7",
        ),
        GroupSpec(
            run_group="p1_p4_recovery_6",
            net_group="p1_p4_recovery",
            baseline_net_file=p1_p4_net,
            full_candidate_csv=(output_root / "manifests" / "p1_p4_recovery_6_candidates.csv").resolve(),
            smart_candidates=p1_p4_df.copy(),
            baseline_placeholder_crosswalk_id="BASELINE_P1_P4_RECOVERY_6",
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
            'SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"',
            'PIPELINE_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"',
            'PROJECT_ROOT="$(cd "$PIPELINE_ROOT/../../.." && pwd)"',
            'export PYTHONPATH="$PROJECT_ROOT:${PYTHONPATH:-}"',
            'if [[ -d "$PROJECT_ROOT/.venv/bin" ]]; then',
            '  export PATH="$PROJECT_ROOT/.venv/bin:$PATH"',
            'fi',
            'if [[ -n "${SUMO_HOME:-}" ]]; then',
            '  export PATH="$SUMO_HOME/bin:$PATH"',
            'elif ! command -v sumo >/dev/null 2>&1; then',
            '  echo "SUMO_HOME is not set and sumo is not on PATH" >&2',
            '  exit 1',
            'fi',
            "",
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
            'SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"',
            'PIPELINE_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"',
            "",
            'bash "$PIPELINE_ROOT/commands/command_to_run_30seed_current_main_12.sh"',
            'bash "$PIPELINE_ROOT/commands/command_to_run_30seed_signal_fix_9.sh"',
            'bash "$PIPELINE_ROOT/commands/command_to_run_30seed_generated_signal_7.sh"',
            'bash "$PIPELINE_ROOT/commands/command_to_run_30seed_p1_p4_recovery_6.sh"',
            "",
        ]
    )


def _build_sampled10_command_lines(
    spec: GroupSpec,
    output_root: Path,
    single_csv_root: Path,
    nets_dir: Path,
    *,
    sim_duration: int = SAMPLED10_SIM_DURATION,
) -> dict[str, str]:
    run_prefix = (
        "python3 -m smart_crosswalk_sumo.run_sampled10_group "
        "--candidate-csv \"$candidate_csv\" --net-file \"$NET_FILE\" "
        "--seed \"$seed\" --output-dir \"$out_dir\" "
        f"--sim-duration {sim_duration} --warmup {SAMPLED10_WARMUP} "
        f"--traci_step_length {SAMPLED10_STEP_LENGTH} --traffic_measure_radius_m 500.0 "
        f"--extension_increment {SAMPLED10_EXTENSION_SEC} --max_extensions 1 "
        f"--metric-sample-interval {SAMPLED10_METRIC_SAMPLE_INTERVAL} "
        f"--vehicle-sample-interval {SAMPLED10_VEHICLE_SAMPLE_INTERVAL} "
        f"--progress-interval {SAMPLED10_PROGRESS_INTERVAL} "
        "--phase-aligned-ped-depart --ped-repeat-count 5 --ped-repeat-spacing-sec 2 "
        "--include-vehicles "
        "--manifest-row-role \"$manifest_row_role\" "
        "--manifest-crosswalk-id \"$manifest_crosswalk_id\""
    )
    report_prefix = (
        "python3 -m smart_crosswalk_sumo.generate_reports "
        f"--figures_dir \"$FIGURES_DIR\""
    )
    return {
        "run_prefix": run_prefix,
        "report_prefix": report_prefix,
    }


def _build_sampled10_group_script(
    spec: GroupSpec,
    output_root: Path,
    single_csv_root: Path,
    nets_dir: Path,
    *,
    seed_start: int = 1,
    seed_end: int = 30,
    sim_duration: int = SAMPLED10_SIM_DURATION,
    smoke_root: bool = False,
) -> str:
    ctx = _build_sampled10_command_lines(spec, output_root, single_csv_root, nets_dir, sim_duration=sim_duration)
    ids = [str(v).strip() for v in spec.smart_candidates.get("crosswalk_id", pd.Series(dtype=str)).tolist()]
    id_array = " ".join(f'"{v}"' for v in ids)
    seed_label = "seed1" if seed_start == seed_end == 1 else f"seed{seed_start}-{seed_end}"
    if smoke_root:
        root_lines = [
            'RUN_CONTAINER_ROOT="${SMOKE_ROOT:-$ACTIVE_ROOT/smoke_30s_sampled10/seed01}"',
            'FIGURES_DIR="$RUN_CONTAINER_ROOT/figures"',
            f'RUN_ROOT="$RUN_CONTAINER_ROOT/runs/{spec.run_group}"',
            f'LOG_ROOT="$RUN_CONTAINER_ROOT/logs/{spec.run_group}"',
        ]
    else:
        root_lines = [
            'FIGURES_DIR="$PIPELINE_ROOT/figures"',
            f'RUN_ROOT="$PIPELINE_ROOT/runs/{spec.run_group}"',
            f'LOG_ROOT="$PIPELINE_ROOT/logs/{spec.run_group}"',
        ]
    return "\n".join(
        [
            "#!/usr/bin/env bash",
            "set -euo pipefail",
            "",
            'SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"',
            'PIPELINE_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"',
            'PROJECT_ROOT="$(cd "$PIPELINE_ROOT/../../.." && pwd)"',
            'RESULT_ROOT="$PROJECT_ROOT/result"',
            'ACTIVE_ROOT="$RESULT_ROOT/active"',
            'NETS_DIR="$ACTIVE_ROOT/nets"',
            *root_lines,
            f'SINGLE_CSV_ROOT="$PIPELINE_ROOT/manifests/single_candidates/{spec.run_group}"',
            f'BASELINE_CSV="$PIPELINE_ROOT/manifests/{spec.run_group}_candidates.csv"',
            f'NET_FILE="$NETS_DIR/{spec.run_group}.net.xml"',
            'export PYTHONPATH="$PROJECT_ROOT:${PYTHONPATH:-}"',
            "",
            'if [[ -z "${SUMO_HOME:-}" ]]; then',
            '  if command -v sumo >/dev/null 2>&1; then',
            '    :',
            '  else',
            '    echo "SUMO_HOME is not set and sumo is not on PATH" >&2',
            '    exit 1',
            '  fi',
            'else',
            '  export PATH="$SUMO_HOME/bin:$PATH"',
            '  if [[ -z "${PROJ_LIB:-}" && -d "$SUMO_HOME/framework/EclipseSUMO.framework/Resources/proj" ]]; then',
            '    export PROJ_LIB="$SUMO_HOME/framework/EclipseSUMO.framework/Resources/proj"',
            '  fi',
            'fi',
            'if [[ -d "$PROJECT_ROOT/.venv/bin" ]]; then',
            '  export PATH="$PROJECT_ROOT/.venv/bin:$PATH"',
            'fi',
            "",
            "mkdir -p \"$RUN_ROOT\" \"$LOG_ROOT\" \"$FIGURES_DIR\"",
            "if [[ ! -f \"$NET_FILE\" ]]; then",
            "  echo \"missing group net input: $NET_FILE\" >&2",
            "  exit 1",
            "fi",
            "",
            "verify_run_success() {",
            "  local benchmark_json=\"$1/benchmark_timing.json\"",
            "  python3 - \"$benchmark_json\" <<'PY'",
            "import json",
            "import sys",
            "from pathlib import Path",
            "path = Path(sys.argv[1])",
            "if not path.exists():",
            "    print(f\"missing benchmark_timing.json: {path}\", file=sys.stderr)",
            "    sys.exit(1)",
            "payload = json.loads(path.read_text(encoding=\"utf-8\"))",
            "def as_int(name):",
            "    try:",
            "        return int(payload.get(name, -1))",
            "    except Exception:",
            "        return -1",
            "ok = (",
            "    payload.get(\"run_success\") is True",
            "    and as_int(\"failed_cases_count\") == 0",
            "    and as_int(\"baseline_result_rows\") >= 1",
            "    and as_int(\"smart_result_rows\") >= 1",
            ")",
            "if not ok:",
            "    reason = payload.get(\"failure_reason\") or \"run_success=false\"",
            "    print(f\"sampled10 run failed: {reason}\", file=sys.stderr)",
            "    sys.exit(1)",
            "PY",
            "}",
            "",
            "verify_report_outputs() {",
            "  local out_dir=\"$1\"",
            "  python3 - \"$out_dir\" <<'PY'",
            "import sys",
            "import xml.etree.ElementTree as ET",
            "from pathlib import Path",
            "out = Path(sys.argv[1])",
            "required = [",
            "    out / \"simulation_summary.csv\",",
            "    out / \"baseline_smart_seed_results.csv\",",
            "    out / \"baseline_smart_summary.csv\",",
            "    out / \"local_tradeoff_summary.csv\",",
            "    out / \"tradeoff_summary.csv\",",
            "    out / \"demand_params.csv\",",
            "    out / \"csv\" / \"results\" / \"demand_params.csv\",",
            "    out / \"demand_vehicle.rou.xml\",",
            "    out / \"demand_vehicle.trips.xml\",",
            "    out / \"csv\" / \"report\" / \"preflight_tradeoff_summary.csv\",",
            "]",
            "missing = [str(path) for path in required if not path.exists()]",
            "peds = sorted(out.glob(\"peds_seed*.rou.xml\"))",
            "if not peds:",
            "    missing.append(str(out / \"peds_seed*.rou.xml\"))",
            "if missing:",
            "    print(\"missing report outputs: \" + \";\".join(missing), file=sys.stderr)",
            "    sys.exit(1)",
            "def count_xml(path, tag):",
            "    try:",
            "        return sum(1 for _ in ET.parse(path).getroot().iter(tag))",
            "    except Exception as exc:",
            "        print(f\"xml read failed: {path}: {exc}\", file=sys.stderr)",
            "        sys.exit(1)",
            "route_count = count_xml(out / \"demand_vehicle.rou.xml\", \"vehicle\")",
            "trip_count = count_xml(out / \"demand_vehicle.trips.xml\", \"trip\")",
            "if route_count != trip_count:",
            "    print(f\"vehicle route/trip count mismatch: route={route_count}, trip={trip_count}\", file=sys.stderr)",
            "    sys.exit(1)",
            "for ped in peds:",
            "    person_count = count_xml(ped, \"person\")",
            "    walk_count = count_xml(ped, \"walk\")",
            "    if person_count <= 0 or walk_count <= 0 or person_count != walk_count:",
            "        print(f\"ped count mismatch: {ped}: person={person_count}, walk={walk_count}\", file=sys.stderr)",
            "        sys.exit(1)",
            "PY",
            "}",
            "",
            "is_successful_run() {",
            "  local out_dir=\"$1\"",
            "  [[ -f \"$out_dir/simulation_summary.csv\" ]] || return 1",
            "  [[ -f \"$out_dir/baseline_smart_seed_results.csv\" ]] || return 1",
            "  [[ -f \"$out_dir/baseline_smart_summary.csv\" ]] || return 1",
            "  [[ -f \"$out_dir/local_tradeoff_summary.csv\" ]] || return 1",
            "  [[ -f \"$out_dir/tradeoff_summary.csv\" ]] || return 1",
            "  verify_run_success \"$out_dir\" >/dev/null 2>&1",
            "  verify_report_outputs \"$out_dir\" >/dev/null 2>&1",
            "}",
            "",
            "run_sampled() {",
            "  local candidate_csv=\"$1\"",
            "  local out_dir=\"$2\"",
            "  local log_file=\"$3\"",
            "  local seed=\"$4\"",
            "  local manifest_row_role=\"$5\"",
            "  local manifest_crosswalk_id=\"$6\"",
            "  mkdir -p \"$out_dir\" \"$(dirname \"$log_file\")\"",
            "  if is_successful_run \"$out_dir\"; then",
            "    echo \"skip seed$seed $out_dir\"",
            "    return 0",
            "  fi",
            "  if verify_run_success \"$out_dir\" >/dev/null 2>&1; then",
            "    echo \"repair report seed$seed $out_dir\"",
            "    python3 -m smart_crosswalk_sumo.repair_sampled10_demand_params --output-dir \"$out_dir\" >>\"$log_file\" 2>&1",
            f"    {ctx['report_prefix']} --output_dir \"$out_dir\" --candidates \"$candidate_csv\" --nets_dir \"$NETS_DIR\" >>\"$log_file\" 2>&1",
            "    verify_report_outputs \"$out_dir\"",
            "    return 0",
            "  fi",
            f"  {ctx['run_prefix']} >>\"$log_file\" 2>&1",
            "  verify_run_success \"$out_dir\"",
            f"  {ctx['report_prefix']} --output_dir \"$out_dir\" --candidates \"$candidate_csv\" --nets_dir \"$NETS_DIR\" >>\"$log_file\" 2>&1",
            "  verify_report_outputs \"$out_dir\"",
            "}",
            "",
            f"echo \"[{spec.run_group}] baseline {seed_label} (sampled10)\"",
            f"for seed in $(seq {seed_start} {seed_end}); do",
            '  out_dir="$RUN_ROOT/baseline/seed$(printf \'%02d\' "$seed")"',
            '  log_file="$LOG_ROOT/baseline/seed$(printf \'%02d\' "$seed").log"',
            f"  run_sampled \"$BASELINE_CSV\" \"$out_dir\" \"$log_file\" \"$seed\" \"baseline_placeholder\" \"{spec.baseline_placeholder_crosswalk_id}\"",
            "done",
            "",
            f"SMART_IDS=({id_array})",
            f"echo \"[{spec.run_group}] smart {seed_label} per candidate (sampled10)\"",
            "for i in \"${!SMART_IDS[@]}\"; do",
            "  crosswalk_id=\"${SMART_IDS[$i]}\"",
            '  candidate_csv="$SINGLE_CSV_ROOT/${crosswalk_id}.csv"',
            f"  for seed in $(seq {seed_start} {seed_end}); do",
            '    out_dir="$RUN_ROOT/smart/${crosswalk_id}/seed$(printf \'%02d\' "$seed")"',
            '    log_file="$LOG_ROOT/smart/${crosswalk_id}/seed$(printf \'%02d\' "$seed").log"',
            "    run_sampled \"$candidate_csv\" \"$out_dir\" \"$log_file\" \"$seed\" \"smart_candidate\" \"$crosswalk_id\"",
            "  done",
            "done",
            "",
        ]
    )


def _build_sampled10_all_groups_script(
    output_root: Path,
    *,
    seed1_only: bool = False,
    smoke30: bool = False,
) -> str:
    if smoke30:
        prefix = "command_to_run_smoke30_seed1"
    elif seed1_only:
        prefix = "command_to_run_seed1"
    else:
        prefix = "command_to_run_30seed"
    return "\n".join(
        [
            "#!/usr/bin/env bash",
            "set -euo pipefail",
            "",
            'SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"',
            'PIPELINE_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"',
            "",
            f'bash "$PIPELINE_ROOT/commands/{prefix}_current_main_12.sh"',
            f'bash "$PIPELINE_ROOT/commands/{prefix}_signal_fix_9.sh"',
            f'bash "$PIPELINE_ROOT/commands/{prefix}_generated_signal_7.sh"',
            f'bash "$PIPELINE_ROOT/commands/{prefix}_p1_p4_recovery_6.sh"',
            "",
        ]
    )


def _build_standardize_script(output_root: Path) -> str:
    return "\n".join(
        [
            "#!/usr/bin/env bash",
            "set -euo pipefail",
            "",
            'SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"',
            'PIPELINE_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"',
            'PROJECT_ROOT="$(cd "$PIPELINE_ROOT/../../.." && pwd)"',
            'export PYTHONPATH="$PROJECT_ROOT:${PYTHONPATH:-}"',
            'if [[ -z "${SUMO_HOME:-}" ]]; then',
            '  if command -v sumo >/dev/null 2>&1; then',
            '    :',
            '  else',
            '    echo "SUMO_HOME is not set and sumo is not on PATH" >&2',
            '    exit 1',
            '  fi',
            'else',
            '  export PATH="$SUMO_HOME/bin:$PATH"',
            '  if [[ -z "${PROJ_LIB:-}" && -d "$SUMO_HOME/framework/EclipseSUMO.framework/Resources/proj" ]]; then',
            '    export PROJ_LIB="$SUMO_HOME/framework/EclipseSUMO.framework/Resources/proj"',
            '  fi',
            'fi',
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
            'SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"',
            'PIPELINE_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"',
            'PROJECT_ROOT="$(cd "$PIPELINE_ROOT/../../.." && pwd)"',
            'export PYTHONPATH="$PROJECT_ROOT:${PYTHONPATH:-}"',
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
            'SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"',
            'PIPELINE_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"',
            "",
            "if command -v open >/dev/null 2>&1; then",
            "  OPEN_CMD=(open)",
            "elif command -v xdg-open >/dev/null 2>&1; then",
            "  OPEN_CMD=(xdg-open)",
            "elif command -v python3 >/dev/null 2>&1; then",
            "  OPEN_CMD=(python3 -m webbrowser)",
            "else",
            "  OPEN_CMD=()",
            "fi",
            "",
            "if [[ ${#OPEN_CMD[@]} -gt 0 ]]; then",
            "  \"${OPEN_CMD[@]}\" \"$PIPELINE_ROOT/README.md\" || true",
            "  \"${OPEN_CMD[@]}\" \"$PIPELINE_ROOT/csv/seed30_group_summary.csv\" || true",
            "else",
            "  echo \"$PIPELINE_ROOT/README.md\"",
            "  echo \"$PIPELINE_ROOT/csv/seed30_group_summary.csv\"",
            "fi",
            "",
        ]
    )


def _build_cleanup_scripts(output_root: Path, result_root: Path) -> tuple[str, str]:
    dry_run = "\n".join(
        [
            "#!/usr/bin/env bash",
            "set -euo pipefail",
            "",
            'SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"',
            'PIPELINE_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"',
            'PROJECT_ROOT="$(cd "$PIPELINE_ROOT/../../.." && pwd)"',
            'RESULT_ROOT="$PROJECT_ROOT/result"',
            'export PYTHONPATH="$PROJECT_ROOT:${PYTHONPATH:-}"',
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
            'SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"',
            'PIPELINE_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"',
            'PROJECT_ROOT="$(cd "$PIPELINE_ROOT/../../.." && pwd)"',
            'RESULT_ROOT="$PROJECT_ROOT/result"',
            'export PYTHONPATH="$PROJECT_ROOT:${PYTHONPATH:-}"',
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
                "baseline_candidate_csv": _portable_path(spec.full_candidate_csv),
                "baseline_net_file": _portable_path(spec.baseline_net_file),
                "output_root": _portable_path(output_root),
            }
        )
    return pd.DataFrame(rows)


def _build_candidate_metadata(
    specs: list[GroupSpec],
    source_csv: Path,
    expected_smart_ids: set[str] | None = None,
) -> pd.DataFrame:
    source_df = _read_csv(source_csv)
    columns = [
        "crosswalk_id",
        "dong_name",
        "admin_dong",
        "net_group",
        "run_group",
        "net_file",
        "source_csv",
        "source_rank",
        "risk_rank",
        "risk_score",
        "phase6_status_label",
        "is_smart_target",
    ]
    if source_df.empty:
        return pd.DataFrame(columns=columns)
    if "crosswalk_id" not in source_df.columns:
        raise ValueError(f"Missing crosswalk_id in candidate metadata source: {source_csv}")

    source_df = source_df.copy().reset_index(drop=False).rename(columns={"index": "source_rank"})
    source_df["crosswalk_id"] = source_df["crosswalk_id"].astype(str).str.strip()
    source_df["source_rank"] = pd.to_numeric(source_df["source_rank"], errors="coerce").fillna(-1).astype(int) + 1
    if "risk_score" in source_df.columns:
        source_df["risk_score"] = pd.to_numeric(source_df["risk_score"], errors="coerce")
        risk_rank_df = source_df.sort_values(
            ["risk_score", "crosswalk_id"],
            ascending=[False, True],
            na_position="last",
        ).reset_index(drop=True)
        risk_rank_map = {str(row["crosswalk_id"]): idx + 1 for idx, row in risk_rank_df.iterrows()}
    else:
        source_df["risk_score"] = np.nan
        risk_rank_map = {}

    source_lookup: dict[str, dict[str, Any]] = {}
    for _, row in source_df.iterrows():
        key = str(row.get("crosswalk_id", "")).strip()
        if not key:
            continue
        source_lookup[key] = row.to_dict()

    rows: list[dict[str, Any]] = []
    smart_ids: set[str] = set()
    for spec in specs:
        if spec.smart_candidates.empty:
            continue
        for _, row in spec.smart_candidates.iterrows():
            crosswalk_id = str(row.get("crosswalk_id", "")).strip()
            if not crosswalk_id:
                continue
            smart_ids.add(crosswalk_id)
            source_row = source_lookup.get(crosswalk_id)
            if source_row is None:
                raise ValueError(f"Missing candidate metadata source row for crosswalk_id={crosswalk_id}")
            status_value = source_row.get("phase6_status_label", "")
            if pd.isna(status_value) or str(status_value).strip() == "":
                status_value = source_row.get("final_tier", "")
            if pd.isna(status_value) or str(status_value).strip() == "":
                status_value = row.get("phase6_status_label", row.get("final_tier", ""))
            risk_score = source_row.get("risk_score", row.get("risk_score", np.nan))
            rows.append(
                {
                    "crosswalk_id": crosswalk_id,
                    "dong_name": source_row.get("dong_name", row.get("dong_name", "")),
                    "admin_dong": source_row.get("admin_dong", row.get("admin_dong", "")),
                    "net_group": spec.net_group,
                    "run_group": spec.run_group,
                    "net_file": _portable_path(spec.baseline_net_file),
                    "source_csv": _portable_path(source_csv),
                    "source_rank": int(source_row.get("source_rank", np.nan)) if pd.notna(source_row.get("source_rank", np.nan)) else np.nan,
                    "risk_rank": int(risk_rank_map.get(crosswalk_id, np.nan)) if crosswalk_id in risk_rank_map else np.nan,
                    "risk_score": _safe_float(risk_score),
                    "phase6_status_label": str(status_value).strip(),
                    "is_smart_target": True,
                }
            )

    metadata_df = pd.DataFrame(rows, columns=columns)
    if not metadata_df.empty:
        metadata_df = metadata_df.sort_values(["run_group", "risk_rank", "crosswalk_id"], ascending=[True, True, True]).reset_index(drop=True)
    if expected_smart_ids is not None:
        metadata_ids = set(metadata_df["crosswalk_id"].astype(str).tolist()) if not metadata_df.empty else set()
        missing = sorted(expected_smart_ids - metadata_ids)
        extra = sorted(metadata_ids - expected_smart_ids)
        if missing or extra:
            raise ValueError(
                "candidate_metadata crosswalk_id mismatch: "
                f"missing={missing[:10]}, extra={extra[:10]}"
            )
    if "dong_name" in metadata_df.columns and metadata_df["dong_name"].isna().any():
        raise ValueError("candidate_metadata is missing dong_name for at least one row")
    if "admin_dong" in metadata_df.columns and metadata_df["admin_dong"].isna().any():
        raise ValueError("candidate_metadata is missing admin_dong for at least one row")
    return metadata_df


def _load_optional_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _find_smoke_candidate_validation_json(output_dir: Path) -> Path | None:
    candidates = sorted(output_dir.glob("phase6_smoke_*_candidate_validation.json"))
    return candidates[0] if candidates else None


def _load_pedestrian_depart_plan(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame()
    return _read_csv(path)


def _load_pedestrian_route_diagnostics(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame()
    return _read_csv(path)


def _load_pedestrian_departures_from_xml(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame(columns=["crosswalk_id", "person_id", "depart"])
    try:
        root = ET.parse(path).getroot()
    except Exception:
        return pd.DataFrame(columns=["crosswalk_id", "person_id", "depart"])
    rows: list[dict[str, Any]] = []
    for person in root.findall("person"):
        person_id = _safe_str(person.get("id"))
        depart = person.get("depart")
        crosswalk_id = ""
        parts = person_id.split("_")
        if len(parts) >= 4:
            crosswalk_id = "_".join(parts[3:])
        rows.append(
            {
                "crosswalk_id": crosswalk_id,
                "person_id": person_id,
                "depart": _safe_float(depart),
            }
        )
    return pd.DataFrame(rows)


def _load_pedestrian_demand_sources() -> tuple[pd.DataFrame, list[Path]]:
    source_paths = [
        Path(__file__).resolve().parents[2] / "result" / "active" / "pedestrian_assumption" / "crosswalk_pedestrian_assumptions_top50_daytime_high_1p2.csv",
        Path(__file__).resolve().parents[2] / "result" / "active" / "pedestrian_assumption" / "crosswalk_pedestrian_assumptions_daytime_high_1p2.csv",
    ]
    frames: list[pd.DataFrame] = []
    used_paths: list[Path] = []
    for path in source_paths:
        frame = _read_csv(path)
        if frame.empty:
            continue
        if "admin_dong" not in frame.columns:
            continue
        cols = [c for c in ["admin_dong", "dong_name", "crosswalk_id", "base_pedestrian_600s", "final_pedestrian_600s", "ped_source", "scenario_name"] if c in frame.columns]
        frames.append(frame[cols].copy())
        used_paths.append(path)
    if not frames:
        return pd.DataFrame(), []
    combined = pd.concat(frames, ignore_index=True)
    if "final_pedestrian_600s" not in combined.columns:
        combined["final_pedestrian_600s"] = pd.NA
    if "base_pedestrian_600s" not in combined.columns:
        combined["base_pedestrian_600s"] = pd.NA
    combined["admin_dong"] = combined["admin_dong"].astype(str).str.strip()
    combined["crosswalk_id"] = combined.get("crosswalk_id", pd.Series(dtype=str)).astype(str).str.strip()
    combined["source_file"] = used_paths[0].as_posix() if len(used_paths) == 1 else ";".join(path.as_posix() for path in used_paths)
    return combined, used_paths


def _resolve_demand_lookup(source_df: pd.DataFrame) -> dict[str, dict[str, Any]]:
    lookup: dict[str, dict[str, Any]] = {}
    if source_df.empty or "admin_dong" not in source_df.columns:
        return lookup
    ordered = source_df.copy()
    if "final_pedestrian_600s" in ordered.columns:
        ordered["final_pedestrian_600s"] = pd.to_numeric(ordered["final_pedestrian_600s"], errors="coerce")
        ordered = ordered.sort_values(["admin_dong", "final_pedestrian_600s", "crosswalk_id"], ascending=[True, False, True], na_position="last")
    else:
        ordered = ordered.sort_values(["admin_dong", "crosswalk_id"], ascending=[True, True])
    for _, row in ordered.iterrows():
        admin = _safe_str(row.get("admin_dong"))
        if not admin:
            continue
        if admin in lookup:
            continue
        lookup[admin] = {
            "admin_dong": admin,
            "dong_name": _safe_str(row.get("dong_name")),
            "demand_10min": _safe_float(row.get("final_pedestrian_600s")),
            "base_pedestrian_600s": _safe_float(row.get("base_pedestrian_600s")),
            "source_file": _safe_str(row.get("source_file")),
            "ped_source": _safe_str(row.get("ped_source")),
        }
    return lookup


def _load_smoke_summary_row(output_dir: Path) -> pd.DataFrame:
    summary_path = output_dir / "phase6_smoke_summary.csv"
    if not summary_path.exists():
        return pd.DataFrame()
    return _read_csv(summary_path)


def _count_xml_persons(route_path: Path) -> int | None:
    if not route_path.exists():
        return None
    try:
        root = ET.parse(route_path).getroot()
    except Exception:
        return None
    return len(root.findall("person"))


def _sum_runtime_observed_ped_count(summary_df: pd.DataFrame) -> float | None:
    if summary_df.empty or "ped_crossing_person_count" not in summary_df.columns:
        return None
    values = pd.to_numeric(summary_df["ped_crossing_person_count"], errors="coerce").dropna()
    if values.empty:
        return None
    return float(values.sum())


def _validation_reason(parts: list[str]) -> str:
    clean = [p for p in parts if p]
    return "; ".join(clean) if clean else "ok"


def _build_pedestrian_smoke_validation_rows(
    manifest_row: pd.Series,
    candidate_metadata_df: pd.DataFrame,
) -> list[dict[str, Any]]:
    run_id = _safe_str(manifest_row.get("run_id"))
    scenario = _safe_str(manifest_row.get("scenario"))
    seed = manifest_row.get("seed")
    run_group = _safe_str(manifest_row.get("run_group"))
    net_group = _safe_str(manifest_row.get("net_group"))
    output_dir = Path(_safe_str(manifest_row.get("output_dir"))).expanduser()
    output_exists = output_dir.is_dir()
    summary_path = output_dir / "phase6_smoke_summary.csv"
    demand_path = output_dir / "demand_pedestrian.rou.xml"
    depart_plan_path = output_dir / "pedestrian_depart_plan.csv"
    diagnostics_path = output_dir / "pedestrian_route_diagnostics.csv"
    validation_json_path = _find_smoke_candidate_validation_json(output_dir) if output_exists else None
    validation_json = _load_optional_json(validation_json_path) if validation_json_path else {}
    candidate_lookup = (
        candidate_metadata_df.set_index("crosswalk_id", drop=False)
        if not candidate_metadata_df.empty and "crosswalk_id" in candidate_metadata_df.columns
        else pd.DataFrame()
    )

    if not output_exists or not summary_path.exists():
        return [
            {
                "run_id": run_id,
                "scenario": scenario,
                "seed": seed,
                "net_group": net_group,
                "run_group": run_group,
                "crosswalk_id": _safe_str(manifest_row.get("crosswalk_id")),
                "dong_name": "",
                "admin_dong": "",
                "output_dir": str(output_dir),
                "expected_ped_count": np.nan,
                "observed_ped_count": np.nan,
                "ped_count_pass": False,
                "spacing_pass": False,
                "phase_aligned_pass": False,
                "pedestrian_crossing_pass": False,
                "extension_pass": False,
                "overall_pass": False,
                "reason": _validation_reason(
                    [
                        "output_dir_missing" if not output_exists else "",
                        "phase6_smoke_summary_missing" if not summary_path.exists() else "",
                    ]
                ),
                "output_dir_exists": output_exists,
                "phase6_smoke_summary_exists": summary_path.exists(),
                "demand_pedestrian_exists": demand_path.exists(),
                "pedestrian_depart_plan_exists": depart_plan_path.exists(),
                "pedestrian_route_diagnostics_exists": diagnostics_path.exists(),
                "candidate_validation_json_exists": bool(validation_json_path),
                "source_crosswalk_id": _safe_str(manifest_row.get("crosswalk_id")),
                "expected_ped_repeat_count": np.nan,
                "observed_depart_groups": np.nan,
                "expected_extension_count": np.nan,
                "observed_extension_count": np.nan,
            }
        ]

    summary_df = _read_csv(summary_path)
    if summary_df.empty:
        return [
            {
                "run_id": run_id,
                "scenario": scenario,
                "seed": seed,
                "net_group": net_group,
                "run_group": run_group,
                "crosswalk_id": _safe_str(manifest_row.get("crosswalk_id")),
                "dong_name": "",
                "admin_dong": "",
                "output_dir": str(output_dir),
                "expected_ped_count": np.nan,
                "observed_ped_count": np.nan,
                "ped_count_pass": False,
                "spacing_pass": False,
                "phase_aligned_pass": False,
                "pedestrian_crossing_pass": False,
                "extension_pass": False,
                "overall_pass": False,
                "reason": "phase6_smoke_summary_empty",
                "output_dir_exists": output_exists,
                "phase6_smoke_summary_exists": summary_path.exists(),
                "demand_pedestrian_exists": demand_path.exists(),
                "pedestrian_depart_plan_exists": depart_plan_path.exists(),
                "pedestrian_route_diagnostics_exists": diagnostics_path.exists(),
                "candidate_validation_json_exists": bool(validation_json_path),
                "source_crosswalk_id": _safe_str(manifest_row.get("crosswalk_id")),
                "expected_ped_repeat_count": np.nan,
                "observed_depart_groups": np.nan,
                "expected_extension_count": np.nan,
                "observed_extension_count": np.nan,
            }
        ]

    plan_df = _load_pedestrian_depart_plan(depart_plan_path)
    diag_df = _load_pedestrian_route_diagnostics(diagnostics_path)
    xml_df = _load_pedestrian_departures_from_xml(demand_path)

    plan_lookup = plan_df.set_index("crosswalk_id", drop=False) if not plan_df.empty and "crosswalk_id" in plan_df.columns else pd.DataFrame()
    diag_lookup = diag_df.groupby("crosswalk_id") if not diag_df.empty and "crosswalk_id" in diag_df.columns else None
    xml_lookup = xml_df.groupby("crosswalk_id") if not xml_df.empty and "crosswalk_id" in xml_df.columns else None

    rows: list[dict[str, Any]] = []
    for _, summary_row in summary_df.iterrows():
        crosswalk_id = _safe_str(summary_row.get("crosswalk_id"))
        meta = candidate_lookup.loc[crosswalk_id] if not candidate_metadata_df.empty and crosswalk_id in candidate_lookup.index else None
        expected_ped_repeat_count = _safe_float(summary_row.get("expected_ped_repeat_count"))
        observed_ped_count = _safe_float(summary_row.get("ped_crossing_person_count"))
        ped_repeat_match = summary_row.get("ped_repeat_count_match")
        expected_extension_count = 0 if scenario == "baseline" else 1
        observed_extension_count = _safe_float(summary_row.get("extension_count"))
        expected_ped_count = expected_ped_repeat_count

        if diag_lookup is not None and crosswalk_id in diag_lookup.groups:
            diag_crosswalk = diag_lookup.get_group(crosswalk_id)
        else:
            diag_crosswalk = pd.DataFrame()
        if xml_lookup is not None and crosswalk_id in xml_lookup.groups:
            xml_crosswalk = xml_lookup.get_group(crosswalk_id)
        else:
            xml_crosswalk = pd.DataFrame()

        depart_times: list[float] = []
        if not diag_crosswalk.empty and "depart" in diag_crosswalk.columns:
            depart_times = sorted({float(v) for v in pd.to_numeric(diag_crosswalk["depart"], errors="coerce").dropna().tolist()})
        elif not xml_crosswalk.empty and "depart" in xml_crosswalk.columns:
            depart_times = sorted({float(v) for v in pd.to_numeric(xml_crosswalk["depart"], errors="coerce").dropna().tolist()})

        plan_row = plan_lookup.loc[crosswalk_id] if not plan_df.empty and crosswalk_id in plan_lookup.index else None
        plan_spacing = _safe_float(plan_row.get("ped_repeat_spacing_sec")) if plan_row is not None else None
        plan_depart_strategy = _safe_str(plan_row.get("depart_strategy")) if plan_row is not None else ""
        plan_depart_time = _safe_float(plan_row.get("depart_time")) if plan_row is not None else None
        plan_first_green = _safe_float(plan_row.get("first_green_start_time")) if plan_row is not None else None

        spacing_pass = False
        if plan_row is not None and len(depart_times) >= 2 and plan_spacing is not None:
            diffs = [round(depart_times[i + 1] - depart_times[i], 6) for i in range(len(depart_times) - 1)]
            spacing_pass = all(abs(diff - plan_spacing) <= 1e-6 for diff in diffs)

        phase_aligned_pass = False
        if plan_row is not None:
            strategy_ok = "phase_aligned" in plan_depart_strategy
            timing_ok = plan_depart_time is not None and plan_first_green is not None and abs((plan_first_green - plan_depart_time) - 1.5) <= 1e-6
            phase_aligned_pass = bool(strategy_ok and timing_ok)

        pedestrian_crossing_pass = bool((observed_ped_count or 0) > 0)
        if validation_json_path and isinstance(validation_json, dict) and crosswalk_id in validation_json:
            json_entry = validation_json.get(crosswalk_id, {})
            pedestrian_crossing_pass = bool(pedestrian_crossing_pass and _safe_bool(json_entry.get("contains_crossing_edge", True)))

        ped_count_pass = bool(ped_repeat_match) if ped_repeat_match is not None and not pd.isna(ped_repeat_match) else bool((observed_ped_count or 0) == expected_ped_count)
        extension_pass = bool((_safe_float(observed_extension_count) or 0) == expected_extension_count)
        overall_pass = bool(
            ped_count_pass
            and spacing_pass
            and phase_aligned_pass
            and pedestrian_crossing_pass
            and extension_pass
            and output_exists
            and summary_path.exists()
            and demand_path.exists()
            and depart_plan_path.exists()
        )

        reason_parts = [
            "" if output_exists else "output_dir_missing",
            "" if summary_path.exists() else "phase6_smoke_summary_missing",
            "" if demand_path.exists() else "demand_pedestrian_missing",
            "" if depart_plan_path.exists() else "pedestrian_depart_plan_missing",
            "" if ped_count_pass else f"ped_count_mismatch(expected={expected_ped_count},observed={observed_ped_count})",
            "" if spacing_pass else "spacing_mismatch",
            "" if phase_aligned_pass else "phase_alignment_mismatch",
            "" if pedestrian_crossing_pass else "no_pedestrian_crossing_detected",
            "" if extension_pass else f"extension_mismatch(expected={expected_extension_count},observed={observed_extension_count})",
        ]

        rows.append(
            {
                "run_id": run_id,
                "scenario": scenario,
                "seed": seed,
                "net_group": net_group,
                "run_group": run_group,
                "crosswalk_id": crosswalk_id,
                "dong_name": _safe_str(meta["dong_name"]) if meta is not None and "dong_name" in meta else "",
                "admin_dong": _safe_str(meta["admin_dong"]) if meta is not None and "admin_dong" in meta else "",
                "output_dir": str(output_dir),
                "expected_ped_count": expected_ped_count,
                "observed_ped_count": observed_ped_count,
                "ped_count_pass": ped_count_pass,
                "spacing_pass": spacing_pass,
                "phase_aligned_pass": phase_aligned_pass,
                "pedestrian_crossing_pass": pedestrian_crossing_pass,
                "extension_pass": extension_pass,
                "overall_pass": overall_pass,
                "reason": _validation_reason(reason_parts),
                "output_dir_exists": output_exists,
                "phase6_smoke_summary_exists": summary_path.exists(),
                "demand_pedestrian_exists": demand_path.exists(),
                "pedestrian_depart_plan_exists": depart_plan_path.exists(),
                "pedestrian_route_diagnostics_exists": diagnostics_path.exists(),
                "candidate_validation_json_exists": bool(validation_json_path),
                "source_crosswalk_id": _safe_str(summary_row.get("crosswalk_id")),
                "expected_ped_repeat_count": expected_ped_repeat_count,
                "observed_depart_groups": len(depart_times),
                "expected_extension_count": expected_extension_count,
                "observed_extension_count": observed_extension_count,
                "ped_repeat_spacing_sec_expected": plan_spacing,
                "depart_strategy": plan_depart_strategy,
                "depart_time": plan_depart_time,
                "first_green_start_time": plan_first_green,
            }
        )
    return rows


def _build_pedestrian_smoke_validation(
    run_manifest_df: pd.DataFrame,
    candidate_metadata_df: pd.DataFrame,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    rows: list[dict[str, Any]] = []
    for _, manifest_row in run_manifest_df.iterrows():
        rows.extend(_build_pedestrian_smoke_validation_rows(manifest_row, candidate_metadata_df))
    validation_df = pd.DataFrame(rows)
    if validation_df.empty:
        summary_df = pd.DataFrame(
            [
                {
                    "run_group": "",
                    "net_group": "",
                    "scenario": "",
                    "total_rows": 0,
                    "overall_pass_rows": 0,
                    "overall_fail_rows": 0,
                    "ped_count_pass_rows": 0,
                    "spacing_pass_rows": 0,
                    "phase_aligned_pass_rows": 0,
                    "pedestrian_crossing_pass_rows": 0,
                    "extension_pass_rows": 0,
                    "output_dir_missing_rows": 0,
                    "summary_missing_rows": 0,
                    "demand_missing_rows": 0,
                    "depart_plan_missing_rows": 0,
                }
            ]
        )
        return validation_df, summary_df

    summary_rows: list[dict[str, Any]] = []
    for keys, sub in validation_df.groupby(["run_group", "net_group", "scenario"], dropna=False):
        run_group, net_group, scenario = keys
        summary_rows.append(
            {
                "run_group": run_group,
                "net_group": net_group,
                "scenario": scenario,
                "total_rows": int(len(sub)),
                "overall_pass_rows": int(sub["overall_pass"].fillna(False).astype(bool).sum()) if "overall_pass" in sub.columns else 0,
                "overall_fail_rows": int((~sub["overall_pass"].fillna(False).astype(bool)).sum()) if "overall_pass" in sub.columns else 0,
                "ped_count_pass_rows": int(sub["ped_count_pass"].fillna(False).astype(bool).sum()) if "ped_count_pass" in sub.columns else 0,
                "spacing_pass_rows": int(sub["spacing_pass"].fillna(False).astype(bool).sum()) if "spacing_pass" in sub.columns else 0,
                "phase_aligned_pass_rows": int(sub["phase_aligned_pass"].fillna(False).astype(bool).sum()) if "phase_aligned_pass" in sub.columns else 0,
                "pedestrian_crossing_pass_rows": int(sub["pedestrian_crossing_pass"].fillna(False).astype(bool).sum()) if "pedestrian_crossing_pass" in sub.columns else 0,
                "extension_pass_rows": int(sub["extension_pass"].fillna(False).astype(bool).sum()) if "extension_pass" in sub.columns else 0,
                "output_dir_missing_rows": int((~sub["output_dir_exists"].fillna(False).astype(bool)).sum()) if "output_dir_exists" in sub.columns else 0,
                "summary_missing_rows": int((~sub["phase6_smoke_summary_exists"].fillna(False).astype(bool)).sum()) if "phase6_smoke_summary_exists" in sub.columns else 0,
                "demand_missing_rows": int((~sub["demand_pedestrian_exists"].fillna(False).astype(bool)).sum()) if "demand_pedestrian_exists" in sub.columns else 0,
                "depart_plan_missing_rows": int((~sub["pedestrian_depart_plan_exists"].fillna(False).astype(bool)).sum()) if "pedestrian_depart_plan_exists" in sub.columns else 0,
            }
        )
    summary_df = pd.DataFrame(summary_rows).sort_values(["run_group", "net_group", "scenario"]).reset_index(drop=True)
    return validation_df, summary_df


def _build_run_manifest(specs: list[GroupSpec], output_root: Path) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    baseline_rows: list[dict[str, Any]] = []
    smart_rows: list[dict[str, Any]] = []
    single_csv_root = output_root / "manifests" / "single_candidates"
    for spec in specs:
        _ensure_dir(single_csv_root / spec.run_group)
        full_csv = _portable_path(spec.full_candidate_csv)
        net_file = _portable_path(spec.baseline_net_file)
        candidate_df = spec.smart_candidates.copy()
        if candidate_df.empty:
            continue
        candidate_df = _portableize_path_columns(candidate_df.reset_index(drop=True))
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
	                        "manifest_row_role": "smart_candidate",
	                        "execution_unit": "paired_group_net",
	                        "network_input_mode": "group_net_file",
	                        "seed": seed,
	                        "crosswalk_id": crosswalk_id,
                        "candidate_csv": _portable_path(single_csv_root / spec.run_group / f"{crosswalk_id}.csv"),
                        "net_file": net_file,
                        "output_dir": _portable_path(smart_out),
                        "log_file": _portable_path(smart_log),
                        "expected_summary_csv": _portable_path(smart_out / "phase6_smoke_summary.csv"),
                        "validation_manifest": _portable_path(output_root / "manifests" / "run_validation_manifest.csv"),
                        "skip_if_exists": True,
                        "command": (
                            "python3 -m smart_crosswalk_sumo.run_phase6_recovery_smoke "
                            f"--candidate-csv \"{_portable_path(single_csv_root / spec.run_group / f'{crosswalk_id}.csv')}\" "
                            f"--net-file \"{net_file}\" --scenario smart --seed {seed} "
                            "--sim-duration 600 --warmup 0 --step-length 0.5 --extension-sec 5.0 "
                            "--phase-aligned-ped-depart --ped-repeat-count 5 --ped-repeat-spacing-sec 2 "
                            f"--output-dir \"{_portable_path(smart_out)}\" >\"{_portable_path(smart_log)}\" 2>&1"
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
                    "output_dir": _portable_path(base_out),
                    "log_file": _portable_path(base_log),
                    "expected_summary_csv": _portable_path(base_out / "phase6_smoke_summary.csv"),
                    "validation_manifest": _portable_path(output_root / "manifests" / "run_validation_manifest.csv"),
                    "skip_if_exists": True,
                    "command": (
                        "python3 -m smart_crosswalk_sumo.run_phase6_recovery_smoke "
                        f"--candidate-csv \"{full_csv}\" --net-file \"{net_file}\" --scenario baseline --seed {seed} "
                        "--sim-duration 600 --warmup 0 --step-length 0.5 --extension-sec 0 "
                        "--phase-aligned-ped-depart --ped-repeat-count 5 --ped-repeat-spacing-sec 2 "
                        f"--output-dir \"{_portable_path(base_out)}\" >\"{_portable_path(base_log)}\" 2>&1"
                    ),
                }
            )
    baseline_df = pd.DataFrame(baseline_rows)
    smart_df = pd.DataFrame(smart_rows)
    run_manifest_df = pd.concat([baseline_df, smart_df], ignore_index=True)
    return run_manifest_df, baseline_df, smart_df


def _build_run_manifest_sampled10(specs: list[GroupSpec], output_root: Path, nets_dir: Path) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    baseline_rows: list[dict[str, Any]] = []
    smart_rows: list[dict[str, Any]] = []
    single_csv_root = output_root / "manifests" / "single_candidates"
    for spec in specs:
        _ensure_dir(single_csv_root / spec.run_group)
        full_csv = _portable_path(spec.full_candidate_csv)
        net_file = _portable_path(spec.baseline_net_file)
        candidate_df = spec.smart_candidates.copy()
        if candidate_df.empty:
            continue
        candidate_df = _portableize_path_columns(candidate_df.reset_index(drop=True))
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
                        "manifest_row_role": "smart_candidate",
                        "execution_unit": "paired_group_net",
                        "network_input_mode": "group_net_file",
                        "seed": seed,
                        "crosswalk_id": crosswalk_id,
                        "candidate_csv": _portable_path(single_csv_root / spec.run_group / f"{crosswalk_id}.csv"),
                        "net_file": net_file,
                        "output_dir": _portable_path(smart_out),
                        "log_file": _portable_path(smart_log),
                        "expected_summary_csv": _portable_path(smart_out / "simulation_result.csv"),
                        "validation_manifest": _portable_path(output_root / "manifests" / "run_validation_manifest.csv"),
                        "skip_if_exists": True,
                        "command": (
                            "python3 -m smart_crosswalk_sumo.run_sampled10_group "
                            "--candidate-csv \"$candidate_csv\" --net-file \"$NET_FILE\" "
                            "--seed \"$seed\" --output-dir \"$out_dir\" "
                            "--sim-duration 600 --warmup 0 "
                            "--traci_step_length 0.1 --traffic_measure_radius_m 500.0 "
                            "--extension_increment 5.0 --max_extensions 1 "
                            "--metric-sample-interval 10 "
                            "--vehicle-sample-interval 10 "
                            "--progress-interval 60 "
                            "--phase-aligned-ped-depart --ped-repeat-count 5 --ped-repeat-spacing-sec 2 "
                            "--include-vehicles "
                            "--manifest-row-role \"smart_candidate\" "
                            "--manifest-crosswalk-id \"$crosswalk_id\" "
                            "&& python3 -m smart_crosswalk_sumo.generate_reports "
                            "--output_dir \"$out_dir\" --figures_dir \"$FIGURES_DIR\" "
                            "--candidates \"$candidate_csv\" --nets_dir \"$NETS_DIR\""
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
                    "manifest_row_role": "baseline_placeholder",
                    "execution_unit": "paired_group_net",
                    "network_input_mode": "group_net_file",
                    "seed": seed,
                    "crosswalk_id": spec.baseline_placeholder_crosswalk_id,
                    "candidate_csv": full_csv,
                    "net_file": net_file,
                    "output_dir": _portable_path(base_out),
                    "log_file": _portable_path(base_log),
                    "expected_summary_csv": _portable_path(base_out / "simulation_result.csv"),
                    "validation_manifest": _portable_path(output_root / "manifests" / "run_validation_manifest.csv"),
                    "skip_if_exists": True,
                    "command": (
                        "python3 -m smart_crosswalk_sumo.run_sampled10_group "
                        "--candidate-csv \"$BASELINE_CSV\" --net-file \"$NET_FILE\" "
                        "--seed \"$seed\" --output-dir \"$out_dir\" "
                        "--sim-duration 600 --warmup 0 "
                        "--traci_step_length 0.1 --traffic_measure_radius_m 500.0 "
                        "--extension_increment 5.0 --max_extensions 1 "
                        "--metric-sample-interval 10 "
                        "--vehicle-sample-interval 10 "
                        "--progress-interval 60 "
                        "--phase-aligned-ped-depart --ped-repeat-count 5 --ped-repeat-spacing-sec 2 "
                        "--include-vehicles "
                        "--manifest-row-role \"baseline_placeholder\" "
                        f"--manifest-crosswalk-id \"{spec.baseline_placeholder_crosswalk_id}\" "
                        "&& python3 -m smart_crosswalk_sumo.generate_reports "
                        "--output_dir \"$out_dir\" --figures_dir \"$FIGURES_DIR\" "
                        "--candidates \"$BASELINE_CSV\" --nets_dir \"$NETS_DIR\""
                    ),
                }
            )
    baseline_df = pd.DataFrame(baseline_rows)
    smart_df = pd.DataFrame(smart_rows)
    run_manifest_df = pd.concat([baseline_df, smart_df], ignore_index=True)
    return run_manifest_df, baseline_df, smart_df


def _build_validation_manifest(
    output_root: Path,
    run_manifest_df: pd.DataFrame,
    run_level_summary: pd.DataFrame,
    comparison_df: pd.DataFrame,
    metric_df: pd.DataFrame,
    group_summary_df: pd.DataFrame,
    completion_summary_df: pd.DataFrame,
    inventory_df: pd.DataFrame,
    audit_summary_df: pd.DataFrame,
    presence_df: pd.DataFrame,
    safety_summary_df: pd.DataFrame | None,
    safety_missing_df: pd.DataFrame,
    cleanup_audit_df: pd.DataFrame,
    safe_df: pd.DataFrame,
    review_df: pd.DataFrame,
    protected_df: pd.DataFrame,
) -> pd.DataFrame:
    def _status(df: pd.DataFrame, required: set[str] | None = None) -> str:
        if df is None or df.empty:
            return "empty"
        if required and not required.issubset(set(df.columns)):
            return "partial"
        return "ready"

    rows: list[dict[str, Any]] = []
    manifest_path = output_root / "manifests" / "run_validation_manifest.csv"

    artifacts: list[tuple[str, str, str, Path, pd.DataFrame, set[str] | None, str]] = [
        (
            "run_manifest",
            "manifest",
            "planned_runs",
            output_root / "run_manifest.csv",
            run_manifest_df,
            {"run_group", "scenario", "seed", "crosswalk_id", "output_dir"},
            "source-of-truth run plan",
        ),
        (
            "run_level_results",
            "post_run",
            "run_outputs",
            output_root / "csv" / "seed30_run_level_results.csv",
            run_level_summary,
            {"run_group", "scenario", "seed", "completed"},
            "per-run smoke output summary",
        ),
        (
            "baseline_smart_comparison",
            "post_run",
            "paired_results",
            output_root / "csv" / "seed30_baseline_smart_comparison.csv",
            comparison_df,
            {"crosswalk_id", "seed", "baseline_output_dir", "smart_output_dir"},
            "baseline vs smart pairing table",
        ),
        (
            "metric_summary",
            "post_run",
            "paired_results",
            output_root / "csv" / "seed30_metric_summary.csv",
            metric_df,
            {"crosswalk_id", "metric"},
            "metric aggregation across paired runs",
        ),
        (
            "group_summary",
            "post_run",
            "group_summary",
            output_root / "csv" / "seed30_group_summary.csv",
            group_summary_df,
            {"run_group", "completed_runs"},
            "high-level per-group completion summary",
        ),
        (
            "run_completion_summary",
            "post_run",
            "summary",
            output_root / "run_completion_summary.csv",
            completion_summary_df,
            {"planned_total_runs", "completed_runs", "missing_runs"},
            "pipeline completion summary",
        ),
        (
            "csv_output_inventory",
            "audit",
            "csv_inventory",
            output_root / "csv" / "csv_output_inventory.csv",
            inventory_df,
            {"file_path", "has_runtime_columns", "has_safety_columns"},
            "inventory of CSV files under result root",
        ),
        (
            "csv_pipeline_audit_summary",
            "audit",
            "csv_inventory",
            output_root / "csv" / "csv_pipeline_audit_summary.csv",
            audit_summary_df,
            {"csv_file_count", "csv_with_runtime_columns", "csv_with_safety_columns"},
            "aggregate CSV audit summary",
        ),
        (
            "safety_metric_presence_check",
            "audit",
            "safety",
            output_root / "csv" / "safety_metric_presence_check.csv",
            presence_df,
            {"file_path", "has_safety"},
            "keyword-based safety metric presence scan",
        ),
        (
            "safety_surrogate_summary",
            "audit",
            "safety",
            output_root / "csv" / "safety_surrogate_summary.csv",
            safety_summary_df if safety_summary_df is not None else pd.DataFrame(),
            {"run_name", "crosswalk_id", "elapsed_sec"},
            "safety surrogate summary when metrics are available",
        ),
        (
            "safety_surrogate_missing_reason",
            "audit",
            "safety",
            output_root / "csv" / "safety_surrogate_missing_reason.csv",
            safety_missing_df,
            {"reason", "note"},
            "fallback when full safety surrogate metrics are absent",
        ),
        (
            "result_cleanup_inventory",
            "cleanup",
            "cleanup",
            output_root / "csv" / "result_cleanup_inventory.csv",
            cleanup_audit_df,
            {"file_path", "cleanup_status", "safe_to_delete"},
            "dry-run cleanup inventory",
        ),
        (
            "result_cleanup_safe_to_delete",
            "cleanup",
            "cleanup",
            output_root / "csv" / "result_cleanup_safe_to_delete.csv",
            safe_df,
            {"file_path", "safe_to_delete"},
            "conservative safe-to-archive subset",
        ),
        (
            "result_cleanup_review_needed",
            "cleanup",
            "cleanup",
            output_root / "csv" / "result_cleanup_review_needed.csv",
            review_df,
            {"file_path", "cleanup_status"},
            "manual review subset",
        ),
        (
            "result_cleanup_protected_files",
            "cleanup",
            "cleanup",
            output_root / "csv" / "result_cleanup_protected_files.csv",
            protected_df,
            {"file_path", "cleanup_status"},
            "protected files subset",
        ),
    ]

    for validation_id, stage, scope, path, df, required, note in artifacts:
        rows.append(
            {
                "validation_id": validation_id,
                "validation_stage": stage,
                "validation_scope": scope,
                "source_artifact": path.name,
                "source_path": _portable_path(path),
                "status": _status(df, required),
                "row_count": int(len(df)) if df is not None else 0,
                "column_count": int(len(df.columns)) if df is not None else 0,
                "evidence_summary": f"rows={len(df)}, cols={len(df.columns)}" if df is not None else "rows=0, cols=0",
                "note": note,
                "manifest_path": _portable_path(manifest_path),
            }
        )
    return pd.DataFrame(rows)


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
    outcome = _benchmark_outcome(path, manifest_row)
    manifest_fields = {
        "manifest_run_id": manifest_row["run_id"],
        "manifest_scenario": manifest_row["scenario"],
        "manifest_crosswalk_id": manifest_row["crosswalk_id"],
        "manifest_row_role": manifest_row.get("manifest_row_role", ""),
        "execution_unit": manifest_row.get("execution_unit", ""),
        "network_input_mode": manifest_row.get("network_input_mode", ""),
    }
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
                    "note": "missing_expected_summary_csv",
                    **manifest_fields,
                    **outcome,
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
        "seed": int(manifest_row["seed"]),
        "sim_duration": 600,
        "warmup": 0,
        "step_length": SAMPLED10_STEP_LENGTH if str(manifest_row.get("execution_unit", "")) == "paired_group_net" else 0.5,
        "output_dir": manifest_row["output_dir"],
        "run_start_time": "",
        "run_end_time": "",
        "elapsed_sec": np.nan,
    }.items():
        if col not in summary.columns:
            summary[col] = value
    for col, value in manifest_fields.items():
        summary[col] = value
    for col, value in outcome.items():
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
        "manifest_run_id",
        "manifest_scenario",
        "manifest_crosswalk_id",
        "manifest_row_role",
        "execution_unit",
        "network_input_mode",
        "run_success",
        "failure_reason",
        "failed_cases_count",
        "result_rows",
        "baseline_result_rows",
        "smart_result_rows",
        "experiment_mode",
        "metrics_exact",
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

    if "manifest_scenario" in run_level_df.columns:
        comparison_source_df = run_level_df[run_level_df["manifest_scenario"].astype(str) == "smart"].copy()
    else:
        comparison_source_df = run_level_df.copy()

    baseline_map: dict[tuple[str, str, int, str], pd.Series] = {}
    smart_map: dict[tuple[str, str, int, str], pd.Series] = {}
    for _, row in comparison_source_df.iterrows():
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
    ready_comparison_df = comparison_df[comparison_df["comparison_status"] == "ready"].copy() if not comparison_df.empty and "comparison_status" in comparison_df.columns else comparison_df
    if not ready_comparison_df.empty:
        for (run_group, net_group, crosswalk_id), sub in ready_comparison_df.groupby(["run_group", "net_group", "crosswalk_id"], dropna=False):
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
        group_run_count_df = group_runs.drop_duplicates("manifest_run_id") if "manifest_run_id" in group_runs.columns else group_runs
        comp = comparison_df[comparison_df["run_group"] == spec.run_group].copy() if not comparison_df.empty else pd.DataFrame()
        rows.append(
            {
                "run_group": spec.run_group,
                "net_group": spec.net_group,
                "candidate_count": int(len(spec.smart_candidates)),
                "expected_baseline_runs": 30,
                "expected_smart_runs": int(len(spec.smart_candidates)) * 30,
                "expected_total_runs": 30 + int(len(spec.smart_candidates)) * 30,
                "completed_runs": int(group_run_count_df["completed"].fillna(False).astype(bool).sum()) if not group_run_count_df.empty and "completed" in group_run_count_df.columns else 0,
                "comparison_ready_runs": int((comp["comparison_status"] == "ready").sum()) if not comp.empty and "comparison_status" in comp.columns else 0,
                "baseline_mean_elapsed_sec": float(pd.to_numeric(group_runs[group_runs["scenario"] == "baseline"]["elapsed_sec"], errors="coerce").dropna().mean()) if not group_runs.empty else np.nan,
                "smart_mean_elapsed_sec": float(pd.to_numeric(group_runs[group_runs["scenario"] == "smart"]["elapsed_sec"], errors="coerce").dropna().mean()) if not group_runs.empty else np.nan,
            }
        )
    return pd.DataFrame(rows)


def _run_completion_summary_df(run_manifest_df: pd.DataFrame, run_level_df: pd.DataFrame, output_root: Path) -> pd.DataFrame:
    completed_outputs = _completed_manifest_count(run_level_df)
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
    *,
    sampled10: bool = False,
    nets_dir: Path | None = None,
) -> dict[str, pd.DataFrame]:
    _ensure_dir(output_root)
    for rel in ["csv", "xml", "logs", "readme", "commands", "manifests", "figures", "runs"]:
        _ensure_dir(output_root / rel)

    single_csv_root = output_root / "manifests" / "single_candidates"
    for spec in specs:
        _build_single_candidate_csvs(spec, single_csv_root)

    if sampled10:
        if nets_dir is None:
            raise ValueError("nets_dir is required for sampled10 prepare mode")
        run_manifest_df, baseline_df, smart_df = _build_run_manifest_sampled10(specs, output_root, nets_dir)
    else:
        run_manifest_df, baseline_df, smart_df = _build_run_manifest(specs, output_root)
    candidate_metadata_df = _build_candidate_metadata(
        specs,
        _default_candidate_metadata_source(),
        expected_smart_ids=set(str(v).strip() for v in run_manifest_df.loc[run_manifest_df["scenario"] == "smart", "crosswalk_id"].astype(str).tolist()),
    )
    plan_df = _build_plan_by_group(specs, output_root)
    run_level_summary = _build_run_level_results(run_manifest_df)
    comparison_df, metric_df = _comparison_rows(run_level_summary, run_manifest_df)
    group_summary_df = _group_summary_df(run_level_summary, comparison_df, specs)
    completion_summary_df = _run_completion_summary_df(run_manifest_df, run_level_summary, output_root)

    if sampled10:
        inventory_df = pd.DataFrame()
        presence_df = pd.DataFrame()
    else:
        inventory_df = _inventory_csvs(result_root, exclude_root=output_root)
        presence_df = _presence_check_df(inventory_df, result_root) if not inventory_df.empty else pd.DataFrame()
    safety_summary_df, safety_missing_df = _safety_surrogate_summary(run_level_summary)
    cleanup_scan_tmp = None
    if sampled10:
        cleanup_audit_df = pd.DataFrame(columns=["file_path", "type", "size_bytes", "size_mb", "cleanup_status", "reason", "suggested_action", "safe_to_delete"])
    else:
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
    _write_csv(candidate_metadata_df, output_root / "manifests" / "candidate_metadata.csv")
    _write_csv(baseline_df, output_root / "manifests" / "baseline_run_manifest.csv")
    _write_csv(smart_df, output_root / "manifests" / "smart_run_manifest.csv")
    _write_csv(plan_df, output_root / "manifests" / "run_plan_by_group.csv")
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
    validation_manifest_df = _build_validation_manifest(
        output_root,
        run_manifest_df,
        run_level_summary,
        comparison_df,
        metric_df,
        group_summary_df,
        completion_summary_df,
        inventory_df,
        audit_summary_df,
        presence_df,
        safety_summary_df,
        safety_missing_df,
        cleanup_audit_df,
        safe_df,
        review_df,
        protected_df,
    )
    _write_csv(validation_manifest_df, output_root / "manifests" / "run_validation_manifest.csv")
    _write_csv(
        validation_manifest_df[
            [
                "validation_id",
                "validation_stage",
                "validation_scope",
                "status",
                "row_count",
                "column_count",
                "source_artifact",
                "source_path",
            ]
        ].copy(),
        output_root / "csv" / "run_validation_summary.csv",
    )
    _write_csv(run_level_summary, output_root / "csv" / "seed30_run_level_results.csv")
    _write_csv(comparison_df, output_root / "csv" / "seed30_baseline_smart_comparison.csv")
    _write_csv(metric_df, output_root / "csv" / "seed30_metric_summary.csv")
    _write_csv(group_summary_df, output_root / "csv" / "seed30_group_summary.csv")
    _write_csv(completion_summary_df, output_root / "run_completion_summary.csv")
    _write_csv(completion_summary_df, output_root / "csv" / "run_completion_summary.csv")
    _write_csv(inventory_df, output_root / "csv" / "csv_output_inventory.csv")
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
            "candidate_metadata.csv": candidate_metadata_df,
            "run_validation_manifest.csv": validation_manifest_df,
            "run_completion_summary.csv": completion_summary_df,
            "seed30_run_level_results.csv": run_level_summary,
            "seed30_baseline_smart_comparison.csv": comparison_df,
            "seed30_metric_summary.csv": metric_df,
            "seed30_group_summary.csv": group_summary_df,
            "run_validation_summary.csv": validation_manifest_df[
                [
                    "validation_id",
                    "validation_stage",
                    "validation_scope",
                    "status",
                    "row_count",
                    "column_count",
                    "source_artifact",
                    "source_path",
                ]
            ].copy(),
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
            "candidate_metadata.csv": "Candidate metadata preserved separately from the execution manifest. One row per smart candidate.",
            "run_validation_manifest.csv": "Catalog of validation artifacts produced by the pipeline package.",
            "run_completion_summary.csv": "Planned run completion summary for the pipeline root.",
            "seed30_run_level_results.csv": "One row per run-level output row from phase6_smoke_summary.csv.",
            "seed30_baseline_smart_comparison.csv": "Baseline vs smart comparison matched by run_group, net_group, seed, and crosswalk_id.",
            "seed30_metric_summary.csv": "Grouped metric summary across seeds.",
            "seed30_group_summary.csv": "High-level per-group completion summary.",
            "run_validation_summary.csv": "Compact summary view of the validation catalog.",
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
    if sampled10:
        _write_text(output_root / "commands" / "command_to_run_30seed_current_main_12.sh", _build_sampled10_group_script(specs[0], output_root, single_csv_root, nets_dir))
        _write_text(output_root / "commands" / "command_to_run_30seed_signal_fix_9.sh", _build_sampled10_group_script(specs[1], output_root, single_csv_root, nets_dir))
        _write_text(output_root / "commands" / "command_to_run_30seed_generated_signal_7.sh", _build_sampled10_group_script(specs[2], output_root, single_csv_root, nets_dir))
        _write_text(output_root / "commands" / "command_to_run_seed1_current_main_12.sh", _build_sampled10_group_script(specs[0], output_root, single_csv_root, nets_dir, seed_start=1, seed_end=1))
        _write_text(output_root / "commands" / "command_to_run_seed1_signal_fix_9.sh", _build_sampled10_group_script(specs[1], output_root, single_csv_root, nets_dir, seed_start=1, seed_end=1))
        _write_text(output_root / "commands" / "command_to_run_seed1_generated_signal_7.sh", _build_sampled10_group_script(specs[2], output_root, single_csv_root, nets_dir, seed_start=1, seed_end=1))
        _write_text(output_root / "commands" / "command_to_run_smoke30_seed1_current_main_12.sh", _build_sampled10_group_script(specs[0], output_root, single_csv_root, nets_dir, seed_start=1, seed_end=1, sim_duration=30, smoke_root=True))
        _write_text(output_root / "commands" / "command_to_run_smoke30_seed1_signal_fix_9.sh", _build_sampled10_group_script(specs[1], output_root, single_csv_root, nets_dir, seed_start=1, seed_end=1, sim_duration=30, smoke_root=True))
        _write_text(output_root / "commands" / "command_to_run_smoke30_seed1_generated_signal_7.sh", _build_sampled10_group_script(specs[2], output_root, single_csv_root, nets_dir, seed_start=1, seed_end=1, sim_duration=30, smoke_root=True))
    else:
        _write_text(output_root / "commands" / "command_to_run_30seed_current_main_12.sh", _build_group_script(specs[0], output_root, single_csv_root))
        _write_text(output_root / "commands" / "command_to_run_30seed_signal_fix_9.sh", _build_group_script(specs[1], output_root, single_csv_root))
        _write_text(output_root / "commands" / "command_to_run_30seed_generated_signal_7.sh", _build_group_script(specs[2], output_root, single_csv_root))
    if len(specs) > 3:
        if sampled10:
            _write_text(output_root / "commands" / "command_to_run_30seed_p1_p4_recovery_6.sh", _build_sampled10_group_script(specs[3], output_root, single_csv_root, nets_dir))
            _write_text(output_root / "commands" / "command_to_run_seed1_p1_p4_recovery_6.sh", _build_sampled10_group_script(specs[3], output_root, single_csv_root, nets_dir, seed_start=1, seed_end=1))
            _write_text(output_root / "commands" / "command_to_run_smoke30_seed1_p1_p4_recovery_6.sh", _build_sampled10_group_script(specs[3], output_root, single_csv_root, nets_dir, seed_start=1, seed_end=1, sim_duration=30, smoke_root=True))
        else:
            _write_text(output_root / "commands" / "command_to_run_30seed_p1_p4_recovery_6.sh", _build_group_script(specs[3], output_root, single_csv_root))
    _write_text(output_root / "commands" / "command_to_run_30seed_all_groups.sh", _build_sampled10_all_groups_script(output_root) if sampled10 else _build_all_groups_script(output_root))
    if sampled10:
        _write_text(output_root / "commands" / "command_to_run_seed1_all_groups.sh", _build_sampled10_all_groups_script(output_root, seed1_only=True))
        _write_text(output_root / "commands" / "command_to_run_smoke30_seed1_all_groups.sh", _build_sampled10_all_groups_script(output_root, smoke30=True))
    _write_text(output_root / "commands" / "command_to_standardize_30seed_outputs.sh", _build_standardize_script(output_root))
    _write_text(output_root / "commands" / "command_to_check_30seed_results.sh", _build_check_script(output_root))
    _write_text(output_root / "commands" / "command_to_open_30seed_summary.sh", _build_open_script(output_root))
    dry_run_script, apply_script = _build_cleanup_scripts(output_root, result_root)
    _write_text(output_root / "commands" / "command_to_cleanup_result_dry_run.sh", dry_run_script)
    _write_text(output_root / "commands" / "command_to_cleanup_result_apply.sh", apply_script)

    if cleanup_scan_tmp is not None:
        shutil.rmtree(cleanup_scan_tmp, ignore_errors=True)

    for script in (output_root / "commands").glob("*.sh"):
        script.chmod(0o755)

    return {
        "run_manifest": run_manifest_df,
        "candidate_metadata": candidate_metadata_df,
        "run_validation_manifest": validation_manifest_df,
        "baseline_manifest": baseline_df,
        "smart_manifest": smart_df,
        "run_plan_by_group": plan_df,
        "run_validation_summary": validation_manifest_df[
            [
                "validation_id",
                "validation_stage",
                "validation_scope",
                "status",
                "row_count",
                "column_count",
                "source_artifact",
                "source_path",
            ]
        ].copy(),
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
    completed = _completed_manifest_count(run_level)
    missing = max(int(len(run_manifest)) - completed, 0)
    group_counts = {}
    if not run_level.empty and {"run_group", "completed"}.issubset(run_level.columns):
        for group, sub in run_level.groupby("run_group", dropna=False):
            group_counts[str(group)] = _completed_manifest_count(sub)
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
    output_root = (
        Path(args.output_root).expanduser().resolve()
        if args.output_root
        else result_root / f"phase_next_30seed_28_ready_pipeline_{_now_stamp()}"
    )
    active_root = Path(args.active_root).expanduser().resolve() if args.active_root else None
    specs = _load_group_specs(input_root, output_root, active_root=active_root)
    _write_prepare_outputs(input_root, output_root, result_root, specs)
    total_runs = sum(30 + len(s.smart_candidates) * 30 for s in specs)
    print(
        json.dumps(
            {"output_root": str(output_root), "planned_total_runs": total_runs, "group_count": len(specs)},
            ensure_ascii=False,
            indent=2,
        )
    )


def cmd_prepare_sampled10(args: argparse.Namespace) -> None:
    input_root = Path(args.input_root).expanduser().resolve()
    result_root = Path(args.result_root).expanduser().resolve()
    output_root = (
        Path(args.output_root).expanduser().resolve()
        if args.output_root
        else result_root / "active" / "real_30seed_runs_sampled10"
    )
    active_root = Path(args.active_root).expanduser().resolve() if args.active_root else result_root / "active"
    nets_dir = active_root / "nets"
    specs = _load_group_specs(input_root, output_root, active_root=active_root)
    _write_prepare_outputs(input_root, output_root, result_root, specs, sampled10=True, nets_dir=nets_dir)
    total_runs = sum(30 + len(s.smart_candidates) * 30 for s in specs)
    print(
        json.dumps(
            {
                "output_root": str(output_root),
                "planned_total_runs": total_runs,
                "group_count": len(specs),
                "experiment_mode": "sampled",
                "metric_sample_interval_s": SAMPLED10_METRIC_SAMPLE_INTERVAL,
                "vehicle_sample_interval_s": SAMPLED10_VEHICLE_SAMPLE_INTERVAL,
                "progress_interval_s": SAMPLED10_PROGRESS_INTERVAL,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


def cmd_aggregate(args: argparse.Namespace) -> None:
    pipeline_root = Path(args.pipeline_root).expanduser().resolve()
    run_manifest = _read_csv(pipeline_root / "run_manifest.csv")
    if run_manifest.empty:
        raise FileNotFoundError(pipeline_root / "run_manifest.csv")
    result_root = pipeline_root.parent
    run_level_df = _build_run_level_results(run_manifest)
    comparison_df, metric_df = _comparison_rows(run_level_df, run_manifest)
    specs = _load_group_specs(Path(args.input_root).expanduser().resolve() if args.input_root else pipeline_root.parent / "phase_next_top50_max_installation_recovery_20260516_002620", pipeline_root)
    candidate_metadata_path = pipeline_root / "manifests" / "candidate_metadata.csv"
    if candidate_metadata_path.exists():
        candidate_metadata_df = _read_csv(candidate_metadata_path)
    else:
        candidate_metadata_df = _build_candidate_metadata(
            specs,
            _default_candidate_metadata_source(),
            expected_smart_ids=set(str(v).strip() for v in run_manifest.loc[run_manifest["scenario"] == "smart", "crosswalk_id"].astype(str).tolist()),
        )
    group_summary_df = _group_summary_df(run_level_df, comparison_df, specs)
    completion_summary_df = _run_completion_summary_df(run_manifest, run_level_df, pipeline_root)
    _write_csv(run_level_df, pipeline_root / "csv" / "seed30_run_level_results.csv")
    _write_csv(comparison_df, pipeline_root / "csv" / "seed30_baseline_smart_comparison.csv")
    _write_csv(metric_df, pipeline_root / "csv" / "seed30_metric_summary.csv")
    _write_csv(candidate_metadata_df, pipeline_root / "manifests" / "candidate_metadata.csv")
    _write_csv(group_summary_df, pipeline_root / "csv" / "seed30_group_summary.csv")
    _write_csv(completion_summary_df, pipeline_root / "csv" / "run_completion_summary.csv")
    _write_csv(completion_summary_df, pipeline_root / "run_completion_summary.csv")
    if pipeline_root.name.endswith("sampled10"):
        inventory_df = pd.DataFrame()
        presence_df = pd.DataFrame()
        cleanup_scan_tmp = None
        cleanup_audit_df = pd.DataFrame(columns=["file_path", "type", "size_bytes", "size_mb", "cleanup_status", "reason", "suggested_action", "safe_to_delete"])
    else:
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
    _readme_files(
        pipeline_root,
        {"candidate_metadata.csv": candidate_metadata_df},
        {"candidate_metadata.csv": "Candidate metadata preserved separately from the execution manifest. One row per smart candidate."},
    )
    if cleanup_scan_tmp is not None:
        shutil.rmtree(cleanup_scan_tmp, ignore_errors=True)
    if args.copy_xml:
        _copy_xml_like(pipeline_root / "runs", pipeline_root, copy_logs=args.copy_logs)
    print(json.dumps({"output_root": str(pipeline_root), "completed_runs": _completed_manifest_count(run_level_df)}, ensure_ascii=False, indent=2))


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


def cmd_pedestrian_smoke_validation(args: argparse.Namespace) -> None:
    run_manifest_path = Path(args.run_manifest).expanduser().resolve()
    candidate_metadata_path = Path(args.candidate_metadata).expanduser().resolve()
    validation_root = Path(args.validation_root).expanduser().resolve()
    validation_root.mkdir(parents=True, exist_ok=True)

    run_manifest_df = _read_csv(run_manifest_path)
    if run_manifest_df.empty:
        raise FileNotFoundError(run_manifest_path)
    candidate_metadata_df = _read_csv(candidate_metadata_path)
    if candidate_metadata_df.empty:
        raise FileNotFoundError(candidate_metadata_path)

    demand_source_df, demand_source_paths = _load_pedestrian_demand_sources()
    demand_lookup = _resolve_demand_lookup(demand_source_df)
    candidate_groups = candidate_metadata_df.copy()
    if not candidate_groups.empty and "run_group" in candidate_groups.columns:
        candidate_groups["run_group"] = candidate_groups["run_group"].astype(str)

    demand_rows: list[dict[str, Any]] = []
    runtime_rows: list[dict[str, Any]] = []

    for _, manifest_row in run_manifest_df.iterrows():
        run_id = _safe_str(manifest_row.get("run_id"))
        scenario = _safe_str(manifest_row.get("scenario"))
        seed = manifest_row.get("seed")
        net_group = _safe_str(manifest_row.get("net_group"))
        run_group = _safe_str(manifest_row.get("run_group"))
        crosswalk_id = _safe_str(manifest_row.get("crosswalk_id"))
        output_dir = Path(_safe_str(manifest_row.get("output_dir"))).expanduser()
        output_exists = output_dir.is_dir()
        summary_df = _load_smoke_summary_row(output_dir)
        demand_xml = output_dir / "demand_pedestrian.rou.xml"
        depart_plan = output_dir / "pedestrian_depart_plan.csv"
        diagnostics = output_dir / "pedestrian_route_diagnostics.csv"
        validation_json_path = _find_smoke_candidate_validation_json(output_dir) if output_exists else None
        runtime_observed_ped_count = _sum_runtime_observed_ped_count(summary_df)
        xml_person_count = _count_xml_persons(demand_xml)
        extension_count = None
        if not summary_df.empty and "extension_count" in summary_df.columns:
            ext_values = pd.to_numeric(summary_df["extension_count"], errors="coerce").dropna()
            if not ext_values.empty:
                extension_count = float(ext_values.iloc[0])

        group_candidates = candidate_groups[candidate_groups["run_group"] == run_group].copy()
        group_candidate_ids = group_candidates["crosswalk_id"].astype(str).tolist() if not group_candidates.empty else []
        row_scope_ids = [crosswalk_id] if scenario == "smart" else group_candidate_ids
        row_source_rows: list[dict[str, Any]] = []
        missing_admins: list[str] = []
        source_files: list[str] = []
        demand_values: list[float] = []
        for cid in row_scope_ids:
            meta_row = group_candidates[group_candidates["crosswalk_id"].astype(str) == cid]
            if meta_row.empty:
                continue
            admin = _safe_str(meta_row.iloc[0].get("admin_dong"))
            if not admin:
                missing_admins.append("")
                continue
            src = demand_lookup.get(admin)
            if src is None or src.get("demand_10min") is None or pd.isna(src.get("demand_10min")):
                missing_admins.append(admin)
                continue
            demand_values.append(float(src["demand_10min"]))
            if src.get("source_file"):
                source_files.append(str(src["source_file"]))
            row_source_rows.append(src)

        unique_source_files = sorted({s for s in source_files if s})
        demand_source_file = ";".join(unique_source_files) if unique_source_files else (";".join(path.as_posix() for path in demand_source_paths) if demand_source_paths else "")
        demand_key_type = "admin_dong"

        if scenario == "smart":
            demand_10min = demand_values[0] if len(demand_values) == 1 else np.nan
            expected_generated_person_count = demand_10min
            baseline_group_expected_person_count = np.nan
            baseline_group_generated_person_count = np.nan
            source_coverage_pass = len(demand_values) == 1 and not missing_admins
        else:
            demand_10min = float(sum(demand_values)) if demand_values and not missing_admins else (float(sum(demand_values)) if demand_values else np.nan)
            expected_generated_person_count = demand_10min
            baseline_group_expected_person_count = demand_10min
            baseline_group_generated_person_count = xml_person_count
            source_coverage_pass = bool(demand_values) and not missing_admins and len(demand_values) == len(group_candidate_ids)

        demand_implementation_pass = bool(
            source_coverage_pass
            and xml_person_count is not None
            and expected_generated_person_count is not None
            and not pd.isna(expected_generated_person_count)
            and float(xml_person_count) == float(expected_generated_person_count)
        )

        runtime_observation_pass = bool(runtime_observed_ped_count is not None and float(runtime_observed_ped_count) > 0)

        extension_pass = False
        expected_extension_count = 1 if scenario == "smart" else 0
        if extension_count is not None:
            extension_pass = bool(int(extension_count) == expected_extension_count)

        overall_pass = bool(demand_implementation_pass and runtime_observation_pass and extension_pass and output_exists and summary_df is not None and not summary_df.empty)
        reason_parts = [
            "" if output_exists else "output_dir_missing",
            "" if summary_df is not None and not summary_df.empty else "phase6_smoke_summary_missing",
            "" if demand_xml.exists() else "demand_pedestrian_missing",
            "" if depart_plan.exists() else "pedestrian_depart_plan_missing",
            "" if xml_person_count is not None else "xml_person_count_unavailable",
            "" if runtime_observed_ped_count is not None else "runtime_observation_unavailable",
            "" if source_coverage_pass else f"missing_source_admin_dong={','.join(sorted({a for a in missing_admins if a}))}",
            "" if demand_implementation_pass else f"demand_mismatch(expected={expected_generated_person_count},generated={xml_person_count})",
            "" if runtime_observation_pass else "runtime_ped_count_zero_or_missing",
            "" if extension_pass else f"extension_mismatch(expected={expected_extension_count},observed={extension_count})",
        ]
        demand_rows.append(
            {
                "run_id": run_id,
                "scenario": scenario,
                "seed": seed,
                "net_group": net_group,
                "run_group": run_group,
                "crosswalk_id": crosswalk_id,
                "dong_name": ";".join(sorted(set(group_candidates["dong_name"].astype(str).tolist()))) if scenario == "baseline" and not group_candidates.empty else _safe_str(candidate_metadata_df.loc[candidate_metadata_df["crosswalk_id"].astype(str) == crosswalk_id, "dong_name"].iloc[0]) if not candidate_metadata_df[candidate_metadata_df["crosswalk_id"].astype(str) == crosswalk_id].empty else "",
                "admin_dong": ";".join(sorted(set(group_candidates["admin_dong"].astype(str).tolist()))) if scenario == "baseline" and not group_candidates.empty else _safe_str(candidate_metadata_df.loc[candidate_metadata_df["crosswalk_id"].astype(str) == crosswalk_id, "admin_dong"].iloc[0]) if not candidate_metadata_df[candidate_metadata_df["crosswalk_id"].astype(str) == crosswalk_id].empty else "",
                "output_dir": str(output_dir),
                "demand_source_file": demand_source_file,
                "demand_key_type": demand_key_type,
                "demand_10min": demand_10min,
                "expected_generated_person_count": expected_generated_person_count,
                "generated_person_count_from_xml": xml_person_count,
                "baseline_group_expected_person_count": baseline_group_expected_person_count,
                "baseline_group_generated_person_count": baseline_group_generated_person_count,
                "demand_implementation_pass": demand_implementation_pass,
                "extension_pass": extension_pass,
                "overall_pass": overall_pass,
                "reason": _validation_reason(reason_parts),
            }
        )
        runtime_rows.append(
            {
                "run_id": run_id,
                "scenario": scenario,
                "seed": seed,
                "net_group": net_group,
                "run_group": run_group,
                "crosswalk_id": crosswalk_id,
                "dong_name": ";".join(sorted(set(group_candidates["dong_name"].astype(str).tolist()))) if scenario == "baseline" and not group_candidates.empty else _safe_str(candidate_metadata_df.loc[candidate_metadata_df["crosswalk_id"].astype(str) == crosswalk_id, "dong_name"].iloc[0]) if not candidate_metadata_df[candidate_metadata_df["crosswalk_id"].astype(str) == crosswalk_id].empty else "",
                "admin_dong": ";".join(sorted(set(group_candidates["admin_dong"].astype(str).tolist()))) if scenario == "baseline" and not group_candidates.empty else _safe_str(candidate_metadata_df.loc[candidate_metadata_df["crosswalk_id"].astype(str) == crosswalk_id, "admin_dong"].iloc[0]) if not candidate_metadata_df[candidate_metadata_df["crosswalk_id"].astype(str) == crosswalk_id].empty else "",
                "output_dir": str(output_dir),
                "runtime_observed_ped_count": runtime_observed_ped_count,
                "runtime_observation_pass": runtime_observation_pass,
                "pedestrian_crossing_pass": runtime_observation_pass,
                "phase6_smoke_summary_exists": bool(summary_df is not None and not summary_df.empty),
                "pedestrian_depart_plan_exists": depart_plan.exists(),
                "pedestrian_route_diagnostics_exists": diagnostics.exists(),
                "candidate_validation_json_exists": bool(validation_json_path),
                "extension_pass": extension_pass,
                "overall_pass": bool(runtime_observation_pass and extension_pass and output_exists),
                "reason": _validation_reason([
                    "" if output_exists else "output_dir_missing",
                    "" if summary_df is not None and not summary_df.empty else "phase6_smoke_summary_missing",
                    "" if runtime_observed_ped_count is not None else "runtime_observation_unavailable",
                    "" if runtime_observation_pass else "runtime_ped_count_zero_or_missing",
                    "" if extension_pass else f"extension_mismatch(expected={expected_extension_count},observed={extension_count})",
                ]),
            }
        )

    demand_df = pd.DataFrame(demand_rows)
    runtime_df = pd.DataFrame(runtime_rows)

    summary_rows: list[dict[str, Any]] = []
    for label, df in [("demand", demand_df), ("runtime", runtime_df)]:
        for keys, sub in df.groupby(["run_group", "net_group", "scenario"], dropna=False):
            run_group, net_group, scenario = keys
            summary_rows.append(
                {
                    "validation_lane": label,
                    "run_group": run_group,
                    "net_group": net_group,
                    "scenario": scenario,
                    "total_runs": int(len(sub)),
                    "pass_runs": int(sub["overall_pass"].fillna(False).astype(bool).sum()) if "overall_pass" in sub.columns else 0,
                    "demand_pass_runs": int(sub["demand_implementation_pass"].fillna(False).astype(bool).sum()) if "demand_implementation_pass" in sub.columns else 0,
                    "runtime_pass_runs": int(sub["runtime_observation_pass"].fillna(False).astype(bool).sum()) if "runtime_observation_pass" in sub.columns else 0,
                    "extension_pass_runs": int(sub["extension_pass"].fillna(False).astype(bool).sum()) if "extension_pass" in sub.columns else 0,
                    "missing_output_runs": int((~sub["output_dir"].astype(str).map(lambda x: Path(x).is_dir())).sum()),
                    "missing_summary_runs": int((~sub["phase6_smoke_summary_exists"].fillna(False).astype(bool)).sum()) if "phase6_smoke_summary_exists" in sub.columns else 0,
                }
            )
    summary_df = pd.DataFrame(summary_rows).sort_values(["validation_lane", "run_group", "net_group", "scenario"]).reset_index(drop=True)

    _write_csv(demand_df, validation_root / "pedestrian_demand_implementation_validation.csv")
    _write_csv(runtime_df, validation_root / "pedestrian_runtime_observation_validation.csv")
    _write_csv(summary_df, validation_root / "pedestrian_validation_summary.csv")
    _write_text(
        validation_root / "README.md",
        "\n".join(
            [
                "# Pedestrian Demand Validation",
                "",
                f"Run manifest: `{run_manifest_path}`",
                f"Candidate metadata: `{candidate_metadata_path}`",
                "",
                "Demand implementation uses XML person count vs source 10min demand.",
                "Runtime observation is separate and only checks pedestrian presence.",
                f"Demand sources: `{';'.join(path.as_posix() for path in demand_source_paths)}`",
                "",
            ]
        ),
    )
    print(
        json.dumps(
            {
                "validation_root": str(validation_root),
                "demand_rows": int(len(demand_df)),
                "runtime_rows": int(len(runtime_df)),
                "summary_rows": int(len(summary_df)),
                "overall_pass_rows": int(demand_df["overall_pass"].fillna(False).astype(bool).sum()) if not demand_df.empty and "overall_pass" in demand_df.columns else 0,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Prepare and aggregate the 30-seed pipeline.")
    sub = parser.add_subparsers(dest="cmd", required=True)

    prepare = sub.add_parser("prepare", help="build manifests, cleanup inventory, command scripts, and README files")
    prepare.add_argument("--input-root", default="result/phase_next_top50_max_installation_recovery_20260516_002620")
    prepare.add_argument("--result-root", default="result")
    prepare.add_argument("--output-root", default=None)
    prepare.add_argument("--active-root", default=None,
                         help="Explicit path to result/active/ (preferred source of truth for nets/csv). "
                              "Overrides legacy input-root CSV/net lookup.")
    prepare.set_defaults(func=cmd_prepare)

    prepare_sampled10 = sub.add_parser("prepare-sampled10", help="build sampled10 manifests, cleanup inventory, command scripts, and README files")
    prepare_sampled10.add_argument("--input-root", default="result/phase_next_top50_max_installation_recovery_20260516_002620")
    prepare_sampled10.add_argument("--result-root", default="result")
    prepare_sampled10.add_argument("--output-root", default=None)
    prepare_sampled10.add_argument("--active-root", default=None,
                                   help="Explicit path to result/active/ (preferred source of truth for nets/csv). "
                                        "Overrides legacy input-root CSV/net lookup.")
    prepare_sampled10.set_defaults(func=cmd_prepare_sampled10)

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

    ped_validation = sub.add_parser("pedestrian-smoke-validation", help="build pedestrian smoke validation CSVs from existing run outputs")
    ped_validation.add_argument("--run-manifest", default="result/active/real_30seed_runs/run_manifest.csv")
    ped_validation.add_argument("--candidate-metadata", default="result/active/real_30seed_runs/manifests/candidate_metadata.csv")
    ped_validation.add_argument("--validation-root", default="result/active/real_30seed_runs/validation")
    ped_validation.set_defaults(func=cmd_pedestrian_smoke_validation)

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
