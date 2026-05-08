from __future__ import annotations

import argparse
import shutil
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
from pandas.errors import EmptyDataError

PROJECT_DIR = Path(__file__).resolve().parents[1]
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from smart_crosswalk_sumo.build_networks import build_all_networks
from smart_crosswalk_sumo.collect_metrics import collect_all
from smart_crosswalk_sumo.generate_demand import generate_for_candidates
from smart_crosswalk_sumo.preprocess import preprocess_inputs


REQUIRED_METRICS = [
    "pedestrian_count",
    "pedestrian_waiting_time_mean",
    "crossing_time",
    "elderly_pedestrian_count",
    "smart_green_extension_count",
    "pedestrian_delay",
    "vehicle_delay",
    "surrounding_queue_total_avg",
]

VEHICLE_COMPARE_METRICS = [
    "avg_vehicle_delay_sec",
    "vehicle_delay",
    "avg_queue_length",
    "max_queue_length",
    "surrounding_queue_total_avg",
    "surrounding_queue_total_max",
    "total_vehicle_delay",
]


def read_csv_safe(path: Path) -> pd.DataFrame:
    if not path.exists() or path.stat().st_size == 0:
        return pd.DataFrame()
    try:
        return pd.read_csv(path)
    except EmptyDataError:
        return pd.DataFrame()


def choose_smoke_candidates(
    candidates_df: pd.DataFrame,
    audit_df: pd.DataFrame,
    smoke_n: int,
) -> pd.DataFrame:
    valid_ids = set(
        audit_df[
            (audit_df["stage"] == "pre_route_generation")
            & (audit_df["validation_status"] == "valid")
        ]["crosswalk_id"].astype(str)
    )
    ranked = candidates_df.copy()
    ranked["crosswalk_id"] = ranked["crosswalk_id"].astype(str)
    valid = ranked[ranked["crosswalk_id"].isin(valid_ids)].copy()
    if valid.empty:
        valid = ranked.copy()
    return valid.head(smoke_n).copy()


def merge_failed_cases(root_output_dir: Path, extra_paths: list[Path]) -> pd.DataFrame:
    frames = []
    root_failed = read_csv_safe(root_output_dir / "failed_cases.csv")
    if not root_failed.empty:
        frames.append(root_failed)
    for path in extra_paths:
        frame = read_csv_safe(path)
        if not frame.empty:
            frames.append(frame)
    if not frames:
        merged = pd.DataFrame(columns=["crosswalk_id", "scenario", "seed", "step", "error"])
    else:
        merged = pd.concat(frames, ignore_index=True, sort=False).drop_duplicates()
    merged.to_csv(root_output_dir / "failed_cases.csv", index=False)
    return merged


def build_smoke_results(
    smoke_candidates: pd.DataFrame,
    seed_values: list[int],
    full_seed_df: pd.DataFrame,
    failed_df: pd.DataFrame,
) -> pd.DataFrame:
    rows: list[dict[str, object]] = []
    key_fail = failed_df.copy()
    if not key_fail.empty:
        key_fail["crosswalk_id"] = key_fail["crosswalk_id"].astype(str)
        if "scenario" in key_fail.columns:
            key_fail["scenario"] = key_fail["scenario"].astype(str)
        if "seed" in key_fail.columns:
            key_fail["seed"] = key_fail["seed"].astype(str)
    full = full_seed_df.copy()
    if not full.empty and {"crosswalk_id", "scenario", "seed"}.issubset(full.columns):
        full["crosswalk_id"] = full["crosswalk_id"].astype(str)
        full["scenario"] = full["scenario"].astype(str)
        full["seed"] = full["seed"].astype(int)
    else:
        full = pd.DataFrame(columns=["crosswalk_id", "scenario", "seed"])

    for cw_id in smoke_candidates["crosswalk_id"].astype(str):
        for seed in seed_values:
            for scenario in ("baseline", "smart"):
                rec = {
                    "crosswalk_id": cw_id,
                    "seed": seed,
                    "scenario": scenario,
                    "run_status": "failed",
                    "reason": "",
                }
                row = full[
                    (full["crosswalk_id"] == cw_id)
                    & (full["seed"] == int(seed))
                    & (full["scenario"] == scenario)
                ]
                if not row.empty:
                    rec["run_status"] = "success"
                    metric_ok = []
                    metric_positive = []
                    first = row.iloc[0]
                    for metric in REQUIRED_METRICS:
                        value = first.get(metric, np.nan)
                        present = pd.notna(value)
                        positive = bool(present and float(value) > 0) if metric != "smart_green_extension_count" else bool(present and float(value) >= 0)
                        rec[metric] = value
                        rec[f"{metric}_present"] = int(present)
                        rec[f"{metric}_positive"] = int(positive)
                        metric_ok.append(present)
                        metric_positive.append(positive)
                    rec["required_metrics_present"] = int(all(metric_ok))
                    rec["required_metrics_positive"] = int(all(metric_positive))
                else:
                    fail = key_fail[
                        (key_fail.get("crosswalk_id", pd.Series(dtype=str)).astype(str) == cw_id)
                        & (key_fail.get("scenario", pd.Series(dtype=str)).astype(str).isin([scenario, "", "nan", "None"]))
                        & (key_fail.get("seed", pd.Series(dtype=str)).astype(str).isin([str(seed), "", "nan", "None"]))
                    ]
                    if not fail.empty:
                        rec["reason"] = " | ".join(
                            fail.apply(
                                lambda x: f"{x.get('step', 'unknown_step')}: {x.get('error', '')}",
                                axis=1,
                            ).tolist()
                        )
                rows.append(rec)
    return pd.DataFrame(rows)


