#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

import numpy as np
import pandas as pd

try:
    from .network_utils import GLOBAL_NETWORK_SCOPE, TARGET_CROSSWALK_SCOPE
except ImportError:
    from network_utils import GLOBAL_NETWORK_SCOPE, TARGET_CROSSWALK_SCOPE


STANDARD_CSVS = [
    "00_run_completion_summary.csv",
    "01_all_results_long.csv",
    "02_baseline_smart_paired_delta.csv",
    "03_paired_summary_by_crosswalk.csv",
    "04_extension_events_all.csv",
    "05_signal_phase_audit.csv",
    "06_impact_scope_edge_counts.csv",
    "07_source_file_inventory.csv",
    "phase6_extension_scope_audit.csv",
]

STANDARD_READMES = [name.replace(".csv", "_README.md") for name in STANDARD_CSVS]

METRIC_COLUMNS = [
    "PET_A_proxy_count",
    "PET_A_proxy_mean",
    "PET_A_proxy_severe",
    "PET_A_proxy_moderate",
    "PET_A_proxy_safe",
    "PET_B_surrogate_count",
    "PET_B_surrogate_mean",
    "PET_B_surrogate_severe",
    "PET_B_surrogate_moderate",
    "PET_B_surrogate_safe",
    "safety_risk_score",
    "accident_expected_value",
    "elderly_incomplete_crossings",
    "safety_risk_delta",
    "accident_expected_delta",
    "elderly_incomplete_crossings_delta",
]

STANDARD_KEEP_NAMES = set(STANDARD_CSVS + STANDARD_READMES + ["README_PHASE6_RESULTS.md", "run_metadata.json"])

STRUCTURED_DEBUG_CSVS = [
    "route_generation_audit.csv",
    "pedestrian_route_connectivity_audit.csv",
    "signal_phase_audit.csv",
    "skipped_pedestrian_routes.csv",
    "source_file_inventory.csv",
]

STRUCTURED_RESULT_CSVS = [
    "00_run_completion_summary.csv",
    "01_global_traffic_results_long.csv",
    "02_target_crosswalk_safety_results_long.csv",
    "03_baseline_smart_paired_delta.csv",
    "04_summary_by_crosswalk.csv",
    "05_extension_events_all.csv",
    "06_benefit_input_table.csv",
]

STRUCTURED_SAFETY_CSVS = [
    "safety_metrics_by_run.csv",
    "safety_paired_delta.csv",
    "safety_summary_by_crosswalk.csv",
]

STANDARD_CSV_SCHEMAS: dict[str, list[str]] = {
    "00_run_completion_summary.csv": [
        "run_name",
        "seed",
        "scenario",
        "source_dir",
        "output_dir",
        "run_start_time",
        "run_end_time",
        "elapsed_sec",
        "completed",
        "sim_duration",
        "warmup",
        "step_length",
        "run_completed",
        "error_message",
        "extension_count",
        "total_extension_sec",
        "arrived_vehicles",
        "network_arrived_vehicles",
        "avg_vehicle_delay_sec",
        "avg_queue_length",
        "max_queue_length",
        "network_avg_travel_time_sec",
        "safety_risk_score",
        "accident_expected_value",
        "elderly_incomplete_crossings",
        "source_summary_csv",
    ],
    "01_all_results_long.csv": [
        "run_name",
        "seed",
        "scenario",
        "crosswalk_id",
        "output_dir",
        "run_start_time",
        "run_end_time",
        "elapsed_sec",
        "completed",
        "sim_duration",
        "warmup",
        "step_length",
        "source_crosswalk_id",
        "canonical_crosswalk_id",
        "registry_crosswalk_id",
        "tls_id",
        "ped_link_index",
        "extension_count",
        "total_extension_sec",
        "avg_ped_wait_sec",
        "avg_vehicle_delay_sec",
        "avg_queue_length",
        "max_queue_length",
        "network_avg_travel_time_sec",
        "PET_A_proxy_count",
        "PET_A_proxy_mean",
        "PET_A_proxy_severe",
        "PET_A_proxy_moderate",
        "PET_A_proxy_safe",
        "PET_B_surrogate_count",
        "PET_B_surrogate_mean",
        "PET_B_surrogate_severe",
        "PET_B_surrogate_moderate",
        "PET_B_surrogate_safe",
        "safety_risk_score",
        "accident_expected_value",
        "elderly_incomplete_crossings",
        "source_result_csv",
        "source_dir",
    ],
    "02_baseline_smart_paired_delta.csv": [
        "seed",
        "crosswalk_id",
        "paired_ok",
        "extension_count_baseline",
        "total_extension_sec_baseline",
        "avg_ped_wait_sec_baseline",
        "avg_vehicle_delay_sec_baseline",
        "avg_queue_length_baseline",
        "max_queue_length_baseline",
        "network_avg_travel_time_sec_baseline",
        "PET_A_proxy_count_baseline",
        "PET_A_proxy_mean_baseline",
        "PET_A_proxy_severe_baseline",
        "PET_A_proxy_moderate_baseline",
        "PET_A_proxy_safe_baseline",
        "PET_B_surrogate_count_baseline",
        "PET_B_surrogate_mean_baseline",
        "PET_B_surrogate_severe_baseline",
        "PET_B_surrogate_moderate_baseline",
        "PET_B_surrogate_safe_baseline",
        "safety_risk_score_baseline",
        "accident_expected_value_baseline",
        "elderly_incomplete_crossings_baseline",
        "extension_count_smart",
        "total_extension_sec_smart",
        "avg_ped_wait_sec_smart",
        "avg_vehicle_delay_sec_smart",
        "avg_queue_length_smart",
        "max_queue_length_smart",
        "network_avg_travel_time_sec_smart",
        "PET_A_proxy_count_smart",
        "PET_A_proxy_mean_smart",
        "PET_A_proxy_severe_smart",
        "PET_A_proxy_moderate_smart",
        "PET_A_proxy_safe_smart",
        "PET_B_surrogate_count_smart",
        "PET_B_surrogate_mean_smart",
        "PET_B_surrogate_severe_smart",
        "PET_B_surrogate_moderate_smart",
        "PET_B_surrogate_safe_smart",
        "safety_risk_score_smart",
        "accident_expected_value_smart",
        "elderly_incomplete_crossings_smart",
        "extension_count_delta",
        "total_extension_sec_delta",
        "avg_ped_wait_sec_delta",
        "avg_vehicle_delay_sec_delta",
        "avg_queue_length_delta",
        "max_queue_length_delta",
        "network_avg_travel_time_sec_delta",
        "PET_A_proxy_count_delta",
        "PET_A_proxy_mean_delta",
        "PET_A_proxy_severe_delta",
        "PET_A_proxy_moderate_delta",
        "PET_A_proxy_safe_delta",
        "PET_B_surrogate_count_delta",
        "PET_B_surrogate_mean_delta",
        "PET_B_surrogate_severe_delta",
        "PET_B_surrogate_moderate_delta",
        "PET_B_surrogate_safe_delta",
        "safety_risk_score_delta",
        "accident_expected_delta",
        "elderly_incomplete_crossings_delta",
    ],
    "03_paired_summary_by_crosswalk.csv": [
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
    ],
    "04_extension_events_all.csv": [
        "seed",
        "scenario",
        "time",
        "crosswalk_id",
        "tls_id",
        "ped_link_index",
        "extension_sec",
        "detected_ped_count",
        "source_dir",
    ],
    "05_signal_phase_audit.csv": [
        "crosswalk_id",
        "tls_id",
        "ped_link_index",
        "ped_has_green",
        "ped_has_red",
        "pedestrian_only_phase_available",
        "mixed_phase_only",
        "phase_extension_affects_non_ped_green",
        "audit_source",
    ],
    "06_impact_scope_edge_counts.csv": [
        "seed",
        "scenario",
        "crosswalk_id",
        "scope",
        "unique_edge_count",
        "impact_edge_source_main",
        "source_impact_edges_csv",
        "source_dir",
    ],
    "07_source_file_inventory.csv": [
        "file_path",
        "file_name",
        "file_type",
        "size_mb",
        "exists",
        "role",
        "recognized_as_input",
        "used_in_standardization",
        "reason",
        "note",
    ],
    "phase6_extension_scope_audit.csv": [
        "seed",
        "scenario",
        "crosswalk_id",
        "time",
        "phase",
        "tls_id",
        "state",
        "ped_link_index",
        "ped_link_state",
        "ped_link_is_green",
        "non_ped_green_link_indices",
        "non_ped_green_count",
        "phase_extension_affects_non_ped_green",
        "baseline_extension_event_error",
        "extension_sec",
        "detected_ped_count",
        "source_dir",
        "source_event_csv",
        "source_audit_csv",
        "scope_verdict",
    ],
    "route_generation_audit.csv": [
        "crosswalk_id",
        "crossing_edge_id",
        "candidate_from_edge",
        "candidate_to_edge",
        "selected",
        "valid",
        "contains_crossing_edge",
        "generated_route_edges",
        "reason",
    ],
    "pedestrian_route_connectivity_audit.csv": [
        "crosswalk_id",
        "person_id",
        "depart",
        "route_from_edge",
        "route_to_edge",
        "crossing_edge_id",
        "generated_route_edges",
        "contains_crossing_edge",
        "detected_crossing_presence",
        "reason",
    ],
    "skipped_pedestrian_routes.csv": [
        "crosswalk_id",
        "person_id",
        "depart",
        "route_from_edge",
        "route_to_edge",
        "crossing_edge_id",
        "generated_route_edges",
        "contains_crossing_edge",
        "detected_crossing_presence",
        "reason",
        "note",
    ],
    "signal_phase_audit_structured.csv": [
        "crosswalk_id",
        "scenario",
        "seed",
        "tls_id",
        "ped_link_index",
        "crossing_edge_id",
        "route_from_edge",
        "route_to_edge",
        "route_reason",
        "source_file",
        "batch_network_file",
        "final_verdict",
        "step_test_ok",
        "controlled_links_count",
        "crossing_inventory_used",
    ],
    "00_run_completion_summary.csv_structured": [
        "run_name",
        "seed",
        "scenario",
        "source_dir",
        "output_dir",
        "run_start_time",
        "run_end_time",
        "elapsed_sec",
        "completed",
        "sim_duration",
        "warmup",
        "step_length",
        "run_completed",
        "error_message",
        "extension_count",
        "total_extension_sec",
        "arrived_vehicles",
        "network_arrived_vehicles",
        "avg_vehicle_delay_sec",
        "avg_queue_length",
        "max_queue_length",
        "network_avg_travel_time_sec",
        "safety_risk_score",
        "accident_expected_value",
        "elderly_incomplete_crossings",
        "source_summary_csv",
    ],
    "01_global_traffic_results_long.csv": [
        "run_name",
        "target_crosswalk_id",
        "seed",
        "scenario",
        "output_dir",
        "run_start_time",
        "run_end_time",
        "elapsed_sec",
        "completed",
        "sim_duration",
        "warmup",
        "step_length",
        "traffic_scope",
        "network_arrived_vehicles",
        "network_avg_travel_time_sec",
        "avg_ped_wait_sec",
        "avg_vehicle_delay_sec",
        "avg_queue_length",
        "max_queue_length",
        "total_vehicle_delay_sec",
        "total_extension_count",
        "total_extension_sec",
        "simulation_duration_sec",
        "demand_multiplier",
        "install_cost_krw",
    ],
    "02_target_crosswalk_safety_results_long.csv": [
        "run_name",
        "target_crosswalk_id",
        "seed",
        "scenario",
        "output_dir",
        "run_start_time",
        "run_end_time",
        "elapsed_sec",
        "completed",
        "sim_duration",
        "warmup",
        "step_length",
        "traffic_scope",
        "safety_scope",
        "pedestrian_count_near_target",
        "crossing_attempt_count",
        "completed_crossing_count",
        "incomplete_crossing_count",
        "elderly_incomplete_crossings",
        "high_risk_event_count",
        "PET_A_proxy_count",
        "PET_A_proxy_mean",
        "PET_A_proxy_min",
        "PET_A_proxy_severe",
        "PET_A_proxy_moderate",
        "PET_A_proxy_safe",
        "PET_B_surrogate_count",
        "PET_B_surrogate_mean",
        "PET_B_surrogate_min",
        "PET_B_surrogate_severe",
        "PET_B_surrogate_moderate",
        "PET_B_surrogate_safe",
        "safety_risk_score",
        "accident_expected_value",
        "note",
    ],
    "03_baseline_smart_paired_delta.csv_structured": [
        "target_crosswalk_id",
        "seed",
        "paired_ok",
        "traffic_scope",
        "safety_scope",
        "extension_count_baseline",
        "total_extension_sec_baseline",
        "avg_ped_wait_sec_baseline",
        "avg_vehicle_delay_sec_baseline",
        "avg_queue_length_baseline",
        "max_queue_length_baseline",
        "network_avg_travel_time_sec_baseline",
        "PET_A_proxy_count_baseline",
        "PET_A_proxy_mean_baseline",
        "PET_A_proxy_severe_baseline",
        "PET_A_proxy_moderate_baseline",
        "PET_A_proxy_safe_baseline",
        "PET_B_surrogate_count_baseline",
        "PET_B_surrogate_mean_baseline",
        "PET_B_surrogate_severe_baseline",
        "PET_B_surrogate_moderate_baseline",
        "PET_B_surrogate_safe_baseline",
        "safety_risk_score_baseline",
        "accident_expected_value_baseline",
        "elderly_incomplete_crossings_baseline",
        "extension_count_smart",
        "total_extension_sec_smart",
        "avg_ped_wait_sec_smart",
        "avg_vehicle_delay_sec_smart",
        "avg_queue_length_smart",
        "max_queue_length_smart",
        "network_avg_travel_time_sec_smart",
        "PET_A_proxy_count_smart",
        "PET_A_proxy_mean_smart",
        "PET_A_proxy_severe_smart",
        "PET_A_proxy_moderate_smart",
        "PET_A_proxy_safe_smart",
        "PET_B_surrogate_count_smart",
        "PET_B_surrogate_mean_smart",
        "PET_B_surrogate_severe_smart",
        "PET_B_surrogate_moderate_smart",
        "PET_B_surrogate_safe_smart",
        "safety_risk_score_smart",
        "accident_expected_value_smart",
        "elderly_incomplete_crossings_smart",
        "extension_count_delta",
        "total_extension_sec_delta",
        "avg_ped_wait_sec_delta",
        "avg_vehicle_delay_sec_delta",
        "avg_queue_length_delta",
        "max_queue_length_delta",
        "network_avg_travel_time_sec_delta",
        "PET_A_proxy_count_delta",
        "PET_A_proxy_mean_delta",
        "PET_A_proxy_severe_delta",
        "PET_A_proxy_moderate_delta",
        "PET_A_proxy_safe_delta",
        "PET_B_surrogate_count_delta",
        "PET_B_surrogate_mean_delta",
        "PET_B_surrogate_severe_delta",
        "PET_B_surrogate_moderate_delta",
        "PET_B_surrogate_safe_delta",
        "safety_risk_score_delta",
        "accident_expected_delta",
        "elderly_incomplete_crossings_delta",
    ],
    "04_summary_by_crosswalk.csv": [
        "crosswalk_id",
        "metric",
        "n",
        "mean_delta",
        "std_delta",
        "se_delta",
        "ci95_low",
        "ci95_high",
        "interpretation_hint",
    ],
    "05_extension_events_all.csv_structured": [
        "seed",
        "scenario",
        "time",
        "crosswalk_id",
        "tls_id",
        "ped_link_index",
        "extension_sec",
        "detected_ped_count",
        "source_dir",
    ],
    "06_benefit_input_table.csv": [
        "target_crosswalk_id",
        "n_successful_pairs",
        "traffic_scope",
        "safety_scope",
        "mean_network_vehicle_delay_delta",
        "mean_network_travel_time_delta",
        "mean_avg_queue_length_delta",
        "mean_max_queue_length_delta",
        "mean_ped_wait_delta",
        "mean_safety_risk_score_delta",
        "mean_accident_expected_delta",
        "mean_elderly_incomplete_crossings_delta",
        "mean_high_risk_event_count_delta",
        "total_extension_count_mean",
        "total_extension_sec_mean",
        "simulation_duration_sec",
        "demand_multiplier",
        "install_cost_krw",
        "usable_for_benefit_analysis",
        "exclusion_reason",
    ],
    "safety_metrics_by_run.csv": [
        "target_crosswalk_id",
        "seed",
        "scenario",
        "safety_scope",
        "pedestrian_count_near_target",
        "crossing_attempt_count",
        "completed_crossing_count",
        "incomplete_crossing_count",
        "elderly_incomplete_crossings",
        "high_risk_event_count",
        "PET_A_proxy_count",
        "PET_A_proxy_mean",
        "PET_A_proxy_min",
        "PET_A_proxy_severe",
        "PET_A_proxy_moderate",
        "PET_A_proxy_safe",
        "PET_B_surrogate_count",
        "PET_B_surrogate_mean",
        "PET_B_surrogate_min",
        "PET_B_surrogate_severe",
        "PET_B_surrogate_moderate",
        "PET_B_surrogate_safe",
        "safety_risk_score",
        "accident_expected_value",
        "note",
    ],
    "safety_paired_delta.csv": [
        "target_crosswalk_id",
        "seed",
        "high_risk_event_count_baseline",
        "high_risk_event_count_smart",
        "high_risk_event_count_delta",
        "PET_A_proxy_mean_delta",
        "PET_A_proxy_min_delta",
        "PET_B_surrogate_mean_delta",
        "PET_B_surrogate_min_delta",
        "safety_risk_score_delta",
        "accident_expected_delta",
        "elderly_incomplete_crossings_delta",
        "incomplete_crossing_count_delta",
    ],
    "safety_summary_by_crosswalk.csv": [
        "target_crosswalk_id",
        "metric",
        "n",
        "mean_delta",
        "std_delta",
        "se_delta",
        "ci95_low",
        "ci95_high",
        "interpretation_hint",
    ],
}


