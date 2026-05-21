#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
import time
import traceback
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

import subprocess

import smart_crosswalk_sumo.run_phase6_recovery_smoke as smoke_runner
from smart_crosswalk_sumo.sampled10_demand_params import build_sampled10_demand_params
from smart_crosswalk_sumo.run_phase6_recovery_smoke import (
    FACTORY_TABLE,
    _build_candidate_table,
    _phase_aligned_depart_plan,
    _run_scenario,
)
from smart_crosswalk_sumo.output_schema import english_output_columns, ensure_simulation_result_columns, write_csv_utf8_sig
from smart_crosswalk_sumo.generate_demand import random_trips_script, _enforce_exact_vehicle_count
from smart_crosswalk_sumo.vehicle_demand_policy import resolve_vehicle_policy_summary
from smart_crosswalk_sumo.network_utils import sumo_env


def _experiment_metadata(metric_sample_interval_s: float, vehicle_sample_interval_s: float) -> dict[str, Any]:
    metrics_exact = metric_sample_interval_s == 0.0 and vehicle_sample_interval_s == 0.0
    return {
        "experiment_mode": "exact" if metrics_exact else "sampled",
        "metric_sample_interval_s": float(metric_sample_interval_s),
        "vehicle_sample_interval_s": float(vehicle_sample_interval_s),
        "metrics_exact": metrics_exact,
        "metrics_interpretation": (
            "exact values; use for exact PET/instantaneous queue claims"
            if metrics_exact
            else "sampled estimate; use for repeated paired comparisons, not exact PET/instantaneous queue claims"
        ),
    }


def _numeric_mean(values: list[Any]) -> float:
    numeric = pd.to_numeric(pd.Series(values), errors="coerce").dropna()
    return float(numeric.mean()) if not numeric.empty else float("nan")


