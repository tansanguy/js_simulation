from __future__ import annotations

import argparse
import math
import os
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

try:
    from .model_config import get_parameter_value, load_model_parameters
    from .network_utils import (
        load_metadata,
        normalized_sumo_home,
        sumo_env,
        validate_pedestrian_connectivity,
    )
    from .output_schema import english_output_columns
    from .sensitivity import (
        apply_parameter_value_overrides,
        load_sensitivity_scenarios,
        resolve_sensitivity_case,
    )
except ImportError:
    from model_config import get_parameter_value, load_model_parameters
    from network_utils import (
        load_metadata,
        normalized_sumo_home,
        sumo_env,
        validate_pedestrian_connectivity,
    )
    from output_schema import english_output_columns
    from sensitivity import apply_parameter_value_overrides, load_sensitivity_scenarios, resolve_sensitivity_case


DEMAND_PROFILES = {
    "average": {
        "veh_multiplier": 1.0,
        "ped_lambda_min": 100,
        "ped_lambda_max": 600,
    },
    "seoul_commute_peak": {
        "veh_multiplier": 2.0,
        "ped_lambda_min": 650,
        "ped_lambda_max": 1100,
    },
}


def load_traffic_counts(
    traffic_counts_csv: str | Path | None,
    representative_day_id: str | None = None,
) -> pd.DataFrame:
    if not traffic_counts_csv:
        return pd.DataFrame()
    path = Path(traffic_counts_csv)
    if not path.exists():
        raise FileNotFoundError(f"교통량 파일이 없습니다: {path}")
    df = pd.read_csv(path)
    expected = {
        "count_point_id",
        "datetime",
        "start_time_sec",
        "end_time_sec",
        "road_name",
        "direction",
        "edge_id",
        "volume",
        "source",
    }
    missing = expected - set(df.columns)
    if missing:
        raise ValueError(f"traffic_counts 컬럼 누락: {sorted(missing)}")

    if representative_day_id:
        if "representative_day_id" in df.columns:
            filtered = df[df["representative_day_id"].astype(str) == str(representative_day_id)]
        else:
            filtered = df[df["datetime"].astype(str).str.startswith(str(representative_day_id))]
        if not filtered.empty:
            df = filtered

    df["start_time_sec"] = pd.to_numeric(df["start_time_sec"], errors="coerce").fillna(0).astype(float)
    df["end_time_sec"] = pd.to_numeric(df["end_time_sec"], errors="coerce").fillna(900).astype(float)
    df["volume"] = pd.to_numeric(df["volume"], errors="coerce").fillna(0).astype(float)
    return df


def observed_vehicle_rate_for_candidate(
    metadata: dict[str, Any],
    counts_df: pd.DataFrame,
    sim_duration: int,
) -> tuple[float | None, list[dict[str, Any]]]:
    if counts_df.empty:
        return None, []
    conflict_edges = set(str(e) for e in metadata.get("vehicle_conflict_edges", []))
    approach_lanes = set(str(l) for l in metadata.get("approach_lanes", []))
    approach_edges = {lane_id.rsplit("_", 1)[0] for lane_id in approach_lanes if "_" in lane_id}
    target_edges = conflict_edges | approach_edges
    if not target_edges:
        return None, []

    matched = counts_df[counts_df["edge_id"].astype(str).isin(target_edges)].copy()
    if matched.empty:
        return None, []

    matched["interval_sec"] = (matched["end_time_sec"] - matched["start_time_sec"]).clip(lower=1.0)
    matched["volume_per_hour"] = matched["volume"] * (3600.0 / matched["interval_sec"])

    # Use robust center to avoid one outlier count point dominating.
    veh_per_hour = float(np.nanmedian(matched["volume_per_hour"].to_numpy(dtype=float)))
    if not np.isfinite(veh_per_hour) or veh_per_hour <= 0:
        return None, []

    audits: list[dict[str, Any]] = []
    grouped = matched.groupby("edge_id", as_index=False)["volume"].sum()
    for row in grouped.itertuples(index=False):
        audits.append(
            {
                "edge_id": str(row.edge_id),
                "demand_source": "observed_15min",
                "volume": float(row.volume),
                "confidence_level": "high" if len(matched) >= 3 else "medium",
            }
        )
    return veh_per_hour, audits


