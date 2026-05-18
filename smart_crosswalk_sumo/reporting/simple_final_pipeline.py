"""Final sampled10 command surface.

Roles:
- command wrapper for commands/*.sh
- final aggregation guardrails for sampled10/museum-derived runs
- source-of-truth final_summary.csv generator

TODO: split later, without changing behavior:
- final_result_pipeline.py
- final_aggregate_guardrails.py
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd

from smart_crosswalk_sumo.network_utils import resolve_sumo_home
from smart_crosswalk_sumo.reporting import aggregate_30seed_results as agg30


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SOURCE_PIPELINE_ROOT = PROJECT_ROOT / "result" / "active" / "real_30seed_runs_sampled10"
DEFAULT_OUTPUTS_ROOT = PROJECT_ROOT / "outputs"
SIMULATION_RESULT_NAME = "simulation_result.csv"
FINAL_SUMMARY_NAME = "final_summary.csv"
EXPECTED_COMMANDS = [
    "bash commands/verify.sh",
    "bash commands/run_smoke.sh",
    "bash commands/run_final.sh",
    "bash commands/aggregate_final.sh",
    "bash commands/check_final_result.sh",
]


@dataclass(frozen=True)
class ModeConfig:
    mode: str
    root_name: str
    sim_duration: int
    seed_start: int
    seed_end: int


MODE_CONFIGS = {
    "smoke": ModeConfig(mode="smoke", root_name="smoke", sim_duration=120, seed_start=1, seed_end=1),
    "final": ModeConfig(mode="final", root_name="final", sim_duration=600, seed_start=1, seed_end=30),
}


def _read_csv(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame()
    for encoding in ("utf-8-sig", "utf-8"):
        try:
            return pd.read_csv(path, encoding=encoding)
        except Exception:
            continue
    return pd.DataFrame()


def _write_csv(df: pd.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(path, index=False, encoding="utf-8-sig")


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _resolve_project_path(value: str) -> Path:
    path = Path(str(value).strip()).expanduser()
    if path.is_absolute():
        return path.resolve()
    return (PROJECT_ROOT / path).resolve()


def _portable_project_path(path: Path) -> str:
    try:
        return path.resolve().relative_to(PROJECT_ROOT).as_posix()
    except Exception:
        return str(path.resolve())


def _path_after_marker(value: str, marker: str) -> Path:
    path = Path(str(value))
    parts = list(path.parts)
    if marker not in parts:
        raise ValueError(f"Missing path marker `{marker}` in {value}")
    idx = parts.index(marker)
    return Path(*parts[idx + 1 :])


def _mode_root(outputs_root: Path, mode: str) -> Path:
    return outputs_root / MODE_CONFIGS[mode].root_name


def _source_manifest_root(source_pipeline_root: Path) -> Path:
    return source_pipeline_root / "manifests"


def _source_run_manifest(source_pipeline_root: Path) -> Path:
    return _source_manifest_root(source_pipeline_root) / "run_manifest.csv"


def _target_manifest_path(outputs_root: Path, mode: str) -> Path:
    return _mode_root(outputs_root, mode) / "manifests" / "run_manifest.csv"


def _target_mode_paths(outputs_root: Path, mode: str) -> dict[str, Path]:
    root = _mode_root(outputs_root, mode)
    return {
        "root": root,
        "manifests": root / "manifests",
        "runs": root / "runs",
        "logs": root / "logs",
        "debug": root / "debug",
        "audit": root / "audit",
        "aggregate": root / "aggregate",
    }


def _final_summary_path(outputs_root: Path) -> Path:
    return _target_mode_paths(outputs_root, "final")["aggregate"] / FINAL_SUMMARY_NAME


def _filtered_source_manifest(source_pipeline_root: Path, mode: str) -> pd.DataFrame:
    config = MODE_CONFIGS[mode]
    manifest = _read_csv(_source_run_manifest(source_pipeline_root))
    if manifest.empty:
        raise FileNotFoundError(_source_run_manifest(source_pipeline_root))
    seed_series = pd.to_numeric(manifest.get("seed"), errors="coerce")
    filtered = manifest[(seed_series >= config.seed_start) & (seed_series <= config.seed_end)].copy()
    filtered["seed"] = seed_series[(seed_series >= config.seed_start) & (seed_series <= config.seed_end)].astype(int)
    return filtered.reset_index(drop=True)


def _rewrite_manifest(source_pipeline_root: Path, outputs_root: Path, mode: str) -> pd.DataFrame:
    filtered = _filtered_source_manifest(source_pipeline_root, mode)
    paths = _target_mode_paths(outputs_root, mode)
    rows: list[dict[str, Any]] = []
    for _, row in filtered.iterrows():
        source_output_rel = _path_after_marker(str(row["output_dir"]), "runs")
        source_log_rel = _path_after_marker(str(row["log_file"]), "logs")
        final_output_dir = paths["runs"] / source_output_rel
        work_output_dir = paths["debug"] / "work" / source_output_rel
        final_log_file = paths["logs"] / source_log_rel
        out = dict(row)
        out["output_dir"] = _portable_project_path(final_output_dir)
        out["work_output_dir"] = _portable_project_path(work_output_dir)
        out["log_file"] = _portable_project_path(final_log_file)
        out["expected_summary_csv"] = _portable_project_path(final_output_dir / SIMULATION_RESULT_NAME)
        out["source_pipeline_root"] = str(source_pipeline_root)
        out["source_output_dir"] = _portable_project_path(_resolve_project_path(str(row["output_dir"])))
        out["result_csv_name"] = SIMULATION_RESULT_NAME
        out["run_mode"] = mode
        rows.append(out)
    return pd.DataFrame(rows)


def _copy_source_manifests(source_pipeline_root: Path, outputs_root: Path, mode: str) -> None:
    source_root = _source_manifest_root(source_pipeline_root)
    target_root = _target_mode_paths(outputs_root, mode)["manifests"]
    target_root.mkdir(parents=True, exist_ok=True)
    for name in [
        "baseline_run_manifest.csv",
        "candidate_metadata.csv",
        "current_main_12_candidates.csv",
        "generated_signal_7_candidates.csv",
        "p1_p4_recovery_6_candidates.csv",
        "run_plan_by_group.csv",
        "run_validation_manifest.csv",
        "signal_fix_9_candidates.csv",
        "smart_run_manifest.csv",
    ]:
        src = source_root / name
        if src.exists():
            shutil.copy2(src, target_root / name)
    source_single_root = source_root / "single_candidates"
    target_single_root = target_root / "single_candidates"
    if source_single_root.exists() and not target_single_root.exists():
        shutil.copytree(source_single_root, target_single_root)


def _build_mode_manifest(source_pipeline_root: Path, outputs_root: Path, mode: str) -> pd.DataFrame:
    manifest = _rewrite_manifest(source_pipeline_root, outputs_root, mode)
    target_path = _target_manifest_path(outputs_root, mode)
    target_path.parent.mkdir(parents=True, exist_ok=True)
    _write_csv(manifest, target_path)
    _copy_source_manifests(source_pipeline_root, outputs_root, mode)
    return manifest


def _filter_mode_manifest(
    manifest: pd.DataFrame,
    *,
    net_group: str | None = None,
    run_id: str | None = None,
    limit: int | None = None,
) -> pd.DataFrame:
    filtered = manifest.copy()
    if net_group:
        group_value = str(net_group).strip()
        if group_value:
            net_match = filtered.get("net_group", pd.Series(dtype=str)).astype(str) == group_value
            run_match = filtered.get("run_group", pd.Series(dtype=str)).astype(str) == group_value
            filtered = filtered[net_match | run_match].copy()
    if run_id:
        filtered = filtered[filtered["run_id"].astype(str) == str(run_id)].copy()
        if filtered.empty:
            raise ValueError(f"run_id not found after filters: {run_id}")
    if limit is not None:
        if limit <= 0:
            raise ValueError("--limit must be positive")
        filtered = filtered.head(int(limit)).copy()
    return filtered


def _is_successful_final_run(output_dir: Path) -> bool:
    result_path = output_dir / SIMULATION_RESULT_NAME
    benchmark_path = output_dir / "benchmark_timing.json"
    if not result_path.exists() or not benchmark_path.exists():
        return False
    payload = _load_json(benchmark_path)
    if payload.get("run_success") is not True:
        return False
    recorded_duration = payload.get("sim_duration")
    if recorded_duration is not None:
        try:
            if int(float(recorded_duration)) != MODE_CONFIGS["final"].sim_duration:
                return False
        except (ValueError, TypeError):
            return False
    return True


def _verify_input_manifest(manifest: pd.DataFrame) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    for column in ("candidate_csv", "net_file"):
        values = sorted(set(str(v).strip() for v in manifest.get(column, pd.Series(dtype=str)).tolist()))
        for value in values:
            path = _resolve_project_path(value)
            checks.append(
                {
                    "artifact": column,
                    "path": str(path),
                    "exists": path.exists(),
                }
            )
    return checks


def _verify_mode(source_pipeline_root: Path, outputs_root: Path, mode: str) -> dict[str, Any]:
    manifest = _build_mode_manifest(source_pipeline_root, outputs_root, mode)
    checks = _verify_input_manifest(manifest)
    missing = [row for row in checks if not row["exists"]]
    paths = _target_mode_paths(outputs_root, mode)
    return {
        "mode": mode,
        "output_root": str(_mode_root(outputs_root, mode)),
        "paths": {name: str(path) for name, path in paths.items()},
        "run_count": int(len(manifest)),
        "unique_run_groups": sorted(set(manifest["run_group"].astype(str))) if not manifest.empty else [],
        "missing_input_count": int(len(missing)),
        "missing_inputs": missing,
    }


def _run_command_for_row(row: pd.Series, mode: str, log_handle: Any) -> list[str]:
    config = MODE_CONFIGS[mode]
    return [
        sys.executable,
        "-m",
        "smart_crosswalk_sumo.run_sampled10_group",
        "--candidate-csv",
        str(_resolve_project_path(str(row["candidate_csv"]))),
        "--net-file",
        str(_resolve_project_path(str(row["net_file"]))),
        "--seed",
        str(int(row["seed"])),
        "--output-dir",
        str(_resolve_project_path(str(row["work_output_dir"]))),
        "--sim-duration",
        str(config.sim_duration),
        "--warmup",
        "0",
        "--traci_step_length",
        "0.1",
        "--traffic_measure_radius_m",
        "500.0",
        "--extension_increment",
        "5.0",
        "--max_extensions",
        "1",
        "--metric-sample-interval",
        "10",
        "--vehicle-sample-interval",
        "10",
        "--progress-interval",
        "60",
        "--phase-aligned-ped-depart",
        "--ped-repeat-count",
        "5",
        "--ped-repeat-spacing-sec",
        "2",
        "--include-vehicles",
        "--manifest-row-role",
        str(row["manifest_row_role"]),
        "--manifest-crosswalk-id",
        str(row["crosswalk_id"]),
        "--scenario-name",
        "main_realistic_stress",
    ]


def _finalize_run_artifacts(row: pd.Series) -> None:
    work_dir = _resolve_project_path(str(row["work_output_dir"]))
    final_dir = _resolve_project_path(str(row["output_dir"]))
    final_dir.mkdir(parents=True, exist_ok=True)
    source_result = work_dir / SIMULATION_RESULT_NAME
    if not source_result.exists():
        source_result = work_dir / "simulation_results_seed.csv"
    shutil.copy2(source_result, final_dir / SIMULATION_RESULT_NAME)
    for name in ("benchmark_timing.json", "run_metadata.json"):
        src = work_dir / name
        if src.exists():
            shutil.copy2(src, final_dir / name)


def _execute_mode(
    source_pipeline_root: Path,
    outputs_root: Path,
    mode: str,
    dry_run: bool,
    net_group: str | None = None,
    limit: int | None = None,
    run_id: str | None = None,
) -> dict[str, Any]:
    manifest = _build_mode_manifest(source_pipeline_root, outputs_root, mode)
    manifest = _filter_mode_manifest(manifest, net_group=net_group, run_id=run_id, limit=limit)
    target_path = _target_manifest_path(outputs_root, mode)
    target_path.parent.mkdir(parents=True, exist_ok=True)
    _write_csv(manifest, target_path)
    executed = 0
    skipped = 0
    pending_commands: list[dict[str, Any]] = []
    for _, row in manifest.iterrows():
        final_dir = _resolve_project_path(str(row["output_dir"]))
        log_file = _resolve_project_path(str(row["log_file"]))
        work_dir = _resolve_project_path(str(row["work_output_dir"]))
        log_file.parent.mkdir(parents=True, exist_ok=True)
        work_dir.parent.mkdir(parents=True, exist_ok=True)
        command = _run_command_for_row(row, mode, None)
        already_successful = _is_successful_final_run(final_dir)
        if dry_run:
            pending_commands.append(
                {
                    "run_id": str(row["run_id"]),
                    "scenario": str(row["scenario"]),
                    "seed": int(row["seed"]),
                    "output_dir": str(final_dir),
                    "would_skip_existing_success": already_successful,
                    "command": command,
                }
            )
            continue
        if already_successful:
            skipped += 1
            continue
        with log_file.open("a", encoding="utf-8") as log_handle:
            proc = subprocess.run(
                command,
                cwd=PROJECT_ROOT,
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                check=False,
            )
        if proc.returncode != 0:
            raise RuntimeError(f"run failed: {row['run_id']} (see {log_file})")
        if not (work_dir / SIMULATION_RESULT_NAME).exists() and not (work_dir / "simulation_results_seed.csv").exists():
            raise FileNotFoundError(work_dir / SIMULATION_RESULT_NAME)
        benchmark = _load_json(work_dir / "benchmark_timing.json")
        if benchmark.get("run_success") is not True:
            raise RuntimeError(f"run incomplete: {row['run_id']} (see {work_dir / 'benchmark_timing.json'})")
        _finalize_run_artifacts(row)
        executed += 1
    return {
        "mode": mode,
        "output_root": str(_mode_root(outputs_root, mode)),
        "planned_runs": int(len(manifest)),
        "skipped_runs": skipped,
        "executed_runs": executed,
        "pending_runs": int(len(pending_commands)),
        "pending_preview": pending_commands[:5],
    }


def _seed_mean_df(run_level_df: pd.DataFrame) -> pd.DataFrame:
    if run_level_df.empty:
        return pd.DataFrame(
            columns=[
                "run_group",
                "net_group",
                "crosswalk_id",
                "scenario",
                "seed_count",
                "elapsed_sec_mean",
                "extension_count_mean",
                "ped_crossing_person_count_mean",
                "veh_delay_mean_mean",
                "high_risk_event_count_mean",
            ]
        )
    rows: list[dict[str, Any]] = []
    numeric_cols = ["elapsed_sec", "extension_count", "ped_crossing_person_count", "veh_delay_mean", "high_risk_event_count"]
    for keys, sub in run_level_df.groupby(["run_group", "net_group", "crosswalk_id", "scenario"], dropna=False):
        run_group, net_group, crosswalk_id, scenario = keys
        row = {
            "run_group": run_group,
            "net_group": net_group,
            "crosswalk_id": crosswalk_id,
            "scenario": scenario,
            "seed_count": int(pd.to_numeric(sub["seed"], errors="coerce").dropna().nunique()) if "seed" in sub.columns else 0,
        }
        for col in numeric_cols:
            raw = sub[col] if col in sub.columns else pd.Series(dtype=float)
            if not isinstance(raw, pd.Series):
                raw = pd.Series([raw])
            values = pd.to_numeric(raw, errors="coerce").dropna()
            row[f"{col}_mean"] = float(values.mean()) if not values.empty else pd.NA
        rows.append(row)
    return pd.DataFrame(rows)


def _comparison_mean_df(comparison_df: pd.DataFrame) -> pd.DataFrame:
    columns = [
        "run_group",
        "net_group",
        "crosswalk_id",
        "ready_seed_count",
        "baseline_elapsed_sec_mean",
        "smart_elapsed_sec_mean",
        "elapsed_sec_delta_mean",
        "baseline_extension_count_mean",
        "smart_extension_count_mean",
        "extension_count_delta_mean",
        "baseline_ped_count_mean",
        "smart_ped_count_mean",
        "ped_count_delta_mean",
        "baseline_vehicle_delay_mean_mean",
        "smart_vehicle_delay_mean_mean",
        "vehicle_delay_mean_delta_mean",
        "baseline_high_risk_event_count_mean",
        "smart_high_risk_event_count_mean",
        "high_risk_event_count_delta_mean",
    ]
    if comparison_df.empty:
        return pd.DataFrame(columns=columns)
    ready = comparison_df[comparison_df["comparison_status"] == "ready"].copy()
    if ready.empty:
        return pd.DataFrame(columns=columns)
    rows: list[dict[str, Any]] = []
    for keys, sub in ready.groupby(["run_group", "net_group", "crosswalk_id"], dropna=False):
        run_group, net_group, crosswalk_id = keys
        def _mean(col: str) -> float | pd._libs.missing.NAType:
            values = pd.to_numeric(sub.get(col), errors="coerce").dropna()
            return float(values.mean()) if not values.empty else pd.NA
        rows.append(
            {
                "run_group": run_group,
                "net_group": net_group,
                "crosswalk_id": crosswalk_id,
                "ready_seed_count": int(pd.to_numeric(sub["seed"], errors="coerce").dropna().nunique()),
                "baseline_elapsed_sec_mean": _mean("baseline_elapsed_sec"),
                "smart_elapsed_sec_mean": _mean("smart_elapsed_sec"),
                "elapsed_sec_delta_mean": _mean("smart_elapsed_sec") - _mean("baseline_elapsed_sec") if pd.notna(_mean("baseline_elapsed_sec")) and pd.notna(_mean("smart_elapsed_sec")) else pd.NA,
                "baseline_extension_count_mean": _mean("baseline_extension_count"),
                "smart_extension_count_mean": _mean("smart_extension_count"),
                "extension_count_delta_mean": _mean("smart_extension_count") - _mean("baseline_extension_count") if pd.notna(_mean("baseline_extension_count")) and pd.notna(_mean("smart_extension_count")) else pd.NA,
                "baseline_ped_count_mean": _mean("baseline_ped_count"),
                "smart_ped_count_mean": _mean("smart_ped_count"),
                "ped_count_delta_mean": _mean("smart_ped_count") - _mean("baseline_ped_count") if pd.notna(_mean("baseline_ped_count")) and pd.notna(_mean("smart_ped_count")) else pd.NA,
                "baseline_vehicle_delay_mean_mean": _mean("baseline_vehicle_delay_mean"),
                "smart_vehicle_delay_mean_mean": _mean("smart_vehicle_delay_mean"),
                "vehicle_delay_mean_delta_mean": _mean("smart_vehicle_delay_mean") - _mean("baseline_vehicle_delay_mean") if pd.notna(_mean("baseline_vehicle_delay_mean")) and pd.notna(_mean("smart_vehicle_delay_mean")) else pd.NA,
                "baseline_high_risk_event_count_mean": _mean("baseline_high_risk_event_count"),
                "smart_high_risk_event_count_mean": _mean("smart_high_risk_event_count"),
                "high_risk_event_count_delta_mean": _mean("smart_high_risk_event_count") - _mean("baseline_high_risk_event_count") if pd.notna(_mean("baseline_high_risk_event_count")) and pd.notna(_mean("smart_high_risk_event_count")) else pd.NA,
            }
        )
    return pd.DataFrame(rows, columns=columns)


def _safe_str(value: Any) -> str:
    if value is None:
        return ""
    try:
        if pd.isna(value):
            return ""
    except Exception:
        pass
    return str(value).strip()


def _safe_float(value: Any) -> float | None:
    if value is None or pd.isna(value):
        return None
    try:
        return float(value)
    except Exception:
        return None


def _safe_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None or pd.isna(value):
        return False
    text = str(value).strip().lower()
    return text in {"1", "true", "yes", "y"}


def _sha256_file(path: Path) -> str:
    if not path.exists():
        return ""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_candidate_catalog(outputs_root: Path) -> pd.DataFrame:
    manifest_root = _target_mode_paths(outputs_root, "final")["manifests"]
    frames: list[pd.DataFrame] = []
    meta = _read_csv(manifest_root / "candidate_metadata.csv")
    if not meta.empty:
        frames.append(meta.copy())
    for path in sorted(manifest_root.glob("*_candidates.csv")):
        if path.name == "candidate_metadata.csv":
            continue
        df = _read_csv(path)
        if not df.empty:
            frames.append(df.copy())
    if not frames:
        return pd.DataFrame()
    merged = frames[0]
    for df in frames[1:]:
        cols = [col for col in df.columns if col not in merged.columns or col == "crosswalk_id"]
        merged = merged.merge(df[cols], on="crosswalk_id", how="outer") if "crosswalk_id" in df.columns and "crosswalk_id" in merged.columns else merged
    return merged.drop_duplicates(subset=["crosswalk_id"], keep="first") if "crosswalk_id" in merged.columns else merged


def _load_registry_df() -> pd.DataFrame:
    return _read_csv(PROJECT_ROOT / "smart_crosswalk_sumo" / "registry" / "junggu_crosswalk_sumo_registry.csv")


def _pedestrian_assumption_paths() -> list[Path]:
    return [
        PROJECT_ROOT / "result" / "active" / "pedestrian_assumption" / "crosswalk_pedestrian_assumptions_top50_daytime_high_1p2.csv",
        PROJECT_ROOT / "result" / "active" / "pedestrian_assumption" / "crosswalk_pedestrian_assumptions_daytime_high_1p2.csv",
    ]


def _pedestrian_assumption_copy_hints() -> list[str]:
    hints: list[str] = []
    source_root = PROJECT_ROOT / "museum" / "result" / "active" / "pedestrian_assumption"
    for target in _pedestrian_assumption_paths():
        source = source_root / target.name
        if source.exists() and not target.exists():
            hints.append(f"cp {source} {target}")
    return hints


def _parse_sumocfg_inputs(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"exists": False, "net_file": "", "route_files": []}
    try:
        root = ET.parse(path).getroot()
    except Exception:
        return {"exists": False, "net_file": "", "route_files": []}
    net_file = ""
    route_files: list[str] = []
    net_elem = root.find(".//input/net-file")
    route_elem = root.find(".//input/route-files")
    if net_elem is not None:
        net_file = _safe_str(net_elem.attrib.get("value"))
    if route_elem is not None:
        route_files = [part.strip() for part in _safe_str(route_elem.attrib.get("value")).split(",") if part.strip()]
    return {"exists": True, "net_file": net_file, "route_files": route_files}


def _pick_route_file(route_files: list[str], needle: str) -> str:
    for path in route_files:
        if needle in Path(path).name:
            return path
    return ""


def _load_demand_params_row(output_dir: Path) -> dict[str, Any]:
    for path in [output_dir / "demand_params.csv", output_dir / "csv" / "results" / "demand_params.csv"]:
        df = _read_csv(path)
        if not df.empty:
            return df.iloc[0].to_dict()
    return {}


def _build_preflight_checks(outputs_root: Path, manifest: pd.DataFrame) -> pd.DataFrame:
    checks: list[dict[str, Any]] = []
    sumo_home = resolve_sumo_home()
    checks.append(
        {
            "check_name": "python_environment",
            "status": "pass" if _safe_str(sys.executable) else "fail",
            "detail": sys.executable,
        }
    )
    checks.append(
        {
            "check_name": "SUMO_HOME",
            "status": "pass" if _safe_str(sumo_home) else "fail",
            "detail": _safe_str(sumo_home),
        }
    )
    mapping_paths = [
        PROJECT_ROOT / "smart_crosswalk_sumo" / "registry" / "junggu_crosswalk_sumo_registry.csv",
        PROJECT_ROOT / "smart_crosswalk_sumo" / "network_validation" / "crosswalk_candidate_matching.csv",
        _target_mode_paths(outputs_root, "final")["manifests"] / "candidate_metadata.csv",
    ]
    for path in mapping_paths:
        checks.append(
            {
                "check_name": f"mapping_csv::{path.name}",
                "status": "pass" if path.exists() else "fail",
                "detail": str(path),
            }
        )
    pedestrian_assumption_candidates = _pedestrian_assumption_paths()
    checks.append(
        {
            "check_name": "pedestrian_assumption::any_candidate_exists",
            "status": "pass" if any(path.exists() for path in pedestrian_assumption_candidates) else "fail",
            "detail": ";".join(str(path) for path in pedestrian_assumption_candidates),
            "suggested_cp_commands": ";".join(_pedestrian_assumption_copy_hints()),
        }
    )
    if not manifest.empty:
        for value in sorted(set(manifest.get("net_file", pd.Series(dtype=str)).astype(str).tolist())):
            path = _resolve_project_path(value)
            checks.append(
                {
                    "check_name": f"net::{Path(value).name}",
                    "status": "pass" if path.exists() else "fail",
                    "detail": str(path),
                }
            )
        for _, row in manifest[manifest.get("scenario", pd.Series(dtype=str)).astype(str) == "smart"].iterrows():
            work_dir = Path(_safe_str(row.get("work_output_dir")) or _safe_str(row.get("output_dir")))
            for name in ["phase6_smoke_baseline.sumocfg", "phase6_smoke_smart.sumocfg"]:
                path = work_dir / name
                checks.append(
                    {
                        "check_name": f"sumocfg::{row.get('run_id')}::{name}",
                        "status": "pass" if (not work_dir.exists() or path.exists()) else "fail",
                        "detail": str(path),
                    }
                )
    return pd.DataFrame(checks)


def _pair_seed_audit_df(outputs_root: Path, manifest: pd.DataFrame, run_level_df: pd.DataFrame) -> pd.DataFrame:
    candidate_df = _load_candidate_catalog(outputs_root)
    registry_df = _load_registry_df()
    candidate_lookup = candidate_df.set_index("crosswalk_id", drop=False) if not candidate_df.empty and "crosswalk_id" in candidate_df.columns else pd.DataFrame()
    registry_lookup = registry_df.set_index("crosswalk_id", drop=False) if not registry_df.empty and "crosswalk_id" in registry_df.columns else pd.DataFrame()
    rows: list[dict[str, Any]] = []
    smart_manifest = manifest[manifest["scenario"].astype(str) == "smart"].copy() if not manifest.empty else pd.DataFrame()
    for _, manifest_row in smart_manifest.iterrows():
        run_id = _safe_str(manifest_row.get("run_id"))
        crosswalk_id = _safe_str(manifest_row.get("crosswalk_id"))
        seed = int(_safe_float(manifest_row.get("seed")) or 0)
        run_rows = run_level_df[run_level_df["manifest_run_id"].astype(str) == run_id].copy() if not run_level_df.empty and "manifest_run_id" in run_level_df.columns else pd.DataFrame()
        baseline_row = run_rows[run_rows["scenario"].astype(str) == "baseline"].head(1)
        smart_row = run_rows[run_rows["scenario"].astype(str) == "smart"].head(1)
        baseline_series = baseline_row.iloc[0] if not baseline_row.empty else pd.Series(dtype=object)
        smart_series = smart_row.iloc[0] if not smart_row.empty else pd.Series(dtype=object)
        output_dir = _resolve_project_path(_safe_str(manifest_row.get("work_output_dir")) or _safe_str(manifest_row.get("output_dir")))
        result_path = output_dir / SIMULATION_RESULT_NAME
        demand_row = _load_demand_params_row(output_dir)
        baseline_cfg = _parse_sumocfg_inputs(output_dir / "phase6_smoke_baseline.sumocfg")
        smart_cfg = _parse_sumocfg_inputs(output_dir / "phase6_smoke_smart.sumocfg")
        baseline_vehicle_route = _pick_route_file(baseline_cfg.get("route_files", []), "demand_vehicle")
        smart_vehicle_route = _pick_route_file(smart_cfg.get("route_files", []), "demand_vehicle")
        baseline_ped_route = _pick_route_file(baseline_cfg.get("route_files", []), "demand_pedestrian")
        smart_ped_route = _pick_route_file(smart_cfg.get("route_files", []), "demand_pedestrian")
        vehicle_route_hash_match = bool(
            baseline_vehicle_route and smart_vehicle_route and _sha256_file(Path(baseline_vehicle_route)) == _sha256_file(Path(smart_vehicle_route))
        )
        pedestrian_route_hash_match = bool(
            baseline_ped_route and smart_ped_route and _sha256_file(Path(baseline_ped_route)) == _sha256_file(Path(smart_ped_route))
        )
        meta = candidate_lookup.loc[crosswalk_id] if not candidate_lookup.empty and crosswalk_id in candidate_lookup.index else pd.Series(dtype=object)
        reg = registry_lookup.loc[crosswalk_id] if not registry_lookup.empty and crosswalk_id in registry_lookup.index else pd.Series(dtype=object)
        mapping_distance_tier = _safe_str(meta.get("mapping_distance_tier") if not meta.empty else "")
        if not mapping_distance_tier:
            mapping_distance_tier = _safe_str(reg.get("mapping_distance_tier") if not reg.empty else "")
        use_real_demand = True
        fail_reasons: list[str] = []
        warning_reasons: list[str] = []
        exclude_reasons: list[str] = []
        if mapping_distance_tier == "T5_FAIL":
            exclude_reasons.append("mapping_distance_tier=T5_FAIL")
        if not reg.empty:
            if not _safe_bool(reg.get("path_uses_crossing", True)) or not _safe_bool(reg.get("path_uses_walkingarea", True)):
                exclude_reasons.append("pedestrian_route_impossible")
            if not _safe_bool(reg.get("runnable_for_signal_extension", True)) or not _safe_str(reg.get("tls_id")):
                exclude_reasons.append("real_world_unsignalized_ox_candidate")
            if _safe_bool(reg.get("requires_patch")) and not _safe_bool(reg.get("c1_patch_survived", True)):
                exclude_reasons.append("signal_patch_breaks_other_tls")
        if not result_path.exists():
            fail_reasons.append("simulation_result_missing")
        required_metric_columns = {
            "scenario",
            "seed",
            "crosswalk_id",
            "pedestrian_crossing_count",
            "extension_count",
            "pet_available",
            "pet_unavailable_reason",
            "network_vehicle_edge_coverage_ratio",
            "local_500m_avg_delay_sec",
            "network_avg_delay_sec",
        }
        if run_rows.empty:
            fail_reasons.append("paired_run_rows_missing")
        elif not required_metric_columns.issubset(set(run_rows.columns)):
            fail_reasons.append("required_simulation_columns_missing")
        if use_real_demand and not any(path.exists() for path in _pedestrian_assumption_paths()):
            fail_reasons.append("real_demand_assumption_missing")
        vehicle_route_count = int(
            _safe_float(demand_row.get("vehicle_route_count"))
            or _safe_float(demand_row.get("generated_vehicle_route_count"))
            or 0
        )
        vehicle_demand_expected = int(
            _safe_float(demand_row.get("vehicle_demand_expected"))
            or _safe_float(demand_row.get("expected_vehicle_count_for_duration"))
            or 0
        )
        unique_vehicle_route_count = int(_safe_float(demand_row.get("unique_vehicle_route_count")) or 0)
        duplicate_factor = _safe_float(demand_row.get("duplicate_factor"))
        unique_vehicle_route_ratio = _safe_float(demand_row.get("unique_vehicle_route_ratio"))
        if unique_vehicle_route_count > 0 and vehicle_route_count > 0:
            if duplicate_factor is None:
                duplicate_factor = float(vehicle_route_count / max(unique_vehicle_route_count, 1))
            if unique_vehicle_route_ratio is None:
                unique_vehicle_route_ratio = float(unique_vehicle_route_count / max(vehicle_route_count, 1))
        pedestrian_route_count = int(_safe_float(demand_row.get("generated_pedestrian_route_count")) or 0)
        if vehicle_route_count <= 1:
            fail_reasons.append("vehicle_route_count<=1")
        else:
            if vehicle_demand_expected > 0 and vehicle_route_count < int(round(vehicle_demand_expected * 0.95)):
                fail_reasons.append("vehicle_route_count_below_expected")
            if unique_vehicle_route_count <= 1:
                fail_reasons.append("unique_vehicle_route_count<=1")
            elif unique_vehicle_route_ratio is not None and unique_vehicle_route_ratio < 0.05:
                fail_reasons.append("unique_vehicle_route_ratio<0.05")
            elif unique_vehicle_route_ratio is not None and unique_vehicle_route_ratio < 0.20:
                warning_reasons.append("unique_vehicle_route_ratio<0.20")
        if duplicate_factor is not None and duplicate_factor > 20.0:
            fail_reasons.append("duplicate_factor>20")
        elif duplicate_factor is not None and duplicate_factor > 5.0:
            warning_reasons.append("duplicate_factor>5")
        if pedestrian_route_count <= 1:
            fail_reasons.append("pedestrian_route_count<=1")
        if baseline_vehicle_route or smart_vehicle_route:
            if not vehicle_route_hash_match:
                fail_reasons.append("baseline_smart_vehicle_route_hash_mismatch")
        else:
            fail_reasons.append("vehicle_sumocfg_missing")
        if baseline_ped_route or smart_ped_route:
            if not pedestrian_route_hash_match:
                fail_reasons.append("baseline_smart_pedestrian_route_hash_mismatch")
        else:
            fail_reasons.append("pedestrian_sumocfg_missing")
        baseline_ped = _safe_float(baseline_series.get("pedestrian_crossing_count")) or _safe_float(baseline_series.get("ped_crossing_person_count")) or 0.0
        smart_ped = _safe_float(smart_series.get("pedestrian_crossing_count")) or _safe_float(smart_series.get("ped_crossing_person_count")) or 0.0
        if baseline_ped <= 0:
            fail_reasons.append("baseline_candidate_crossing_has_no_pedestrian")
        if baseline_ped <= 0 and smart_ped > 0:
            fail_reasons.append("smart_only_pedestrian_presence")
        baseline_ext = _safe_float(baseline_series.get("extension_count")) or 0.0
        smart_comp_ext = _safe_float(smart_series.get("vehicle_green_compensation_extension_count"))
        if baseline_ext != 0:
            fail_reasons.append("baseline_extension_count_nonzero")
        if smart_comp_ext is not None and smart_comp_ext > 0:
            fail_reasons.append("smart_vehicle_green_compensation_extension_detected")
        if mapping_distance_tier in {"T3_REVIEW", "T4_WEAK"}:
            warning_reasons.append(f"mapping_distance_tier={mapping_distance_tier}")
        pet_source = _safe_str(smart_series.get("pet_source"))
        pet_available = _safe_bool(smart_series.get("pet_available"))
        pet_coverage_ratio = _safe_float(smart_series.get("pet_coverage_ratio"))
        pet_unavailable_reason = _safe_str(smart_series.get("pet_unavailable_reason"))
        if pet_source != "ssm_pet":
            warning_reasons.append("ssm_pet_unsupported")
            if pet_source == "local_watcher":
                warning_reasons.append("local_watcher_fallback")
        if not pet_available and pet_unavailable_reason:
            warning_reasons.append(f"pet_quality:{pet_unavailable_reason}")
        if (_safe_float(smart_series.get("low_pet_event_count")) or 0.0) <= 0:
            warning_reasons.append("no_low_pet_event")
        coverage = _safe_float(smart_series.get("network_vehicle_edge_coverage_ratio"))
        if coverage is not None and coverage < 0.01:
            warning_reasons.append("vehicle_edge_coverage_low")
        if smart_ped <= 1:
            warning_reasons.append("low_exposure")
        if _safe_float(smart_series.get("low_pet_per_100_conflict_candidates")) is None:
            warning_reasons.append("no_conflict_candidate")
        if _safe_bool(reg.get("requires_patch")):
            warning_reasons.append("OX_signal_patch_candidate")
        seed_status = "pass"
        if exclude_reasons:
            seed_status = "exclude"
        elif fail_reasons:
            seed_status = "fail"
        elif warning_reasons:
            seed_status = "warning"
        rows.append(
            {
                "run_group": _safe_str(manifest_row.get("run_group")),
                "net_group": _safe_str(manifest_row.get("net_group")),
                "crosswalk_id": crosswalk_id,
                "seed": seed,
                "admin_dong": _safe_str(meta.get("admin_dong") if not meta.empty else ""),
                "dong_name": _safe_str(meta.get("dong_name") if not meta.empty else ""),
                "mapping_distance_tier": mapping_distance_tier,
                "registry_status": _safe_str(reg.get("registry_status") if not reg.empty else ""),
                "implementation_status": _safe_str(reg.get("implementation_status") if not reg.empty else ""),
                "control_mode": _safe_str(reg.get("control_mode") if not reg.empty else ""),
                "result_status": seed_status,
                "result_reason": ";".join(dict.fromkeys(exclude_reasons or fail_reasons or warning_reasons)),
                "exclude_reason": ";".join(dict.fromkeys(exclude_reasons)),
                "fail_reason": ";".join(dict.fromkeys(fail_reasons)),
                "warning_reason": ";".join(dict.fromkeys(warning_reasons)),
                "simulation_result_exists": result_path.exists(),
                "vehicle_demand_expected": vehicle_demand_expected,
                "vehicle_route_count": vehicle_route_count,
                "unique_vehicle_route_count": unique_vehicle_route_count,
                "duplicate_factor": duplicate_factor,
                "unique_vehicle_route_ratio": unique_vehicle_route_ratio,
                "pedestrian_route_count": pedestrian_route_count,
                "vehicle_route_hash": _safe_str(demand_row.get("route_file_sha256")) or _sha256_file(Path(baseline_vehicle_route)) if baseline_vehicle_route else "",
                "pedestrian_route_hash": _safe_str(demand_row.get("ped_file_sha256")) or _sha256_file(Path(baseline_ped_route)) if baseline_ped_route else "",
                "vehicle_route_hash_match": vehicle_route_hash_match,
                "pedestrian_route_hash_match": pedestrian_route_hash_match,
                "baseline_pedestrian_crossing_count": baseline_ped,
                "smart_pedestrian_crossing_count": smart_ped,
                "baseline_extension_count": baseline_ext,
                "smart_extension_count": _safe_float(smart_series.get("extension_count")),
                "baseline_network_avg_delay_sec": _safe_float(baseline_series.get("network_avg_delay_sec")),
                "smart_network_avg_delay_sec": _safe_float(smart_series.get("network_avg_delay_sec")),
                "baseline_network_mean_time_loss": _safe_float(baseline_series.get("network_mean_time_loss")),
                "smart_network_mean_time_loss": _safe_float(smart_series.get("network_mean_time_loss")),
                "baseline_local_500m_avg_delay_sec": _safe_float(baseline_series.get("local_500m_avg_delay_sec")),
                "smart_local_500m_avg_delay_sec": _safe_float(smart_series.get("local_500m_avg_delay_sec")),
                "baseline_network_vehicle_edge_coverage_ratio": _safe_float(baseline_series.get("network_vehicle_edge_coverage_ratio")),
                "smart_network_vehicle_edge_coverage_ratio": _safe_float(smart_series.get("network_vehicle_edge_coverage_ratio")),
                "baseline_low_pet_per_100_crossings": _safe_float(baseline_series.get("low_pet_per_100_crossings")),
                "smart_low_pet_per_100_crossings": _safe_float(smart_series.get("low_pet_per_100_crossings")),
                "baseline_accident_risk_estimate": _safe_float(baseline_series.get("accident_risk_estimate")),
                "smart_accident_risk_estimate": _safe_float(smart_series.get("accident_risk_estimate")),
                "baseline_pet_available": _safe_bool(baseline_series.get("pet_available")),
                "smart_pet_available": pet_available,
                "smart_pet_coverage_ratio": pet_coverage_ratio,
                "baseline_pet_unavailable_reason": _safe_str(baseline_series.get("pet_unavailable_reason")),
                "smart_pet_unavailable_reason": pet_unavailable_reason,
                "smart_pet_source": pet_source,
                "use_real_demand": use_real_demand,
            }
        )
    return pd.DataFrame(rows)


def _mean_numeric(series: pd.Series) -> float | pd.NA:
    values = pd.to_numeric(series, errors="coerce").dropna()
    return float(values.mean()) if not values.empty else pd.NA


def _final_candidate_summary_df(pair_df: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    if pair_df.empty:
        return pd.DataFrame()
    for keys, sub in pair_df.groupby(["run_group", "net_group", "crosswalk_id"], dropna=False):
        run_group, net_group, crosswalk_id = keys
        usable = sub[sub["result_status"].isin(["pass", "warning"])].copy()
        statuses = sub["result_status"].astype(str).tolist()
        result_status = "pass"
        if "exclude" in statuses:
            result_status = "exclude"
        elif "fail" in statuses:
            result_status = "fail"
        elif "warning" in statuses:
            result_status = "warning"
        reasons = []
        for col in ["exclude_reason", "fail_reason", "warning_reason"]:
            for value in sub.get(col, pd.Series(dtype=str)).fillna("").astype(str):
                if value:
                    reasons.extend([part for part in value.split(";") if part])
        row = {
            "run_group": run_group,
            "net_group": net_group,
            "crosswalk_id": crosswalk_id,
            "admin_dong": _safe_str(sub["admin_dong"].dropna().astype(str).iloc[0]) if "admin_dong" in sub.columns and not sub["admin_dong"].dropna().empty else "",
            "dong_name": _safe_str(sub["dong_name"].dropna().astype(str).iloc[0]) if "dong_name" in sub.columns and not sub["dong_name"].dropna().empty else "",
            "mapping_distance_tier": _safe_str(sub["mapping_distance_tier"].dropna().astype(str).iloc[0]) if "mapping_distance_tier" in sub.columns and not sub["mapping_distance_tier"].dropna().empty else "",
            "registry_status": _safe_str(sub["registry_status"].dropna().astype(str).iloc[0]) if "registry_status" in sub.columns and not sub["registry_status"].dropna().empty else "",
            "implementation_status": _safe_str(sub["implementation_status"].dropna().astype(str).iloc[0]) if "implementation_status" in sub.columns and not sub["implementation_status"].dropna().empty else "",
            "control_mode": _safe_str(sub["control_mode"].dropna().astype(str).iloc[0]) if "control_mode" in sub.columns and not sub["control_mode"].dropna().empty else "",
            "planned_seed_count": int(len(sub)),
            "usable_seed_count": int(len(usable)),
            "pass_seed_count": int((sub["result_status"] == "pass").sum()),
            "warning_seed_count": int((sub["result_status"] == "warning").sum()),
            "fail_seed_count": int((sub["result_status"] == "fail").sum()),
            "exclude_seed_count": int((sub["result_status"] == "exclude").sum()),
            "result_status": result_status,
            "result_reason": ";".join(dict.fromkeys(reasons)),
            "vehicle_route_hash": _safe_str(sub["vehicle_route_hash"].dropna().astype(str).iloc[0]) if "vehicle_route_hash" in sub.columns and not sub["vehicle_route_hash"].dropna().empty else "",
            "pedestrian_route_hash": _safe_str(sub["pedestrian_route_hash"].dropna().astype(str).iloc[0]) if "pedestrian_route_hash" in sub.columns and not sub["pedestrian_route_hash"].dropna().empty else "",
            "vehicle_route_hash_unique_count": int(sub["vehicle_route_hash"].fillna("").astype(str).replace("", pd.NA).dropna().nunique()) if "vehicle_route_hash" in sub.columns else 0,
            "pedestrian_route_hash_unique_count": int(sub["pedestrian_route_hash"].fillna("").astype(str).replace("", pd.NA).dropna().nunique()) if "pedestrian_route_hash" in sub.columns else 0,
            "vehicle_demand_expected_mean": _mean_numeric(sub.get("vehicle_demand_expected", pd.Series(dtype=float))),
            "vehicle_route_count_mean": _mean_numeric(sub.get("vehicle_route_count", pd.Series(dtype=float))),
            "unique_vehicle_route_count_mean": _mean_numeric(sub.get("unique_vehicle_route_count", pd.Series(dtype=float))),
            "duplicate_factor_mean": _mean_numeric(sub.get("duplicate_factor", pd.Series(dtype=float))),
            "unique_vehicle_route_ratio_mean": _mean_numeric(sub.get("unique_vehicle_route_ratio", pd.Series(dtype=float))),
            "baseline_pedestrian_crossing_count_mean": _mean_numeric(usable.get("baseline_pedestrian_crossing_count", pd.Series(dtype=float))),
            "smart_pedestrian_crossing_count_mean": _mean_numeric(usable.get("smart_pedestrian_crossing_count", pd.Series(dtype=float))),
            "baseline_extension_count_mean": _mean_numeric(usable.get("baseline_extension_count", pd.Series(dtype=float))),
            "smart_extension_count_mean": _mean_numeric(usable.get("smart_extension_count", pd.Series(dtype=float))),
            "baseline_network_avg_delay_sec_mean": _mean_numeric(usable.get("baseline_network_avg_delay_sec", pd.Series(dtype=float))),
            "smart_network_avg_delay_sec_mean": _mean_numeric(usable.get("smart_network_avg_delay_sec", pd.Series(dtype=float))),
            "baseline_network_mean_time_loss_mean": _mean_numeric(usable.get("baseline_network_mean_time_loss", pd.Series(dtype=float))),
            "smart_network_mean_time_loss_mean": _mean_numeric(usable.get("smart_network_mean_time_loss", pd.Series(dtype=float))),
            "baseline_local_500m_avg_delay_sec_mean": _mean_numeric(usable.get("baseline_local_500m_avg_delay_sec", pd.Series(dtype=float))),
            "smart_local_500m_avg_delay_sec_mean": _mean_numeric(usable.get("smart_local_500m_avg_delay_sec", pd.Series(dtype=float))),
            "baseline_network_vehicle_edge_coverage_ratio_mean": _mean_numeric(usable.get("baseline_network_vehicle_edge_coverage_ratio", pd.Series(dtype=float))),
            "smart_network_vehicle_edge_coverage_ratio_mean": _mean_numeric(usable.get("smart_network_vehicle_edge_coverage_ratio", pd.Series(dtype=float))),
            "baseline_low_pet_per_100_crossings_mean": _mean_numeric(usable.get("baseline_low_pet_per_100_crossings", pd.Series(dtype=float))),
            "smart_low_pet_per_100_crossings_mean": _mean_numeric(usable.get("smart_low_pet_per_100_crossings", pd.Series(dtype=float))),
            "baseline_accident_risk_estimate_mean": _mean_numeric(usable.get("baseline_accident_risk_estimate", pd.Series(dtype=float))),
            "smart_accident_risk_estimate_mean": _mean_numeric(usable.get("smart_accident_risk_estimate", pd.Series(dtype=float))),
            "smart_pet_source_mode": _safe_str(usable["smart_pet_source"].mode(dropna=True).iloc[0]) if "smart_pet_source" in usable.columns and not usable["smart_pet_source"].dropna().empty else "",
            "smart_pet_available_all_usable_seeds": bool(usable["smart_pet_available"].fillna(False).astype(bool).all()) if not usable.empty and "smart_pet_available" in usable.columns else False,
            "smart_pet_coverage_ratio_mean": _mean_numeric(usable.get("smart_pet_coverage_ratio", pd.Series(dtype=float))),
            "smart_pet_unavailable_reason_mode": _safe_str(sub["smart_pet_unavailable_reason"].replace("", pd.NA).dropna().mode().iloc[0]) if "smart_pet_unavailable_reason" in sub.columns and not sub["smart_pet_unavailable_reason"].replace("", pd.NA).dropna().empty else "",
        }
        for base_col, smart_col, delta_col in [
            ("baseline_network_avg_delay_sec_mean", "smart_network_avg_delay_sec_mean", "network_avg_delay_sec_delta_mean"),
            ("baseline_network_mean_time_loss_mean", "smart_network_mean_time_loss_mean", "network_mean_time_loss_delta_mean"),
            ("baseline_local_500m_avg_delay_sec_mean", "smart_local_500m_avg_delay_sec_mean", "local_500m_avg_delay_sec_delta_mean"),
            ("baseline_low_pet_per_100_crossings_mean", "smart_low_pet_per_100_crossings_mean", "low_pet_per_100_crossings_delta_mean"),
            ("baseline_accident_risk_estimate_mean", "smart_accident_risk_estimate_mean", "accident_risk_estimate_delta_mean"),
        ]:
            base_val = _safe_float(row.get(base_col))
            smart_val = _safe_float(row.get(smart_col))
            row[delta_col] = (smart_val - base_val) if base_val is not None and smart_val is not None else pd.NA
        rows.append(row)
    return pd.DataFrame(rows)


def _final_summary_public_df(candidate_df: pd.DataFrame) -> pd.DataFrame:
    keep = [
        "run_group",
        "net_group",
        "crosswalk_id",
        "admin_dong",
        "dong_name",
        "mapping_distance_tier",
        "registry_status",
        "implementation_status",
        "control_mode",
        "planned_seed_count",
        "usable_seed_count",
        "pass_seed_count",
        "warning_seed_count",
        "fail_seed_count",
        "exclude_seed_count",
        "result_status",
        "result_reason",
        "vehicle_route_hash_unique_count",
        "pedestrian_route_hash_unique_count",
        "baseline_pedestrian_crossing_count_mean",
        "smart_pedestrian_crossing_count_mean",
        "baseline_extension_count_mean",
        "smart_extension_count_mean",
        "baseline_network_avg_delay_sec_mean",
        "smart_network_avg_delay_sec_mean",
        "network_avg_delay_sec_delta_mean",
        "baseline_network_mean_time_loss_mean",
        "smart_network_mean_time_loss_mean",
        "network_mean_time_loss_delta_mean",
        "baseline_local_500m_avg_delay_sec_mean",
        "smart_local_500m_avg_delay_sec_mean",
        "local_500m_avg_delay_sec_delta_mean",
        "baseline_network_vehicle_edge_coverage_ratio_mean",
        "smart_network_vehicle_edge_coverage_ratio_mean",
        "baseline_low_pet_per_100_crossings_mean",
        "smart_low_pet_per_100_crossings_mean",
        "low_pet_per_100_crossings_delta_mean",
        "baseline_accident_risk_estimate_mean",
        "smart_accident_risk_estimate_mean",
        "accident_risk_estimate_delta_mean",
        "smart_pet_source_mode",
        "smart_pet_available_all_usable_seeds",
        "smart_pet_coverage_ratio_mean",
        "smart_pet_unavailable_reason_mode",
    ]
    cols = [col for col in keep if col in candidate_df.columns]
    return candidate_df[cols].copy()


def _aggregate_final(outputs_root: Path) -> dict[str, Any]:
    final_root = _mode_root(outputs_root, "final")
    manifest = _read_csv(_target_manifest_path(outputs_root, "final"))
    if manifest.empty:
        raise FileNotFoundError(_target_manifest_path(outputs_root, "final"))
    preflight_df = _build_preflight_checks(outputs_root, manifest)
    if not preflight_df.empty and (preflight_df["status"] == "fail").any():
        audit_dir = final_root / "audit"
        _write_csv(preflight_df, audit_dir / "preflight_checks.csv")
        raise RuntimeError("aggregate preflight failed")
    run_level_df = agg30._build_run_level_results(manifest)
    completed_mask = run_level_df.get("run_success", pd.Series(dtype=bool)).astype(bool) if not run_level_df.empty else pd.Series(dtype=bool)
    if not completed_mask.any():
        raise RuntimeError(f"aggregate aborted: no final runs have completed successfully ({len(manifest)} planned, 0 succeeded)")
    comparison_df, metric_df = agg30._comparison_rows(run_level_df, manifest)
    seed_mean_df = _seed_mean_df(run_level_df)
    baseline_vs_smart_mean_df = _comparison_mean_df(comparison_df)
    pair_df = _pair_seed_audit_df(outputs_root, manifest, run_level_df)
    final_candidate_df = _final_candidate_summary_df(pair_df)
    final_summary_df = _final_summary_public_df(final_candidate_df)
    audit_dir = final_root / "audit"
    if final_summary_df.empty:
        raise RuntimeError("final_summary generation failed: no candidate rows")
    _write_csv(final_summary_df, _final_summary_path(outputs_root))
    _write_csv(preflight_df, audit_dir / "preflight_checks.csv")
    _write_csv(run_level_df, audit_dir / "run_level_results.csv")
    _write_csv(comparison_df, audit_dir / "comparison_by_seed.csv")
    _write_csv(metric_df, audit_dir / "metric_summary.csv")
    _write_csv(seed_mean_df, audit_dir / "seed_mean.csv")
    _write_csv(baseline_vs_smart_mean_df, audit_dir / "baseline_vs_smart_mean.csv")
    _write_csv(pair_df, audit_dir / "final_pair_seed_audit.csv")
    _write_csv(final_candidate_df, audit_dir / "final_candidate_summary_full.csv")
    return {
        "final_summary": str(_final_summary_path(outputs_root)),
        "row_count": int(len(final_summary_df)),
        "ready_seed_rows": int((comparison_df["comparison_status"] == "ready").sum()) if not comparison_df.empty and "comparison_status" in comparison_df.columns else 0,
        "fail_rows": int((final_summary_df["result_status"] == "fail").sum()) if "result_status" in final_summary_df.columns else 0,
        "warning_rows": int((final_summary_df["result_status"] == "warning").sum()) if "result_status" in final_summary_df.columns else 0,
        "exclude_rows": int((final_summary_df["result_status"] == "exclude").sum()) if "result_status" in final_summary_df.columns else 0,
    }


def _existing_run_csv_issues(manifest: pd.DataFrame) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    if manifest.empty:
        return issues
    for _, row in manifest.iterrows():
        output_dir = _resolve_project_path(str(row["output_dir"]))
        if not output_dir.exists():
            continue
        csvs = sorted(path.name for path in output_dir.glob("*.csv"))
        if not csvs:
            continue
        if csvs != [SIMULATION_RESULT_NAME]:
            issues.append(
                {
                    "run_id": str(row.get("run_id", "")),
                    "output_dir": str(output_dir),
                    "csv_files": csvs,
                }
            )
    return issues


def _check_final_result(outputs_root: Path) -> dict[str, Any]:
    final_root = _mode_root(outputs_root, "final")
    aggregate_dir = final_root / "aggregate"
    final_summary = _final_summary_path(outputs_root)
    manifest = _read_csv(_target_manifest_path(outputs_root, "final"))
    run_level_df = agg30._build_run_level_results(manifest) if not manifest.empty else pd.DataFrame()
    preflight_df = _build_preflight_checks(outputs_root, manifest) if not manifest.empty else pd.DataFrame()
    summary_df = _read_csv(final_summary)
    required_final_columns = {
        "crosswalk_id",
        "result_status",
        "result_reason",
        "usable_seed_count",
        "fail_seed_count",
        "smart_network_avg_delay_sec_mean",
        "smart_low_pet_per_100_crossings_mean",
        "smart_accident_risk_estimate_mean",
    }
    aggregate_csvs = sorted(path.name for path in aggregate_dir.glob("*.csv"))
    run_csv_issues = _existing_run_csv_issues(manifest)
    unexpected_aggregate_csvs = [name for name in aggregate_csvs if name != FINAL_SUMMARY_NAME]
    missing_final_columns = sorted(required_final_columns - set(summary_df.columns)) if not summary_df.empty else sorted(required_final_columns)
    fail_rows = int((summary_df["result_status"] == "fail").sum()) if not summary_df.empty and "result_status" in summary_df.columns else 0
    preflight_failures = preflight_df[preflight_df["status"] == "fail"].to_dict(orient="records") if not preflight_df.empty else []
    valid = final_summary.exists() and not unexpected_aggregate_csvs and not run_csv_issues and fail_rows == 0 and not preflight_failures and not missing_final_columns
    return {
        "valid": valid,
        "final_summary_exists": final_summary.exists(),
        "final_summary_path": str(final_summary),
        "final_summary_row_count": int(len(summary_df)),
        "missing_final_columns": missing_final_columns,
        "aggregate_csvs": aggregate_csvs,
        "aggregate_csv_count": int(len(aggregate_csvs)),
        "unexpected_aggregate_csvs": unexpected_aggregate_csvs,
        "run_csv_issues": run_csv_issues,
        "preflight_failures": preflight_failures,
        "final_summary_fail_rows": fail_rows,
        "final_summary_warning_rows": int((summary_df["result_status"] == "warning").sum()) if not summary_df.empty and "result_status" in summary_df.columns else 0,
        "final_summary_exclude_rows": int((summary_df["result_status"] == "exclude").sum()) if not summary_df.empty and "result_status" in summary_df.columns else 0,
        "expected_run_csv_name": SIMULATION_RESULT_NAME,
        "expected_final_summary_name": FINAL_SUMMARY_NAME,
        "completed_runs": int(agg30._completed_manifest_count(run_level_df)) if not run_level_df.empty else 0,
        "planned_runs": int(len(manifest)),
    }


def cmd_verify(args: argparse.Namespace) -> None:
    source_pipeline_root = Path(args.source_pipeline_root).expanduser().resolve()
    outputs_root = Path(args.outputs_root).expanduser().resolve()
    sumo_home = resolve_sumo_home()
    smoke = _verify_mode(source_pipeline_root, outputs_root, "smoke")
    final = _verify_mode(source_pipeline_root, outputs_root, "final")
    payload = {
        "source_pipeline_root": str(source_pipeline_root),
        "outputs_root": str(outputs_root),
        "expected_commands": EXPECTED_COMMANDS,
        "expected_final_summary_path": str(_final_summary_path(outputs_root)),
        "sumo_home": _safe_str(sumo_home),
        "smoke": smoke,
        "final": final,
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    if not _safe_str(sumo_home) or smoke["missing_input_count"] or final["missing_input_count"]:
        raise SystemExit(1)


def cmd_run(args: argparse.Namespace) -> None:
    source_pipeline_root = Path(args.source_pipeline_root).expanduser().resolve()
    outputs_root = Path(args.outputs_root).expanduser().resolve()
    result = _execute_mode(
        source_pipeline_root,
        outputs_root,
        args.mode,
        dry_run=args.dry_run,
        net_group=args.net_group,
        limit=args.limit,
        run_id=args.run_id,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


def cmd_aggregate_final(args: argparse.Namespace) -> None:
    outputs_root = Path(args.outputs_root).expanduser().resolve()
    print(json.dumps(_aggregate_final(outputs_root), ensure_ascii=False, indent=2))


def cmd_check_final_result(args: argparse.Namespace) -> None:
    outputs_root = Path(args.outputs_root).expanduser().resolve()
    payload = _check_final_result(outputs_root)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    if not payload["valid"]:
        raise SystemExit(1)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Simple command surface for smoke/final sampled10 runs.")
    parser.add_argument("--source-pipeline-root", default=str(DEFAULT_SOURCE_PIPELINE_ROOT))
    parser.add_argument("--outputs-root", default=str(DEFAULT_OUTPUTS_ROOT))
    sub = parser.add_subparsers(dest="cmd", required=True)

    verify = sub.add_parser("verify")
    verify.set_defaults(func=cmd_verify)

    run = sub.add_parser("run")
    run.add_argument("--mode", choices=sorted(MODE_CONFIGS), required=True)
    run.add_argument("--dry-run", action="store_true")
    run.add_argument("--net-group", "--run-group", dest="net_group", default=None)
    run.add_argument("--limit", type=int, default=None)
    run.add_argument("--run-id", default=None)
    run.set_defaults(func=cmd_run)

    aggregate_final = sub.add_parser("aggregate-final")
    aggregate_final.set_defaults(func=cmd_aggregate_final)

    check_final = sub.add_parser("check-final-result")
    check_final.set_defaults(func=cmd_check_final_result)
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