def _smoke_to_seed_schema(df: pd.DataFrame, extension_sec: float, net_file: Path, scenario_name: str) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame()
    out = df.copy()
    out["scenario"] = out["scenario"].replace({"smart_selected": "smart"})
    if "scenario_name" not in out.columns:
        out["scenario_name"] = str(scenario_name)
    else:
        out["scenario_name"] = out["scenario_name"].fillna(str(scenario_name)).replace("", str(scenario_name))
    if "demand_profile" not in out.columns:
        out["demand_profile"] = str(scenario_name)
    else:
        out["demand_profile"] = out["demand_profile"].fillna(str(scenario_name)).replace("", str(scenario_name))
    out["average_pedestrian_wait_time"] = pd.to_numeric(out.get("ped_wait_time_mean"), errors="coerce")
    out["pedestrian_waiting_time_mean"] = out["average_pedestrian_wait_time"]
    out["pedestrian_waiting_time"] = out["average_pedestrian_wait_time"]
    out["pedestrian_waiting_time_max"] = pd.to_numeric(out.get("ped_wait_time_max"), errors="coerce")
    out["max_pedestrian_wait_time"] = out["pedestrian_waiting_time_max"]
    out["veh_avg_delay_sec"] = pd.to_numeric(out.get("veh_delay_mean"), errors="coerce")
    out["avg_vehicle_delay_sec"] = pd.to_numeric(out.get("network_avg_delay_sec"), errors="coerce")
    out["surrounding_road_delay_sec"] = pd.to_numeric(out.get("local_500m_avg_delay_sec"), errors="coerce")
    out["vehicle_delay_cost"] = pd.to_numeric(out.get("network_mean_time_loss"), errors="coerce")
    out["avg_queue_length"] = np.nan
    out["max_queue_length"] = np.nan
    out["safety_risk_score"] = pd.to_numeric(out.get("accident_risk_estimate"), errors="coerce").fillna(0.0)
    out["accident_expected_value"] = out["safety_risk_score"]
    out["elderly_incomplete_crossings"] = 0.0
    out["pedestrian_green_extension_count"] = pd.to_numeric(out.get("extension_count"), errors="coerce").fillna(0)
    out["total_extension_sec"] = out["pedestrian_green_extension_count"] * float(extension_sec)
    out["pedestrian_clearance_failure_count"] = pd.to_numeric(
        out.get("pedestrian_clearance_failure_count"), errors="coerce"
    )
    out["unfinished_crossing_count"] = pd.to_numeric(out.get("unfinished_crossing_count"), errors="coerce")
    if out["pedestrian_clearance_failure_count"].isna().all() and {
        "expected_ped_repeat_count",
        "pedestrian_crossing_count",
    }.issubset(out.columns):
        expected = pd.to_numeric(out.get("expected_ped_repeat_count"), errors="coerce")
        observed = pd.to_numeric(out.get("pedestrian_crossing_count"), errors="coerce")
        fallback = (expected - observed).clip(lower=0)
        out["pedestrian_clearance_failure_count"] = fallback
        out["unfinished_crossing_count"] = fallback
    out["generated_vehicle_count"] = pd.to_numeric(out.get("generated_vehicle_count"), errors="coerce")
    out["network_arrived_vehicles"] = pd.to_numeric(out.get("network_arrived_vehicles"), errors="coerce")
    out["network_departed_vehicles"] = pd.to_numeric(out.get("network_departed_vehicles"), errors="coerce")
    out["network_mean_travel_time"] = pd.to_numeric(out.get("network_mean_travel_time"), errors="coerce")
    out["network_mean_time_loss"] = pd.to_numeric(out.get("network_mean_time_loss"), errors="coerce")
    out["network_avg_delay_sec"] = pd.to_numeric(out.get("network_avg_delay_sec"), errors="coerce")
    out["network_vehicle_edge_coverage_ratio"] = pd.to_numeric(
        out.get("network_vehicle_edge_coverage_ratio"), errors="coerce"
    )
    out["network_teleport_count"] = pd.to_numeric(out.get("network_teleport_count"), errors="coerce")
    out["network_collision_count"] = pd.to_numeric(out.get("network_collision_count"), errors="coerce")
    out["vehicle_route_count"] = pd.to_numeric(out.get("vehicle_route_count"), errors="coerce")
    out["unique_vehicle_route_count"] = pd.to_numeric(out.get("unique_vehicle_route_count"), errors="coerce")
    out["duplicate_factor"] = pd.to_numeric(out.get("duplicate_factor"), errors="coerce")
    out["unique_vehicle_route_ratio"] = pd.to_numeric(out.get("unique_vehicle_route_ratio"), errors="coerce")
    out["used_vehicle_edges"] = pd.to_numeric(out.get("used_vehicle_edges"), errors="coerce")
    out["local_500m_vehicle_count"] = pd.to_numeric(out.get("local_500m_vehicle_count"), errors="coerce")
    out["local_500m_mean_speed"] = pd.to_numeric(out.get("local_500m_mean_speed"), errors="coerce")
    out["local_500m_mean_time_loss"] = pd.to_numeric(out.get("local_500m_mean_time_loss"), errors="coerce")
    out["local_500m_avg_delay_sec"] = pd.to_numeric(out.get("local_500m_avg_delay_sec"), errors="coerce")
    out["local_500m_queue_proxy"] = pd.to_numeric(out.get("local_500m_queue_proxy"), errors="coerce")
    out["local_500m_stop_count"] = pd.to_numeric(out.get("local_500m_stop_count"), errors="coerce")
    out["pet_event_count"] = pd.to_numeric(out.get("pet_event_count"), errors="coerce")
    out["very_risky_crossing_count"] = pd.to_numeric(out.get("very_risky_crossing_count"), errors="coerce")
    out["risky_crossing_count"] = pd.to_numeric(out.get("risky_crossing_count"), errors="coerce")
    out["safe_crossing_count"] = pd.to_numeric(out.get("safe_crossing_count"), errors="coerce")
    out["low_pet_event_count"] = pd.to_numeric(out.get("low_pet_event_count"), errors="coerce")
    out["low_pet_per_100_crossings"] = pd.to_numeric(out.get("low_pet_per_100_crossings"), errors="coerce")
    out["low_pet_per_100_conflict_candidates"] = pd.to_numeric(
        out.get("low_pet_per_100_conflict_candidates"), errors="coerce"
    )
    out["pet_min"] = pd.to_numeric(out.get("pet_min"), errors="coerce")
    out["pet_p10"] = pd.to_numeric(out.get("pet_p10"), errors="coerce")
    out["pet_mean"] = pd.to_numeric(out.get("pet_mean"), errors="coerce")
    out["total_vehicle_arrivals"] = pd.to_numeric(out.get("total_vehicle_arrivals"), errors="coerce")
    route_files = out.get("generated_vehicle_route_file")
    if route_files is None:
        out["generated_vehicle_route_file"] = ""
    else:
        out["generated_vehicle_route_file"] = route_files.fillna("")
    out["surrounding_lane_count"] = pd.to_numeric(out.get("surrounding_lane_count"), errors="coerce")
    out["batch_network_file"] = str(net_file)
    return out


