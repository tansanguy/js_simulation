from __future__ import annotations

import argparse
import math
import os
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from pandas.errors import EmptyDataError

try:
    from .model_config import get_parameter_value, load_model_parameters
except ImportError:
    from model_config import get_parameter_value, load_model_parameters


PROJECT_DIR = Path(__file__).resolve().parents[1]
DEFAULT_RESULT_DIR = PROJECT_DIR / "result" / "pedestrian_full_validation_2026-05-07"
DEFAULT_OUTPUT_ROOT = PROJECT_DIR / "result"
DEFAULT_MODEL_ASSUMPTIONS = PROJECT_DIR / "smart_crosswalk_sumo" / "config" / "model_assumptions.yaml"
RECOMMENDATION_ORDER = {
    "install_recommended": 0,
    "install_conditional": 1,
    "not_recommended": 2,
    "needs_review": 3,
    "simulation_failed": 4,
}
RECOMMENDATION_COLORS = {
    "install_recommended": "#2a9d8f",
    "install_conditional": "#e9c46a",
    "not_recommended": "#e76f51",
    "needs_review": "#577590",
    "simulation_failed": "#6c757d",
}


def read_csv_safe(path: Path) -> pd.DataFrame:
    if not path.exists() or path.stat().st_size == 0:
        return pd.DataFrame()
    try:
        return pd.read_csv(path)
    except EmptyDataError:
        return pd.DataFrame()


def as_candidate_id(value: Any) -> str | None:
    if pd.isna(value):
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.endswith(".0") and text.replace(".", "", 1).isdigit():
        text = text[:-2]
    return text


def to_numeric(frame: pd.DataFrame, columns: list[str]) -> pd.DataFrame:
    out = frame.copy()
    for column in columns:
        if column in out.columns:
            out[column] = pd.to_numeric(out[column], errors="coerce")
    return out


def first_existing(frame: pd.DataFrame, row_idx: int, names: list[str]) -> Any:
    for name in names:
        if name in frame.columns:
            return frame.iloc[row_idx][name]
    return None


def first_non_null(row: pd.Series, names: list[str]) -> Any:
    for name in names:
        if name in row.index and pd.notna(row[name]):
            return row[name]
    return np.nan


def aggregate_mode_results(mode_dir: Path, mode_name: str) -> pd.DataFrame:
    seed_df = read_csv_safe(mode_dir / "simulation_results_seed.csv")
    avg_df = read_csv_safe(mode_dir / "simulation_results.csv")
    if not seed_df.empty and {"crosswalk_id", "scenario"}.issubset(seed_df.columns):
        frame = seed_df.copy()
    elif not avg_df.empty and {"crosswalk_id", "scenario"}.issubset(avg_df.columns):
        frame = avg_df.copy()
    else:
        return pd.DataFrame()

    frame["candidate_id"] = frame["crosswalk_id"].map(as_candidate_id)
    numeric_cols = [col for col in frame.columns if pd.api.types.is_numeric_dtype(frame[col])]
    grouped = (
        frame.groupby(["candidate_id", "scenario"], dropna=False)[numeric_cols]
        .mean(numeric_only=True)
        .reset_index()
    )
    if "seed" in frame.columns:
        seed_counts = (
            frame.groupby(["candidate_id", "scenario"])["seed"]
            .nunique()
            .reset_index(name="seed_count")
        )
        grouped = grouped.merge(seed_counts, on=["candidate_id", "scenario"], how="left")
    else:
        grouped["seed_count"] = np.nan
    grouped["mode"] = mode_name
    return grouped


def load_candidate_metadata(outputs_dir: Path) -> pd.DataFrame:
    candidates = read_csv_safe(outputs_dir / "candidates.csv")
    preprocessed = read_csv_safe(outputs_dir / "preprocessed_crosswalks.csv")
    if candidates.empty and preprocessed.empty:
        return pd.DataFrame(columns=["candidate_id", "crosswalk_id"])
    if candidates.empty:
        merged = preprocessed.copy()
    else:
        merged = candidates.copy()
        if not preprocessed.empty and "crosswalk_id" in preprocessed.columns:
            fallback = preprocessed.copy()
            fallback["crosswalk_id"] = fallback["crosswalk_id"].map(as_candidate_id)
            merged["crosswalk_id"] = merged["crosswalk_id"].map(as_candidate_id)
            merged = merged.merge(
                fallback,
                on="crosswalk_id",
                how="left",
                suffixes=("", "_fallback"),
            )
            for column in fallback.columns:
                if column == "crosswalk_id":
                    continue
                fallback_col = f"{column}_fallback"
                if fallback_col in merged.columns:
                    if column not in merged.columns:
                        merged[column] = merged[fallback_col]
                    else:
                        merged[column] = merged[column].where(merged[column].notna(), merged[fallback_col])
                    merged = merged.drop(columns=[fallback_col])
    merged["candidate_id"] = merged.get("crosswalk_id", pd.Series(dtype=object)).map(as_candidate_id)
    merged["crosswalk_id"] = merged.get("crosswalk_id", pd.Series(dtype=object)).map(as_candidate_id)
    merged = merged[merged["candidate_id"].notna()].copy()
    numeric_cols = [
        "longitude",
        "latitude",
        "accident_count",
        "accident_count_50m",
        "accident_count_100m",
        "accident_count_200m",
        "elderly_ratio",
        "night_accident_ratio",
        "lane_count",
        "max_speed_kph",
        "crosswalk_length",
        "crossing_length_m",
        "risk_score",
        "estimated_aadt",
    ]
    merged = to_numeric(merged, numeric_cols)
    merged = merged.sort_values(["candidate_id"]).drop_duplicates("candidate_id", keep="first")
    return merged


