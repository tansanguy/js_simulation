from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

PROJECT_DIR = Path(__file__).resolve().parents[1]

import sys

sys.path.insert(0, str(PROJECT_DIR))

from smart_crosswalk_sumo.collect_metrics import mean_or_nan  # noqa: E402
from smart_crosswalk_sumo.generate_demand import (  # noqa: E402
    build_calibrator_additional,
    generate_pedestrian_demand,
    generate_vehicle_routes,
    get_demand_params,
    load_traffic_counts,
    observed_vehicle_rate_for_candidate,
    write_sumocfg,
)
from smart_crosswalk_sumo.model_config import load_model_parameters  # noqa: E402
from smart_crosswalk_sumo.network_utils import load_metadata, validate_pedestrian_connectivity  # noqa: E402
from smart_crosswalk_sumo.output_schema import english_output_columns  # noqa: E402
from smart_crosswalk_sumo.run_simulations import (  # noqa: E402
    compute_signal_timing,
    generate_incident_schedule,
    run_simulation,
)
from smart_crosswalk_sumo.sensitivity import (  # noqa: E402
    apply_parameter_value_overrides,
    build_sensitivity_cases,
    load_sensitivity_scenarios,
)


DEFAULT_CANDIDATES = PROJECT_DIR / "smart_crosswalk_sumo" / "outputs" / "candidates.csv"
DEFAULT_NETS_DIR = PROJECT_DIR / "smart_crosswalk_sumo" / "sumo_nets"
DEFAULT_OUTPUT_DIR = PROJECT_DIR / "smart_crosswalk_sumo" / "outputs" / "distribution_sensitivity"
DEFAULT_MODEL_ASSUMPTIONS = (
    PROJECT_DIR / "smart_crosswalk_sumo" / "config" / "model_assumptions.yaml"
)
DEFAULT_SENSITIVITY_CONFIG = (
    PROJECT_DIR / "smart_crosswalk_sumo" / "config" / "sensitivity_scenarios.yaml"
)
DEFAULT_TRAFFIC_COUNTS = PROJECT_DIR / "smart_crosswalk_sumo" / "inputs" / "traffic_counts_15min.csv"


def safe_case_name(case_id: str) -> str:
    return case_id.replace(":", "__").replace("/", "_")


def df_to_text(frame: pd.DataFrame) -> str:
    try:
        return frame.to_markdown(index=False)
    except Exception:
        return "```text\n" + frame.to_csv(index=False) + "```"


def ensure_candidate_columns(candidates_csv: Path) -> pd.DataFrame:
    candidates = pd.read_csv(candidates_csv)
    candidates = english_output_columns(candidates)
    return candidates


def select_candidates(
    candidates: pd.DataFrame,
    crosswalk_ids: list[str] | None,
    max_candidates: int,
) -> pd.DataFrame:
    if crosswalk_ids:
        wanted = {str(item) for item in crosswalk_ids}
        selected = candidates[candidates["crosswalk_id"].astype(str).isin(wanted)].copy()
    else:
        selected = candidates.head(max_candidates).copy()
    if selected.empty:
        raise ValueError("민감도 분석 대상 후보가 비어 있습니다.")
    return selected


def mean_numeric_columns(frame: pd.DataFrame, group_cols: list[str]) -> pd.DataFrame:
    if frame.empty:
        return pd.DataFrame()
    value_cols = [
        col
        for col in frame.columns
        if col not in group_cols and col != "seed" and pd.api.types.is_numeric_dtype(frame[col])
    ]
    agg = frame.groupby(group_cols, as_index=False)[value_cols].mean(numeric_only=True)
    if "seed" in frame.columns:
        seed_count = frame.groupby(group_cols, as_index=False)["seed"].nunique()
        seed_count = seed_count.rename(columns={"seed": "seed_count"})
        agg = agg.merge(seed_count, on=group_cols, how="left")
    else:
        agg["seed_count"] = 0
    if "run_mode" in frame.columns:
        mode_summary = (
            frame.groupby(group_cols)["run_mode"]
            .apply(lambda s: "|".join(sorted(set(s.astype(str)))))
            .reset_index(name="run_modes")
        )
        agg = agg.merge(mode_summary, on=group_cols, how="left")
    return agg