_TOP7_SUMMARY_COLS = [
    "crosswalk_id",
    "scenario",
    "seed",
    "extension_count",
    "total_extension_sec",
    "ped_wait_time_mean",
    "pedestrian_crossing_count",
    "pedestrian_clearance_failure_count",
    "network_avg_delay_sec",
    "network_mean_travel_time",
    "network_arrived_vehicles",
    "local_500m_avg_delay_sec",
    "pet_event_count",
    "accident_risk_estimate",
]


def _write_top7_summary(df: pd.DataFrame, out_dir: "Path") -> None:
    if df.empty:
        return
    cols = [c for c in _TOP7_SUMMARY_COLS if c in df.columns]
    summary = df[cols].copy()

    # baseline vs smart delta 계산
    base = summary[summary["scenario"] == "baseline"].set_index("crosswalk_id")
    smart = summary[summary["scenario"] == "smart"].set_index("crosswalk_id")
    delta_rows = []
    for cid in smart.index:
        row: dict[str, Any] = {"crosswalk_id": cid}
        s = smart.loc[cid]
        b = base.loc[cid] if cid in base.index else None
        row["extension_count"] = _safe_val(s, "extension_count")
        row["total_extension_sec"] = _safe_val(s, "total_extension_sec")
        for col in ("ped_wait_time_mean", "network_avg_delay_sec", "network_mean_travel_time", "local_500m_avg_delay_sec"):
            sv = _safe_val(s, col)
            bv = _safe_val(b, col) if b is not None else float("nan")
            row[f"smart_{col}"] = sv
            row[f"baseline_{col}"] = bv
            try:
                row[f"delta_{col}"] = round(float(sv) - float(bv), 4) if sv == sv and bv == bv else float("nan")
            except Exception:
                row[f"delta_{col}"] = float("nan")
        row["network_arrived_vehicles_smart"] = _safe_val(s, "network_arrived_vehicles")
        row["network_arrived_vehicles_baseline"] = _safe_val(b, "network_arrived_vehicles") if b is not None else float("nan")
        row["pet_event_count"] = _safe_val(s, "pet_event_count")
        row["accident_risk_estimate"] = _safe_val(s, "accident_risk_estimate")
        delta_rows.append(row)

    write_csv_utf8_sig(summary, out_dir / "top7_summary.csv")
    if delta_rows:
        write_csv_utf8_sig(pd.DataFrame(delta_rows), out_dir / "top7_delta_summary.csv")


def _xml_float(value: Any) -> float:
    try:
        return float(value)
    except Exception:
        return float("nan")


def _flatten_statistics_xml(path: Path, scenario: str) -> dict[str, Any]:
    row: dict[str, Any] = {
        "scenario": scenario,
        "source_statistics_xml": str(path),
        "statistics_xml_exists": path.exists(),
    }
    if not path.exists():
        return row
    try:
        root = ET.parse(path).getroot()
    except Exception as exc:
        row["statistics_parse_error"] = f"{exc.__class__.__name__}: {exc}"
        return row
    for child in list(root):
        tag = str(child.tag).split("}", 1)[-1]
        for key, value in child.attrib.items():
            column = f"{tag}__{key}"
            number = _xml_float(value)
            row[column] = number if number == number else value
    return row