def fallback_vehicle_rate(row: pd.Series, rng: np.random.Generator, profile_name: str) -> float:
    profile = DEMAND_PROFILES[profile_name]
    aadt_noisy = max(1000, float(row["estimated_aadt"]) + int(rng.integers(-3000, 3001)))
    hourly_veh = aadt_noisy / 24 / max(float(row["lane_count"]), 1.0)
    return float(hourly_veh * 1.8 * float(profile["veh_multiplier"]))


def get_demand_params(
    row: pd.Series,
    seed: int,
    demand_profile: str,
    model_params: dict[str, dict[str, Any]],
    observed_veh_per_hour: float | None,
    sensitivity_config: dict[str, Any] | None = None,
) -> dict[str, float | str]:
    if demand_profile not in DEMAND_PROFILES:
        valid = ", ".join(sorted(DEMAND_PROFILES))
        raise ValueError(f"Unknown demand_profile={demand_profile!r}. Valid profiles: {valid}")

    profile = DEMAND_PROFILES[demand_profile]
    rng = np.random.default_rng(seed)
    if observed_veh_per_hour is not None and observed_veh_per_hour > 0:
        veh_per_hour = float(observed_veh_per_hour)
        veh_source = "observed_15min"
    else:
        veh_per_hour = fallback_vehicle_rate(row, rng, demand_profile)
        veh_source = "AADT_fallback"

    ped_lambda = int(rng.integers(int(profile["ped_lambda_min"]), int(profile["ped_lambda_max"]) + 1))
    effective_model_params = apply_parameter_value_overrides(
        model_params,
        (sensitivity_config or {}).get("parameter_overrides"),
    )

    pedestrian_arrival_rate_multiplier = float(
        (sensitivity_config or {}).get("pedestrian_arrival_rate_multiplier", 1.0) or 1.0
    )
    vehicle_volume_multiplier = float(
        (sensitivity_config or {}).get("vehicle_volume_multiplier", 1.0) or 1.0
    )
    elderly_ratio = float(row["elderly_ratio"])
    elderly_ratio_override = (sensitivity_config or {}).get("elderly_ratio_override")
    if elderly_ratio_override is not None:
        elderly_ratio = float(elderly_ratio_override)
    else:
        elderly_ratio *= float((sensitivity_config or {}).get("elderly_ratio_multiplier", 1.0) or 1.0)
    elderly_ratio = float(min(1.0, max(0.0, elderly_ratio)))
    ped_lambda = max(1, int(round(float(ped_lambda) * pedestrian_arrival_rate_multiplier)))
    veh_per_hour = max(1.0, float(veh_per_hour) * vehicle_volume_multiplier)

    return {
        "demand_profile": demand_profile,
        "veh_per_hour": float(veh_per_hour),
        "veh_source": veh_source,
        "ped_mean_gap_sec": float(3600 / max(ped_lambda, 1)),
        "elderly_ratio": elderly_ratio,
        "ped_lambda": float(ped_lambda),
        "normal_ped_speed_mps": float(
            get_parameter_value(effective_model_params, "normal_ped_speed_mps", 1.2)
        ),
        "elderly_ped_speed_mps": float(
            get_parameter_value(effective_model_params, "elderly_ped_speed_mps", 0.9)
        ),
        "elderly_startup_delay_sec": float(
            get_parameter_value(effective_model_params, "elderly_startup_delay_sec", 1.5)
        ),
        "pedestrian_arrival_rate_multiplier": pedestrian_arrival_rate_multiplier,
        "vehicle_volume_multiplier": vehicle_volume_multiplier,
        "walking_speed_profile": str(
            (sensitivity_config or {}).get("walking_speed_profile_name", "base")
        ),
    }


