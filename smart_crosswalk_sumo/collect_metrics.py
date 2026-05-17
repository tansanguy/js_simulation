from __future__ import annotations

import argparse
import json
import shutil
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

try:
    from .demand_scenarios import (
        DEFAULT_DEMAND_SCENARIO_NAME,
        DEFAULT_PEDESTRIAN_SCALE,
        DEFAULT_VEHICLE_FLOW_SCALE,
    )
    from .network_utils import load_metadata
    from .run_simulations import (
        compute_signal_timing,
        generate_incident_schedule,
        run_simulation,
        serialize_incident_event,
    )
except ImportError:
    from demand_scenarios import (
        DEFAULT_DEMAND_SCENARIO_NAME,
        DEFAULT_PEDESTRIAN_SCALE,
        DEFAULT_VEHICLE_FLOW_SCALE,
    )
    from network_utils import load_metadata
    from run_simulations import (
        compute_signal_timing,
        generate_incident_schedule,
        run_simulation,
        serialize_incident_event,
    )

try:
    from .model_config import get_parameter_value, load_model_parameters
    from .csv_outputs import ensure_csv_output_layout, write_csv_bundle
    from .output_schema import english_output_columns
    from .sensitivity import (
        apply_parameter_value_overrides,
        load_sensitivity_scenarios,
        resolve_sensitivity_case,
    )
except ImportError:
    from model_config import get_parameter_value, load_model_parameters
    from csv_outputs import ensure_csv_output_layout, write_csv_bundle
    from output_schema import english_output_columns
    from sensitivity import apply_parameter_value_overrides, load_sensitivity_scenarios, resolve_sensitivity_case


def mean_or_nan(values: list[float]) -> float:
    arr = np.asarray(values, dtype=float)
    if arr.size == 0 or np.all(np.isnan(arr)):
        return float("nan")
    return float(np.nanmean(arr))


def _load_demand_lookup(output_dir: Path) -> dict[tuple[str, str], dict[str, Any]]:
    demand_csv = output_dir / "demand_params.csv"
    if not demand_csv.exists():
        return {}
    df = pd.read_csv(demand_csv)
    lookup: dict[tuple[str, str], dict[str, Any]] = {}
    for row in df.to_dict(orient="records"):
        key = (str(row.get("crosswalk_id", "")), str(row.get("seed", "")))
        lookup[key] = row
    return lookup


def _experiment_metadata(metric_sample_interval_s: float, vehicle_sample_interval_s: float) -> dict[str, object]:
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