def _parse_tripinfo_flow_xml(path: Path, scenario: str) -> dict[str, Any]:
    row: dict[str, Any] = {
        "scenario": scenario,
        "source_tripinfo_xml": str(path),
        "tripinfo_xml_exists": path.exists(),
    }
    if not path.exists():
        return row
    try:
        root = ET.parse(path).getroot()
    except Exception as exc:
        row["tripinfo_parse_error"] = f"{exc.__class__.__name__}: {exc}"
        return row

    numeric_fields = ("depart", "arrival", "duration", "routeLength", "waitingTime", "timeLoss", "departDelay")
    values: dict[str, list[float]] = {field: [] for field in numeric_fields}
    type_counts: dict[str, int] = {}
    trip_count = 0
    for tripinfo in root.findall(".//tripinfo"):
        trip_count += 1
        vtype = str(tripinfo.attrib.get("vType", "") or "unknown")
        type_counts[vtype] = type_counts.get(vtype, 0) + 1
        for field in numeric_fields:
            value = _xml_float(tripinfo.attrib.get(field))
            if value == value:
                values[field].append(value)

    row["tripinfo__count"] = int(trip_count)
    for field, nums in values.items():
        if nums:
            row[f"tripinfo__{field}_mean"] = float(np.mean(nums))
            row[f"tripinfo__{field}_sum"] = float(np.sum(nums))
            row[f"tripinfo__{field}_min"] = float(np.min(nums))
            row[f"tripinfo__{field}_max"] = float(np.max(nums))
        else:
            row[f"tripinfo__{field}_mean"] = float("nan")
            row[f"tripinfo__{field}_sum"] = 0.0
            row[f"tripinfo__{field}_min"] = float("nan")
            row[f"tripinfo__{field}_max"] = float("nan")
    for vtype, count in sorted(type_counts.items()):
        row[f"tripinfo__vtype_{vtype}_count"] = int(count)
    return row


def _with_delta_row(rows: list[dict[str, Any]]) -> pd.DataFrame:
    df = pd.DataFrame(rows)
    if df.empty or "scenario" not in df.columns:
        return df
    baseline = df[df["scenario"] == "baseline"]
    smart = df[df["scenario"] == "smart"]
    if baseline.empty or smart.empty:
        return df
    b = baseline.iloc[0]
    s = smart.iloc[0]
    delta: dict[str, Any] = {"scenario": "delta(smart-baseline)"}
    for column in df.columns:
        if column == "scenario":
            continue
        sv_raw = s.get(column)
        bv_raw = b.get(column)
        if isinstance(sv_raw, (bool, np.bool_)) or isinstance(bv_raw, (bool, np.bool_)):
            delta[column] = ""
            continue
        sv = pd.to_numeric(pd.Series([sv_raw]), errors="coerce").iloc[0]
        bv = pd.to_numeric(pd.Series([bv_raw]), errors="coerce").iloc[0]
        if pd.notna(sv) and pd.notna(bv):
            delta[column] = float(sv - bv)
        else:
            delta[column] = ""
    return pd.concat([df, pd.DataFrame([delta])], ignore_index=True)


def _write_global_traffic_flow_csvs(out_dir: Path) -> dict[str, str]:
    stats_rows = [
        _flatten_statistics_xml(out_dir / f"phase6_smoke_{scenario}_statistics.xml", scenario)
        for scenario in ("baseline", "smart")
    ]
    trip_rows = [
        _parse_tripinfo_flow_xml(out_dir / f"phase6_smoke_{scenario}_tripinfo.xml", scenario)
        for scenario in ("baseline", "smart")
    ]

    stats_df = _with_delta_row(stats_rows)
    trip_df = _with_delta_row(trip_rows)
    merged_rows = []
    for scenario in ("baseline", "smart"):
        stats = next((row for row in stats_rows if row.get("scenario") == scenario), {"scenario": scenario})
        trips = next((row for row in trip_rows if row.get("scenario") == scenario), {"scenario": scenario})
        merged = dict(stats)
        for key, value in trips.items():
            if key == "scenario":
                continue
            merged[key] = value
        merged_rows.append(merged)
    flow_df = _with_delta_row(merged_rows)

    stats_path = out_dir / "global_traffic_comparison.csv"
    trip_path = out_dir / "global_tripinfo_flow.csv"
    flow_path = out_dir / "global_traffic_flow.csv"
    write_csv_utf8_sig(stats_df, stats_path)
    write_csv_utf8_sig(trip_df, trip_path)
    write_csv_utf8_sig(flow_df, flow_path)
    return {
        "global_traffic_comparison_csv": str(stats_path),
        "global_tripinfo_flow_csv": str(trip_path),
        "global_traffic_flow_csv": str(flow_path),
    }


def _safe_val(row: "Any", col: str) -> "Any":
    if row is None:
        return float("nan")
    try:
        v = row[col] if hasattr(row, "__getitem__") else getattr(row, col, float("nan"))
        return v if pd.notna(v) else float("nan")
    except Exception:
        return float("nan")


