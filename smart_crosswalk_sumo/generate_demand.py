from __future__ import annotations

import argparse
import hashlib
import math
import os
import shutil
import subprocess
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

try:
    from .demand_scenarios import (
        DEFAULT_DEMAND_SCENARIO_NAME,
        DEFAULT_PEDESTRIAN_SCALE,
        DEFAULT_VEHICLE_FLOW_SCALE,
        build_demand_payload,
        build_fixed_departure_times,
        build_fixed_type_assignments,
        resolve_demand_scenario_name,
    )
    from .csv_outputs import ensure_csv_output_layout, write_csv_bundle
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
    from .vehicle_demand_policy import (
        build_vehicle_demand_audit,
        resolve_vehicle_policy_summary,
        summarize_vehicle_route_artifact,
    )
except ImportError:
    from demand_scenarios import (
        DEFAULT_DEMAND_SCENARIO_NAME,
        DEFAULT_PEDESTRIAN_SCALE,
        DEFAULT_VEHICLE_FLOW_SCALE,
        build_demand_payload,
        build_fixed_departure_times,
        build_fixed_type_assignments,
        resolve_demand_scenario_name,
    )
    from csv_outputs import ensure_csv_output_layout, write_csv_bundle
    from vehicle_demand_policy import (
        build_vehicle_demand_audit,
        resolve_vehicle_policy_summary,
        summarize_vehicle_route_artifact,
    )
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
    DEFAULT_DEMAND_SCENARIO_NAME: {
        "veh_multiplier": DEFAULT_VEHICLE_FLOW_SCALE,
        "pedestrian_scale": DEFAULT_PEDESTRIAN_SCALE,
    },
}


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _copy_file_if_exists(source: Path, target: Path) -> None:
    if source.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)


def _safe_int_value(value: Any, default: int = -1) -> int:
    try:
        if value is None:
            return default
        if isinstance(value, float) and math.isnan(value):
            return default
        text = str(value).strip()
        if not text or text.lower() == "nan":
            return default
        return int(float(text))
    except Exception:
        return default


def _safe_float_value(value: Any, default: float = float("nan")) -> float:
    try:
        if value is None:
            return default
        if isinstance(value, float) and math.isnan(value):
            return default
        text = str(value).strip()
        if not text or text.lower() == "nan":
            return default
        return float(text)
    except Exception:
        return default


def _resolve_reuse_sources(reuse_demand_dir: str | Path | None) -> tuple[Path | None, Path | None]:
    if not reuse_demand_dir:
        return None, None
    root = Path(reuse_demand_dir).expanduser().resolve()
    candidates = [
        (root, root / "sumo_nets"),
        (root / "outputs", root / "sumo_nets"),
        (root, root / "outputs" / "sumo_nets"),
        (root / "outputs", root.parent / "sumo_nets"),
    ]
    for output_dir, nets_dir in candidates:
        if (output_dir / "demand_params.csv").exists() and nets_dir.exists():
            return output_dir, nets_dir
    return None, None