def summarize_tradeoff(results_df: pd.DataFrame) -> pd.DataFrame:
    if results_df.empty:
        return pd.DataFrame()
    keys = ["crosswalk_id", "case_id", "dimension_name", "level_name"]
    baseline = results_df[results_df["scenario"] == "baseline"].copy()
    smart = results_df[results_df["scenario"] == "smart"].copy()
    merged = baseline.merge(smart, on=keys, suffixes=("_baseline", "_smart"))
    if merged.empty:
        return pd.DataFrame()

    summary = pd.DataFrame()
    for key in keys:
        summary[key] = merged[key]
    summary["baseline_vehicle_delay_cost"] = pd.to_numeric(
        merged.get("vehicle_delay_cost_baseline"), errors="coerce"
    )
    summary["smart_vehicle_delay_cost"] = pd.to_numeric(
        merged.get("vehicle_delay_cost_smart"), errors="coerce"
    )
    summary["baseline_safety_risk_score"] = pd.to_numeric(
        merged.get("safety_risk_score_baseline"), errors="coerce"
    )
    summary["smart_safety_risk_score"] = pd.to_numeric(
        merged.get("safety_risk_score_smart"), errors="coerce"
    )
    summary["baseline_accident_expected_value"] = pd.to_numeric(
        merged.get("accident_expected_value_baseline"), errors="coerce"
    )
    summary["smart_accident_expected_value"] = pd.to_numeric(
        merged.get("accident_expected_value_smart"), errors="coerce"
    )
    summary["pedestrian_risk_reduction"] = (
        summary["baseline_safety_risk_score"] - summary["smart_safety_risk_score"]
    )
    summary["safety_benefit_surrogate"] = (
        summary["baseline_accident_expected_value"] - summary["smart_accident_expected_value"]
    )
    summary["vehicle_delay_cost_delta"] = (
        summary["smart_vehicle_delay_cost"] - summary["baseline_vehicle_delay_cost"]
    )
    summary["net_benefit"] = summary["safety_benefit_surrogate"] - summary["vehicle_delay_cost_delta"]
    denom = summary["vehicle_delay_cost_delta"].replace(0, np.nan)
    summary["tradeoff_score"] = summary["pedestrian_risk_reduction"] / denom
    summary["elderly_incomplete_cross_delta"] = pd.to_numeric(
        merged.get("elderly_incomplete_crossings_smart"), errors="coerce"
    ) - pd.to_numeric(merged.get("elderly_incomplete_crossings_baseline"), errors="coerce")
    summary["pedestrian_waiting_time_delta"] = pd.to_numeric(
        merged.get("average_pedestrian_wait_time_smart"), errors="coerce"
    ) - pd.to_numeric(merged.get("average_pedestrian_wait_time_baseline"), errors="coerce")
    summary["queue_length_delta"] = pd.to_numeric(
        merged.get("avg_queue_length_smart"), errors="coerce"
    ) - pd.to_numeric(merged.get("avg_queue_length_baseline"), errors="coerce")
    summary["vehicle_only_fallback_runs"] = pd.to_numeric(
        merged.get("vehicle_only_fallback_runs_smart"), errors="coerce"
    ).fillna(0) + pd.to_numeric(
        merged.get("vehicle_only_fallback_runs_baseline"), errors="coerce"
    ).fillna(0)
    return summary