def random_trips_script() -> str:
    sumo_home = normalized_sumo_home()
    if not sumo_home:
        raise RuntimeError("SUMO_HOME이 설정되어 있지 않습니다.")
    candidates = [
        Path(sumo_home) / "tools" / "randomTrips.py",
        Path(sumo_home) / "share" / "sumo" / "tools" / "randomTrips.py",
    ]
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    raise RuntimeError(f"randomTrips.py를 찾지 못했습니다: {sumo_home}")


def generate_vehicle_routes(
    params: dict[str, float | str],
    net_file: str | Path,
    output_file: str | Path,
    sim_duration: int = 1800,
    seed: int = 42,
) -> None:
    period = max(0.1, 3600 / max(float(params["veh_per_hour"]), 1.0))
    output_file = Path(output_file)
    trip_file = output_file.with_name(output_file.stem.replace(".rou", "") + ".trips.xml")
    log_file_path = output_file.with_name(f"demand_seed{seed}.log")
    with log_file_path.open("w", encoding="utf-8") as log_file:
        subprocess.run(
            [
                "python3",
                random_trips_script(),
                "-n",
                str(net_file),
                "-o",
                str(trip_file),
                "-r",
                str(output_file),
                "--period",
                str(period),
                "--seed",
                str(seed),
                "--begin",
                "0",
                "--end",
                str(sim_duration),
                "--vehicle-class",
                "passenger",
                "--validate",
            ],
            check=True,
            env=sumo_env(),
            stdout=log_file,
            stderr=subprocess.STDOUT,
        )


def generate_pedestrian_demand(
    params: dict[str, float | str],
    ped_route: dict[str, str],
    output_file: str | Path,
    sim_duration: int = 1800,
    seed: int = 42,
) -> int:
    rng = np.random.default_rng(seed + 1000)
    elderly_ratio = float(params["elderly_ratio"])
    mean_gap = float(params["ped_mean_gap_sec"])
    normal_speed = float(params["normal_ped_speed_mps"])
    elderly_speed = float(params["elderly_ped_speed_mps"])
    elderly_startup_delay = float(params["elderly_startup_delay_sec"])

    root = ET.Element("routes")
    ET.SubElement(
        root,
        "vType",
        {
            "id": "adult",
            "vClass": "pedestrian",
            "minGap": "0.25",
            "width": "0.5",
            "length": "0.25",
            "maxSpeed": f"{normal_speed:.3f}",
            "speedDev": "0.1",
        },
    )
    ET.SubElement(
        root,
        "vType",
        {
            "id": "elderly",
            "vClass": "pedestrian",
            "minGap": "0.25",
            "width": "0.5",
            "length": "0.25",
            "maxSpeed": f"{elderly_speed:.3f}",
            "speedDev": "0.15",
            "startupDelay": f"{elderly_startup_delay:.2f}",
            "color": "255,0,0",
        },
    )

    t = float(rng.exponential(mean_gap))
    ped_id = 0
    while t < sim_duration:
        vtype = "elderly" if rng.random() < elderly_ratio else "adult"
        person = ET.SubElement(
            root,
            "person",
            {"id": f"ped_{ped_id}", "depart": f"{t:.2f}", "type": vtype},
        )
        ET.SubElement(
            person,
            "walk",
            {"from": ped_route["from_edge"], "to": ped_route["to_edge"]},
        )
        t += float(rng.exponential(mean_gap))
        ped_id += 1

    ET.indent(root, space="  ")
    ET.ElementTree(root).write(output_file, encoding="utf-8", xml_declaration=True)
    return ped_id