def _demand_reuse_reason(
    source_row: dict[str, Any] | None,
    *,
    cw_id: str,
    seed: int,
    scenario_name: str,
    sim_duration: int,
    net_file_sha256: str,
    ped_enabled: bool,
    generated_pedestrian_count: int,
    pedestrian_count_600s: int,
    vehicle_type: str,
    passenger_ratio: float,
    walking_speed_profile: str,
    pedestrian_scale_source: str,
    elderly_ratio: float,
    veh_per_hour: float,
) -> str | None:
    if not source_row:
        return "missing_reuse_metadata"
    if str(source_row.get("crosswalk_id", "")) != str(cw_id):
        return "crosswalk_id_mismatch"
    if _safe_int_value(source_row.get("seed", -1), default=-1) != int(seed):
        return "seed_mismatch"
    if str(source_row.get("scenario_name", "")) != str(scenario_name):
        return "scenario_name_mismatch"
    if _safe_int_value(source_row.get("sim_duration_sec", -1), default=-1) != int(sim_duration):
        return "sim_duration_mismatch"
    if str(source_row.get("net_file_sha256", "")) != str(net_file_sha256):
        return "net_file_sha256_mismatch"
    source_ped_count = _safe_int_value(source_row.get("generated_pedestrian_count", 0), default=0)
    if bool(source_ped_count > 0) != bool(ped_enabled):
        return "pedestrian_enabled_mismatch"
    if _safe_int_value(source_row.get("generated_pedestrian_count", -1), default=-1) != int(generated_pedestrian_count):
        return "generated_pedestrian_count_mismatch"
    if _safe_int_value(source_row.get("pedestrian_count_600s", -1), default=-1) != int(pedestrian_count_600s):
        return "pedestrian_count_600s_mismatch"
    if str(source_row.get("vehicle_type", "")) != str(vehicle_type):
        return "vehicle_type_mismatch"
    source_passenger_ratio = _safe_float_value(source_row.get("passenger_ratio", float("nan")))
    if not math.isfinite(source_passenger_ratio) or abs(source_passenger_ratio - float(passenger_ratio)) > 1e-9:
        return "passenger_ratio_mismatch"
    if str(source_row.get("walking_speed_profile", "")) != str(walking_speed_profile):
        return "walking_speed_profile_mismatch"
    if str(source_row.get("pedestrian_scale_source", "")) != str(pedestrian_scale_source):
        return "pedestrian_scale_source_mismatch"
    source_elderly_ratio = _safe_float_value(source_row.get("elderly_ratio", float("nan")))
    if not math.isfinite(source_elderly_ratio) or abs(source_elderly_ratio - float(elderly_ratio)) > 1e-9:
        return "elderly_ratio_mismatch"
    source_veh_per_hour = _safe_float_value(source_row.get("veh_per_hour", float("nan")))
    if not math.isfinite(source_veh_per_hour) or abs(source_veh_per_hour - float(veh_per_hour)) > 1e-9:
        return "veh_per_hour_mismatch"
    return None


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


def fallback_vehicle_rate(row: pd.Series) -> float:
    aadt = max(1000.0, float(row["estimated_aadt"]))
    return float(aadt / 24.0 / max(float(row["lane_count"]), 1.0))