def build_impact_summary(summary_df: pd.DataFrame) -> pd.DataFrame:
    if summary_df.empty:
        return pd.DataFrame(columns=["dimension_name", "impact_metric", "impact_span"])
    non_base = summary_df[summary_df["dimension_name"] != "base"].copy()
    if non_base.empty:
        return pd.DataFrame(columns=["dimension_name", "impact_metric", "impact_span"])
    rows: list[dict[str, Any]] = []
    for dimension_name, group in non_base.groupby("dimension_name", sort=False):
        for metric in [
            "net_benefit",
            "pedestrian_risk_reduction",
            "vehicle_delay_cost_delta",
            "elderly_incomplete_cross_delta",
            "pedestrian_waiting_time_delta",
            "queue_length_delta",
        ]:
            values = pd.to_numeric(group[metric], errors="coerce").dropna()
            if values.empty:
                continue
            rows.append(
                {
                    "dimension_name": dimension_name,
                    "impact_metric": metric,
                    "impact_span": float(values.max() - values.min()),
                }
            )
    impact_df = pd.DataFrame(rows)
    if impact_df.empty:
        return impact_df
    impact_df["impact_rank"] = impact_df.groupby("impact_metric")["impact_span"].rank(
        ascending=False,
        method="dense",
    )
    return impact_df.sort_values(["impact_metric", "impact_rank", "dimension_name"])


def write_plots(
    summary_df: pd.DataFrame,
    impact_df: pd.DataFrame,
    plot_path: Path,
) -> None:
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        return

    fig, axes = plt.subplots(2, 2, figsize=(14, 10))

    if summary_df.empty:
        for ax in axes.flat:
            ax.text(0.5, 0.5, "no data", ha="center", va="center")
            ax.set_axis_off()
        plt.tight_layout()
        plt.savefig(plot_path, dpi=180)
        plt.close(fig)
        return

    tradeoff = summary_df.sort_values("net_benefit", ascending=False).head(15)
    axes[0, 0].barh(tradeoff["case_id"], tradeoff["net_benefit"], color="#2a6f97")
    axes[0, 0].invert_yaxis()
    axes[0, 0].set_title("Top Net Benefit Cases")
    axes[0, 0].set_xlabel("net_benefit")

    axes[0, 1].scatter(
        summary_df["vehicle_delay_cost_delta"],
        summary_df["pedestrian_risk_reduction"],
        c=summary_df["vehicle_only_fallback_runs"].fillna(0),
        cmap="viridis",
        alpha=0.8,
    )
    axes[0, 1].axhline(0, color="gray", linewidth=1)
    axes[0, 1].axvline(0, color="gray", linewidth=1)
    axes[0, 1].set_title("Safety Benefit vs Delay Cost")
    axes[0, 1].set_xlabel("vehicle_delay_cost_delta")
    axes[0, 1].set_ylabel("pedestrian_risk_reduction")

    queue_chart = summary_df.sort_values("queue_length_delta", ascending=False).head(15)
    axes[1, 0].barh(queue_chart["case_id"], queue_chart["queue_length_delta"], color="#bc4749")
    axes[1, 0].invert_yaxis()
    axes[1, 0].set_title("Queue Length Delta")
    axes[1, 0].set_xlabel("avg_queue_length delta")

    impact_net = impact_df[impact_df["impact_metric"] == "net_benefit"].sort_values(
        "impact_span", ascending=False
    )
    if impact_net.empty:
        axes[1, 1].text(0.5, 0.5, "no impact summary", ha="center", va="center")
        axes[1, 1].set_axis_off()
    else:
        axes[1, 1].bar(impact_net["dimension_name"], impact_net["impact_span"], color="#386641")
        axes[1, 1].tick_params(axis="x", rotation=30)
        axes[1, 1].set_title("Parameter Impact Span on Net Benefit")
        axes[1, 1].set_ylabel("impact_span")

    plt.tight_layout()
    plt.savefig(plot_path, dpi=180)
    plt.close(fig)