def build_calibrator_additional(
    metadata: dict[str, Any],
    counts_df: pd.DataFrame,
    output_file: Path,
) -> Path | None:
    if counts_df.empty:
        return None
    target_edges = set(str(e) for e in metadata.get("vehicle_conflict_edges", []))
    if not target_edges:
        return None
    filtered = counts_df[counts_df["edge_id"].astype(str).isin(target_edges)].copy()
    if filtered.empty:
        return None

    root = ET.Element("additional")
    for i, row in enumerate(filtered.itertuples(index=False), start=1):
        begin = max(0.0, float(row.start_time_sec))
        end = max(begin + 1.0, float(row.end_time_sec))
        veh_per_hour = float(row.volume) * (3600.0 / max(end - begin, 1.0))
        ET.SubElement(
            root,
            "calibrator",
            {
                "id": f"cal_{i}",
                "edge": str(row.edge_id),
                "pos": "5",
                "begin": f"{begin:.2f}",
                "end": f"{end:.2f}",
                "vehsPerHour": f"{veh_per_hour:.3f}",
                "type": "passenger",
            },
        )

    ET.indent(root, space="  ")
    ET.ElementTree(root).write(output_file, encoding="utf-8", xml_declaration=True)
    return output_file


def write_sumocfg(
    net_file: str | Path,
    vehicle_route_file: str | Path,
    pedestrian_route_file: str | Path | None,
    output_file: str | Path,
    sim_duration: int,
    warmup: int,
    step_length: float,
    additional_files: list[str] | None = None,
    vehicle_only: bool = False,
) -> None:
    root = ET.Element("configuration")
    input_elem = ET.SubElement(root, "input")
    ET.SubElement(input_elem, "net-file", {"value": str(Path(net_file).resolve())})
    route_files = [str(Path(vehicle_route_file).resolve())]
    if not vehicle_only and pedestrian_route_file is not None:
        route_files.append(str(Path(pedestrian_route_file).resolve()))
    ET.SubElement(input_elem, "route-files", {"value": ",".join(route_files)})
    if additional_files:
        ET.SubElement(input_elem, "additional-files", {"value": ",".join(additional_files)})
    time_elem = ET.SubElement(root, "time")
    ET.SubElement(time_elem, "begin", {"value": "0"})
    ET.SubElement(time_elem, "end", {"value": str(sim_duration + warmup)})
    ET.SubElement(time_elem, "step-length", {"value": str(step_length)})
    processing_elem = ET.SubElement(root, "processing")
    ET.SubElement(processing_elem, "time-to-teleport", {"value": "300"})
    if not vehicle_only:
        ET.SubElement(processing_elem, "pedestrian.model", {"value": "striping"})
    report_elem = ET.SubElement(root, "report")
    ET.SubElement(report_elem, "no-step-log", {"value": "true"})
    ET.SubElement(report_elem, "duration-log.disable", {"value": "true"})
    ET.indent(root, space="  ")
    ET.ElementTree(root).write(output_file, encoding="utf-8", xml_declaration=True)


