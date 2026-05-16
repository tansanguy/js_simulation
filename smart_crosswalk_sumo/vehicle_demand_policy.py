from __future__ import annotations

import hashlib
import json
import xml.etree.ElementTree as ET
from functools import lru_cache
from collections import Counter
from pathlib import Path
from typing import Any

import pandas as pd

from .network_utils import edge_function, read_net
from .output_schema import write_csv_utf8_sig


VEHICLE_POLICY_SCENARIO = "main_realistic_stress"
VEHICLE_POLICY_CSV = Path(__file__).resolve().parent / "config" / "vehicle_demand_policy_main_realistic_stress.csv"
VEHICLE_POLICY_ALLOCATION_BASIS = "road_06_22_volume_share"
VEHICLE_POLICY_TOTAL_FLOW_VPH = 20877.0
VEHICLE_POLICY_TOTAL_COUNT_600S = 3480
VEHICLE_POLICY_VEHICLE_TYPE = "passenger"
VEHICLE_POLICY_PASSENGER_RATIO = 1.0
VEHICLE_POLICY_TYPE_SPLIT = "passenger:1.0|taxi:0.0|bus:0.0|truck:0.0"
VEHICLE_POLICY_FLOW_SCALE = 1.15


@lru_cache(maxsize=1)
def load_vehicle_policy_table() -> pd.DataFrame:
    if not VEHICLE_POLICY_CSV.exists():
        raise FileNotFoundError(f"vehicle policy CSV missing: {VEHICLE_POLICY_CSV}")
    df = pd.read_csv(VEHICLE_POLICY_CSV)
    required = {
        "scenario_name",
        "total_vehicle_flow_vph",
        "total_vehicle_count_600s",
        "vehicle_type",
        "passenger_ratio",
        "allocation_basis",
        "road_group",
        "road_name",
        "road_allocated_count_600s",
        "direction_split",
        "road_group_type",
        "edge_group_mapping_status",
        "network_edge_group",
        "vehicle_type_split",
    }
    missing = sorted(required - set(df.columns))
    if missing:
        raise ValueError(f"vehicle policy CSV missing columns: {missing}")
    return df


def resolve_vehicle_policy_table(scenario_name: str) -> pd.DataFrame:
    resolved = str(scenario_name or "").strip()
    if resolved != VEHICLE_POLICY_SCENARIO:
        raise ValueError(f"Only {VEHICLE_POLICY_SCENARIO!r} is supported, got {resolved!r}")
    df = load_vehicle_policy_table().copy()
    df["scenario_name"] = df["scenario_name"].astype(str).str.strip()
    df = df[df["scenario_name"] == VEHICLE_POLICY_SCENARIO].reset_index(drop=True)
    if df.empty:
        raise ValueError(f"No rows found for scenario {VEHICLE_POLICY_SCENARIO!r} in {VEHICLE_POLICY_CSV}")
    return df


def resolve_vehicle_policy_summary(scenario_name: str) -> dict[str, Any]:
    df = resolve_vehicle_policy_table(scenario_name)
    first = df.iloc[0].to_dict()
    road_allocated_count_600s = int(pd.to_numeric(df["road_allocated_count_600s"], errors="coerce").fillna(0).sum())
    road_allocated_flow_vph = float(VEHICLE_POLICY_TOTAL_FLOW_VPH * road_allocated_count_600s / VEHICLE_POLICY_TOTAL_COUNT_600S)
    return {
        "scenario_name": VEHICLE_POLICY_SCENARIO,
        "allocation_basis": VEHICLE_POLICY_ALLOCATION_BASIS,
        "total_vehicle_flow_vph": float(first.get("total_vehicle_flow_vph", VEHICLE_POLICY_TOTAL_FLOW_VPH)),
        "total_vehicle_count_600s": int(first.get("total_vehicle_count_600s", VEHICLE_POLICY_TOTAL_COUNT_600S)),
        "vehicle_type": str(first.get("vehicle_type", VEHICLE_POLICY_VEHICLE_TYPE)),
        "passenger_ratio": float(first.get("passenger_ratio", VEHICLE_POLICY_PASSENGER_RATIO)),
        "vehicle_type_split": str(first.get("vehicle_type_split", VEHICLE_POLICY_TYPE_SPLIT)),
        "vehicle_flow_scale": float(VEHICLE_POLICY_FLOW_SCALE),
        "road_allocated_count_600s": int(road_allocated_count_600s),
        "road_allocated_flow_vph": float(round(road_allocated_flow_vph, 3)),
    }


