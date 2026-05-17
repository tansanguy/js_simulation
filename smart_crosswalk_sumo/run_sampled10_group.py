#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

import smart_crosswalk_sumo.run_phase6_recovery_smoke as smoke_runner
from smart_crosswalk_sumo.sampled10_demand_params import build_sampled10_demand_params
from smart_crosswalk_sumo.run_phase6_recovery_smoke import (
    FACTORY_TABLE,
    _build_candidate_table,
    _phase_aligned_depart_plan,
    _run_scenario,
)
from smart_crosswalk_sumo.output_schema import english_output_columns, write_csv_utf8_sig


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


def _smoke_to_seed_schema(df: pd.DataFrame, extension_sec: float, net_file: Path) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame()
    out = df.copy()
    out["scenario"] = out["scenario"].replace({"smart_selected": "smart"})
    out["average_pedestrian_wait_time"] = pd.to_numeric(out.get("ped_wait_time_mean"), errors="coerce")
    out["pedestrian_waiting_time_mean"] = out["average_pedestrian_wait_time"]
    out["pedestrian_waiting_time"] = out["average_pedestrian_wait_time"]
    out["pedestrian_waiting_time_max"] = pd.to_numeric(out.get("ped_wait_time_max"), errors="coerce")
    out["max_pedestrian_wait_time"] = out["pedestrian_waiting_time_max"]
    out["veh_avg_delay_sec"] = pd.to_numeric(out.get("veh_delay_mean"), errors="coerce")
    out["avg_vehicle_delay_sec"] = out["veh_avg_delay_sec"]
    out["surrounding_road_delay_sec"] = out["veh_avg_delay_sec"]
    out["vehicle_delay_cost"] = out["veh_avg_delay_sec"]
    out["avg_queue_length"] = np.nan
    out["max_queue_length"] = np.nan
    out["safety_risk_score"] = out["average_pedestrian_wait_time"].fillna(0.0)
    out["accident_expected_value"] = 0.0
    out["elderly_incomplete_crossings"] = 0.0
    out["pedestrian_green_extension_count"] = pd.to_numeric(out.get("extension_count"), errors="coerce").fillna(0)
    out["total_extension_sec"] = out["pedestrian_green_extension_count"] * float(extension_sec)
    out["generated_vehicle_count"] = pd.to_numeric(out.get("generated_vehicle_count"), errors="coerce")
    out["network_arrived_vehicles"] = pd.to_numeric(out.get("network_arrived_vehicles"), errors="coerce")
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
        "args": {
            "metric_sample_interval": float(args.metric_sample_interval),
            "vehicle_sample_interval": float(args.vehicle_sample_interval),
            "progress_interval": float(args.progress_interval),
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
            )
            timing[f"simulation_{scenario}_sec"] = float(time.perf_counter() - scenario_t0)
            scenario_frames.append(summary)

        seed_df = _smoke_to_seed_schema(pd.concat(scenario_frames, ignore_index=True), float(args.extension_increment), net_file)
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
    english_output_columns(seed_df).to_csv(out_dir / "simulation_results_seed.csv", index=False)
    english_output_columns(avg_df).to_csv(out_dir / "simulation_results.csv", index=False)
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
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args()
    sys.exit(run_sampled10_group(args))


if __name__ == "__main__":
    main()