def generate_for_candidates(
    candidates_csv: str | Path,
    nets_dir: str | Path = "sumo_nets",
    output_dir: str | Path = "outputs",
    seeds: tuple[int, ...] = (42, 43, 44),
    sim_duration: int = 1800,
    warmup: int = 300,
    step_length: float = 1.0,
    demand_profile: str = "average",
    traffic_counts_csv: str | Path | None = None,
    representative_day_id: str | None = None,
    model_parameters_path: str | Path | None = None,
    vehicle_only: bool = False,
    sensitivity_config: dict[str, Any] | None = None,
) -> pd.DataFrame:
    model_params = load_model_parameters(model_parameters_path)
    counts_df = load_traffic_counts(traffic_counts_csv, representative_day_id)

    candidates = pd.read_csv(candidates_csv)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    demand_audit_rows: list[dict[str, Any]] = []
    invalid_ped_rows: list[dict[str, Any]] = []
    ped_connectivity_rows: list[dict[str, Any]] = []

    for _, row in candidates.iterrows():
        cw_id = row["crosswalk_id"]
        cw_dir = Path(nets_dir) / f"cw_{cw_id}"
        net_file = cw_dir / "network.net.xml"
        metadata_file = cw_dir / "metadata.json"
        try:
            metadata = load_metadata(metadata_file)
        except Exception as exc:
            failures.append({"crosswalk_id": cw_id, "step": "load_metadata", "error": str(exc)})
            continue

        observed_veh_per_hour, matched_audits = observed_vehicle_rate_for_candidate(
            metadata, counts_df, sim_duration
        )
        if matched_audits:
            demand_audit_rows.extend(matched_audits)
        else:
            for edge_id in metadata.get("vehicle_conflict_edges", []):
                demand_audit_rows.append(
                    {
                        "edge_id": str(edge_id),
                        "demand_source": "AADT_fallback",
                        "volume": float(row.get("추정AADT", 0.0)),
                        "confidence_level": "low",
                    }
                )

        ped_validation = validate_pedestrian_connectivity(net_file, metadata, cw_id=cw_id)
        ped_connectivity_rows.append(
            {
                "stage": "pre_route_generation",
                "seed": "",
                **ped_validation,
            }
        )
        ped_candidate_valid = ped_validation["validation_status"] == "valid"
        if not vehicle_only and not ped_candidate_valid:
            invalid_ped_rows.append(
                {
                    "crosswalk_id": cw_id,
                    "seed": "",
                    "reason": ped_validation["invalid_reason"] or "invalid_pedestrian_candidate",
                    "detail": "pre_route_generation connectivity audit failed",
                }
            )

        for seed in seeds:
            print(f"[demand] cw_{cw_id} seed={seed} profile={demand_profile}", flush=True)
            params = get_demand_params(
                row,
                seed,
                demand_profile,
                model_params,
                observed_veh_per_hour,
                sensitivity_config,
            )
            vehicle_file = cw_dir / f"routes_seed{seed}.rou.xml"
            pedestrian_file = cw_dir / f"peds_seed{seed}.rou.xml"
            calibrator_file = cw_dir / f"calibrator_seed{seed}.add.xml"
            try:
                generate_vehicle_routes(params, net_file, vehicle_file, sim_duration, seed)
                ped_count = 0
                ped_enabled = (not vehicle_only) and ped_candidate_valid
                if ped_enabled:
                    ped_count = generate_pedestrian_demand(
                        params,
                        metadata["ped_route"],
                        pedestrian_file,
                        sim_duration,
                        seed,
                    )
                    post_validation = validate_pedestrian_connectivity(
                        net_file,
                        metadata,
                        cw_id=cw_id,
                    )
                    ped_connectivity_rows.append(
                        {
                            "stage": "post_route_generation",
                            "seed": seed,
                            "generated_pedestrian_count": ped_count,
                            **post_validation,
                        }
                    )
                    if post_validation["validation_status"] != "valid":
                        ped_enabled = False
                        ped_count = 0
                        invalid_ped_rows.append(
                            {
                                "crosswalk_id": cw_id,
                                "seed": seed,
                                "reason": post_validation["invalid_reason"] or "invalid_pedestrian_candidate",
                                "detail": "post_route_generation connectivity audit failed",
                            }
                        )
                        ET.ElementTree(ET.Element("routes")).write(
                            pedestrian_file,
                            encoding="utf-8",
                            xml_declaration=True,
                        )
                else:
                    ET.ElementTree(ET.Element("routes")).write(
                        pedestrian_file,
                        encoding="utf-8",
                        xml_declaration=True,
                    )
                calib_path = build_calibrator_additional(metadata, counts_df, calibrator_file)
                additional_files = [str(calib_path.resolve())] if calib_path else None

                for scenario in ("baseline", "smart"):
                    write_sumocfg(
                        net_file,
                        vehicle_file,
                        pedestrian_file,
                        cw_dir / f"{scenario}_seed{seed}.sumocfg",
                        sim_duration,
                        warmup,
                        step_length,
                        additional_files,
                        vehicle_only=vehicle_only,
                    )
                rows.append(
                    {
                        "crosswalk_id": cw_id,
                        "seed": seed,
                        "demand_profile": params["demand_profile"],
                        "veh_per_hour": params["veh_per_hour"],
                        "veh_source": params["veh_source"],
                        "ped_lambda": params["ped_lambda"],
                        "elderly_ratio": params["elderly_ratio"],
                        "ped_count": ped_count,
                        "pedestrian_arrival_rate_multiplier": params["pedestrian_arrival_rate_multiplier"],
                        "vehicle_volume_multiplier": params["vehicle_volume_multiplier"],
                        "walking_speed_profile": params["walking_speed_profile"],
                    }
                )
            except Exception as exc:
                failures.append(
                    {
                        "crosswalk_id": cw_id,
                        "seed": seed,
                        "step": "generate_demand",
                        "error": str(exc),
                    }
                )

    demand_cols = [
        "crosswalk_id",
        "seed",
        "demand_profile",
        "veh_per_hour",
        "veh_source",
        "ped_lambda",
        "elderly_ratio",
        "ped_count",
        "pedestrian_arrival_rate_multiplier",
        "vehicle_volume_multiplier",
        "walking_speed_profile",
    ]
    demand_df = pd.DataFrame(rows, columns=demand_cols)
    demand_df.to_csv(output_dir / "demand_params.csv", index=False)
    audit_cols = ["edge_id","demand_source","volume","confidence_level"]
    pd.DataFrame(demand_audit_rows, columns=audit_cols).drop_duplicates().to_csv(
        output_dir / "demand_source_audit.csv", index=False
    )

    if failures:
        failed_path = output_dir / "failed_cases.csv"
        english_output_columns(pd.DataFrame(failures)).to_csv(
            failed_path,
            mode="a",
            header=not failed_path.exists(),
            index=False,
        )
    pd.DataFrame(ped_connectivity_rows).to_csv(
        output_dir / "pedestrian_connectivity_audit.csv",
        index=False,
    )
    invalid_ped_df = pd.DataFrame(
        invalid_ped_rows,
        columns=["crosswalk_id", "seed", "reason", "detail"],
    ).drop_duplicates()
    invalid_ped_df.to_csv(output_dir / "invalid_pedestrian_candidates.csv", index=False)
    invalid_ped_df.to_csv(output_dir / "invalid_pedestrian_routes.csv", index=False)
    return demand_df


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--candidates", default="outputs/candidates.csv")
    parser.add_argument("--nets_dir", default="sumo_nets")
    parser.add_argument("--output_dir", default="outputs")
    parser.add_argument("--seeds", nargs="+", type=int, default=[42, 43, 44])
    parser.add_argument("--sim_duration", type=int, default=1800)
    parser.add_argument("--warmup", type=int, default=300)
    parser.add_argument("--step_length", type=float, default=1.0)
    parser.add_argument("--demand_profile", choices=sorted(DEMAND_PROFILES), default="average")
    parser.add_argument("--traffic_counts", default=None)
    parser.add_argument("--representative_day_id", default=None)
    parser.add_argument("--model_parameters", default=None)
    parser.add_argument("--vehicle_only", action="store_true")
    parser.add_argument("--sensitivity_scenarios", default=None)
    parser.add_argument("--sensitivity_case", default=None)
    args = parser.parse_args()
    sensitivity_config = None
    if args.sensitivity_case:
        sensitivity_definitions = load_sensitivity_scenarios(args.sensitivity_scenarios)
        sensitivity_config = resolve_sensitivity_case(sensitivity_definitions, args.sensitivity_case)
    generate_for_candidates(
        args.candidates,
        args.nets_dir,
        args.output_dir,
        tuple(args.seeds),
        args.sim_duration,
        args.warmup,
        args.step_length,
        args.demand_profile,
        args.traffic_counts,
        args.representative_day_id,
        args.model_parameters,
        args.vehicle_only,
        sensitivity_config,
    )


if __name__ == "__main__":
    main()
