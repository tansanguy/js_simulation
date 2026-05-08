from __future__ import annotations

import argparse
import shutil
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

try:
    from .network_utils import load_metadata
    from .run_simulations import (
        compute_signal_timing,
        generate_incident_schedule,
        run_simulation,
        serialize_incident_event,
    )
except ImportError:
    from network_utils import load_metadata
    from run_simulations import (
        compute_signal_timing,
        generate_incident_schedule,
        run_simulation,
        serialize_incident_event,
    )

try:
    from .model_config import get_parameter_value, load_model_parameters
    from .output_schema import english_output_columns
    from .sensitivity import (
        apply_parameter_value_overrides,
        load_sensitivity_scenarios,
        resolve_sensitivity_case,
    )
except ImportError:
    from model_config import get_parameter_value, load_model_parameters
    from output_schema import english_output_columns
    from sensitivity import apply_parameter_value_overrides, load_sensitivity_scenarios, resolve_sensitivity_case


def mean_or_nan(values: list[float]) -> float:
    arr = np.asarray(values, dtype=float)
    if arr.size == 0 or np.all(np.isnan(arr)):
        return float("nan")
    return float(np.nanmean(arr))


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
) -> tuple[pd.DataFrame, pd.DataFrame]:
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    candidates = pd.read_csv(candidates_csv)
    seed_rows = []
    avg_rows = []
    failures = []
    extension_rows: list[dict[str, Any]] = []
    incident_event_rows: list[dict[str, Any]] = []
    incident_impact_rows: list[dict[str, Any]] = []
    boundary_warning_rows: list[dict[str, Any]] = []

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

                seed_row = {
                    "crosswalk_id": cw_id,
                    "seed": seed,
                    "admin_dong": row["admin_dong"],
                    "dong_name": row["dong_name"],
                    "scenario": scenario,
                    "raw_accident_count": row["accident_count"],
                    "elderly_ratio": row["elderly_ratio"],
                    "lane_count": lane_count_for_sim,
                    "vehicle_arrival_rate_per_lane": queue_vehicle_arrival_rate,
                    "queue_vehicle_num_lanes": queue_vehicle_num_lanes,
                    "pedestrian_arrival_rate_multiplier": float(
                        (sensitivity_config or {}).get("pedestrian_arrival_rate_multiplier", 1.0) or 1.0
                    ),
                    "vehicle_volume_multiplier": float(
                        (sensitivity_config or {}).get("vehicle_volume_multiplier", 1.0) or 1.0
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
            metric_keys = [
                key
                for key in scenario_seed_rows[0]
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
                    **avg_metrics,
                }
            )

    seed_df = pd.DataFrame(seed_rows)
    avg_df = pd.DataFrame(avg_rows)
    english_output_columns(seed_df).to_csv(output_dir / "simulation_results_seed.csv", index=False)
    english_output_columns(avg_df).to_csv(output_dir / "simulation_results.csv", index=False)

    extension_debug = pd.DataFrame(
        extension_rows,
        columns=[
            "sim_time",
            "crosswalk_id",
            "tls_id",
            "phase_index",
            "remaining_before_extension",
            "extension_sec",
            "ped_count_on_crossing",
            "seed",
            "scenario",
        ],
    )
    extension_debug.to_csv(output_dir / "extension_events_seed.csv", index=False)
    extension_debug.to_csv(output_dir / "debug_extension_events.csv", index=False)
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
    incident_debug.to_csv(output_dir / "incident_events_seed.csv", index=False)
    incident_debug.to_csv(output_dir / "debug_incident_events.csv", index=False)
    pd.DataFrame(incident_impact_rows, columns=["incident_id","crosswalk_id","seed","scenario","event_type","before_queue_avg","after_queue_avg","before_wait_avg_sec","after_wait_avg_sec","before_speed_avg_mps","after_speed_avg_mps"]).to_csv(output_dir / "incident_impact_seed.csv", index=False)

    if boundary_warning_rows:
        pd.DataFrame(boundary_warning_rows).to_csv(
            output_dir / "network_boundary_warnings.csv", index=False
        )

    if failures:
        failed_path = output_dir / "failed_cases.csv"
        english_output_columns(pd.DataFrame(failures)).to_csv(
            failed_path,
            mode="a",
            header=not failed_path.exists(),
            index=False,
        )
    return seed_df, avg_df


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
    )


if __name__ == "__main__":
    main()