def get_demand_params(
    row: pd.Series,
    seed: int,
    scenario_name: str,
    model_params: dict[str, dict[str, Any]],
    observed_veh_per_hour: float | None,
    sim_duration: int,
    sensitivity_config: dict[str, Any] | None = None,
    vehicle_policy_summary: dict[str, Any] | None = None,
) -> dict[str, float | str]:
    scenario_name = resolve_demand_scenario_name(scenario_name)
    if scenario_name == DEFAULT_DEMAND_SCENARIO_NAME:
        policy = vehicle_policy_summary or resolve_vehicle_policy_summary(scenario_name)
        veh_per_hour = float(policy["total_vehicle_flow_vph"])
        veh_source = "policy_fixed_road_06_22_share"
    else:
        if observed_veh_per_hour is not None and observed_veh_per_hour > 0:
            veh_per_hour = float(observed_veh_per_hour)
            veh_source = "observed_15min"
        else:
            veh_per_hour = fallback_vehicle_rate(row)
            veh_source = "AADT_fallback"
        policy = {
            "total_vehicle_flow_vph": float(veh_per_hour),
            "total_vehicle_count_600s": int(round(float(veh_per_hour) * float(sim_duration) / 3600.0)),
            "vehicle_type": "passenger",
            "passenger_ratio": 1.0,
            "allocation_basis": "legacy_candidate_rate",
            "vehicle_type_split": "passenger:1.0|taxi:0.0|bus:0.0|truck:0.0",
            "road_allocated_count_600s": int(round(float(veh_per_hour) * float(sim_duration) / 3600.0)),
            "road_allocated_flow_vph": float(veh_per_hour),
            "vehicle_flow_scale": float(DEFAULT_VEHICLE_FLOW_SCALE),
        }

    risk_score_value = row.get("risk_score", np.nan)
    risk_score = float(risk_score_value) if pd.notna(risk_score_value) else None
    ped_payload = build_demand_payload(
        str(row.get("admin_dong", "")),
        float(row["elderly_ratio"]),
        sim_duration,
        risk_score=risk_score,
    )
    effective_model_params = apply_parameter_value_overrides(
        model_params,
        (sensitivity_config or {}).get("parameter_overrides"),
    )
    elderly_ratio = float(row["elderly_ratio"])
    elderly_ratio_override = (sensitivity_config or {}).get("elderly_ratio_override")
    if elderly_ratio_override is not None:
        elderly_ratio = float(elderly_ratio_override)
    else:
        elderly_ratio *= float((sensitivity_config or {}).get("elderly_ratio_multiplier", 1.0) or 1.0)
    elderly_ratio = float(min(1.0, max(0.0, elderly_ratio)))
    generated_ped_count = int(ped_payload["generated_pedestrian_count"])
    ped_lambda = float(generated_ped_count * 3600.0 / max(float(sim_duration), 1.0))
    ped_mean_gap_sec = float(sim_duration / max(generated_ped_count, 1)) if generated_ped_count > 0 else 0.0

    return {
        "scenario_name": scenario_name,
        "demand_profile": scenario_name,
        "sim_duration_sec": int(sim_duration),
        "veh_per_hour": float(veh_per_hour),
        "veh_source": veh_source,
        "vehicle_flow_scale": float(policy.get("vehicle_flow_scale", DEFAULT_VEHICLE_FLOW_SCALE)),
        "total_vehicle_flow_vph": float(policy.get("total_vehicle_flow_vph", veh_per_hour)),
        "total_vehicle_count_600s": int(policy.get("total_vehicle_count_600s", 0)),
        "expected_vehicle_count_for_duration": int(
            round(float(policy.get("total_vehicle_flow_vph", veh_per_hour)) * float(sim_duration) / 3600.0)
        ),
        "vehicle_type": str(policy.get("vehicle_type", "passenger")),
        "passenger_ratio": float(policy.get("passenger_ratio", 1.0)),
        "allocation_basis": str(policy.get("allocation_basis", "legacy_candidate_rate")),
        "vehicle_type_split": str(policy.get("vehicle_type_split", "passenger:1.0|taxi:0.0|bus:0.0|truck:0.0")),
        "road_group": str(policy.get("road_group", "all_network")),
        "road_name": str(policy.get("road_name", "all_network")),
        "road_group_type": str(policy.get("road_group_type", "full_network")),
        "edge_group_mapping_status": str(policy.get("edge_group_mapping_status", "full_network_fallback")),
        "network_edge_group": str(policy.get("network_edge_group", "full_network")),
        "road_allocated_count_600s": int(policy.get("road_allocated_count_600s", 0)),
        "road_allocated_flow_vph": float(policy.get("road_allocated_flow_vph", veh_per_hour)),
        "pedestrian_scale": float(DEFAULT_PEDESTRIAN_SCALE),
        "pedestrian_scale_source": str(ped_payload["pedestrian_scale_source"]),
        "pedestrian_count_600s": int(ped_payload["pedestrian_count_600s"]),
        "generated_pedestrian_count": int(generated_ped_count),
        "ped_mean_gap_sec": float(ped_mean_gap_sec),
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
        "pedestrian_arrival_rate_multiplier": float(DEFAULT_PEDESTRIAN_SCALE),
        "vehicle_volume_multiplier": float(DEFAULT_VEHICLE_FLOW_SCALE),
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
) -> int:
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
    try:
        root = ET.parse(output_file).getroot()
        return int(len(root.findall("vehicle")))
    except Exception:
        return 0