def load_failed_cases(result_dir: Path) -> pd.DataFrame:
    frames = []
    for path in [
        result_dir / "outputs" / "failed_cases.csv",
        result_dir / "outputs_full_smoke" / "failed_cases.csv",
        result_dir / "outputs_vehicle_only_smoke" / "failed_cases.csv",
    ]:
        frame = read_csv_safe(path)
        if frame.empty:
            continue
        frame = frame.copy()
        frame["source_path"] = str(path.relative_to(result_dir))
        frames.append(frame)
    if not frames:
        return pd.DataFrame(
            columns=["candidate_id", "scenario", "seed", "step", "error", "source_path"]
        )

    failed = pd.concat(frames, ignore_index=True, sort=False).drop_duplicates()
    failed["candidate_id"] = failed.get("crosswalk_id", pd.Series(dtype=object)).map(as_candidate_id)
    failed["scenario"] = failed.get("scenario", pd.Series(dtype=object)).fillna("").astype(str)
    failed["seed"] = failed.get("seed", pd.Series(dtype=object))
    failed["step"] = failed.get("step", pd.Series(dtype=object)).fillna("").astype(str)
    failed["error"] = failed.get("error", pd.Series(dtype=object)).fillna("").astype(str)
    return failed


def classify_error_type(step: str, error_text: str) -> str:
    lower = f"{step} {error_text}".lower()
    if "traffic light" in lower and "not known" in lower:
        return "tls_error"
    if "osm" in lower and ("timeout" in lower or "httpsconnectionpool" in lower):
        return "osm_download_timeout"
    if "metadata.json" in lower or "load_metadata" in lower:
        return "missing_metadata"
    if "path_missing_crossing_edge" in lower or "pedestrian" in lower and "path_missing" in lower:
        return "pedestrian_connectivity_error"
    if "build_network" in lower:
        return "build_network_error"
    if "run_simulation" in lower:
        return "run_simulation_error"
    return "unknown"


def parse_root_cause(failures: pd.DataFrame, candidate_id: str, scenario: str = "") -> dict[str, Any]:
    rows = failures[failures["candidate_id"] == candidate_id].copy()
    if scenario:
        scoped = rows[rows["scenario"].isin([scenario, ""])]
        if not scoped.empty:
            rows = scoped
    if rows.empty:
        return {
            "failure_stage": "",
            "primary_error_type": "",
            "root_cause": "",
            "downstream_errors": "",
        }

    rows["error_type"] = rows.apply(
        lambda item: classify_error_type(str(item.get("step", "")), str(item.get("error", ""))),
        axis=1,
    )
    first = rows.iloc[0]
    downstream = rows["error_type"].dropna().astype(str).tolist()[1:]
    return {
        "failure_stage": str(first.get("step", "")),
        "primary_error_type": str(first.get("error_type", "")),
        "root_cause": str(first.get("error", ""))[:240],
        "downstream_errors": "; ".join(dict.fromkeys(downstream)),
    }


def build_mode_lookup(frame: pd.DataFrame) -> dict[tuple[str, str], pd.Series]:
    if frame.empty:
        return {}
    return {
        (str(row["candidate_id"]), str(row["scenario"])): row
        for _, row in frame.iterrows()
        if pd.notna(row.get("candidate_id")) and pd.notna(row.get("scenario"))
    }


def get_metric(row: pd.Series | None, names: list[str]) -> float | None:
    if row is None:
        return None
    value = first_non_null(row, names)
    if pd.isna(value):
        return None
    return float(value)


def get_text_metric(row: pd.Series | None, names: list[str]) -> str:
    if row is None:
        return ""
    for name in names:
        if name in row.index and pd.notna(row[name]):
            return str(row[name])
    return ""


def compute_delay_cost(
    baseline_row: pd.Series | None,
    smart_row: pd.Series | None,
    delay_cost_per_hour: float,
) -> tuple[float | None, str]:
    baseline_cost = get_metric(baseline_row, ["vehicle_delay_cost"])
    smart_cost = get_metric(smart_row, ["vehicle_delay_cost"])
    if baseline_cost is not None and smart_cost is not None:
        return max(smart_cost - baseline_cost, 0.0), "observed_vehicle_delay_cost_delta"

    baseline_delay = get_metric(baseline_row, ["avg_vehicle_delay_sec", "veh_avg_delay_sec"])
    smart_delay = get_metric(smart_row, ["avg_vehicle_delay_sec", "veh_avg_delay_sec"])
    if baseline_delay is None or smart_delay is None:
        return None, ""
    return max(smart_delay - baseline_delay, 0.0) * delay_cost_per_hour / 3600.0, "delay_delta_x_value_of_time"