def _average_results(seed_df: pd.DataFrame) -> pd.DataFrame:
    if seed_df.empty:
        return pd.DataFrame()
    avg_rows: list[dict[str, Any]] = []
    for (crosswalk_id, scenario), sub in seed_df.groupby(["crosswalk_id", "scenario"], dropna=False):
        records = sub.to_dict(orient="records")
        excluded = {"crosswalk_id", "scenario", "seed", "run_name", "output_dir", "run_start_time", "run_end_time"}
        numeric_keys = [
            key
            for key in sub.columns
            if key not in excluded and pd.api.types.is_numeric_dtype(pd.to_numeric(sub[key], errors="coerce"))
        ]
        first = records[0]
        row = {
            "crosswalk_id": crosswalk_id,
            "scenario": scenario,
            "seed_count": int(sub["seed"].nunique()) if "seed" in sub.columns else len(sub),
        }
        for key in ("admin_dong", "dong_name", "scenario_name", "demand_profile", "batch_network_file"):
            if key in first:
                row[key] = first.get(key)
        for key in numeric_keys:
            row[key] = _numeric_mean([record.get(key) for record in records])
        avg_rows.append(row)
    return pd.DataFrame(avg_rows)


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _ensure_global_vehicle_routes(
    net_file: Path,
    sim_duration: int,
    seed: int,
    out_dir: Path,
    scenario_name: str,
) -> "Path | None":
    global_dir = out_dir / "global_vehicle_routes"
    global_dir.mkdir(parents=True, exist_ok=True)
    route_file = global_dir / "demand_vehicle.rou.xml"
    trip_file = global_dir / "demand_vehicle.trips.xml"
    try:
        policy = resolve_vehicle_policy_summary(scenario_name)
        veh_per_hour = float(policy["total_vehicle_flow_vph"])
        total_600s = int(policy["total_vehicle_count_600s"])
        expected_count = int(round(total_600s * sim_duration / 600.0))
        period = max(0.1, 3600.0 / max(veh_per_hour, 1.0))
    except Exception as exc:
        print(f"[sampled10_group][WARN] vehicle policy resolution failed: {exc}", flush=True)
        return None
    if route_file.exists():
        try:
            root = ET.parse(route_file).getroot()
            actual_count = sum(1 for _ in root.iter("vehicle"))
            threshold = int(expected_count * 0.9)
            if actual_count >= threshold:
                return route_file
            print(
                f"[sampled10_group][WARN] route file under-populated"
                f" (actual={actual_count} < expected*0.9={threshold}, expected={expected_count}), regenerating: {route_file}",
                flush=True,
            )
        except ET.ParseError as exc:
            print(f"[sampled10_group][WARN] route file corrupt, regenerating: {route_file} ({exc})", flush=True)
        route_file.unlink(missing_ok=True)
        trip_file.unlink(missing_ok=True)
    try:
        rt_script = random_trips_script()
        log_file = global_dir / "randomtrips.log"
        with log_file.open("w", encoding="utf-8") as log_handle:
            subprocess.run(
                [
                    sys.executable, rt_script,
                    "-n", str(net_file),
                    "-o", str(trip_file),
                    "-r", str(route_file),
                    "--period", str(period),
                    "--seed", str(seed),
                    "--begin", "0",
                    "--end", str(sim_duration),
                    "--vehicle-class", "passenger",
                    "--validate",
                ],
                check=True,
                env=sumo_env(),
                stdout=log_handle,
                stderr=subprocess.STDOUT,
            )
        if expected_count > 0:
            _enforce_exact_vehicle_count(route_file, trip_file, expected_count, sim_duration)
        print(
            f"[sampled10_group] global vehicle routes generated: {route_file} ({expected_count} vehicles, seed={seed})",
            flush=True,
        )
        return route_file
    except Exception as exc:
        print(f"[sampled10_group][WARN] global vehicle route generation failed: {exc}", flush=True)
        return None


