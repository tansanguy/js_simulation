from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import pandas as pd

try:
    from .collect_metrics import mean_or_nan
    from .network_utils import load_metadata
    from .run_simulations import compute_signal_timing, run_simulation
except ImportError:
    from collect_metrics import mean_or_nan
    from network_utils import load_metadata
    from run_simulations import compute_signal_timing, run_simulation


DEFAULT_TRAFFIC_LEVELS = {
    "low": 300.0,
    "medium": 800.0,
    "high": 1200.0,
    "saturated": 1800.0,
}

DEFAULT_DISRUPTION_SCENARIOS = (
    "best_case",
    "normal_urban",
    "congested_urban",
    "incident_case",
)

SIGNAL_POLICIES = {
    "fixed_signal": "baseline",
    "smart_crosswalk": "smart",
}


def df_to_text(df: pd.DataFrame) -> str:
    try:
        return df.to_markdown(index=False)
    except ImportError:
        return df.to_csv(index=False)


def find_target_row(candidates: pd.DataFrame, crosswalk_id: str | None) -> pd.Series:
    if crosswalk_id:
        selected = candidates[candidates["횡단보도ID"].astype(str) == str(crosswalk_id)]
        if selected.empty:
            raise ValueError(f"횡단보도ID={crosswalk_id} 를 candidates.csv에서 찾지 못했습니다.")
        return selected.iloc[0]
    return candidates.iloc[0]


def weighted_cost(
    average_pedestrian_wait_time: float,
    average_vehicle_delay: float,
    alpha: float,
    beta: float,
) -> float:
    return float(alpha * average_pedestrian_wait_time + beta * average_vehicle_delay)