def summarize_status_rows(
    candidate_ids: list[str],
    full_results: pd.DataFrame,
    vehicle_results: pd.DataFrame,
    smoke_results: pd.DataFrame,
    failures: pd.DataFrame,
) -> pd.DataFrame:
    smoke_lookup: dict[tuple[str, str], pd.Series] = {}
    if not smoke_results.empty:
        smoke = smoke_results.copy()
        smoke["candidate_id"] = smoke.get("crosswalk_id", pd.Series(dtype=object)).map(as_candidate_id)
        smoke["scenario"] = smoke.get("scenario", pd.Series(dtype=object)).fillna("").astype(str)
        smoke_lookup = {
            (str(row["candidate_id"]), str(row["scenario"])): row for _, row in smoke.iterrows()
        }

    rows: list[dict[str, Any]] = []
    for mode_name, frame in [("full", full_results), ("vehicle_only", vehicle_results)]:
        lookup = build_mode_lookup(frame)
        for candidate_id in candidate_ids:
            for scenario in ["baseline", "smart"]:
                agg_row = lookup.get((candidate_id, scenario))
                smoke_row = smoke_lookup.get((candidate_id, scenario))
                failure = parse_root_cause(failures, candidate_id, scenario)
                status = "not_run"
                sumo_loaded = False
                metrics_available = False
                ped_metrics = False
                veh_metrics = False
                if agg_row is not None:
                    veh_metrics = get_metric(agg_row, ["avg_vehicle_delay_sec", "veh_avg_delay_sec"]) is not None
                    ped_metrics = get_metric(
                        agg_row,
                        [
                            "safety_risk_score",
                            "pedestrian_waiting_time_mean",
                            "average_pedestrian_wait_time",
                            "PET_B_surrogate_severe",
                        ],
                    ) is not None
                    metrics_available = ped_metrics or veh_metrics
                    status = "success" if metrics_available else "partial_metrics"
                    sumo_loaded = True
                elif smoke_row is not None and str(smoke_row.get("run_status", "")) == "failed":
                    status = "failed"
                elif not failure["primary_error_type"]:
                    status = "not_run"
                else:
                    status = "failed"
                if mode_name == "vehicle_only":
                    ped_metrics = False
                rows.append(
                    {
                        "candidate_id": candidate_id,
                        "scenario": scenario,
                        "mode": mode_name,
                        "status": status,
                        "failure_stage": failure["failure_stage"],
                        "primary_error_type": failure["primary_error_type"],
                        "root_cause": failure["root_cause"],
                        "downstream_errors": failure["downstream_errors"],
                        "sumo_loaded": bool(sumo_loaded),
                        "metrics_available": bool(metrics_available),
                        "pedestrian_metrics_available": bool(ped_metrics),
                        "vehicle_metrics_available": bool(veh_metrics),
                    }
                )
    return pd.DataFrame(rows)