def _result_counts(seed_df: pd.DataFrame, failures: list[dict[str, Any]]) -> dict[str, Any]:
    result_rows = int(len(seed_df))
    baseline_result_rows = int((seed_df["scenario"] == "baseline").sum()) if not seed_df.empty and "scenario" in seed_df.columns else 0
    smart_result_rows = int((seed_df["scenario"] == "smart").sum()) if not seed_df.empty and "scenario" in seed_df.columns else 0
    failed_cases_count = int(len(failures))
    run_success = failed_cases_count == 0 and baseline_result_rows >= 1 and smart_result_rows >= 1
    reasons: list[str] = []
    if failed_cases_count > 0:
        reasons.append(f"failed_cases_count={failed_cases_count}")
    if result_rows == 0:
        reasons.append("result_rows=0")
    if baseline_result_rows < 1:
        reasons.append(f"baseline_result_rows={baseline_result_rows}")
    if smart_result_rows < 1:
        reasons.append(f"smart_result_rows={smart_result_rows}")
    return {
        "run_success": run_success,
        "failure_reason": "" if run_success else ";".join(reasons),
        "failed_cases_count": failed_cases_count,
        "result_rows": result_rows,
        "baseline_result_rows": baseline_result_rows,
        "smart_result_rows": smart_result_rows,
        "partial": not run_success,
    }


def _enable_ssm_from_args(args: argparse.Namespace) -> bool:
    return bool(getattr(args, "enable_ssm", False)) and not bool(getattr(args, "disable_ssm", False))


def _last_failure_error(failures: list[dict[str, Any]]) -> str:
    if not failures:
        return ""
    return str(failures[-1].get("error") or "")


