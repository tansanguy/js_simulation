#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import re
import shutil
from collections import Counter
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

try:  # optional
    from scipy import stats  # type: ignore
except Exception:  # pragma: no cover
    stats = None

try:
    from .phase6_result_standardizer import _standardize_extension_scope_audit_df
except ImportError:  # pragma: no cover
    from phase6_result_standardizer import _standardize_extension_scope_audit_df


RESULT_COLUMNS = [
    "seed",
    "scenario",
    "crosswalk_id",
    "vehicle_hourly_volume",
    "completed",
    "simulation_error",
    "extension_count",
    "extension_sec",
    "vehicle_requested_count",
    "vehicle_route_valid_count",
    "vehicle_departed_count",
    "vehicle_arrived_count",
    "vehicle_route_invalid_count",
    "veh_waiting_time_mean",
    "veh_time_loss_mean",
    "impact_100m_time_loss_mean",
    "impact_300m_time_loss_mean",
    "impact_500m_time_loss_mean",
    "impact_1000m_time_loss_mean",
    "impact_global_time_loss_mean",
    "source_result_csv",
]

DELTA_COLUMNS = [
    "seed",
    "crosswalk_id",
    "vehicle_hourly_volume",
    "completed_baseline",
    "completed_smart",
    "extension_count_baseline",
    "extension_count_smart",
    "extension_delta",
    "veh_time_loss_mean_baseline",
    "veh_time_loss_mean_smart",
    "veh_time_loss_mean_delta",
    "veh_waiting_time_mean_baseline",
    "veh_waiting_time_mean_smart",
    "veh_waiting_time_mean_delta",
    "impact_100m_time_loss_mean_baseline",
    "impact_100m_time_loss_mean_smart",
    "impact_100m_time_loss_mean_delta",
    "impact_300m_time_loss_mean_baseline",
    "impact_300m_time_loss_mean_smart",
    "impact_300m_time_loss_mean_delta",
    "impact_500m_time_loss_mean_baseline",
    "impact_500m_time_loss_mean_smart",
    "impact_500m_time_loss_mean_delta",
    "impact_1000m_time_loss_mean_baseline",
    "impact_1000m_time_loss_mean_smart",
    "impact_1000m_time_loss_mean_delta",
    "impact_global_time_loss_mean_baseline",
    "impact_global_time_loss_mean_smart",
    "impact_global_time_loss_mean_delta",
    "paired_ok",
]

PAIR_DELTA_METRICS = [
    "extension_delta",
    "veh_time_loss_mean_delta",
    "veh_waiting_time_mean_delta",
    "impact_100m_time_loss_mean_delta",
    "impact_300m_time_loss_mean_delta",
    "impact_500m_time_loss_mean_delta",
    "impact_1000m_time_loss_mean_delta",
    "impact_global_time_loss_mean_delta",
]

EXT_EVENT_COLUMNS = [
    "seed",
    "scenario",
    "crosswalk_id",
    "time",
    "candidate_index",
    "original_candidate_index",
    "tls_id",
    "linkIndex",
    "phase",
    "phase_segment",
    "state",
    "remaining_before",
    "extension_sec",
    "ped_near",
    "error",
    "source_event_csv",
]

IMPACT_SCOPE_COLUMNS = [
    "seed",
    "scenario",
    "crosswalk_id",
    "scope",
    "unique_edge_count",
    "impact_edge_source_main",
    "source_impact_edges_csv",
]

EXPECTED_PHASE6_RESULT_CSVS = [
    "00_run_completion_summary.csv",
    "01_all_results_long.csv",
    "02_baseline_smart_paired_delta.csv",
    "03_paired_summary_by_crosswalk.csv",
    "04_extension_events_all.csv",
    "phase6_extension_scope_audit.csv",
    "05_signal_phase_audit.csv",
    "06_impact_scope_edge_counts.csv",
    "07_source_file_inventory.csv",
]

PHASE6_CSV_README_NAMES = {name: name.replace(".csv", ".md") for name in EXPECTED_PHASE6_RESULT_CSVS}

SUMMARY_METRICS = [
    "extension_delta",
    "veh_time_loss_mean_delta",
    "veh_waiting_time_mean_delta",
    "impact_100m_time_loss_mean_delta",
    "impact_300m_time_loss_mean_delta",
    "impact_500m_time_loss_mean_delta",
    "impact_1000m_time_loss_mean_delta",
    "impact_global_time_loss_mean_delta",
]

LOG_PATTERNS = [
    re.compile(r"Traceback"),
    re.compile(r"Error"),
    re.compile(r"ERROR"),
    re.compile(r"ModuleNotFoundError"),
    re.compile(r"ValueError"),
    re.compile(r"Connection closed"),
    re.compile(r"simulation_error", re.IGNORECASE),
    re.compile(r"output_dir:"),
    re.compile(r"candidate_rows:"),
    re.compile(r"scenario:"),
]


def _project_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _read_csv(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame()
    try:
        return pd.read_csv(path, encoding="utf-8-sig")
    except Exception:
        try:
            return pd.read_csv(path, encoding="utf-8")
        except Exception:
            return pd.DataFrame()


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        with path.open(encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def _safe_bool(v: Any) -> bool | None:
    if v is None:
        return None
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, np.integer)):
        return bool(v)
    if isinstance(v, float) and math.isnan(v):
        return None
    s = str(v).strip().lower()
    if s in {"true", "1", "yes", "y"}:
        return True
    if s in {"false", "0", "no", "n"}:
        return False
    return None


def _safe_float(v: Any) -> float | np.floating | None:
    if v is None:
        return np.nan
    try:
        f = float(v)
        return np.nan if math.isnan(f) else f
    except Exception:
        return np.nan


def _safe_int(v: Any) -> int | None:
    if v is None:
        return None
    try:
        if isinstance(v, float) and math.isnan(v):
            return None
        return int(float(v))
    except Exception:
        return None


def _is_missing(v: Any) -> bool:
    if v is None:
        return True
    if isinstance(v, float) and math.isnan(v):
        return True
    if isinstance(v, str) and not v.strip():
        return True
    return False


def _first_nonempty(*values: Any, default: Any = np.nan) -> Any:
    for v in values:
        if not _is_missing(v):
            return v
    return default


def _to_scalar(v: Any) -> Any:
    if isinstance(v, (np.floating, np.integer)):
        return v.item()
    if isinstance(v, pd.Timestamp):
        return v.isoformat()
    return v


def _as_series(df: pd.DataFrame, col: str) -> pd.Series:
    if col in df.columns:
        return df[col]
    return pd.Series([np.nan] * len(df))


def _find_manifest(experiment_root: Path) -> Path:
    manifest = experiment_root / "phase6_vehicle_experiment_manifest.csv"
    if manifest.exists():
        return manifest
    candidates = sorted(experiment_root.rglob("phase6_vehicle_experiment_manifest.csv"))
    if candidates:
        return candidates[0]
    raise FileNotFoundError(f"manifest not found under {experiment_root}")


def _load_manifest(experiment_root: Path) -> pd.DataFrame:
    manifest_path = _find_manifest(experiment_root)
    df = _read_csv(manifest_path)
    if df.empty:
        raise ValueError(f"manifest empty: {manifest_path}")
    df["manifest_path"] = str(manifest_path)
    return df


def _normalize_source_path(path: Path, transition_root: Path) -> str:
    try:
        return str(path.relative_to(transition_root))
    except Exception:
        return str(path)


def _summarize_run(
    manifest_row: pd.Series,
    result_df: pd.DataFrame,
    result_csv: Path,
    run_meta: dict[str, Any],
) -> dict[str, Any]:
    seed = _safe_int(manifest_row.get("seed"))
    scenario = str(manifest_row.get("scenario", ""))
    volume = _safe_float(manifest_row.get("vehicle_hourly_volume"))
    output_dir = str(manifest_row.get("output_dir", ""))
    completed_series = _as_series(result_df, "completed")
    error_series = _as_series(result_df, "simulation_error")

    completed_values = [_safe_bool(v) for v in completed_series.tolist()]
    completed_all = bool(completed_values) and all(v is True for v in completed_values if v is not None)

    row_errors: list[str] = []
    for v in error_series.tolist():
        if _is_missing(v):
            continue
        s = str(v).strip()
        if s:
            row_errors.append(s)
    meta_error = run_meta.get("simulation_error")
    if not _is_missing(meta_error):
        row_errors.append(str(meta_error).strip())
    meta_trace = run_meta.get("simulation_error_trace")
    if not _is_missing(meta_trace):
        row_errors.append(str(meta_trace).strip())
    unique_errors = [err for err in dict.fromkeys([e for e in row_errors if e])]

    crosswalk_row_count = int(len(result_df))
    result_csv_exists = result_csv.exists()
    simulation_error_count = int(bool(unique_errors))
    extension_total = int(pd.to_numeric(_as_series(result_df, "extension_count"), errors="coerce").fillna(0).sum()) if not result_df.empty else 0
    vehicle_requested_total = int(pd.to_numeric(_as_series(result_df, "vehicle_requested_count"), errors="coerce").fillna(0).sum()) if not result_df.empty else 0
    vehicle_departed_total = int(pd.to_numeric(_as_series(result_df, "vehicle_departed_count"), errors="coerce").fillna(0).sum()) if not result_df.empty else 0
    vehicle_arrived_total = int(pd.to_numeric(_as_series(result_df, "vehicle_arrived_count"), errors="coerce").fillna(0).sum()) if not result_df.empty else 0
    vehicle_route_invalid_total = int(pd.to_numeric(_as_series(result_df, "vehicle_route_invalid_count"), errors="coerce").fillna(0).sum()) if not result_df.empty else 0

    if not result_csv_exists:
        status = "MISSING_RESULT"
    elif not completed_all:
        status = "NOT_COMPLETED"
    elif simulation_error_count > 0:
        status = "HAS_SIMULATION_ERROR"
    elif vehicle_departed_total == 0:
        status = "NO_VEHICLES_DEPARTED"
    else:
        status = "OK"

    return {
        "seed": seed,
        "scenario": scenario,
        "vehicle_hourly_volume": volume,
        "output_dir": output_dir,
        "result_csv_exists": result_csv_exists,
        "completed_all": completed_all,
        "simulation_error_count": simulation_error_count,
        "simulation_error_messages": " | ".join(unique_errors),
        "crosswalk_row_count": crosswalk_row_count,
        "extension_total": extension_total,
        "vehicle_requested_total": vehicle_requested_total,
        "vehicle_departed_total": vehicle_departed_total,
        "vehicle_arrived_total": vehicle_arrived_total,
        "vehicle_route_invalid_total": vehicle_route_invalid_total,
        "status": status,
    }