def build_summary_tables(
    metadata: pd.DataFrame,
    full_results: pd.DataFrame,
    vehicle_results: pd.DataFrame,
    simulation_status: pd.DataFrame,
    failures: pd.DataFrame,
    delay_cost_per_hour: float,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    full_lookup = build_mode_lookup(full_results)
    vehicle_lookup = build_mode_lookup(vehicle_results)
    status_lookup = {
        (str(row["candidate_id"]), str(row["scenario"]), str(row["mode"])): row
        for _, row in simulation_status.iterrows()
    }

    summary_rows: list[dict[str, Any]] = []
    traffic_rows: list[dict[str, Any]] = []
    safety_rows: list[dict[str, Any]] = []

    candidate_ids = metadata["candidate_id"].dropna().astype(str).tolist()
    comparable_delay_costs: list[float] = []

    interim_rows: list[dict[str, Any]] = []
    for candidate_id in candidate_ids:
        meta = metadata[metadata["candidate_id"] == candidate_id].iloc[0]
        full_base = full_lookup.get((candidate_id, "baseline"))
        full_smart = full_lookup.get((candidate_id, "smart"))
        vehicle_base = vehicle_lookup.get((candidate_id, "baseline"))
        vehicle_smart = vehicle_lookup.get((candidate_id, "smart"))

        full_pair = full_base is not None and full_smart is not None
        vehicle_pair = vehicle_base is not None and vehicle_smart is not None
        traffic_source = "full_simulation_observed" if full_pair else "vehicle_only_observed" if vehicle_pair else ""
        traffic_base = full_base if full_pair else vehicle_base
        traffic_smart = full_smart if full_pair else vehicle_smart

        baseline_vehicle_delay_sec = get_metric(traffic_base, ["avg_vehicle_delay_sec", "veh_avg_delay_sec"])
        smart_vehicle_delay_sec = get_metric(traffic_smart, ["avg_vehicle_delay_sec", "veh_avg_delay_sec"])
        vehicle_delay_delta_sec = (
            smart_vehicle_delay_sec - baseline_vehicle_delay_sec
            if baseline_vehicle_delay_sec is not None and smart_vehicle_delay_sec is not None
            else None
        )
        vehicle_delay_cost, cost_source = compute_delay_cost(traffic_base, traffic_smart, delay_cost_per_hour)

        baseline_safety = get_metric(full_base, ["safety_risk_score"])
        smart_safety = get_metric(full_smart, ["safety_risk_score"])
        safety_reduction = (
            baseline_safety - smart_safety
            if baseline_safety is not None and smart_safety is not None
            else None
        )
        safety_benefit_score = safety_reduction
        net_benefit_score = (
            safety_benefit_score - vehicle_delay_cost
            if safety_benefit_score is not None and vehicle_delay_cost is not None
            else None
        )
        benefit_cost_ratio = (
            safety_benefit_score / vehicle_delay_cost
            if safety_benefit_score is not None
            and vehicle_delay_cost is not None
            and vehicle_delay_cost > 0
            else None
        )

        if full_pair and safety_benefit_score is not None and vehicle_delay_cost is not None:
            comparable_delay_costs.append(vehicle_delay_cost)

        if full_pair and safety_benefit_score is not None:
            scenario_status = "full_simulation_success"
        elif full_pair:
            scenario_status = "full_metrics_partial"
        elif vehicle_pair:
            scenario_status = "vehicle_only_only"
        else:
            scenario_status = "full_simulation_failed"

        failure = parse_root_cause(failures, candidate_id)
        notes = []
        if scenario_status == "vehicle_only_only":
            notes.append("full_simulation_not_comparable_vehicle_only_used_for_traffic")
        if scenario_status == "full_metrics_partial":
            notes.append("full_simulation_completed_but_pedestrian_metrics_missing")
        if scenario_status == "full_simulation_failed" and failure["primary_error_type"]:
            notes.append(f"failure={failure['primary_error_type']}")
        if cost_source == "delay_delta_x_value_of_time":
            notes.append("vehicle_delay_cost_fallback_used")

        baseline_queue = get_metric(traffic_base, ["avg_queue_length", "queue_avg"])
        smart_queue = get_metric(traffic_smart, ["avg_queue_length", "queue_avg"])
        baseline_throughput = get_metric(traffic_base, ["throughput", "network_throughput_veh_per_hour"])
        smart_throughput = get_metric(traffic_smart, ["throughput", "network_throughput_veh_per_hour"])
        traffic_rows.append(
            {
                "candidate_id": candidate_id,
                "baseline_vehicle_delay_sec": baseline_vehicle_delay_sec,
                "smart_vehicle_delay_sec": smart_vehicle_delay_sec,
                "vehicle_delay_delta_sec": vehicle_delay_delta_sec,
                "baseline_queue_length": baseline_queue,
                "smart_queue_length": smart_queue,
                "queue_length_delta": smart_queue - baseline_queue
                if baseline_queue is not None and smart_queue is not None
                else None,
                "baseline_throughput": baseline_throughput,
                "smart_throughput": smart_throughput,
                "throughput_delta": smart_throughput - baseline_throughput
                if baseline_throughput is not None and smart_throughput is not None
                else None,
                "lane_count": pd.to_numeric(meta.get("lane_count"), errors="coerce"),
                "vehicle_delay_cost": vehicle_delay_cost,
                "cost_source": cost_source or traffic_source,
                "notes": "; ".join(notes),
            }
        )

        baseline_pet = get_metric(full_base, ["PET_B_surrogate_severe"])
        smart_pet = get_metric(full_smart, ["PET_B_surrogate_severe"])
        baseline_wait = get_metric(full_base, ["pedestrian_waiting_time_mean", "average_pedestrian_wait_time"])
        smart_wait = get_metric(full_smart, ["pedestrian_waiting_time_mean", "average_pedestrian_wait_time"])
        baseline_elderly_incomplete = get_metric(full_base, ["elderly_incomplete_crossings", "elderly_incomplete_cross"])
        smart_elderly_incomplete = get_metric(full_smart, ["elderly_incomplete_crossings", "elderly_incomplete_cross"])
        safety_rows.append(
            {
                "candidate_id": candidate_id,
                "accident_count": pd.to_numeric(meta.get("accident_count"), errors="coerce"),
                "accident_count_50m": pd.to_numeric(meta.get("accident_count_50m"), errors="coerce"),
                "accident_count_100m": pd.to_numeric(meta.get("accident_count_100m"), errors="coerce"),
                "accident_count_200m": pd.to_numeric(meta.get("accident_count_200m"), errors="coerce"),
                "elderly_ratio": pd.to_numeric(meta.get("elderly_ratio"), errors="coerce"),
                "night_accident_ratio": pd.to_numeric(meta.get("night_accident_ratio"), errors="coerce"),
                "lanes": pd.to_numeric(meta.get("lane_count"), errors="coerce"),
                "max_spd": pd.to_numeric(meta.get("max_speed_kph"), errors="coerce"),
                "crosswalk_length": pd.to_numeric(
                    meta.get("crosswalk_length") if pd.notna(meta.get("crosswalk_length")) else meta.get("crossing_length_m"),
                    errors="coerce",
                ),
                "baseline_safety_risk_score": baseline_safety,
                "smart_safety_risk_score": smart_safety,
                "safety_risk_reduction": safety_reduction,
                "elderly_incomplete_cross_delta": smart_elderly_incomplete - baseline_elderly_incomplete
                if baseline_elderly_incomplete is not None and smart_elderly_incomplete is not None
                else None,
                "pedestrian_waiting_time_delta": smart_wait - baseline_wait
                if baseline_wait is not None and smart_wait is not None
                else None,
                "conflict_surrogate_delta": smart_pet - baseline_pet
                if baseline_pet is not None and smart_pet is not None
                else None,
                "safety_benefit_score": safety_benefit_score,
                "benefit_source": "full_simulation_observed" if safety_benefit_score is not None else None,
                "notes": "; ".join(notes) if safety_benefit_score is None else "",
            }
        )

        interim_rows.append(
            {
                "candidate_id": candidate_id,
                "crosswalk_id": candidate_id,
                "lon": pd.to_numeric(meta.get("longitude"), errors="coerce"),
                "lat": pd.to_numeric(meta.get("latitude"), errors="coerce"),
                "scenario_status": scenario_status,
                "baseline_vehicle_delay_sec": baseline_vehicle_delay_sec,
                "smart_vehicle_delay_sec": smart_vehicle_delay_sec,
                "vehicle_delay_delta_sec": vehicle_delay_delta_sec,
                "vehicle_delay_cost": vehicle_delay_cost,
                "baseline_safety_risk_score": baseline_safety,
                "smart_safety_risk_score": smart_safety,
                "safety_risk_reduction": safety_reduction,
                "safety_benefit_score": safety_benefit_score,
                "net_benefit_score": net_benefit_score,
                "benefit_cost_ratio": benefit_cost_ratio,
                "failure_type_internal": failure["primary_error_type"],
                "notes_internal": "; ".join(notes),
            }
        )

    positive_delay_costs = [value for value in comparable_delay_costs if value is not None and value > 0]
    delay_threshold = float(np.median(positive_delay_costs)) if positive_delay_costs else 0.0

    summary_frame = pd.DataFrame(interim_rows)
    if summary_frame.empty:
        return summary_frame, pd.DataFrame(traffic_rows), pd.DataFrame(safety_rows)

    recommendations = []
    reasons = []
    for row in summary_frame.itertuples(index=False):
        if row.scenario_status == "full_simulation_failed":
            recommendation = "simulation_failed"
            reason = (
                f"simulation_failed_{row.failure_type_internal}" if row.failure_type_internal else "simulation_failed"
            )
        elif row.safety_benefit_score is None or pd.isna(row.safety_benefit_score):
            recommendation = "needs_review"
            if row.scenario_status == "vehicle_only_only":
                reason = "vehicle_only_result_available"
            else:
                reason = "pedestrian_metrics_missing"
        elif row.safety_benefit_score <= 0 or row.net_benefit_score is None or row.net_benefit_score <= 0:
            recommendation = "not_recommended"
            reason = "low_safety_benefit"
        elif row.vehicle_delay_cost is not None and row.vehicle_delay_cost <= delay_threshold:
            recommendation = "install_recommended"
            reason = "high_safety_benefit_low_delay_cost"
        else:
            recommendation = "install_conditional"
            reason = "safety_benefit_positive_but_delay_cost_high"
        recommendations.append(recommendation)
        reasons.append(reason)

    summary_frame["recommendation"] = recommendations
    summary_frame["primary_reason"] = reasons

    comparable = summary_frame[summary_frame["net_benefit_score"].notna()].copy()
    if not comparable.empty:
        comparable = comparable.sort_values(
            by=["net_benefit_score", "safety_benefit_score", "vehicle_delay_cost", "candidate_id"],
            ascending=[False, False, True, True],
            na_position="last",
        )
        comparable["rank"] = np.arange(1, len(comparable) + 1)
        summary_frame = summary_frame.merge(
            comparable[["candidate_id", "rank"]],
            on="candidate_id",
            how="left",
        )
    else:
        summary_frame["rank"] = np.nan

    summary_frame = summary_frame[
        [
            "candidate_id",
            "crosswalk_id",
            "lon",
            "lat",
            "scenario_status",
            "baseline_vehicle_delay_sec",
            "smart_vehicle_delay_sec",
            "vehicle_delay_delta_sec",
            "vehicle_delay_cost",
            "baseline_safety_risk_score",
            "smart_safety_risk_score",
            "safety_risk_reduction",
            "safety_benefit_score",
            "net_benefit_score",
            "benefit_cost_ratio",
            "recommendation",
            "rank",
            "primary_reason",
        ]
    ].sort_values(
        by=["rank", "recommendation", "candidate_id"],
        key=lambda col: col.map(RECOMMENDATION_ORDER) if col.name == "recommendation" else col,
        na_position="last",
    )
    return summary_frame, pd.DataFrame(traffic_rows), pd.DataFrame(safety_rows)


def build_assumptions_sheet(params: dict[str, dict[str, Any]]) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "value_of_time_per_vehicle_hour": get_parameter_value(params, "delay_cost_per_hour", 15000.0),
                "vehicle_delay_cost_formula": "max(vehicle_delay_cost_smart - vehicle_delay_cost_baseline, 0); fallback=max(vehicle_delay_delta_sec, 0)*value_of_time_per_vehicle_hour/3600",
                "safety_benefit_formula": "baseline_safety_risk_score - smart_safety_risk_score",
                "risk_score_formula": "accident_count*0.5 + elderly_ratio*10 + lane_count*0.3 + (max_speed_kph/50)*0.2",
                "accident_weight": 0.5,
                "elderly_ratio_weight": 10.0,
                "night_risk_weight": np.nan,
                "lane_weight": 0.3,
                "max_speed_weight": 0.2,
                "crosswalk_length_weight": np.nan,
                "notes": "Current package uses observed full-simulation safety_risk_score when available. Static candidate risk_score is context only, not a fallback decision metric.",
            }
        ]
    )


