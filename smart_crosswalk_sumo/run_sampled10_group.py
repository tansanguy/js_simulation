#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
import time
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
from smart_crosswalk_sumo.output_schema import english_output_columns, write_csv_utf8_sig
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
        "args": {
            "metric_sample_interval": float(args.metric_sample_interval),
            "vehicle_sample_interval": float(args.vehicle_sample_interval),
            "progress_interval": float(args.progress_interval),
            "output_profile": str(args.output_profile),
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
            )
            timing[f"simulation_{scenario}_sec"] = float(time.perf_counter() - scenario_t0)
            scenario_frames.append(summary)

        seed_df = _smoke_to_seed_schema(
            pd.concat(scenario_frames, ignore_index=True),
            float(args.extension_increment),
            net_file,
            str(args.scenario_name),
        )
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
    seed_output = english_output_columns(seed_df)
    seed_output.to_csv(out_dir / "simulation_result.csv", index=False)
    seed_output.to_csv(out_dir / "simulation_results_seed.csv", index=False)
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
        **experiment_metadata,
        **counts,
    }
    _write_json(out_dir / "benchmark_timing.json", benchmark)
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
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args()
    sys.exit(run_sampled10_group(args))


if __name__ == "__main__":
    main()