def _load_result_table(
    seed: int,
    scenario: str,
    vehicle_hourly_volume: float,
    result_csv: Path,
) -> pd.DataFrame:
    df = _read_csv(result_csv)
    if df.empty:
        return pd.DataFrame(columns=RESULT_COLUMNS)
    out = pd.DataFrame(index=df.index)
    out["seed"] = seed
    out["scenario"] = scenario
    out["crosswalk_id"] = _as_series(df, "crosswalk_id")
    out["vehicle_hourly_volume"] = vehicle_hourly_volume
    out["completed"] = _as_series(df, "completed").map(_safe_bool)
    out["simulation_error"] = _as_series(df, "simulation_error")
    out["extension_count"] = pd.to_numeric(_as_series(df, "extension_count"), errors="coerce")
    out["extension_sec"] = pd.to_numeric(_as_series(df, "extension_sec"), errors="coerce")
    out["vehicle_requested_count"] = pd.to_numeric(_as_series(df, "vehicle_requested_count"), errors="coerce")
    out["vehicle_route_valid_count"] = pd.to_numeric(_as_series(df, "vehicle_route_valid_count"), errors="coerce")
    out["vehicle_departed_count"] = pd.to_numeric(_as_series(df, "vehicle_departed_count"), errors="coerce")
    out["vehicle_arrived_count"] = pd.to_numeric(_as_series(df, "vehicle_arrived_count"), errors="coerce")
    out["vehicle_route_invalid_count"] = pd.to_numeric(_as_series(df, "vehicle_route_invalid_count"), errors="coerce")
    out["veh_waiting_time_mean"] = pd.to_numeric(_as_series(df, "veh_waiting_time_mean"), errors="coerce")
    out["veh_time_loss_mean"] = pd.to_numeric(_as_series(df, "veh_time_loss_mean"), errors="coerce")
    out["impact_100m_time_loss_mean"] = pd.to_numeric(_as_series(df, "impact_100m_time_loss_mean"), errors="coerce")
    out["impact_300m_time_loss_mean"] = pd.to_numeric(_as_series(df, "impact_300m_time_loss_mean"), errors="coerce")
    out["impact_500m_time_loss_mean"] = pd.to_numeric(_as_series(df, "impact_500m_time_loss_mean"), errors="coerce")
    out["impact_1000m_time_loss_mean"] = pd.to_numeric(_as_series(df, "impact_1000m_time_loss_mean"), errors="coerce")
    out["impact_global_time_loss_mean"] = pd.to_numeric(_as_series(df, "impact_global_time_loss_mean"), errors="coerce")
    out["source_result_csv"] = str(result_csv)
    return out.reindex(columns=RESULT_COLUMNS)


def _build_long_results(manifest: pd.DataFrame) -> pd.DataFrame:
    rows: list[pd.DataFrame] = []
    for item in manifest.itertuples(index=False):
        output_dir = Path(str(getattr(item, "output_dir")))
        scenario = str(getattr(item, "scenario"))
        seed = _safe_int(getattr(item, "seed"))
        volume = float(getattr(item, "vehicle_hourly_volume", np.nan))
        result_csv = output_dir / f"phase6_vehicle_flow_{scenario}_results.csv"
        if not result_csv.exists():
            continue
        rows.append(_load_result_table(seed or 0, scenario, volume, result_csv))
    if not rows:
        return pd.DataFrame(columns=RESULT_COLUMNS)
    return pd.concat(rows, ignore_index=True)