def build_raw_index(result_dir: Path) -> pd.DataFrame:
    rows = [
        {
            "source_file": "candidates.csv",
            "source_folder": "outputs",
            "used_in_summary": True,
            "mapped_sheet": "summary,safety_benefit,raw_index",
            "description": "candidate metadata and static context fields",
        },
        {
            "source_file": "preprocessed_crosswalks.csv",
            "source_folder": "outputs",
            "used_in_summary": True,
            "mapped_sheet": "summary,safety_benefit",
            "description": "fallback candidate metadata when fields are missing in candidates.csv",
        },
        {
            "source_file": "full_simulation_smoke_results.csv",
            "source_folder": "outputs",
            "used_in_summary": True,
            "mapped_sheet": "simulation_status",
            "description": "full simulation run status and required metric availability",
        },
        {
            "source_file": "full_vs_vehicle_only_comparison.csv",
            "source_folder": "outputs",
            "used_in_summary": True,
            "mapped_sheet": "simulation_status,raw_index",
            "description": "diagnostic comparison between full and vehicle-only smoke runs",
        },
        {
            "source_file": "failed_cases.csv",
            "source_folder": "outputs",
            "used_in_summary": True,
            "mapped_sheet": "simulation_status,summary",
            "description": "merged failure log for root cause classification",
        },
        {
            "source_file": "simulation_results_seed.csv",
            "source_folder": "outputs_full_smoke",
            "used_in_summary": True,
            "mapped_sheet": "summary,traffic_cost,safety_benefit,simulation_status",
            "description": "observed full simulation baseline and smart metrics",
        },
        {
            "source_file": "simulation_results_seed.csv",
            "source_folder": "outputs_vehicle_only_smoke",
            "used_in_summary": True,
            "mapped_sheet": "summary,traffic_cost,simulation_status",
            "description": "observed vehicle-only fallback traffic metrics",
        },
        {
            "source_file": "model_assumptions.yaml",
            "source_folder": "smart_crosswalk_sumo/config",
            "used_in_summary": True,
            "mapped_sheet": "assumptions",
            "description": "assumption-backed value-of-time and model parameter notes",
        },
    ]
    return pd.DataFrame(rows)


