from __future__ import annotations

import argparse
import json
import math
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from pandas.errors import EmptyDataError

try:
    from .mpl_runtime import configure_matplotlib, ensure_matplotlib_env
    from .model_config import load_model_parameters
    from .csv_outputs import ensure_csv_output_layout, write_csv_bundle
    from .output_schema import write_csv_utf8_sig
except ImportError:
    from mpl_runtime import configure_matplotlib, ensure_matplotlib_env
    from model_config import load_model_parameters
    from csv_outputs import ensure_csv_output_layout, write_csv_bundle
    from output_schema import write_csv_utf8_sig


SIMULATION_SUMMARY_COLUMNS = [
    "crosswalk_id",
    "scenario",
    "seed_count",
    "safety_risk_score",
    "accident_expected_value",
    "elderly_incomplete_crossings",
    "pedestrian_clearance_failure_count",
    "unfinished_crossing_count",
    "avg_vehicle_delay_sec",
    "avg_queue_length",
    "max_queue_length",
    "surrounding_road_delay_sec",
    "vehicle_delay_cost",
    "extension_count",
    "total_extension_sec",
]

BASELINE_SMART_COLUMNS = [
    "crosswalk_id",
    "safety_risk_delta",
    "accident_expected_delta",
    "elderly_incomplete_crossings_delta",
    "pedestrian_clearance_failure_delta",
    "unfinished_crossing_delta",
    "avg_vehicle_delay_delta_sec",
    "avg_queue_length_delta",
    "max_queue_length_delta",
    "surrounding_road_delay_delta_sec",
    "vehicle_delay_cost_delta",
    "extension_count_smart",
    "total_extension_sec_smart",
    "traffic_metric_scope",
    "metrics_exact",
]

REPORT_COLUMNS = [
    "candidate_id",
    "seed_count",
    "scenario_name",
    "simulation_mode",
    "experiment_kind",
    "traffic_metric_scope",
    "generated_vehicle_count",
    "generated_pedestrian_count",
    "demand_vehicle_validation_status",
    "demand_pedestrian_validation_status",
    "safety_improvement_pct",
    "traffic_degradation_pct",
    "tradeoff_ratio",
    "throughput",
    "network_arrived_vehicles",
    "vehicle_mean_time_loss",
    "vehicle_mean_travel_time",
    "vehicle_mean_speed",
    "pedestrian_waiting_time_mean",
    "pedestrian_waiting_time_p95",
    "result_status",
    "result_reason",
]

REPORT_FORBIDDEN_COLUMNS = {
    "metrics_exact",
    "experiment_mode",
    "toy_queue_vehicle_arrivals",
    "vehicle_count",
    "arrived_count",
    "total_vehicle_arrivals",
    "vehicle_arrivals",
}

NA_TEXT = "not_applicable"


def df_to_markdown(df: pd.DataFrame) -> str:
    try:
        return df.to_markdown(index=False)
    except ImportError:
        return "```text\n" + df.to_csv(index=False) + "```"


def mean_or_nan(series: pd.Series) -> float:
    arr = series.to_numpy(dtype=float)
    if arr.size == 0 or np.all(np.isnan(arr)):
        return float("nan")
    return float(np.nanmean(arr))


def pct_reduction(baseline: pd.Series, smart: pd.Series) -> pd.Series:
    denom = baseline.replace(0, np.nan)
    return (baseline - smart) / denom * 100


def metric_or_nan(frame: pd.DataFrame, column: str, index: pd.Index) -> pd.Series:
    if column in frame.columns:
        return frame[column]
    return pd.Series(np.nan, index=index, dtype=float)


def ensure_columns(df: pd.DataFrame, columns: list[str]) -> pd.DataFrame:
    frame = df.copy()
    for column in columns:
        if column not in frame.columns:
            frame[column] = np.nan
    return frame[columns]