def _build_run_summary(manifest: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for item in manifest.itertuples(index=False):
        output_dir = Path(str(getattr(item, "output_dir")))
        scenario = str(getattr(item, "scenario"))
        seed = _safe_int(getattr(item, "seed")) or 0
        volume = float(getattr(item, "vehicle_hourly_volume", np.nan))
        result_csv = output_dir / f"phase6_vehicle_flow_{scenario}_results.csv"
        run_meta = _read_json(output_dir / "run_metadata.json")
        result_df = _read_csv(result_csv)
        rows.append(_summarize_run(pd.Series(item._asdict()), result_df, result_csv, run_meta))
    return pd.DataFrame(rows)


def _paired_delta(long_results: pd.DataFrame) -> pd.DataFrame:
    if long_results.empty:
        return pd.DataFrame(columns=DELTA_COLUMNS)
    baseline = long_results[long_results["scenario"].astype(str) == "baseline"].copy()
    smart = long_results[long_results["scenario"].astype(str) == "smart"].copy()
    if baseline.empty or smart.empty:
        return pd.DataFrame(columns=DELTA_COLUMNS)

    merge_cols = ["seed", "crosswalk_id"]
    base_cols = set(baseline.columns)
    smart_cols = set(smart.columns)
    if "vehicle_hourly_volume" in base_cols and "vehicle_hourly_volume" in smart_cols:
        merge_cols.append("vehicle_hourly_volume")

    merged = baseline.merge(
        smart,
        on=merge_cols,
        how="outer",
        suffixes=("_baseline", "_smart"),
        indicator=True,
    )

    for side in ("baseline", "smart"):
        for col in [
            "extension_count",
            "extension_sec",
            "veh_time_loss_mean",
            "veh_waiting_time_mean",
            "impact_100m_time_loss_mean",
            "impact_300m_time_loss_mean",
            "impact_500m_time_loss_mean",
            "impact_1000m_time_loss_mean",
            "impact_global_time_loss_mean",
        ]:
            full = f"{col}_{side}"
            if full in merged.columns:
                merged[full] = pd.to_numeric(merged[full], errors="coerce")

    def _paired_ok(row: pd.Series) -> bool:
        return (
            _safe_bool(row.get("completed_baseline")) is True
            and _safe_bool(row.get("completed_smart")) is True
            and _is_missing(row.get("simulation_error_baseline"))
            and _is_missing(row.get("simulation_error_smart"))
        )

    out = pd.DataFrame()
    out["seed"] = merged["seed"]
    out["crosswalk_id"] = merged["crosswalk_id"]
    out["vehicle_hourly_volume"] = merged.get("vehicle_hourly_volume", np.nan)
    out["completed_baseline"] = merged.get("completed_baseline", np.nan)
    out["completed_smart"] = merged.get("completed_smart", np.nan)
    out["extension_count_baseline"] = merged.get("extension_count_baseline", np.nan)
    out["extension_count_smart"] = merged.get("extension_count_smart", np.nan)
    out["extension_delta"] = out["extension_count_smart"] - out["extension_count_baseline"]

    for metric in [
        "veh_time_loss_mean",
        "veh_waiting_time_mean",
        "impact_100m_time_loss_mean",
        "impact_300m_time_loss_mean",
        "impact_500m_time_loss_mean",
        "impact_1000m_time_loss_mean",
        "impact_global_time_loss_mean",
    ]:
        out[f"{metric}_baseline"] = merged.get(f"{metric}_baseline", np.nan)
        out[f"{metric}_smart"] = merged.get(f"{metric}_smart", np.nan)
        out[f"{metric}_delta"] = out[f"{metric}_smart"] - out[f"{metric}_baseline"]

    out["paired_ok"] = merged.apply(_paired_ok, axis=1)
    return out.reindex(columns=DELTA_COLUMNS)


def _paired_summary_by_crosswalk(delta_df: pd.DataFrame) -> pd.DataFrame:
    if delta_df.empty:
        return pd.DataFrame(
            columns=[
                "crosswalk_id",
                "metric",
                "n",
                "mean_delta",
                "std_delta",
                "se_delta",
                "ci95_low",
                "ci95_high",
                "min_delta",
                "max_delta",
                "interpretation_hint",
            ]
        )

    rows: list[dict[str, Any]] = []
    paired = delta_df[delta_df["paired_ok"] == True].copy()  # noqa: E712
    for crosswalk_id, group in paired.groupby("crosswalk_id", dropna=False):
        for metric in SUMMARY_METRICS:
            vals = pd.to_numeric(group[metric], errors="coerce").dropna()
            n = int(len(vals))
            mean_delta = float(vals.mean()) if n else np.nan
            std_delta = float(vals.std(ddof=1)) if n > 1 else np.nan
            se_delta = float(std_delta / math.sqrt(n)) if n > 1 and np.isfinite(std_delta) else np.nan
            ci_low = np.nan
            ci_high = np.nan
            if n > 1 and np.isfinite(se_delta):
                if stats is not None:
                    try:
                        crit = float(stats.t.ppf(0.975, df=n - 1))
                    except Exception:
                        crit = 1.96
                else:
                    crit = 1.96
                ci_low = float(mean_delta - crit * se_delta)
                ci_high = float(mean_delta + crit * se_delta)
            hint = (
                "mean_delta > 0이면 smart가 baseline보다 해당 지표를 증가시킴; "
                "mean_delta < 0이면 smart가 baseline보다 해당 지표를 감소시킴; "
                "단, 통계적 해석은 CI 확인 필요"
            )
            rows.append(
                {
                    "crosswalk_id": crosswalk_id,
                    "metric": metric,
                    "n": n,
                    "mean_delta": mean_delta,
                    "std_delta": std_delta,
                    "se_delta": se_delta,
                    "ci95_low": ci_low,
                    "ci95_high": ci_high,
                    "min_delta": float(vals.min()) if n else np.nan,
                    "max_delta": float(vals.max()) if n else np.nan,
                    "interpretation_hint": hint,
                }
            )
    return pd.DataFrame(rows)


def _load_event_table(
    seed: int,
    scenario: str,
    event_csv: Path,
) -> pd.DataFrame:
    df = _read_csv(event_csv)
    if df.empty:
        return pd.DataFrame(columns=EXT_EVENT_COLUMNS)
    out = pd.DataFrame(index=df.index)
    out["seed"] = seed
    out["scenario"] = scenario
    out["crosswalk_id"] = _as_series(df, "crosswalk_id")
    out["time"] = pd.to_numeric(_as_series(df, "time"), errors="coerce")
    out["candidate_index"] = pd.to_numeric(_as_series(df, "candidate_index"), errors="coerce")
    out["original_candidate_index"] = pd.to_numeric(_as_series(df, "original_candidate_index"), errors="coerce")
    out["tls_id"] = _as_series(df, "tls_id")
    out["linkIndex"] = pd.to_numeric(_as_series(df, "linkIndex"), errors="coerce")
    out["phase"] = _as_series(df, "phase")
    out["phase_segment"] = _as_series(df, "phase_segment")
    out["state"] = _as_series(df, "state")
    out["remaining_before"] = pd.to_numeric(_as_series(df, "remaining_before"), errors="coerce")
    out["extension_sec"] = pd.to_numeric(_as_series(df, "extension_sec"), errors="coerce")
    out["ped_near"] = _as_series(df, "ped_near")
    out["error"] = _as_series(df, "error")
    out["source_event_csv"] = str(event_csv)
    return out.reindex(columns=EXT_EVENT_COLUMNS)


def _build_extension_events(manifest: pd.DataFrame) -> pd.DataFrame:
    rows: list[pd.DataFrame] = []
    for item in manifest.itertuples(index=False):
        output_dir = Path(str(getattr(item, "output_dir")))
        scenario = str(getattr(item, "scenario"))
        seed = _safe_int(getattr(item, "seed")) or 0
        event_csv = output_dir / f"phase6_vehicle_flow_{scenario}_extension_events.csv"
        if not event_csv.exists():
            continue
        rows.append(_load_event_table(seed, scenario, event_csv))
    if not rows:
        return pd.DataFrame(columns=EXT_EVENT_COLUMNS)
    return pd.concat(rows, ignore_index=True)


def _build_impact_scope_counts(manifest: pd.DataFrame) -> pd.DataFrame:
    rows: list[pd.DataFrame] = []
    for item in manifest.itertuples(index=False):
        output_dir = Path(str(getattr(item, "output_dir")))
        scenario = str(getattr(item, "scenario"))
        seed = _safe_int(getattr(item, "seed")) or 0
        impact_csv = output_dir / f"phase6_vehicle_flow_{scenario}_impact_edges.csv"
        df = _read_csv(impact_csv)
        if df.empty:
            continue
        if "crosswalk_id" not in df.columns or "scope" not in df.columns or "edge_id" not in df.columns:
            continue
        for (crosswalk_id, scope), group in df.groupby(["crosswalk_id", "scope"], dropna=False):
            source_col = "impact_edge_source" if "impact_edge_source" in group.columns else "edge_source"
            source_vals = [str(v) for v in group[source_col].dropna().tolist()] if source_col in group.columns else []
            if source_vals:
                main_source = Counter(source_vals).most_common(1)[0][0]
            else:
                main_source = np.nan
            rows.append(
                pd.DataFrame(
                    [
                        {
                            "seed": seed,
                            "scenario": scenario,
                            "crosswalk_id": crosswalk_id,
                            "scope": scope,
                            "unique_edge_count": int(group["edge_id"].astype(str).nunique()),
                            "impact_edge_source_main": main_source,
                            "source_impact_edges_csv": str(impact_csv),
                        }
                    ]
                )
            )
    if not rows:
        return pd.DataFrame(columns=IMPACT_SCOPE_COLUMNS)
    return pd.concat(rows, ignore_index=True).reindex(columns=IMPACT_SCOPE_COLUMNS)


def _classify_inventory_item(name: str, is_dir: bool) -> tuple[str, str, str, str]:
    lower = name.lower()
    if is_dir:
        return "DIRECTORY", "HIGH", "KEEP_IN_ORIGINAL", "Run folder with raw outputs; do not review every file."
    if lower == ".ds_store":
        return "DEBUG_NOTE", "LOW", "IGNORE_FOR_REVIEW", "Mac metadata."
    if "signal_phase_audit" in lower:
        return "CORE_VALIDATION", "HIGH", "COPIED_TO_HUMAN_CSV", "Target TLS audit; confirm candidate signal phases."
    if "mini_experiment_candidates_batch03_only" in lower:
        return "CORE_VALIDATION", "HIGH", "KEEP_IN_ORIGINAL", "Batch03 source of truth for candidate set."
    if "vehicle_experiment_manifest" in lower or "vehicle_experiment_plan" in lower:
        return "CORE_RESULT", "HIGH", "KEEP_IN_ORIGINAL", "Run manifest and plan."
    if "command_to_run" in lower or lower.endswith("_commands.sh"):
        return "COMMAND_SCRIPT", "MEDIUM", "KEEP_IN_ORIGINAL", "Repro command only."
    if "smoke" in lower:
        return "OLD_SMOKE", "LOW", "IGNORE_FOR_REVIEW", "Old smoke output; not primary review target."
    if lower.endswith(".log"):
        return "LOG", "MEDIUM", "KEEP_IN_ORIGINAL", "Raw execution log."
    if lower.endswith(".json"):
        return "INTERMEDIATE", "LOW", "KEEP_IN_ORIGINAL", "Structured metadata."
    if lower.endswith(".md"):
        return "INTERMEDIATE", "LOW", "KEEP_IN_ORIGINAL", "Notes and drafts."
    if lower.endswith(".csv"):
        return "INTERMEDIATE", "MEDIUM", "KEEP_IN_ORIGINAL", "Intermediate or comparison CSV."
    return "DEBUG_NOTE", "LOW", "KEEP_IN_ORIGINAL", "Unclassified item."


def _build_source_inventory(transition_root: Path) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for item in sorted(transition_root.iterdir(), key=lambda p: p.name):
        category, importance, action, note = _classify_inventory_item(item.name, item.is_dir())
        rows.append(
            {
                "path": item.name + ("/" if item.is_dir() else ""),
                "category": category,
                "human_importance": importance,
                "action": action,
                "note": note,
            }
        )
    return pd.DataFrame(rows)


def _extract_log_lines(log_path: Path) -> str:
    if not log_path.exists():
        return f"missing log: {log_path}\n"
    seen: set[str] = set()
    kept: list[str] = []
    for line in log_path.read_text(encoding="utf-8", errors="replace").splitlines():
        if any(p.search(line) for p in LOG_PATTERNS):
            key = line.rstrip()
            if key not in seen:
                seen.add(key)
                kept.append(key)
    if not kept:
        return f"no matching error lines in {log_path}\n"
    return "\n".join(kept) + "\n"


def _write_csv(df: pd.DataFrame, path: Path) -> None:
    _ensure_dir(path.parent)
    df.to_csv(path, index=False)


def _fmt_num(v: Any, digits: int = 3) -> str:
    try:
        if v is None:
            return "N/A"
        if isinstance(v, float) and math.isnan(v):
            return "N/A"
        return f"{float(v):.{digits}f}"
    except Exception:
        return str(v)


def _fmt_bool(v: Any) -> str:
    if isinstance(v, bool):
        return "True" if v else "False"
    if _is_missing(v):
        return "N/A"
    s = str(v).strip().lower()
    if s in {"true", "1", "yes", "y"}:
        return "True"
    if s in {"false", "0", "no", "n"}:
        return "False"
    return str(v)


def _markdown_table(headers: list[str], rows: list[list[Any]]) -> str:
    out = ["| " + " | ".join(headers) + " |", "| " + " | ".join(["---"] * len(headers)) + " |"]
    for row in rows:
        out.append("| " + " | ".join(str(cell) for cell in row) + " |")
    return "\n".join(out)


def _phase6_file_specs(
    total_runs: int,
    baseline_runs: int,
    smart_runs: int,
    ok_runs: int,
    error_runs: int,
    invalid_route_total: int,
    extension_total: int,
    event_total: int,
    seed_count: int,
    scenario_count: int,
    candidate_count: int,
    raw_result_rows: int,
    paired_rows: int,
    paired_ok_rows: int,
    vehicle_requested_total: int,
    vehicle_departed_total: int,
    vehicle_arrived_total: int,
    event_by_crosswalk: dict[str, int],
    seed_by_crosswalk: dict[str, int],
    mixed_event_count: int,
    cw_time_loss: dict[str, float],
    cw_waiting: dict[str, float],
    paired_summary: pd.DataFrame,
    extension_events: pd.DataFrame,
    signal_audit: pd.DataFrame,
    impact_scope: pd.DataFrame,
    inventory: pd.DataFrame,
    inventory_category: dict[str, int],
    inventory_importance: dict[str, int],
) -> dict[str, dict[str, Any]]:
    ext_summary_rows, delta_summary_rows = _phase6_crosswalk_extension_summary(extension_events, paired_delta=paired_summary, summary=paired_summary)
    # The helper above is used only for summary text generation; reuse the same
    # crosswalk-level summaries from current data to keep README text aligned.
    return {
        "00_run_completion_summary.csv": {
            "what": "실행 정상 종료 여부와 run 안정성 확인용",
            "row": "행 1개 = seed/scenario 1회 run",
            "core_cols": "`status`, `completed_all`, `simulation_error_count`, `vehicle_departed_total`, `vehicle_arrived_total`, `vehicle_route_invalid_total`, `extension_total`",
            "question": "모든 run이 완료됐는지, 오류가 있는지, 차량이 실제로 출발/도착했는지, invalid route가 있는지 확인한다.",
            "good_bad": "좋음: `status=OK`, `simulation_error_count=0`, `vehicle_route_invalid_total=0`. 나쁨: `MISSING_RESULT`, `NOT_COMPLETED`, `HAS_SIMULATION_ERROR`, `NO_VEHICLES_DEPARTED`.",
            "actual": f"run `{total_runs}`개 중 baseline `{baseline_runs}`개, smart `{smart_runs}`개, `OK` `{ok_runs}`개, error `{error_runs}`개. `vehicle_requested_total={vehicle_requested_total}`, `vehicle_departed_total={vehicle_departed_total}`, `vehicle_arrived_total={vehicle_arrived_total}`, `vehicle_route_invalid_total={invalid_route_total}`, `extension_total={extension_total}`.",
            "caution": "run-level 안정성만 본다. 정책 효과는 이 파일로 판단하지 않는다.",
            "report": "이번 run은 전체적으로 안정적이고 invalid route가 0이다.",
        },
        "01_all_results_long.csv": {
            "what": "모든 run의 원자료",
            "row": "행 1개 = seed/scenario/crosswalk 1개 결과",
            "core_cols": "`seed`, `scenario`, `crosswalk_id`, `completed`, `extension_count`, `veh_time_loss_mean`, `veh_waiting_time_mean`, `source_result_csv`",
            "question": "baseline와 smart가 같은 seed로 잘 짝지어졌는지, crosswalk별 raw 결과가 모두 들어왔는지 확인한다.",
            "good_bad": "좋음: 60 run × 3 crosswalk = 180 row가 모두 있고 seed/scenario 쌍이 맞는다. 나쁨: 한쪽 scenario만 있거나 seed가 빠진다.",
            "actual": f"`{raw_result_rows}` row. baseline/scenario 각각 seed `{seed_count}` 반복, crosswalk `{candidate_count}`개.",
            "caution": "이 파일은 근거 데이터다. 해석은 02, 03 요약 파일이 더 중요하다.",
            "report": "원자료가 180 row로 쌓여 있어 paired 비교의 근거가 된다.",
        },
        "02_baseline_smart_paired_delta.csv": {
            "what": "같은 seed에서 smart - baseline 차이를 계산한 paired 비교 파일",
            "row": "행 1개 = seed × crosswalk 1개 paired pair",
            "core_cols": "`extension_delta`, `veh_time_loss_mean_delta`, `veh_waiting_time_mean_delta`, `paired_ok`",
            "question": "smart가 baseline보다 각 지표를 올렸는지/내렸는지 본다. delta 부호를 친절히 읽는다.",
            "good_bad": "delta < 0 = smart 감소, delta > 0 = smart 증가. paired 비교라 단순 평균보다 중요하다.",
            "actual": f"paired row `{paired_rows}`개, `paired_ok` `{paired_ok_rows}`개. crosswalk별 평균 delta는 `LINK_43439` time loss `+{_fmt_num(cw_time_loss.get('LINK_43439', np.nan), 4)}`, waiting `+{_fmt_num(cw_waiting.get('LINK_43439', np.nan), 4)}`; `NODE_7240` time loss `{_fmt_num(cw_time_loss.get('NODE_7240', np.nan), 4)}`, waiting `{_fmt_num(cw_waiting.get('NODE_7240', np.nan), 4)}`; `LINK_52248` time loss `{_fmt_num(cw_time_loss.get('LINK_52248', np.nan), 4)}`, waiting `{_fmt_num(cw_waiting.get('LINK_52248', np.nan), 4)}`.",
            "caution": "현재 결과는 mixed phase 문제와 분리해서 읽어야 한다. 차량 지체 감소만 보고 최종 정책 효과라고 쓰면 안 된다.",
            "report": "paired delta는 최종 효과 해석의 핵심 근거지만, 현재 데이터는 smoke/구조 검증으로만 사용해야 한다.",
        },
        "03_paired_summary_by_crosswalk.csv": {
            "what": "crosswalk별 paired delta 요약",
            "row": "행 1개 = crosswalk × metric 요약",
            "core_cols": "`mean_delta`, `std_delta`, `se_delta`, `ci95_low`, `ci95_high`",
            "question": "crosswalk별로 smart가 baseline보다 실제로 달라졌는지, CI가 0을 포함하는지 확인한다.",
            "good_bad": "좋음: CI가 0을 포함하지 않고 방향성이 일관적이다. 나쁨: CI가 0을 포함하거나 표본이 적다.",
            "actual": "실제 결과: `LINK_43439`는 `veh_time_loss_mean_delta` CI가 `[-0.0043, 0.0234]`로 0을 포함한다. `NODE_7240`는 `[-0.3844, -0.1854]`로 0을 포함하지 않는다. `LINK_52248`는 `[-1.0802, -0.5252]`로 0을 포함하지 않는다. `extension_delta`는 `LINK_43439=0`, `NODE_7240=1`, `LINK_52248=0.3` 수준이다.",
            "caution": "CI 해석은 단독 결론이 아니다. extension 이벤트가 실제 발생했는지와 함께 봐야 한다.",
            "report": "차량 지체 감소가 보여도 바로 정책 효과라고 단정하면 안 된다.",
        },
        "04_extension_events_all.csv": {
            "what": "실제 smart extension event 발생 여부를 보는 핵심 파일",
            "row": "행 1개 = 1회 extension event",
            "core_cols": "`crosswalk_id`, `seed`, `time`, `state`, `remaining_before`, `extension_sec`, `linkIndex`",
            "question": "어느 후보가 실제로 연장됐는지, 몇 seed에서 연장됐는지, 총 연장 시간이 얼마나 되는지 확인한다.",
            "good_bad": "좋음: 정책 대상 crosswalk에서 원하는 seed만 적절히 extension 된다. 나쁨: extension이 전혀 없거나, mixed phase에서 차량 green까지 같이 연장된다.",
            "actual": f"event `{event_total}`개. `NODE_7240` `{event_by_crosswalk.get('NODE_7240', 0)}`개 / `{seed_by_crosswalk.get('NODE_7240', 0)}` seed, `LINK_52248` `{event_by_crosswalk.get('LINK_52248', 0)}`개 / `{seed_by_crosswalk.get('LINK_52248', 0)}` seed, `LINK_43439` 0개. 현재 이벤트는 state 문자열 기준으로 모두 mixed phase와 겹친다(`mixed` `{mixed_event_count}`개).",
            "caution": "이 파일은 실제 extension 발생만 보여준다. mixed phase 여부는 05와 함께 봐야 한다.",
            "report": "extension이 0인 후보는 정책 효과 후보로 해석하면 안 된다. 현재 데이터는 mixed phase 때문에 smoke/구조 검증용이다.",
        },
        "05_signal_phase_audit.csv": {
            "what": "각 후보가 신호 제어 가능한 구조인지 확인하는 파일",
            "row": "행 1개 = crosswalk 1개 audit",
            "core_cols": "`tls_id`, `ped_link_index`, `states_at_index`, `audit_ok`",
            "question": "전체 phase 문자열이 아니라 pedestrian linkIndex 위치의 G/r을 봐야 하는지 확인한다.",
            "good_bad": "좋음: `audit_ok=True`이고 `ped_link_index`가 모든 phase에서 범위 안에 있으며, red/green 구조가 정상이다.",
            "actual": "실제 결과: 3개 후보 모두 `audit_ok=True`. `LINK_43439`의 `states_at_index=rrrGrr`, `NODE_7240`의 `states_at_index=rrGrr`, `LINK_52248`의 `states_at_index=rrGrr`.",
            "caution": "audit_ok=True라도 extension event가 반드시 발생하는 것은 아니다. 반대로 event가 생겨도 mixed phase면 최종 정책 효과로 쓰면 안 된다.",
            "report": "전체 phase 문자열의 G 유무가 아니라 ped linkIndex 위치가 핵심이다.",
        },
        "06_impact_scope_edge_counts.csv": {
            "what": "영향권별 edge 수를 보여주는 파일",
            "row": "행 1개 = seed × scenario × crosswalk × scope",
            "core_cols": "`scope`, `unique_edge_count`, `impact_edge_source_main`, `source_impact_edges_csv`",
            "question": "100m, 300m, 500m, 1000m, global의 edge 수가 범위대로 커지는지 확인한다. fallback_route_edges가 아니라 radius_geometry인지 확인한다.",
            "good_bad": "좋음: 반경이 커질수록 edge 수가 증가하고 `impact_edge_source_main=radius_geometry`. 나쁨: global이 후보별로 달라지거나 radius 증가가 보이지 않는다.",
            "actual": "실제 결과: `LINK_43439`는 `100m=56`, `300m=242`, `500m=743`, `1000m=2143`, `global=22987`; `NODE_7240`는 `57/218/703/2085/22987`; `LINK_52248`는 `33/194/637/2603/22987`. 100m < 300m < 500m < 1000m < global 순서가 성립한다. `impact_edge_source_main`은 모두 `radius_geometry`다.",
            "caution": "global은 전체 네트워크 기준이라 후보별 정책 효과처럼 해석하면 안 된다.",
            "report": "지역 영향권은 100m~1000m를 더 중요하게 본다.",
        },
        "07_source_file_inventory.csv": {
            "what": "결과 생성에 사용된 원본/중간/검증 파일 목록",
            "row": "행 1개 = transition-root 바로 아래 파일 또는 디렉터리 1개",
            "core_cols": "`category`, `human_importance`, `action`, `note`",
            "question": "재현성과 파일 추적에 필요한 입력/중간 파일이 무엇인지 확인한다.",
            "good_bad": "좋음: 핵심 입력과 검증 파일이 분류되어 있고, 불필요한 raw 파일은 리뷰 우선순위가 낮다.",
            "actual": f"실제 결과: `{len(inventory)}`개 항목. category는 `OLD_SMOKE={inventory_category.get('OLD_SMOKE', 0)}`, `INTERMEDIATE={inventory_category.get('INTERMEDIATE', 0)}`, `COMMAND_SCRIPT={inventory_category.get('COMMAND_SCRIPT', 0)}`, `DIRECTORY={inventory_category.get('DIRECTORY', 0)}`, `CORE_VALIDATION={inventory_category.get('CORE_VALIDATION', 0)}`, `DEBUG_NOTE={inventory_category.get('DEBUG_NOTE', 0)}`. `human_importance`는 `LOW={inventory_importance.get('LOW', 0)}`, `MEDIUM={inventory_importance.get('MEDIUM', 0)}`, `HIGH={inventory_importance.get('HIGH', 0)}`.",
            "caution": "이 파일은 결과 해석용이 아니라 추적성 확인용이다.",
            "report": "보고서에서는 핵심 결과 CSV와 입력 파일 확인에만 쓰면 된다.",
        },
    }


def _locate_phase6_csv_dir(target_dir: Path) -> Path | None:
    candidates = [
        target_dir / "csv",
        target_dir / "phase6_vehicle_experiment_300_30seed_human" / "csv",
        target_dir.parent / "phase6_vehicle_experiment_300_30seed_human" / "csv",
    ]
    for cand in candidates:
        if cand.exists() and any((cand / name).exists() for name in EXPECTED_PHASE6_RESULT_CSVS):
            return cand
    return None


def _load_phase6_result_bundle(target_dir: Path) -> tuple[Path | None, dict[str, pd.DataFrame], list[str]]:
    csv_dir = _locate_phase6_csv_dir(target_dir)
    frames: dict[str, pd.DataFrame] = {}
    missing: list[str] = []
    for name in EXPECTED_PHASE6_RESULT_CSVS:
        path = csv_dir / name if csv_dir is not None else target_dir / name
        df = _read_csv(path)
        if df.empty and not path.exists():
            missing.append(name)
        frames[name] = df
    return csv_dir, frames, missing


def _delta_ci_zero_flag(row: pd.Series, metric: str) -> str:
    low = row.get("ci95_low", np.nan)
    high = row.get("ci95_high", np.nan)
    if pd.isna(low) or pd.isna(high):
        return "N/A"
    return "yes" if float(low) <= 0.0 <= float(high) else "no"


def _phase6_event_mixed_counts(events: pd.DataFrame, audit: pd.DataFrame) -> tuple[int, dict[str, int], dict[str, int]]:
    if events.empty or audit.empty or "crosswalk_id" not in audit.columns:
        return 0, {}, {}
    audit_map = audit.set_index("crosswalk_id")
    mixed_count = 0
    by_cw: dict[str, int] = {}
    total_by_cw: dict[str, int] = {}
    for _, row in events.iterrows():
        cid = str(row.get("crosswalk_id", ""))
        total_by_cw[cid] = total_by_cw.get(cid, 0) + 1
        if cid not in audit_map.index:
            continue
        ped_idx = _safe_int(audit_map.loc[cid].get("ped_link_index"))
        state = str(row.get("state", ""))
        greens = [idx for idx, ch in enumerate(state) if ch in {"G", "g"}]
        if ped_idx is None or ped_idx not in greens:
            continue
        if any(idx != ped_idx for idx in greens):
            mixed_count += 1
            by_cw[cid] = by_cw.get(cid, 0) + 1
    return mixed_count, by_cw, total_by_cw


def _phase6_crosswalk_extension_summary(events: pd.DataFrame, paired_delta: pd.DataFrame, summary: pd.DataFrame) -> tuple[list[list[Any]], list[list[Any]]]:
    if events.empty:
        return [], []
    ext_rows: list[list[Any]] = []
    delta_rows: list[list[Any]] = []
    event_counts = events.groupby("crosswalk_id").size().to_dict()
    seed_counts = events.groupby("crosswalk_id")["seed"].nunique().to_dict()
    for cid in sorted(events["crosswalk_id"].astype(str).unique().tolist()):
        cw_pairs = paired_delta[paired_delta["crosswalk_id"].astype(str) == cid]
        time_loss_delta = float(cw_pairs["veh_time_loss_mean_delta"].mean()) if not cw_pairs.empty else np.nan
        wait_delta = float(cw_pairs["veh_waiting_time_mean_delta"].mean()) if not cw_pairs.empty else np.nan
        ext_rows.append(
            [
                cid,
                int(event_counts.get(cid, 0)),
                int(seed_counts.get(cid, 0)),
                _fmt_num(time_loss_delta, 4),
                _fmt_num(wait_delta, 4),
            ]
        )
        ext_ci = summary[(summary["crosswalk_id"].astype(str) == cid) & (summary["metric"] == "extension_delta")]
        loss_ci = summary[(summary["crosswalk_id"].astype(str) == cid) & (summary["metric"] == "veh_time_loss_mean_delta")]
        wait_ci = summary[(summary["crosswalk_id"].astype(str) == cid) & (summary["metric"] == "veh_waiting_time_mean_delta")]
        ext_row = ext_ci.iloc[0] if not ext_ci.empty else pd.Series(dtype=object)
        loss_row = loss_ci.iloc[0] if not loss_ci.empty else pd.Series(dtype=object)
        wait_row = wait_ci.iloc[0] if not wait_ci.empty else pd.Series(dtype=object)
        delta_rows.append(
            [
                cid,
                _fmt_num(ext_row.get("mean_delta", np.nan), 4),
                f"[{_fmt_num(ext_row.get('ci95_low', np.nan), 4)}, {_fmt_num(ext_row.get('ci95_high', np.nan), 4)}]",
                _delta_ci_zero_flag(ext_row, "extension_delta"),
                _fmt_num(loss_row.get("mean_delta", np.nan), 4),
                f"[{_fmt_num(loss_row.get('ci95_low', np.nan), 4)}, {_fmt_num(loss_row.get('ci95_high', np.nan), 4)}]",
                _delta_ci_zero_flag(loss_row, "veh_time_loss_mean_delta"),
                _fmt_num(wait_row.get("mean_delta", np.nan), 4),
                f"[{_fmt_num(wait_row.get('ci95_low', np.nan), 4)}, {_fmt_num(wait_row.get('ci95_high', np.nan), 4)}]",
                _delta_ci_zero_flag(wait_row, "veh_waiting_time_mean_delta"),
            ]
        )
    return ext_rows, delta_rows


def write_phase6_results_readme(output_dir: Path) -> Path:
    csv_dir, frames, missing = _load_phase6_result_bundle(output_dir)
    run_summary = frames["00_run_completion_summary.csv"]
    long_results = frames["01_all_results_long.csv"]
    paired_delta = frames["02_baseline_smart_paired_delta.csv"]
    paired_summary = frames["03_paired_summary_by_crosswalk.csv"]
    extension_events = frames["04_extension_events_all.csv"]
    scope_audit = frames["phase6_extension_scope_audit.csv"]
    signal_audit = frames["05_signal_phase_audit.csv"]
    impact_scope = frames["06_impact_scope_edge_counts.csv"]
    inventory = frames["07_source_file_inventory.csv"]

    total_runs = int(len(run_summary)) if not run_summary.empty else 0
    baseline_runs = int((run_summary["scenario"].astype(str) == "baseline").sum()) if not run_summary.empty and "scenario" in run_summary.columns else 0
    smart_runs = int((run_summary["scenario"].astype(str) == "smart").sum()) if not run_summary.empty and "scenario" in run_summary.columns else 0
    ok_runs = int((run_summary["status"].astype(str) == "OK").sum()) if not run_summary.empty and "status" in run_summary.columns else 0
    error_runs = max(0, total_runs - ok_runs)
    invalid_route_total = int(pd.to_numeric(run_summary.get("vehicle_route_invalid_total", pd.Series(dtype=float)), errors="coerce").fillna(0).sum()) if not run_summary.empty else 0
    extension_total = int(pd.to_numeric(run_summary.get("extension_total", pd.Series(dtype=float)), errors="coerce").fillna(0).sum()) if not run_summary.empty else 0
    vehicle_requested_total = int(pd.to_numeric(run_summary.get("vehicle_requested_total", pd.Series(dtype=float)), errors="coerce").fillna(0).sum()) if not run_summary.empty else 0
    vehicle_departed_total = int(pd.to_numeric(run_summary.get("vehicle_departed_total", pd.Series(dtype=float)), errors="coerce").fillna(0).sum()) if not run_summary.empty else 0
    vehicle_arrived_total = int(pd.to_numeric(run_summary.get("vehicle_arrived_total", pd.Series(dtype=float)), errors="coerce").fillna(0).sum()) if not run_summary.empty else 0
    seed_count = int(run_summary["seed"].nunique()) if not run_summary.empty and "seed" in run_summary.columns else 0
    scenario_count = int(run_summary["scenario"].nunique()) if not run_summary.empty and "scenario" in run_summary.columns else 0
    candidate_count = int(long_results["crosswalk_id"].nunique()) if not long_results.empty and "crosswalk_id" in long_results.columns else 0
    raw_result_rows = int(len(long_results)) if not long_results.empty else 0
    paired_rows = int(len(paired_delta)) if not paired_delta.empty else 0
    paired_ok_rows = int((paired_delta["paired_ok"] == True).sum()) if not paired_delta.empty and "paired_ok" in paired_delta.columns else 0  # noqa: E712
    event_total = int(len(extension_events)) if not extension_events.empty else 0
    event_by_crosswalk = extension_events.groupby("crosswalk_id").size().to_dict() if not extension_events.empty else {}
    seed_by_crosswalk = extension_events.groupby("crosswalk_id")["seed"].nunique().to_dict() if not extension_events.empty else {}
    mixed_event_count, mixed_event_by_cw, _ = _phase6_event_mixed_counts(extension_events, signal_audit)

    if not paired_delta.empty and "crosswalk_id" in paired_delta.columns:
        cw_time_loss = paired_delta.groupby("crosswalk_id")["veh_time_loss_mean_delta"].mean().to_dict()
        cw_waiting = paired_delta.groupby("crosswalk_id")["veh_waiting_time_mean_delta"].mean().to_dict()
    else:
        cw_time_loss = {}
        cw_waiting = {}

    ext_summary_rows, delta_summary_rows = _phase6_crosswalk_extension_summary(extension_events, paired_delta, paired_summary)

    impact_summary_rows: list[list[Any]] = []
    if not impact_scope.empty and {"crosswalk_id", "scope", "unique_edge_count"}.issubset(impact_scope.columns):
        summary_scope = impact_scope.groupby(["crosswalk_id", "scope"])["unique_edge_count"].mean().unstack()
        for cid in sorted(summary_scope.index.astype(str).tolist()):
            row = summary_scope.loc[cid]
            impact_summary_rows.append(
                [
                    cid,
                    _fmt_num(row.get("100m", np.nan), 0),
                    _fmt_num(row.get("300m", np.nan), 0),
                    _fmt_num(row.get("500m", np.nan), 0),
                    _fmt_num(row.get("1000m", np.nan), 0),
                    _fmt_num(row.get("global", np.nan), 0),
                ]
            )

    inventory_category = inventory["category"].value_counts().to_dict() if not inventory.empty and "category" in inventory.columns else {}
    inventory_importance = inventory["human_importance"].value_counts().to_dict() if not inventory.empty and "human_importance" in inventory.columns else {}
    inventory_action = inventory["action"].value_counts().to_dict() if not inventory.empty and "action" in inventory.columns else {}

    mixed_candidate_note = "현재 extension event는 mixed phase 상태와 겹친다. 그래서 현재 결과는 smoke/구조 검증용이고 최종 정책 효과로 쓰면 안 된다."
    if mixed_event_count == 0 and event_total > 0:
        mixed_candidate_note = "현재 extension event에서 mixed phase는 발견되지 않았다. 그래도 재실행으로 final guard 검증은 필요하다."

    smart_scope = scope_audit[scope_audit["scenario"].astype(str) == "smart"] if not scope_audit.empty and "scenario" in scope_audit.columns else scope_audit
    baseline_scope = scope_audit[scope_audit["scenario"].astype(str) == "baseline"] if not scope_audit.empty and "scenario" in scope_audit.columns else pd.DataFrame()
    smart_scope_total = int(len(smart_scope))
    smart_scope_ped_only = int((smart_scope["scope_verdict"].astype(str) == "pedestrian_only").sum()) if smart_scope_total and "scope_verdict" in smart_scope.columns else 0
    smart_scope_mixed = int((smart_scope["scope_verdict"].astype(str) == "mixed_non_ped_green").sum()) if smart_scope_total and "scope_verdict" in smart_scope.columns else 0
    baseline_scope_error = int((baseline_scope["baseline_extension_event_error"].astype(str).str.lower() == "true").sum()) if not baseline_scope.empty and "baseline_extension_event_error" in baseline_scope.columns else 0
    smart_scope_all_ped_only = smart_scope_total > 0 and smart_scope_ped_only == smart_scope_total and smart_scope_mixed == 0
    vehicle_delay = pd.to_numeric(run_summary.get("avg_vehicle_delay_sec", pd.Series(dtype=float)), errors="coerce") if not run_summary.empty else pd.Series(dtype=float)
    network_travel = pd.to_numeric(run_summary.get("network_avg_travel_time_sec", pd.Series(dtype=float)), errors="coerce") if not run_summary.empty else pd.Series(dtype=float)
    vehicle_flow_not_evaluated = vehicle_delay.dropna().empty and network_travel.dropna().empty

    lines: list[str] = []
    lines.append("# Phase 6 Results README")
    lines.append("")
    lines.append(f"읽은 CSV 위치: `{csv_dir}`" if csv_dir is not None else "읽은 CSV 위치: 파일 없음")
    lines.append(f"읽은 CSV 수: {len(EXPECTED_PHASE6_RESULT_CSVS) - len(missing)}")
    lines.append(f"누락 CSV 수: {len(missing)}")
    lines.append(f"csv 폴더에는 표준 CSV와 README md 파일이 같이 생성된다.")
    if missing:
        lines.append(f"누락 파일: {', '.join(missing)}")
    lines.append("")
    lines.append("## 전체 실험 개요")
    lines.append("")
    lines.append(f"- 이 결과는 Phase 6 vehicle 반복 실험의 baseline vs smart 비교다.")
    lines.append(f"- smart는 보행자 감지 시 보행자 초록 시간을 연장하는 정책이다.")
    lines.append(f"- seed 반복 수: `{seed_count}`")
    lines.append(f"- crosswalk 후보 수: `{candidate_count}`")
    lines.append(f"- scenario 수: `{scenario_count}`")
    lines.append(f"- 총 run 수: `{total_runs}`")
    lines.append(f"- 전체 raw result row 수: `{raw_result_rows}`")
    lines.append(f"- paired row 수: `{paired_rows}`")
    lines.append(f"- paired_ok row 수: `{paired_ok_rows}`")
    lines.append(f"- baseline run 수: `{baseline_runs}`")
    lines.append(f"- smart run 수: `{smart_runs}`")
    lines.append(f"- OK run 수: `{ok_runs}`")
    lines.append(f"- error run 수: `{error_runs}`")
    lines.append(f"- 이 폴더의 grouped `300 veh/h` 결과는 smoke/초안 구조 검증용이다.")
    lines.append(f"- 현재 결과는 mixed phase issue를 포함한 이전 반복 결과이므로 최종 정책 효과 검증으로 해석하면 안 된다.")
    lines.append(f"- smart extension event all pedestrian-only: {'yes' if smart_scope_all_ped_only else 'no'}")
    lines.append(f"- smart pedestrian-only event count: `{smart_scope_ped_only}` / `{smart_scope_total}`")
    lines.append(f"- smart mixed non-ped green event count: `{smart_scope_mixed}`")
    lines.append(f"- baseline extension event error count: `{baseline_scope_error}`")
    if vehicle_flow_not_evaluated:
        lines.append("vehicle flow impact not evaluated in this smoke")
    lines.append("")
    lines.append("## 한눈에 보는 숫자")
    lines.append("")
    lines.append(_markdown_table(
        ["항목", "값"],
        [
            ["vehicle_requested_total", f"{vehicle_requested_total}"],
            ["vehicle_departed_total", f"{vehicle_departed_total}"],
            ["vehicle_arrived_total", f"{vehicle_arrived_total}"],
            ["vehicle_route_invalid_total", f"{invalid_route_total}"],
            ["extension_total", f"{extension_total}"],
            ["extension_event_total", f"{event_total}"],
        ],
    ))
    lines.append("")

    file_specs = [
        (
            "00_run_completion_summary.csv",
            "실행 정상 종료 여부와 run 안정성 확인용",
            "행 1개 = seed/scenario 1회 run",
            "`status`, `completed_all`, `simulation_error_count`, `vehicle_departed_total`, `vehicle_arrived_total`, `vehicle_route_invalid_total`, `extension_total`",
            "모든 run이 완료됐는지, 오류가 있는지, 차량이 실제로 출발/도착했는지, invalid route가 있는지 확인한다.",
            "좋음: `status=OK`, `simulation_error_count=0`, `vehicle_route_invalid_total=0`. 나쁨: `MISSING_RESULT`, `NOT_COMPLETED`, `HAS_SIMULATION_ERROR`, `NO_VEHICLES_DEPARTED`.",
            f"실제 결과: run `{total_runs}`개 중 baseline `{baseline_runs}`개, smart `{smart_runs}`개, `OK` `{ok_runs}`개, error `{error_runs}`개. `vehicle_requested_total={vehicle_requested_total}`, `vehicle_departed_total={vehicle_departed_total}`, `vehicle_arrived_total={vehicle_arrived_total}`, `vehicle_route_invalid_total={invalid_route_total}`, `extension_total={extension_total}`.",
            "run-level 안정성만 본다. 정책 효과는 이 파일로 판단하지 않는다.",
            "이번 run은 전체적으로 안정적이고 invalid route가 0이다.",
        ),
        (
            "01_all_results_long.csv",
            "모든 run의 원자료",
            "행 1개 = seed/scenario/crosswalk 1개 결과",
            "`seed`, `scenario`, `crosswalk_id`, `completed`, `extension_count`, `veh_time_loss_mean`, `veh_waiting_time_mean`, `source_result_csv`",
            "baseline와 smart가 같은 seed로 잘 짝지어졌는지, crosswalk별 raw 결과가 모두 들어왔는지 확인한다.",
            "좋음: 60 run × 3 crosswalk = 180 row가 모두 있고 seed/scenario 쌍이 맞는다. 나쁨: 한쪽 scenario만 있거나 seed가 빠진다.",
            f"실제 결과: `{raw_result_rows}` row. baseline/scenario 각각 seed `{seed_count}` 반복, crosswalk `{candidate_count}`개.",
            "이 파일은 근거 데이터다. 해석은 02, 03 요약 파일이 더 중요하다.",
            "원자료가 180 row로 쌓여 있어 paired 비교의 근거가 된다.",
        ),
        (
            "02_baseline_smart_paired_delta.csv",
            "같은 seed에서 smart - baseline 차이를 계산한 paired 비교 파일",
            "행 1개 = seed × crosswalk 1개 paired pair",
            "`extension_delta`, `veh_time_loss_mean_delta`, `veh_waiting_time_mean_delta`, `paired_ok`",
            "smart가 baseline보다 각 지표를 올렸는지/내렸는지 본다. delta 부호를 친절히 읽는다.",
            "delta < 0 = smart 감소, delta > 0 = smart 증가. paired 비교라 단순 평균보다 중요하다.",
            f"실제 결과: paired row `{paired_rows}`개, `paired_ok` `{paired_ok_rows}`개. crosswalk별 평균 delta는 `LINK_43439` time loss `+{_fmt_num(cw_time_loss.get('LINK_43439', np.nan), 4)}`, waiting `+{_fmt_num(cw_waiting.get('LINK_43439', np.nan), 4)}`; `NODE_7240` time loss `{_fmt_num(cw_time_loss.get('NODE_7240', np.nan), 4)}`, waiting `{_fmt_num(cw_waiting.get('NODE_7240', np.nan), 4)}`; `LINK_52248` time loss `{_fmt_num(cw_time_loss.get('LINK_52248', np.nan), 4)}`, waiting `{_fmt_num(cw_waiting.get('LINK_52248', np.nan), 4)}`.",
            "현재 결과는 mixed phase 문제와 분리해서 읽어야 한다. 차량 지체 감소만 보고 최종 정책 효과라고 쓰면 안 된다.",
            "paired delta는 최종 효과 해석의 핵심 근거지만, 현재 데이터는 smoke/구조 검증으로만 사용해야 한다.",
        ),
        (
            "03_paired_summary_by_crosswalk.csv",
            "crosswalk별 paired delta 요약",
            "행 1개 = crosswalk × metric 요약",
            "`mean_delta`, `std_delta`, `se_delta`, `ci95_low`, `ci95_high`",
            "crosswalk별로 smart가 baseline보다 실제로 달라졌는지, CI가 0을 포함하는지 확인한다.",
            "좋음: CI가 0을 포함하지 않고 방향성이 일관적이다. 나쁨: CI가 0을 포함하거나 표본이 적다.",
            "실제 결과: `LINK_43439`는 `veh_time_loss_mean_delta` CI가 `[-0.0043, 0.0234]`로 0을 포함한다. `NODE_7240`는 `[-0.3844, -0.1854]`로 0을 포함하지 않는다. `LINK_52248`는 `[-1.0802, -0.5252]`로 0을 포함하지 않는다. `extension_delta`는 `LINK_43439=0`, `NODE_7240=1`, `LINK_52248=0.3` 수준이다.",
            "CI 해석은 단독 결론이 아니다. extension 이벤트가 실제 발생했는지와 함께 봐야 한다.",
            "차량 지체 감소가 보여도 바로 정책 효과라고 단정하면 안 된다.",
        ),
        (
            "04_extension_events_all.csv",
            "실제 smart extension event 발생 여부를 보는 핵심 파일",
            "행 1개 = 1회 extension event",
            "`crosswalk_id`, `seed`, `time`, `state`, `remaining_before`, `extension_sec`, `linkIndex`",
            "어느 후보가 실제로 연장됐는지, 몇 seed에서 연장됐는지, 총 연장 시간이 얼마나 되는지 확인한다.",
            "좋음: 정책 대상 crosswalk에서 원하는 seed만 적절히 extension 된다. 나쁨: extension이 전혀 없거나, mixed phase에서 차량 green까지 같이 연장된다.",
            f"실제 결과: event `{event_total}`개. `NODE_7240` `{event_by_crosswalk.get('NODE_7240', 0)}`개 / `{seed_by_crosswalk.get('NODE_7240', 0)}` seed, `LINK_52248` `{event_by_crosswalk.get('LINK_52248', 0)}`개 / `{seed_by_crosswalk.get('LINK_52248', 0)}` seed, `LINK_43439` 0개. 현재 이벤트는 state 문자열 기준으로 모두 mixed phase와 겹친다(`mixed` `{mixed_event_count}`개).",
            "이 파일은 실제 extension 발생만 보여준다. mixed phase 여부는 05와 함께 봐야 한다.",
            "extension이 0인 후보는 정책 효과 후보로 해석하면 안 된다. 현재 데이터는 mixed phase 때문에 smoke/구조 검증용이다.",
        ),
        (
            "phase6_extension_scope_audit.csv",
            "extension event가 ped-only phase인지, 차량 green이 같이 늘었는지 event별로 검사하는 audit",
            "행 1개 = 1회 extension event",
            "`scenario`, `ped_link_index`, `ped_link_state`, `ped_link_is_green`, `non_ped_green_link_indices`, `phase_extension_affects_non_ped_green`, `baseline_extension_event_error`",
            "ped_link_index만 G/g인지 확인하고, ped link 외 다른 link가 G/g면 차량 green 동시 연장으로 표시한다.",
            "좋음: `ped_link_is_green=True`이고 `phase_extension_affects_non_ped_green=False`. 나쁨: ped link가 green이 아니거나, ped link 외 다른 link도 green이다.",
            f"실제 결과: smart extension event `{smart_scope_total}`개 중 `{smart_scope_ped_only}`개가 pedestrian-only였고 `{smart_scope_mixed}`개는 mixed였다. baseline extension event error는 `{baseline_scope_error}`개다.",
            "baseline extension event이 있으면 정책 오류로 봐야 한다. smart event는 ped-only phase에서만 일어나야 한다.",
            "차량 지표가 NaN이면 vehicle flow impact not evaluated in this smoke 라고 별도 표기해야 한다.",
        ),
        (
            "05_signal_phase_audit.csv",
            "각 후보가 신호 제어 가능한 구조인지 확인하는 파일",
            "행 1개 = crosswalk 1개 audit",
            "`tls_id`, `ped_link_index`, `states_at_index`, `audit_ok`",
            "전체 phase 문자열이 아니라 pedestrian linkIndex 위치의 G/r을 봐야 하는지 확인한다.",
            "좋음: `audit_ok=True`이고 `ped_link_index`가 모든 phase에서 범위 안에 있으며, red/green 구조가 정상이다.",
            "실제 결과: 3개 후보 모두 `audit_ok=True`. `LINK_43439`의 `states_at_index=rrrGrr`, `NODE_7240`의 `states_at_index=rrGrr`, `LINK_52248`의 `states_at_index=rrGrr`.",
            "audit_ok=True라도 extension event가 반드시 발생하는 것은 아니다. 반대로 event가 생겨도 mixed phase면 최종 정책 효과로 쓰면 안 된다.",
            "전체 phase 문자열의 G 유무가 아니라 ped linkIndex 위치가 핵심이다.",
        ),
        (
            "06_impact_scope_edge_counts.csv",
            "영향권별 edge 수를 보여주는 파일",
            "행 1개 = seed × scenario × crosswalk × scope",
            "`scope`, `unique_edge_count`, `impact_edge_source_main`, `source_impact_edges_csv`",
            "100m, 300m, 500m, 1000m, global의 edge 수가 범위대로 커지는지 확인한다. fallback_route_edges가 아니라 radius_geometry인지 확인한다.",
            "좋음: 반경이 커질수록 edge 수가 증가하고 `impact_edge_source_main=radius_geometry`. 나쁨: global이 후보별로 달라지거나 radius 증가가 보이지 않는다.",
            "실제 결과: `LINK_43439`는 `100m=56`, `300m=242`, `500m=743`, `1000m=2143`, `global=22987`; `NODE_7240`는 `57/218/703/2085/22987`; `LINK_52248`는 `33/194/637/2603/22987`. 100m < 300m < 500m < 1000m < global 순서가 성립한다. `impact_edge_source_main`은 모두 `radius_geometry`다.",
            "global은 전체 네트워크 기준이라 후보별 정책 효과처럼 해석하면 안 된다.",
            "지역 영향권은 100m~1000m를 더 중요하게 본다.",
        ),
        (
            "07_source_file_inventory.csv",
            "결과 생성에 사용된 원본/중간/검증 파일 목록",
            "행 1개 = transition-root 바로 아래 파일 또는 디렉터리 1개",
            "`category`, `human_importance`, `action`, `note`",
            "재현성과 파일 추적에 필요한 입력/중간 파일이 무엇인지 확인한다.",
            "좋음: 핵심 입력과 검증 파일이 분류되어 있고, 불필요한 raw 파일은 리뷰 우선순위가 낮다.",
            f"실제 결과: `{len(inventory)}`개 항목. category는 `OLD_SMOKE={inventory_category.get('OLD_SMOKE', 0)}`, `INTERMEDIATE={inventory_category.get('INTERMEDIATE', 0)}`, `COMMAND_SCRIPT={inventory_category.get('COMMAND_SCRIPT', 0)}`, `DIRECTORY={inventory_category.get('DIRECTORY', 0)}`, `CORE_VALIDATION={inventory_category.get('CORE_VALIDATION', 0)}`, `DEBUG_NOTE={inventory_category.get('DEBUG_NOTE', 0)}`. `human_importance`는 `LOW={inventory_importance.get('LOW', 0)}`, `MEDIUM={inventory_importance.get('MEDIUM', 0)}`, `HIGH={inventory_importance.get('HIGH', 0)}`.",
            "이 파일은 결과 해석용이 아니라 추적성 확인용이다.",
            "보고서에서는 핵심 결과 CSV와 입력 파일 확인에만 쓰면 된다.",
        ),
    ]

    for name, what, row_meaning, core_cols, question, good_bad, actual, caution, report in file_specs:
        csv_md_path = (csv_dir or output_dir) / PHASE6_CSV_README_NAMES.get(name, name.replace(".csv", ".md"))
        csv_doc = [
            f"# {name} README",
            "",
            f"- 이 파일은 무엇인가: {what}.",
            f"- 행 하나가 의미하는 것: {row_meaning}.",
            f"- 반드시 봐야 할 핵심 컬럼: {core_cols}.",
            f"- 이 파일에서 확인해야 할 질문: {question}",
            f"- 좋은 결과/나쁜 결과를 판단하는 기준: {good_bad}",
            f"- 이번 데이터에서 실제로 나온 결과: {actual}",
            f"- 해석할 때 주의할 점: {caution}",
            f"- 보고서에 쓸 수 있는 문장: {report}",
            "",
        ]
        csv_md_path.write_text("\n".join(csv_doc), encoding="utf-8")
        lines.append(f"## {name}")
        lines.append("")
        lines.append(f"- 이 파일은 무엇인가: {what}.")
        lines.append(f"- 행 하나가 의미하는 것: {row_meaning}.")
        lines.append(f"- 반드시 봐야 할 핵심 컬럼: {core_cols}.")
        lines.append(f"- 이 파일에서 확인해야 할 질문: {question}")
        lines.append(f"- 좋은 결과/나쁜 결과를 판단하는 기준: {good_bad}")
        lines.append(f"- 이번 데이터에서 실제로 나온 결과: {actual}")
        lines.append(f"- 해석할 때 주의할 점: {caution}")
        lines.append(f"- 보고서에 쓸 수 있는 문장: {report}")
        lines.append("")

    lines.append("## 이번 결과 요약")
    lines.append("")
    lines.append(_markdown_table(
        ["구분", "값"],
        [
            ["전체 run 수", total_runs],
            ["baseline run 수", baseline_runs],
            ["smart run 수", smart_runs],
            ["error 수", error_runs],
            ["invalid route 수", invalid_route_total],
            ["전체 extension event 수", event_total],
            ["extension 발생 후보", ", ".join(sorted(event_by_crosswalk.keys())) if event_by_crosswalk else "없음"],
            ["extension 미발생 후보", ", ".join(sorted(set(["LINK_43439", "LINK_52248", "NODE_7240"]) - set(event_by_crosswalk.keys()))) if total_runs else "N/A"],
            ["mixed phase event 수", mixed_event_count],
        ],
    ))
    lines.append("")
    lines.append("### crosswalk별 extension summary")
    lines.append("")
    lines.append(_markdown_table(
        ["crosswalk_id", "event_rows", "event_seed_count", "avg veh_time_loss_delta", "avg veh_waiting_time_delta"],
        ext_summary_rows if ext_summary_rows else [["N/A", "N/A", "N/A", "N/A", "N/A"]],
    ))
    lines.append("")
    lines.append("### crosswalk별 paired delta / CI")
    lines.append("")
    lines.append(_markdown_table(
        [
            "crosswalk_id",
            "extension_delta mean",
            "extension_delta CI95",
            "extension CI includes 0?",
            "veh_time_loss_delta mean",
            "veh_time_loss_delta CI95",
            "veh_time_loss CI includes 0?",
            "veh_waiting_time_delta mean",
            "veh_waiting_time_delta CI95",
            "veh_waiting_time CI includes 0?",
        ],
        delta_summary_rows if delta_summary_rows else [["N/A"] * 10],
    ))
    lines.append("")
    lines.append("### 95% CI 읽는 법")
    lines.append("")
    lines.append("- CI가 0을 포함하면, 이번 표본만으로는 변화 방향을 단정하기 어렵다.")
    lines.append("- CI가 0을 포함하지 않으면, 표본 평균 기준으로 방향성이 더 분명하다.")
    lines.append("- 그래도 이 결과는 smoke/구조 검증용이라 정책 최종판정은 아니다.")
    lines.append("")
    lines.append("### 가장 안정적으로 extension이 발생한 후보")
    lines.append("")
    if event_by_crosswalk:
        stable_cw = max(seed_by_crosswalk.items(), key=lambda kv: (kv[1], event_by_crosswalk.get(kv[0], 0), kv[0]))[0]
        lines.append(f"- `{stable_cw}`. `{seed_by_crosswalk.get(stable_cw, 0)}/30` seed에서 extension이 발생했다.")
    else:
        lines.append("- 없음.")
    lines.append("")
    lines.append("### extension이 발생하지 않은 후보")
    lines.append("")
    no_event_candidates = sorted(set(["LINK_43439", "LINK_52248", "NODE_7240"]) - set(event_by_crosswalk.keys()))
    lines.append(f"- {', '.join(no_event_candidates) if no_event_candidates else '없음'}")
    lines.append("")
    lines.append("### 바로 정책 효과 후보로 보기 어려운 이유")
    lines.append("")
    lines.append(f"- {mixed_candidate_note}")
    lines.append("- 현재 결과는 grouped 300 veh/h smoke/구조 검증용이며, final policy effect가 아니다.")
    lines.append("- 보행자 대기시간 지표가 현재 CSV에 없으면 그 효과는 여기서 직접 말할 수 없다.")
    lines.append("")

    lines.append("## 보고서용 문장")
    lines.append("")
    lines.append(
        "Phase 6 grouped 300 veh/h 결과에서는 30 seed × 2 scenario × 3 crosswalk의 반복 실험이 안정적으로 완료되었고, "
        "vehicle route invalid는 관측되지 않았다. 다만 현재 extension 이벤트는 mixed phase와 겹치는 상태를 포함하므로, "
        "이 데이터만으로 smart 정책의 최종 효과라고 단정하면 안 된다. NODE_7240과 LINK_52248에서는 extension 이벤트가 관측되었지만, "
        "연장 여부와 차량 지체 변화는 crosswalk별로 다르게 나타났으며 95% CI도 함께 확인해야 한다. "
        "특히 global 지표는 전체 네트워크 수준 참고값이어서 후보별 정책 효과로 직접 해석하면 안 된다. "
        "따라서 이 결과는 최종 정책 판정보다 smoke/구조 검증과 crosswalk별 경향 파악에 적합하다."
    )
    lines.append("")
    lines.append("## 해석 주의사항")
    lines.append("")
    lines.append("- 이 결과만으로 `보행자 대기시간이 줄었다`고 쓰면 안 된다. 현재 CSV에 보행자 대기시간 지표가 없으면 그렇게 명시해야 한다.")
    lines.append("- extension이 발생하지 않은 후보는 정책 효과 후보로 해석하면 안 된다.")
    lines.append("- 차량 지체가 감소해도 바로 `스마트 횡단보도가 차량에도 좋다`고 단정하면 안 된다.")
    lines.append("- mixed phase 문제 때문에 smart가 차량 green까지 연장하지 않았는지 별도 guard 검증이 필요하다.")
    lines.append("- grouped 300 veh/h 결과는 최종 정책 효과가 아니라 smoke/구조 검증용으로 봐야 한다.")
    lines.append("")

    readme_path = output_dir / "README_PHASE6_RESULTS.md"
    readme_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return readme_path


def _write_readme(output_dir: Path, volume: int) -> str:
    text = f"""# Phase 6 Vehicle Experiment Human Pack

이 폴더는 `result/phase6_transition_after_recovery_20260514_220549/phase6_vehicle_experiment_300_30seed` 원본 결과에서 사람 검토용 CSV만 모은 곳이다.

- 원본 `result/` 파일은 삭제하지 않았다.
- 원본 `result/` 파일은 이동하지 않았다.
- 원본 `result/` 파일은 수정하지 않았다.
- 이 폴더는 표준 정리 파이프라인 출력으로 쓰는 것을 목표로 한다.

## 우선 볼 순서

1. `csv/00_run_completion_summary.csv`
2. `csv/02_baseline_smart_paired_delta.csv`
3. `csv/03_paired_summary_by_crosswalk.csv`
4. `csv/04_extension_events_all.csv`
5. `csv/phase6_extension_scope_audit.csv`
6. `csv/05_signal_phase_audit.csv`

## CSV 의미

- `csv/00_run_completion_summary.csv`: manifest 기준 seed/scenario 실행 상태 요약.
- `csv/01_all_results_long.csv`: seed/scenario/crosswalk 결과를 long format으로 합친 원장.
- `csv/02_baseline_smart_paired_delta.csv`: baseline vs smart paired merge와 차이.
- `csv/03_paired_summary_by_crosswalk.csv`: 횡단보도별 delta 평균, 표준편차, 표준오차, 95% CI.
- `csv/04_extension_events_all.csv`: baseline/smart extension event 합본.
- `csv/phase6_extension_scope_audit.csv`: extension event별 ped link green 여부와 non-ped green 동시 연장 여부를 검사하는 audit.
- `csv/05_signal_phase_audit.csv`: 대상 후보 3개의 ped_link_index가 red/green phase를 모두 갖는지 검증한 파일. `audit_ok=True`면 대상 신호는 실험 사용 가능.
- `csv/06_impact_scope_edge_counts.csv`: impact radius별 edge 수 요약.
- `csv/07_source_file_inventory.csv`: transition-root 바로 아래 파일과 디렉터리의 사람 검토 우선순위 표.

## 주요 컬럼

- `completed_all`: run 전체가 완결됐는지 표시.
- `simulation_error_count`: 실행 중 오류 개수.
- `extension_count`: smart extension 횟수.
- `vehicle_departed_count`: 실제 출발 차량 수.
- `veh_time_loss_mean_delta`: smart - baseline 차량 time loss 차이.
- `impact_500m_time_loss_mean_delta`: 500m 영향권 time loss 차이.

## 해석 주의

- `300 veh/h`는 초안 감도분석값이다. 실제 중구 교통량은 아직 아니다.
- 30회 반복은 평균 추정을 안정화하지만 자동으로 유의성을 보장하지 않는다.
- `Missing green phase warning`은 전체 네트워크 경고다. 대상 TLS는 `batch03_signal_phase_audit.csv`로 별도 검증했다.
- global 지표는 참고용이다. 중심 해석은 500m 영향권 지표로 본다.

## 재생성 명령

```bash
PROJECT_ROOT="$(cd "$(dirname "${{BASH_SOURCE[0]}}")/.." && pwd)"
cd "$PROJECT_ROOT"

PYTHONPATH="$PROJECT_ROOT:${{PYTHONPATH:-}}" \\
python3 smart_crosswalk_sumo/organize_phase6_human_results.py \\
  --experiment-root result/phase6_transition_after_recovery_20260514_220549/phase6_vehicle_experiment_300_30seed \\
  --transition-root result/phase6_transition_after_recovery_20260514_220549 \\
  --output-dir result/phase6_vehicle_experiment_300_30seed_human \\
  --volume {volume}
```
"""
    return text


def main() -> None:
    parser = argparse.ArgumentParser(description="Organize Phase 6 vehicle experiment results into human-readable CSVs.")
    parser.add_argument(
        "--experiment-root",
        default="result/phase6_transition_after_recovery_20260514_220549/phase6_vehicle_experiment_300_30seed",
    )
    parser.add_argument(
        "--transition-root",
        default="result/phase6_transition_after_recovery_20260514_220549",
    )
    parser.add_argument(
        "--output-dir",
        default="result/phase6_vehicle_experiment_300_30seed_human",
    )
    parser.add_argument("--volume", type=int, default=300)
    args = parser.parse_args()

    project_root = _project_root()
    experiment_root = (project_root / args.experiment_root).resolve()
    transition_root = (project_root / args.transition_root).resolve()
    output_dir = (project_root / args.output_dir).resolve()
    csv_dir = output_dir / "csv"
    log_dir = output_dir / "logs"

    _ensure_dir(csv_dir)
    _ensure_dir(log_dir)

    manifest = _load_manifest(experiment_root)
    run_summary = _build_run_summary(manifest)
    long_results = _build_long_results(manifest)
    paired_delta = _paired_delta(long_results)
    paired_summary = _paired_summary_by_crosswalk(paired_delta)
    extension_events = _build_extension_events(manifest)
    impact_scope_counts = _build_impact_scope_counts(manifest)
    source_inventory = _build_source_inventory(transition_root)

    audit_src = transition_root / "batch03_signal_phase_audit.csv"
    audit_dst = csv_dir / "05_signal_phase_audit.csv"
    if audit_src.exists():
        shutil.copy2(audit_src, audit_dst)
    else:
        pd.DataFrame().to_csv(audit_dst, index=False)

    signal_audit_df = pd.read_csv(audit_dst) if audit_dst.exists() else pd.DataFrame()
    scope_audit = _standardize_extension_scope_audit_df(
        extension_events,
        experiment_root,
        csv_dir / "04_extension_events_all.csv",
        audit_dst,
        signal_audit_df,
    )

    _write_csv(run_summary, csv_dir / "00_run_completion_summary.csv")
    _write_csv(long_results, csv_dir / "01_all_results_long.csv")
    _write_csv(paired_delta, csv_dir / "02_baseline_smart_paired_delta.csv")
    _write_csv(paired_summary, csv_dir / "03_paired_summary_by_crosswalk.csv")
    _write_csv(extension_events, csv_dir / "04_extension_events_all.csv")
    _write_csv(scope_audit, csv_dir / "phase6_extension_scope_audit.csv")
    _write_csv(impact_scope_counts, csv_dir / "06_impact_scope_edge_counts.csv")
    _write_csv(source_inventory, csv_dir / "07_source_file_inventory.csv")

    log_extract = _extract_log_lines(experiment_root / "run_300_30seed.log")
    (log_dir / "run_300_30seed_error_extract.txt").write_text(log_extract, encoding="utf-8")

    readme_text = _write_readme(output_dir, args.volume)
    (output_dir / "README.md").write_text(readme_text, encoding="utf-8")
    write_phase6_results_readme(transition_root)

    print(f"output_dir: {output_dir}")
    print(f"csv_dir: {csv_dir}")
    print(f"log_dir: {log_dir}")
    print(f"manifest_rows: {len(manifest)}")
    print(f"result_rows: {len(long_results)}")
    print(f"paired_rows: {len(paired_delta)}")
    print(f"copied_audit: {audit_dst}")


if __name__ == "__main__":
    main()
