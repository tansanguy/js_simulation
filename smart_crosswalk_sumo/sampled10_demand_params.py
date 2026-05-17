from __future__ import annotations

import json
import hashlib
import shutil
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

import pandas as pd

from .csv_outputs import ensure_csv_output_layout, write_csv_bundle
from .vehicle_demand_policy import summarize_vehicle_route_artifact


def _sha256_file(path: Path) -> str:
    if not path.exists():
        return ""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _load_csv_any(paths: list[Path]) -> pd.DataFrame:
    for path in paths:
        if path.exists():
            try:
                return pd.read_csv(path, encoding="utf-8-sig")
            except Exception:
                continue
    return pd.DataFrame()


def _xml_count(path: Path, tag: str) -> int:
    if not path.exists():
        return 0
    try:
        root = ET.parse(path).getroot()
    except Exception:
        return 0
    return sum(1 for _ in root.iter(tag))


def _first_nonempty(*values: Any) -> str:
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text and text.lower() not in {"nan", "none", "null"}:
            return text
    return ""


def _load_route_rows(output_dir: Path) -> pd.DataFrame:
    return _load_csv_any(
        [
            output_dir / "simulation_results_seed.csv",
            output_dir / "baseline_smart_seed_results.csv",
            output_dir / "phase6_smoke_baseline_results.csv",
            output_dir / "phase6_smoke_smart_results.csv",
        ]
    )


def _trip_records_from_route(route_file: Path, trip_file: Path) -> None:
    root = ET.parse(route_file).getroot()
    trip_root = ET.Element("routes")

    for vehicle in root.findall("vehicle"):
        trip_id = str(vehicle.attrib.get("id", "") or "").strip()
        depart = str(vehicle.attrib.get("depart", "0") or "0").strip()
        vehicle_type = str(vehicle.attrib.get("type", "") or vehicle.attrib.get("vClass", "") or "").strip()
        route_elem = vehicle.find("route")
        edges = str(route_elem.attrib.get("edges", "") if route_elem is not None else "").split()
        origin = edges[0] if edges else ""
        destination = edges[-1] if edges else ""
        trip = ET.SubElement(
            trip_root,
            "trip",
            {
                "id": trip_id,
                "depart": depart,
                "from": origin,
                "to": destination,
            },
        )
        if vehicle_type:
            trip.set("type", vehicle_type)

    if not trip_root.findall("trip"):
        trip_root.append(ET.Comment("empty trip file generated from sampled10 repair"))
    trip_file.parent.mkdir(parents=True, exist_ok=True)
    ET.ElementTree(trip_root).write(trip_file, encoding="utf-8", xml_declaration=True)


def _copy_if_missing_or_changed(source: Path, target: Path) -> None:
    if target.exists() and _sha256_file(source) == _sha256_file(target):
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)