def ensure_matplotlib(output_dir: Path) -> None:
    cache_dir = Path("/private/tmp/codex_mpl_cache")
    cache_dir.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("MPLCONFIGDIR", str(cache_dir))


def save_placeholder(path: Path, title: str, body: str) -> None:
    ensure_matplotlib(path.parent.parent if path.parent.name == "figures" else path.parent)
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(8, 4.5))
    ax.text(0.5, 0.65, title, ha="center", va="center", fontsize=16, fontweight="bold")
    ax.text(0.5, 0.45, body, ha="center", va="center", fontsize=11, wrap=True)
    ax.set_axis_off()
    plt.tight_layout()
    plt.savefig(path, dpi=180, bbox_inches="tight")
    plt.close(fig)


def write_figures(summary: pd.DataFrame, output_dir: Path) -> None:
    figures_dir = output_dir / "figures"
    figures_dir.mkdir(parents=True, exist_ok=True)
    ensure_matplotlib(output_dir)
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    total_delay_cost = float(summary["vehicle_delay_cost"].fillna(0).sum()) if not summary.empty else 0.0
    total_safety_benefit = float(summary["safety_benefit_score"].fillna(0).sum()) if not summary.empty else 0.0
    avg_net_benefit = (
        float(summary["net_benefit_score"].dropna().mean()) if "net_benefit_score" in summary.columns and not summary["net_benefit_score"].dropna().empty else float("nan")
    )
    success_count = int((summary["scenario_status"] == "full_simulation_success").sum()) if not summary.empty else 0
    failed_count = int((summary["recommendation"] == "simulation_failed").sum()) if not summary.empty else 0

    fig, axes = plt.subplots(2, 2, figsize=(12, 8))
    metrics = [
        ("total_vehicle_delay_cost", total_delay_cost, "#577590"),
        ("total_safety_benefit_score", total_safety_benefit, "#2a9d8f"),
        ("avg_net_benefit_score", avg_net_benefit if not math.isnan(avg_net_benefit) else 0.0, "#e9c46a"),
        ("success_vs_failed", success_count - failed_count, "#e76f51"),
    ]
    for ax, (label, value, color) in zip(axes.flat, metrics):
        ax.bar([label], [value], color=color)
        ax.set_title(label)
        ax.tick_params(axis="x", rotation=15)
        ax.text(0, value, f"{value:.2f}" if isinstance(value, float) else str(value), ha="center", va="bottom")
    plt.tight_layout()
    plt.savefig(figures_dir / "cost_benefit_overview.png", dpi=180)
    plt.close(fig)

    comparable = summary[summary["net_benefit_score"].notna()].copy()
    if comparable.empty:
        save_placeholder(
            figures_dir / "candidate_ranking.png",
            "candidate_ranking",
            "No comparable candidates with both traffic cost and safety benefit.",
        )
    else:
        ranking = comparable.sort_values("net_benefit_score", ascending=False).head(10)
        colors = [RECOMMENDATION_COLORS.get(value, "#577590") for value in ranking["recommendation"]]
        fig, ax = plt.subplots(figsize=(10, 6))
        ax.barh(ranking["candidate_id"], ranking["net_benefit_score"], color=colors)
        ax.invert_yaxis()
        ax.set_xlabel("net_benefit_score")
        ax.set_ylabel("candidate_id")
        ax.set_title("Top Candidates by Net Benefit")
        plt.tight_layout()
        plt.savefig(figures_dir / "candidate_ranking.png", dpi=180)
        plt.close(fig)

    scatter = summary[
        summary["vehicle_delay_cost"].notna() & summary["safety_benefit_score"].notna()
    ].copy()
    if scatter.empty:
        save_placeholder(
            figures_dir / "traffic_cost_vs_safety_benefit.png",
            "traffic_cost_vs_safety_benefit",
            "No candidates have both traffic cost and safety benefit.",
        )
    else:
        fig, ax = plt.subplots(figsize=(8, 6))
        for recommendation, group in scatter.groupby("recommendation", dropna=False):
            ax.scatter(
                group["vehicle_delay_cost"],
                group["safety_benefit_score"],
                label=str(recommendation),
                color=RECOMMENDATION_COLORS.get(str(recommendation), "#577590"),
                s=60,
                alpha=0.85,
            )
        for row in scatter.itertuples(index=False):
            ax.annotate(str(row.candidate_id), (row.vehicle_delay_cost, row.safety_benefit_score), fontsize=8)
        ax.axhline(0, color="#adb5bd", linewidth=1)
        ax.axvline(0, color="#adb5bd", linewidth=1)
        ax.set_xlabel("vehicle_delay_cost")
        ax.set_ylabel("safety_benefit_score")
        ax.set_title("Traffic Cost vs Safety Benefit")
        ax.legend()
        plt.tight_layout()
        plt.savefig(figures_dir / "traffic_cost_vs_safety_benefit.png", dpi=180)
        plt.close(fig)

    map_df = summary[summary["lon"].notna() & summary["lat"].notna()].copy()
    if map_df.empty:
        save_placeholder(
            figures_dir / "candidate_location_map.png",
            "candidate_location_map",
            "Candidate coordinates are not available.",
        )
    else:
        fig, ax = plt.subplots(figsize=(8, 8))
        for recommendation, group in map_df.groupby("recommendation", dropna=False):
            ax.scatter(
                group["lon"],
                group["lat"],
                label=str(recommendation),
                color=RECOMMENDATION_COLORS.get(str(recommendation), "#577590"),
                s=65,
                alpha=0.9,
            )
        for row in map_df.itertuples(index=False):
            ax.annotate(str(row.candidate_id), (row.lon, row.lat), fontsize=8)
        ax.set_xlabel("lon")
        ax.set_ylabel("lat")
        ax.set_title("Candidate Locations")
        ax.legend()
        plt.tight_layout()
        plt.savefig(figures_dir / "candidate_location_map.png", dpi=180)
        plt.close(fig)