def _apply_schema(df: pd.DataFrame, schema: list[str]) -> pd.DataFrame:
    if df.empty and not df.columns.tolist():
        return pd.DataFrame(columns=schema)
    out = df.copy()
    for col in schema:
        if col not in out.columns:
            out[col] = np.nan
    return out[schema]


def _write_csv_with_schema(df: pd.DataFrame, path: Path, schema: list[str] | None = None) -> None:
    _ensure_dir(path.parent)
    if schema is not None:
        df = _apply_schema(df, schema)
    elif df.empty and not df.columns.tolist():
        df = pd.DataFrame(columns=[])
    df.to_csv(path, index=False)


@dataclass
class SourceBundle:
    source_root: Path
    source_kind: str
    csv_dir: Path | None
    files: dict[str, Path]
    notes: list[str]


def _ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


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


def _write_csv(df: pd.DataFrame, path: Path) -> None:
    _ensure_dir(path.parent)
    df.to_csv(path, index=False)


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _hash_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _file_type(path: Path) -> str:
    if path.name == ".DS_Store":
        return "mac_metadata"
    suffix = path.suffix.lower()
    if not suffix:
        return "file"
    return suffix.lstrip(".")


def _safe_non_null_count(df: pd.DataFrame, column: str) -> int:
    if column not in df.columns:
        return 0
    return int(pd.to_numeric(df[column], errors="coerce").notna().sum())


def _sample_values(df: pd.DataFrame, column: str, limit: int = 3) -> str:
    if column not in df.columns:
        return "[]"
    series = df[column].dropna()
    if series.empty:
        return "[]"
    samples = [str(v) for v in series.head(limit).tolist()]
    return json.dumps(samples, ensure_ascii=False)


def _metric_interpretation(metric: str) -> str:
    if metric.startswith("PET_A_proxy_") or metric.startswith("PET_B_surrogate_"):
        return "surrogate safety proxy / accident substitute metric"
    if metric == "safety_risk_score":
        return "composite safety proxy / risk score; not actual accidents"
    if metric == "accident_expected_value":
        return "expected accident proxy value; do not read as actual accident count"
    if metric == "elderly_incomplete_crossings":
        return "elderly crossing incomplete proxy"
    if metric.endswith("_delta"):
        return "paired delta; positive and negative signs matter"
    return "metric"


def _metric_caution(metric: str, exists: bool, non_null_count: int) -> str:
    if not exists:
        return "column not present in this CSV"
    if non_null_count == 0:
        return "column exists but has no non-null values"
    if metric == "accident_expected_value":
        return "do not call this actual accident count"
    if metric.startswith("PET_"):
        return "use as surrogate safety proxy only"
    if metric == "elderly_incomplete_crossings":
        return "use as incomplete-crossing proxy only"
    return "interpret with source context"


def _is_empty_file(path: Path) -> bool:
    try:
        return path.exists() and path.stat().st_size == 0
    except OSError:
        return False


def _detect_source_kind(source_root: Path) -> str:
    if (source_root / "csv" / "00_run_completion_summary.csv").exists():
        return "phase6_standard"
    if (source_root / "outputs" / "network_simulation_summary_seed.csv").exists():
        return "smoke_or_integrated_selected"
    return "generic_result_tree"


def _collect_source_bundle(source_root: Path) -> SourceBundle:
    source_root = source_root.resolve()
    source_kind = _detect_source_kind(source_root)
    candidates = [
        source_root / "csv",
        source_root / "outputs",
        source_root,
    ]
    csv_dir = next((p for p in candidates if p.exists() and any(p.glob("*.csv"))), None)
    files: dict[str, Path] = {}
    if csv_dir is not None:
        for path in sorted(csv_dir.glob("*")):
            if path.is_file():
                files[path.name] = path
    return SourceBundle(source_root=source_root, source_kind=source_kind, csv_dir=csv_dir, files=files, notes=[])


def _resolve_source(bundle: SourceBundle, candidates: Iterable[str]) -> Path | None:
    for name in candidates:
        path = bundle.files.get(name)
        if path and path.exists():
            return path
        alt = bundle.source_root / name
        if alt.exists():
            return alt
        alt = bundle.source_root / "outputs" / name
        if alt.exists():
            return alt
        alt = bundle.source_root / "csv" / name
        if alt.exists():
            return alt
    return None


def _aggregate_delta_summary(delta_df: pd.DataFrame) -> pd.DataFrame:
    if delta_df.empty or "crosswalk_id" not in delta_df.columns:
        return pd.DataFrame()
    metric_cols = [col for col in delta_df.columns if col.endswith("_delta")]
    rows: list[dict[str, Any]] = []
    for crosswalk_id, group in delta_df.groupby(delta_df["crosswalk_id"].astype(str), dropna=False):
        for metric in metric_cols:
            values = pd.to_numeric(group[metric], errors="coerce").dropna()
            n = int(values.shape[0])
            if n == 0:
                continue
            mean = float(values.mean())
            std = float(values.std(ddof=1)) if n > 1 else 0.0
            se = float(std / np.sqrt(n)) if n > 0 else np.nan
            ci = 1.96 * se if n > 1 else 0.0
            rows.append(
                {
                    "crosswalk_id": crosswalk_id,
                    "metric": metric,
                    "n": n,
                    "mean_delta": mean,
                    "std_delta": std,
                    "se_delta": se,
                    "ci95_low": mean - ci,
                    "ci95_high": mean + ci,
                    "min_delta": float(values.min()),
                    "max_delta": float(values.max()),
                    "interpretation_hint": "mean_delta > 0 means smart increases the metric relative to baseline; mean_delta < 0 means smart decreases it; check ci95 for uncertainty",
                }
            )
    return pd.DataFrame(rows)


def _derive_signal_phase_audit(bundle: SourceBundle) -> pd.DataFrame:
    existing = _resolve_source(bundle, ["05_signal_phase_audit.csv", "batch03_signal_phase_audit.csv"])
    if existing is not None:
        return _read_csv(existing)
    return pd.DataFrame()


def _derive_impact_scope(bundle: SourceBundle) -> pd.DataFrame:
    existing = _resolve_source(bundle, ["06_impact_scope_edge_counts.csv"])
    if existing is not None:
        return _read_csv(existing)
    return pd.DataFrame()


def _derive_source_inventory(bundle: SourceBundle) -> pd.DataFrame:
    existing = _resolve_source(bundle, ["07_source_file_inventory.csv"])
    if existing is not None:
        return _read_csv(existing)
    return _build_source_file_inventory(bundle.source_root, bundle.files)


def _recognize_source_file(path: Path) -> tuple[bool, str, str]:
    name = path.name
    if name in STANDARD_KEEP_NAMES:
        return True, "recognized_result_csv", "standard result file"
    if name in {
        "route_generation_audit.csv",
        "pedestrian_route_connectivity_audit.csv",
        "signal_phase_audit.csv",
        "skipped_pedestrian_routes.csv",
        "source_file_inventory.csv",
        "phase6_smoke_summary.md",
        "runtime_trace_seed1.log",
    }:
        return False, "supporting_artifact", "supporting_artifact"
    if path.suffix.lower() in {".csv", ".log", ".xml", ".sumocfg", ".rou", ".md", ".err", ".out"}:
        return False, "unrecognized_input_file", "unrecognized input file"
    return False, "unrecognized_input_file", "unrecognized input file"