def run_vehicle_sensitivity(
    candidates_csv: str | Path,
    nets_dir: str | Path,
    output_csv: str | Path,
    crosswalk_id: str | None = None,
    seeds: tuple[int, ...] = (42, 43, 44),
    sim_duration: int = 1800,
    warmup: int = 300,
    traci_step_length: float = 0.5,
    traffic_measure_radius_m: float = 500.0,
    extension_increment: float = 10.0,
    max_extensions: int = 1,
    saturation_flow_rate_per_hour: float = 1800.0,
    vehicle_arrival_model: str = "poisson",
    alpha: float = 1.0,
    beta: float = 1.0,
    enable_random_disruptions: bool = False,
    bus_stop_rate_per_hour: float = 0.0,
    illegal_parking_rate_per_hour: float = 0.0,
    minor_incident_rate_per_hour: float = 0.0,
    accident_rate_per_hour: float = 0.0,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    candidates = pd.read_csv(candidates_csv)
    row = find_target_row(candidates, crosswalk_id)
    cw_id = str(row["횡단보도ID"])
    cw_dir = Path(nets_dir) / f"cw_{cw_id}"
    metadata = load_metadata(cw_dir / "metadata.json")
    signal_timing = compute_signal_timing(row)
    cw_params = {"crossing_length_m": float(row["crossing_length_m"])}
    vehicle_num_lanes = max(1, int(row["LANES"]))

    raw_rows: list[dict[str, float | int | str]] = []
    for traffic_level, vehicle_arrival_rate_per_hour in DEFAULT_TRAFFIC_LEVELS.items():
        for disruption_scenario in DEFAULT_DISRUPTION_SCENARIOS:
            for signal_policy, scenario in SIGNAL_POLICIES.items():
                for seed in seeds:
                    metrics = run_simulation(
                        cw_dir / "network.net.xml",
                        cw_dir / f"routes_seed{seed}.rou.xml",
                        cw_dir / f"peds_seed{seed}.rou.xml",
                        cw_dir / f"{scenario}_seed{seed}.sumocfg",
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
                        vehicle_arrival_rate_per_hour,
                        saturation_flow_rate_per_hour,
                        vehicle_num_lanes,
                        vehicle_arrival_model,
                        disruption_scenario,
                        enable_random_disruptions,
                        bus_stop_rate_per_hour,
                        illegal_parking_rate_per_hour,
                        minor_incident_rate_per_hour,
                        accident_rate_per_hour,
                    )
                    raw_rows.append(
                        {
                            "횡단보도ID": cw_id,
                            "traffic_level": traffic_level,
                            "vehicle_arrival_rate_per_hour": vehicle_arrival_rate_per_hour,
                            "disruption_scenario": disruption_scenario,
                            "signal_policy": signal_policy,
                            "scenario": scenario,
                            "seed": seed,
                            **metrics,
                        }
                    )

    raw_df = pd.DataFrame(raw_rows)
    metric_keys = [
        key
        for key in raw_df.columns
        if key
        not in {
            "횡단보도ID",
            "traffic_level",
            "vehicle_arrival_rate_per_hour",
            "disruption_scenario",
            "signal_policy",
            "scenario",
            "seed",
        }
        and pd.api.types.is_numeric_dtype(raw_df[key])
    ]
    summary_rows: list[dict[str, float | int | str]] = []
    for (
        traffic_level,
        arrival_rate,
        disruption_scenario,
        signal_policy,
    ), group in raw_df.groupby(
        [
            "traffic_level",
            "vehicle_arrival_rate_per_hour",
            "disruption_scenario",
            "signal_policy",
        ],
        sort=False,
    ):
        summary = {
            "횡단보도ID": cw_id,
            "traffic_level": traffic_level,
            "vehicle_arrival_rate_per_hour": float(arrival_rate),
            "disruption_scenario": disruption_scenario,
            "signal_policy": signal_policy,
        }
        for key in metric_keys:
            summary[key] = mean_or_nan(group[key].tolist())
        summary["weighted_cost"] = weighted_cost(
            float(summary.get("average_pedestrian_wait_time", 0.0)),
            float(summary.get("average_vehicle_delay", 0.0)),
            alpha,
            beta,
        )
        summary_rows.append(summary)
    summary_df = pd.DataFrame(summary_rows)

    comparison_rows: list[dict[str, float | int | str]] = []
    for (traffic_level, disruption_scenario), group in summary_df.groupby(
        ["traffic_level", "disruption_scenario"],
        sort=False,
    ):
        baseline = group[group["signal_policy"] == "fixed_signal"]
        smart = group[group["signal_policy"] == "smart_crosswalk"]
        if baseline.empty or smart.empty:
            continue
        baseline_row = baseline.iloc[0]
        smart_row = smart.iloc[0]
        comparison_rows.append(
            {
                "횡단보도ID": cw_id,
                "traffic_level": traffic_level,
                "disruption_scenario": disruption_scenario,
                "pedestrian_wait_reduction_vs_baseline": float(
                    baseline_row["average_pedestrian_wait_time"]
                    - smart_row["average_pedestrian_wait_time"]
                ),
                "vehicle_delay_increase_vs_baseline": float(
                    smart_row["average_vehicle_delay"] - baseline_row["average_vehicle_delay"]
                ),
                "queue_increase_vs_baseline": float(
                    smart_row["max_vehicle_queue_length"]
                    - baseline_row["max_vehicle_queue_length"]
                ),
                "throughput_change_vs_baseline": float(
                    smart_row["throughput"] - baseline_row["throughput"]
                ),
            }
        )
    comparison_df = pd.DataFrame(comparison_rows)
    if not comparison_df.empty:
        summary_df = summary_df.merge(
            comparison_df,
            on=["횡단보도ID", "traffic_level", "disruption_scenario"],
            how="left",
        )
    else:
        summary_df["pedestrian_wait_reduction_vs_baseline"] = np.nan
        summary_df["vehicle_delay_increase_vs_baseline"] = np.nan
        summary_df["queue_increase_vs_baseline"] = np.nan
        summary_df["throughput_change_vs_baseline"] = np.nan

    output_csv = Path(output_csv)
    output_csv.parent.mkdir(parents=True, exist_ok=True)
    raw_df.to_csv(output_csv.with_name(output_csv.stem + "_raw.csv"), index=False)
    summary_df.to_csv(output_csv, index=False)

    table = summary_df[
        [
            "traffic_level",
            "vehicle_arrival_rate_per_hour",
            "disruption_scenario",
            "signal_policy",
            "average_pedestrian_wait_time",
            "max_pedestrian_wait_time",
            "total_pedestrian_green_extension_time",
            "average_vehicle_delay",
            "average_vehicle_queue_length",
            "max_vehicle_queue_length",
            "throughput",
            "remaining_vehicle_queue",
            "disruption_event_count",
            "total_disruption_duration",
            "disruption_time_ratio",
            "average_capacity_multiplier",
            "average_effective_capacity",
            "lost_capacity_time",
            "weighted_cost",
        ]
    ].copy()
    print(df_to_text(table))
    return raw_df, summary_df


def main() -> None:
    parser = argparse.ArgumentParser(
        description="스마트 횡단보도 교통량-교란 시나리오 민감도 분석"
    )
    parser.add_argument("--candidates", required=True)
    parser.add_argument("--nets_dir", required=True)
    parser.add_argument("--output_csv", required=True)
    parser.add_argument("--crosswalk_id", default=None)
    parser.add_argument("--seeds", nargs="+", type=int, default=[42, 43, 44])
    parser.add_argument("--sim_duration", type=int, default=1800)
    parser.add_argument("--warmup", type=int, default=300)
    parser.add_argument("--traci_step_length", type=float, default=0.5)
    parser.add_argument("--traffic_measure_radius_m", type=float, default=500.0)
    parser.add_argument("--extension_increment", type=float, default=10.0)
    parser.add_argument("--max_extensions", type=int, default=1)
    parser.add_argument("--saturation_flow_rate_per_hour", type=float, default=1800.0)
    parser.add_argument(
        "--vehicle_arrival_model",
        choices=["poisson", "bernoulli"],
        default="poisson",
    )
    parser.add_argument("--alpha", type=float, default=1.0)
    parser.add_argument("--beta", type=float, default=1.0)
    parser.add_argument("--enable_random_disruptions", action="store_true")
    parser.add_argument("--bus_stop_rate_per_hour", type=float, default=0.0)
    parser.add_argument("--illegal_parking_rate_per_hour", type=float, default=0.0)
    parser.add_argument("--minor_incident_rate_per_hour", type=float, default=0.0)
    parser.add_argument("--accident_rate_per_hour", type=float, default=0.0)
    args = parser.parse_args()
    run_vehicle_sensitivity(
        args.candidates,
        args.nets_dir,
        args.output_csv,
        args.crosswalk_id,
        tuple(args.seeds),
        args.sim_duration,
        args.warmup,
        args.traci_step_length,
        args.traffic_measure_radius_m,
        args.extension_increment,
        args.max_extensions,
        args.saturation_flow_rate_per_hour,
        args.vehicle_arrival_model,
        args.alpha,
        args.beta,
        args.enable_random_disruptions,
        args.bus_stop_rate_per_hour,
        args.illegal_parking_rate_per_hour,
        args.minor_incident_rate_per_hour,
        args.accident_rate_per_hour,
    )


if __name__ == "__main__":
    main()