def scale_count_for_duration(base_count_600s: int, sim_duration: int) -> int:
    if base_count_600s <= 0 or sim_duration <= 0:
        return 0
    return max(0, int(round(float(base_count_600s) * float(sim_duration) / 600.0)))


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _render_table(df: pd.DataFrame, columns: list[str]) -> str:
    if df.empty:
        return "_empty_"
    view = df.reindex(columns=columns)
    try:
        return view.to_markdown(index=False)
    except Exception:
        try:
            return view.to_string(index=False)
        except Exception:
            rows = [", ".join(columns)]
            for _, row in view.iterrows():
                rows.append(", ".join(str(row.get(col, "")) for col in columns))
            return "\n".join(rows)


def _load_trip_data(trip_file: Path) -> pd.DataFrame:
    columns = ["trip_id", "depart_sec", "origin_edge_id", "destination_edge_id"]
    if not trip_file.exists():
        return pd.DataFrame(columns=columns)
    try:
        root = ET.parse(trip_file).getroot()
    except Exception:
        return pd.DataFrame(columns=columns)
    rows = []
    for trip in root.findall("trip"):
        rows.append(
            {
                "trip_id": str(trip.attrib.get("id", "")),
                "depart_sec": float(trip.attrib.get("depart", 0.0) or 0.0),
                "origin_edge_id": str(trip.attrib.get("from", "")),
                "destination_edge_id": str(trip.attrib.get("to", "")),
            }
        )
    return pd.DataFrame(rows, columns=columns)


def _load_vehicle_routes(route_file: Path) -> pd.DataFrame:
    columns = ["vehicle_id", "depart_sec", "route_edges"]
    if not route_file.exists():
        return pd.DataFrame(columns=columns)
    try:
        root = ET.parse(route_file).getroot()
    except Exception:
        return pd.DataFrame(columns=columns)
    rows = []
    for vehicle in root.findall("vehicle"):
        route_elem = vehicle.find("route")
        if route_elem is None:
            continue
        rows.append(
            {
                "vehicle_id": str(vehicle.attrib.get("id", "")),
                "depart_sec": float(vehicle.attrib.get("depart", 0.0) or 0.0),
                "route_edges": route_elem.attrib.get("edges", "").split(),
            }
        )
    return pd.DataFrame(rows, columns=columns)


def _collect_driveable_edges(net: Any) -> list[Any]:
    edges: list[Any] = []
    for edge in net.getEdges():
        edge_id = edge.getID()
        if edge_id.startswith(":"):
            continue
        if edge_function(edge) == "internal":
            continue
        try:
            if not edge.allows("passenger"):
                continue
        except Exception:
            continue
        edges.append(edge)
    return edges


def _edge_bbox(edge: Any) -> tuple[float, float, float, float] | None:
    try:
        bbox = edge.getBoundingBox()
    except Exception:
        return None
    if not bbox:
        return None
    return float(bbox[0]), float(bbox[1]), float(bbox[2]), float(bbox[3])


def _edge_center(edge: Any) -> tuple[float, float] | None:
    bbox = _edge_bbox(edge)
    if bbox is None:
        return None
    return ((bbox[0] + bbox[2]) / 2.0, (bbox[1] + bbox[3]) / 2.0)


def _route_is_connected(route_edges: list[str], net: Any) -> bool:
    if not route_edges:
        return False
    try:
        prev_edge = net.getEdge(route_edges[0])
    except Exception:
        return False
    for edge_id in route_edges[1:]:
        try:
            cur_edge = net.getEdge(edge_id)
        except Exception:
            return False
        if prev_edge.getToNode().getID() != cur_edge.getFromNode().getID():
            return False
        prev_edge = cur_edge
    return True


