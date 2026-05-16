#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import sys
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import sumolib

from smart_crosswalk_sumo.output_schema import write_csv_utf8_sig


DEFAULT_BASELINE_SUMOCFG = Path(
    "result/pipeline_smoke_2runs_p1p4_recovery_6_NODE_10060_s1/sumo_nets/integrated_selected/baseline_seed1.sumocfg"
)
DEFAULT_SMART_SUMOCFG = Path(
    "result/pipeline_smoke_2runs_p1p4_recovery_6_NODE_10060_s1/sumo_nets/integrated_selected/smart_selected_seed1.sumocfg"
)
DEFAULT_OUTPUT_DIR = Path("result/active/vehicle_flow_audit")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _parse_sumocfg(sumocfg_path: Path) -> tuple[Path, list[Path]]:
    root = ET.parse(sumocfg_path).getroot()
    net_node = root.find("./input/net-file")
    route_node = root.find("./input/route-files")
    if net_node is None or route_node is None:
        raise ValueError(f"invalid sumocfg: missing net-file or route-files: {sumocfg_path}")
    net_file = Path(net_node.attrib["value"]).expanduser()
    route_files = [Path(part.strip()).expanduser() for part in route_node.attrib["value"].split(",") if part.strip()]
    return net_file, route_files


def _load_trip_data(trip_file: Path) -> pd.DataFrame:
    root = ET.parse(trip_file).getroot()
    rows = []
    for trip in root.findall("trip"):
        rows.append(
            {
                "trip_id": str(trip.attrib.get("id", "")),
                "depart_sec": float(trip.attrib.get("depart", 0.0) or 0.0),
                "origin_edge_id": str(trip.attrib.get("from", "")),
                "destination_edge_id": str(trip.attrib.get("to", "")),
                "vehicle_type": str(trip.attrib.get("type", "")),
            }
        )
    return pd.DataFrame(rows)


def _load_vehicle_routes(route_file: Path) -> pd.DataFrame:
    root = ET.parse(route_file).getroot()
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
    if not rows:
        raise ValueError(f"no vehicle routes found in {route_file}")
    return pd.DataFrame(rows)


def _route_file_with_vehicles(route_files: list[Path]) -> Path:
    for route_file in route_files:
        root = ET.parse(route_file).getroot()
        if root.find("vehicle") is not None:
            return route_file
    raise ValueError(f"no vehicle route file found in route-files: {route_files}")


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


def _collect_network_edges(net: Any) -> list[Any]:
    edges: list[Any] = []
    for edge in net.getEdges():
        eid = edge.getID()
        if eid.startswith(":"):
            continue
        if edge.getFunction() == "internal":
            continue
        try:
            if not edge.allows("passenger"):
                continue
        except Exception:
            continue
        edges.append(edge)
    return edges


def _df_to_markdown(df: pd.DataFrame) -> str:
    if df.empty:
        return "_empty_"
    rows = []
    columns = list(df.columns)
    header = "| " + " | ".join(str(col) for col in columns) + " |"
    separator = "| " + " | ".join(["---"] * len(columns)) + " |"
    rows.append(header)
    rows.append(separator)
    for _, row in df.iterrows():
        values = []
        for col in columns:
            value = row[col]
            if pd.isna(value):
                values.append("")
            elif isinstance(value, float):
                values.append(f"{value:.3f}")
            else:
                values.append(str(value))
        rows.append("| " + " | ".join(values) + " |")
    return "\n".join(rows)


def _route_is_connected(route_edges: list[str], net: Any) -> bool:
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