def _load_run_experiment_metadata(output_dir: Path) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    args_merged: dict[str, Any] = {}
    for metadata_path in (
        output_dir.parent / "run_metadata.json",
        output_dir.parent / "benchmark_timing.json",
        output_dir / "run_metadata.json",
        output_dir / "benchmark_timing.json",
    ):
        if not metadata_path.exists():
            continue
        try:
            payload = json.loads(metadata_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(payload, dict):
            continue
        if isinstance(payload.get("args"), dict):
            args_merged.update(payload["args"])
        merged.update({k: v for k, v in payload.items() if k != "args"})
    if args_merged:
        merged["args"] = args_merged
    return merged


def _load_run_metrics_exact(output_dir: Path) -> bool | None:
    payload = _load_run_experiment_metadata(output_dir)
    if payload:
        try:
            metrics_exact = payload.get("metrics_exact")
            if isinstance(metrics_exact, bool):
                return metrics_exact
            experiment_mode = str(payload.get("experiment_mode", "") or "").strip().lower()
            if experiment_mode in {"sampled", "exact"}:
                return experiment_mode == "exact"
        except Exception:
            pass
        try:
            args = payload.get("args", {})
            metric_interval = float(args.get("metric_sample_interval", 0.0) or 0.0)
            vehicle_interval = float(args.get("vehicle_sample_interval", 0.0) or 0.0)
            return metric_interval == 0.0 and vehicle_interval == 0.0
        except Exception:
            pass
    return None


def _load_csv_any(*paths: Path) -> pd.DataFrame:
    for path in paths:
        if not path or not path.exists():
            continue
        try:
            return pd.read_csv(path)
        except Exception:
            continue
    return pd.DataFrame()


def _first_existing_path(*paths: Path) -> Path:
    for path in paths:
        if path.exists():
            return path
    return paths[0]


def _first_nonempty(value: Any, default: Any = NA_TEXT) -> Any:
    if value is None:
        return default
    try:
        if pd.isna(value):
            return default
    except Exception:
        pass
    text = str(value).strip()
    if text.lower() in {"nan", "none", "null"}:
        return default
    return text if text else default


def _safe_value_or_na(value: Any) -> Any:
    if value is None:
        return NA_TEXT
    try:
        if pd.isna(value):
            return NA_TEXT
    except Exception:
        pass
    if isinstance(value, str) and value.strip().lower() in {"nan", "none", "null", ""}:
        return NA_TEXT
    return value


def _xml_count(path: str | Path | None, tag: str) -> int:
    if not path:
        return 0
    p = Path(path)
    if not p.exists():
        return 0
    try:
        root = ET.parse(p).getroot()
    except Exception:
        return 0
    return sum(1 for _ in root.iter(tag))


def _load_demand_validation_rows(output_dir: Path) -> pd.DataFrame:
    csv_layout = ensure_csv_output_layout(output_dir)
    return _load_csv_any(
        csv_layout.results / "demand_validation_summary.csv",
        output_dir / "demand_validation_summary.csv",
    )


def _load_demand_params_row(output_dir: Path) -> dict[str, Any]:
    csv_layout = ensure_csv_output_layout(output_dir)
    demand_df = _load_csv_any(csv_layout.results / "demand_params.csv", output_dir / "demand_params.csv")
    if demand_df.empty:
        return {}
    return demand_df.iloc[0].to_dict()


def _validation_lookup(validation_df: pd.DataFrame, validation_name: str) -> dict[str, Any]:
    if validation_df.empty or "validation_name" not in validation_df.columns:
        return {}
    rows = validation_df[validation_df["validation_name"] == validation_name]
    if rows.empty:
        return {}
    return rows.iloc[0].to_dict()


def _report_experiment_kind(metrics_exact: bool | None, experiment_mode: str) -> str:
    if metrics_exact and str(experiment_mode).strip().lower() == "exact":
        return "final"
    return "preflight"


def _report_target_filename(experiment_kind: str) -> str:
    return "final_tradeoff_summary.csv" if experiment_kind == "final" else "preflight_tradeoff_summary.csv"


def _report_status_from_validations(
    vehicle_status: str,
    pedestrian_status: str,
    vehicle_reason: str = "",
    pedestrian_reason: str = "",
) -> tuple[str, str]:
    statuses = [str(vehicle_status).strip().lower(), str(pedestrian_status).strip().lower()]
    if any(status == "fail" for status in statuses):
        status = "fail"
    elif any(status == "warning" for status in statuses):
        status = "warning"
    else:
        status = "pass"
    reason_bits = []
    for reason in (vehicle_reason, pedestrian_reason):
        text = str(reason).strip()
        if not text or text.lower() in {"nan", "none", "null"}:
            continue
        reason_bits.append(text)
    return status, ";".join(reason_bits) if reason_bits else "ok"


def _report_guardrail_paths(output_dir: Path) -> dict[str, Path]:
    csv_layout = ensure_csv_output_layout(output_dir)
    return {
        "csv_layout": csv_layout,
        "demand_params": csv_layout.results / "demand_params.csv",
        "demand_validation": csv_layout.results / "demand_validation_summary.csv",
        "vehicle_validation": csv_layout.results / "vehicle_flow_policy_validation.csv",
        "ped_validation": csv_layout.results / "pedestrian_flow_policy_validation.csv",
        "seed_results": csv_layout.results / "simulation_results_seed.csv",
        "avg_results": csv_layout.results / "simulation_result.csv",
    }


def _move_to_stale(path: Path) -> None:
    if not path.exists():
        return
    stale_dir = path.parent / "stale"
    stale_dir.mkdir(parents=True, exist_ok=True)
    target = stale_dir / path.name
    if target.exists():
        target = stale_dir / f"{path.stem}_{pd.Timestamp.utcnow().strftime('%Y%m%d_%H%M%S')}{path.suffix}"
    path.replace(target)


def _reconcile_report_artifacts(output_dir: Path, report_dir: Path, report_name: str) -> None:
    active_paths = {
        "final_tradeoff_summary.csv": [report_dir / "final_tradeoff_summary.csv", output_dir / "final_tradeoff_summary.csv"],
        "preflight_tradeoff_summary.csv": [report_dir / "preflight_tradeoff_summary.csv", output_dir / "preflight_tradeoff_summary.csv"],
    }
    for name, paths in active_paths.items():
        if name == report_name:
            continue
        for path in paths:
            _move_to_stale(path)


def _count_affected_route_vehicles(
    route_file: str | Path | None,
    vehicle_conflict_edges: set[str],
) -> int | None:
    if not route_file:
        return None
    route_path = Path(route_file)
    if not route_path.exists() or not vehicle_conflict_edges:
        return None
    try:
        root = ET.parse(route_path).getroot()
    except Exception:
        return None
    conflict_edges = {str(edge) for edge in vehicle_conflict_edges if str(edge)}
    if not conflict_edges:
        return None
    count = 0
    for vehicle in root.findall("vehicle"):
        route = vehicle.find("route")
        if route is None:
            continue
        edges = str(route.attrib.get("edges", "")).split()
        if any(edge in conflict_edges for edge in edges):
            count += 1
    return count


def _load_vehicle_conflict_edges(nets_dir: Path | None, crosswalk_id: str) -> set[str]:
    if nets_dir is None:
        return set()
    metadata_path = nets_dir / f"cw_{crosswalk_id}" / "metadata.json"
    if not metadata_path.exists():
        return set()
    try:
        payload = json.loads(metadata_path.read_text(encoding="utf-8"))
    except Exception:
        return set()
    conflict_edges = payload.get("vehicle_conflict_edges", []) if isinstance(payload, dict) else []
    return {str(edge) for edge in conflict_edges if str(edge)}


def build_simulation_summary(avg_df: pd.DataFrame, seed_df: pd.DataFrame) -> pd.DataFrame:
    if avg_df.empty:
        return pd.DataFrame(columns=SIMULATION_SUMMARY_COLUMNS)

    seed_counts = (
        seed_df.groupby(["crosswalk_id", "scenario"], as_index=False)["seed"]
        .nunique()
        .rename(columns={"seed": "seed_count"})
        if not seed_df.empty and {"crosswalk_id", "scenario", "seed"}.issubset(seed_df.columns)
        else pd.DataFrame(columns=["crosswalk_id", "scenario", "seed_count"])
    )

    summary = avg_df.merge(seed_counts, on=["crosswalk_id", "scenario"], how="left")
    summary["crosswalk_id"] = summary["crosswalk_id"].astype(str)
    return ensure_columns(summary, SIMULATION_SUMMARY_COLUMNS).sort_values(
        ["crosswalk_id", "scenario"]
    )


def build_baseline_vs_smart_summary(
    summary_df: pd.DataFrame,
    metrics_exact: bool | None = None,
) -> pd.DataFrame:
    if summary_df.empty:
        return pd.DataFrame(columns=BASELINE_SMART_COLUMNS)

    baseline = summary_df[summary_df["scenario"] == "baseline"].set_index("crosswalk_id")
    smart = summary_df[summary_df["scenario"] == "smart"].set_index("crosswalk_id")
    common = baseline.index.intersection(smart.index)
    if common.empty:
        return pd.DataFrame(columns=BASELINE_SMART_COLUMNS)

    result = pd.DataFrame(
        {
            "crosswalk_id": common,
            "safety_risk_delta": smart.loc[common, "safety_risk_score"].to_numpy()
            - baseline.loc[common, "safety_risk_score"].to_numpy(),
            "accident_expected_delta": smart.loc[common, "accident_expected_value"].to_numpy()
            - baseline.loc[common, "accident_expected_value"].to_numpy(),
            "elderly_incomplete_crossings_delta": smart.loc[common, "elderly_incomplete_crossings"].to_numpy()
            - baseline.loc[common, "elderly_incomplete_crossings"].to_numpy(),
            "pedestrian_clearance_failure_delta": smart.loc[common, "pedestrian_clearance_failure_count"].to_numpy()
            - baseline.loc[common, "pedestrian_clearance_failure_count"].to_numpy(),
            "unfinished_crossing_delta": smart.loc[common, "unfinished_crossing_count"].to_numpy()
            - baseline.loc[common, "unfinished_crossing_count"].to_numpy(),
            "avg_vehicle_delay_delta_sec": smart.loc[common, "avg_vehicle_delay_sec"].to_numpy()
            - baseline.loc[common, "avg_vehicle_delay_sec"].to_numpy(),
            "avg_queue_length_delta": smart.loc[common, "avg_queue_length"].to_numpy()
            - baseline.loc[common, "avg_queue_length"].to_numpy(),
            "max_queue_length_delta": smart.loc[common, "max_queue_length"].to_numpy()
            - baseline.loc[common, "max_queue_length"].to_numpy(),
            "surrounding_road_delay_delta_sec": smart.loc[common, "surrounding_road_delay_sec"].to_numpy()
            - baseline.loc[common, "surrounding_road_delay_sec"].to_numpy(),
            "vehicle_delay_cost_delta": smart.loc[common, "vehicle_delay_cost"].to_numpy()
            - baseline.loc[common, "vehicle_delay_cost"].to_numpy(),
            "extension_count_smart": smart.loc[common, "extension_count"].to_numpy(),
            "total_extension_sec_smart": smart.loc[common, "total_extension_sec"].to_numpy(),
            "traffic_metric_scope": "global_500m",
            "metrics_exact": metrics_exact if metrics_exact is not None else pd.NA,
        }
    )
    return ensure_columns(result, BASELINE_SMART_COLUMNS).sort_values("crosswalk_id")


def build_local_tradeoff_summary(seed_tradeoff: pd.DataFrame) -> pd.DataFrame:
    if seed_tradeoff.empty:
        return pd.DataFrame()
    required = {"candidate_id", "seed"}
    if not required.issubset(seed_tradeoff.columns):
        return pd.DataFrame()

    grouped = (
        seed_tradeoff.groupby("candidate_id", as_index=False)
        .agg(
            seed_count=("seed", "nunique"),
            metrics_exact=("metrics_exact", "first"),
            experiment_mode=("experiment_mode", "first"),
            generated_vehicle_count=("generated_vehicle_count", "mean"),
            network_arrived_vehicles=("network_arrived_vehicles", "mean"),
            toy_queue_vehicle_arrivals=("toy_queue_vehicle_arrivals", "mean"),
            affected_route_vehicle_count=("affected_route_vehicle_count", "mean"),
            affected_route_vehicle_share=("affected_route_vehicle_share", "mean"),
            local_approach_queue_avg_baseline=("local_approach_queue_avg_baseline", "mean"),
            local_approach_queue_avg_smart=("local_approach_queue_avg_smart", "mean"),
            local_approach_queue_delta=("local_approach_queue_delta", "mean"),
            local_approach_queue_max_baseline=("local_approach_queue_max_baseline", "mean"),
            local_approach_queue_max_smart=("local_approach_queue_max_smart", "mean"),
            local_approach_queue_max_delta=("local_approach_queue_max_delta", "mean"),
            surrounding_lane_count=("surrounding_lane_count", "mean"),
        )
        .sort_values("candidate_id")
    )
    grouped["traffic_metric_scope"] = "local_approach+affected_route"
    return grouped


def build_tradeoff_report_summary(
    seed_df: pd.DataFrame,
    seed_tradeoff: pd.DataFrame,
    output_dir: Path,
    cli_simulation_mode: str | None = None,
) -> pd.DataFrame:
    if seed_df.empty or seed_tradeoff.empty:
        return pd.DataFrame(columns=REPORT_COLUMNS)

    exp_meta = _load_run_experiment_metadata(output_dir)
    metrics_exact = _load_run_metrics_exact(output_dir)
    experiment_mode = str(exp_meta.get("experiment_mode", "") or "")
    metadata_result_status = _first_nonempty(exp_meta.get("result_status"), "")
    metadata_result_reason = _first_nonempty(exp_meta.get("result_reason"), "")
    experiment_kind = _report_experiment_kind(metrics_exact, experiment_mode)
    if metadata_result_status == "not_verified" or metadata_result_reason == "regeneration_without_original_metadata":
        experiment_kind = "preflight"
    simulation_mode = _first_nonempty(
        exp_meta.get("args", {}).get("simulation_mode"),
        "",
    )
    if simulation_mode == "":
        simulation_mode = _first_nonempty(
            exp_meta.get("simulation_mode"),
            "",
        )
    if simulation_mode == "":
        simulation_mode = _first_nonempty(cli_simulation_mode, "")
    demand_df = _load_demand_params_row(output_dir)
    validation_df = _load_demand_validation_rows(output_dir)
    vehicle_val = _validation_lookup(validation_df, "vehicle_flow_policy_validation")
    pedestrian_val = _validation_lookup(validation_df, "pedestrian_flow_policy_validation")
    vehicle_status = str(vehicle_val.get("status", NA_TEXT) or NA_TEXT)
    pedestrian_status = str(pedestrian_val.get("status", NA_TEXT) or NA_TEXT)
    result_status, result_reason = _report_status_from_validations(
        vehicle_status,
        pedestrian_status,
        str(vehicle_val.get("reason", "") or ""),
        str(pedestrian_val.get("reason", "") or ""),
    )
    if metadata_result_status:
        result_status = metadata_result_status
        result_reason = metadata_result_reason or result_reason

    rows: list[dict[str, Any]] = []
    seed_df = seed_df.copy()
    seed_df["crosswalk_id"] = seed_df["crosswalk_id"].astype(str)
    for candidate_id, group in seed_tradeoff.groupby("candidate_id", sort=True):
        candidate_id = str(candidate_id)
        smart_rows = seed_df[(seed_df["crosswalk_id"] == candidate_id) & (seed_df["scenario"] == "smart")]
        smart_seed = smart_rows.iloc[0] if not smart_rows.empty else None
        generated_vehicle_count = _safe_value_or_na(
            smart_seed.get("generated_vehicle_count") if smart_seed is not None else demand_df.get("generated_vehicle_count")
        )
        generated_pedestrian_count = _safe_value_or_na(
            smart_seed.get("generated_pedestrian_count") if smart_seed is not None else demand_df.get("generated_pedestrian_count")
        )
        scenario_name = _safe_value_or_na(
            smart_seed.get("scenario_name") if smart_seed is not None else demand_df.get("scenario_name")
        )
        candidate_simulation_mode = simulation_mode
        if candidate_simulation_mode == "" and smart_seed is not None:
            candidate_simulation_mode = _first_nonempty(smart_seed.get("simulation_mode"), "")
        if candidate_simulation_mode == "" and "simulation_mode" in group.columns:
            candidate_simulation_mode = _first_nonempty(group["simulation_mode"].iloc[0], "")
        if candidate_simulation_mode == "":
            candidate_simulation_mode = NA_TEXT
        traffic_metric_scope = _safe_value_or_na(group["traffic_metric_scope"].iloc[0] if "traffic_metric_scope" in group.columns else NA_TEXT)
        pedestrian_waiting_time_p95 = _safe_value_or_na(
            smart_seed.get("pedestrian_waiting_time_p95") if smart_seed is not None else NA_TEXT
        )
        rows.append(
            {
                "candidate_id": candidate_id,
                "seed_count": int(group["seed"].nunique()) if "seed" in group.columns else 0,
                "scenario_name": scenario_name,
                "simulation_mode": candidate_simulation_mode,
                "experiment_kind": experiment_kind,
                "traffic_metric_scope": traffic_metric_scope,
                "generated_vehicle_count": generated_vehicle_count,
                "generated_pedestrian_count": generated_pedestrian_count,
                "demand_vehicle_validation_status": vehicle_status,
                "demand_pedestrian_validation_status": pedestrian_status,
                "safety_improvement_pct": _safe_value_or_na(group["safety_improvement_pct"].mean() if "safety_improvement_pct" in group.columns else NA_TEXT),
                "traffic_degradation_pct": _safe_value_or_na(group["traffic_degradation_pct"].mean() if "traffic_degradation_pct" in group.columns else NA_TEXT),
                "tradeoff_ratio": _safe_value_or_na(group["tradeoff_ratio"].mean() if "tradeoff_ratio" in group.columns else NA_TEXT),
                "throughput": _safe_value_or_na(group["throughput"].mean() if "throughput" in group.columns else NA_TEXT),
                "network_arrived_vehicles": _safe_value_or_na(group["network_arrived_vehicles"].mean() if "network_arrived_vehicles" in group.columns else NA_TEXT),
                "vehicle_mean_time_loss": _safe_value_or_na(group["vehicle_mean_time_loss"].mean() if "vehicle_mean_time_loss" in group.columns else NA_TEXT),
                "vehicle_mean_travel_time": _safe_value_or_na(group["vehicle_mean_travel_time"].mean() if "vehicle_mean_travel_time" in group.columns else NA_TEXT),
                "vehicle_mean_speed": _safe_value_or_na(group["vehicle_mean_speed"].mean() if "vehicle_mean_speed" in group.columns else NA_TEXT),
                "pedestrian_waiting_time_mean": _safe_value_or_na(group["pedestrian_waiting_time_mean"].mean() if "pedestrian_waiting_time_mean" in group.columns else NA_TEXT),
                "pedestrian_waiting_time_p95": pedestrian_waiting_time_p95,
                "result_status": result_status,
                "result_reason": ";".join(
                    part for part in [
                        _first_nonempty(result_reason, ""),
                        "sampled_preflight" if experiment_kind == "preflight" else "final_exact",
                    ]
                    if part and str(part).strip()
                ),
            }
        )

    report_df = pd.DataFrame(rows)
    if report_df.empty:
        return pd.DataFrame(columns=REPORT_COLUMNS)

    missing_columns = [column for column in REPORT_COLUMNS if column not in report_df.columns]
    for column in missing_columns:
        report_df[column] = NA_TEXT
    report_df = report_df[REPORT_COLUMNS]
    return report_df


def validate_tradeoff_report_guardrails(output_dir: Path, report_df: pd.DataFrame) -> None:
    if report_df.empty:
        raise ValueError("tradeoff report is empty")

    forbidden = sorted(set(report_df.columns).intersection(REPORT_FORBIDDEN_COLUMNS))
    if forbidden:
        raise ValueError(f"tradeoff report contains forbidden columns: {forbidden}")

    guardrail_paths = _report_guardrail_paths(output_dir)
    demand_params_path = guardrail_paths["demand_params"]
    if not demand_params_path.exists():
        raise ValueError(f"required report input missing: {demand_params_path}")

    demand = _load_demand_params_row(output_dir)
    if not demand:
        raise ValueError("demand_params.csv is missing for tradeoff report generation")

    generated_vehicle_count = int(float(demand.get("generated_vehicle_count", 0) or 0))
    expected_vehicle_count = int(
        float(
            demand.get("expected_vehicle_count_for_duration")
            or demand.get("total_vehicle_count_600s")
            or generated_vehicle_count
        )
    )
    if generated_vehicle_count != expected_vehicle_count:
        raise ValueError(
            f"generated_vehicle_count mismatch: generated={generated_vehicle_count}, expected={expected_vehicle_count}"
        )

    route_file = demand.get("generated_vehicle_route_file")
    trip_file = demand.get("generated_vehicle_trip_file")
    if route_file is None or (isinstance(route_file, float) and pd.isna(route_file)) or str(route_file).strip().lower() == "nan":
        route_file = trip_file
    seed_value = str(demand.get("seed", "") or "")
    ped_file = Path(str(route_file)).with_name(f"peds_seed{seed_value}.rou.xml") if route_file else None

    route_vehicle_count = _xml_count(route_file, "vehicle")
    trip_vehicle_count = _xml_count(trip_file, "trip")
    if route_vehicle_count != generated_vehicle_count:
        raise ValueError(
            f"route XML vehicle count mismatch: route={route_vehicle_count}, expected={generated_vehicle_count}"
        )
    if trip_vehicle_count != generated_vehicle_count:
        raise ValueError(
            f"trip XML vehicle count mismatch: trip={trip_vehicle_count}, expected={generated_vehicle_count}"
        )

    generated_pedestrian_count = int(float(demand.get("generated_pedestrian_count", 0) or 0))
    pedestrian_count_600s = int(float(demand.get("pedestrian_count_600s", 0) or 0))
    if generated_pedestrian_count != pedestrian_count_600s:
        raise ValueError(
            f"generated_pedestrian_count mismatch: generated={generated_pedestrian_count}, policy={pedestrian_count_600s}"
        )
    person_count = _xml_count(ped_file, "person")
    walk_count = _xml_count(ped_file, "walk")
    if person_count <= 0 or walk_count <= 0:
        raise ValueError("pedestrian XML has no person/walk entries")
    if person_count != generated_pedestrian_count or walk_count != generated_pedestrian_count:
        raise ValueError(
            f"pedestrian XML count mismatch: person={person_count}, walk={walk_count}, expected={generated_pedestrian_count}"
        )

    exp_meta = _load_run_experiment_metadata(output_dir)
    metrics_exact = _load_run_metrics_exact(output_dir)
    experiment_mode = str(exp_meta.get("experiment_mode", "") or "").strip().lower()
    expected_kind = _report_experiment_kind(metrics_exact, experiment_mode)
    observed_kind = str(report_df.iloc[0].get("experiment_kind", "") or "").strip().lower()
    if observed_kind != expected_kind:
        raise ValueError(f"experiment_kind mismatch: observed={observed_kind}, expected={expected_kind}")
    if not metrics_exact and observed_kind != "preflight":
        raise ValueError("metrics_exact=false must map to preflight report")
    if experiment_mode == "sampled" and observed_kind != "preflight":
        raise ValueError("experiment_mode=sampled must map to preflight report")


def write_methodology_report(
    output_dir: Path,
    model_params: dict[str, dict[str, Any]],
    simulation_summary: pd.DataFrame,
    delta_summary: pd.DataFrame,
) -> None:
    if simulation_summary.empty:
        status_text = "현재 상태는 `pending/not_run`이다. 테스트 실행 단계이며 최종 결과표는 아직 채워지지 않았다."
    else:
        status_text = (
            f"현재 결과는 테스트 실행 단계 산출물이며, `crosswalk_id` 기준 요약 행 수는 {len(simulation_summary)}개다."
        )

    assumptions_preview = pd.DataFrame(
        [
            {
                "parameter": name,
                "value": json.dumps(meta.get("value"), ensure_ascii=False)
                if isinstance(meta.get("value"), (dict, list))
                else meta.get("value"),
                "source_type": meta.get("source_type"),
                "used_in": meta.get("used_in"),
            }
            for name, meta in model_params.items()
        ]
    )
    body = f"""# 시뮬레이션 메모

## 목적

이 시뮬레이션은 미래 교통량을 정확히 예측하는 모델이 아니라, 동일 조건에서 후보 횡단보도별 baseline/smart를 비교하는 정책 시나리오 실험이다.

- 동일 조건 비교
- 상대적 효과 평가
- 후보 횡단보도별 전/후 비교
- 5초 보행녹색 연장의 안전-지체 trade-off 산출

시뮬레이션은 후보를 자동 선정하지 않는다. 후보 횡단보도는 사전에 선정되며, 시뮬레이션은 선정된 후보에 대한 정량 비교와 비용-효과 보정 용도로 사용한다.

## 현재 상태

{status_text}

- SUMO 시각화는 일부 테스트 수준으로만 유지한다.
- 실제 최종 결과는 아직 확정본이 아니다.
- 서버 또는 실행 환경 연결은 계속 진행 중일 수 있다.

## 핵심 모델 규칙

- baseline: 고정 신호 유지
- smart: 보행 녹색 종료 직전 감지 시 5초 1회 연장
- 연장 조건: 보행 녹색 phase, 잔여시간 임계 이내, crossing/detector 보행자 존재, 같은 cycle 미연장, false negative 미발생
- 이벤트 기반 용량 감소: `accident`, `bus_stop`
- 네트워크 기본값: `expanded` + `buffer_m=1000`

## 사용 assumptions

{df_to_markdown(assumptions_preview)}

## 최종 보고용 파일

- `outputs/csv/results/simulation_summary.csv`
- `outputs/csv/results/baseline_vs_smart_summary.csv`
- `outputs/csv/report/preflight_tradeoff_summary.csv` 또는 `outputs/csv/report/final_tradeoff_summary.csv`
- `outputs/model_assumptions_used.csv`
- `figures/tradeoff_summary.png`

## 비교 결과 미리보기

{df_to_markdown(delta_summary.head(20)) if not delta_summary.empty else '비교 가능한 baseline/smart 결과가 아직 없다.'}
"""
    (output_dir / "report_4_methodology.md").write_text(body, encoding="utf-8")


def comparison_table(avg_df: pd.DataFrame) -> pd.DataFrame:
    baseline = avg_df[avg_df["시나리오"] == "baseline"].set_index("횡단보도ID")
    smart = avg_df[avg_df["시나리오"] == "smart"].set_index("횡단보도ID")
    common = baseline.index.intersection(smart.index)
    baseline = baseline.loc[common]
    smart = smart.loc[common]
    baseline_sur_delay = metric_or_nan(baseline, "surrounding_veh_avg_delay_sec", common)
    smart_sur_delay = metric_or_nan(smart, "surrounding_veh_avg_delay_sec", common)
    baseline_sur_queue = metric_or_nan(baseline, "surrounding_queue_total_max", common)
    smart_sur_queue = metric_or_nan(smart, "surrounding_queue_total_max", common)
    baseline_adj_queue = metric_or_nan(baseline, "adjacent_tls_queue_total_max", common)
    smart_adj_queue = metric_or_nan(smart, "adjacent_tls_queue_total_max", common)

    table = pd.DataFrame(
        {
            "횡단보도ID": common,
            "행정동": baseline["행정동"].values,
            "읍면동명": baseline["읍면동명"].values,
            "노인비율": baseline.get("노인비율", pd.Series(index=common, dtype=float)).values,
            "baseline_PET_B_고위험": baseline.get("PET_B_surrogate_severe", pd.Series(index=common, dtype=float)).values,
            "smart_PET_B_고위험": smart.get("PET_B_surrogate_severe", pd.Series(index=common, dtype=float)).values,
            "PET_B_고위험_감소율_pct": pct_reduction(
                baseline.get("PET_B_surrogate_severe", pd.Series(index=common, dtype=float)),
                smart.get("PET_B_surrogate_severe", pd.Series(index=common, dtype=float)),
            ).values,
            "baseline_고령자미완료": baseline.get("elderly_incomplete_cross", pd.Series(index=common, dtype=float)).values,
            "smart_고령자미완료": smart.get("elderly_incomplete_cross", pd.Series(index=common, dtype=float)).values,
            "고령자미완료_감소율_pct": pct_reduction(
                baseline.get("elderly_incomplete_cross", pd.Series(index=common, dtype=float)),
                smart.get("elderly_incomplete_cross", pd.Series(index=common, dtype=float)),
            ).values,
            "baseline_차량평균지체_sec": baseline.get("veh_avg_delay_sec", pd.Series(index=common, dtype=float)).values,
            "smart_차량평균지체_sec": smart.get("veh_avg_delay_sec", pd.Series(index=common, dtype=float)).values,
            "차량평균지체_증가_sec": (
                smart.get("veh_avg_delay_sec", pd.Series(index=common, dtype=float))
                - baseline.get("veh_avg_delay_sec", pd.Series(index=common, dtype=float))
            ).values,
            "baseline_주변차량평균지체_sec": baseline_sur_delay.values,
            "smart_주변차량평균지체_sec": smart_sur_delay.values,
            "주변차량평균지체_증가_sec": (smart_sur_delay - baseline_sur_delay).values,
            "baseline_주변전체최대대기행렬": baseline_sur_queue.values,
            "smart_주변전체최대대기행렬": smart_sur_queue.values,
            "주변전체최대대기행렬_증가": (smart_sur_queue - baseline_sur_queue).values,
            "baseline_인접교차로최대대기행렬": baseline_adj_queue.values,
            "smart_인접교차로최대대기행렬": smart_adj_queue.values,
            "인접교차로최대대기행렬_증가": (smart_adj_queue - baseline_adj_queue).values,
        }
    )
    return table.sort_values("PET_B_고위험_감소율_pct", ascending=False, na_position="last")


def report_1(avg_df: pd.DataFrame, output_dir: Path) -> pd.DataFrame:
    report = avg_df.copy()
    write_csv_utf8_sig(report, output_dir / "report_1_simulation_results.csv")
    return report


def write_comparison_report(comp: pd.DataFrame, output_dir: Path) -> None:
    if comp.empty:
        body = "비교 가능한 baseline/smart 결과가 없습니다.\n"
    else:
        pet_reduction = mean_or_nan(comp["PET_B_고위험_감소율_pct"])
        delay_delta = mean_or_nan(comp["차량평균지체_증가_sec"])
        body = f"""# 일반 횡단보도 vs 스마트 횡단보도 시뮬레이션 비교 보고서

- 분석 횡단보도 수: {len(comp)}
- PET_B 고위험 감소율 평균: {pet_reduction:.2f}%
- 차량 평균 지체 증가(접근부): {delay_delta:.2f} sec

{df_to_markdown(comp.head(20))}
"""
    (output_dir / "report_2_comparison.md").write_text(body, encoding="utf-8")


def write_tradeoff_report(comp: pd.DataFrame, output_dir: Path) -> None:
    body = "# 노인 보행 안전 vs 차량 지체 트레이드오프 분석\n\n"
    if comp.empty:
        body += "비교 가능한 결과가 없습니다.\n"
    else:
        body += df_to_markdown(
            comp[
                [
                    "횡단보도ID",
                    "행정동",
                    "PET_B_고위험_감소율_pct",
                    "차량평균지체_증가_sec",
                    "주변차량평균지체_증가_sec",
                    "주변전체최대대기행렬_증가",
                ]
            ]
        )
    (output_dir / "report_3_tradeoff.md").write_text(body, encoding="utf-8")


def model_parameter_markdown(model_params: dict[str, dict[str, Any]]) -> str:
    rows = []
    for name, meta in model_params.items():
        rows.append(
            {
                "parameter": name,
                "value": json.dumps(meta.get("value"), ensure_ascii=False)
                if isinstance(meta.get("value"), (dict, list))
                else meta.get("value"),
                "unit": meta.get("unit"),
                "source_type": meta.get("source_type"),
                "source_note": meta.get("source_note"),
                "fallback": meta.get("fallback"),
            }
        )
    return df_to_markdown(pd.DataFrame(rows))


def parse_osm_poi_from_net_dirs(nets_dir: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    bus_stops: list[dict[str, Any]] = []
    subway_ents: list[dict[str, Any]] = []
    for osm in nets_dir.glob("cw_*/map.osm"):
        try:
            root = ET.parse(osm).getroot()
        except Exception:
            continue
        for node in root.findall("node"):
            tags = {t.attrib.get("k"): t.attrib.get("v") for t in node.findall("tag")}
            lat = float(node.attrib.get("lat", "0"))
            lon = float(node.attrib.get("lon", "0"))
            name = tags.get("name", "")
            if tags.get("highway") == "bus_stop":
                bus_stops.append({"lat": lat, "lon": lon, "name": name})
            if tags.get("railway") == "subway_entrance":
                subway_ents.append({"lat": lat, "lon": lon, "name": name})
    return bus_stops, subway_ents


def write_figures(
    delta_summary: pd.DataFrame,
    figures_dir: Path,
) -> None:
    figures_dir.mkdir(parents=True, exist_ok=True)
    try:
        ensure_matplotlib_env()
        import matplotlib

        matplotlib.use("Agg")
        configure_matplotlib(matplotlib)
        import matplotlib.pyplot as plt
    except ImportError:
        return

    plt.figure(figsize=(8, 6))
    if delta_summary.empty:
        plt.text(0.5, 0.5, "pending / not_run", ha="center", va="center", fontsize=16)
        plt.xlim(0, 1)
        plt.ylim(0, 1)
        plt.xticks([])
        plt.yticks([])
    else:
        x_vals = pd.to_numeric(delta_summary["avg_vehicle_delay_delta_sec"], errors="coerce")
        y_vals = pd.to_numeric(delta_summary["safety_risk_delta"], errors="coerce")
        plt.scatter(
            x_vals,
            y_vals,
            color="#1f5aa6",
            s=70,
            alpha=0.85,
        )
        for row in delta_summary.itertuples(index=False):
            plt.annotate(
                str(row.crosswalk_id),
                (row.avg_vehicle_delay_delta_sec, row.safety_risk_delta),
                xytext=(4, 4),
                textcoords="offset points",
                fontsize=8,
            )
        plt.axhline(0, color="gray", linewidth=1)
        plt.axvline(0, color="gray", linewidth=1)
        plt.xlabel("Average vehicle delay delta (sec)")
        plt.ylabel("Safety risk delta")
    plt.title("Safety-delay tradeoff from a 5-second pedestrian green extension")
    plt.tight_layout()
    plt.savefig(figures_dir / "tradeoff_summary.png", dpi=160)
    plt.close()


def safe_div(a: float, b: float) -> float:
    return float(a / b) if b and not math.isclose(b, 0.0) else float("nan")


def numeric_series(frame: pd.DataFrame, column: str, default: float = np.nan) -> pd.Series:
    if column in frame.columns:
        return pd.to_numeric(frame[column], errors="coerce")
    return pd.Series([default] * len(frame), index=frame.index, dtype="float64")


def build_seed_level_tradeoff(seed_df: pd.DataFrame) -> pd.DataFrame:
    if seed_df.empty:
        return pd.DataFrame()
    required = {"crosswalk_id", "seed", "scenario"}
    if not required.issubset(seed_df.columns):
        return pd.DataFrame()
    base = seed_df[seed_df["scenario"] == "baseline"].copy()
    smart = seed_df[seed_df["scenario"] == "smart"].copy()
    keys = ["crosswalk_id", "seed"]
    merged = base.merge(smart, on=keys, suffixes=("_baseline", "_smart"))
    if merged.empty:
        return pd.DataFrame()

    out = pd.DataFrame()
    out["candidate_id"] = merged["crosswalk_id"].astype(str)
    out["seed"] = merged["seed"]
    out["scenario"] = "baseline_vs_smart"

    out["pedestrian_waiting_time_mean_baseline"] = numeric_series(merged, "average_pedestrian_wait_time_baseline")
    out["pedestrian_waiting_time_mean_smart"] = numeric_series(merged, "average_pedestrian_wait_time_smart")
    out["pedestrian_waiting_time_mean"] = out["pedestrian_waiting_time_mean_smart"]
    out["pedestrian_waiting_time_p95"] = np.nan
    out["vehicle_mean_time_loss"] = numeric_series(merged, "veh_avg_delay_sec_smart")
    out["vehicle_mean_travel_time"] = numeric_series(merged, "surrounding_avg_travel_time_sec_smart")
    out["vehicle_mean_speed"] = numeric_series(merged, "surrounding_mean_speed_mps_smart")
    out["throughput"] = numeric_series(merged, "surrounding_throughput_veh_per_hour_smart")

    b_wait = out["pedestrian_waiting_time_mean_baseline"]
    s_wait = out["pedestrian_waiting_time_mean_smart"]
    b_clearance = numeric_series(merged, "pedestrian_clearance_failure_count_baseline")
    s_clearance = numeric_series(merged, "pedestrian_clearance_failure_count_smart")
    if b_clearance.isna().all():
        b_clearance = numeric_series(merged, "unfinished_crossing_count_baseline")
    if s_clearance.isna().all():
        s_clearance = numeric_series(merged, "unfinished_crossing_count_smart")
    b_risk = numeric_series(merged, "low_pet_event_count_baseline")
    s_risk = numeric_series(merged, "low_pet_event_count_smart")
    safety_baseline = b_clearance.where(b_clearance.notna(), b_risk)
    safety_smart = s_clearance.where(s_clearance.notna(), s_risk)
    b_loss = numeric_series(merged, "veh_avg_delay_sec_baseline")
    s_loss = numeric_series(merged, "veh_avg_delay_sec_smart")
    b_spd = numeric_series(merged, "surrounding_mean_speed_mps_baseline")
    s_spd = numeric_series(merged, "surrounding_mean_speed_mps_smart")

    out["pedestrian_wait_delta_abs"] = s_wait - b_wait
    out["pedestrian_wait_improvement_pct"] = (b_wait - s_wait) / b_wait.replace(0, np.nan) * 100.0
    out["safety_metric_delta_abs"] = safety_smart - safety_baseline
    out["safety_metric_delta_pct"] = (safety_baseline - safety_smart) / safety_baseline.replace(0, np.nan) * 100.0
    out["traffic_metric_delta_abs"] = s_loss - b_loss
    out["traffic_metric_delta_pct"] = (s_loss - b_loss) / b_loss.replace(0, np.nan) * 100.0
    speed_drop_pct = (b_spd - s_spd) / b_spd.replace(0, np.nan) * 100.0
    out["safety_improvement_pct"] = out["safety_metric_delta_pct"]
    out["traffic_degradation_pct"] = (out["traffic_metric_delta_pct"] + speed_drop_pct) / 2.0
    out["tradeoff_ratio"] = out["safety_improvement_pct"] / out["traffic_degradation_pct"].replace(0, np.nan)

    out["pedestrian_completed_count"] = numeric_series(merged, "PET_A_proxy_count_smart")
    out["pedestrian_unserved_count"] = numeric_series(merged, "elderly_incomplete_cross_smart")
    out["pedestrian_red_wait_exposure"] = numeric_series(merged, "max_pedestrian_wait_time_smart")
    out["pedestrian_green_extension_count"] = numeric_series(merged, "pedestrian_green_extension_count_smart")
    out["pedestrian_green_shortage_count"] = numeric_series(merged, "PET_B_surrogate_severe_smart")
    out["pedestrian_safety_score"] = numeric_series(merged, "safety_risk_score_smart")

    out["traffic_metric_scope"] = "global_500m"
    out["generated_vehicle_count"] = numeric_series(merged, "generated_vehicle_count_smart")
    out["network_arrived_vehicles"] = numeric_series(merged, "network_arrived_vehicles_smart")
    out["toy_queue_vehicle_arrivals"] = numeric_series(merged, "total_vehicle_arrivals_smart")
    out["vehicle_count"] = out["toy_queue_vehicle_arrivals"]
    out["vehicle_count_semantics"] = "toy_queue_vehicle_arrivals"
    out["arrived_count"] = out["network_arrived_vehicles"]
    out["arrived_count_semantics"] = "network_arrived_vehicles"

    route_files = merged.get("generated_vehicle_route_file_smart")
    if route_files is None:
        route_files = merged.get("generated_vehicle_route_file_baseline")
    if isinstance(route_files, pd.Series):
        route_files = route_files.iloc[0]
    route_file = str(route_files) if route_files is not None else ""
    conflict_edges = set()
    if "candidate_id" in merged.columns:
        conflict_edges = _load_vehicle_conflict_edges(None, str(merged["crosswalk_id"].iloc[0]))
    out["affected_route_vehicle_count"] = _count_affected_route_vehicles(route_file, conflict_edges)
    generated_vehicle_count = numeric_series(merged, "generated_vehicle_count_smart")
    out["affected_route_vehicle_share"] = (
        pd.to_numeric(out["affected_route_vehicle_count"], errors="coerce")
        / generated_vehicle_count.replace(0, np.nan)
        if isinstance(generated_vehicle_count, pd.Series)
        else np.nan
    )
    out["local_approach_queue_avg_baseline"] = numeric_series(merged, "avg_queue_length_baseline")
    out["local_approach_queue_avg_smart"] = numeric_series(merged, "avg_queue_length_smart")
    out["local_approach_queue_delta"] = out["local_approach_queue_avg_smart"] - out["local_approach_queue_avg_baseline"]
    out["local_approach_queue_max_baseline"] = numeric_series(merged, "max_queue_length_baseline")
    out["local_approach_queue_max_smart"] = numeric_series(merged, "max_queue_length_smart")
    out["local_approach_queue_max_delta"] = out["local_approach_queue_max_smart"] - out["local_approach_queue_max_baseline"]
    out["surrounding_lane_count"] = numeric_series(merged, "surrounding_lane_count_smart")
    out["mean_travel_time"] = out["vehicle_mean_travel_time"]
    out["mean_delay"] = numeric_series(merged, "veh_avg_delay_sec_smart")
    out["mean_time_loss"] = numeric_series(merged, "avg_vehicle_delay_sec_smart")
    out["mean_waiting_time"] = numeric_series(merged, "surrounding_veh_avg_delay_sec_smart")
    out["mean_speed"] = out["vehicle_mean_speed"]
    out["queue_length_mean"] = numeric_series(merged, "surrounding_queue_total_avg_smart")
    out["queue_length_p95"] = np.nan
    out["stop_count_mean"] = numeric_series(merged, "queue_avg_smart")
    out["teleported_count"] = np.nan
    out["vehicle_efficiency_score"] = out["throughput"] / out["mean_delay"].replace(0, np.nan)
    return out


def build_candidate_quality_report(candidates: pd.DataFrame, nets_dir: Path, seed_tradeoff: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    by_cand = seed_tradeoff.groupby("candidate_id").size().to_dict() if not seed_tradeoff.empty else {}
    for row in candidates.itertuples(index=False):
        cw = str(getattr(row, "crosswalk_id"))
        cw_dir = nets_dir / f"cw_{cw}"
        metadata_path = cw_dir / "metadata.json"
        net_path = cw_dir / "network.net.xml"
        route_exists = any(cw_dir.glob("routes_seed*.rou.xml"))
        ped_exists = any(cw_dir.glob("peds_seed*.rou.xml"))
        baseline_exists = any(cw_dir.glob("baseline_seed*.sumocfg"))
        smart_exists = any(cw_dir.glob("smart_seed*.sumocfg"))
        md = {}
        if metadata_path.exists():
            try:
                md = json.loads(metadata_path.read_text(encoding="utf-8"))
            except Exception:
                md = {}
        has_vehicle_edges = bool(md.get("vehicle_conflict_edges"))
        has_ped_route = bool(md.get("ped_route", {}).get("from_edge")) and bool(md.get("ped_route", {}).get("to_edge"))
        has_tls = bool(md.get("tls_id"))
        has_crossing = bool(md.get("crossing_edge"))
        ran_pairs = int(by_cand.get(cw, 0))
        if all([net_path.exists(), route_exists, ped_exists, baseline_exists, smart_exists, has_vehicle_edges, has_ped_route, has_tls, has_crossing, ran_pairs > 0]):
            grade = "A"
        elif net_path.exists() and (route_exists or ped_exists):
            grade = "B"
        else:
            grade = "C"
        rows.append(
            {
                "candidate_id": cw,
                "network_exists": int(net_path.exists()),
                "vehicle_edge_connected": int(has_vehicle_edges),
                "pedestrian_path_exists": int(has_ped_route),
                "signalized_junction_exists": int(has_tls),
                "crossing_exists": int(has_crossing),
                "vehicle_route_exists": int(route_exists),
                "pedestrian_route_exists": int(ped_exists),
                "baseline_cfg_exists": int(baseline_exists),
                "smart_cfg_exists": int(smart_exists),
                "seed_pairs_ran": ran_pairs,
                "quality_grade": grade,
                "warning": "" if grade == "A" else "structure weak or run missing",
            }
        )
    return pd.DataFrame(rows)


def write_required_outputs(
    output_dir: Path,
    figures_dir: Path,
    candidates_csv: Path | None,
    nets_dir: Path | None,
    seed_df: pd.DataFrame,
    cli_simulation_mode: str | None = None,
) -> None:
    csv_layout = ensure_csv_output_layout(output_dir)
    results_dir = csv_layout.results
    report_dir = csv_layout.report
    internal_dir = csv_layout.internal
    docs_dir = output_dir / "docs"
    figs_dir = figures_dir
    results_dir.mkdir(parents=True, exist_ok=True)
    report_dir.mkdir(parents=True, exist_ok=True)
    internal_dir.mkdir(parents=True, exist_ok=True)
    docs_dir.mkdir(parents=True, exist_ok=True)
    figs_dir.mkdir(parents=True, exist_ok=True)

    experiment_metadata = _load_run_experiment_metadata(output_dir)
    metrics_exact = _load_run_metrics_exact(output_dir)
    experiment_mode = str(experiment_metadata.get("experiment_mode", "") or "")
    seed_tradeoff = build_seed_level_tradeoff(seed_df)
    if not seed_tradeoff.empty:
        seed_tradeoff["metrics_exact"] = metrics_exact if metrics_exact is not None else pd.NA
        seed_tradeoff["experiment_mode"] = experiment_mode if experiment_mode else pd.NA
        if nets_dir is not None:
            for idx, row in seed_tradeoff.iterrows():
                cw_id = str(row.get("candidate_id", ""))
                conflict_edges = _load_vehicle_conflict_edges(nets_dir, cw_id)
                if not conflict_edges:
                    continue
                route_candidates = seed_df[
                    (seed_df["crosswalk_id"].astype(str) == cw_id)
                    & (seed_df["scenario"] == "smart")
                ]["generated_vehicle_route_file"]
                route_file = str(route_candidates.iloc[0]) if not route_candidates.empty else ""
                affected_count = _count_affected_route_vehicles(route_file, conflict_edges)
                if affected_count is None:
                    continue
                generated_count_series = seed_df[
                    (seed_df["crosswalk_id"].astype(str) == cw_id)
                    & (seed_df["scenario"] == "smart")
                ]["generated_vehicle_count"]
                generated_count = float(generated_count_series.iloc[0]) if not generated_count_series.empty else np.nan
                seed_tradeoff.at[idx, "affected_route_vehicle_count"] = affected_count
                seed_tradeoff.at[idx, "affected_route_vehicle_share"] = (
                    float(affected_count) / generated_count if generated_count and not np.isnan(generated_count) else np.nan
                )

    write_csv_bundle(
        seed_tradeoff,
        results_dir / "baseline_smart_seed_results.csv",
        mirrors=[output_dir / "baseline_smart_seed_results.csv", internal_dir / "debug_tradeoff_summary.csv"],
    )

    summary = (
        seed_tradeoff.groupby("candidate_id", as_index=False)
        .agg(
            safety_improvement_pct=("safety_improvement_pct", "mean"),
            safety_improvement_std=("safety_improvement_pct", "std"),
            traffic_degradation_pct=("traffic_degradation_pct", "mean"),
            traffic_degradation_std=("traffic_degradation_pct", "std"),
            tradeoff_ratio=("tradeoff_ratio", "mean"),
            throughput=("throughput", "mean"),
            vehicle_mean_time_loss=("vehicle_mean_time_loss", "mean"),
            vehicle_mean_travel_time=("vehicle_mean_travel_time", "mean"),
            vehicle_mean_speed=("vehicle_mean_speed", "mean"),
            pedestrian_waiting_time_mean=("pedestrian_waiting_time_mean", "mean"),
            pedestrian_waiting_time_p95=("pedestrian_waiting_time_p95", "mean"),
            traffic_metric_scope=("traffic_metric_scope", "first"),
            metrics_exact=("metrics_exact", "first"),
            experiment_mode=("experiment_mode", "first"),
            generated_vehicle_count=("generated_vehicle_count", "mean"),
            network_arrived_vehicles=("network_arrived_vehicles", "mean"),
            toy_queue_vehicle_arrivals=("toy_queue_vehicle_arrivals", "mean"),
            vehicle_count=("vehicle_count", "mean"),
            arrived_count=("arrived_count", "mean"),
        )
        if not seed_tradeoff.empty
        else pd.DataFrame()
    )
    write_csv_bundle(
        summary,
        results_dir / "baseline_smart_summary.csv",
        mirrors=[output_dir / "baseline_smart_summary.csv"],
    )
    local_tradeoff_summary = build_local_tradeoff_summary(seed_tradeoff)
    write_csv_bundle(
        local_tradeoff_summary,
        results_dir / "local_tradeoff_summary.csv",
        mirrors=[output_dir / "local_tradeoff_summary.csv"],
    )

    quality = pd.DataFrame()
    if candidates_csv and candidates_csv.exists() and nets_dir and nets_dir.exists():
        candidates = pd.read_csv(candidates_csv)
        quality = build_candidate_quality_report(candidates, nets_dir, seed_tradeoff)
    write_csv_bundle(
        quality,
        results_dir / "candidate_quality_report.csv",
        mirrors=[output_dir / "candidate_quality_report.csv"],
    )

    tradeoff_summary = summary.copy()
    write_csv_bundle(
        tradeoff_summary,
        results_dir / "tradeoff_summary.csv",
        mirrors=[output_dir / "tradeoff_summary.csv"],
    )

    report_tradeoff = build_tradeoff_report_summary(seed_df, seed_tradeoff, output_dir, cli_simulation_mode=cli_simulation_mode)
    validate_tradeoff_report_guardrails(output_dir, report_tradeoff)
    report_kind = str(report_tradeoff["experiment_kind"].iloc[0]) if not report_tradeoff.empty else "preflight"
    report_name = _report_target_filename(report_kind)
    _reconcile_report_artifacts(output_dir, report_dir, report_name)
    write_csv_bundle(
        report_tradeoff,
        report_dir / report_name,
        mirrors=[output_dir / report_name],
    )

    try:
        ensure_matplotlib_env()
        import matplotlib

        matplotlib.use("Agg")
        configure_matplotlib(matplotlib)
        import matplotlib.pyplot as plt

        if not summary.empty:
            s = summary.sort_values("safety_improvement_pct", ascending=False)
            plt.figure(figsize=(10, 5))
            plt.bar(s["candidate_id"].astype(str), s["safety_improvement_pct"])
            plt.xticks(rotation=60, ha="right")
            plt.tight_layout()
            plt.savefig(figs_dir / "safety_improvement_by_candidate.png", dpi=160)
            plt.close()

            t = summary.sort_values("traffic_degradation_pct", ascending=False)
            plt.figure(figsize=(10, 5))
            plt.bar(t["candidate_id"].astype(str), t["traffic_degradation_pct"], color="#c24")
            plt.xticks(rotation=60, ha="right")
            plt.tight_layout()
            plt.savefig(figs_dir / "traffic_degradation_by_candidate.png", dpi=160)
            plt.close()

            plt.figure(figsize=(7, 6))
            plt.scatter(summary["traffic_degradation_pct"], summary["safety_improvement_pct"], color="#1f5aa6")
            for r in summary.itertuples(index=False):
                plt.annotate(str(r.candidate_id), (r.traffic_degradation_pct, r.safety_improvement_pct), fontsize=8)
            plt.xlabel("Traffic degradation (%)")
            plt.ylabel("Safety improvement (%)")
            plt.tight_layout()
            plt.savefig(figs_dir / "safety_vs_traffic_tradeoff.png", dpi=160)
            plt.close()
        else:
            for name in [
                "safety_improvement_by_candidate.png",
                "traffic_degradation_by_candidate.png",
                "safety_vs_traffic_tradeoff.png",
            ]:
                plt.figure(figsize=(7, 4))
                plt.text(0.5, 0.5, "no comparable baseline/smart result", ha="center", va="center")
                plt.xlim(0, 1)
                plt.ylim(0, 1)
                plt.xticks([])
                plt.yticks([])
                plt.tight_layout()
                plt.savefig(figs_dir / name, dpi=160)
                plt.close()
    except Exception:
        pass

    sim_md = (
        "# simulation_methodology\n\n"
        "본 분석은 중구 OSM 기반 SUMO 변환망에서 baseline/smart 상대 비교 실험을 수행했다.\n"
        "스마트 횡단보도 정책은 대기시간 단축 장치가 아니라 보행자 녹색 종료 시점의 미완료 횡단을 1회 5초 연장으로 보조하는 신호 연장 정책으로 평가했다.\n"
        "Primary safety endpoint는 보행자 평균 대기시간이 아니라 횡단 완료 실패 또는 low-PET 위험 proxy의 baseline-smart paired delta로 해석했다.\n"
    )
    (docs_dir / "simulation_methodology.md").write_text(sim_md, encoding="utf-8")

    net_md = "# network_quality_report\n\n"
    if not quality.empty:
        net_md += df_to_markdown(quality)
    else:
        net_md += "candidate quality data unavailable."
    (docs_dir / "network_quality_report.md").write_text(net_md, encoding="utf-8")

    if not seed_tradeoff.empty:
        safety_imp = float(seed_tradeoff["safety_improvement_pct"].mean())
        safety_std = float(seed_tradeoff["safety_improvement_pct"].std())
        loss_inc = float(seed_tradeoff["traffic_metric_delta_pct"].mean())
        spd_drop = float(((seed_tradeoff["traffic_degradation_pct"] * 2.0) - seed_tradeoff["traffic_metric_delta_pct"]).mean())
        interp = (
            f"스마트 횡단보도 적용 시, 전체 후보지 평균 safety proxy는 baseline 대비 {safety_imp:.1f}% 개선되었다 "
            f"(표준편차 {safety_std:.1f}%p). 반면 차량 평균 timeLoss는 {loss_inc:.1f}% 증가하고 "
            f"평균속도는 {spd_drop:.1f}% 감소하였다. 따라서 본 시뮬레이션은 안전성 대리지표 개선과 "
            f"교통 효율 비용 간 trade-off를 보였다."
        )
    else:
        interp = "실행 결과가 없어 해석 문장을 생성하지 못했다."
    (docs_dir / "final_interpretation.md").write_text(interp + "\n", encoding="utf-8")


def generate_all_reports(
    output_dir: str | Path = "outputs",
    figures_dir: str | Path = "figures",
    candidates_csv: str | Path | None = None,
    nets_dir: str | Path | None = None,
    model_parameters_path: str | Path | None = None,
    cli_simulation_mode: str | None = None,
) -> dict[str, pd.DataFrame]:
    output_dir = Path(output_dir)
    figures_dir = Path(figures_dir)
    csv_layout = ensure_csv_output_layout(output_dir)
    avg_path = _first_existing_path(
        csv_layout.results / "simulation_result.csv",
        output_dir / "simulation_result.csv",
        csv_layout.results / "simulation_results.csv",
        output_dir / "simulation_results.csv",
    )
    seed_path = _first_existing_path(
        csv_layout.results / "simulation_results_seed.csv",
        output_dir / "simulation_results_seed.csv",
        csv_layout.results / "simulation_result.csv",
        output_dir / "simulation_result.csv",
    )
    if not avg_path.exists():
        raise FileNotFoundError(f"{avg_path}가 없습니다. 먼저 시뮬레이션을 실행하세요.")

    try:
        avg_df = pd.read_csv(avg_path)
    except EmptyDataError:
        avg_df = pd.DataFrame(columns=["crosswalk_id","admin_dong","dong_name","scenario"])
    try:
        seed_df = pd.DataFrame(columns=["crosswalk_id", "scenario", "seed"]) if seed_path == avg_path else pd.read_csv(seed_path)
    except (EmptyDataError, FileNotFoundError):
        seed_df = pd.DataFrame(columns=["crosswalk_id", "scenario", "seed"])
    model_params = load_model_parameters(model_parameters_path)
    write_csv_bundle(
        avg_df,
        csv_layout.results / "simulation_results.csv",
        mirrors=[output_dir / "simulation_results.csv"],
    )
    simulation_summary = build_simulation_summary(avg_df, seed_df)
    delta_summary = build_baseline_vs_smart_summary(simulation_summary, _load_run_metrics_exact(output_dir))
    write_csv_bundle(
        simulation_summary,
        csv_layout.results / "simulation_summary.csv",
        mirrors=[output_dir / "simulation_summary.csv"],
    )
    write_csv_bundle(
        delta_summary,
        csv_layout.results / "baseline_vs_smart_summary.csv",
        mirrors=[output_dir / "baseline_vs_smart_summary.csv"],
    )
    write_methodology_report(output_dir, model_params, simulation_summary, delta_summary)
    write_figures(delta_summary, figures_dir)
    write_required_outputs(
        output_dir,
        figures_dir,
        Path(candidates_csv) if candidates_csv else None,
        Path(nets_dir) if nets_dir else None,
        seed_df,
        cli_simulation_mode=cli_simulation_mode,
    )
    return {
        "simulation_summary": simulation_summary,
        "baseline_vs_smart_summary": delta_summary,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output_dir", default="outputs")
    parser.add_argument("--figures_dir", default="figures")
    parser.add_argument("--candidates", default="outputs/candidates.csv")
    parser.add_argument("--nets_dir", default="sumo_nets")
    parser.add_argument("--model_parameters", default=None)
    parser.add_argument("--simulation_mode", default=None)
    args = parser.parse_args()
    generate_all_reports(
        args.output_dir,
        args.figures_dir,
        args.candidates,
        args.nets_dir,
        args.model_parameters,
        args.simulation_mode,
    )


if __name__ == "__main__":
    main()