def run_sampled10_group(args: argparse.Namespace) -> int:
    t0 = time.perf_counter()
    candidate_csv = Path(args.candidate_csv).expanduser().resolve()
    net_file = Path(args.net_file).expanduser().resolve()
    out_dir = Path(args.output_dir).expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    smoke_runner.RESULT_DIR = out_dir / "legacy_smoke_compare"
    experiment_metadata = _experiment_metadata(args.metric_sample_interval, args.vehicle_sample_interval)
    failures: list[dict[str, Any]] = []
    timing: dict[str, float | None] = {"simulation_baseline_sec": 0.0, "simulation_smart_sec": 0.0}
    enable_ssm = _enable_ssm_from_args(args)

    run_metadata = {
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "runner": "run_sampled10_group",
        "execution_unit": "paired_group_net",
        "manifest_row_role": args.manifest_row_role,
        "manifest_crosswalk_id": args.manifest_crosswalk_id,
        "scenario_name": str(args.scenario_name),
        "demand_profile": str(args.scenario_name),
        "candidate_csv": str(candidate_csv),
        "net_file": str(net_file),
        "output_dir": str(out_dir),
        "seed": int(args.seed),
        "sim_duration": int(args.sim_duration),
        "warmup": int(args.warmup),
        "traci_step_length": float(args.traci_step_length),
        "traffic_measure_radius_m": float(args.traffic_measure_radius_m),
        "extension_increment": float(args.extension_increment),
        "max_extensions": int(args.max_extensions),
        "ped_repeat_count": int(args.ped_repeat_count),
        "ped_repeat_spacing_sec": float(args.ped_repeat_spacing_sec),
        "phase_aligned_ped_depart": bool(args.phase_aligned_ped_depart),
        "include_vehicles": bool(args.include_vehicles),
        "output_profile": str(args.output_profile),
        "enable_ssm": enable_ssm,
        "args": {
            "metric_sample_interval": float(args.metric_sample_interval),
            "vehicle_sample_interval": float(args.vehicle_sample_interval),
            "progress_interval": float(args.progress_interval),
            "output_profile": str(args.output_profile),
            "disable_ssm": bool(getattr(args, "disable_ssm", False)),
            "enable_ssm": bool(getattr(args, "enable_ssm", False)),
        },
        **experiment_metadata,
    }
    _write_json(out_dir / "run_metadata.json", run_metadata)

    seed_df = pd.DataFrame()
    try:
        if not candidate_csv.exists():
            raise FileNotFoundError(candidate_csv)
        if not net_file.exists():
            raise FileNotFoundError(net_file)

        factory_table = Path(args.factory_table).expanduser().resolve() if args.factory_table else None
        crossing_inventory = Path(args.crossing_inventory).expanduser().resolve() if args.crossing_inventory else None
        candidate_df, diagnostics_df = _build_candidate_table(candidate_csv, net_file, crossing_inventory, out_dir, factory_table)
        if args.ped_repeat_count is not None:
            candidate_df["ped_repeat_count"] = int(args.ped_repeat_count)
        if args.ped_repeat_spacing_sec is not None:
            candidate_df["ped_repeat_spacing_sec"] = float(args.ped_repeat_spacing_sec)
        if args.phase_aligned_ped_depart:
            depart_plan_df = _phase_aligned_depart_plan(
                net_file,
                candidate_df,
                ped_repeat_count=args.ped_repeat_count,
                ped_repeat_spacing_sec=args.ped_repeat_spacing_sec,
                ped_depart_offset_sec=args.ped_depart_offset_sec,
            )
            depart_plan_df.to_csv(out_dir / "pedestrian_depart_plan.csv", index=False)
            if not depart_plan_df.empty:
                candidate_df = candidate_df.merge(
                    depart_plan_df[["crosswalk_id", "depart_time"]].rename(columns={"depart_time": "ped_depart_time"}),
                    on="crosswalk_id",
                    how="left",
                )
        elif args.ped_depart_offset_sec is not None:
            candidate_df["ped_depart_offset_sec"] = float(args.ped_depart_offset_sec)

        candidate_df.to_csv(out_dir / "sampled10_candidates_resolved.csv", index=False)
        diagnostics_df.to_csv(out_dir / "candidate_table_source_diagnostics.csv", index=False)

        global_veh_file = None
        if args.include_vehicles:
            global_veh_file = _ensure_global_vehicle_routes(
                net_file=net_file,
                sim_duration=int(args.sim_duration),
                seed=int(args.seed),
                out_dir=out_dir,
                scenario_name=str(args.scenario_name),
            )

        scenario_frames: list[pd.DataFrame] = []
        for scenario, extension_sec in (("baseline", 0.0), ("smart", float(args.extension_increment))):
            scenario_t0 = time.perf_counter()
            try:
                summary = _run_scenario(
                    candidate_df,
                    net_file,
                    scenario,
                    int(args.seed),
                    int(args.sim_duration),
                    int(args.warmup),
                    float(args.traci_step_length),
                    out_dir,
                    bool(args.include_vehicles),
                    float(extension_sec),
                    global_vehicle_file=global_veh_file,
                    output_profile=str(args.output_profile),
                    enable_ssm=enable_ssm,
                )
                timing[f"simulation_{scenario}_sec"] = float(time.perf_counter() - scenario_t0)
                scenario_frames.append(summary)
            except Exception as exc:
                timing[f"simulation_{scenario}_sec"] = float(time.perf_counter() - scenario_t0)
                failures.append(
                    {
                        "crosswalk_id": str(args.manifest_crosswalk_id or ""),
                        "seed": int(args.seed),
                        "step": f"run_scenario:{scenario}",
                        "error": f"{exc.__class__.__name__}: {exc}",
                        "traceback": traceback.format_exc(),
                    }
                )
                print(
                    f"[sampled10_group][ERROR] scenario={scenario} crosswalk={args.manifest_crosswalk_id} failed: {exc}",
                    flush=True,
                )
                traceback.print_exc()
                continue

        if scenario_frames:
            seed_df = _smoke_to_seed_schema(
                pd.concat(scenario_frames, ignore_index=True),
                float(args.extension_increment),
                net_file,
                str(args.scenario_name),
            )
        else:
            seed_df = pd.DataFrame()
    except Exception as exc:
        failures.append(
            {
                "crosswalk_id": str(args.manifest_crosswalk_id or ""),
                "seed": int(args.seed),
                "step": "run_sampled10_group",
                "error": f"{exc.__class__.__name__}: {exc}",
            }
        )

    avg_df = _average_results(seed_df)
    seed_output = ensure_simulation_result_columns(english_output_columns(seed_df))
    write_csv_utf8_sig(seed_output, out_dir / "simulation_result.csv")
    write_csv_utf8_sig(seed_output, out_dir / "simulation_results_seed.csv")
    _write_top7_summary(seed_output, out_dir)
    global_traffic_paths: dict[str, str] = {}
    try:
        global_traffic_paths = _write_global_traffic_flow_csvs(out_dir)
    except Exception as exc:
        failures.append(
            {
                "crosswalk_id": str(args.manifest_crosswalk_id or ""),
                "seed": int(args.seed),
                "step": "write_global_traffic_flow_csvs",
                "error": f"{exc.__class__.__name__}: {exc}",
            }
        )
    manifest_columns = [
        "seed",
        "scenario",
        "sim_duration",
        "scenario_name",
        "demand_profile",
        "batch_network_file",
        "net_sha256",
        "generated_vehicle_route_file",
        "vehicle_route_sha256",
        "pedestrian_route_file",
        "pedestrian_route_sha256",
    ]
    available_manifest_columns = [column for column in manifest_columns if column in seed_output.columns]
    if available_manifest_columns:
        seed_output[available_manifest_columns].drop_duplicates().to_csv(
            out_dir / "route_demand_manifest.csv",
            index=False,
        )
    if not seed_df.empty:
        try:
            build_sampled10_demand_params(out_dir)
        except Exception as exc:
            failures.append(
                {
                    "crosswalk_id": str(args.manifest_crosswalk_id or ""),
                    "seed": int(args.seed),
                    "step": "build_sampled10_demand_params",
                    "error": f"{exc.__class__.__name__}: {exc}",
                }
            )
    failed_cases_path = out_dir / "failed_cases.csv"
    if failures:
        english_output_columns(pd.DataFrame(failures)).to_csv(failed_cases_path, index=False)
    else:
        english_output_columns(pd.DataFrame(columns=["crosswalk_id", "seed", "step", "error"])).to_csv(
            failed_cases_path,
            index=False,
        )

    counts = _result_counts(seed_df, failures)
    benchmark = {
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "runner": "run_sampled10_group",
        "execution_unit": "paired_group_net",
        "manifest_row_role": args.manifest_row_role,
        "manifest_crosswalk_id": args.manifest_crosswalk_id,
        "network_input_mode": "group_net_file",
        "network_input_path": str(net_file),
        "simulation_baseline_sec": timing.get("simulation_baseline_sec"),
        "simulation_smart_sec": timing.get("simulation_smart_sec"),
        "post_validation_sec": None,
        "total_sec": float(time.perf_counter() - t0),
        "interrupted": False,
        "completed_scenarios": ["simulation"] if counts["run_success"] else [],
        "current_scenario": "",
        "last_error": _last_failure_error(failures),
        **global_traffic_paths,
        **experiment_metadata,
        **counts,
    }
    _write_json(out_dir / "benchmark_timing.json", benchmark)
    print(
        "[sampled10_group][summary]",
        f"seed={int(args.seed)}",
        f"role={args.manifest_row_role}",
        f"crosswalk={args.manifest_crosswalk_id}",
        f"run_success={counts['run_success']}",
        f"result_rows={counts['result_rows']}",
        f"failed_cases_count={counts['failed_cases_count']}",
        f"failure_reason={benchmark['failure_reason'] or 'ok'}",
        f"last_error={benchmark['last_error'] or ''}",
        flush=True,
    )
    return 0 if counts["run_success"] else 1