def format_top_candidates(summary: pd.DataFrame) -> str:
    top = summary[summary["rank"].notna()].sort_values("rank").head(5)
    if top.empty:
        return "- comparable top candidates are not available"
    lines = []
    for row in top.itertuples(index=False):
        lines.append(
            f"- {row.candidate_id}: rank={int(row.rank)}, recommendation={row.recommendation}, reason={row.primary_reason}, net_benefit_score={row.net_benefit_score:.3f}"
        )
    return "\n".join(lines)


def build_report(summary: pd.DataFrame, simulation_status: pd.DataFrame, output_dir: Path) -> None:
    traffic_count = int(summary["vehicle_delay_cost"].notna().sum()) if not summary.empty else 0
    safety_count = int(summary["safety_benefit_score"].notna().sum()) if not summary.empty else 0
    failed_safety_count = int(
        summary[
            summary["safety_benefit_score"].isna()
            & summary["scenario_status"].isin(["vehicle_only_only", "full_simulation_failed", "full_metrics_partial"])
        ].shape[0]
    ) if not summary.empty else 0
    vehicle_only_count = int((summary["scenario_status"] == "vehicle_only_only").sum()) if not summary.empty else 0
    recommended_count = int((summary["recommendation"] == "install_recommended").sum()) if not summary.empty else 0
    report_ready = (
        recommended_count > 0
        and failed_safety_count == 0
        and int((summary["scenario_status"] == "full_simulation_success").sum()) == len(summary)
    )

    blocker_lines = []
    if failed_safety_count > 0:
        blocker_lines.append(
            f"- {failed_safety_count} candidates do not have comparable full-simulation safety benefit."
        )
    if int((summary["recommendation"] == "simulation_failed").sum()) > 0:
        blocker_lines.append("- Some candidates failed before comparable simulation outputs were produced.")
    if safety_count < len(summary):
        blocker_lines.append("- Vehicle-only fallback cases should not be treated as final install decisions.")
    if not blocker_lines:
        blocker_lines.append("- No major blockers detected in the current package.")

    lines = [
        "# Final Cost Benefit Report",
        "",
        "## 1. How many candidates have calculable traffic cost?",
        f"- {traffic_count} candidates have non-null `vehicle_delay_cost`.",
        "",
        "## 2. How many candidates have calculable safety benefit?",
        f"- {safety_count} candidates have non-null `safety_benefit_score`.",
        "",
        "## 3. How many candidates cannot compute safety benefit because full simulation failed?",
        f"- {failed_safety_count} candidates are missing comparable full-simulation safety benefit.",
        "",
        "## 4. How were vehicle-only-only results labeled?",
        f"- {vehicle_only_count} candidates were labeled `needs_review` with `primary_reason=vehicle_only_result_available`.",
        "",
        "## 5. How many final recommended candidates are there?",
        f"- {recommended_count} candidates are `install_recommended`.",
        "",
        "## 6. What are the top 5 candidates and why?",
        format_top_candidates(summary),
        "",
        "## 7. Can this be used in a research report immediately?",
        (
            "- Yes. All candidates have comparable full-simulation traffic and safety results."
            if report_ready
            else "- Not yet as a final result table. It is suitable as a smoke/validation decision package, but missing or failed full-simulation candidates must be disclosed."
        ),
        "",
        "## 8. What blockers remain?",
        *blocker_lines,
        "",
        "## Recommendation Legend",
        "- `install_recommended`: positive safety benefit and positive net benefit with relatively low delay cost.",
        "- `install_conditional`: positive safety benefit and positive net benefit, but delay cost is relatively high.",
        "- `not_recommended`: non-positive safety benefit or non-positive net benefit.",
        "- `needs_review`: traffic metrics exist, but comparable full-simulation safety benefit is missing.",
        "- `simulation_failed`: no usable comparison metrics were produced.",
    ]
    (output_dir / "final_cost_benefit_report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_excel(
    output_dir: Path,
    summary: pd.DataFrame,
    traffic_cost: pd.DataFrame,
    safety_benefit: pd.DataFrame,
    simulation_status: pd.DataFrame,
    assumptions: pd.DataFrame,
    raw_index: pd.DataFrame,
) -> None:
    excel_path = output_dir / "final_results_summary.xlsx"
    with pd.ExcelWriter(excel_path, engine="openpyxl") as writer:
        summary.to_excel(writer, sheet_name="summary", index=False)
        traffic_cost.to_excel(writer, sheet_name="traffic_cost", index=False)
        safety_benefit.to_excel(writer, sheet_name="safety_benefit", index=False)
        simulation_status.to_excel(writer, sheet_name="simulation_status", index=False)
        assumptions.to_excel(writer, sheet_name="assumptions", index=False)
        raw_index.to_excel(writer, sheet_name="raw_index", index=False)