def build_comparison(full_seed_df: pd.DataFrame, vehicle_seed_df: pd.DataFrame) -> pd.DataFrame:
    if full_seed_df.empty or vehicle_seed_df.empty:
        cols = ["crosswalk_id", "seed", "scenario"]
        return pd.DataFrame(columns=cols)
    keys = ["crosswalk_id", "seed", "scenario"]
    full = full_seed_df[keys + [c for c in VEHICLE_COMPARE_METRICS if c in full_seed_df.columns]].copy()
    veh = vehicle_seed_df[keys + [c for c in VEHICLE_COMPARE_METRICS if c in vehicle_seed_df.columns]].copy()
    merged = full.merge(veh, on=keys, how="inner", suffixes=("_full", "_vehicle_only"))
    for metric in VEHICLE_COMPARE_METRICS:
        left = f"{metric}_full"
        right = f"{metric}_vehicle_only"
        if left in merged.columns and right in merged.columns:
            merged[f"delta_{metric}"] = pd.to_numeric(merged[left], errors="coerce") - pd.to_numeric(
                merged[right], errors="coerce"
            )
    return merged


def write_report(
    output_dir: Path,
    smoke_candidates: pd.DataFrame,
    audit_df: pd.DataFrame,
    invalid_df: pd.DataFrame,
    smoke_df: pd.DataFrame,
    comparison_df: pd.DataFrame,
    failed_df: pd.DataFrame,
) -> None:
    valid_pre = audit_df[
        (audit_df["stage"] == "pre_route_generation") & (audit_df["validation_status"] == "valid")
    ]
    smoke_success = smoke_df.groupby("crosswalk_id")["run_status"].apply(
        lambda s: int(set(s.tolist()) == {"success"})
    )
    success_candidate_count = int(smoke_success.sum()) if not smoke_success.empty else 0
    smoke_candidate_count = int(smoke_df["crosswalk_id"].nunique()) if not smoke_df.empty else 0
    success_ratio = (success_candidate_count / smoke_candidate_count * 100.0) if smoke_candidate_count else 0.0

    invalid_reason_counts = (
        invalid_df["reason"].fillna("unknown").value_counts().to_dict() if not invalid_df.empty else {}
    )
    failed_reason_counts = (
        failed_df["step"].fillna("unknown").value_counts().to_dict() if not failed_df.empty and "step" in failed_df.columns else {}
    )
    remaining_error_counts = (
        audit_df[audit_df["validation_status"] != "valid"]["invalid_reason"].fillna("unknown").value_counts().to_dict()
        if not audit_df.empty
        else {}
    )

    metric_present_ratio = (
        float(smoke_df["required_metrics_present"].mean() * 100.0)
        if "required_metrics_present" in smoke_df.columns and not smoke_df.empty
        else 0.0
    )
    metric_positive_ratio = (
        float(smoke_df["required_metrics_positive"].mean() * 100.0)
        if "required_metrics_positive" in smoke_df.columns and not smoke_df.empty
        else 0.0
    )
    failed_reason_lines = [f"- {k}: {v}" for k, v in failed_reason_counts.items()] or ["- none"]
    remaining_error_lines = [f"- {k}: {v}" for k, v in remaining_error_counts.items()] or ["- none"]
    invalid_reason_lines = [f"- {k}: {v}" for k, v in invalid_reason_counts.items()] or ["- none"]

    comparison_lines = []
    for metric in VEHICLE_COMPARE_METRICS:
        delta_col = f"delta_{metric}"
        if delta_col in comparison_df.columns:
            series = pd.to_numeric(comparison_df[delta_col], errors="coerce").dropna()
            if not series.empty:
                comparison_lines.append(
                    f"- {delta_col}: mean={series.mean():.4f}, median={series.median():.4f}, max_abs={series.abs().max():.4f}"
                )
    if not comparison_lines:
        comparison_lines = ["- comparable rows not available"]

    lines = [
        "# Pedestrian Full Simulation Validation Report",
        "",
        f"- created_at: {datetime.now().isoformat(timespec='seconds')}",
        f"- smoke_candidates: {smoke_candidate_count}",
        f"- valid_pre_route_candidates: {len(valid_pre)}",
        f"- full_simulation_success_candidates: {success_candidate_count}/{smoke_candidate_count} ({success_ratio:.1f}%)",
        "",
        "## 1) Full simulation success ratio",
        f"- pedestrian 포함 full simulation이 정상 실행된 후보 비율: {success_ratio:.1f}% ({success_candidate_count}/{smoke_candidate_count})",
        "",
        "## 2) Main failure causes",
        *failed_reason_lines,
        "",
        "## 3) Remaining pedestrian connectivity error types",
        *remaining_error_lines,
        "",
        "## 4) Invalid pedestrian candidates",
        *invalid_reason_lines,
        "",
        "## 5) Pedestrian metric availability",
        f"- required metrics present ratio: {metric_present_ratio:.1f}%",
        f"- required metrics positive ratio: {metric_positive_ratio:.1f}%",
        "",
        "## 6) Vehicle-only vs full simulation",
        *comparison_lines,
        "",
        "## 7) Expansion decision",
        (
            "- 다음 단계에서 전체 후보로 확장 가능: yes"
            if smoke_candidate_count > 0 and success_candidate_count == smoke_candidate_count and metric_present_ratio >= 100.0
            else "- 다음 단계에서 전체 후보로 확장 가능: conditional"
        ),
        "- conditional인 경우 invalid 후보 skip 정책과 smoke failure 원인 정리 후 확장 권장",
        "",
        "## Output Files",
        f"- {output_dir / 'pedestrian_connectivity_audit.csv'}",
        f"- {output_dir / 'invalid_pedestrian_candidates.csv'}",
        f"- {output_dir / 'full_simulation_smoke_results.csv'}",
        f"- {output_dir / 'full_vs_vehicle_only_comparison.csv'}",
        f"- {output_dir / 'failed_cases.csv'}",
    ]
    (output_dir / "pedestrian_full_simulation_validation_report.md").write_text(
        "\n".join(lines) + "\n",
        encoding="utf-8",
    )