def write_report(
    report_path: Path,
    summary_df: pd.DataFrame,
    impact_df: pd.DataFrame,
    failures_df: pd.DataFrame,
    selected_candidates: pd.DataFrame,
    cases: list[dict[str, Any]],
) -> None:
    lines = [
        "# Distribution Sensitivity Report",
        "",
        "## Scope",
        "",
        f"- candidate_count: {len(selected_candidates)}",
        f"- case_count: {len(cases)}",
        f"- analyzed_crosswalk_ids: {', '.join(selected_candidates['crosswalk_id'].astype(str).tolist())}",
        "",
        "## Result Summary",
        "",
    ]
    if summary_df.empty:
        lines.append("- no completed baseline/smart comparison rows")
    else:
        top_net = summary_df.sort_values("net_benefit", ascending=False).head(5)
        worst_net = summary_df.sort_values("net_benefit", ascending=True).head(5)
        lines.append("- top net benefit cases:")
        lines.append(df_to_text(top_net))
        lines.append("")
        lines.append("- lowest net benefit cases:")
        lines.append(df_to_text(worst_net))
    lines.extend(["", "## Most Sensitive Parameters", ""])
    if impact_df.empty:
        lines.append("- no impact span could be computed")
    else:
        best = impact_df[impact_df["impact_metric"] == "net_benefit"].sort_values(
            "impact_span", ascending=False
        )
        lines.append(df_to_text(best.head(10)))
    lines.extend(["", "## Failure Audit", ""])
    if failures_df.empty:
        lines.append("- no failures recorded")
    else:
        lines.append(f"- failure_count: {len(failures_df)}")
        preview_cols = [col for col in ["crosswalk_id", "case_id", "scenario", "seed", "step", "error"] if col in failures_df.columns]
        lines.append(df_to_text(failures_df[preview_cols].head(20)))
    report_path.write_text("\n".join(lines), encoding="utf-8")