def summarize_vehicle_route_artifact(
    route_file: str | Path,
    net_file: str | Path,
    *,
    scenario_name: str,
    road_allocated_count_600s: int,
    road_allocated_flow_vph: float,
    vehicle_type: str = VEHICLE_POLICY_VEHICLE_TYPE,
    passenger_ratio: float = VEHICLE_POLICY_PASSENGER_RATIO,
    allocation_basis: str = VEHICLE_POLICY_ALLOCATION_BASIS,
    road_group: str = "all_network",
    vehicle_type_split: str = VEHICLE_POLICY_TYPE_SPLIT,
    road_group_type: str = "full_network",
    edge_group_mapping_status: str = "full_network_fallback",
    network_edge_group: str = "full_network",
) -> dict[str, Any]:
    route_file = Path(route_file)
    net_file = Path(net_file)
    trip_file = route_file.with_name(route_file.name.replace(".rou.xml", ".trips.xml"))

    net = read_net(net_file)
    driveable_edges = _collect_driveable_edges(net)
    driveable_edge_count = len(driveable_edges)
    driveable_edge_ids = {edge.getID() for edge in driveable_edges}
    driveable_edge_map = {edge.getID(): edge for edge in driveable_edges}

    trip_df = _load_trip_data(trip_file)
    route_df = _load_vehicle_routes(route_file)

    route_edge_ids: set[str] = set()
    route_edge_visit_counts: Counter[str] = Counter()
    origin_edges: set[str] = set()
    arrival_edges: set[str] = set()
    route_bboxes: list[tuple[float, float, float, float]] = []
    route_lengths_m: list[float] = []
    disconnected_route_count = 0

    for row in route_df.itertuples(index=False):
        edges = list(row.route_edges)
        if not edges:
            continue
        route_edge_ids.update(edges)
        route_edge_visit_counts.update(edges)
        if not _route_is_connected(edges, net):
            disconnected_route_count += 1
        route_length_m = 0.0
        bbox_parts: list[tuple[float, float, float, float]] = []
        for edge_id in edges:
            edge = driveable_edge_map.get(edge_id)
            if edge is None:
                try:
                    edge = net.getEdge(edge_id)
                except Exception:
                    edge = None
            if edge is None:
                continue
            route_length_m += float(edge.getLength())
            bbox = _edge_bbox(edge)
            if bbox is not None:
                bbox_parts.append(bbox)
        if bbox_parts:
            route_bboxes.append(
                (
                    min(b[0] for b in bbox_parts),
                    min(b[1] for b in bbox_parts),
                    max(b[2] for b in bbox_parts),
                    max(b[3] for b in bbox_parts),
                )
            )
        route_lengths_m.append(route_length_m)

    if not trip_df.empty:
        origin_edges = set(trip_df["origin_edge_id"].astype(str))
        arrival_edges = set(trip_df["destination_edge_id"].astype(str))
    else:
        origin_edges = {edges[0] for edges in route_df["route_edges"].tolist() if edges}
        arrival_edges = {edges[-1] for edges in route_df["route_edges"].tolist() if edges}

    used_vehicle_edges = route_edge_ids & driveable_edge_ids
    unique_route_edges = len(route_edge_ids)
    edge_coverage_ratio = unique_route_edges / driveable_edge_count if driveable_edge_count else 0.0

    all_bbox = None
    all_boxes = [b for edge in driveable_edges if (b := _edge_bbox(edge)) is not None]
    if all_boxes:
        all_bbox = (
            min(b[0] for b in all_boxes),
            min(b[1] for b in all_boxes),
            max(b[2] for b in all_boxes),
            max(b[3] for b in all_boxes),
        )
    route_bbox = None
    if route_bboxes:
        route_bbox = (
            min(b[0] for b in route_bboxes),
            min(b[1] for b in route_bboxes),
            max(b[2] for b in route_bboxes),
            max(b[3] for b in route_bboxes),
        )

    def _bbox_area(bbox: tuple[float, float, float, float] | None) -> float:
        if bbox is None:
            return 0.0
        return max(0.0, float(bbox[2] - bbox[0])) * max(0.0, float(bbox[3] - bbox[1]))

    all_bbox_area = _bbox_area(all_bbox)
    route_bbox_area = _bbox_area(route_bbox)
    route_bbox_area_ratio = (route_bbox_area / all_bbox_area) if all_bbox_area else pd.NA

    route_summary: dict[str, Any] = {
        "scenario_name": scenario_name,
        "allocation_basis": allocation_basis,
        "vehicle_type": vehicle_type,
        "passenger_ratio": float(passenger_ratio),
        "vehicle_type_split": vehicle_type_split,
        "road_group": road_group,
        "road_group_type": road_group_type,
        "edge_group_mapping_status": edge_group_mapping_status,
        "network_edge_group": network_edge_group,
        "road_allocated_count_600s": int(road_allocated_count_600s),
        "road_allocated_flow_vph": float(round(road_allocated_flow_vph, 3)),
        "generated_vehicle_count": int(len(route_df)),
        "generated_vehicle_route_file": str(route_file.resolve()),
        "generated_vehicle_trip_file": str(trip_file.resolve()),
        "unique_depart_edges": int(len(origin_edges)),
        "unique_arrival_edges": int(len(arrival_edges)),
        "unique_route_edges": int(unique_route_edges),
        "network_edge_coverage_ratio": round(edge_coverage_ratio, 10),
        "driveable_edge_count": int(driveable_edge_count),
        "used_vehicle_edges": int(len(used_vehicle_edges)),
        "unused_driveable_edges": int(max(0, driveable_edge_count - len(used_vehicle_edges))),
        "disconnected_route_count": int(disconnected_route_count),
        "shared_edge_count": int(sum(1 for count in route_edge_visit_counts.values() if count > 1)),
        "route_bbox_min_x": route_bbox[0] if route_bbox else pd.NA,
        "route_bbox_min_y": route_bbox[1] if route_bbox else pd.NA,
        "route_bbox_max_x": route_bbox[2] if route_bbox else pd.NA,
        "route_bbox_max_y": route_bbox[3] if route_bbox else pd.NA,
        "net_bbox_min_x": all_bbox[0] if all_bbox else pd.NA,
        "net_bbox_min_y": all_bbox[1] if all_bbox else pd.NA,
        "net_bbox_max_x": all_bbox[2] if all_bbox else pd.NA,
        "net_bbox_max_y": all_bbox[3] if all_bbox else pd.NA,
        "route_bbox_area_ratio": route_bbox_area_ratio,
        "route_bbox_quadrant_sw": 0,
        "route_bbox_quadrant_se": 0,
        "route_bbox_quadrant_nw": 0,
        "route_bbox_quadrant_ne": 0,
        "route_length_m_min": round(float(min(route_lengths_m)), 3) if route_lengths_m else pd.NA,
        "route_length_m_max": round(float(max(route_lengths_m)), 3) if route_lengths_m else pd.NA,
        "route_length_m_mean": round(float(sum(route_lengths_m) / len(route_lengths_m)), 3) if route_lengths_m else pd.NA,
        "route_file_sha256": _sha256(route_file) if route_file.exists() else "",
        "trip_file_sha256": _sha256(trip_file) if trip_file.exists() else "",
        "net_file_sha256": _sha256(net_file) if net_file.exists() else "",
        "baseline_and_smart_same_vehicle_route_file": "not_checked",
        "baseline_and_smart_same_trip_file": "not_checked",
        "baseline_and_smart_same_net_file": "not_checked",
        "baseline_sumocfg_file": "",
        "smart_sumocfg_file": "",
        "baseline_vehicle_route_file": "",
        "smart_vehicle_route_file": "",
        "baseline_vehicle_trip_file": "",
        "smart_vehicle_trip_file": "",
        "baseline_net_file": "",
        "smart_net_file": "",
        "sumocfg_comparison_status": "not_checked",
    }

    if all_bbox is not None:
        mid_x = (all_bbox[0] + all_bbox[2]) / 2.0
        mid_y = (all_bbox[1] + all_bbox[3]) / 2.0
        quad_counts = {"SW": 0, "SE": 0, "NW": 0, "NE": 0}
        for edge_id in route_edge_ids:
            edge = net.getEdge(edge_id)
            center = _edge_center(edge)
            if center is None:
                continue
            quadrant = ("N" if center[1] >= mid_y else "S") + ("E" if center[0] >= mid_x else "W")
            quad_counts[quadrant] += 1
        route_summary.update(
            {
                "route_bbox_quadrant_sw": int(quad_counts["SW"]),
                "route_bbox_quadrant_se": int(quad_counts["SE"]),
                "route_bbox_quadrant_nw": int(quad_counts["NW"]),
                "route_bbox_quadrant_ne": int(quad_counts["NE"]),
            }
        )

    return route_summary