def copy_subset_nets(src_dir: Path, dst_dir: Path, candidate_ids: list[str]) -> None:
    if dst_dir.exists():
        shutil.rmtree(dst_dir)
    dst_dir.mkdir(parents=True, exist_ok=True)
    for cw_id in candidate_ids:
        shutil.copytree(src_dir / f"cw_{cw_id}", dst_dir / f"cw_{cw_id}")


def run_validation(args: argparse.Namespace) -> Path:
    run_dir = Path(args.run_dir)
    output_dir = run_dir / "outputs"
    nets_dir = Path(args.reuse_nets_dir) if args.reuse_nets_dir else run_dir / "sumo_nets"
    full_output_dir = run_dir / "outputs_full_smoke"
    vehicle_output_dir = run_dir / "outputs_vehicle_only_smoke"
    vehicle_nets_dir = run_dir / "sumo_nets_vehicle_only"
    run_dir.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)

    if args.candidates_csv:
        source_candidates_csv = Path(args.candidates_csv)
        candidates_df = pd.read_csv(source_candidates_csv)
        candidates_csv = output_dir / "candidates.csv"
        candidates_df.to_csv(candidates_csv, index=False)
    else:
        candidates_df, _, _ = preprocess_inputs(args.t1, args.t2, output_dir, args.top_n, None)
        candidates_csv = output_dir / "candidates.csv"

    if not args.skip_build_networks:
        build_all_networks(
            candidates_csv=candidates_csv,
            nets_dir=nets_dir,
            output_dir=output_dir,
            force=args.force_networks,
            network_radius_m=args.network_radius_m,
            network_mode=args.network_mode,
            admin_polygon_path=args.admin_polygon_path,
            buffer_m=args.buffer_m,
            corridor_whitelist=None,
        )

    generate_for_candidates(
        candidates_csv=candidates_csv,
        nets_dir=nets_dir,
        output_dir=output_dir,
        seeds=tuple(args.seeds),
        sim_duration=args.sim_duration,
        warmup=args.warmup,
        step_length=args.sumo_step_length,
        demand_profile=args.demand_profile,
        traffic_counts_csv=None,
        representative_day_id=None,
        model_parameters_path=args.model_assumptions,
        vehicle_only=False,
    )

    audit_df = read_csv_safe(output_dir / "pedestrian_connectivity_audit.csv")
    invalid_df = read_csv_safe(output_dir / "invalid_pedestrian_candidates.csv")
    smoke_candidates = choose_smoke_candidates(candidates_df, audit_df, args.smoke_n)
    smoke_candidates_csv = output_dir / "smoke_candidates.csv"
    smoke_candidates.to_csv(smoke_candidates_csv, index=False)

    collect_all(
        candidates_csv=smoke_candidates_csv,
        output_dir=full_output_dir,
        nets_dir=nets_dir,
        sim_duration=args.sim_duration,
        warmup=args.warmup,
        seeds=tuple(args.seeds),
        traci_step_length=args.traci_step_length,
        traffic_measure_radius_m=args.traffic_measure_radius_m,
        model_parameters_path=args.model_assumptions,
        vehicle_only=False,
    )

    copy_subset_nets(nets_dir, vehicle_nets_dir, smoke_candidates["crosswalk_id"].astype(str).tolist())
    generate_for_candidates(
        candidates_csv=smoke_candidates_csv,
        nets_dir=vehicle_nets_dir,
        output_dir=vehicle_output_dir,
        seeds=tuple(args.seeds),
        sim_duration=args.sim_duration,
        warmup=args.warmup,
        step_length=args.sumo_step_length,
        demand_profile=args.demand_profile,
        traffic_counts_csv=None,
        representative_day_id=None,
        model_parameters_path=args.model_assumptions,
        vehicle_only=True,
    )
    collect_all(
        candidates_csv=smoke_candidates_csv,
        output_dir=vehicle_output_dir,
        nets_dir=vehicle_nets_dir,
        sim_duration=args.sim_duration,
        warmup=args.warmup,
        seeds=tuple(args.seeds),
        traci_step_length=args.traci_step_length,
        traffic_measure_radius_m=args.traffic_measure_radius_m,
        model_parameters_path=args.model_assumptions,
        vehicle_only=True,
    )

    failed_df = merge_failed_cases(
        output_dir,
        [
            full_output_dir / "failed_cases.csv",
            vehicle_output_dir / "failed_cases.csv",
        ],
    )
    full_seed_df = read_csv_safe(full_output_dir / "simulation_results_seed.csv")
    vehicle_seed_df = read_csv_safe(vehicle_output_dir / "simulation_results_seed.csv")
    smoke_df = build_smoke_results(smoke_candidates, args.seeds, full_seed_df, failed_df)
    smoke_df.to_csv(output_dir / "full_simulation_smoke_results.csv", index=False)

    comparison_df = build_comparison(full_seed_df, vehicle_seed_df)
    comparison_df.to_csv(output_dir / "full_vs_vehicle_only_comparison.csv", index=False)

    write_report(
        output_dir=output_dir,
        smoke_candidates=smoke_candidates,
        audit_df=audit_df,
        invalid_df=invalid_df,
        smoke_df=smoke_df,
        comparison_df=comparison_df,
        failed_df=failed_df,
    )
    return run_dir