def build_vehicle_flow_audit(
    baseline_sumocfg: Path,
    smart_sumocfg: Path,
    output_dir: Path,
) -> dict[str, Path]:
    baseline_net, baseline_route_files = _parse_sumocfg(baseline_sumocfg)
    smart_net, smart_route_files = _parse_sumocfg(smart_sumocfg)

    baseline_vehicle_route_file = _route_file_with_vehicles(baseline_route_files)
    smart_vehicle_route_file = _route_file_with_vehicles(smart_route_files)

    net = sumolib.net.readNet(str(baseline_net))
    driveable_edges = _collect_network_edges(net)
    driveable_edge_ids = [edge.getID() for edge in driveable_edges]
    driveable_edge_map = {edge.getID(): edge for edge in driveable_edges}

    baseline_trip_file = baseline_vehicle_route_file.with_name(baseline_vehicle_route_file.name.replace(".rou.xml", ".trips.xml"))
    smart_trip_file = smart_vehicle_route_file.with_name(smart_vehicle_route_file.name.replace(".rou.xml", ".trips.xml"))

    trip_df = _load_trip_data(baseline_trip_file)
    route_df = _load_vehicle_routes(baseline_vehicle_route_file)
    smart_trip_df = _load_trip_data(smart_trip_file)
    smart_route_df = _load_vehicle_routes(smart_vehicle_route_file)

    if not trip_df.empty and len(trip_df) != len(route_df):
        raise ValueError(
            f"trip count mismatch: {baseline_trip_file} has {len(trip_df)} trips, "
            f"{baseline_vehicle_route_file} has {len(route_df)} vehicles"
        )
    if not smart_trip_df.empty and len(smart_trip_df) != len(smart_route_df):
        raise ValueError(
            f"trip count mismatch: {smart_trip_file} has {len(smart_trip_df)} trips, "
            f"{smart_vehicle_route_file} has {len(smart_route_df)} vehicles"
        )

    vehicle_rows: list[dict[str, Any]] = []
    origin_dest_rows: list[dict[str, Any]] = []
    used_edge_vehicle_ids: dict[str, set[str]] = defaultdict(set)
    used_edge_visit_counts: Counter[str] = Counter()
    used_edge_position_sums: defaultdict[str, list[int]] = defaultdict(list)
    route_bboxes: list[tuple[float, float, float, float]] = []
    route_lengths_m: list[float] = []
    disconnected_vehicle_ids: list[str] = []

    for row in route_df.itertuples(index=False):
        vehicle_id = str(row.vehicle_id)
        route_edges = list(row.route_edges)
        if not route_edges:
            continue
        origin_edge_id = route_edges[0]
        destination_edge_id = route_edges[-1]
        connected = _route_is_connected(route_edges, net)
        if not connected:
            disconnected_vehicle_ids.append(vehicle_id)

        route_length_m = 0.0
        bbox_parts: list[tuple[float, float, float, float]] = []
        for idx, edge_id in enumerate(route_edges):
            used_edge_vehicle_ids[edge_id].add(vehicle_id)
            used_edge_visit_counts[edge_id] += 1
            used_edge_position_sums[edge_id].append(idx)
            edge = driveable_edge_map.get(edge_id)
            if edge is None:
                try:
                    edge = net.getEdge(edge_id)
                except Exception:
                    edge = None
            if edge is not None:
                route_length_m += float(edge.getLength())
                bbox = _edge_bbox(edge)
                if bbox is not None:
                    bbox_parts.append(bbox)

        if bbox_parts:
            route_bbox = (
                min(b[0] for b in bbox_parts),
                min(b[1] for b in bbox_parts),
                max(b[2] for b in bbox_parts),
                max(b[3] for b in bbox_parts),
            )
            route_bboxes.append(route_bbox)
        else:
            route_bbox = (float("nan"), float("nan"), float("nan"), float("nan"))

        route_lengths_m.append(route_length_m)
        origin_edge = net.getEdge(origin_edge_id)
        destination_edge = net.getEdge(destination_edge_id)
        origin_center = _edge_center(origin_edge)
        destination_center = _edge_center(destination_edge)

        vehicle_rows.append(
            {
                "vehicle_id": vehicle_id,
                "depart_sec": float(row.depart_sec),
                "origin_edge_id": origin_edge_id,
                "destination_edge_id": destination_edge_id,
                "route_edge_count": len(route_edges),
                "route_length_m": round(route_length_m, 3),
                "route_connectivity_ok": bool(connected),
                "route_bbox_min_x": route_bbox[0],
                "route_bbox_min_y": route_bbox[1],
                "route_bbox_max_x": route_bbox[2],
                "route_bbox_max_y": route_bbox[3],
                "origin_center_x": origin_center[0] if origin_center else pd.NA,
                "origin_center_y": origin_center[1] if origin_center else pd.NA,
                "destination_center_x": destination_center[0] if destination_center else pd.NA,
                "destination_center_y": destination_center[1] if destination_center else pd.NA,
            }
        )
        origin_dest_rows.append(
            {
                "vehicle_id": vehicle_id,
                "depart_sec": float(row.depart_sec),
                "origin_edge_id": origin_edge_id,
                "destination_edge_id": destination_edge_id,
                "route_edge_count": len(route_edges),
                "route_length_m": round(route_length_m, 3),
                "route_connectivity_ok": bool(connected),
            }
        )

    if not vehicle_rows:
        raise ValueError(f"no vehicle rows parsed from {baseline_vehicle_route_file}")

    used_edge_rows: list[dict[str, Any]] = []
    for edge in driveable_edges:
        edge_id = edge.getID()
        bbox = _edge_bbox(edge)
        center = _edge_center(edge)
        vehicle_count = len(used_edge_vehicle_ids.get(edge_id, set()))
        visit_count = int(used_edge_visit_counts.get(edge_id, 0))
        if vehicle_count == 0:
            coverage_status = "unused"
        elif vehicle_count == 1:
            coverage_status = "used"
        else:
            coverage_status = "shared"
        used_edge_rows.append(
            {
                "edge_id": edge_id,
                "vehicle_count": vehicle_count,
                "visit_count": visit_count,
                "coverage_status": coverage_status,
                "is_origin_edge": any(row["origin_edge_id"] == edge_id for row in origin_dest_rows),
                "is_destination_edge": any(row["destination_edge_id"] == edge_id for row in origin_dest_rows),
                "edge_length_m": float(edge.getLength()),
                "edge_bbox_min_x": bbox[0] if bbox else pd.NA,
                "edge_bbox_min_y": bbox[1] if bbox else pd.NA,
                "edge_bbox_max_x": bbox[2] if bbox else pd.NA,
                "edge_bbox_max_y": bbox[3] if bbox else pd.NA,
                "edge_center_x": center[0] if center else pd.NA,
                "edge_center_y": center[1] if center else pd.NA,
            }
        )

    used_edges_df = pd.DataFrame(used_edge_rows).sort_values(
        ["visit_count", "vehicle_count", "edge_id"], ascending=[False, False, True]
    )
    vehicle_df = pd.DataFrame(vehicle_rows).sort_values(["depart_sec", "vehicle_id"], ascending=[True, True])
    od_df = pd.DataFrame(origin_dest_rows).sort_values(["depart_sec", "vehicle_id"], ascending=[True, True])

    departure_bin_sec = 60
    max_depart = max(float(v["depart_sec"]) for v in vehicle_rows)
    bin_rows = []
    bin_start = 0
    while bin_start <= max_depart:
        bin_end = bin_start + departure_bin_sec
        mask = (vehicle_df["depart_sec"] >= bin_start) & (vehicle_df["depart_sec"] < bin_end)
        count = int(mask.sum())
        bin_rows.append(
            {
                "bin_start_sec": bin_start,
                "bin_end_sec": bin_end,
                "vehicle_count": count,
                "share_of_total": round(count / len(vehicle_df), 6),
            }
        )
        bin_start = bin_end
    departure_df = pd.DataFrame(bin_rows)

    all_bbox = None
    route_bbox = None
    if driveable_edges:
        all_boxes = [b for edge in driveable_edges if (b := _edge_bbox(edge)) is not None]
        if all_boxes:
            all_bbox = (
                min(b[0] for b in all_boxes),
                min(b[1] for b in all_boxes),
                max(b[2] for b in all_boxes),
                max(b[3] for b in all_boxes),
            )
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

    used_edge_count = int((used_edges_df["visit_count"] > 0).sum())
    shared_edge_count = int((used_edges_df["vehicle_count"] > 1).sum())
    unused_edge_count = int((used_edges_df["visit_count"] == 0).sum())
    total_route_edge_traversals = int(sum(used_edge_visit_counts.values()))
    unique_route_edge_count = int(len(used_edge_vehicle_ids))
    edge_coverage_ratio = unique_route_edge_count / len(driveable_edges) if driveable_edges else 0.0
    distinct_origin_edge_count = int(od_df["origin_edge_id"].nunique()) if not od_df.empty else 0
    distinct_destination_edge_count = int(od_df["destination_edge_id"].nunique()) if not od_df.empty else 0
    distinct_od_pair_count = int(
        od_df[["origin_edge_id", "destination_edge_id"]].drop_duplicates().shape[0]
    ) if not od_df.empty else 0
    route_lengths_series = vehicle_df["route_length_m"] if "route_length_m" in vehicle_df.columns else pd.Series(dtype=float)
    route_edge_count_series = vehicle_df["route_edge_count"] if "route_edge_count" in vehicle_df.columns else pd.Series(dtype=float)

    summary_row = {
        "baseline_sumocfg": str(baseline_sumocfg),
        "smart_sumocfg": str(smart_sumocfg),
        "baseline_net_file": str(baseline_net),
        "smart_net_file": str(smart_net),
        "baseline_vehicle_route_file": str(baseline_vehicle_route_file),
        "smart_vehicle_route_file": str(smart_vehicle_route_file),
        "baseline_trip_file": str(baseline_trip_file),
        "smart_trip_file": str(smart_trip_file),
        "baseline_and_smart_same_net_file": bool(baseline_net.resolve() == smart_net.resolve()),
        "baseline_and_smart_same_vehicle_route_file": bool(
            baseline_vehicle_route_file.resolve() == smart_vehicle_route_file.resolve()
        ),
        "baseline_and_smart_same_trip_file": bool(baseline_trip_file.resolve() == smart_trip_file.resolve()),
        "total_vehicle_count": int(len(vehicle_df)),
        "total_trip_count": int(len(trip_df)),
        "trip_route_count_match": bool(len(trip_df) == len(vehicle_df)),
        "driveable_edge_count": int(len(driveable_edges)),
        "used_driveable_edge_count": used_edge_count,
        "unused_driveable_edge_count": unused_edge_count,
        "shared_edge_count": shared_edge_count,
        "total_route_edge_traversals": total_route_edge_traversals,
        "unique_route_edge_count": unique_route_edge_count,
        "edge_coverage_ratio": round(edge_coverage_ratio, 10),
        "distinct_origin_edge_count": distinct_origin_edge_count,
        "distinct_destination_edge_count": distinct_destination_edge_count,
        "distinct_od_pair_count": distinct_od_pair_count,
        "disconnected_route_count": int(len(disconnected_vehicle_ids)),
        "depart_min_sec": float(vehicle_df["depart_sec"].min()),
        "depart_max_sec": float(vehicle_df["depart_sec"].max()),
        "depart_mean_sec": float(vehicle_df["depart_sec"].mean()),
        "route_edge_count_min": int(route_edge_count_series.min()),
        "route_edge_count_max": int(route_edge_count_series.max()),
        "route_edge_count_mean": round(float(route_edge_count_series.mean()), 3),
        "route_length_m_min": round(float(route_lengths_series.min()), 3),
        "route_length_m_max": round(float(route_lengths_series.max()), 3),
        "route_length_m_mean": round(float(route_lengths_series.mean()), 3),
        "net_bbox_min_x": all_bbox[0] if all_bbox else pd.NA,
        "net_bbox_min_y": all_bbox[1] if all_bbox else pd.NA,
        "net_bbox_max_x": all_bbox[2] if all_bbox else pd.NA,
        "net_bbox_max_y": all_bbox[3] if all_bbox else pd.NA,
        "route_bbox_min_x": route_bbox[0] if route_bbox else pd.NA,
        "route_bbox_min_y": route_bbox[1] if route_bbox else pd.NA,
        "route_bbox_max_x": route_bbox[2] if route_bbox else pd.NA,
        "route_bbox_max_y": route_bbox[3] if route_bbox else pd.NA,
        "route_bbox_area_ratio": route_bbox_area_ratio,
        "route_bbox_quadrant_sw": 0,
        "route_bbox_quadrant_se": 0,
        "route_bbox_quadrant_nw": 0,
        "route_bbox_quadrant_ne": 0,
    }

    if all_bbox is not None:
        mid_x = (all_bbox[0] + all_bbox[2]) / 2.0
        mid_y = (all_bbox[1] + all_bbox[3]) / 2.0
        quad_counts = {"SW": 0, "SE": 0, "NW": 0, "NE": 0}
        for edge_id in used_edge_vehicle_ids:
            edge = net.getEdge(edge_id)
            center = _edge_center(edge)
            if center is None:
                continue
            quadrant = ("N" if center[1] >= mid_y else "S") + ("E" if center[0] >= mid_x else "W")
            quad_counts[quadrant] += 1
        summary_row.update(
            {
                "route_bbox_quadrant_sw": int(quad_counts["SW"]),
                "route_bbox_quadrant_se": int(quad_counts["SE"]),
                "route_bbox_quadrant_nw": int(quad_counts["NW"]),
                "route_bbox_quadrant_ne": int(quad_counts["NE"]),
            }
        )

    summary_df = pd.DataFrame([summary_row])

    output_dir.mkdir(parents=True, exist_ok=True)
    summary_path = output_dir / "vehicle_route_summary.csv"
    edge_path = output_dir / "vehicle_edge_coverage.csv"
    departure_path = output_dir / "vehicle_departure_distribution.csv"
    od_detail_path = output_dir / "vehicle_origin_destination_edges.csv"
    od_summary_path = output_dir / "vehicle_origin_destination_summary.csv"
    spatial_summary_path = output_dir / "vehicle_route_spatial_summary.csv"
    report_path = output_dir / "vehicle_flow_audit_report.md"

    write_csv_utf8_sig(summary_df, summary_path)
    write_csv_utf8_sig(used_edges_df, edge_path)
    write_csv_utf8_sig(departure_df, departure_path)
    write_csv_utf8_sig(od_df, od_detail_path)

    od_summary_df = (
        od_df.groupby(["origin_edge_id", "destination_edge_id"], as_index=False)
        .agg(
            vehicle_count=("vehicle_id", "count"),
            depart_min_sec=("depart_sec", "min"),
            depart_max_sec=("depart_sec", "max"),
            depart_mean_sec=("depart_sec", "mean"),
            route_length_m_mean=("route_length_m", "mean"),
            route_edge_count_mean=("route_edge_count", "mean"),
            route_connectivity_ok_count=("route_connectivity_ok", "sum"),
        )
        .sort_values(["vehicle_count", "depart_min_sec", "origin_edge_id", "destination_edge_id"], ascending=[False, True, True, True])
    )
    od_summary_df["route_connectivity_ok_ratio"] = od_summary_df["route_connectivity_ok_count"] / od_summary_df["vehicle_count"]
    write_csv_utf8_sig(od_summary_df, od_summary_path)

    spatial_summary_df = pd.DataFrame(
        [
            {
                "total_driveable_edges": int(len(driveable_edges)),
                "used_vehicle_edges": int(unique_route_edge_count),
                "edge_coverage_ratio": round(edge_coverage_ratio, 10),
                "total_vehicle_count": int(len(vehicle_df)),
                "unique_origin_edges": int(distinct_origin_edge_count),
                "unique_destination_edges": int(distinct_destination_edge_count),
                "route_bbox_min_x": route_bbox[0] if route_bbox else pd.NA,
                "route_bbox_min_y": route_bbox[1] if route_bbox else pd.NA,
                "route_bbox_max_x": route_bbox[2] if route_bbox else pd.NA,
                "route_bbox_max_y": route_bbox[3] if route_bbox else pd.NA,
                "net_bbox_min_x": all_bbox[0] if all_bbox else pd.NA,
                "net_bbox_min_y": all_bbox[1] if all_bbox else pd.NA,
                "net_bbox_max_x": all_bbox[2] if all_bbox else pd.NA,
                "net_bbox_max_y": all_bbox[3] if all_bbox else pd.NA,
                "route_bbox_area_ratio": route_bbox_area_ratio,
                "route_bbox_quadrant_sw": int(summary_row["route_bbox_quadrant_sw"]),
                "route_bbox_quadrant_se": int(summary_row["route_bbox_quadrant_se"]),
                "route_bbox_quadrant_nw": int(summary_row["route_bbox_quadrant_nw"]),
                "route_bbox_quadrant_ne": int(summary_row["route_bbox_quadrant_ne"]),
                "vehicle_route_file_sha256": _sha256(baseline_vehicle_route_file),
                "trip_file_sha256": _sha256(baseline_trip_file),
                "net_file_sha256": _sha256(baseline_net),
                "baseline_and_smart_same_net_file": bool(baseline_net.resolve() == smart_net.resolve()),
                "baseline_and_smart_same_vehicle_route_file": bool(
                    baseline_vehicle_route_file.resolve() == smart_vehicle_route_file.resolve()
                ),
                "baseline_and_smart_same_trip_file": bool(baseline_trip_file.resolve() == smart_trip_file.resolve()),
                "total_route_edge_traversals": total_route_edge_traversals,
                "disconnected_route_count": int(len(disconnected_vehicle_ids)),
            }
        ]
    )
    write_csv_utf8_sig(spatial_summary_df, spatial_summary_path)

    route_bbox_area_pct = None if pd.isna(route_bbox_area_ratio) else float(route_bbox_area_ratio) * 100.0
    coverage_pct = edge_coverage_ratio * 100.0

    def _fmt(v: Any) -> str:
        if isinstance(v, float):
            return f"{v:.3f}"
        return str(v)

    report_lines = [
        "# Vehicle Flow Audit",
        "",
        f"- Baseline sumocfg: `{baseline_sumocfg}`",
        f"- Smart sumocfg: `{smart_sumocfg}`",
        f"- Net file: `{baseline_net}`",
        f"- Vehicle route file: `{baseline_vehicle_route_file}`",
        f"- Trip file: `{baseline_trip_file}`",
        "",
        "## Scope",
        "",
        "- The current `current_main_12` smoke run in `result/active/old_real_30seed_runs/...` is pedestrian-only (`include_vehicles=false`), so it is not used for this vehicle audit.",
        "- This audit uses the vehicle-inclusive `integrated_selected` artifact under `result/pipeline_smoke_2runs_p1p4_recovery_6_NODE_10060_s1`.",
        "",
        "## Summary",
        "",
        "| Metric | Value |",
        "| --- | ---: |",
        f"| total_driveable_edges | {_fmt(len(driveable_edges))} |",
        f"| used_vehicle_edges | {_fmt(unique_route_edge_count)} |",
        f"| edge_coverage_ratio | {_fmt(coverage_pct)}% |",
        f"| total_vehicle_count | {_fmt(len(vehicle_df))} |",
        f"| Total trip records | {_fmt(len(trip_df))} |",
        f"| unique_origin_edges | {_fmt(distinct_origin_edge_count)} |",
        f"| unique_destination_edges | {_fmt(distinct_destination_edge_count)} |",
        f"| Distinct origin-destination pairs | {_fmt(distinct_od_pair_count)} |",
        f"| Shared edges | {_fmt(shared_edge_count)} |",
        f"| Disconnected routes | {_fmt(len(disconnected_vehicle_ids))} |",
        f"| Route bbox area ratio vs network bbox | {_fmt(route_bbox_area_pct) if route_bbox_area_pct is not None else 'NA'}% |",
        "",
        "## Departure Distribution",
        "",
        _df_to_markdown(departure_df),
        "",
        "## Origin/Destination Diversity",
        "",
        _df_to_markdown(od_summary_df),
        "",
        "## Edge Coverage",
        "",
        "- `vehicle_edge_coverage.csv` includes every driveable edge with `visit_count=0` for unused roads.",
        "- Filter `coverage_status == 'unused'` to get vehicle-free roads.",
        "- Filter `coverage_status == 'shared'` to get edges hit by more than one vehicle route.",
        "",
        "### Key Fields",
        "",
        f"- `total_driveable_edges`: {len(driveable_edges)}",
        f"- `used_vehicle_edges`: {unique_route_edge_count}",
        f"- `edge_coverage_ratio`: {edge_coverage_ratio:.10f}",
        f"- `total_vehicle_count`: {len(vehicle_df)}",
        f"- `unique_origin_edges`: {distinct_origin_edge_count}",
        f"- `unique_destination_edges`: {distinct_destination_edge_count}",
        "",
        "## Spatial Summary",
        "",
        "- `vehicle_route_spatial_summary.csv` captures the route bbox, network bbox, coverage ratio, and identity hashes for the vehicle-inclusive active artifact.",
        "",
        "## Verdict",
        "",
        (
            "The vehicle routes are not candidate-crosswalk-only. They are generated from `randomTrips.py` on the full "
            "integrated network and the baseline/smart configs share the same vehicle route file. "
            f"However, this smoke artifact is sparse: only {len(vehicle_df)} vehicles, {unique_route_edge_count} unique route edges, "
            f"and {coverage_pct:.2f}% of driveable edges are used. "
            "So it is network-wide by generation method, but it is not dense enough to honestly claim that traffic is "
            "evenly distributed across all of Jung-gu."
        ),
        "",
        "## Baseline / Smart Identity",
        "",
        f"- Same net file: `{bool(baseline_net.resolve() == smart_net.resolve())}`",
        f"- Same vehicle route file: `{bool(baseline_vehicle_route_file.resolve() == smart_vehicle_route_file.resolve())}`",
        f"- Same trip file: `{bool(baseline_trip_file.resolve() == smart_trip_file.resolve())}`",
        "",
        "## Notes",
        "",
        "- Candidate-group-by-admin-dong coverage is not directly derivable from the current vehicle route artifact because the route XML is network-wide randomTrips output and the candidate CSV does not carry a vehicle-edge join key.",
        "- The route bbox spread is still broad relative to the network bbox, but the edge coverage ratio is the stronger indicator for this question.",
        "- Smart policy does not change vehicle demand generation here: baseline and smart share the same net, trip, and route files.",
    ]
    report_path.write_text("\n".join(report_lines) + "\n", encoding="utf-8-sig")

    return {
        "summary": summary_path,
        "edge": edge_path,
        "departure": departure_path,
        "od_detail": od_detail_path,
        "od_summary": od_summary_path,
        "spatial_summary": spatial_summary_path,
        "report": report_path,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline_sumocfg", default=str(DEFAULT_BASELINE_SUMOCFG))
    parser.add_argument("--smart_sumocfg", default=str(DEFAULT_SMART_SUMOCFG))
    parser.add_argument("--output_dir", default=str(DEFAULT_OUTPUT_DIR))
    args = parser.parse_args()

    paths = build_vehicle_flow_audit(
        Path(args.baseline_sumocfg),
        Path(args.smart_sumocfg),
        Path(args.output_dir),
    )
    for name, path in paths.items():
        print(f"{name}: {path}")


if __name__ == "__main__":
    main()