def run_case(
    row: pd.Series,
    metadata: dict[str, Any],
    net_file: Path,
    working_dir: Path,
    counts_df: pd.DataFrame,
    model_params: dict[str, dict[str, Any]],
    sensitivity_case: dict[str, Any],
    args: argparse.Namespace,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    result_rows: list[dict[str, Any]] = []
    failure_rows: list[dict[str, Any]] = []

    cw_id = str(row["crosswalk_id"])
    signal_timing = compute_signal_timing(row)
    cw_params = {"crossing_length_m": float(row["crossing_length_m"])}
    lane_count_for_sim = float(metadata.get("lane_count_csv", row["lane_count"]))
    queue_vehicle_num_lanes = max(1, int(lane_count_for_sim))

    observed_veh_per_hour, _ = observed_vehicle_rate_for_candidate(metadata, counts_df, args.sim_duration)
    effective_model_params = apply_parameter_value_overrides(
        model_params,
        sensitivity_case.get("parameter_overrides"),
    )

    ped_validation = validate_pedestrian_connectivity(net_file, metadata, cw_id=cw_id)
    ped_candidate_valid = ped_validation["validation_status"] == "valid"

    for seed in args.seeds:
        params = get_demand_params(
            row,
            seed,
            args.demand_profile,
            effective_model_params,
            observed_veh_per_hour,
            sensitivity_case,
        )
        run_dir = working_dir / f"cw_{cw_id}"
        run_dir.mkdir(parents=True, exist_ok=True)
        vehicle_file = run_dir / f"routes_seed{seed}.rou.xml"
        pedestrian_file = run_dir / f"peds_seed{seed}.rou.xml"
        calibrator_file = run_dir / f"calibrator_seed{seed}.add.xml"

        try:
            generate_vehicle_routes(params, net_file, vehicle_file, args.sim_duration, seed)
            ped_enabled = ped_candidate_valid
            ped_count = 0
            if ped_enabled:
                ped_count = generate_pedestrian_demand(
                    params,
                    metadata["ped_route"],
                    pedestrian_file,
                    args.sim_duration,
                    seed,
                )
            else:
                pedestrian_file.write_text(
                    '<?xml version="1.0" encoding="utf-8"?>\n<routes />\n',
                    encoding="utf-8",
                )
            calib_path = build_calibrator_additional(metadata, counts_df, calibrator_file)
            additional_files = [str(calib_path.resolve())] if calib_path else None
            for scenario in ("baseline", "smart"):
                write_sumocfg(
                    net_file,
                    vehicle_file,
                    pedestrian_file,
                    run_dir / f"{scenario}_seed{seed}.sumocfg",
                    args.sim_duration,
                    args.warmup,
                    args.sumo_step_length,
                    additional_files,
                    vehicle_only=False,
                )
                write_sumocfg(
                    net_file,
                    vehicle_file,
                    pedestrian_file,
                    run_dir / f"{scenario}_seed{seed}_vehicle_only.sumocfg",
                    args.sim_duration,
                    args.warmup,
                    args.sumo_step_length,
                    additional_files,
                    vehicle_only=True,
                )
        except Exception as exc:
            failure_rows.append(
                {
                    "crosswalk_id": cw_id,
                    "case_id": sensitivity_case["case_id"],
                    "dimension_name": sensitivity_case["dimension_name"],
                    "level_name": sensitivity_case["level_name"],
                    "scenario": "shared",
                    "seed": seed,
                    "step": "generate_demand",
                    "error": str(exc),
                }
            )
            continue

        try:
            schedule_events = generate_incident_schedule(
                args.disruption_scenario,
                args.sim_duration,
                seed,
                metadata,
                metadata.get("approach_lanes", []),
                effective_model_params,
                args.enable_random_disruptions,
                args.bus_stop_rate_per_hour,
                args.illegal_parking_rate_per_hour,
                args.minor_incident_rate_per_hour,
                args.accident_rate_per_hour,
            )
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
            failure_rows.append(
                {
                    "crosswalk_id": cw_id,
                    "case_id": sensitivity_case["case_id"],
                    "dimension_name": sensitivity_case["dimension_name"],
                    "level_name": sensitivity_case["level_name"],
                    "scenario": "shared",
                    "seed": seed,
                    "step": "incident_schedule",
                    "error": str(exc),
                }
            )
            schedule_payload = []

        for scenario in ("baseline", "smart"):
            base_row = {
                "crosswalk_id": cw_id,
                "case_id": sensitivity_case["case_id"],
                "dimension_name": sensitivity_case["dimension_name"],
                "level_name": sensitivity_case["level_name"],
                "scenario": scenario,
                "seed": seed,
                "ped_count": ped_count,
                "ped_candidate_valid": int(ped_candidate_valid),
                "pedestrian_arrival_rate_multiplier": float(
                    sensitivity_case.get("pedestrian_arrival_rate_multiplier", 1.0)
                ),
                "elderly_ratio_multiplier": float(
                    sensitivity_case.get("elderly_ratio_multiplier", 1.0)
                ),
                "elderly_ratio_override": sensitivity_case.get("elderly_ratio_override"),
                "walking_speed_profile": sensitivity_case.get("walking_speed_profile_name", "base"),
                "vehicle_volume_multiplier": float(
                    sensitivity_case.get("vehicle_volume_multiplier", 1.0)
                ),
                "vehicle_speed_factor": float(
                    sensitivity_case.get("vehicle_speed_display_factor", 1.0)
                ),
                "accident_risk_coefficient": float(
                    sensitivity_case.get("accident_risk_coefficient_value", 1.0)
                ),
                "green_extension_policy": sensitivity_case.get(
                    "green_extension_policy_name", "base_extension"
                ),
            }
            try:
                metrics, _, _, _, _ = run_simulation(
                    net_file,
                    vehicle_file,
                    pedestrian_file,
                    run_dir / f"{scenario}_seed{seed}.sumocfg",
                    scenario,
                    signal_timing,
                    cw_params,
                    metadata,
                    args.sim_duration,
                    args.warmup,
                    seed,
                    args.traci_step_length,
                    args.traffic_measure_radius_m,
                    None,
                    None,
                    float(params["veh_per_hour"]),
                    float(
                        effective_model_params.get("saturation_flow_rate", {}).get(
                            "value",
                            effective_model_params.get("saturation_flow_rate_pcphpl", {}).get(
                                "value", 1900.0
                            ),
                        )
                    ),
                    queue_vehicle_num_lanes,
                    args.vehicle_arrival_model,
                    args.disruption_scenario,
                    args.enable_random_disruptions,
                    args.bus_stop_rate_per_hour,
                    args.illegal_parking_rate_per_hour,
                    args.minor_incident_rate_per_hour,
                    args.accident_rate_per_hour,
                    schedule_payload,
                    cw_id,
                    args.model_assumptions,
                    False,
                    working_dir,
                    False,
                    sensitivity_case,
                )
                result_rows.append(
                    {
                        **base_row,
                        "run_mode": "full",
                        "vehicle_only_fallback_runs": 0,
                        **metrics,
                    }
                )
            except Exception as exc:
                failure_rows.append(
                    {
                        "crosswalk_id": cw_id,
                        "case_id": sensitivity_case["case_id"],
                        "dimension_name": sensitivity_case["dimension_name"],
                        "level_name": sensitivity_case["level_name"],
                        "scenario": scenario,
                        "seed": seed,
                        "step": "run_simulation_full",
                        "error": str(exc),
                    }
                )
                try:
                    metrics, _, _, _, _ = run_simulation(
                        net_file,
                        vehicle_file,
                        pedestrian_file,
                        run_dir / f"{scenario}_seed{seed}_vehicle_only.sumocfg",
                        scenario,
                        signal_timing,
                        cw_params,
                        metadata,
                        args.sim_duration,
                        args.warmup,
                        seed,
                        args.traci_step_length,
                        args.traffic_measure_radius_m,
                        None,
                        None,
                        float(params["veh_per_hour"]),
                        float(
                            effective_model_params.get("saturation_flow_rate", {}).get(
                                "value",
                                effective_model_params.get("saturation_flow_rate_pcphpl", {}).get(
                                    "value", 1900.0
                                ),
                            )
                        ),
                        queue_vehicle_num_lanes,
                        args.vehicle_arrival_model,
                        args.disruption_scenario,
                        args.enable_random_disruptions,
                        args.bus_stop_rate_per_hour,
                        args.illegal_parking_rate_per_hour,
                        args.minor_incident_rate_per_hour,
                        args.accident_rate_per_hour,
                        schedule_payload,
                        cw_id,
                        args.model_assumptions,
                        False,
                        working_dir,
                        True,
                        sensitivity_case,
                    )
                    result_rows.append(
                        {
                            **base_row,
                            "run_mode": "vehicle_only_fallback",
                            "vehicle_only_fallback_runs": 1,
                            "full_simulation_failure_reason": str(exc),
                            **metrics,
                        }
                    )
                except Exception as fallback_exc:
                    failure_rows.append(
                        {
                            "crosswalk_id": cw_id,
                            "case_id": sensitivity_case["case_id"],
                            "dimension_name": sensitivity_case["dimension_name"],
                            "level_name": sensitivity_case["level_name"],
                            "scenario": scenario,
                            "seed": seed,
                            "step": "run_simulation_vehicle_only_fallback",
                            "error": str(fallback_exc),
                        }
                    )
    return result_rows, failure_rows


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="분포 및 파라미터 정당성 민감도 분석 실행")
    parser.add_argument("--candidates", default=str(DEFAULT_CANDIDATES))
    parser.add_argument("--nets_dir", default=str(DEFAULT_NETS_DIR))
    parser.add_argument("--output_dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--crosswalk_ids", nargs="*", default=None)
    parser.add_argument("--max_candidates", type=int, default=1)
    parser.add_argument("--seeds", nargs="+", type=int, default=[42])
    parser.add_argument("--sim_duration", type=int, default=900)
    parser.add_argument("--warmup", type=int, default=180)
    parser.add_argument("--sumo_step_length", type=float, default=1.0)
    parser.add_argument("--traci_step_length", type=float, default=0.5)
    parser.add_argument("--traffic_measure_radius_m", type=float, default=500.0)
    parser.add_argument("--demand_profile", choices=["average", "seoul_commute_peak"], default="average")
    parser.add_argument("--traffic_counts", default=str(DEFAULT_TRAFFIC_COUNTS) if DEFAULT_TRAFFIC_COUNTS.exists() else None)
    parser.add_argument("--representative_day_id", default=None)
    parser.add_argument("--model_assumptions", default=str(DEFAULT_MODEL_ASSUMPTIONS))
    parser.add_argument("--sensitivity_scenarios", default=str(DEFAULT_SENSITIVITY_CONFIG))
    parser.add_argument("--dimensions", nargs="*", default=None)
    parser.add_argument(
        "--vehicle_arrival_model",
        choices=["poisson", "bernoulli"],
        default="poisson",
    )
    parser.add_argument(
        "--disruption_scenario",
        choices=["best_case", "normal_urban", "congested_urban", "incident_case"],
        default="normal_urban",
    )
    parser.add_argument("--enable_random_disruptions", action="store_true")
    parser.add_argument("--bus_stop_rate_per_hour", type=float, default=0.0)
    parser.add_argument("--illegal_parking_rate_per_hour", type=float, default=0.0)
    parser.add_argument("--minor_incident_rate_per_hour", type=float, default=0.0)
    parser.add_argument("--accident_rate_per_hour", type=float, default=0.0)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    candidates_csv = Path(args.candidates)
    nets_dir = Path(args.nets_dir)
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    candidates = ensure_candidate_columns(candidates_csv)
    selected_candidates = select_candidates(candidates, args.crosswalk_ids, args.max_candidates)
    model_params = load_model_parameters(args.model_assumptions)
    sensitivity_definitions = load_sensitivity_scenarios(args.sensitivity_scenarios)
    cases = build_sensitivity_cases(sensitivity_definitions, args.dimensions)
    counts_df = load_traffic_counts(args.traffic_counts, args.representative_day_id)

    all_result_rows: list[dict[str, Any]] = []
    all_failure_rows: list[dict[str, Any]] = []

    for row in selected_candidates.itertuples(index=False):
        row_series = pd.Series(row._asdict())
        cw_id = str(row_series["crosswalk_id"])
        net_file = nets_dir / f"cw_{cw_id}" / "network.net.xml"
        metadata_file = nets_dir / f"cw_{cw_id}" / "metadata.json"
        metadata = load_metadata(metadata_file)
        for case in cases:
            case_working_dir = output_dir / "_case_runs" / safe_case_name(case["case_id"])
            result_rows, failure_rows = run_case(
                row_series,
                metadata,
                net_file,
                case_working_dir,
                counts_df,
                model_params,
                case,
                args,
            )
            all_result_rows.extend(result_rows)
            all_failure_rows.extend(failure_rows)

    raw_df = pd.DataFrame(all_result_rows)
    failures_df = pd.DataFrame(all_failure_rows)
    if not failures_df.empty:
        failures_df.to_csv(output_dir / "failed_cases.csv", index=False)

    results_df = mean_numeric_columns(
        raw_df,
        ["crosswalk_id", "case_id", "dimension_name", "level_name", "scenario"],
    )
    summary_df = summarize_tradeoff(results_df)
    impact_df = build_impact_summary(summary_df)

    results_path = output_dir / "distribution_sensitivity_results.csv"
    summary_path = output_dir / "distribution_sensitivity_summary.csv"
    plot_path = output_dir / "distribution_sensitivity_plots.png"
    report_path = output_dir / "distribution_sensitivity_report.md"

    results_df.to_csv(results_path, index=False)
    summary_df.to_csv(summary_path, index=False)
    write_plots(summary_df, impact_df, plot_path)
    write_report(report_path, summary_df, impact_df, failures_df, selected_candidates, cases)

    # Keep a machine-readable impact table next to the requested outputs for follow-up analysis.
    if not impact_df.empty:
        impact_df.to_csv(output_dir / "distribution_sensitivity_impact.csv", index=False)

    print(f"[distribution-sensitivity] results={results_path}")
    print(f"[distribution-sensitivity] summary={summary_path}")
    print(f"[distribution-sensitivity] plots={plot_path}")
    print(f"[distribution-sensitivity] report={report_path}")
    if not failures_df.empty:
        print(f"[distribution-sensitivity] failed_cases={output_dir / 'failed_cases.csv'}")


if __name__ == "__main__":
    main()