def collect_all(
    candidates_csv: str | Path,
    output_dir: str | Path = "outputs",
    nets_dir: str | Path = "sumo_nets",
    sim_duration: int = 1800,
    warmup: int = 300,
    seeds: tuple[int, ...] = (42, 43, 44),
    traci_step_length: float = 0.1,
    traffic_measure_radius_m: float = 500.0,
    extension_increment: float | None = None,
    max_extensions: int | None = None,
    vehicle_arrival_rate_per_hour: float | None = None,
    saturation_flow_rate_per_hour: float = 1900.0,
    vehicle_arrival_model: str = "poisson",
    disruption_scenario: str = "best_case",
    enable_random_disruptions: bool = False,
    bus_stop_rate_per_hour: float = 0.0,
    illegal_parking_rate_per_hour: float = 0.0,
    minor_incident_rate_per_hour: float = 0.0,
    accident_rate_per_hour: float = 0.0,
    model_parameters_path: str | Path | None = None,
    export_fcd: bool = False,
    vehicle_only: bool = False,
    sensitivity_config: dict[str, Any] | None = None,
    metric_sample_interval_s: float = 0.0,
    vehicle_sample_interval_s: float = 0.0,
    progress_interval_s: float = 0.0,
) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, float | str | None]]:
    t0 = time.perf_counter()
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    csv_layout = ensure_csv_output_layout(output_dir)
    demand_lookup = _load_demand_lookup(output_dir)
    candidates = pd.read_csv(candidates_csv)
    seed_rows = []
    avg_rows = []
    failures = []
    extension_rows: list[dict[str, Any]] = []
    incident_event_rows: list[dict[str, Any]] = []
    incident_impact_rows: list[dict[str, Any]] = []
    boundary_warning_rows: list[dict[str, Any]] = []
    timing: dict[str, float | str | None] = {
        "simulation_baseline_sec": 0.0,
        "simulation_smart_sec": 0.0,
    }

    model_params = apply_parameter_value_overrides(
        load_model_parameters(model_parameters_path),
        (sensitivity_config or {}).get("parameter_overrides"),
    )
    if saturation_flow_rate_per_hour is None:
        saturation_flow_rate_per_hour = float(
            get_parameter_value(
                model_params,
                "saturation_flow_rate",
                get_parameter_value(model_params, "saturation_flow_rate_pcphpl", 1900.0),
            )
        )

    for _, row in candidates.iterrows():
        cw_id = row["crosswalk_id"]
        cw_dir = Path(nets_dir) / f"cw_{cw_id}"
        net_file = cw_dir / "network.net.xml"
        metadata_file = cw_dir / "metadata.json"
        try:
            metadata = load_metadata(metadata_file)
        except Exception as exc:
            failures.append({"crosswalk_id": cw_id, "step": "collect_load_metadata", "error": str(exc)})
            continue

        signal_timing = compute_signal_timing(row)
        cw_params = {"crossing_length_m": float(row["crossing_length_m"])}
        lane_count_for_sim = float(metadata.get("lane_count_csv", row["lane_count"]))
        default_vehicle_arrival_rate = float(row["estimated_aadt"] / 24.0 / max(lane_count_for_sim, 1.0))
        queue_vehicle_arrival_rate = (
            float(vehicle_arrival_rate_per_hour)
            if vehicle_arrival_rate_per_hour is not None
            else default_vehicle_arrival_rate
        )
        queue_vehicle_arrival_rate *= float(
            (sensitivity_config or {}).get("vehicle_volume_multiplier", 1.0) or 1.0
        )
        queue_vehicle_num_lanes = max(1, int(lane_count_for_sim))

        for seed in seeds:
            # Baseline/Smart 공통 incident schedule
            try:
                schedule_events = generate_incident_schedule(
                    disruption_scenario,
                    sim_duration,
                    seed,
                    metadata,
                    metadata.get("approach_lanes", []),
                    model_params,
                    enable_random_disruptions,
                    bus_stop_rate_per_hour,
                    illegal_parking_rate_per_hour,
                    minor_incident_rate_per_hour,
                    accident_rate_per_hour,
                )
                for event in schedule_events:
                    incident_event_rows.append(serialize_incident_event(event, str(cw_id), int(seed), "shared"))
                schedule_payload = [
                    {
                        "incident_id": event.incident_id,
                        "event_type": event.event_type,
                        "start_time": event.start_time,
                        "end_time": event.end_time,
                        "affected_edge_ids": list(event.affected_edge_ids),
                        "affected_lane_ids": list(event.affected_lane_ids),
                        "severity": event.severity,
                        "capacity_multiplier": event.capacity_multiplier,
                        "speed_multiplier": event.speed_multiplier,
                        "blocked_lanes_count": event.blocked_lanes_count,
                        "allow_rerouting": event.allow_rerouting,
                    }
                    for event in schedule_events
                ]
            except Exception as exc:
                failures.append(
                    {
                        "crosswalk_id": cw_id,
                        "scenario": "shared",
                        "seed": seed,
                        "step": "incident_schedule",
                        "error": str(exc),
                    }
                )
                schedule_payload = []

            for scenario in ("baseline", "smart"):
                route_file = cw_dir / f"routes_seed{seed}.rou.xml"
                ped_file = cw_dir / f"peds_seed{seed}.rou.xml"
                sumocfg = cw_dir / f"{scenario}_seed{seed}.sumocfg"
                try:
                    scenario_t0 = time.perf_counter()
                    metrics, extension_events, _, incident_impacts, export_paths = run_simulation(
                        net_file,
                        route_file,
                        ped_file,
                        sumocfg,
                        scenario,
                        signal_timing,
                        cw_params,
                        metadata,
                        sim_duration,
                        warmup,
                        seed,
                        traci_step_length,
                        traffic_measure_radius_m,
                        extension_increment,
                        max_extensions,
                        queue_vehicle_arrival_rate,
                        saturation_flow_rate_per_hour,
                        queue_vehicle_num_lanes,
                        vehicle_arrival_model,
                        disruption_scenario,
                        enable_random_disruptions,
                        bus_stop_rate_per_hour,
                        illegal_parking_rate_per_hour,
                        minor_incident_rate_per_hour,
                        accident_rate_per_hour,
                        schedule_payload,
                        str(cw_id),
                        model_parameters_path,
                        export_fcd,
                        output_dir,
                        vehicle_only,
                        sensitivity_config,
                        metric_sample_interval_s=metric_sample_interval_s,
                        vehicle_sample_interval_s=vehicle_sample_interval_s,
                        progress_interval_s=progress_interval_s,
                    )
                    timing_key = f"simulation_{scenario}_sec"
                    timing[timing_key] = float(timing.get(timing_key, 0.0) or 0.0) + float(
                        time.perf_counter() - scenario_t0
                    )
                except Exception as exc:
                    failures.append(
                        {
                        "crosswalk_id": cw_id,
                            "scenario": scenario,
                            "seed": seed,
                            "step": "run_simulation",
                            "error": str(exc),
                        }
                    )
                    continue


                if export_paths:
                    # Keep canonical filenames for downstream tools expecting stable paths.
                    if scenario == "baseline" and seed == seeds[0]:
                        edge_src = export_paths.get("edge_data_path")
                        lane_src = export_paths.get("lane_data_path")
                        veh_src = export_paths.get("fcd_vehicle_path")
                        ped_src = export_paths.get("fcd_person_path")
                        if edge_src and Path(edge_src).exists():
                            shutil.copy2(edge_src, output_dir / "edge_data.xml")
                        if lane_src and Path(lane_src).exists():
                            shutil.copy2(lane_src, output_dir / "lane_data.xml")
                        if veh_src and Path(veh_src).exists():
                            shutil.copy2(veh_src, output_dir / "fcd_vehicle.xml")
                        if ped_src and Path(ped_src).exists():
                            shutil.copy2(ped_src, output_dir / "fcd_person.xml")

                for evt in extension_events:
                    extension_rows.append(evt)
                for imp in incident_impacts:
                    incident_impact_rows.append(imp)

                if float(metrics.get("spillback_rate", 0.0)) >= 0.2:
                    boundary_warning_rows.append(
                        {
                            "crosswalk_id": cw_id,
                            "seed": seed,
                            "scenario": scenario,
                            "warning_type": "boundary_spillback",
                            "spillback_rate": float(metrics.get("spillback_rate", 0.0)),
                            "surrounding_boundary_lane_count": int(
                                metrics.get("surrounding_boundary_lane_count", 0)
                            ),
                            "message": "네트워크 범위가 작아 spillback이 과소추정될 수 있음",
                        }
                    )

                demand_row = demand_lookup.get((str(cw_id), str(seed)), {})
                seed_row = {
                    "crosswalk_id": cw_id,
                    "seed": seed,
                    "admin_dong": row["admin_dong"],
                    "dong_name": row["dong_name"],
                    "scenario": scenario,
                    "scenario_name": demand_row.get("scenario_name", DEFAULT_DEMAND_SCENARIO_NAME),
                    "demand_profile": demand_row.get("demand_profile", DEFAULT_DEMAND_SCENARIO_NAME),
                    "allocation_basis": demand_row.get("allocation_basis", ""),
                    "total_vehicle_flow_vph": float(demand_row.get("total_vehicle_flow_vph", 0.0) or 0.0),
                    "total_vehicle_count_600s": int(demand_row.get("total_vehicle_count_600s", 0) or 0),
                    "vehicle_type": demand_row.get("vehicle_type", "passenger"),
                    "passenger_ratio": float(demand_row.get("passenger_ratio", 1.0) or 1.0),
                    "vehicle_type_split": demand_row.get("vehicle_type_split", ""),
                    "road_group": demand_row.get("road_group", ""),
                    "road_name": demand_row.get("road_name", ""),
                    "road_group_type": demand_row.get("road_group_type", ""),
                    "edge_group_mapping_status": demand_row.get("edge_group_mapping_status", ""),
                    "network_edge_group": demand_row.get("network_edge_group", ""),
                    "road_allocated_count_600s": int(demand_row.get("road_allocated_count_600s", 0) or 0),
                    "road_allocated_flow_vph": float(demand_row.get("road_allocated_flow_vph", 0.0) or 0.0),
                    "vehicle_flow_scale": float(
                        demand_row.get("vehicle_flow_scale", DEFAULT_VEHICLE_FLOW_SCALE)
                    ),
                    "pedestrian_scale": float(
                        demand_row.get("pedestrian_scale", DEFAULT_PEDESTRIAN_SCALE)
                    ),
                    "pedestrian_count_600s": int(demand_row.get("pedestrian_count_600s", 0) or 0),
                    "generated_pedestrian_count": int(
                        demand_row.get("generated_pedestrian_count", 0) or 0
                    ),
                    "generated_vehicle_count": int(demand_row.get("generated_vehicle_count", 0) or 0),
                    "generated_vehicle_route_file": demand_row.get("generated_vehicle_route_file", ""),
                    "generated_vehicle_trip_file": demand_row.get("generated_vehicle_trip_file", ""),
                    "vehicle_net_file": demand_row.get("vehicle_net_file", ""),
                    "unique_depart_edges": int(demand_row.get("unique_depart_edges", 0) or 0),
                    "unique_arrival_edges": int(demand_row.get("unique_arrival_edges", 0) or 0),
                    "unique_route_edges": int(demand_row.get("unique_route_edges", 0) or 0),
                    "network_edge_coverage_ratio": float(
                        demand_row.get("network_edge_coverage_ratio", 0.0) or 0.0
                    ),
                    "pedestrian_scale_source": demand_row.get("pedestrian_scale_source", ""),
                    "raw_accident_count": row["accident_count"],
                    "elderly_ratio": row["elderly_ratio"],
                    "lane_count": lane_count_for_sim,
                    "vehicle_arrival_rate_per_lane": queue_vehicle_arrival_rate,
                    "queue_vehicle_num_lanes": queue_vehicle_num_lanes,
                    "pedestrian_arrival_rate_multiplier": float(
                        demand_row.get("pedestrian_arrival_rate_multiplier", DEFAULT_PEDESTRIAN_SCALE)
                        or DEFAULT_PEDESTRIAN_SCALE
                    ),
                    "vehicle_volume_multiplier": float(
                        demand_row.get("vehicle_volume_multiplier", DEFAULT_VEHICLE_FLOW_SCALE)
                        or DEFAULT_VEHICLE_FLOW_SCALE
                    ),
                    "walking_speed_profile": str(
                        (sensitivity_config or {}).get("walking_speed_profile_name", "base")
                    ),
                    "vehicle_speed_factor": float(
                        (sensitivity_config or {}).get("vehicle_speed_display_factor", 1.0) or 1.0
                    ),
                    "accident_risk_coefficient": float(
                        (sensitivity_config or {}).get("accident_risk_coefficient_value", 1.0) or 1.0
                    ),
                    "green_extension_policy": str(
                        (sensitivity_config or {}).get("green_extension_policy_name", "base_extension")
                    ),
                    "max_speed_kph": row["max_speed_kph"],
                    "crossing_length_m": row["crossing_length_m"],
                    "ped_green_base_sec": signal_timing["ped_green"],
                    "risk_score": row["risk_score"],
                    **metrics,
                }
                seed_rows.append(seed_row)

        # scenario 평균
        for scenario in ("baseline", "smart"):
            scenario_seed_rows = [r for r in seed_rows if r["crosswalk_id"] == cw_id and r["scenario"] == scenario]
            if not scenario_seed_rows:
                continue
            first_row = scenario_seed_rows[0]
            metric_keys = [
                key
                for key in first_row
                if key
                not in {
                    "crosswalk_id",
                    "seed",
                    "admin_dong",
                    "dong_name",
                    "scenario",
                }
                and pd.api.types.is_numeric_dtype(pd.Series([scenario_seed_rows[0][key]]))
            ]
            avg_metrics = {
                key: mean_or_nan([seed_row.get(key, np.nan) for seed_row in scenario_seed_rows])
                for key in metric_keys
            }
            avg_rows.append(
                {
                    "crosswalk_id": cw_id,
                    "admin_dong": row["admin_dong"],
                    "dong_name": row["dong_name"],
                    "scenario": scenario,
                    "scenario_name": first_row.get("scenario_name", DEFAULT_DEMAND_SCENARIO_NAME),
                    "demand_profile": first_row.get("demand_profile", DEFAULT_DEMAND_SCENARIO_NAME),
                    "allocation_basis": first_row.get("allocation_basis", ""),
                    "total_vehicle_flow_vph": float(first_row.get("total_vehicle_flow_vph", 0.0) or 0.0),
                    "total_vehicle_count_600s": int(first_row.get("total_vehicle_count_600s", 0) or 0),
                    "vehicle_type": first_row.get("vehicle_type", "passenger"),
                    "passenger_ratio": float(first_row.get("passenger_ratio", 1.0) or 1.0),
                    "vehicle_type_split": first_row.get("vehicle_type_split", ""),
                    "road_group": first_row.get("road_group", ""),
                    "road_name": first_row.get("road_name", ""),
                    "road_group_type": first_row.get("road_group_type", ""),
                    "edge_group_mapping_status": first_row.get("edge_group_mapping_status", ""),
                    "network_edge_group": first_row.get("network_edge_group", ""),
                    "road_allocated_count_600s": int(first_row.get("road_allocated_count_600s", 0) or 0),
                    "road_allocated_flow_vph": float(first_row.get("road_allocated_flow_vph", 0.0) or 0.0),
                    "pedestrian_scale_source": first_row.get("pedestrian_scale_source", ""),
                    **avg_metrics,
                }
            )

    seed_df = pd.DataFrame(seed_rows)
    avg_df = pd.DataFrame(avg_rows)
    seed_out = english_output_columns(seed_df)
    avg_out = english_output_columns(avg_df)
    write_csv_bundle(
        seed_out,
        csv_layout.results / "simulation_results_seed.csv",
        mirrors=[output_dir / "simulation_results_seed.csv"],
    )
    write_csv_bundle(
        avg_out,
        csv_layout.results / "simulation_results.csv",
        mirrors=[output_dir / "simulation_results.csv"],
    )
    write_csv_bundle(
        seed_out,
        csv_layout.internal / "raw_simulation_metrics.csv",
        mirrors=[csv_layout.internal / "simulation_results_seed.csv"],
    )

    experiment_metadata = _experiment_metadata(metric_sample_interval_s, vehicle_sample_interval_s)
    failed_cases_count = int(len(failures))
    result_rows = int(len(seed_df))
    baseline_result_rows = int((seed_df["scenario"] == "baseline").sum()) if not seed_df.empty and "scenario" in seed_df.columns else 0
    smart_result_rows = int((seed_df["scenario"] == "smart").sum()) if not seed_df.empty and "scenario" in seed_df.columns else 0
    run_success = failed_cases_count == 0 and baseline_result_rows >= 1 and smart_result_rows >= 1
    failure_parts: list[str] = []
    if failed_cases_count > 0:
        failure_parts.append(f"failed_cases_count={failed_cases_count}")
    if result_rows == 0:
        failure_parts.append("result_rows=0")
    if baseline_result_rows < 1:
        failure_parts.append(f"baseline_result_rows={baseline_result_rows}")
    if smart_result_rows < 1:
        failure_parts.append(f"smart_result_rows={smart_result_rows}")

    run_metadata = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "run_dir": str(output_dir),
        "output_dir": str(output_dir),
        "args": {
            "candidates": str(candidates_csv),
            "nets_dir": str(nets_dir),
            "sim_duration": int(sim_duration),
            "warmup": int(warmup),
            "seeds": list(seeds),
            "metric_sample_interval": float(metric_sample_interval_s),
            "vehicle_sample_interval": float(vehicle_sample_interval_s),
            "progress_interval": float(progress_interval_s),
        },
        **experiment_metadata,
    }
    (output_dir / "run_metadata.json").write_text(json.dumps(run_metadata, ensure_ascii=False, indent=2), encoding="utf-8")

    benchmark_timing = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "network_build_sec": None,
        "demand_generation_sec": None,
        "simulation_baseline_sec": timing.get("simulation_baseline_sec"),
        "simulation_smart_sec": timing.get("simulation_smart_sec"),
        "post_validation_sec": None,
        "total_sec": float(time.perf_counter() - t0),
        "partial": not run_success,
        "interrupted": False,
        "completed_scenarios": ["simulation"],
        "current_scenario": "",
        **experiment_metadata,
        "run_success": run_success,
        "failure_reason": "" if run_success else ";".join(failure_parts),
        "failed_cases_count": failed_cases_count,
        "result_rows": result_rows,
        "baseline_result_rows": baseline_result_rows,
        "smart_result_rows": smart_result_rows,
    }
    (output_dir / "benchmark_timing.json").write_text(json.dumps(benchmark_timing, ensure_ascii=False, indent=2), encoding="utf-8")

    extension_debug = pd.DataFrame(
        extension_rows,
        columns=[
            "sim_time",
            "crosswalk_id",
            "tls_id",
            "phase_index",
            "tls_state",
            "ped_link_indices",
            "vehicle_green_link_count",
            "is_ped_only_phase",
            "remaining_before_extension",
            "extension_sec",
            "ped_count_on_crossing",
            "seed",
            "scenario",
        ],
    )
    write_csv_bundle(
        extension_debug,
        output_dir / "extension_events_seed.csv",
        mirrors=[csv_layout.internal / "debug_extension_events.csv"],
    )
    incident_debug = pd.DataFrame(
        incident_event_rows,
        columns=[
            "incident_id",
            "event_type",
            "start_time",
            "end_time",
            "affected_edge_ids",
            "affected_lane_ids",
            "severity",
            "capacity_multiplier",
            "speed_multiplier",
            "blocked_lanes_count",
            "allow_rerouting",
            "crosswalk_id",
            "seed",
            "scenario",
        ],
    ).drop_duplicates()
    write_csv_bundle(
        incident_debug,
        output_dir / "incident_events_seed.csv",
        mirrors=[csv_layout.internal / "debug_incident_events.csv"],
    )
    write_csv_bundle(
        pd.DataFrame(
            incident_impact_rows,
            columns=[
                "incident_id",
                "crosswalk_id",
                "seed",
                "scenario",
                "event_type",
                "before_queue_avg",
                "after_queue_avg",
                "before_wait_avg_sec",
                "after_wait_avg_sec",
                "before_speed_avg_mps",
                "after_speed_avg_mps",
            ],
        ),
        output_dir / "incident_impact_seed.csv",
        mirrors=[csv_layout.internal / "intermediate_counter_dump.csv"],
    )

    if boundary_warning_rows:
        write_csv_bundle(
            pd.DataFrame(boundary_warning_rows),
            output_dir / "network_boundary_warnings.csv",
            mirrors=[csv_layout.results / "network_boundary_warnings.csv"],
        )

    if failures:
        failed_path = output_dir / "failed_cases.csv"
        write_csv_bundle(
            english_output_columns(pd.DataFrame(failures)),
            failed_path,
            mirrors=[csv_layout.internal / "failed_cases.csv"],
        )
    runtime_progress_path = output_dir / "runtime_progress.csv"
    if runtime_progress_path.exists():
        try:
            runtime_progress_df = pd.read_csv(runtime_progress_path)
            write_csv_bundle(
                runtime_progress_df,
                csv_layout.internal / "runtime_progress.csv",
                mirrors=[runtime_progress_path],
            )
        except Exception:
            pass
    return seed_df, avg_df, timing


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--candidates", default="outputs/candidates.csv")
    parser.add_argument("--output_dir", default="outputs")
    parser.add_argument("--nets_dir", default="sumo_nets")
    parser.add_argument("--seeds", nargs="+", type=int, default=[42, 43, 44])
    parser.add_argument("--sim_duration", type=int, default=1800)
    parser.add_argument("--warmup", type=int, default=300)
    parser.add_argument("--traci_step_length", type=float, default=0.1)
    parser.add_argument("--traffic_measure_radius_m", type=float, default=500.0)
    parser.add_argument("--extension_increment", type=float, default=None)
    parser.add_argument("--max_extensions", type=int, default=None)
    parser.add_argument("--vehicle_arrival_rate_per_hour", type=float, default=None)
    parser.add_argument("--saturation_flow_rate_per_hour", type=float, default=1900.0)
    parser.add_argument(
        "--vehicle_arrival_model",
        choices=["poisson", "bernoulli"],
        default="poisson",
    )
    parser.add_argument(
        "--disruption_scenario",
        choices=["best_case", "normal_urban", "congested_urban", "incident_case"],
        default="best_case",
    )
    parser.add_argument("--enable_random_disruptions", action="store_true")
    parser.add_argument("--bus_stop_rate_per_hour", type=float, default=0.0)
    parser.add_argument("--illegal_parking_rate_per_hour", type=float, default=0.0)
    parser.add_argument("--minor_incident_rate_per_hour", type=float, default=0.0)
    parser.add_argument("--accident_rate_per_hour", type=float, default=0.0)
    parser.add_argument("--model_parameters", default=None)
    parser.add_argument("--export_fcd", action="store_true")
    parser.add_argument("--vehicle_only", action="store_true")
    parser.add_argument("--sensitivity_scenarios", default=None)
    parser.add_argument("--sensitivity_case", default=None)
    parser.add_argument("--metric-sample-interval", type=float, default=0.0)
    parser.add_argument("--vehicle-sample-interval", type=float, default=0.0)
    parser.add_argument("--progress-interval", type=float, default=0.0)
    args = parser.parse_args()
    sensitivity_config = None
    if args.sensitivity_case:
        sensitivity_definitions = load_sensitivity_scenarios(args.sensitivity_scenarios)
        sensitivity_config = resolve_sensitivity_case(sensitivity_definitions, args.sensitivity_case)
    collect_all(
        args.candidates,
        args.output_dir,
        args.nets_dir,
        args.sim_duration,
        args.warmup,
        tuple(args.seeds),
        args.traci_step_length,
        args.traffic_measure_radius_m,
        args.extension_increment,
        args.max_extensions,
        args.vehicle_arrival_rate_per_hour,
        args.saturation_flow_rate_per_hour,
        args.vehicle_arrival_model,
        args.disruption_scenario,
        args.enable_random_disruptions,
        args.bus_stop_rate_per_hour,
        args.illegal_parking_rate_per_hour,
        args.minor_incident_rate_per_hour,
        args.accident_rate_per_hour,
        args.model_parameters,
        args.export_fcd,
        args.vehicle_only,
        sensitivity_config,
        metric_sample_interval_s=args.metric_sample_interval,
        vehicle_sample_interval_s=args.vehicle_sample_interval,
        progress_interval_s=args.progress_interval,
    )


if __name__ == "__main__":
    main()