def build_sampled10_demand_params(output_dir: str | Path) -> pd.DataFrame:
    output_path = Path(output_dir).expanduser().resolve()
    layout = ensure_csv_output_layout(output_path)
    run_metadata = _load_json(output_path / "run_metadata.json")
    seed_df = _load_route_rows(output_path)
    first_seed_row = seed_df.iloc[0].to_dict() if not seed_df.empty else {}

    route_file_text = _first_nonempty(
        first_seed_row.get("generated_vehicle_route_file"),
        output_path / "demand_vehicle.rou.xml",
    )
    route_file = Path(route_file_text).expanduser().resolve()
    if not route_file.exists():
        raise FileNotFoundError(route_file)

    net_file = _first_nonempty(
        run_metadata.get("net_file"),
        first_seed_row.get("batch_network_file"),
        first_seed_row.get("vehicle_net_file"),
    )
    net_path = Path(net_file).expanduser().resolve() if net_file else Path()

    sim_duration = int(float(_first_nonempty(run_metadata.get("sim_duration"), first_seed_row.get("sim_duration"), 600)))
    warmup = int(float(_first_nonempty(run_metadata.get("warmup"), first_seed_row.get("warmup"), 0)))
    seed = int(float(_first_nonempty(run_metadata.get("seed"), first_seed_row.get("seed"), 0)))

    trip_file = route_file.with_name(route_file.name.replace(".rou.xml", ".trips.xml"))
    if not trip_file.exists() or _xml_count(trip_file, "trip") != _xml_count(route_file, "vehicle"):
        _trip_records_from_route(route_file, trip_file)

    ped_file = output_path / "demand_pedestrian.rou.xml"
    if not ped_file.exists():
        raise FileNotFoundError(ped_file)
    ped_alias_file = output_path / f"peds_seed{seed}.rou.xml"
    _copy_if_missing_or_changed(ped_file, ped_alias_file)
    vehicle_count = _xml_count(route_file, "vehicle")
    trip_count = _xml_count(trip_file, "trip")
    ped_count = _xml_count(ped_file, "person")
    walk_count = _xml_count(ped_file, "walk")
    if vehicle_count != trip_count:
        raise ValueError(f"vehicle route/trip count mismatch: route={vehicle_count}, trip={trip_count}")
    if ped_count <= 0 or walk_count <= 0 or ped_count != walk_count:
        raise ValueError(f"pedestrian route count mismatch: person={ped_count}, walk={walk_count}")
    veh_per_hour = float(vehicle_count) * 3600.0 / max(float(sim_duration), 1.0)
    ped_lambda = float(ped_count) * 3600.0 / max(float(sim_duration), 1.0)

    route_summary = summarize_vehicle_route_artifact(
        route_file,
        net_path,
        scenario_name=str(_first_nonempty(first_seed_row.get("scenario_name"), run_metadata.get("scenario_name"), "sampled10_group")),
        road_allocated_count_600s=int(vehicle_count),
        road_allocated_flow_vph=float(veh_per_hour),
        vehicle_type="passenger",
        passenger_ratio=1.0,
        allocation_basis="sampled10_route_file_count",
        road_group="all_network",
        vehicle_type_split="passenger:1.0|taxi:0.0|bus:0.0|truck:0.0",
        road_group_type="full_network",
        edge_group_mapping_status="full_network_fallback",
        network_edge_group="full_network",
    ) if net_file else {
        "route_file_sha256": "",
        "trip_file_sha256": "",
        "net_file_sha256": "",
        "unique_depart_edges": 0,
        "unique_arrival_edges": 0,
        "unique_route_edges": 0,
        "network_edge_coverage_ratio": 0.0,
    }

    manifest_crosswalk_id = _first_nonempty(run_metadata.get("manifest_crosswalk_id"), first_seed_row.get("crosswalk_id"), "sampled10_seed")
    demand_df = pd.DataFrame(
        [
            {
                "crosswalk_id": manifest_crosswalk_id,
                "seed": seed,
                "experiment_mode": _first_nonempty(run_metadata.get("experiment_mode"), "sampled"),
                "scenario_name": _first_nonempty(first_seed_row.get("scenario_name"), "sampled10_group"),
                "demand_profile": _first_nonempty(first_seed_row.get("demand_profile"), "sampled10_group"),
                "sim_duration_sec": sim_duration,
                "warmup_sec": warmup,
                "candidate_csv": _first_nonempty(run_metadata.get("candidate_csv")),
                "net_file": _first_nonempty(run_metadata.get("net_file"), str(net_path) if net_file else ""),
                "output_dir": str(output_path),
                "manifest_row_role": _first_nonempty(run_metadata.get("manifest_row_role"), "baseline_placeholder"),
                "manifest_crosswalk_id": _first_nonempty(run_metadata.get("manifest_crosswalk_id"), manifest_crosswalk_id),
                "include_vehicles": bool(run_metadata.get("include_vehicles", True)),
                "metric_sample_interval_s": float(run_metadata.get("metric_sample_interval_s", 10.0)),
                "vehicle_sample_interval_s": float(run_metadata.get("vehicle_sample_interval_s", 10.0)),
                "traci_step_length": float(run_metadata.get("traci_step_length", 0.1)),
                "traffic_measure_radius_m": float(run_metadata.get("traffic_measure_radius_m", 500.0)),
                "extension_increment": float(run_metadata.get("extension_increment", 5.0)),
                "max_extensions": int(run_metadata.get("max_extensions", 1)),
                "ped_repeat_count": int(run_metadata.get("ped_repeat_count", 5)),
                "ped_repeat_spacing_sec": float(run_metadata.get("ped_repeat_spacing_sec", 2.0)),
                "phase_aligned_ped_depart": bool(run_metadata.get("phase_aligned_ped_depart", True)),
                "veh_per_hour": veh_per_hour,
                "veh_source": "sampled10_route_file_count",
                "vehicle_flow_scale": 1.0,
                "total_vehicle_flow_vph": veh_per_hour,
                "total_vehicle_count_600s": vehicle_count,
                "expected_vehicle_count_for_duration": vehicle_count,
                "vehicle_type": "passenger",
                "passenger_ratio": 1.0,
                "allocation_basis": "sampled10_route_file_count",
                "vehicle_type_split": "passenger:1.0|taxi:0.0|bus:0.0|truck:0.0",
                "road_group": "all_network",
                "road_name": "all_network",
                "road_group_type": "full_network",
                "edge_group_mapping_status": "full_network_fallback",
                "network_edge_group": "full_network",
                "road_allocated_count_600s": vehicle_count,
                "road_allocated_flow_vph": veh_per_hour,
                "pedestrian_scale": 1.0,
                "pedestrian_count_600s": ped_count,
                "generated_pedestrian_count": ped_count,
                "generated_vehicle_count": vehicle_count,
                "generated_vehicle_route_file": str(route_file),
                "generated_vehicle_trip_file": str(trip_file),
                "vehicle_net_file": _first_nonempty(run_metadata.get("net_file"), str(net_path) if net_file else ""),
                "route_file_sha256": route_summary.get("route_file_sha256", ""),
                "trip_file_sha256": route_summary.get("trip_file_sha256", ""),
                "ped_file_sha256": _sha256_file(ped_file),
                "net_file_sha256": route_summary.get("net_file_sha256", ""),
                "demand_reused": False,
                "demand_reused_from": "",
                "demand_reuse_reason": "generated_sampled10_route_file",
                "unique_depart_edges": route_summary.get("unique_depart_edges", 0),
                "unique_arrival_edges": route_summary.get("unique_arrival_edges", 0),
                "unique_route_edges": route_summary.get("unique_route_edges", 0),
                "network_edge_coverage_ratio": route_summary.get("network_edge_coverage_ratio", 0.0),
                "pedestrian_scale_source": "sampled10_actual_ped_count",
                "ped_lambda": ped_lambda,
                "elderly_ratio": pd.NA,
                "ped_count": ped_count,
                "pedestrian_arrival_rate_multiplier": 1.0,
                "vehicle_volume_multiplier": 1.0,
                "walking_speed_profile": "base",
                "generated_pedestrian_route_file": str(ped_file),
                "generated_pedestrian_alias_file": str(ped_alias_file),
                "generated_vehicle_trip_count": trip_count,
                "generated_vehicle_route_count": vehicle_count,
                "generated_pedestrian_route_count": ped_count,
                "generated_pedestrian_walk_count": walk_count,
            }
        ]
    )

    write_csv_bundle(
        demand_df,
        layout.results / "demand_params.csv",
        mirrors=[output_path / "demand_params.csv"],
    )
    return demand_df


def main() -> None:
    import argparse
    import sys

    parser = argparse.ArgumentParser(description="Repair sampled10 demand_params.csv from existing route outputs.")
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args()
    build_sampled10_demand_params(args.output_dir)
    sys.exit(0)


if __name__ == "__main__":
    main()