def parse_args() -> argparse.Namespace:
    base_dir = Path(__file__).resolve().parents[1] / "smart_crosswalk_sumo"
    default_run_dir = Path(__file__).resolve().parents[1] / "result" / "pedestrian_full_validation_2026-05-07"
    parser = argparse.ArgumentParser()
    parser.add_argument("--t1", default=str(base_dir / "data" / "T1_accident_crosswalk.csv"))
    parser.add_argument("--t2", default=str(base_dir / "data" / "T2_crosswalk_features.csv"))
    parser.add_argument("--run_dir", default=str(default_run_dir))
    parser.add_argument("--candidates_csv", default=None)
    parser.add_argument("--reuse_nets_dir", default=None)
    parser.add_argument("--top_n", type=int, default=12)
    parser.add_argument("--smoke_n", type=int, default=5)
    parser.add_argument("--seeds", nargs="+", type=int, default=[42])
    parser.add_argument("--sim_duration", type=int, default=600)
    parser.add_argument("--warmup", type=int, default=120)
    parser.add_argument("--sumo_step_length", type=float, default=1.0)
    parser.add_argument("--traci_step_length", type=float, default=0.1)
    parser.add_argument("--traffic_measure_radius_m", type=float, default=500.0)
    parser.add_argument("--network_radius_m", type=float, default=None)
    parser.add_argument("--network_mode", default="local")
    parser.add_argument(
        "--admin_polygon_path",
        default=str(base_dir / "data" / "junggu_admin_boundary.geojson"),
    )
    parser.add_argument("--buffer_m", type=float, default=1000.0)
    parser.add_argument("--demand_profile", default="average")
    parser.add_argument(
        "--model_assumptions",
        default=str(base_dir / "config" / "model_assumptions.yaml"),
    )
    parser.add_argument("--force_networks", action="store_true")
    parser.add_argument("--skip_build_networks", action="store_true")
    return parser.parse_args()


if __name__ == "__main__":
    run_path = run_validation(parse_args())
    print(run_path)