def generate_final_decision_summary(
    result_dir: str | Path = DEFAULT_RESULT_DIR,
    output_dir: str | Path | None = None,
    model_assumptions_path: str | Path = DEFAULT_MODEL_ASSUMPTIONS,
) -> Path:
    result_dir = Path(result_dir)
    outputs_dir = result_dir / "outputs"
    output_path = (
        Path(output_dir)
        if output_dir is not None
        else DEFAULT_OUTPUT_ROOT / f"final_decision_summary_{datetime.now().strftime('%Y%m%d')}"
    )
    output_path.mkdir(parents=True, exist_ok=True)

    metadata = load_candidate_metadata(outputs_dir)
    params = load_model_parameters(model_assumptions_path)
    delay_cost_per_hour = float(get_parameter_value(params, "delay_cost_per_hour", 15000.0))
    full_results = aggregate_mode_results(result_dir / "outputs_full_smoke", "full")
    vehicle_results = aggregate_mode_results(result_dir / "outputs_vehicle_only_smoke", "vehicle_only")
    smoke_results = read_csv_safe(outputs_dir / "full_simulation_smoke_results.csv")
    failures = load_failed_cases(result_dir)

    extra_ids = set()
    for frame in [full_results, vehicle_results, smoke_results, failures]:
        if not frame.empty and "candidate_id" in frame.columns:
            extra_ids.update(frame["candidate_id"].dropna().astype(str).tolist())
        elif not frame.empty and "crosswalk_id" in frame.columns:
            extra_ids.update(frame["crosswalk_id"].map(as_candidate_id).dropna().astype(str).tolist())
    if extra_ids:
        known_ids = set(metadata["candidate_id"].dropna().astype(str))
        missing_ids = sorted(extra_ids - known_ids)
        if missing_ids:
            metadata = pd.concat(
                [
                    metadata,
                    pd.DataFrame(
                        [{"candidate_id": item, "crosswalk_id": item} for item in missing_ids]
                    ),
                ],
                ignore_index=True,
                sort=False,
            )
            metadata = metadata.sort_values("candidate_id").drop_duplicates("candidate_id", keep="first")

    simulation_status = summarize_status_rows(
        metadata["candidate_id"].dropna().astype(str).tolist(),
        full_results,
        vehicle_results,
        smoke_results,
        failures,
    )
    summary, traffic_cost, safety_benefit = build_summary_tables(
        metadata,
        full_results,
        vehicle_results,
        simulation_status,
        failures,
        delay_cost_per_hour,
    )
    assumptions = build_assumptions_sheet(params)
    raw_index = build_raw_index(result_dir)

    summary.to_csv(output_path / "final_cost_benefit_summary.csv", index=False)
    write_excel(output_path, summary, traffic_cost, safety_benefit, simulation_status, assumptions, raw_index)
    build_report(summary, simulation_status, output_path)
    write_figures(summary, output_path)
    return output_path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build final cost-benefit decision package.")
    parser.add_argument("--result_dir", default=str(DEFAULT_RESULT_DIR))
    parser.add_argument("--output_dir", default=None)
    parser.add_argument("--model_assumptions", default=str(DEFAULT_MODEL_ASSUMPTIONS))
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    output_path = generate_final_decision_summary(
        result_dir=args.result_dir,
        output_dir=args.output_dir,
        model_assumptions_path=args.model_assumptions,
    )
    print(output_path)


if __name__ == "__main__":
    main()