def main() -> None:
    parser = argparse.ArgumentParser(description="Sampled10 paired runner for Phase 6 group-net command roots.")
    parser.add_argument("--candidate-csv", required=True)
    parser.add_argument("--net-file", required=True)
    parser.add_argument("--factory-table", default=str(FACTORY_TABLE))
    parser.add_argument("--crossing-inventory", default=None)
    parser.add_argument("--seed", type=int, required=True)
    parser.add_argument("--sim-duration", type=int, default=600)
    parser.add_argument("--warmup", type=int, default=0)
    parser.add_argument("--traci_step_length", type=float, default=0.1)
    parser.add_argument("--traffic_measure_radius_m", type=float, default=500.0)
    parser.add_argument("--extension_increment", type=float, default=5.0)
    parser.add_argument("--max_extensions", type=int, default=1)
    parser.add_argument("--metric-sample-interval", type=float, default=10.0)
    parser.add_argument("--vehicle-sample-interval", type=float, default=10.0)
    parser.add_argument("--progress-interval", type=float, default=60.0)
    parser.add_argument("--ped-repeat-count", type=int, default=5)
    parser.add_argument("--ped-repeat-spacing-sec", type=float, default=2.0)
    parser.add_argument("--ped-depart-offset-sec", type=float, default=None)
    parser.add_argument("--phase-aligned-ped-depart", action="store_true")
    parser.add_argument("--include-vehicles", action="store_true")
    parser.add_argument("--manifest-row-role", choices=["baseline_placeholder", "smart_candidate"], required=True)
    parser.add_argument("--manifest-crosswalk-id", default="")
    parser.add_argument("--scenario-name", default="main_realistic_stress")
    parser.add_argument("--output-profile", choices=["full", "light"], default="full")
    parser.add_argument("--disable-ssm", action="store_true")
    parser.add_argument("--enable-ssm", action="store_true")
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args()
    sys.exit(run_sampled10_group(args))


if __name__ == "__main__":
    main()