def _canonical_path_text(path_text: str | Path | None) -> str:
    if path_text is None:
        return ""
    text = str(path_text).strip()
    if not text:
        return ""
    return str(Path(text).expanduser().resolve())


def _parse_sumocfg_inputs(sumocfg_file: str | Path | None) -> dict[str, Any]:
    sumocfg_path = Path(sumocfg_file) if sumocfg_file else None
    if sumocfg_path is None or not sumocfg_path.exists():
        return {
            "sumocfg_file": _canonical_path_text(sumocfg_file),
            "status": "missing",
            "route_files": [],
            "net_file": "",
            "vehicle_route_file": "",
            "vehicle_trip_file": "",
        }

    root = ET.parse(sumocfg_path).getroot()
    route_value = root.findtext("./input/route-files", default="")
    net_value = root.findtext("./input/net-file", default="")
    route_files = [item.strip() for item in str(route_value or "").split(",") if item.strip()]
    canonical_route_files = [_canonical_path_text(item) for item in route_files]
    vehicle_route_file = ""
    for item in canonical_route_files:
        if item.endswith(".rou.xml"):
            vehicle_route_file = item
            break
    if not vehicle_route_file and canonical_route_files:
        vehicle_route_file = canonical_route_files[0]
    vehicle_trip_file = ""
    if vehicle_route_file:
        route_path = Path(vehicle_route_file)
        vehicle_trip_file = _canonical_path_text(route_path.with_name(route_path.name.replace(".rou.xml", ".trips.xml")))
    return {
        "sumocfg_file": _canonical_path_text(sumocfg_path),
        "status": "ok",
        "route_files": canonical_route_files,
        "net_file": _canonical_path_text(net_value),
        "vehicle_route_file": vehicle_route_file,
        "vehicle_trip_file": vehicle_trip_file,
    }