def generate_pedestrian_demand(
    params: dict[str, float | str],
    ped_route: dict[str, str],
    output_file: str | Path,
    sim_duration: int = 1800,
    seed: int = 42,
) -> int:
    elderly_ratio = float(params["elderly_ratio"])
    generated_count = int(params.get("generated_pedestrian_count", 0))
    normal_speed = float(params["normal_ped_speed_mps"])
    elderly_speed = float(params["elderly_ped_speed_mps"])
    elderly_startup_delay = float(params["elderly_startup_delay_sec"])
    depart_times = build_fixed_departure_times(generated_count, sim_duration)
    type_assignments = build_fixed_type_assignments(generated_count, elderly_ratio)

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

    for ped_id, (t, vtype) in enumerate(zip(depart_times, type_assignments, strict=False)):
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

    ET.indent(root, space="  ")
    ET.ElementTree(root).write(output_file, encoding="utf-8", xml_declaration=True)
    return generated_count


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
    scenario_name: str = DEFAULT_DEMAND_SCENARIO_NAME,
    traffic_counts_csv: str | Path | None = None,
    representative_day_id: str | None = None,
    model_parameters_path: str | Path | None = None,
    vehicle_only: bool = False,
    sensitivity_config: dict[str, Any] | None = None,
    reuse_demand_dir: str | Path | None = None,
    force_demand: bool = False,
) -> pd.DataFrame:
    model_params = load_model_parameters(model_parameters_path)
    counts_df = load_traffic_counts(traffic_counts_csv, representative_day_id)
    policy_summary = resolve_vehicle_policy_summary(scenario_name)

    candidates = pd.read_csv(candidates_csv)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    csv_layout = ensure_csv_output_layout(output_dir)
    rows: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    demand_audit_rows: list[dict[str, Any]] = []
    invalid_ped_rows: list[dict[str, Any]] = []
    ped_connectivity_rows: list[dict[str, Any]] = []
    reuse_output_dir, reuse_nets_dir = _resolve_reuse_sources(reuse_demand_dir)
    source_demand_lookup: dict[tuple[str, str], dict[str, Any]] = {}
    if reuse_output_dir is not None:
        source_demand_csv = reuse_output_dir / "demand_params.csv"
        if source_demand_csv.exists():
            source_demand_df = pd.read_csv(source_demand_csv)
            for row in source_demand_df.to_dict(orient="records"):
                source_demand_lookup[(str(row.get("crosswalk_id", "")), str(row.get("seed", "")))] = row

    if scenario_name == "main_realistic_stress" and int(sim_duration) != 600:
        print(
            f"[demand][WARN] scenario_name=main_realistic_stress expects sim_duration=600, got {sim_duration}",
            flush=True,
        )

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
            print(f"[demand] cw_{cw_id} seed={seed} scenario={scenario_name}", flush=True)
            params = get_demand_params(
                row,
                seed,
                scenario_name,
                model_params,
                observed_veh_per_hour,
                sim_duration,
                sensitivity_config,
                policy_summary,
            )
            vehicle_file = cw_dir / f"routes_seed{seed}.rou.xml"
            pedestrian_file = cw_dir / f"peds_seed{seed}.rou.xml"
            calibrator_file = cw_dir / f"calibrator_seed{seed}.add.xml"
            ped_enabled = (not vehicle_only) and ped_candidate_valid
            try:
                source_row = source_demand_lookup.get((str(cw_id), str(seed)))
                source_cw_dir = reuse_nets_dir / f"cw_{cw_id}" if reuse_nets_dir is not None else None
                reuse_reason = None
                reuse_from = ""
                if not force_demand and source_row is not None and source_cw_dir is not None:
                    source_vehicle_file = source_cw_dir / vehicle_file.name
                    source_trip_file = source_cw_dir / vehicle_file.name.replace(".rou.xml", ".trips.xml")
                    source_pedestrian_file = source_cw_dir / pedestrian_file.name
                    source_calibrator_file = source_cw_dir / calibrator_file.name
                    source_baseline_sumocfg = source_cw_dir / f"baseline_seed{seed}.sumocfg"
                    source_smart_sumocfg = source_cw_dir / f"smart_seed{seed}.sumocfg"
                    if (
                        source_vehicle_file.exists()
                        and source_trip_file.exists()
                        and source_pedestrian_file.exists()
                        and source_baseline_sumocfg.exists()
                        and source_smart_sumocfg.exists()
                    ):
                        reuse_reason = _demand_reuse_reason(
                            source_row,
                            cw_id=str(cw_id),
                            seed=seed,
                            scenario_name=scenario_name,
                            sim_duration=sim_duration,
                            net_file_sha256=_sha256_file(net_file),
                            ped_enabled=ped_enabled,
                            generated_pedestrian_count=int(params.get("generated_pedestrian_count", 0)),
                            pedestrian_count_600s=int(params.get("pedestrian_count_600s", 0)),
                            vehicle_type=str(params["vehicle_type"]),
                            passenger_ratio=float(params["passenger_ratio"]),
                            walking_speed_profile=str(params["walking_speed_profile"]),
                            pedestrian_scale_source=str(params["pedestrian_scale_source"]),
                            elderly_ratio=float(params["elderly_ratio"]),
                            veh_per_hour=float(params["veh_per_hour"]),
                        )
                        if reuse_reason is None:
                            expected_route_sha = str(source_row.get("route_file_sha256", ""))
                            expected_trip_sha = str(source_row.get("trip_file_sha256", ""))
                            expected_ped_sha = str(source_row.get("ped_file_sha256", ""))
                            actual_route_sha = _sha256_file(source_vehicle_file)
                            actual_trip_sha = _sha256_file(source_trip_file)
                            actual_ped_sha = _sha256_file(source_pedestrian_file)
                            if expected_route_sha and expected_route_sha != actual_route_sha:
                                reuse_reason = "route_file_sha256_mismatch"
                            elif expected_trip_sha and expected_trip_sha != actual_trip_sha:
                                reuse_reason = "trip_file_sha256_mismatch"
                            elif expected_ped_sha and expected_ped_sha != actual_ped_sha:
                                reuse_reason = "ped_file_sha256_mismatch"
                        if reuse_reason is None:
                            _copy_file_if_exists(source_vehicle_file, vehicle_file)
                            _copy_file_if_exists(source_trip_file, vehicle_file.with_name(vehicle_file.name.replace(".rou.xml", ".trips.xml")))
                            _copy_file_if_exists(source_pedestrian_file, pedestrian_file)
                            _copy_file_if_exists(source_calibrator_file, calibrator_file)
                            reuse_from = str(source_cw_dir)
                        else:
                            print(
                                f"[demand][WARN] cw_{cw_id} seed={seed} reuse skipped: {reuse_reason}",
                                flush=True,
                            )

                if reuse_reason is None and source_row is not None and source_cw_dir is not None and reuse_from:
                    generated_vehicle_count = _safe_int_value(source_row.get("generated_vehicle_count", 0), default=0)
                    ped_count = _safe_int_value(source_row.get("generated_pedestrian_count", 0), default=0)
                    route_summary = summarize_vehicle_route_artifact(
                        vehicle_file,
                        net_file,
                        scenario_name=scenario_name,
                        road_allocated_count_600s=int(policy_summary["road_allocated_count_600s"]),
                        road_allocated_flow_vph=float(policy_summary["road_allocated_flow_vph"]),
                        vehicle_type=str(params["vehicle_type"]),
                        passenger_ratio=float(params["passenger_ratio"]),
                        allocation_basis=str(params["allocation_basis"]),
                        road_group="all_network",
                        vehicle_type_split=str(params["vehicle_type_split"]),
                        road_group_type="full_network",
                        edge_group_mapping_status="full_network_fallback",
                        network_edge_group="full_network",
                    )
                    ped_enabled = (not vehicle_only) and ped_candidate_valid
                    if not ped_enabled:
                        ET.ElementTree(ET.Element("routes")).write(
                            pedestrian_file,
                            encoding="utf-8",
                            xml_declaration=True,
                        )
                    calib_path = calibrator_file if calibrator_file.exists() else None
                    additional_files = [str(calib_path.resolve())] if calib_path else None
                else:
                    generated_vehicle_count = generate_vehicle_routes(
                        params,
                        net_file,
                        vehicle_file,
                        sim_duration,
                        seed,
                    )
                    route_summary = summarize_vehicle_route_artifact(
                        vehicle_file,
                        net_file,
                        scenario_name=scenario_name,
                        road_allocated_count_600s=int(policy_summary["road_allocated_count_600s"]),
                        road_allocated_flow_vph=float(policy_summary["road_allocated_flow_vph"]),
                        vehicle_type=str(params["vehicle_type"]),
                        passenger_ratio=float(params["passenger_ratio"]),
                        allocation_basis=str(params["allocation_basis"]),
                        road_group="all_network",
                        vehicle_type_split=str(params["vehicle_type_split"]),
                        road_group_type="full_network",
                        edge_group_mapping_status="full_network_fallback",
                        network_edge_group="full_network",
                    )
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
                baseline_sumocfg_file = str((cw_dir / f"baseline_seed{seed}.sumocfg").resolve())
                smart_sumocfg_file = str((cw_dir / f"smart_seed{seed}.sumocfg").resolve())
                route_summary = summarize_vehicle_route_artifact(
                    vehicle_file,
                    net_file,
                    scenario_name=scenario_name,
                    road_allocated_count_600s=int(policy_summary["road_allocated_count_600s"]),
                    road_allocated_flow_vph=float(policy_summary["road_allocated_flow_vph"]),
                    vehicle_type=str(params["vehicle_type"]),
                    passenger_ratio=float(params["passenger_ratio"]),
                    allocation_basis=str(params["allocation_basis"]),
                    road_group="all_network",
                    vehicle_type_split=str(params["vehicle_type_split"]),
                    road_group_type="full_network",
                    edge_group_mapping_status="full_network_fallback",
                    network_edge_group="full_network",
                )
                route_file_sha256 = _sha256_file(vehicle_file) if vehicle_file.exists() else ""
                trip_file = vehicle_file.with_name(vehicle_file.name.replace(".rou.xml", ".trips.xml"))
                trip_file_sha256 = _sha256_file(trip_file) if trip_file.exists() else ""
                ped_file_sha256 = _sha256_file(pedestrian_file) if pedestrian_file.exists() else ""
                expected_vehicle_count = int(params["expected_vehicle_count_for_duration"])
                if expected_vehicle_count > 0:
                    count_delta = abs(int(generated_vehicle_count) - expected_vehicle_count)
                    if scenario_name == "main_realistic_stress" and int(sim_duration) == 600 and int(generated_vehicle_count) != 3480:
                        print(
                            "[demand][WARN] "
                            f"cw_{cw_id} seed={seed} main_realistic_stress 600s expects generated_vehicle_count=3480, "
                            f"got {generated_vehicle_count}",
                            flush=True,
                        )
                    if count_delta > max(1, int(round(expected_vehicle_count * 0.01))):
                        print(
                            "[demand][WARN] "
                            f"cw_{cw_id} seed={seed} generated_vehicle_count={generated_vehicle_count} "
                            f"expected_vehicle_count_for_duration={expected_vehicle_count}",
                            flush=True,
                        )
                rows.append(
                    {
                        "crosswalk_id": cw_id,
                        "seed": seed,
                        "scenario_name": params["scenario_name"],
                        "sim_duration_sec": int(params["sim_duration_sec"]),
                        "demand_profile": params["demand_profile"],
                        "allocation_basis": params["allocation_basis"],
                        "veh_per_hour": params["veh_per_hour"],
                        "veh_source": params["veh_source"],
                        "vehicle_flow_scale": params["vehicle_flow_scale"],
                        "total_vehicle_flow_vph": params["total_vehicle_flow_vph"],
                        "total_vehicle_count_600s": params["total_vehicle_count_600s"],
                        "expected_vehicle_count_for_duration": params["expected_vehicle_count_for_duration"],
                        "vehicle_type": params["vehicle_type"],
                        "passenger_ratio": params["passenger_ratio"],
                        "vehicle_type_split": params["vehicle_type_split"],
                        "road_group": params["road_group"],
                        "road_name": params["road_name"],
                        "road_group_type": params["road_group_type"],
                        "edge_group_mapping_status": params["edge_group_mapping_status"],
                        "network_edge_group": params["network_edge_group"],
                        "road_allocated_count_600s": params["road_allocated_count_600s"],
                        "road_allocated_flow_vph": params["road_allocated_flow_vph"],
                        "pedestrian_scale": params["pedestrian_scale"],
                        "pedestrian_count_600s": params["pedestrian_count_600s"],
                        "generated_pedestrian_count": ped_count,
                        "generated_vehicle_count": generated_vehicle_count,
                        "generated_vehicle_route_file": str(vehicle_file.resolve()),
                        "generated_vehicle_trip_file": str(trip_file.resolve()),
                        "vehicle_net_file": str(net_file.resolve()),
                        "baseline_sumocfg_file": baseline_sumocfg_file,
                        "smart_sumocfg_file": smart_sumocfg_file,
                        "route_file_sha256": route_file_sha256,
                        "trip_file_sha256": trip_file_sha256,
                        "ped_file_sha256": ped_file_sha256,
                        "net_file_sha256": route_summary["net_file_sha256"],
                        "demand_reused": bool(reuse_from),
                        "demand_reused_from": reuse_from,
                        "demand_reuse_reason": reuse_reason or ("generated_new" if not reuse_from else "reused"),
                        "unique_depart_edges": route_summary["unique_depart_edges"],
                        "unique_arrival_edges": route_summary["unique_arrival_edges"],
                        "unique_route_edges": route_summary["unique_route_edges"],
                        "network_edge_coverage_ratio": route_summary["network_edge_coverage_ratio"],
                        "pedestrian_scale_source": params["pedestrian_scale_source"],
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
        "scenario_name",
        "sim_duration_sec",
        "demand_profile",
        "allocation_basis",
        "veh_per_hour",
        "veh_source",
        "vehicle_flow_scale",
        "total_vehicle_flow_vph",
        "total_vehicle_count_600s",
        "expected_vehicle_count_for_duration",
        "vehicle_type",
        "passenger_ratio",
        "vehicle_type_split",
        "road_group",
        "road_name",
        "road_group_type",
        "edge_group_mapping_status",
        "network_edge_group",
        "road_allocated_count_600s",
        "road_allocated_flow_vph",
        "pedestrian_scale",
        "pedestrian_count_600s",
        "generated_pedestrian_count",
        "generated_vehicle_count",
        "generated_vehicle_route_file",
        "generated_vehicle_trip_file",
        "vehicle_net_file",
        "baseline_sumocfg_file",
        "smart_sumocfg_file",
        "route_file_sha256",
        "trip_file_sha256",
        "ped_file_sha256",
        "net_file_sha256",
        "demand_reused",
        "demand_reused_from",
        "demand_reuse_reason",
        "unique_depart_edges",
        "unique_arrival_edges",
        "unique_route_edges",
        "network_edge_coverage_ratio",
        "pedestrian_scale_source",
        "ped_lambda",
        "elderly_ratio",
        "ped_count",
        "pedestrian_arrival_rate_multiplier",
        "vehicle_volume_multiplier",
        "walking_speed_profile",
    ]
    demand_df = pd.DataFrame(rows, columns=demand_cols)
    write_csv_bundle(
        demand_df,
        csv_layout.results / "demand_params.csv",
        mirrors=[output_dir / "demand_params.csv"],
    )
    audit_cols = ["edge_id","demand_source","volume","confidence_level"]
    demand_audit_df = pd.DataFrame(demand_audit_rows, columns=audit_cols).drop_duplicates()
    write_csv_bundle(
        demand_audit_df,
        csv_layout.results / "demand_source_audit.csv",
        mirrors=[output_dir / "demand_source_audit.csv"],
    )
    write_csv_bundle(
        demand_audit_df,
        csv_layout.results / "route_generation_audit.csv",
        mirrors=[output_dir / "route_generation_audit.csv"],
    )
    build_vehicle_demand_audit(
        output_dir,
        demand_df,
        scenario_name=scenario_name,
        legacy_notes=[
            "Per-candidate observed vehicle-rate lookup remains in the code for legacy scenarios, but the fixed policy does not use it.",
            "Route generation remains full-network randomTrips output; road-group tables are policy metadata.",
        ],
    )

    if failures:
        failed_path = output_dir / "failed_cases.csv"
        write_csv_bundle(
            english_output_columns(pd.DataFrame(failures)),
            failed_path,
            mirrors=[csv_layout.internal / "failed_cases.csv"],
        )
    write_csv_bundle(
        pd.DataFrame(ped_connectivity_rows),
        csv_layout.results / "pedestrian_connectivity_audit.csv",
        mirrors=[output_dir / "pedestrian_connectivity_audit.csv"],
    )
    write_csv_bundle(
        pd.DataFrame(ped_connectivity_rows),
        csv_layout.results / "pedestrian_route_connectivity_audit.csv",
        mirrors=[output_dir / "pedestrian_route_connectivity_audit.csv"],
    )
    invalid_ped_df = pd.DataFrame(
        invalid_ped_rows,
        columns=["crosswalk_id", "seed", "reason", "detail"],
    ).drop_duplicates()
    write_csv_bundle(
        invalid_ped_df,
        csv_layout.results / "invalid_pedestrian_candidates.csv",
        mirrors=[output_dir / "invalid_pedestrian_candidates.csv"],
    )
    write_csv_bundle(
        invalid_ped_df,
        csv_layout.results / "invalid_pedestrian_routes.csv",
        mirrors=[output_dir / "invalid_pedestrian_routes.csv"],
    )
    write_csv_bundle(
        invalid_ped_df,
        csv_layout.results / "skipped_pedestrian_routes.csv",
        mirrors=[output_dir / "skipped_pedestrian_routes.csv"],
    )
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
    parser.add_argument("--scenario_name", default=DEFAULT_DEMAND_SCENARIO_NAME)
    parser.add_argument("--demand_profile", default=None, help=argparse.SUPPRESS)
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
        args.scenario_name,
        args.traffic_counts,
        args.representative_day_id,
        args.model_parameters,
        args.vehicle_only,
        sensitivity_config,
    )


if __name__ == "__main__":
    main()