def _build_source_file_inventory(source_root: Path, bundle_files: dict[str, Path] | None = None) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for path in sorted(source_root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(source_root).as_posix()
        seen.add(rel)
        recognized, reason, note = _recognize_source_file(path)
        rows.append(
            {
                "file_path": rel,
                "file_name": path.name,
                "file_type": _file_type(path),
                "size_mb": round(path.stat().st_size / (1024 * 1024), 6),
                "exists": True,
                "role": "result_csv" if recognized else "input_artifact",
                "recognized_as_input": bool(recognized),
                "used_in_standardization": bool(recognized),
                "reason": reason,
                "note": note,
            }
        )
    if bundle_files:
        for name, path in bundle_files.items():
            rel = path.relative_to(source_root).as_posix() if path.is_relative_to(source_root) else name
            if rel in seen:
                continue
            recognized, reason, note = _recognize_source_file(path)
            rows.append(
                {
                    "file_path": rel,
                    "file_name": path.name,
                    "file_type": _file_type(path),
                    "size_mb": round(path.stat().st_size / (1024 * 1024), 6) if path.exists() else 0.0,
                    "exists": bool(path.exists()),
                    "role": "result_csv" if recognized else "input_artifact",
                    "recognized_as_input": bool(recognized),
                    "used_in_standardization": bool(recognized),
                    "reason": reason if path.exists() else "missing",
                    "note": note if path.exists() else "file_missing",
                }
            )
    df = pd.DataFrame(rows, columns=STANDARD_CSV_SCHEMAS["07_source_file_inventory.csv"])
    if not df.empty:
        df["exists"] = df["exists"].astype(bool)
        df["recognized_as_input"] = df["recognized_as_input"].astype(bool)
        df["used_in_standardization"] = df["used_in_standardization"].astype(bool)
    return df


def _ensure_target_scope_columns(df: pd.DataFrame) -> pd.DataFrame:
    out = _target_crosswalk_column(df)
    out = _add_scope_columns(out)
    return out


def _combine_scope_row(row: pd.Series, names: list[str]) -> Any:
    for name in names:
        if name in row.index and pd.notna(row[name]):
            return row[name]
    return np.nan


def _build_global_traffic_results(run_summary: pd.DataFrame, long_df: pd.DataFrame) -> pd.DataFrame:
    def _pick(df: pd.DataFrame, *cols: str) -> pd.Series:
        for col in cols:
            if col in df.columns:
                value = df[col]
                if isinstance(value, pd.DataFrame):
                    return value.iloc[:, 0]
                return value
        return pd.Series([np.nan] * len(df), index=df.index)

    if run_summary.empty and long_df.empty:
        return pd.DataFrame(
            columns=[
                "target_crosswalk_id",
                "seed",
                "scenario",
                "traffic_scope",
                "network_arrived_vehicles",
                "network_avg_travel_time_sec",
                "avg_ped_wait_sec",
                "avg_vehicle_delay_sec",
                "avg_queue_length",
                "max_queue_length",
                "total_vehicle_delay_sec",
                "total_extension_count",
                "total_extension_sec",
                "simulation_duration_sec",
                "demand_multiplier",
                "install_cost_krw",
            ]
        )
    if long_df.empty:
        base = run_summary.copy()
        base["target_crosswalk_id"] = np.nan
    else:
        base = _target_crosswalk_column(long_df.copy())
    base["traffic_scope"] = GLOBAL_NETWORK_SCOPE
    if "target_crosswalk_id" not in base.columns:
        base["target_crosswalk_id"] = base.get("crosswalk_id", base.get("requested_crosswalk_id", np.nan))

    summary = run_summary.copy()
    if not summary.empty:
        summary = summary.rename(
            columns={
                "arrived_vehicles": "network_arrived_vehicles_summary",
                "network_arrived_vehicles": "network_arrived_vehicles_summary",
                "network_avg_travel_time_sec": "network_avg_travel_time_sec_summary",
                "avg_vehicle_delay_sec": "avg_vehicle_delay_sec_summary",
                "avg_ped_wait_sec": "avg_ped_wait_sec_summary",
                "avg_queue_length": "avg_queue_length_summary",
                "max_queue_length": "max_queue_length_summary",
                "total_vehicle_delay": "total_vehicle_delay_sec_summary",
                "total_vehicle_delay_sec": "total_vehicle_delay_sec_summary",
                "extension_count": "total_extension_count_summary",
                "total_extension_sec": "total_extension_sec_summary",
            }
        )
        summary_cols = ["seed", "scenario"] + [c for c in summary.columns if c.endswith("_summary")]
        summary = summary[summary_cols].copy()
        base = base.merge(summary, on=["seed", "scenario"], how="left")

    base["network_arrived_vehicles"] = _pick(base, "network_arrived_vehicles_summary", "network_arrived_vehicles")
    base["network_avg_travel_time_sec"] = _pick(base, "network_avg_travel_time_sec_summary", "network_avg_travel_time_sec")
    base["avg_vehicle_delay_sec"] = _pick(base, "avg_vehicle_delay_sec_summary", "avg_vehicle_delay_sec")
    base["avg_ped_wait_sec"] = _pick(base, "avg_ped_wait_sec_summary", "avg_ped_wait_sec", "pedestrian_waiting_time_mean", "pedestrian_delay")
    base["avg_queue_length"] = _pick(base, "avg_queue_length_summary", "avg_queue_length")
    base["max_queue_length"] = _pick(base, "max_queue_length_summary", "max_queue_length")
    base["total_vehicle_delay_sec"] = _pick(base, "total_vehicle_delay_sec_summary", "total_vehicle_delay_sec", "total_vehicle_delay", "vehicle_delay")
    base["total_extension_count"] = _pick(base, "total_extension_count_summary", "total_extension_count", "extension_count")
    base["total_extension_sec"] = _pick(base, "total_extension_sec_summary", "total_extension_sec")
    for col in ["run_name", "output_dir", "run_start_time", "run_end_time", "elapsed_sec", "completed", "sim_duration", "warmup", "step_length", "simulation_duration_sec", "demand_multiplier", "install_cost_krw"]:
        if col not in base.columns:
            base[col] = np.nan
    keep = [
        "run_name",
        "target_crosswalk_id",
        "seed",
        "scenario",
        "output_dir",
        "run_start_time",
        "run_end_time",
        "elapsed_sec",
        "completed",
        "sim_duration",
        "warmup",
        "step_length",
        "traffic_scope",
        "network_arrived_vehicles",
        "network_avg_travel_time_sec",
        "avg_ped_wait_sec",
        "avg_vehicle_delay_sec",
        "avg_queue_length",
        "max_queue_length",
        "total_vehicle_delay_sec",
        "total_extension_count",
        "total_extension_sec",
        "simulation_duration_sec",
        "demand_multiplier",
        "install_cost_krw",
    ]
    for col in keep:
        if col not in base.columns:
            base[col] = np.nan
    return base[keep].copy()


def _build_target_safety_results(long_df: pd.DataFrame) -> pd.DataFrame:
    if long_df.empty:
        return pd.DataFrame(
            columns=[
                "run_name",
                "target_crosswalk_id",
                "seed",
                "scenario",
                "output_dir",
                "run_start_time",
                "run_end_time",
                "elapsed_sec",
                "completed",
                "sim_duration",
                "warmup",
                "step_length",
                "traffic_scope",
                "safety_scope",
                "pedestrian_count_near_target",
                "crossing_attempt_count",
                "completed_crossing_count",
                "incomplete_crossing_count",
                "elderly_incomplete_crossings",
                "high_risk_event_count",
                "PET_A_proxy_count",
                "PET_A_proxy_mean",
                "PET_A_proxy_min",
                "PET_A_proxy_severe",
                "PET_A_proxy_moderate",
                "PET_A_proxy_safe",
                "PET_B_surrogate_count",
                "PET_B_surrogate_mean",
                "PET_B_surrogate_min",
                "PET_B_surrogate_severe",
                "PET_B_surrogate_moderate",
                "PET_B_surrogate_safe",
                "safety_risk_score",
                "accident_expected_value",
                "note",
            ]
        )
    out = _ensure_target_scope_columns(long_df)
    for col in [
        "run_name",
        "output_dir",
        "run_start_time",
        "run_end_time",
        "elapsed_sec",
        "completed",
        "sim_duration",
        "warmup",
        "step_length",
        "pedestrian_count_near_target",
        "crossing_attempt_count",
        "completed_crossing_count",
        "incomplete_crossing_count",
        "elderly_incomplete_crossings",
        "high_risk_event_count",
        "PET_A_proxy_count",
        "PET_A_proxy_mean",
        "PET_A_proxy_min",
        "PET_A_proxy_severe",
        "PET_A_proxy_moderate",
        "PET_A_proxy_safe",
        "PET_B_surrogate_count",
        "PET_B_surrogate_mean",
        "PET_B_surrogate_min",
        "PET_B_surrogate_severe",
        "PET_B_surrogate_moderate",
        "PET_B_surrogate_safe",
        "safety_risk_score",
        "accident_expected_value",
    ]:
        if col not in out.columns:
            out[col] = np.nan
    if "pedestrian_count_near_target" not in out.columns or out["pedestrian_count_near_target"].isna().all():
        if "pedestrian_count" in out.columns:
            out["pedestrian_count_near_target"] = pd.to_numeric(out["pedestrian_count"], errors="coerce")
    if "crossing_attempt_count" not in out.columns or out["crossing_attempt_count"].isna().all():
        out["crossing_attempt_count"] = pd.to_numeric(out.get("pedestrian_count_near_target", np.nan), errors="coerce")
    if "completed_crossing_count" not in out.columns or out["completed_crossing_count"].isna().all():
        if "PET_A_proxy_count" in out.columns:
            out["completed_crossing_count"] = pd.to_numeric(out["PET_A_proxy_count"], errors="coerce")
    if "incomplete_crossing_count" not in out.columns or out["incomplete_crossing_count"].isna().all():
        attempts = pd.to_numeric(out.get("crossing_attempt_count"), errors="coerce")
        completed = pd.to_numeric(out.get("completed_crossing_count"), errors="coerce")
        out["incomplete_crossing_count"] = attempts - completed
    if "high_risk_event_count" not in out.columns or out["high_risk_event_count"].isna().all():
        if "PET_B_surrogate_severe" in out.columns:
            out["high_risk_event_count"] = pd.to_numeric(out["PET_B_surrogate_severe"], errors="coerce")
    out["note"] = "surrogate_safety_proxy"
    keep = [
        "run_name",
        "target_crosswalk_id",
        "seed",
        "scenario",
        "output_dir",
        "run_start_time",
        "run_end_time",
        "elapsed_sec",
        "completed",
        "sim_duration",
        "warmup",
        "step_length",
        "traffic_scope",
        "safety_scope",
        "pedestrian_count_near_target",
        "crossing_attempt_count",
        "completed_crossing_count",
        "incomplete_crossing_count",
        "elderly_incomplete_crossings",
        "high_risk_event_count",
        "PET_A_proxy_count",
        "PET_A_proxy_mean",
        "PET_A_proxy_min",
        "PET_A_proxy_severe",
        "PET_A_proxy_moderate",
        "PET_A_proxy_safe",
        "PET_B_surrogate_count",
        "PET_B_surrogate_mean",
        "PET_B_surrogate_min",
        "PET_B_surrogate_severe",
        "PET_B_surrogate_moderate",
        "PET_B_surrogate_safe",
        "safety_risk_score",
        "accident_expected_value",
        "note",
    ]
    return out[keep].copy()


def _build_paired_delta(results_df: pd.DataFrame, include_traffic: bool = True) -> pd.DataFrame:
    if results_df.empty:
        base_cols = ["target_crosswalk_id", "seed"]
        if include_traffic:
            base_cols.extend(["traffic_scope"])
        base_cols.extend(["safety_scope"])
        return pd.DataFrame(columns=base_cols)
    df = _target_crosswalk_column(results_df)
    join_cols = ["target_crosswalk_id", "seed"]
    if "scenario" not in df.columns:
        return pd.DataFrame(columns=join_cols)
    baseline = df[df["scenario"].astype(str) == "baseline"].copy()
    smart = df[df["scenario"].astype(str) == "smart"].copy()
    if baseline.empty or smart.empty:
        return pd.DataFrame(columns=join_cols)
    base = baseline.rename(columns={c: f"{c}_baseline" for c in baseline.columns if c not in join_cols + ["scenario"]})
    smart = smart.rename(columns={c: f"{c}_smart" for c in smart.columns if c not in join_cols + ["scenario"]})
    merged = base.merge(smart, on=join_cols, how="inner")
    if merged.empty:
        return pd.DataFrame(columns=join_cols)
    merged["paired_ok"] = True
    if "traffic_scope_baseline" in merged.columns:
        merged["traffic_scope"] = merged["traffic_scope_baseline"]
    if "safety_scope_baseline" in merged.columns:
        merged["safety_scope"] = merged["safety_scope_baseline"]
    delta_specs = {
        "avg_vehicle_delay_sec": "mean_network_vehicle_delay_delta",
        "network_avg_travel_time_sec": "mean_network_travel_time_delta",
        "avg_queue_length": "mean_avg_queue_length_delta",
        "max_queue_length": "mean_max_queue_length_delta",
        "avg_ped_wait_sec": "mean_ped_wait_delta",
        "safety_risk_score": "mean_safety_risk_score_delta",
        "accident_expected_value": "mean_accident_expected_delta",
        "elderly_incomplete_crossings": "mean_elderly_incomplete_crossings_delta",
        "high_risk_event_count": "mean_high_risk_event_count_delta",
        "total_extension_count": "total_extension_count_delta",
        "total_extension_sec": "total_extension_sec_delta",
    }
    for src, dst in delta_specs.items():
        if f"{src}_baseline" in merged.columns and f"{src}_smart" in merged.columns:
            merged[dst] = pd.to_numeric(merged[f"{src}_smart"], errors="coerce") - pd.to_numeric(merged[f"{src}_baseline"], errors="coerce")
        else:
            merged[dst] = np.nan
    keep = [
        "target_crosswalk_id",
        "seed",
        "paired_ok",
        "traffic_scope",
        "safety_scope",
        *[f"{name}_baseline" for name in delta_specs if f"{name}_baseline" in merged.columns],
        *[f"{name}_smart" for name in delta_specs if f"{name}_smart" in merged.columns],
        *delta_specs.values(),
    ]
    keep = [c for c in keep if c in merged.columns]
    return merged[keep].copy()


def _build_summary_by_crosswalk(delta_df: pd.DataFrame) -> pd.DataFrame:
    if delta_df.empty or "target_crosswalk_id" not in delta_df.columns:
        return pd.DataFrame(
            columns=["target_crosswalk_id", "metric", "n", "mean_delta", "std_delta", "se_delta", "ci95_low", "ci95_high", "interpretation_hint"]
        )
    metric_cols = [c for c in delta_df.columns if c.endswith("_delta") or c in {"mean_network_vehicle_delay_delta", "mean_network_travel_time_delta", "mean_avg_queue_length_delta", "mean_max_queue_length_delta", "mean_ped_wait_delta", "mean_safety_risk_score_delta", "mean_accident_expected_delta", "mean_elderly_incomplete_crossings_delta", "mean_high_risk_event_count_delta", "total_extension_count_delta", "total_extension_sec_delta"}]
    rows: list[dict[str, Any]] = []
    for crosswalk_id, group in delta_df.groupby("target_crosswalk_id", dropna=False):
        for metric in metric_cols:
            values = pd.to_numeric(group[metric], errors="coerce").dropna()
            n = int(values.shape[0])
            if n == 0:
                continue
            mean = float(values.mean())
            std = float(values.std(ddof=1)) if n > 1 else 0.0
            se = float(std / np.sqrt(n)) if n > 0 else np.nan
            ci = 1.96 * se if n > 1 else 0.0
            rows.append(
                {
                    "target_crosswalk_id": crosswalk_id,
                    "metric": metric,
                    "n": n,
                    "mean_delta": mean,
                    "std_delta": std,
                    "se_delta": se,
                    "ci95_low": mean - ci,
                    "ci95_high": mean + ci,
                    "interpretation_hint": "mean_delta > 0 means smart increases the metric relative to baseline; mean_delta < 0 means smart decreases it; check ci95 for uncertainty",
                }
            )
    return pd.DataFrame(rows)


def _build_safety_metrics_by_run(safety_df: pd.DataFrame) -> pd.DataFrame:
    if safety_df.empty:
        return pd.DataFrame(
            columns=[
                "target_crosswalk_id",
                "seed",
                "scenario",
                "safety_scope",
                "pedestrian_count_near_target",
                "crossing_attempt_count",
                "completed_crossing_count",
                "incomplete_crossing_count",
                "elderly_incomplete_crossings",
                "high_risk_event_count",
                "PET_A_proxy_count",
                "PET_A_proxy_mean",
                "PET_A_proxy_min",
                "PET_A_proxy_severe",
                "PET_A_proxy_moderate",
                "PET_A_proxy_safe",
                "PET_B_surrogate_count",
                "PET_B_surrogate_mean",
                "PET_B_surrogate_min",
                "PET_B_surrogate_severe",
                "PET_B_surrogate_moderate",
                "PET_B_surrogate_safe",
                "safety_risk_score",
                "accident_expected_value",
                "note",
            ]
        )
    out = safety_df.copy()
    out["note"] = out.get("note", "derived safety metrics")
    return out[
        [
            "target_crosswalk_id",
            "seed",
            "scenario",
            "safety_scope",
            "pedestrian_count_near_target",
            "crossing_attempt_count",
            "completed_crossing_count",
            "incomplete_crossing_count",
            "elderly_incomplete_crossings",
            "high_risk_event_count",
            "PET_A_proxy_count",
            "PET_A_proxy_mean",
            "PET_A_proxy_min",
            "PET_A_proxy_severe",
            "PET_A_proxy_moderate",
            "PET_A_proxy_safe",
            "PET_B_surrogate_count",
            "PET_B_surrogate_mean",
            "PET_B_surrogate_min",
            "PET_B_surrogate_severe",
            "PET_B_surrogate_moderate",
            "PET_B_surrogate_safe",
            "safety_risk_score",
            "accident_expected_value",
            "note",
        ]
    ].copy()


def _build_safety_paired_delta(safety_df: pd.DataFrame) -> pd.DataFrame:
    if safety_df.empty:
        return pd.DataFrame(
            columns=[
                "target_crosswalk_id",
                "seed",
                "high_risk_event_count_baseline",
                "high_risk_event_count_smart",
                "high_risk_event_count_delta",
                "PET_A_proxy_mean_delta",
                "PET_A_proxy_min_delta",
                "PET_B_surrogate_mean_delta",
                "PET_B_surrogate_min_delta",
                "safety_risk_score_delta",
                "accident_expected_delta",
                "elderly_incomplete_crossings_delta",
                "incomplete_crossing_count_delta",
            ]
        )
    baseline = safety_df[safety_df["scenario"].astype(str) == "baseline"].copy()
    smart = safety_df[safety_df["scenario"].astype(str) == "smart"].copy()
    if baseline.empty or smart.empty:
        return pd.DataFrame(columns=["target_crosswalk_id", "seed"])
    join_cols = ["target_crosswalk_id", "seed"]
    base = baseline.rename(columns={c: f"{c}_baseline" for c in baseline.columns if c not in join_cols + ["scenario"]})
    smart = smart.rename(columns={c: f"{c}_smart" for c in smart.columns if c not in join_cols + ["scenario"]})
    merged = base.merge(smart, on=join_cols, how="inner")
    if merged.empty:
        return pd.DataFrame(columns=join_cols)
    delta_map = {
        "high_risk_event_count": "high_risk_event_count_delta",
        "PET_A_proxy_mean": "PET_A_proxy_mean_delta",
        "PET_A_proxy_min": "PET_A_proxy_min_delta",
        "PET_B_surrogate_mean": "PET_B_surrogate_mean_delta",
        "PET_B_surrogate_min": "PET_B_surrogate_min_delta",
        "safety_risk_score": "safety_risk_score_delta",
        "accident_expected_value": "accident_expected_delta",
        "elderly_incomplete_crossings": "elderly_incomplete_crossings_delta",
        "incomplete_crossing_count": "incomplete_crossing_count_delta",
    }
    for src, dst in delta_map.items():
        merged[dst] = pd.to_numeric(merged.get(f"{src}_smart"), errors="coerce") - pd.to_numeric(merged.get(f"{src}_baseline"), errors="coerce")
    merged["high_risk_event_count_baseline"] = merged.get("high_risk_event_count_baseline", np.nan)
    merged["high_risk_event_count_smart"] = merged.get("high_risk_event_count_smart", np.nan)
    keep = ["target_crosswalk_id", "seed", "high_risk_event_count_baseline", "high_risk_event_count_smart"] + list(delta_map.values())
    return merged[keep].copy()


def _build_benefit_input_table(results_delta: pd.DataFrame, safety_delta: pd.DataFrame) -> pd.DataFrame:
    if results_delta.empty and safety_delta.empty:
        return pd.DataFrame(
            columns=[
                "target_crosswalk_id",
                "n_successful_pairs",
                "traffic_scope",
                "safety_scope",
                "mean_network_vehicle_delay_delta",
                "mean_network_travel_time_delta",
                "mean_avg_queue_length_delta",
                "mean_max_queue_length_delta",
                "mean_ped_wait_delta",
                "mean_safety_risk_score_delta",
                "mean_accident_expected_delta",
                "mean_elderly_incomplete_crossings_delta",
                "mean_high_risk_event_count_delta",
                "total_extension_count_mean",
                "total_extension_sec_mean",
                "simulation_duration_sec",
                "demand_multiplier",
                "install_cost_krw",
                "usable_for_benefit_analysis",
                "exclusion_reason",
            ]
        )
    rows: list[dict[str, Any]] = []
    ids = set(results_delta.get("target_crosswalk_id", pd.Series(dtype=object)).dropna().astype(str).tolist()) | set(
        safety_delta.get("target_crosswalk_id", pd.Series(dtype=object)).dropna().astype(str).tolist()
    )
    for crosswalk_id in sorted(ids):
        res = results_delta[results_delta["target_crosswalk_id"].astype(str) == str(crosswalk_id)] if not results_delta.empty and "target_crosswalk_id" in results_delta.columns else pd.DataFrame()
        saf = safety_delta[safety_delta["target_crosswalk_id"].astype(str) == str(crosswalk_id)] if not safety_delta.empty and "target_crosswalk_id" in safety_delta.columns else pd.DataFrame()
        n_pairs = int(len(res)) if not res.empty else int(len(saf))
        row = {
            "target_crosswalk_id": crosswalk_id,
            "n_successful_pairs": n_pairs,
            "traffic_scope": GLOBAL_NETWORK_SCOPE,
            "safety_scope": TARGET_CROSSWALK_SCOPE,
            "mean_network_vehicle_delay_delta": _safe_mean(res["mean_network_vehicle_delay_delta"]) if not res.empty and "mean_network_vehicle_delay_delta" in res.columns else np.nan,
            "mean_network_travel_time_delta": _safe_mean(res["mean_network_travel_time_delta"]) if not res.empty and "mean_network_travel_time_delta" in res.columns else np.nan,
            "mean_avg_queue_length_delta": _safe_mean(res["mean_avg_queue_length_delta"]) if not res.empty and "mean_avg_queue_length_delta" in res.columns else np.nan,
            "mean_max_queue_length_delta": _safe_mean(res["mean_max_queue_length_delta"]) if not res.empty and "mean_max_queue_length_delta" in res.columns else np.nan,
            "mean_ped_wait_delta": _safe_mean(res["mean_ped_wait_delta"]) if not res.empty and "mean_ped_wait_delta" in res.columns else np.nan,
            "mean_safety_risk_score_delta": _safe_mean(saf["safety_risk_score_delta"]) if not saf.empty and "safety_risk_score_delta" in saf.columns else np.nan,
            "mean_accident_expected_delta": _safe_mean(saf["accident_expected_delta"]) if not saf.empty and "accident_expected_delta" in saf.columns else np.nan,
            "mean_elderly_incomplete_crossings_delta": _safe_mean(saf["elderly_incomplete_crossings_delta"]) if not saf.empty and "elderly_incomplete_crossings_delta" in saf.columns else np.nan,
            "mean_high_risk_event_count_delta": _safe_mean(saf["high_risk_event_count_delta"]) if not saf.empty and "high_risk_event_count_delta" in saf.columns else np.nan,
            "total_extension_count_mean": _safe_mean(res["total_extension_count_delta"]) if not res.empty and "total_extension_count_delta" in res.columns else np.nan,
            "total_extension_sec_mean": _safe_mean(res["total_extension_sec_delta"]) if not res.empty and "total_extension_sec_delta" in res.columns else np.nan,
            "simulation_duration_sec": _safe_mean(res["simulation_duration_sec"]) if not res.empty and "simulation_duration_sec" in res.columns else np.nan,
            "demand_multiplier": _safe_mean(res["demand_multiplier"]) if not res.empty and "demand_multiplier" in res.columns else np.nan,
            "install_cost_krw": _safe_mean(res["install_cost_krw"]) if not res.empty and "install_cost_krw" in res.columns else np.nan,
        }
        usable = all(
            pd.notna(row.get(col))
            for col in [
                "mean_network_vehicle_delay_delta",
                "mean_network_travel_time_delta",
                "mean_avg_queue_length_delta",
                "mean_max_queue_length_delta",
                "mean_ped_wait_delta",
                "mean_safety_risk_score_delta",
                "mean_accident_expected_delta",
            ]
        ) and n_pairs > 0
        row["usable_for_benefit_analysis"] = bool(usable)
        row["exclusion_reason"] = "" if usable else "missing_paired_data_or_required_deltas"
        rows.append(row)
    return pd.DataFrame(rows)


def _write_structured_phase6_outputs(
    output_root: Path,
    bundle: SourceBundle,
    run_summary_df: pd.DataFrame,
    long_df: pd.DataFrame,
    paired_df: pd.DataFrame,
    summary_df: pd.DataFrame,
    event_df: pd.DataFrame,
    audit_df: pd.DataFrame,
    impact_df: pd.DataFrame,
    source_inventory: pd.DataFrame,
    scope_audit_df: pd.DataFrame | None = None,
) -> dict[str, pd.DataFrame]:
    structured_debug_dir = output_root / "csv" / "debug"
    structured_results_dir = output_root / "csv" / "results"
    structured_safety_dir = output_root / "csv" / "safety"
    for path in [structured_debug_dir, structured_results_dir, structured_safety_dir]:
        _ensure_dir(path)

    traffic_df = _build_global_traffic_results(run_summary_df, long_df)
    safety_df = _build_target_safety_results(long_df)
    safety_metrics_df = _build_safety_metrics_by_run(safety_df)
    paired_source = traffic_df.merge(
        safety_df,
        on=["target_crosswalk_id", "seed", "scenario"],
        how="inner",
        suffixes=("", "_safety"),
    )
    results_delta_df = _build_paired_delta(paired_source, include_traffic=True)
    results_summary_df = _build_summary_by_crosswalk(results_delta_df)
    safety_delta_df = _build_safety_paired_delta(safety_metrics_df)
    safety_summary_df = _build_summary_by_crosswalk(safety_delta_df)
    benefit_df = _build_benefit_input_table(results_delta_df, safety_delta_df)

    debug_route_df = _read_csv(bundle.files.get("route_pair_diagnostics.csv")) if bundle.files.get("route_pair_diagnostics.csv") else pd.DataFrame()
    if debug_route_df.empty and not audit_df.empty:
        debug_route_df = audit_df.copy()
    debug_route_df = _apply_schema(debug_route_df, STANDARD_CSV_SCHEMAS["route_generation_audit.csv"])
    debug_ped_df = _read_csv(bundle.files.get("pedestrian_route_diagnostics.csv")) if bundle.files.get("pedestrian_route_diagnostics.csv") else pd.DataFrame()
    if debug_ped_df.empty and not long_df.empty:
        debug_ped_df = long_df[["crosswalk_id", "seed", "scenario"]].copy()
    debug_ped_df = _apply_schema(debug_ped_df, STANDARD_CSV_SCHEMAS["pedestrian_route_connectivity_audit.csv"])
    if "detected_crossing_presence" in debug_ped_df.columns:
        skipped_mask = pd.to_numeric(debug_ped_df["detected_crossing_presence"], errors="coerce").fillna(False).astype(bool)
    else:
        skipped_mask = pd.Series([False] * len(debug_ped_df))
    skipped_df = debug_ped_df[~skipped_mask].copy()
    signal_df = audit_df.copy()
    if signal_df.empty and not long_df.empty:
        signal_df = long_df[[c for c in ["crosswalk_id", "seed", "scenario", "tls_id", "ped_link_index"] if c in long_df.columns]].copy()
    signal_df = _apply_schema(signal_df, STANDARD_CSV_SCHEMAS["signal_phase_audit_structured.csv"])
    if "crosswalk_id" not in signal_df.columns:
        signal_df["crosswalk_id"] = np.nan
    inventory_df = _build_source_file_inventory(bundle.source_root, bundle.files)
    if inventory_df.empty:
        inventory_df = pd.DataFrame(columns=STANDARD_CSV_SCHEMAS["07_source_file_inventory.csv"])

    _write_csv_with_schema(debug_route_df, structured_debug_dir / "route_generation_audit.csv", STANDARD_CSV_SCHEMAS["route_generation_audit.csv"])
    _write_csv_with_schema(debug_ped_df, structured_debug_dir / "pedestrian_route_connectivity_audit.csv", STANDARD_CSV_SCHEMAS["pedestrian_route_connectivity_audit.csv"])
    _write_csv_with_schema(signal_df, structured_debug_dir / "signal_phase_audit.csv", STANDARD_CSV_SCHEMAS["signal_phase_audit_structured.csv"])
    _write_csv_with_schema(skipped_df, structured_debug_dir / "skipped_pedestrian_routes.csv", STANDARD_CSV_SCHEMAS["skipped_pedestrian_routes.csv"])
    _write_csv_with_schema(inventory_df, structured_debug_dir / "source_file_inventory.csv", STANDARD_CSV_SCHEMAS["07_source_file_inventory.csv"])

    _write_csv_with_schema(run_summary_df, structured_results_dir / "00_run_completion_summary.csv", STANDARD_CSV_SCHEMAS["00_run_completion_summary.csv_structured"])
    _write_csv_with_schema(traffic_df, structured_results_dir / "01_global_traffic_results_long.csv", STANDARD_CSV_SCHEMAS["01_global_traffic_results_long.csv"])
    _write_csv_with_schema(safety_df, structured_results_dir / "02_target_crosswalk_safety_results_long.csv", STANDARD_CSV_SCHEMAS["02_target_crosswalk_safety_results_long.csv"])
    _write_csv_with_schema(results_delta_df, structured_results_dir / "03_baseline_smart_paired_delta.csv", STANDARD_CSV_SCHEMAS["03_baseline_smart_paired_delta.csv_structured"])
    _write_csv_with_schema(results_summary_df, structured_results_dir / "04_summary_by_crosswalk.csv", STANDARD_CSV_SCHEMAS["04_summary_by_crosswalk.csv"])
    _write_csv_with_schema(event_df, structured_results_dir / "05_extension_events_all.csv", STANDARD_CSV_SCHEMAS["05_extension_events_all.csv_structured"])
    _write_csv_with_schema(benefit_df, structured_results_dir / "06_benefit_input_table.csv", STANDARD_CSV_SCHEMAS["06_benefit_input_table.csv"])

    _write_csv_with_schema(safety_metrics_df, structured_safety_dir / "safety_metrics_by_run.csv", STANDARD_CSV_SCHEMAS["safety_metrics_by_run.csv"])
    _write_csv_with_schema(safety_delta_df, structured_safety_dir / "safety_paired_delta.csv", STANDARD_CSV_SCHEMAS["safety_paired_delta.csv"])
    _write_csv_with_schema(safety_summary_df, structured_safety_dir / "safety_summary_by_crosswalk.csv", STANDARD_CSV_SCHEMAS["safety_summary_by_crosswalk.csv"])

    return {
        "traffic_df": traffic_df,
        "safety_df": safety_df,
        "safety_metrics_df": safety_metrics_df,
        "results_delta_df": results_delta_df,
        "safety_delta_df": safety_delta_df,
        "safety_summary_df": safety_summary_df,
        "benefit_df": benefit_df,
        "debug_route_df": debug_route_df,
        "debug_ped_df": debug_ped_df,
        "signal_df": signal_df,
        "skipped_df": skipped_df,
        "inventory_df": inventory_df,
    }


def _load_standard_csv(bundle: SourceBundle, csv_name: str) -> tuple[pd.DataFrame, str, str]:
    source = None
    note = ""
    if csv_name == "00_run_completion_summary.csv":
        source = _resolve_source(bundle, ["00_run_completion_summary.csv", "network_simulation_summary_seed.csv"])
    elif csv_name == "01_all_results_long.csv":
        source = _resolve_source(bundle, ["01_all_results_long.csv", "per_crosswalk_simulation_results_seed.csv"])
    elif csv_name == "02_baseline_smart_paired_delta.csv":
        source = _resolve_source(bundle, ["02_baseline_smart_paired_delta.csv", "baseline_vs_smart_summary.csv", "network_baseline_vs_smart_summary.csv"])
    elif csv_name == "03_paired_summary_by_crosswalk.csv":
        source = _resolve_source(bundle, ["03_paired_summary_by_crosswalk.csv"])
        if source is None:
            delta = _load_standard_csv(bundle, "02_baseline_smart_paired_delta.csv")[0]
            derived = _aggregate_delta_summary(delta)
            if not derived.empty:
                return derived, "derived from 02_baseline_smart_paired_delta.csv", "derived summary by crosswalk"
    elif csv_name == "04_extension_events_all.csv":
        source = _resolve_source(bundle, ["04_extension_events_all.csv", "extension_events_seed.csv"])
    elif csv_name == "05_signal_phase_audit.csv":
        source = _resolve_source(bundle, ["05_signal_phase_audit.csv", "batch03_signal_phase_audit.csv"])
    elif csv_name == "phase6_extension_scope_audit.csv":
        source = _resolve_source(bundle, ["phase6_extension_scope_audit.csv"])
        if source is None:
            event_path = _resolve_source(bundle, ["04_extension_events_all.csv", "extension_events_seed.csv", "phase6_smoke_extension_events.csv"])
            audit_path = _resolve_source(bundle, ["05_signal_phase_audit.csv", "batch03_signal_phase_audit.csv", "signal_phase_audit.csv"])
            if event_path is not None:
                event_df = _read_csv(event_path)
                audit_df = _read_csv(audit_path) if audit_path is not None else pd.DataFrame()
                derived = _standardize_extension_scope_audit_df(
                    event_df,
                    bundle.source_root,
                    event_path,
                    audit_path,
                    audit_df,
                )
                if not derived.empty:
                    return derived, f"derived from {event_path.name}", "extension scope audit"
    elif csv_name == "06_impact_scope_edge_counts.csv":
        source = _resolve_source(bundle, ["06_impact_scope_edge_counts.csv"])
    elif csv_name == "07_source_file_inventory.csv":
        source = _resolve_source(bundle, ["07_source_file_inventory.csv"])

    if source is None:
        if csv_name == "07_source_file_inventory.csv":
            return _derive_source_inventory(bundle), "derived from source tree", "source inventory"
        return pd.DataFrame(), "file_missing", "file missing"

    df = _read_csv(source)
    if csv_name == "03_paired_summary_by_crosswalk.csv" and not df.empty:
        # If a precomputed summary exists but has the wrong shape, prefer a derived table.
        if {"metric", "mean_delta"}.issubset(df.columns):
            return df, str(source), "precomputed paired summary"
        derived = _aggregate_delta_summary(df if csv_name == "03_paired_summary_by_crosswalk.csv" else df)
        if not derived.empty:
            return derived, f"derived from {source.name}", "derived summary by crosswalk"
    if csv_name == "07_source_file_inventory.csv" and df.empty:
        return _derive_source_inventory(bundle), str(source), "precomputed or derived source inventory"
    return df, str(source), note


def _value_from_row(row: pd.Series, names: Iterable[str], default: Any = np.nan) -> Any:
    for name in names:
        if name in row.index and not pd.isna(row.get(name)):
            return row.get(name)
    return default


def _ensure_columns(df: pd.DataFrame, columns: list[str]) -> pd.DataFrame:
    out = df.copy()
    for col in columns:
        if col not in out.columns:
            out[col] = np.nan
    return out


def _coerce_text_series(series: pd.Series, default: str = "") -> pd.Series:
    if series.empty:
        return series
    out = series.astype(str).replace({"nan": default, "None": default})
    return out.fillna(default)


def _add_scope_columns(df: pd.DataFrame, traffic_scope: str = GLOBAL_NETWORK_SCOPE, safety_scope: str = TARGET_CROSSWALK_SCOPE) -> pd.DataFrame:
    out = df.copy()
    if "traffic_scope" not in out.columns:
        out["traffic_scope"] = traffic_scope
    if "safety_scope" not in out.columns:
        out["safety_scope"] = safety_scope
    return out


def _safe_mean(series: pd.Series) -> float:
    values = pd.to_numeric(series, errors="coerce").dropna()
    return float(values.mean()) if not values.empty else np.nan


def _safe_std(series: pd.Series) -> float:
    values = pd.to_numeric(series, errors="coerce").dropna()
    return float(values.std(ddof=1)) if len(values) > 1 else 0.0 if len(values) == 1 else np.nan


def _safe_se(series: pd.Series) -> float:
    values = pd.to_numeric(series, errors="coerce").dropna()
    if len(values) == 0:
        return np.nan
    std = float(values.std(ddof=1)) if len(values) > 1 else 0.0
    return float(std / np.sqrt(len(values))) if len(values) > 0 else np.nan


def _ci95_from_series(series: pd.Series) -> tuple[float, float]:
    values = pd.to_numeric(series, errors="coerce").dropna()
    if len(values) == 0:
        return np.nan, np.nan
    mean = float(values.mean())
    if len(values) == 1:
        return mean, mean
    se = float(values.std(ddof=1) / np.sqrt(len(values)))
    delta = 1.96 * se
    return mean - delta, mean + delta


def _target_crosswalk_column(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    if "target_crosswalk_id" not in out.columns:
        if "crosswalk_id" in out.columns:
            out["target_crosswalk_id"] = out["crosswalk_id"]
        elif "requested_crosswalk_id" in out.columns:
            out["target_crosswalk_id"] = out["requested_crosswalk_id"]
    if "target_crosswalk_id" in out.columns:
        out["target_crosswalk_id"] = _coerce_text_series(out["target_crosswalk_id"])
    return out


def _safe_alias_row(row: pd.Series | pd.Series, names: list[str]) -> Any:
    for name in names:
        if name in row.index and pd.notna(row[name]):
            return row[name]
    return np.nan


def _first_existing_csv(source_dir: Path, candidates: list[str]) -> tuple[Path | None, pd.DataFrame]:
    for name in candidates:
        direct = source_dir / name
        if direct.exists():
            return direct, _read_csv(direct)
        nested = sorted(source_dir.rglob(name))
        if nested:
            return nested[0], _read_csv(nested[0])
        outputs = source_dir / "outputs" / name
        if outputs.exists():
            return outputs, _read_csv(outputs)
        csv_dir = source_dir / "csv" / name
        if csv_dir.exists():
            return csv_dir, _read_csv(csv_dir)
    return None, pd.DataFrame()


def _normalize_seed_list(seed_spec: str) -> list[int]:
    text = str(seed_spec).strip()
    if not text:
        return []
    if "-" in text and "," not in text and " " not in text:
        start_s, end_s = text.split("-", 1)
        start = int(start_s)
        end = int(end_s)
        step = 1 if end >= start else -1
        return list(range(start, end + step, step))
    seeds: list[int] = []
    for token in text.replace(",", " ").split():
        token = token.strip()
        if token:
            seeds.append(int(token))
    return seeds


def _scenario_source_dir(root_dir: Path, pattern: str, seed: int) -> Path:
    return Path(str(pattern).format(seed=seed))


def _source_summary_path(source_dir: Path) -> tuple[Path | None, pd.DataFrame]:
    return _first_existing_csv(
        source_dir,
        [
            "phase6_smoke_summary.csv",
            "network_simulation_summary_seed.csv",
            "network_simulation_summary.csv",
            "00_run_completion_summary.csv",
        ],
    )


def _source_long_path(source_dir: Path) -> tuple[Path | None, pd.DataFrame]:
    return _first_existing_csv(
        source_dir,
        [
            "per_crosswalk_simulation_results_seed.csv",
            "per_crosswalk_simulation_results.csv",
            "phase6_smoke_per_crosswalk.csv",
        ],
    )


def _source_event_path(source_dir: Path) -> tuple[Path | None, pd.DataFrame]:
    return _first_existing_csv(
        source_dir,
        [
            "extension_events.csv",
            "extension_events_seed.csv",
            "phase6_smoke_extension_events.csv",
        ],
    )


def _source_audit_path(source_dir: Path) -> tuple[Path | None, pd.DataFrame]:
    return _first_existing_csv(
        source_dir,
        [
            "signal_phase_audit.csv",
            "smoke_candidates_resolved.csv",
            "candidate_table_source_diagnostics.csv",
        ],
    )


def _source_impact_path(source_dir: Path) -> tuple[Path | None, pd.DataFrame]:
    return _first_existing_csv(
        source_dir,
        [
            "impact_scope_edge_counts.csv",
            "06_impact_scope_edge_counts.csv",
        ],
    )


def _source_mapping_path(source_dir: Path) -> tuple[Path | None, pd.DataFrame]:
    return _first_existing_csv(
        source_dir,
        [
            "crosswalk_id_mapping.csv",
            "smart_crosswalk_manifest.csv",
            "smoke_candidates_resolved.csv",
            "candidate_table_source_diagnostics.csv",
        ],
    )


def _extract_crosswalk_enrichment(mapping_df: pd.DataFrame) -> pd.DataFrame:
    if mapping_df.empty:
        return pd.DataFrame()
    df = mapping_df.copy()
    if "crosswalk_id" not in df.columns and "requested_crosswalk_id" in df.columns:
        df["crosswalk_id"] = df["requested_crosswalk_id"]
    cols = {}
    for src, dst in [
        ("source_crosswalk_id", "source_crosswalk_id"),
        ("canonical_crosswalk_id", "canonical_crosswalk_id"),
        ("registry_crosswalk_id", "registry_crosswalk_id"),
        ("tls_id", "tls_id"),
        ("ped_link_index", "ped_link_index"),
        ("ped_link_indices", "ped_link_indices"),
        ("ped_link_index", "ped_link_index"),
    ]:
        if src in df.columns:
            cols[dst] = df[src]
    if "crosswalk_id" not in df.columns:
        return pd.DataFrame()
    out = df[[c for c in ["crosswalk_id", "source_crosswalk_id", "canonical_crosswalk_id", "registry_crosswalk_id", "tls_id", "ped_link_index", "ped_link_indices"] if c in df.columns]].copy()
    if "ped_link_index" not in out.columns and "ped_link_indices" in out.columns:
        out["ped_link_index"] = out["ped_link_indices"]
    return out.drop_duplicates(subset=["crosswalk_id"], keep="first")


def _standardize_long_df(source_df: pd.DataFrame, scenario: str, seed: int, source_dir: Path, source_csv: Path | None, mapping_df: pd.DataFrame) -> pd.DataFrame:
    if source_df.empty:
        return pd.DataFrame()
    df = source_df.copy()
    if "seed" not in df.columns:
        df["seed"] = seed
    if "scenario" not in df.columns:
        df["scenario"] = scenario
    df["seed"] = pd.to_numeric(df["seed"], errors="coerce").fillna(seed).astype(int)
    df["scenario"] = df["scenario"].fillna(scenario).astype(str)
    if "crosswalk_id" not in df.columns:
        for alt in ["requested_crosswalk_id", "source_crosswalk_id"]:
            if alt in df.columns:
                df["crosswalk_id"] = df[alt]
                break
    if "crosswalk_id" not in df.columns:
        return pd.DataFrame()
    enrich = _extract_crosswalk_enrichment(mapping_df)
    if not enrich.empty:
        df = df.merge(enrich, on="crosswalk_id", how="left", suffixes=("", "_map"))
    for col, aliases in {
        "source_crosswalk_id": ["source_crosswalk_id", "requested_crosswalk_id", "crosswalk_id"],
        "canonical_crosswalk_id": ["canonical_crosswalk_id"],
        "registry_crosswalk_id": ["registry_crosswalk_id"],
        "tls_id": ["tls_id", "tls_id_used"],
        "ped_link_index": ["ped_link_index", "ped_link_indices", "linkIndex"],
    }.items():
        if col not in df.columns:
            df[col] = np.nan
        for alias in aliases:
            if alias in df.columns:
                df[col] = df[col].where(~df[col].notna(), df[alias])
    if "ped_link_index" in df.columns:
        df["ped_link_index"] = pd.to_numeric(df["ped_link_index"], errors="coerce")
    if "avg_ped_wait_sec" not in df.columns:
        df["avg_ped_wait_sec"] = _first_nonempty_series(df, ["avg_ped_wait_sec", "pedestrian_waiting_time_mean", "average_pedestrian_wait_time", "pedestrian_waiting_time", "pedestrian_wait_time_mean"])
    if "total_extension_sec" not in df.columns:
        df["total_extension_sec"] = _first_nonempty_series(df, ["total_extension_sec", "extension_sec", "pedestrian_green_extension_time", "total_pedestrian_green_extension_time"])
    if "extension_count" not in df.columns:
        df["extension_count"] = _first_nonempty_series(df, ["extension_count", "smart_green_extension_count", "pedestrian_green_extension_count"])
    if "avg_vehicle_delay_sec" not in df.columns:
        df["avg_vehicle_delay_sec"] = _first_nonempty_series(df, ["avg_vehicle_delay_sec", "veh_avg_delay_sec", "average_vehicle_delay", "vehicle_delay"])
    if "avg_queue_length" not in df.columns:
        df["avg_queue_length"] = _first_nonempty_series(df, ["avg_queue_length", "queue_avg", "average_vehicle_queue_length", "vehicle_queue_length", "avg_queue_length"])
    if "max_queue_length" not in df.columns:
        df["max_queue_length"] = _first_nonempty_series(df, ["max_queue_length", "queue_max", "max_vehicle_queue_length"])
    if "network_avg_travel_time_sec" not in df.columns:
        df["network_avg_travel_time_sec"] = _first_nonempty_series(df, ["network_avg_travel_time_sec", "surrounding_total_travel_time_sec"])
    for metric in [
        "PET_A_proxy_count",
        "PET_A_proxy_mean",
        "PET_A_proxy_severe",
        "PET_A_proxy_moderate",
        "PET_A_proxy_safe",
        "PET_B_surrogate_count",
        "PET_B_surrogate_mean",
        "PET_B_surrogate_severe",
        "PET_B_surrogate_moderate",
        "PET_B_surrogate_safe",
        "safety_risk_score",
        "accident_expected_value",
        "elderly_incomplete_crossings",
    ]:
        if metric not in df.columns:
            df[metric] = np.nan
    if source_csv is not None:
        df["source_result_csv"] = str(source_csv)
    else:
        df["source_result_csv"] = str(source_dir)
    df["source_dir"] = str(source_dir)
    if "run_name" not in df.columns:
        df["run_name"] = f"{scenario}_seed{seed}"
    else:
        df["run_name"] = df["run_name"].fillna(f"{scenario}_seed{seed}").astype(str)
    if "output_dir" not in df.columns:
        df["output_dir"] = str(source_dir)
    else:
        df["output_dir"] = df["output_dir"].fillna(str(source_dir)).astype(str)
    for col in ["run_start_time", "run_end_time", "elapsed_sec", "completed", "sim_duration", "warmup", "step_length"]:
        if col not in df.columns:
            df[col] = np.nan
    keep = [
        "run_name",
        "seed",
        "scenario",
        "crosswalk_id",
        "output_dir",
        "run_start_time",
        "run_end_time",
        "elapsed_sec",
        "completed",
        "sim_duration",
        "warmup",
        "step_length",
        "source_crosswalk_id",
        "canonical_crosswalk_id",
        "registry_crosswalk_id",
        "tls_id",
        "ped_link_index",
        "extension_count",
        "total_extension_sec",
        "avg_ped_wait_sec",
        "avg_vehicle_delay_sec",
        "avg_queue_length",
        "max_queue_length",
        "network_avg_travel_time_sec",
        "PET_A_proxy_count",
        "PET_A_proxy_mean",
        "PET_A_proxy_severe",
        "PET_A_proxy_moderate",
        "PET_A_proxy_safe",
        "PET_B_surrogate_count",
        "PET_B_surrogate_mean",
        "PET_B_surrogate_severe",
        "PET_B_surrogate_moderate",
        "PET_B_surrogate_safe",
        "safety_risk_score",
        "accident_expected_value",
        "elderly_incomplete_crossings",
        "source_result_csv",
        "source_dir",
    ]
    return _ensure_columns(df[keep].copy(), keep)


def _first_nonempty_series(df: pd.DataFrame, cols: list[str]) -> pd.Series:
    out = pd.Series([np.nan] * len(df))
    for col in cols:
        if col in df.columns:
            values = pd.to_numeric(df[col], errors="coerce")
            out = out.where(~out.isna(), values)
    return out


def _standardize_summary_row(source_df: pd.DataFrame, scenario: str, seed: int, source_dir: Path, source_csv: Path | None, long_df: pd.DataFrame) -> dict[str, Any]:
    row: dict[str, Any] = {
        "run_name": f"{scenario}_seed{seed}",
        "seed": seed,
        "scenario": scenario,
        "source_dir": str(source_dir),
        "output_dir": str(source_dir),
        "run_start_time": np.nan,
        "run_end_time": np.nan,
        "elapsed_sec": np.nan,
        "completed": False,
        "sim_duration": np.nan,
        "warmup": np.nan,
        "step_length": np.nan,
        "run_completed": False,
        "error_message": "",
        "extension_count": np.nan,
        "total_extension_sec": np.nan,
        "arrived_vehicles": np.nan,
        "network_arrived_vehicles": np.nan,
        "avg_vehicle_delay_sec": np.nan,
        "avg_queue_length": np.nan,
        "max_queue_length": np.nan,
        "network_avg_travel_time_sec": np.nan,
        "safety_risk_score": np.nan,
        "accident_expected_value": np.nan,
        "elderly_incomplete_crossings": np.nan,
        "source_summary_csv": str(source_csv) if source_csv is not None else "",
    }
    if source_df.empty:
        if not long_df.empty:
            row["run_completed"] = True
            row["extension_count"] = float(pd.to_numeric(long_df["extension_count"], errors="coerce").fillna(0).sum())
            row["total_extension_sec"] = float(pd.to_numeric(long_df["total_extension_sec"], errors="coerce").fillna(0).sum())
            row["avg_vehicle_delay_sec"] = float(pd.to_numeric(long_df["avg_vehicle_delay_sec"], errors="coerce").dropna().mean()) if "avg_vehicle_delay_sec" in long_df.columns else np.nan
            row["avg_queue_length"] = float(pd.to_numeric(long_df["avg_queue_length"], errors="coerce").dropna().mean()) if "avg_queue_length" in long_df.columns else np.nan
            row["max_queue_length"] = float(pd.to_numeric(long_df["max_queue_length"], errors="coerce").dropna().max()) if "max_queue_length" in long_df.columns else np.nan
            row["network_avg_travel_time_sec"] = float(pd.to_numeric(long_df["network_avg_travel_time_sec"], errors="coerce").dropna().mean()) if "network_avg_travel_time_sec" in long_df.columns else np.nan
            row["safety_risk_score"] = float(pd.to_numeric(long_df["safety_risk_score"], errors="coerce").dropna().mean()) if "safety_risk_score" in long_df.columns else np.nan
            row["accident_expected_value"] = float(pd.to_numeric(long_df["accident_expected_value"], errors="coerce").dropna().mean()) if "accident_expected_value" in long_df.columns else np.nan
            row["elderly_incomplete_crossings"] = float(pd.to_numeric(long_df["elderly_incomplete_crossings"], errors="coerce").dropna().mean()) if "elderly_incomplete_crossings" in long_df.columns else np.nan
        return row
    r = source_df.iloc[0]
    row["run_completed"] = bool(_value_from_row(r, ["run_completed", "completed_all", "completed", "result_csv_exists"], True))
    row["completed"] = bool(_value_from_row(r, ["completed", "run_completed"], row["run_completed"]))
    status = str(_value_from_row(r, ["status"], "") or "")
    error = _value_from_row(r, ["error_message", "simulation_error", "error", "simulation_error_messages"], "")
    if pd.isna(error):
        error = ""
    row["error_message"] = "" if status.lower() in {"ok", "completed", "success"} else str(error or status)
    row["run_name"] = str(_value_from_row(r, ["run_name"], f"{scenario}_seed{seed}"))
    row["output_dir"] = str(_value_from_row(r, ["output_dir"], source_dir))
    row["run_start_time"] = _value_from_row(r, ["run_start_time"], np.nan)
    row["run_end_time"] = _value_from_row(r, ["run_end_time"], np.nan)
    row["elapsed_sec"] = _value_from_row(r, ["elapsed_sec"], np.nan)
    row["sim_duration"] = _value_from_row(r, ["sim_duration", "simulation_duration_sec"], np.nan)
    row["warmup"] = _value_from_row(r, ["warmup"], np.nan)
    row["step_length"] = _value_from_row(r, ["step_length"], np.nan)
    row["extension_count"] = _value_from_row(r, ["extension_count", "smart_green_extension_count", "pedestrian_green_extension_count"], np.nan)
    row["total_extension_sec"] = _value_from_row(r, ["total_extension_sec", "pedestrian_green_extension_time", "total_pedestrian_green_extension_time", "extension_sec"], np.nan)
    row["arrived_vehicles"] = _value_from_row(r, ["arrived_vehicles", "vehicle_arrived_total", "network_arrived_vehicles"], np.nan)
    row["network_arrived_vehicles"] = _value_from_row(r, ["network_arrived_vehicles", "arrived_vehicles", "vehicle_arrived_total"], np.nan)
    row["avg_vehicle_delay_sec"] = _value_from_row(r, ["avg_vehicle_delay_sec", "veh_avg_delay_sec", "average_vehicle_delay", "vehicle_delay"], np.nan)
    row["avg_queue_length"] = _value_from_row(r, ["avg_queue_length", "queue_avg", "average_vehicle_queue_length", "vehicle_queue_length"], np.nan)
    row["max_queue_length"] = _value_from_row(r, ["max_queue_length", "queue_max", "max_vehicle_queue_length"], np.nan)
    row["network_avg_travel_time_sec"] = _value_from_row(r, ["network_avg_travel_time_sec", "surrounding_total_travel_time_sec"], np.nan)
    row["safety_risk_score"] = _value_from_row(r, ["safety_risk_score"], np.nan)
    row["accident_expected_value"] = _value_from_row(r, ["accident_expected_value"], np.nan)
    row["elderly_incomplete_crossings"] = _value_from_row(r, ["elderly_incomplete_crossings"], np.nan)
    if pd.isna(row["extension_count"]) and not long_df.empty:
        row["extension_count"] = float(pd.to_numeric(long_df["extension_count"], errors="coerce").fillna(0).sum())
    if pd.isna(row["total_extension_sec"]) and not long_df.empty:
        row["total_extension_sec"] = float(pd.to_numeric(long_df["total_extension_sec"], errors="coerce").fillna(0).sum())
    return row


def _standardize_event_df(source_df: pd.DataFrame, scenario: str, seed: int, source_dir: Path, source_csv: Path | None) -> pd.DataFrame:
    if source_df.empty:
        return pd.DataFrame(columns=["seed", "scenario", "time", "crosswalk_id", "tls_id", "ped_link_index", "extension_sec", "detected_ped_count", "source_dir"])
    df = source_df.copy()
    if "seed" not in df.columns:
        df["seed"] = seed
    if "scenario" not in df.columns:
        df["scenario"] = scenario
    df["seed"] = pd.to_numeric(df["seed"], errors="coerce").fillna(seed).astype(int)
    df["scenario"] = df["scenario"].fillna(scenario).astype(str)
    df["time"] = _first_nonempty_series(df, ["time", "sim_time", "time_s", "remaining_before_extension"]).astype(float)
    if "crosswalk_id" not in df.columns:
        for alt in ["crosswalk_ids", "source_crosswalk_id"]:
            if alt in df.columns:
                df["crosswalk_id"] = df[alt].astype(str).str.replace("[\\[\\]\"']", "", regex=True).str.split(",").str[0].str.strip()
                break
    if "tls_id" not in df.columns:
        df["tls_id"] = np.nan
    if "ped_link_index" not in df.columns:
        if "linkIndex" in df.columns:
            df["ped_link_index"] = df["linkIndex"]
        else:
            df["ped_link_index"] = np.nan
    if "extension_sec" not in df.columns:
        df["extension_sec"] = _first_nonempty_series(df, ["extension_sec", "extension_increment_sec"])
    if "detected_ped_count" not in df.columns:
        df["detected_ped_count"] = _first_nonempty_series(df, ["detected_peds_count", "ped_count_on_crossing", "ped_near", "ped_on_crossing_count"])
    if "source_dir" not in df.columns:
        df["source_dir"] = str(source_dir)
    if source_csv is not None:
        df["source_event_csv"] = str(source_csv)
    keep = ["seed", "scenario", "time", "crosswalk_id", "tls_id", "ped_link_index", "extension_sec", "detected_ped_count", "source_dir"]
    return _ensure_columns(df[keep].copy(), keep)


def _safe_int(value: Any) -> int | None:
    if value is None or pd.isna(value):
        return None
    try:
        return int(float(value))
    except Exception:
        return None


def _extension_scope_audit_from_state(state: str, ped_link_index: int | None) -> dict[str, Any]:
    text = "" if state is None or pd.isna(state) else str(state)
    idx = ped_link_index if ped_link_index is not None else -1
    green_indices = [i for i, ch in enumerate(text) if ch in {"G", "g"}]
    ped_state = text[idx] if 0 <= idx < len(text) else ""
    ped_is_green = ped_state in {"G", "g"}
    non_ped_green_indices = [i for i in green_indices if i != idx]
    return {
        "state": text,
        "ped_link_state": ped_state,
        "ped_link_is_green": bool(ped_is_green),
        "non_ped_green_link_indices": "|".join(str(i) for i in non_ped_green_indices),
        "non_ped_green_count": int(len(non_ped_green_indices)),
        "phase_extension_affects_non_ped_green": bool(non_ped_green_indices),
        "scope_verdict": "pedestrian_only"
        if ped_is_green and not non_ped_green_indices
        else "mixed_non_ped_green"
        if ped_is_green
        else "ped_link_not_green",
    }


def _standardize_extension_scope_audit_df(
    source_df: pd.DataFrame,
    source_dir: Path,
    event_csv: Path | None,
    source_audit_csv: Path | None,
    fallback_audit: pd.DataFrame,
) -> pd.DataFrame:
    columns = STANDARD_CSV_SCHEMAS["phase6_extension_scope_audit.csv"]
    if source_df.empty:
        return pd.DataFrame(columns=columns)

    df = source_df.copy()
    if "seed" not in df.columns:
        df["seed"] = np.nan
    if "scenario" not in df.columns:
        df["scenario"] = ""
    if "crosswalk_id" not in df.columns and "crosswalk_ids" in df.columns:
        df["crosswalk_id"] = df["crosswalk_ids"].astype(str).str.replace("[\\[\\]\"']", "", regex=True).str.split(",").str[0].str.strip()
    if "tls_id" not in df.columns:
        df["tls_id"] = np.nan
    if "ped_link_index" not in df.columns:
        if "linkIndex" in df.columns:
            df["ped_link_index"] = df["linkIndex"]
        else:
            df["ped_link_index"] = np.nan
    if "time" not in df.columns:
        df["time"] = _first_nonempty_series(df, ["time", "sim_time", "time_s", "remaining_before_extension"])
    if "phase" not in df.columns:
        df["phase"] = _first_nonempty_series(df, ["phase", "phase_index", "current_phase_index"])
    if "state" not in df.columns:
        df["state"] = _first_nonempty_series(df, ["state", "phase_state", "current_tls_state"])
    if "extension_sec" not in df.columns:
        df["extension_sec"] = _first_nonempty_series(df, ["extension_sec", "extension_increment_sec"])
    if "detected_ped_count" not in df.columns:
        df["detected_ped_count"] = _first_nonempty_series(df, ["detected_ped_count", "detected_peds_count", "ped_count_on_crossing", "ped_near"])
    if "source_dir" not in df.columns:
        df["source_dir"] = str(source_dir)

    audit_lookup: dict[str, int] = {}
    if not fallback_audit.empty and {"crosswalk_id", "ped_link_index"}.issubset(fallback_audit.columns):
        for _, row in fallback_audit.iterrows():
            cid = str(row.get("crosswalk_id", "")).strip()
            ped_idx = _safe_int(row.get("ped_link_index"))
            if cid and ped_idx is not None:
                audit_lookup[cid] = ped_idx

    rows: list[dict[str, Any]] = []
    for _, row in df.iterrows():
        cid = str(row.get("crosswalk_id", "")).strip()
        seed_val = _safe_int(row.get("seed"))
        scenario = str(row.get("scenario", "")).strip()
        tls_id = row.get("tls_id", np.nan)
        ped_link_index = _safe_int(row.get("ped_link_index"))
        if ped_link_index is None and cid in audit_lookup:
            ped_link_index = audit_lookup[cid]
        scope = _extension_scope_audit_from_state(row.get("state", ""), ped_link_index)
        rows.append(
            {
                "seed": seed_val if seed_val is not None else np.nan,
                "scenario": scenario,
                "crosswalk_id": cid,
                "time": pd.to_numeric(row.get("time", np.nan), errors="coerce"),
                "phase": _safe_int(row.get("phase")),
                "tls_id": tls_id,
                "state": scope["state"],
                "ped_link_index": ped_link_index if ped_link_index is not None else np.nan,
                "ped_link_state": scope["ped_link_state"],
                "ped_link_is_green": bool(scope["ped_link_is_green"]),
                "non_ped_green_link_indices": scope["non_ped_green_link_indices"],
                "non_ped_green_count": int(scope["non_ped_green_count"]),
                "phase_extension_affects_non_ped_green": bool(scope["phase_extension_affects_non_ped_green"]),
                "baseline_extension_event_error": scenario == "baseline",
                "extension_sec": pd.to_numeric(row.get("extension_sec", np.nan), errors="coerce"),
                "detected_ped_count": pd.to_numeric(row.get("detected_ped_count", np.nan), errors="coerce"),
                "source_dir": str(row.get("source_dir", source_dir)),
                "source_event_csv": str(event_csv) if event_csv is not None else "",
                "source_audit_csv": str(source_audit_csv) if source_audit_csv is not None else "",
                "scope_verdict": "baseline_extension_error" if scenario == "baseline" else scope["scope_verdict"],
            }
        )

    out = pd.DataFrame(rows)
    return _ensure_columns(out[columns].copy(), columns)


def _standardize_audit_df(source_df: pd.DataFrame, source_dir: Path, source_csv: Path | None, fallback_long: pd.DataFrame) -> pd.DataFrame:
    if not source_df.empty and {"crosswalk_id", "tls_id", "ped_link_index"}.issubset(source_df.columns):
        df = source_df.copy()
        if "ped_has_green" not in df.columns:
            df["ped_has_green"] = df.get("has_green_G_or_g", False)
        if "ped_has_red" not in df.columns:
            df["ped_has_red"] = df.get("has_red_r", False)
        if "pedestrian_only_phase_available" not in df.columns:
            mixed = df["mixed_phase_only"].astype(bool) if "mixed_phase_only" in df.columns else pd.Series([False] * len(df))
            df["pedestrian_only_phase_available"] = ~mixed
        if "mixed_phase_only" not in df.columns:
            df["mixed_phase_only"] = False
        if "phase_extension_affects_non_ped_green" not in df.columns:
            df["phase_extension_affects_non_ped_green"] = False
        df["audit_source"] = str(source_csv) if source_csv is not None else str(source_dir)
        keep = [
            "crosswalk_id",
            "tls_id",
            "ped_link_index",
            "ped_has_green",
            "ped_has_red",
            "pedestrian_only_phase_available",
            "mixed_phase_only",
            "phase_extension_affects_non_ped_green",
            "audit_source",
        ]
        return _ensure_columns(df[keep].copy(), keep)
    if fallback_long.empty:
        return pd.DataFrame()
    rows = []
    for crosswalk_id, group in fallback_long.groupby("crosswalk_id", dropna=False):
        row = group.iloc[0]
        rows.append(
            {
                "crosswalk_id": crosswalk_id,
                "tls_id": _value_from_row(row, ["tls_id"], np.nan),
                "ped_link_index": _value_from_row(row, ["ped_link_index"], np.nan),
                "ped_has_green": False,
                "ped_has_red": False,
                "pedestrian_only_phase_available": False,
                "mixed_phase_only": False,
                "phase_extension_affects_non_ped_green": False,
                "audit_source": "file_missing_placeholder",
            }
        )
    return pd.DataFrame(rows)


def _merge_metric_delta_df(baseline_df: pd.DataFrame, smart_df: pd.DataFrame) -> pd.DataFrame:
    join_cols = ["seed", "crosswalk_id"]
    if baseline_df.empty or smart_df.empty:
        return pd.DataFrame(columns=[
            "seed",
            "crosswalk_id",
            "paired_ok",
        ])
    base = baseline_df.copy()
    smart = smart_df.copy()
    base = base.rename(columns={c: f"{c}_baseline" for c in base.columns if c not in join_cols})
    smart = smart.rename(columns={c: f"{c}_smart" for c in smart.columns if c not in join_cols})
    merged = base.merge(smart, on=join_cols, how="inner")
    merged["paired_ok"] = True
    delta_cols = {
        "extension_count": "extension_count_delta",
        "total_extension_sec": "total_extension_sec_delta",
        "avg_ped_wait_sec": "avg_ped_wait_sec_delta",
        "avg_vehicle_delay_sec": "avg_vehicle_delay_sec_delta",
        "avg_queue_length": "avg_queue_length_delta",
        "max_queue_length": "max_queue_length_delta",
        "network_avg_travel_time_sec": "network_avg_travel_time_sec_delta",
        "PET_A_proxy_count": "PET_A_proxy_count_delta",
        "PET_A_proxy_mean": "PET_A_proxy_mean_delta",
        "PET_A_proxy_severe": "PET_A_proxy_severe_delta",
        "PET_A_proxy_moderate": "PET_A_proxy_moderate_delta",
        "PET_A_proxy_safe": "PET_A_proxy_safe_delta",
        "PET_B_surrogate_count": "PET_B_surrogate_count_delta",
        "PET_B_surrogate_mean": "PET_B_surrogate_mean_delta",
        "PET_B_surrogate_severe": "PET_B_surrogate_severe_delta",
        "PET_B_surrogate_moderate": "PET_B_surrogate_moderate_delta",
        "PET_B_surrogate_safe": "PET_B_surrogate_safe_delta",
        "safety_risk_score": "safety_risk_score_delta",
        "accident_expected_value": "accident_expected_delta",
        "elderly_incomplete_crossings": "elderly_incomplete_crossings_delta",
    }
    for src, dst in delta_cols.items():
        left = pd.to_numeric(merged.get(f"{src}_smart"), errors="coerce")
        right = pd.to_numeric(merged.get(f"{src}_baseline"), errors="coerce")
        merged[dst] = left - right
    out_cols = ["seed", "crosswalk_id", "paired_ok"]
    for src in delta_cols:
        out_cols.extend([f"{src}_baseline", f"{src}_smart", f"{delta_cols[src]}"])
    return merged[out_cols].copy()


def _summarize_by_crosswalk(delta_df: pd.DataFrame) -> pd.DataFrame:
    if delta_df.empty:
        return pd.DataFrame(columns=["crosswalk_id", "metric", "n", "mean_delta", "std_delta", "se_delta", "ci95_low", "ci95_high", "min_delta", "max_delta", "interpretation_hint"])
    metric_cols = [c for c in delta_df.columns if c.endswith("_delta") and c not in {"paired_ok"}]
    rows: list[dict[str, Any]] = []
    for crosswalk_id, group in delta_df.groupby("crosswalk_id", dropna=False):
        for metric in metric_cols:
            values = pd.to_numeric(group[metric], errors="coerce").dropna()
            n = int(values.shape[0])
            if n == 0:
                continue
            mean = float(values.mean())
            std = float(values.std(ddof=1)) if n > 1 else 0.0
            se = float(std / np.sqrt(n)) if n > 0 else np.nan
            ci = 1.96 * se if n > 1 else 0.0
            rows.append(
                {
                    "crosswalk_id": crosswalk_id,
                    "metric": metric,
                    "n": n,
                    "mean_delta": mean,
                    "std_delta": std,
                    "se_delta": se,
                    "ci95_low": mean - ci,
                    "ci95_high": mean + ci,
                    "min_delta": float(values.min()),
                    "max_delta": float(values.max()),
                    "interpretation_hint": "mean_delta > 0 means smart increases the metric relative to baseline; mean_delta < 0 means smart decreases it; check ci95 for uncertainty",
                }
            )
    return pd.DataFrame(rows)


def _source_inventory_rows(root_dir: Path, seed_sources: list[tuple[int, str, Path]]) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    tried = [
        "phase6_smoke_summary.csv",
        "per_crosswalk_simulation_results_seed.csv",
        "per_crosswalk_simulation_results.csv",
        "network_simulation_summary_seed.csv",
        "network_simulation_summary.csv",
        "extension_events.csv",
        "extension_events_seed.csv",
        "signal_phase_audit.csv",
        "smoke_candidates_resolved.csv",
        "candidate_table_source_diagnostics.csv",
        "impact_scope_edge_counts.csv",
        "06_impact_scope_edge_counts.csv",
        "crosswalk_id_mapping.csv",
        "smart_crosswalk_manifest.csv",
    ]
    for seed, scenario, source_dir in seed_sources:
        for name in tried:
            p = source_dir / name
            if not p.exists():
                nested = sorted(source_dir.rglob(name))
                p = nested[0] if nested else p
            rows.append(
                {
                    "source_dir": str(source_dir),
                    "file_path": str(p.relative_to(source_dir)) if p.exists() and p.is_relative_to(source_dir) else name,
                    "exists": bool(p.exists()),
                    "size_mb": round(p.stat().st_size / (1024 * 1024), 6) if p.exists() else 0.0,
                    "role": f"{scenario}_seed_{seed}_input",
                    "used_in_merge": bool(p.exists()),
                    "note": "merge_input_candidate" if p.exists() else "file_missing",
                }
            )
    return pd.DataFrame(rows)


def merge_30seed_results(
    root_dir: str | Path,
    baseline_pattern: str,
    smart_pattern: str,
    seeds: str,
    output_dir: str | Path,
) -> dict[str, Any]:
    root = Path(root_dir).resolve()
    output_root = Path(output_dir).resolve()
    _ensure_dir(output_root / "csv")
    _ensure_dir(output_root / "readme")
    _ensure_dir(output_root / "figures")
    _ensure_dir(output_root / "archive")
    _ensure_dir(output_root / "csv" / "debug")
    _ensure_dir(output_root / "csv" / "results")
    _ensure_dir(output_root / "csv" / "safety")

    seed_list = _normalize_seed_list(seeds)
    seed_sources: list[tuple[int, str, Path]] = []
    summary_rows: list[dict[str, Any]] = []
    long_frames: list[pd.DataFrame] = []
    event_frames: list[pd.DataFrame] = []
    scope_audit_frames: list[pd.DataFrame] = []
    audit_frames: list[pd.DataFrame] = []
    impact_frames: list[pd.DataFrame] = []
    missing_notes: list[str] = []
    source_inventory = _build_source_file_inventory(root, {})

    for seed in seed_list:
        for scenario, pattern in [("baseline", baseline_pattern), ("smart", smart_pattern)]:
            source_dir = (root / _scenario_source_dir(root, pattern, seed)).resolve()
            seed_sources.append((seed, scenario, source_dir))
            if not source_dir.exists():
                missing_notes.append(f"{scenario} seed {seed} source_dir 없음: {source_dir}")
            summary_path, summary_df = _source_summary_path(source_dir)
            long_path, long_df_raw = _source_long_path(source_dir)
            event_path, event_df_raw = _source_event_path(source_dir)
            audit_path, audit_df_raw = _source_audit_path(source_dir)
            impact_path, impact_df_raw = _source_impact_path(source_dir)
            mapping_path, mapping_df = _source_mapping_path(source_dir)

            long_df = _standardize_long_df(long_df_raw, scenario, seed, source_dir, long_path, mapping_df)
            summary_rows.append(_standardize_summary_row(summary_df, scenario, seed, source_dir, summary_path, long_df))
            if not long_df.empty:
                long_frames.append(long_df)
            event_df = _standardize_event_df(event_df_raw, scenario, seed, source_dir, event_path)
            if not event_df.empty:
                event_frames.append(event_df)
            scope_audit_df = _standardize_extension_scope_audit_df(
                event_df_raw,
                source_dir,
                event_path,
                audit_path,
                audit_df_raw,
            )
            if not scope_audit_df.empty:
                scope_audit_frames.append(scope_audit_df)
            audit_df = _standardize_audit_df(audit_df_raw, source_dir, audit_path, long_df)
            if not audit_df.empty:
                audit_frames.append(audit_df)
            if not impact_df_raw.empty:
                impact_df = impact_df_raw.copy()
                impact_df["seed"] = seed
                impact_df["scenario"] = scenario
                impact_df["source_dir"] = str(source_dir)
                impact_frames.append(impact_df)

    run_summary_df = pd.DataFrame(summary_rows)
    long_df = pd.concat(long_frames, ignore_index=True) if long_frames else pd.DataFrame(columns=[
        "seed",
        "scenario",
        "crosswalk_id",
        "source_crosswalk_id",
        "canonical_crosswalk_id",
        "registry_crosswalk_id",
        "tls_id",
        "ped_link_index",
        "extension_count",
        "total_extension_sec",
        "avg_ped_wait_sec",
        "avg_vehicle_delay_sec",
        "avg_queue_length",
        "max_queue_length",
        "network_avg_travel_time_sec",
        "PET_A_proxy_count",
        "PET_A_proxy_mean",
        "PET_A_proxy_severe",
        "PET_A_proxy_moderate",
        "PET_A_proxy_safe",
        "PET_B_surrogate_count",
        "PET_B_surrogate_mean",
        "PET_B_surrogate_severe",
        "PET_B_surrogate_moderate",
        "PET_B_surrogate_safe",
        "safety_risk_score",
        "accident_expected_value",
        "elderly_incomplete_crossings",
        "source_result_csv",
        "source_dir",
    ])
    paired_df = _merge_metric_delta_df(
        long_df[long_df["scenario"] == "baseline"].copy(),
        long_df[long_df["scenario"] == "smart"].copy(),
    )
    summary_df = _summarize_by_crosswalk(paired_df)
    event_df = pd.concat(event_frames, ignore_index=True) if event_frames else pd.DataFrame(columns=["seed", "scenario", "time", "crosswalk_id", "tls_id", "ped_link_index", "extension_sec", "detected_ped_count", "source_dir"])
    scope_audit_df = pd.concat(scope_audit_frames, ignore_index=True) if scope_audit_frames else pd.DataFrame(columns=STANDARD_CSV_SCHEMAS["phase6_extension_scope_audit.csv"])
    audit_df = pd.concat(audit_frames, ignore_index=True) if audit_frames else pd.DataFrame(columns=["crosswalk_id", "tls_id", "ped_link_index", "ped_has_green", "ped_has_red", "pedestrian_only_phase_available", "mixed_phase_only", "phase_extension_affects_non_ped_green", "audit_source"])
    impact_df = pd.concat(impact_frames, ignore_index=True) if impact_frames else pd.DataFrame(columns=["seed", "scenario", "crosswalk_id", "scope", "unique_edge_count", "impact_edge_source_main", "source_impact_edges_csv", "source_dir"])
    if not impact_df.empty and "source_dir" not in impact_df.columns:
        impact_df["source_dir"] = np.nan

    # Keep source inventory broad, including missing files.
    source_inventory = _build_source_file_inventory(root, {})

    _write_csv_with_schema(run_summary_df, output_root / "csv" / "00_run_completion_summary.csv", STANDARD_CSV_SCHEMAS["00_run_completion_summary.csv"])
    _write_csv_with_schema(long_df, output_root / "csv" / "01_all_results_long.csv", STANDARD_CSV_SCHEMAS["01_all_results_long.csv"])
    _write_csv_with_schema(paired_df, output_root / "csv" / "02_baseline_smart_paired_delta.csv", STANDARD_CSV_SCHEMAS["02_baseline_smart_paired_delta.csv"])
    _write_csv_with_schema(summary_df, output_root / "csv" / "03_paired_summary_by_crosswalk.csv", STANDARD_CSV_SCHEMAS["03_paired_summary_by_crosswalk.csv"])
    _write_csv_with_schema(event_df, output_root / "csv" / "04_extension_events_all.csv", STANDARD_CSV_SCHEMAS["04_extension_events_all.csv"])
    _write_csv_with_schema(audit_df, output_root / "csv" / "05_signal_phase_audit.csv", STANDARD_CSV_SCHEMAS["05_signal_phase_audit.csv"])
    _write_csv_with_schema(impact_df, output_root / "csv" / "06_impact_scope_edge_counts.csv", STANDARD_CSV_SCHEMAS["06_impact_scope_edge_counts.csv"])
    _write_csv_with_schema(source_inventory, output_root / "csv" / "07_source_file_inventory.csv", STANDARD_CSV_SCHEMAS["07_source_file_inventory.csv"])
    _write_csv_with_schema(scope_audit_df, output_root / "csv" / "phase6_extension_scope_audit.csv", STANDARD_CSV_SCHEMAS["phase6_extension_scope_audit.csv"])

    bundle = SourceBundle(source_root=root, source_kind="merged_30seed", csv_dir=output_root / "csv", files={}, notes=missing_notes)
    csv_results = {name: _read_csv(output_root / "csv" / name) for name in STANDARD_CSVS}
    root_readme = _format_root_readme(bundle, output_root, csv_results)
    (output_root / "README_PHASE6_RESULTS.md").write_text(root_readme + "\n", encoding="utf-8")
    for csv_name in STANDARD_CSVS:
        readme_text = _format_metric_readme(csv_name, csv_results[csv_name], str(output_root / "csv" / csv_name), "merged 30seed result")
        (output_root / "readme" / STANDARD_READMES[STANDARD_CSVS.index(csv_name)]).write_text(readme_text + "\n", encoding="utf-8")
    structured = _write_structured_phase6_outputs(
        output_root,
        bundle,
        run_summary_df,
        long_df,
        paired_df,
        summary_df,
        event_df,
        audit_df,
        impact_df,
        source_inventory,
        scope_audit_df=scope_audit_df,
    )

    metadata = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "root_dir": str(root),
        "output_root": str(output_root),
        "baseline_pattern": baseline_pattern,
        "smart_pattern": smart_pattern,
        "seeds": seed_list,
        "seed_spec": seeds,
        "source_kind": "merged_30seed",
        "merge_mode": "baseline_smart_paired",
        "notes": missing_notes,
        "standard_csvs": STANDARD_CSVS,
        "structured_csvs": {
            "debug": STRUCTURED_DEBUG_CSVS,
            "results": STRUCTURED_RESULT_CSVS,
            "safety": STRUCTURED_SAFETY_CSVS,
        },
    }
    (output_root / "run_metadata.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    return metadata


def _format_metric_readme(csv_name: str, df: pd.DataFrame, source_desc: str, note: str) -> str:
    if df.empty and not df.columns.tolist():
        actual_summary = "파일 없음"
        core_cols = "파일 없음"
        row_meaning = "파일 없음"
        questions = "파일 없음"
        good_bad = "파일 없음"
        caution = "파일 없음"
        report_sentence = "파일 없음"
    else:
        row_meaning = "행 1개는 CSV의 원래 행 의미를 그대로 따른다."
        core_cols = ", ".join(f"`{c}`" for c in list(df.columns)[:8]) if len(df.columns) else "파일 없음"
        questions = "이 파일이 무엇을 요약하는지, 값이 0/음수/결측인지, delta의 부호가 어떤 의미인지 확인한다."
        good_bad = "좋음: 핵심 지표가 채워지고, delta는 해석 가능한 방향성을 가진다. 나쁨: 파일이 비어 있거나 핵심 컬럼이 없거나 전부 결측이다."
        caution = "이 파일의 숫자는 실제 사고가 아니라 surrogate/proxy일 수 있으니, 파일 맥락을 확인해야 한다."
        if csv_name == "00_run_completion_summary.csv":
            report_sentence = "실행 완료 여부와 차량/extension 집계를 먼저 확인한다."
        elif csv_name == "01_all_results_long.csv":
            report_sentence = "원자료가 long format으로 정리되어 paired 비교의 근거가 된다."
        elif csv_name == "02_baseline_smart_paired_delta.csv":
            report_sentence = "baseline 대비 smart 차이는 이 paired delta 파일이 1차 근거다."
        elif csv_name == "03_paired_summary_by_crosswalk.csv":
            report_sentence = "crosswalk별 delta 요약은 방향성과 CI를 동시에 보기 좋다."
        elif csv_name == "04_extension_events_all.csv":
            report_sentence = "extension event가 실제로 언제, 어디서, 몇 번 발생했는지 확인한다."
        elif csv_name == "05_signal_phase_audit.csv":
            report_sentence = "signal phase audit는 ped linkIndex 위치가 실제로 green인지 확인할 때 쓴다."
        elif csv_name == "06_impact_scope_edge_counts.csv":
            report_sentence = "impact scope edge counts는 반경별 영향권 크기를 판단하는 참고 자료다."
        elif csv_name == "phase6_extension_scope_audit.csv":
            report_sentence = "extension scope audit는 ped link만 green인지, 차량 green이 함께 연장됐는지 확인한다."
        else:
            report_sentence = "source inventory는 재현성과 정리 상태를 확인하는 자료다."
        sample_cols = list(df.columns)[: min(5, len(df.columns))]
        preview = {c: _sample_values(df, c) for c in sample_cols}
        actual_summary = f"source={source_desc}; rows={len(df)}; columns={len(df.columns)}; preview={json.dumps(preview, ensure_ascii=False)}"
    text = [
        f"# {csv_name} README",
        "",
        f"- 이 파일은 무엇인가: {note or 'standard phase 6 csv'}.",
        f"- 행 하나가 의미하는 것: {row_meaning}",
        f"- 핵심 컬럼: {core_cols}",
        f"- 이 파일에서 확인할 질문: {questions}",
        f"- 좋은 결과/나쁜 결과 기준: {good_bad}",
        f"- 현재 데이터에서 실제로 나온 요약: {actual_summary}",
        f"- 주의사항: {caution}",
        f"- 보고서용 문장: {report_sentence}",
        "",
    ]
    return "\n".join(text)


def _format_root_readme(bundle: SourceBundle, output_root: Path, csv_results: dict[str, pd.DataFrame]) -> str:
    run_summary = csv_results.get("00_run_completion_summary.csv", pd.DataFrame())
    paired = csv_results.get("02_baseline_smart_paired_delta.csv", pd.DataFrame())
    ext = csv_results.get("04_extension_events_all.csv", pd.DataFrame())
    audit = csv_results.get("05_signal_phase_audit.csv", pd.DataFrame())
    scope_audit = csv_results.get("phase6_extension_scope_audit.csv", pd.DataFrame())
    inv = csv_results.get("07_source_file_inventory.csv", pd.DataFrame())

    if not run_summary.empty:
        seed_count = int(run_summary["seed"].nunique()) if "seed" in run_summary.columns else 0
        scenario_count = int(run_summary["scenario"].nunique()) if "scenario" in run_summary.columns else 0
        run_count = int(len(run_summary))
    else:
        seed_count = scenario_count = run_count = 0
    paired_count = int(len(paired)) if not paired.empty else 0
    ext_count = int(len(ext)) if not ext.empty else 0
    audit_count = int(len(audit)) if not audit.empty else 0
    scope_audit_count = int(len(scope_audit)) if not scope_audit.empty else 0
    inv_count = int(len(inv)) if not inv.empty else 0

    if bundle.source_kind == "phase6_standard":
        experiment_type = "main experiment / human pack"
    elif run_count <= 2 or seed_count <= 1:
        experiment_type = "smoke"
    else:
        experiment_type = "main experiment"

    metric_locations = []
    for metric in METRIC_COLUMNS:
        found_at = []
        for csv_name, df in csv_results.items():
            if metric in df.columns:
                found_at.append(csv_name)
        metric_locations.append((metric, ", ".join(found_at) if found_at else "파일 없음"))

    lines = [
        "# Phase 6 Results README",
        "",
        f"- source_root: `{bundle.source_root}`",
        f"- standardized_output: `{output_root}`",
        f"- source_kind: `{bundle.source_kind}`",
        f"- 판정: `{experiment_type}`",
        f"- seed_count: `{seed_count}`",
        f"- scenario_count: `{scenario_count}`",
        f"- run_count: `{run_count}`",
        f"- paired_row_count: `{paired_count}`",
        f"- extension_event_count: `{ext_count}`",
        f"- signal_audit_row_count: `{audit_count}`",
        f"- extension_scope_audit_row_count: `{scope_audit_count}`",
        f"- source_inventory_row_count: `{inv_count}`",
        "",
        "## 실험 개요",
        "",
        "- baseline/smart 비교 구조: paired delta와 crosswalk별 summary를 함께 본다.",
        "- seed 반복 구조: 같은 seed를 baseline과 smart에 맞춰 비교한다.",
        "- 이 결과는 30 seed paired baseline/smart 결과를 한 폴더에 합친 표준 human pack이다." if bundle.source_kind == "merged_30seed" else "- 이 결과는 단일 run 결과를 human pack 형식으로 정리한 것이다.",
        "- CSV files are intentionally English-only for encoding stability and downstream analysis.",
        "- Header-only CSVs mean the smoke output was failed or incomplete and no recognized result CSV was available.",
        f"- traffic_scope = `{GLOBAL_NETWORK_SCOPE}` 이면 차량/혼잡 지표는 global network 기준이다.",
        f"- safety_scope = `{TARGET_CROSSWALK_SCOPE}` 이면 안전/사고 proxy 지표는 target crosswalk only 기준이다.",
        "- 사고 대체 지표 위치: 아래 표에서 실제 컬럼 위치를 확인한다.",
        "- 보행자 편익 지표 위치: `pedestrian_wait*`, `elderly_*`, `PET_*` 계열을 각 CSV에서 확인한다.",
        "- 차량 지체 지표 위치: `veh_*`, `avg_vehicle_delay_sec`, `network_avg_travel_time_sec`, `vehicle_delay_cost`를 확인한다.",
        "- extension event 해석: event가 있어도 mixed phase면 최종 정책 효과로 단정하지 않는다.",
        "- signal phase audit 해석: ped linkIndex 위치가 실제로 green인지 확인한다.",
        "- extension scope audit 해석: ped link만 green인지, 차량 green이 함께 연장됐는지 event별로 확인한다.",
        "- pedestrian-only / mixed phase 주의사항: mixed phase는 차량 green 연장 위험이 있어 별도 검증이 필요하다.",
        f"- smoke인지 본실험인지: `{experiment_type}`",
        "",
    ]
    if not scope_audit.empty:
        smart_rows = scope_audit[scope_audit["scenario"].astype(str) == "smart"] if "scenario" in scope_audit.columns else scope_audit
        baseline_rows = scope_audit[scope_audit["scenario"].astype(str) == "baseline"] if "scenario" in scope_audit.columns else pd.DataFrame()
        smart_total = int(len(smart_rows))
        smart_ped_only = int((smart_rows["scope_verdict"].astype(str) == "pedestrian_only").sum()) if smart_total and "scope_verdict" in smart_rows.columns else 0
        smart_mixed = int((smart_rows["scope_verdict"].astype(str) == "mixed_non_ped_green").sum()) if smart_total and "scope_verdict" in smart_rows.columns else 0
        baseline_error = int((baseline_rows["baseline_extension_event_error"].astype(str).str.lower() == "true").sum()) if not baseline_rows.empty and "baseline_extension_event_error" in baseline_rows.columns else 0
        smart_all_ped_only = smart_total > 0 and smart_ped_only == smart_total and smart_mixed == 0
        lines.extend([
            "## extension scope audit",
            "",
            f"- smart extension event all pedestrian-only: {'yes' if smart_all_ped_only else 'no'}",
            f"- smart pedestrian-only event count: `{smart_ped_only}` / `{smart_total}`",
            f"- smart mixed non-ped green event count: `{smart_mixed}`",
            f"- baseline extension event error count: `{baseline_error}`",
            "",
        ])
    vehicle_delay = pd.to_numeric(run_summary.get("avg_vehicle_delay_sec", pd.Series(dtype=float)), errors="coerce") if not run_summary.empty else pd.Series(dtype=float)
    network_travel = pd.to_numeric(run_summary.get("network_avg_travel_time_sec", pd.Series(dtype=float)), errors="coerce") if not run_summary.empty else pd.Series(dtype=float)
    if vehicle_delay.dropna().empty and network_travel.dropna().empty:
        lines.extend([
            "vehicle flow impact not evaluated in this smoke",
            "",
        ])
    lines.extend([
        "## 사고 대체 / proxy 지표 위치",
        "",
    ])
    for metric, location in metric_locations:
        lines.append(f"- `{metric}`: {location}")
    lines.extend([
        "",
        "## 다음 명령어",
        "",
        "```bash",
        "python3 -m smart_crosswalk_sumo.phase6_result_standardizer standardize \\",
        f"  --input-dir {bundle.source_root} \\",
        f"  --output-dir {output_root}",
        "```",
        "",
        "```bash",
        "python3 -m smart_crosswalk_sumo.phase6_result_standardizer cleanup \\",
        "  --result-root result \\",
        "  --output-csv result/result_cleanup_candidates.csv",
        "```",
        "",
        "## 표준 CSV",
        "",
    ])
    for csv_name in STANDARD_CSVS:
        lines.append(f"- `{csv_name}`")
    lines.extend([
        "",
        "## 주의사항",
        "",
        "- 파일이 없으면 README에서 파일 없음으로 기록한다.",
        "- accident_expected_value는 실제 사고 건수가 아닌 사고 위험 proxy다.",
        "- PET 계열은 surrogate safety proxy로 읽는다.",
        "- high_risk_event_count는 시뮬레이션 내 surrogate 위험 상황 횟수다.",
        "- 차량/혼잡 지표는 global network 기준이고, 안전/사고 proxy 지표는 target crosswalk only 기준이다.",
        "",
    ])
    return "\n".join(lines)


def _copy_figures(source_root: Path, output_figures: Path) -> list[Path]:
    copied: list[Path] = []
    for fig_root in [source_root / "figures", source_root / "figure", source_root / "figs"]:
        if not fig_root.exists() or not fig_root.is_dir():
            continue
        for path in fig_root.rglob("*"):
            if not path.is_file():
                continue
            rel = path.relative_to(fig_root)
            dst = output_figures / rel
            _ensure_dir(dst.parent)
            shutil.copy2(path, dst)
            copied.append(dst)
    return copied


def standardize_phase6_result_folder(input_dir: str | Path, output_dir: str | Path) -> dict[str, Any]:
    bundle = _collect_source_bundle(Path(input_dir))
    output_root = Path(output_dir).resolve()
    csv_dir = output_root / "csv"
    readme_dir = output_root / "readme"
    figures_dir = output_root / "figures"
    archive_dir = output_root / "archive"
    for path in [csv_dir, readme_dir, figures_dir, archive_dir]:
        _ensure_dir(path)
    _ensure_dir(csv_dir / "debug")
    _ensure_dir(csv_dir / "results")
    _ensure_dir(csv_dir / "safety")

    csv_results: dict[str, pd.DataFrame] = {}
    source_map: dict[str, str] = {}
    notes: dict[str, str] = {}
    for csv_name in STANDARD_CSVS:
        df, source_desc, note = _load_standard_csv(bundle, csv_name)
        csv_results[csv_name] = df
        source_map[csv_name] = source_desc
        notes[csv_name] = note
        _write_csv_with_schema(df, csv_dir / csv_name, STANDARD_CSV_SCHEMAS.get(csv_name))
    extra_notes = list(bundle.notes)
    if not any(not df.empty for df in csv_results.values()):
        extra_notes.extend(["no_recognized_result_csv", "failed_or_incomplete_smoke_output", "structured_csv_headers_only"])
    if extra_notes != bundle.notes:
        bundle = SourceBundle(
            source_root=bundle.source_root,
            source_kind=bundle.source_kind,
            csv_dir=bundle.csv_dir,
            files=bundle.files,
            notes=extra_notes,
        )
    for csv_name in STANDARD_CSVS:
        readme_text = _format_metric_readme(csv_name, csv_results[csv_name], source_map[csv_name], notes[csv_name])
        (readme_dir / STANDARD_READMES[STANDARD_CSVS.index(csv_name)]).write_text(readme_text + "\n", encoding="utf-8")
    root_readme = _format_root_readme(bundle, output_root, csv_results)
    (output_root / "README_PHASE6_RESULTS.md").write_text(root_readme + "\n", encoding="utf-8")

    copied_figures = _copy_figures(bundle.source_root, figures_dir)
    structured = _write_structured_phase6_outputs(
        output_root,
        bundle,
        csv_results.get("00_run_completion_summary.csv", pd.DataFrame()),
        csv_results.get("01_all_results_long.csv", pd.DataFrame()),
        csv_results.get("02_baseline_smart_paired_delta.csv", pd.DataFrame()),
        csv_results.get("03_paired_summary_by_crosswalk.csv", pd.DataFrame()),
        csv_results.get("04_extension_events_all.csv", pd.DataFrame()),
        csv_results.get("05_signal_phase_audit.csv", pd.DataFrame()),
        csv_results.get("06_impact_scope_edge_counts.csv", pd.DataFrame()),
        csv_results.get("07_source_file_inventory.csv", pd.DataFrame()),
    )
    metadata = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "source_root": str(bundle.source_root),
        "source_kind": bundle.source_kind,
        "output_root": str(output_root),
        "csv_dir": str(csv_dir),
        "readme_dir": str(readme_dir),
        "figures_dir": str(figures_dir),
        "archive_dir": str(archive_dir),
        "source_files_seen": len(bundle.files),
        "standard_csvs": STANDARD_CSVS,
        "structured_csvs": {
            "debug": STRUCTURED_DEBUG_CSVS,
            "results": STRUCTURED_RESULT_CSVS,
            "safety": STRUCTURED_SAFETY_CSVS,
        },
        "copied_figures": [str(p) for p in copied_figures],
        "notes": bundle.notes,
    }
    (output_root / "run_metadata.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    return metadata


def build_safety_proxy_metric_inventory(result_dirs: Iterable[str | Path]) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for result_dir in result_dirs:
        result_path = Path(result_dir)
        for csv_path in sorted(result_path.rglob("*.csv")):
            try:
                df = _read_csv(csv_path)
            except Exception:
                df = pd.DataFrame()
            for metric in METRIC_COLUMNS:
                exists = metric in df.columns
                non_null = _safe_non_null_count(df, metric)
                rows.append(
                    {
                        "result_dir": str(result_path),
                        "csv_file": str(csv_path.relative_to(result_path)) if csv_path.is_relative_to(result_path) else str(csv_path),
                        "metric_column": metric,
                        "exists": bool(exists),
                        "non_null_count": int(non_null),
                        "sample_values": _sample_values(df, metric),
                        "interpretation": _metric_interpretation(metric),
                        "caution": _metric_caution(metric, exists, non_null),
                    }
                )
    return pd.DataFrame(rows)


def build_phase6_standard_folder_audit(folder: str | Path) -> pd.DataFrame:
    root = Path(folder)
    rows: list[dict[str, Any]] = []
    rows.append({"item_type": "directory", "expected_name": "csv", "actual_path": str(root / "csv"), "exists": (root / "csv").exists(), "note": "standard csv directory"})
    rows.append({"item_type": "directory", "expected_name": "readme", "actual_path": str(root / "readme"), "exists": (root / "readme").exists(), "note": "standard readme directory"})
    rows.append({"item_type": "directory", "expected_name": "figures", "actual_path": str(root / "figures"), "exists": (root / "figures").exists(), "note": "standard figures directory"})
    rows.append({"item_type": "directory", "expected_name": "archive", "actual_path": str(root / "archive"), "exists": (root / "archive").exists(), "note": "standard archive directory"})
    rows.append({"item_type": "file", "expected_name": "README_PHASE6_RESULTS.md", "actual_path": str(root / "README_PHASE6_RESULTS.md"), "exists": (root / "README_PHASE6_RESULTS.md").exists(), "note": "root README"})
    rows.append({"item_type": "file", "expected_name": "run_metadata.json", "actual_path": str(root / "run_metadata.json"), "exists": (root / "run_metadata.json").exists(), "note": "run metadata"})
    for csv_name in STANDARD_CSVS:
        csv_path = root / "csv" / csv_name
        rows.append({"item_type": "csv", "expected_name": csv_name, "actual_path": str(csv_path), "exists": csv_path.exists(), "note": "standard csv"})
    for readme_name, csv_name in zip(STANDARD_READMES, STANDARD_CSVS):
        actual_path = root / "csv" / csv_name.replace(".csv", ".md")
        rows.append({"item_type": "csv_readme", "expected_name": readme_name, "actual_path": str(actual_path), "exists": actual_path.exists(), "note": "actual README location in current base folder"})
    return pd.DataFrame(rows)


def _cleanup_candidate_reason(path: Path, source_root: Path) -> tuple[str | None, bool, str]:
    rel = path.relative_to(source_root).as_posix()
    name = path.name
    if name == ".DS_Store":
        return "delete", True, "macOS metadata"
    if name.startswith("edge_data_") and path.suffix == ".xml":
        return "delete", True, "generated edge xml"
    if name.startswith("lane_data_") and path.suffix == ".xml":
        return "delete", True, "generated lane xml"
    if name.startswith("runtime_trace_seed") and path.suffix == ".log":
        return "delete", True, "runtime trace log"
    if name.startswith("sumo_stdout_") and path.suffix == ".log":
        return "delete", True, "SUMO stdout log"
    if name.startswith("sumo_stderr_") and path.suffix == ".log":
        return "delete", True, "SUMO stderr log"
    if name in {"calibration_report.csv", "calibration_summary.md"} and _is_empty_file(path):
        return "delete", True, "empty calibration artifact"
    if path.suffix == ".sumocfg":
        return "review", False, "reproducible sumocfg"
    if path.suffix == ".rou.xml" or name.endswith(".trips.xml"):
        return "review", False, "reproducible route xml"
    if name == "network.net.xml":
        return "review", False, "network file is usually kept unless a duplicate is proven"
    if rel.startswith("readme/") or name in STANDARD_KEEP_NAMES:
        return None, False, "protected standard file"
    if rel.endswith("README_PHASE6_RESULTS.md") or name == "run_metadata.json":
        return None, False, "protected standard file"
    if rel.startswith("csv/") and name in STANDARD_CSVS:
        return None, False, "protected standard csv"
    return None, False, "not a cleanup candidate"


def build_cleanup_candidates(result_root: str | Path) -> pd.DataFrame:
    root = Path(result_root).resolve()
    rows: list[dict[str, Any]] = []
    seen_hashes: dict[str, Path] = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        try:
            action, safe, reason = _cleanup_candidate_reason(path, root)
        except ValueError:
            continue
        if action is None:
            continue
        rel = path.relative_to(root).as_posix()
        size_mb = round(path.stat().st_size / (1024 * 1024), 6)
        keep_reason = ""
        if path.name == "network.net.xml":
            file_hash = _hash_file(path)
            if file_hash in seen_hashes:
                action = "delete"
                safe = True
                reason = f"duplicate network.net.xml; first copy at {seen_hashes[file_hash].relative_to(root).as_posix()}"
            else:
                seen_hashes[file_hash] = path
                action = "review"
                safe = False
                keep_reason = "baseline network copy"
        rows.append(
            {
                "file_path": rel,
                "size_mb": size_mb,
                "file_type": _file_type(path),
                "suggested_action": action.upper(),
                "reason": reason,
                "safe_to_delete": bool(safe),
                "keep_reason_if_any": keep_reason,
            }
        )
    return pd.DataFrame(rows)


def _cleanup_apply(rows: pd.DataFrame, result_root: Path) -> int:
    deleted = 0
    for _, row in rows.iterrows():
        if not bool(row.get("safe_to_delete", False)):
            continue
        path = result_root / str(row["file_path"])
        if path.exists() and path.is_file():
            path.unlink()
            deleted += 1
    return deleted


def _standardize_cli(args: argparse.Namespace) -> None:
    metadata = standardize_phase6_result_folder(args.input_dir, args.output_dir)
    print(json.dumps(metadata, ensure_ascii=False, indent=2))


def _cleanup_cli(args: argparse.Namespace) -> None:
    df = build_cleanup_candidates(args.result_root)
    output_csv = Path(args.output_csv)
    _ensure_dir(output_csv.parent)
    df.to_csv(output_csv, index=False)
    deleted = 0
    if args.apply_cleanup:
        deleted = _cleanup_apply(df, Path(args.result_root).resolve())
    print(json.dumps({"output_csv": str(output_csv), "candidate_rows": int(len(df)), "deleted": deleted, "apply_cleanup": bool(args.apply_cleanup)}, ensure_ascii=False, indent=2))


def _merge_cli(args: argparse.Namespace) -> None:
    metadata = merge_30seed_results(
        root_dir=args.root_dir,
        baseline_pattern=args.baseline_pattern,
        smart_pattern=args.smart_pattern,
        seeds=args.seeds,
        output_dir=args.output_dir,
    )
    print(json.dumps(metadata, ensure_ascii=False, indent=2))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Standardize Phase 6 result folders and generate cleanup dry-run inventories.")
    subparsers = parser.add_subparsers(dest="command")

    std = subparsers.add_parser("standardize", help="standardize a result folder into the Phase 6 layout")
    std.add_argument("--input-dir", required=True)
    std.add_argument("--output-dir", required=True)
    std.set_defaults(func=_standardize_cli)

    merge = subparsers.add_parser("merge-30seed", help="merge 30 seed baseline/smart folders into one standard result folder")
    merge.add_argument("--root-dir", required=True)
    merge.add_argument("--baseline-pattern", required=True)
    merge.add_argument("--smart-pattern", required=True)
    merge.add_argument("--seeds", default="1-30")
    merge.add_argument("--output-dir", required=True)
    merge.set_defaults(func=_merge_cli)

    cleanup = subparsers.add_parser("cleanup", help="scan result/ for cleanup candidates")
    cleanup.add_argument("--result-root", default="result")
    cleanup.add_argument("--output-csv", required=True)
    cleanup.add_argument("--apply-cleanup", action="store_true")
    cleanup.set_defaults(func=_cleanup_cli)

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    if not getattr(args, "command", None):
        parser.print_help()
        return
    args.func(args)


if __name__ == "__main__":
    main()