def compare_sumocfg_vehicle_inputs(
    baseline_sumocfg_file: str | Path | None,
    smart_sumocfg_file: str | Path | None,
) -> dict[str, Any]:
    baseline = _parse_sumocfg_inputs(baseline_sumocfg_file)
    smart = _parse_sumocfg_inputs(smart_sumocfg_file)
    if baseline["status"] != "ok" or smart["status"] != "ok":
        return {
            "baseline_sumocfg_file": baseline["sumocfg_file"],
            "smart_sumocfg_file": smart["sumocfg_file"],
            "baseline_vehicle_route_file": baseline["vehicle_route_file"],
            "smart_vehicle_route_file": smart["vehicle_route_file"],
            "baseline_vehicle_trip_file": baseline["vehicle_trip_file"],
            "smart_vehicle_trip_file": smart["vehicle_trip_file"],
            "baseline_net_file": baseline["net_file"],
            "smart_net_file": smart["net_file"],
            "baseline_and_smart_same_vehicle_route_file": "not_checked",
            "baseline_and_smart_same_trip_file": "not_checked",
            "baseline_and_smart_same_net_file": "not_checked",
            "sumocfg_comparison_status": "not_checked",
        }

    route_files_match = baseline["route_files"] == smart["route_files"]
    trip_files_match = baseline["vehicle_trip_file"] == smart["vehicle_trip_file"]
    net_files_match = baseline["net_file"] == smart["net_file"]
    return {
        "baseline_sumocfg_file": baseline["sumocfg_file"],
        "smart_sumocfg_file": smart["sumocfg_file"],
        "baseline_vehicle_route_file": baseline["vehicle_route_file"],
        "smart_vehicle_route_file": smart["vehicle_route_file"],
        "baseline_vehicle_trip_file": baseline["vehicle_trip_file"],
        "smart_vehicle_trip_file": smart["vehicle_trip_file"],
        "baseline_net_file": baseline["net_file"],
        "smart_net_file": smart["net_file"],
        "baseline_and_smart_same_vehicle_route_file": "true" if route_files_match else "false",
        "baseline_and_smart_same_trip_file": "true" if trip_files_match else "false",
        "baseline_and_smart_same_net_file": "true" if net_files_match else "false",
        "sumocfg_comparison_status": "compared" if route_files_match and trip_files_match and net_files_match else "mismatch",
    }


def build_vehicle_demand_audit(
    output_dir: str | Path,
    demand_df: pd.DataFrame,
    *,
    scenario_name: str = VEHICLE_POLICY_SCENARIO,
    legacy_notes: list[str] | None = None,
) -> dict[str, Path]:
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    policy_df = resolve_vehicle_policy_table(scenario_name)
    policy_summary = resolve_vehicle_policy_summary(scenario_name)

    if demand_df.empty:
        route_summary_df = pd.DataFrame(columns=list(policy_summary.keys()))
    else:
        if "generated_vehicle_route_file" not in demand_df.columns:
            raise ValueError("demand_df missing generated_vehicle_route_file")
        if "vehicle_net_file" not in demand_df.columns:
            raise ValueError("demand_df missing vehicle_net_file")
        route_rows: list[dict[str, Any]] = []
        for route_file, group in demand_df.groupby("generated_vehicle_route_file", sort=False):
            first = group.iloc[0]
            route_summary = summarize_vehicle_route_artifact(
                route_file,
                first["vehicle_net_file"],
                scenario_name=scenario_name,
                road_allocated_count_600s=policy_summary["road_allocated_count_600s"],
                road_allocated_flow_vph=policy_summary["road_allocated_flow_vph"],
                vehicle_type=str(first.get("vehicle_type", policy_summary["vehicle_type"])),
                passenger_ratio=float(first.get("passenger_ratio", policy_summary["passenger_ratio"])),
                allocation_basis=str(first.get("allocation_basis", policy_summary["allocation_basis"])),
                road_group="all_network",
                vehicle_type_split=str(first.get("vehicle_type_split", policy_summary["vehicle_type_split"])),
                road_group_type="full_network",
                edge_group_mapping_status="full_network_fallback",
                network_edge_group="full_network",
            )
            route_summary.update(
                compare_sumocfg_vehicle_inputs(
                    first.get("baseline_sumocfg_file"),
                    first.get("smart_sumocfg_file"),
                )
            )
            route_rows.append(route_summary)
        route_summary_df = pd.DataFrame(route_rows)

    route_audit_path = output_dir / "vehicle_route_generation_audit.csv"
    edge_coverage_path = output_dir / "vehicle_edge_coverage_summary.csv"
    road_group_path = output_dir / "road_group_allocation_summary.csv"
    report_path = output_dir / "vehicle_demand_pipeline_audit.md"

    if route_summary_df.empty:
        route_summary_df = pd.DataFrame(columns=[
            "scenario_name",
            "vehicle_type",
            "passenger_ratio",
            "allocation_basis",
            "road_group",
            "road_allocated_count_600s",
            "road_allocated_flow_vph",
            "generated_vehicle_count",
            "generated_vehicle_route_file",
            "generated_vehicle_trip_file",
            "unique_depart_edges",
            "unique_arrival_edges",
            "unique_route_edges",
            "network_edge_coverage_ratio",
        ])

    route_columns = [
        "scenario_name",
        "allocation_basis",
        "vehicle_type",
        "passenger_ratio",
        "vehicle_type_split",
        "road_group",
        "road_group_type",
        "edge_group_mapping_status",
        "network_edge_group",
        "baseline_sumocfg_file",
        "smart_sumocfg_file",
        "baseline_vehicle_route_file",
        "smart_vehicle_route_file",
        "baseline_vehicle_trip_file",
        "smart_vehicle_trip_file",
        "baseline_net_file",
        "smart_net_file",
        "sumocfg_comparison_status",
        "road_allocated_count_600s",
        "road_allocated_flow_vph",
        "generated_vehicle_count",
        "generated_vehicle_route_file",
        "generated_vehicle_trip_file",
        "unique_depart_edges",
        "unique_arrival_edges",
        "unique_route_edges",
        "network_edge_coverage_ratio",
        "driveable_edge_count",
        "used_vehicle_edges",
        "unused_driveable_edges",
        "shared_edge_count",
        "disconnected_route_count",
        "route_bbox_min_x",
        "route_bbox_min_y",
        "route_bbox_max_x",
        "route_bbox_max_y",
        "net_bbox_min_x",
        "net_bbox_min_y",
        "net_bbox_max_x",
        "net_bbox_max_y",
        "route_bbox_area_ratio",
        "route_bbox_quadrant_sw",
        "route_bbox_quadrant_se",
        "route_bbox_quadrant_nw",
        "route_bbox_quadrant_ne",
        "route_length_m_min",
        "route_length_m_max",
        "route_length_m_mean",
        "route_file_sha256",
        "trip_file_sha256",
        "net_file_sha256",
        "baseline_and_smart_same_vehicle_route_file",
        "baseline_and_smart_same_trip_file",
        "baseline_and_smart_same_net_file",
    ]
    route_summary_df = route_summary_df.reindex(columns=route_columns)

    edge_summary_columns = [
        "scenario_name",
        "allocation_basis",
        "vehicle_type",
        "passenger_ratio",
        "road_group",
        "road_allocated_count_600s",
        "road_allocated_flow_vph",
        "generated_vehicle_count",
        "generated_vehicle_route_file",
        "unique_depart_edges",
        "unique_arrival_edges",
        "unique_route_edges",
        "network_edge_coverage_ratio",
        "driveable_edge_count",
        "used_vehicle_edges",
        "unused_driveable_edges",
        "shared_edge_count",
        "disconnected_route_count",
        "route_bbox_area_ratio",
        "route_bbox_min_x",
        "route_bbox_min_y",
        "route_bbox_max_x",
        "route_bbox_max_y",
        "net_bbox_min_x",
        "net_bbox_min_y",
        "net_bbox_max_x",
        "net_bbox_max_y",
        "route_bbox_quadrant_sw",
        "route_bbox_quadrant_se",
        "route_bbox_quadrant_nw",
        "route_bbox_quadrant_ne",
        "route_file_sha256",
        "trip_file_sha256",
        "net_file_sha256",
    ]
    edge_summary_df = route_summary_df.reindex(columns=edge_summary_columns)

    policy_rows = policy_df.copy()
    policy_rows["scenario_name"] = policy_rows["scenario_name"].astype(str)
    policy_rows["road_allocated_flow_vph"] = (
        policy_rows["road_allocated_count_600s"].astype(float) * policy_summary["total_vehicle_flow_vph"] / policy_summary["total_vehicle_count_600s"]
    ).round(3)
    policy_rows["generated_vehicle_count"] = (
        int(route_summary_df["generated_vehicle_count"].sum()) if not route_summary_df.empty else pd.NA
    )
    if len(route_summary_df) == 1:
        policy_rows["generated_vehicle_route_file"] = route_summary_df.iloc[0]["generated_vehicle_route_file"]
        policy_rows["unique_depart_edges"] = route_summary_df.iloc[0]["unique_depart_edges"]
        policy_rows["unique_arrival_edges"] = route_summary_df.iloc[0]["unique_arrival_edges"]
        policy_rows["unique_route_edges"] = route_summary_df.iloc[0]["unique_route_edges"]
        policy_rows["network_edge_coverage_ratio"] = route_summary_df.iloc[0]["network_edge_coverage_ratio"]
    else:
        policy_rows["generated_vehicle_route_file"] = "multiple_route_files"
        policy_rows["unique_depart_edges"] = pd.NA
        policy_rows["unique_arrival_edges"] = pd.NA
        policy_rows["unique_route_edges"] = pd.NA
        policy_rows["network_edge_coverage_ratio"] = pd.NA
    road_group_columns = [
        "scenario_name",
        "total_vehicle_flow_vph",
        "total_vehicle_count_600s",
        "vehicle_type",
        "passenger_ratio",
        "allocation_basis",
        "road_group",
        "road_name",
        "road_group_type",
        "edge_group_mapping_status",
        "network_edge_group",
        "vehicle_type_split",
        "direction_split",
        "baseline_sumocfg_file",
        "smart_sumocfg_file",
        "baseline_vehicle_route_file",
        "smart_vehicle_route_file",
        "baseline_vehicle_trip_file",
        "smart_vehicle_trip_file",
        "baseline_net_file",
        "smart_net_file",
        "sumocfg_comparison_status",
        "road_allocated_count_600s",
        "road_allocated_flow_vph",
        "generated_vehicle_count",
        "generated_vehicle_route_file",
        "unique_depart_edges",
        "unique_arrival_edges",
        "unique_route_edges",
        "network_edge_coverage_ratio",
    ]
    road_group_df = policy_rows.reindex(columns=road_group_columns)

    write_csv_utf8_sig(route_summary_df, route_audit_path)
    write_csv_utf8_sig(edge_summary_df, edge_coverage_path)
    write_csv_utf8_sig(road_group_df, road_group_path)

    generated_count = int(route_summary_df["generated_vehicle_count"].sum()) if not route_summary_df.empty else 0
    unique_route_files = int(route_summary_df["generated_vehicle_route_file"].nunique()) if not route_summary_df.empty else 0
    route_file_line = (
        route_summary_df.iloc[0]["generated_vehicle_route_file"]
        if len(route_summary_df) == 1
        else "multiple_route_files"
    )
    legacy_notes = legacy_notes or []
    report_lines = [
        "# Vehicle Demand Pipeline Audit",
        "",
        f"- Scenario: `{scenario_name}`",
        f"- Policy CSV: `{VEHICLE_POLICY_CSV}`",
        f"- Allocation basis: `{policy_summary['allocation_basis']}`",
        f"- Total vehicle flow vph: `{policy_summary['total_vehicle_flow_vph']}`",
        f"- Total vehicle count / 600s: `{policy_summary['total_vehicle_count_600s']}`",
        f"- Vehicle type: `{policy_summary['vehicle_type']}`",
        f"- Passenger ratio: `{policy_summary['passenger_ratio']}`",
        f"- Unique generated route files: `{unique_route_files}`",
        f"- Total generated vehicle count: `{generated_count}`",
        "",
        "## Used Pipeline",
        "",
        "- `smart_crosswalk_sumo/main.py -> run_pipeline(...)`",
        "- `smart_crosswalk_sumo/generate_demand.py -> generate_for_candidates(...)`",
        "- `smart_crosswalk_sumo/integrated_mode.py -> generate_integrated_demand(...)`",
        "- `smart_crosswalk_sumo/collect_metrics.py -> collect_all(...)`",
        "- `generate_vehicle_routes(...)` uses `randomTrips.py` with passenger-only trips on the active SUMO net.",
        "- Baseline and smart scenarios share the same generated vehicle route file for the same seed.",
        "- Standalone route-artifact summaries do not prove baseline/smart equality; when `sumocfg` inputs are available, this audit compares route, trip, and net paths directly.",
        "",
        "## Legacy / Unused Paths",
        "",
        "- `observed_vehicle_rate_for_candidate(...)` and `fallback_vehicle_rate(...)` remain in the codebase for legacy scenarios, but the fixed `main_realistic_stress` policy uses the CSV-backed total flow instead.",
        "- `scripts/run_distribution_sensitivity.py` is a sensitivity tool, not the active policy pipeline.",
        "- ROAD_RANK is not used for vehicle demand allocation.",
        "- If the active run is pedestrian-only (`vehicle_only=true`), vehicle demand generation is skipped by design.",
    ]
    if legacy_notes:
        report_lines.extend(["", "## Notes", ""])
        report_lines.extend([f"- {note}" for note in legacy_notes])
    report_lines.extend(
        [
            "",
            "## Route Coverage",
            "",
            _render_table(route_summary_df, [
                "generated_vehicle_route_file",
                "generated_vehicle_count",
                "unique_depart_edges",
                "unique_arrival_edges",
                "unique_route_edges",
                "network_edge_coverage_ratio",
            ]),
            "",
            "## Road Group Policy",
            "",
            _render_table(road_group_df, [
                "road_group",
                "road_name",
                "road_allocated_count_600s",
                "road_allocated_flow_vph",
                "direction_split",
                "road_group_type",
                "edge_group_mapping_status",
                "network_edge_group",
            ]),
            "",
            "## Verdict",
            "",
            "The vehicle demand is now driven by a single CSV-backed `main_realistic_stress` policy and generated across the full passenger-valid SUMO network. Road-group allocation is preserved as policy metadata, while the actual route generation remains network-wide `randomTrips.py` output.",
        ]
    )
    report_path = output_dir / "vehicle_demand_pipeline_audit.md"
    report_path.write_text("\n".join(report_lines) + "\n", encoding="utf-8-sig")

    return {
        "vehicle_route_generation_audit": route_audit_path,
        "vehicle_edge_coverage_summary": edge_coverage_path,
        "road_group_allocation_summary": road_group_path,
        "vehicle_demand_pipeline_audit": report_path,
    }
