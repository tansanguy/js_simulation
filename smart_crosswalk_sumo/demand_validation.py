from __future__ import annotations

import json
import math
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import pandas as pd

from .network_utils import edge_function, read_net
from .output_schema import write_csv_utf8_sig
from .vehicle_demand_policy import compare_sumocfg_vehicle_inputs, resolve_vehicle_policy_summary


VALIDATION_SUMMARY_FILENAME = "demand_validation_summary.csv"
PEDESTRIAN_VALIDATION_FILENAME = "pedestrian_flow_policy_validation.csv"
VEHICLE_VALIDATION_FILENAME = "vehicle_flow_policy_validation.csv"
GLOBAL_COVERAGE_FILENAME = "vehicle_global_coverage_validation.csv"
REPORT_FILENAME = "demand_validation_report.md"

VEHICLE_COUNT_TOLERANCE = 5
VEHICLE_WARNING_TOLERANCE = 35
VEHICLE_CANDIDATE_BUFFER_WARNING_THRESHOLD = 0.5
GLOBAL_COVERAGE_WARNING_THRESHOLD = 0.01
GLOBAL_COVERAGE_MIN_GRID_SHARE = 0.25
PEDESTRIAN_CONCENTRATION_WARNING_THRESHOLD = 0.90


def _resolve_run_paths(run_dir: str | Path | None = None, output_dir: str | Path | None = None) -> tuple[Path, Path, Path]:
    if run_dir is None and output_dir is None:
        raise ValueError("run_dir or output_dir is required")
    if run_dir is not None:
        run_path = Path(run_dir)
        if run_path.name == "outputs":
            output_path = run_path
            run_path = run_path.parent
        else:
            output_path = Path(output_dir) if output_dir is not None else run_path / "outputs"
        nets_path = run_path / "sumo_nets"
        return run_path, output_path, nets_path
    output_path = Path(output_dir)  # type: ignore[arg-type]
    run_path = output_path.parent
    return run_path, output_path, run_path / "sumo_nets"


def _read_csv(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame()
    return pd.read_csv(path, encoding="utf-8-sig")


def _parse_xml(path: Path) -> ET.Element | None:
    if not path.exists():
        return None
    try:
        return ET.parse(path).getroot()
    except Exception:
        return None


def _canonical_path(path: str | Path | None) -> str:
    if path is None:
        return ""
    text = str(path).strip()
    if not text:
        return ""
    return str(Path(text).expanduser().resolve())


def _path_or_empty(path: str | Path | None) -> Path | None:
    if path is None:
        return None
    text = str(path).strip()
    if not text:
        return None
    return Path(text)


def _infer_ped_route_file(vehicle_route_file: Path, seed: int) -> Path:
    return vehicle_route_file.parent / f"peds_seed{seed}.rou.xml"


def _summarize_histogram(values: list[float], bin_size: float = 60.0) -> str:
    if not values:
        return ""
    counts: Counter[str] = Counter()
    for value in values:
        bucket = int(math.floor(max(0.0, float(value)) / bin_size) * bin_size)
        label = f"{bucket:.0f}-{bucket + bin_size:.0f}"
        counts[label] += 1
    return "|".join(f"{key}:{counts[key]}" for key in sorted(counts.keys(), key=lambda item: int(item.split("-", 1)[0])))


def _extract_crosswalk_id(person_id: str) -> str:
    parts = str(person_id).split("_")
    if len(parts) >= 3:
        return parts[1]
    return ""


def _load_vehicle_route_records(route_file: Path) -> tuple[list[dict[str, Any]], list[float], list[str], list[str]]:
    root = _parse_xml(route_file)
    if root is None:
        return [], [], [], []
    rows: list[dict[str, Any]] = []
    depart_times: list[float] = []
    route_edges: list[str] = []
    vehicle_types: list[str] = []
    for vehicle in root.findall("vehicle"):
        depart = float(vehicle.attrib.get("depart", 0.0) or 0.0)
        route_elem = vehicle.find("route")
        edges = str(route_elem.attrib.get("edges", "") if route_elem is not None else "").split()
        vehicle_type = str(vehicle.attrib.get("type") or vehicle.attrib.get("vClass") or "")
        rows.append(
            {
                "id": str(vehicle.attrib.get("id", "")),
                "depart": depart,
                "type": vehicle_type,
                "edges": edges,
            }
        )
        depart_times.append(depart)
        route_edges.extend(edges)
        if vehicle_type:
            vehicle_types.append(vehicle_type)
    if not rows:
        for trip in root.findall("trip"):
            depart = float(trip.attrib.get("depart", 0.0) or 0.0)
            vehicle_type = str(trip.attrib.get("type") or trip.attrib.get("vClass") or "")
            rows.append(
                {
                    "id": str(trip.attrib.get("id", "")),
                    "depart": depart,
                    "type": vehicle_type,
                    "edges": [str(trip.attrib.get("from", "")), str(trip.attrib.get("to", ""))],
                }
            )
            depart_times.append(depart)
            if vehicle_type:
                vehicle_types.append(vehicle_type)
    return rows, depart_times, route_edges, vehicle_types


def _load_trip_records(trip_file: Path) -> tuple[list[dict[str, Any]], list[float], list[str]]:
    root = _parse_xml(trip_file)
    if root is None:
        return [], [], []
    rows: list[dict[str, Any]] = []
    depart_times: list[float] = []
    vehicle_types: list[str] = []
    for trip in root.findall("trip"):
        depart = float(trip.attrib.get("depart", 0.0) or 0.0)
        trip_type = str(trip.attrib.get("type") or trip.attrib.get("vClass") or "")
        rows.append(
            {
                "id": str(trip.attrib.get("id", "")),
                "depart": depart,
                "type": trip_type,
                "from": str(trip.attrib.get("from", "")),
                "to": str(trip.attrib.get("to", "")),
            }
        )
        depart_times.append(depart)
        if trip_type:
            vehicle_types.append(trip_type)
    return rows, depart_times, vehicle_types


def _load_pedestrian_route_records(route_file: Path) -> tuple[list[dict[str, Any]], list[float], dict[str, int], list[str]]:
    root = _parse_xml(route_file)
    if root is None:
        return [], [], {}, []
    rows: list[dict[str, Any]] = []
    depart_times: list[float] = []
    crosswalk_counts: Counter[str] = Counter()
    walk_edges: list[str] = []
    for person in root.findall("person"):
        depart = float(person.attrib.get("depart", 0.0) or 0.0)
        person_id = str(person.attrib.get("id", ""))
        route_elem = person.find("walk")
        from_edge = str(route_elem.attrib.get("from", "") if route_elem is not None else "")
        to_edge = str(route_elem.attrib.get("to", "") if route_elem is not None else "")
        rows.append(
            {
                "id": person_id,
                "depart": depart,
                "from": from_edge,
                "to": to_edge,
            }
        )
        depart_times.append(depart)
        crosswalk_id = _extract_crosswalk_id(person_id)
        if crosswalk_id:
            crosswalk_counts[crosswalk_id] += 1
        walk_edges.extend([from_edge, to_edge])
    return rows, depart_times, dict(crosswalk_counts), walk_edges


def _load_driveable_edge_count(net_file: Path) -> tuple[int, dict[str, Any], dict[str, Any]]:
    net = read_net(net_file)
    driveable_edges = []
    edge_map: dict[str, Any] = {}
    for edge in net.getEdges():
        edge_id = edge.getID()
        if edge_id.startswith(":") or edge_function(edge) == "internal":
            continue
        try:
            if not edge.allows("passenger"):
                continue
        except Exception:
            continue
        driveable_edges.append(edge)
        edge_map[edge_id] = edge
    return len(driveable_edges), edge_map, {edge.getID(): edge for edge in driveable_edges}


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


def _bbox_area(bbox: tuple[float, float, float, float] | None) -> float:
    if bbox is None:
        return 0.0
    return max(0.0, float(bbox[2] - bbox[0])) * max(0.0, float(bbox[3] - bbox[1]))


def _route_bbox(edges: list[str], edge_map: dict[str, Any]) -> tuple[float, float, float, float] | None:
    boxes: list[tuple[float, float, float, float]] = []
    for edge_id in edges:
        edge = edge_map.get(edge_id)
        if edge is None:
            continue
        bbox = _edge_bbox(edge)
        if bbox is not None:
            boxes.append(bbox)
    if not boxes:
        return None
    return (
        min(box[0] for box in boxes),
        min(box[1] for box in boxes),
        max(box[2] for box in boxes),
        max(box[3] for box in boxes),
    )


def _grid_coverage(route_edges: list[str], edge_map: dict[str, Any], grid_size: int = 4) -> tuple[int, int, float]:
    if not route_edges or not edge_map:
        return 0, grid_size * grid_size, 0.0
    centers = [_edge_center(edge_map[edge_id]) for edge_id in route_edges if edge_id in edge_map]
    centers = [center for center in centers if center is not None]
    if not centers:
        return 0, grid_size * grid_size, 0.0
    xs = [center[0] for center in centers]
    ys = [center[1] for center in centers]
    min_x, max_x = min(xs), max(xs)
    min_y, max_y = min(ys), max(ys)
    if max_x == min_x or max_y == min_y:
        return 1, grid_size * grid_size, 1.0 / float(grid_size * grid_size)
    used_cells: set[tuple[int, int]] = set()
    for x, y in centers:
        col = min(grid_size - 1, int((x - min_x) / (max_x - min_x + 1e-9) * grid_size))
        row = min(grid_size - 1, int((y - min_y) / (max_y - min_y + 1e-9) * grid_size))
        used_cells.add((row, col))
    total = grid_size * grid_size
    return len(used_cells), total, len(used_cells) / float(total)


def _load_candidate_buffer_edges(route_file: Path, output_dir: Path) -> set[str]:
    candidate_edges: set[str] = set()
    metadata_file = route_file.parent / "metadata.json"
    if metadata_file.exists():
        try:
            metadata = json.loads(metadata_file.read_text(encoding="utf-8"))
            candidate_edges.update(str(edge_id) for edge_id in metadata.get("vehicle_conflict_edges", []))
            candidate_edges.update(
                str(lane_id).rsplit("_", 1)[0]
                for lane_id in metadata.get("approach_lanes", [])
                if isinstance(lane_id, str) and "_" in lane_id
            )
            return candidate_edges
        except Exception:
            pass

    manifest_path = route_file.parent / "smart_crosswalk_manifest.json"
    if not manifest_path.exists():
        manifest_path = output_dir.parent / "sumo_nets" / "integrated_selected" / "smart_crosswalk_manifest.json"
    if manifest_path.exists():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            for row in manifest.get("crosswalks", []):
                candidate_edges.update(str(edge_id) for edge_id in row.get("vehicle_conflict_edges", []))
                candidate_edges.update(
                    str(lane_id).rsplit("_", 1)[0]
                    for lane_id in row.get("approach_lanes", [])
                    if isinstance(lane_id, str) and "_" in lane_id
                )
        except Exception:
            pass
    return candidate_edges


def _parse_vehicle_types(type_values: list[str]) -> tuple[int, int, int, list[str], list[str]]:
    suspicious_tokens = ("taxi", "bus", "truck")
    suspect_values: list[str] = []
    non_passenger_values: list[str] = []
    suspicious_count = 0
    for value in type_values:
        text = str(value or "").strip().lower()
        if not text:
            continue
        if any(token in text for token in suspicious_tokens):
            suspicious_count += 1
            suspect_values.append(text)
        elif text not in {"passenger", "car", "private"}:
            non_passenger_values.append(text)
    passenger_count = max(0, len(type_values) - suspicious_count)
    return passenger_count, suspicious_count, len(non_passenger_values), suspect_values, non_passenger_values


def _status_from_checks(*, fail: bool, warning: bool) -> tuple[str, str]:
    if fail:
        return "fail", "critical"
    if warning:
        return "warning", "warning"
    return "pass", "info"


def _final_reason(reasons: list[str]) -> str:
    seen: list[str] = []
    for reason in reasons:
        if reason and reason not in seen:
            seen.append(reason)
    return "|".join(seen)


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


def _as_float(value: Any) -> float | None:
    try:
        if value is None or pd.isna(value):
            return None
        return float(value)
    except Exception:
        return None


def _demand_rows_with_paths(demand_df: pd.DataFrame, run_dir: Path, output_dir: Path, nets_dir: Path) -> pd.DataFrame:
    if demand_df.empty:
        return demand_df
    df = demand_df.copy()
    if "seed" not in df.columns:
        df["seed"] = 0
    if "generated_vehicle_route_file" not in df.columns:
        df["generated_vehicle_route_file"] = ""
    if "generated_vehicle_trip_file" not in df.columns:
        df["generated_vehicle_trip_file"] = ""
    if "vehicle_net_file" not in df.columns:
        df["vehicle_net_file"] = ""
    if "baseline_sumocfg_file" not in df.columns:
        df["baseline_sumocfg_file"] = ""
    if "smart_sumocfg_file" not in df.columns:
        df["smart_sumocfg_file"] = ""
    if "generated_pedestrian_count" not in df.columns:
        df["generated_pedestrian_count"] = 0

    vehicle_route_paths: list[str] = []
    vehicle_trip_paths: list[str] = []
    ped_route_paths: list[str] = []
    for row in df.itertuples(index=False):
        vehicle_route_file = _path_or_empty(getattr(row, "generated_vehicle_route_file", ""))
        vehicle_trip_file = _path_or_empty(getattr(row, "generated_vehicle_trip_file", ""))
        seed = int(float(getattr(row, "seed", 0) or 0))
        ped_route_file = _infer_ped_route_file(vehicle_route_file, seed) if vehicle_route_file is not None else None
        vehicle_route_paths.append(str(vehicle_route_file) if vehicle_route_file is not None else "")
        vehicle_trip_paths.append(str(vehicle_trip_file) if vehicle_trip_file is not None else "")
        ped_route_paths.append(str(ped_route_file) if ped_route_file is not None else "")
    df["vehicle_route_file"] = vehicle_route_paths
    df["vehicle_trip_file"] = vehicle_trip_paths
    df["pedestrian_route_file"] = ped_route_paths
    return df


def _summarize_pedestrian_validation_group(
    group: pd.DataFrame,
    output_dir: Path,
) -> dict[str, Any]:
    first = group.iloc[0]
    seed = int(first.get("seed", 0) or 0)
    scenario_name = str(first.get("scenario_name", ""))
    vehicle_route_file = Path(str(first.get("vehicle_route_file", "")))
    ped_route_file = Path(str(first.get("pedestrian_route_file", "")))
    baseline_sumocfg_file = str(first.get("baseline_sumocfg_file", "") or "")
    smart_sumocfg_file = str(first.get("smart_sumocfg_file", "") or "")
    summary = compare_sumocfg_vehicle_inputs(baseline_sumocfg_file, smart_sumocfg_file)

    policy_expected_600s = int(pd.to_numeric(group.get("pedestrian_count_600s", pd.Series(dtype=float)), errors="coerce").fillna(0).sum())
    expected_count = int(pd.to_numeric(group.get("generated_pedestrian_count", pd.Series(dtype=float)), errors="coerce").fillna(0).sum())
    rows, depart_times, crosswalk_counts, walk_edges = _load_pedestrian_route_records(ped_route_file)
    generated_count = int(len(rows))
    count_diff = generated_count - expected_count
    count_diff_ratio = (count_diff / expected_count) if expected_count else 0.0
    trip_file = ped_route_file.with_name(ped_route_file.name.replace(".rou.xml", ".trips.xml"))
    trip_status = "not_applicable" if not trip_file.exists() else "available"

    dominant_crosswalk_id = ""
    dominant_crosswalk_share = 0.0
    if crosswalk_counts:
        dominant_crosswalk_id, dominant_crosswalk_count = max(crosswalk_counts.items(), key=lambda item: item[1])
        dominant_crosswalk_share = dominant_crosswalk_count / float(max(generated_count, 1))
    crosswalk_distribution_summary = "|".join(
        f"{key}:{value}" for key, value in sorted(crosswalk_counts.items(), key=lambda item: (-item[1], item[0]))[:10]
    )
    depart_time_histogram = _summarize_histogram(depart_times, bin_size=60.0)
    concentration_warning = bool(
        generated_count > 0
        and len(crosswalk_counts) > 1
        and dominant_crosswalk_share >= PEDESTRIAN_CONCENTRATION_WARNING_THRESHOLD
    )
    zero_generated_warning = generated_count == 0 and expected_count > 0
    mismatch = count_diff != 0
    route_missing = not ped_route_file.exists()

    baseline_route_file = summary.get("baseline_vehicle_route_file", "")
    smart_route_file = summary.get("smart_vehicle_route_file", "")
    baseline_trip_file = summary.get("baseline_vehicle_trip_file", "")
    smart_trip_file = summary.get("smart_vehicle_trip_file", "")
    baseline_net_file = summary.get("baseline_net_file", "")
    smart_net_file = summary.get("smart_net_file", "")
    baseline_same_route = summary.get("baseline_and_smart_same_vehicle_route_file", "not_checked")
    baseline_same_trip = summary.get("baseline_and_smart_same_trip_file", "not_checked")
    baseline_same_net = summary.get("baseline_and_smart_same_net_file", "not_checked")

    fail = route_missing or zero_generated_warning or mismatch
    warning = concentration_warning or trip_status == "not_applicable" or baseline_same_route == "not_checked"
    if baseline_same_route == "false" or baseline_same_trip == "false" or baseline_same_net == "false":
        fail = True
    if not fail and warning:
        status, severity = "warning", "warning"
    elif fail:
        status, severity = "fail", "critical"
    else:
        status, severity = "pass", "info"
    reasons = []
    if route_missing:
        reasons.append("pedestrian_route_missing")
    if zero_generated_warning:
        reasons.append("pedestrian_zero_generated")
    if mismatch:
        reasons.append("pedestrian_count_mismatch")
    if concentration_warning:
        reasons.append("pedestrian_crosswalk_concentration_high")
    if trip_status == "not_applicable":
        reasons.append("pedestrian_trip_not_applicable")
    if baseline_same_route == "false" or baseline_same_trip == "false" or baseline_same_net == "false":
        reasons.append("baseline_smart_sumocfg_mismatch")
    if baseline_same_route == "not_checked":
        reasons.append("baseline_smart_sumocfg_not_checked")

    return {
        "validation_name": "pedestrian_flow_policy_validation",
        "status": status,
        "severity": severity,
        "reason": _final_reason(reasons),
        "scenario_name": scenario_name,
        "seed": seed,
        "expected_count": expected_count,
        "generated_count": generated_count,
        "count_diff": count_diff,
        "count_diff_ratio": round(count_diff_ratio, 6),
        "policy_expected_count_600s": policy_expected_600s,
        "pedestrian_scale": float(first.get("pedestrian_scale", 0.0) or 0.0),
        "pedestrian_scale_source": str(first.get("pedestrian_scale_source", "")),
        "route_file": str(ped_route_file.resolve()) if ped_route_file.exists() else str(ped_route_file),
        "trip_file": str(trip_file.resolve()) if trip_status == "available" else "",
        "trip_file_status": trip_status,
        "sumocfg_file": str(Path(baseline_sumocfg_file).resolve()) if baseline_sumocfg_file else "",
        "baseline_sumocfg_file": str(Path(baseline_sumocfg_file).resolve()) if baseline_sumocfg_file else "",
        "smart_sumocfg_file": str(Path(smart_sumocfg_file).resolve()) if smart_sumocfg_file else "",
        "baseline_route_file": baseline_route_file,
        "smart_route_file": smart_route_file,
        "baseline_trip_file": baseline_trip_file,
        "smart_trip_file": smart_trip_file,
        "baseline_net_file": baseline_net_file,
        "smart_net_file": smart_net_file,
        "baseline_smart_same_route_file": baseline_same_route,
        "baseline_smart_same_trip_file": baseline_same_trip,
        "baseline_smart_same_net_file": baseline_same_net,
        "unique_depart_edges": 0,
        "unique_arrival_edges": 0,
        "unique_route_edges": 0,
        "total_driveable_edges": 0,
        "used_vehicle_edges": 0,
        "coverage_ratio": 0.0,
        "candidate_buffer_vehicle_share": "",
        "route_bbox_area_ratio": "",
        "grid_cells_used": "",
        "grid_cells_total": "",
        "grid_coverage_ratio": "",
        "depart_time_min": round(float(min(depart_times)), 3) if depart_times else "",
        "depart_time_max": round(float(max(depart_times)), 3) if depart_times else "",
        "depart_time_mean": round(float(sum(depart_times) / len(depart_times)), 3) if depart_times else "",
        "depart_time_p95": round(float(pd.Series(depart_times).quantile(0.95)), 3) if depart_times else "",
        "depart_time_histogram": depart_time_histogram,
        "crosswalk_distribution_summary": crosswalk_distribution_summary,
        "crosswalk_id_count": int(len(crosswalk_counts)),
        "dominant_crosswalk_id": dominant_crosswalk_id,
        "dominant_crosswalk_share": round(float(dominant_crosswalk_share), 6),
        "zero_generated_flag": int(generated_count == 0),
        "concentration_warning": int(concentration_warning),
    }


def _summarize_vehicle_validation_group(
    group: pd.DataFrame,
    output_dir: Path,
) -> dict[str, Any]:
    first = group.iloc[0]
    seed = int(first.get("seed", 0) or 0)
    scenario_name = str(first.get("scenario_name", ""))
    vehicle_route_file = Path(str(first.get("vehicle_route_file", first.get("generated_vehicle_route_file", ""))))
    vehicle_trip_file = Path(str(first.get("vehicle_trip_file", first.get("generated_vehicle_trip_file", ""))))
    baseline_sumocfg_file = str(first.get("baseline_sumocfg_file", "") or "")
    smart_sumocfg_file = str(first.get("smart_sumocfg_file", "") or "")
    summary = compare_sumocfg_vehicle_inputs(baseline_sumocfg_file, smart_sumocfg_file)

    expected_count = int(pd.to_numeric(group.get("total_vehicle_count_600s", pd.Series(dtype=float)), errors="coerce").fillna(0).iloc[0] if not group.empty else 0)
    policy_total_flow_vph = float(pd.to_numeric(group.get("total_vehicle_flow_vph", pd.Series(dtype=float)), errors="coerce").fillna(0).iloc[0] if not group.empty else 0.0)
    vehicle_type = str(first.get("vehicle_type", ""))
    passenger_ratio = float(first.get("passenger_ratio", 1.0) or 1.0)
    allocation_basis = str(first.get("allocation_basis", ""))
    road_group_file = output_dir / "road_group_allocation_summary.csv"
    road_group_allocation_sum = 0
    road_group_allocation_status = "missing"
    if road_group_file.exists():
        try:
            road_group_df = pd.read_csv(road_group_file, encoding="utf-8-sig")
            if not road_group_df.empty and "road_allocated_count_600s" in road_group_df.columns:
                road_group_allocation_sum = int(pd.to_numeric(road_group_df["road_allocated_count_600s"], errors="coerce").fillna(0).sum())
                road_group_allocation_status = "ok" if road_group_allocation_sum == expected_count else "mismatch"
            else:
                road_group_allocation_status = "missing"
        except Exception:
            road_group_allocation_status = "missing"
    else:
        road_group_allocation_sum = int(pd.to_numeric(group.get("road_allocated_count_600s", pd.Series(dtype=float)), errors="coerce").fillna(0).iloc[0] if "road_allocated_count_600s" in group.columns and not group.empty else 0)
        road_group_allocation_status = "ok" if road_group_allocation_sum == expected_count else "mismatch"
    route_rows, depart_times, route_edges, route_types = _load_vehicle_route_records(vehicle_route_file)
    trip_rows, trip_depart_times, trip_types = _load_trip_records(vehicle_trip_file)
    generated_count = int(len(route_rows))
    count_diff = generated_count - expected_count
    count_diff_ratio = (count_diff / expected_count) if expected_count else 0.0
    route_type_values = [row.get("type", "") for row in route_rows] + route_types + trip_types
    passenger_count, suspicious_count, non_passenger_count, suspicious_values, non_passenger_values = _parse_vehicle_types(route_type_values)
    trip_count_matches = (len(trip_rows) == generated_count) if vehicle_trip_file.exists() else False
    trip_status = "ok" if vehicle_trip_file.exists() else "missing"

    route_file_missing = not vehicle_route_file.exists()
    trip_file_missing = not vehicle_trip_file.exists()
    tolerance = VEHICLE_COUNT_TOLERANCE
    warning_tolerance = VEHICLE_WARNING_TOLERANCE
    count_ok = abs(count_diff) <= tolerance
    count_warn = not count_ok and abs(count_diff) <= warning_tolerance
    type_fail = suspicious_count > 0
    type_warn = non_passenger_count > 0 and not type_fail
    baseline_same_route = summary.get("baseline_and_smart_same_vehicle_route_file", "not_checked")
    baseline_same_trip = summary.get("baseline_and_smart_same_trip_file", "not_checked")
    baseline_same_net = summary.get("baseline_and_smart_same_net_file", "not_checked")
    route_mismatch = baseline_same_route == "false" or baseline_same_trip == "false" or baseline_same_net == "false"
    route_not_checked = baseline_same_route == "not_checked"
    if route_file_missing or trip_file_missing or not trip_count_matches:
        fail = True
    else:
        fail = (not count_ok and not count_warn) or type_fail or route_mismatch or road_group_allocation_status == "missing"
    warning = count_warn or type_warn or route_not_checked or road_group_allocation_sum != expected_count or not trip_count_matches
    if not fail and warning:
        status, severity = "warning", "warning"
    elif fail:
        status, severity = "fail", "critical"
    else:
        status, severity = "pass", "info"

    reasons = []
    if route_file_missing:
        reasons.append("vehicle_route_missing")
    if trip_file_missing:
        reasons.append("vehicle_trip_missing")
    if not trip_count_matches and not trip_file_missing:
        reasons.append("vehicle_route_trip_count_mismatch")
    if not count_ok and not count_warn:
        reasons.append("vehicle_count_out_of_tolerance")
    elif count_warn:
        reasons.append("vehicle_count_warning")
    if type_fail:
        reasons.append("vehicle_non_passenger_type_detected")
    elif type_warn:
        reasons.append("vehicle_non_passenger_type_warning")
    if road_group_allocation_sum != expected_count:
        reasons.append("road_group_allocation_sum_mismatch")
    if route_mismatch:
        reasons.append("baseline_smart_sumocfg_mismatch")
    if route_not_checked:
        reasons.append("baseline_smart_sumocfg_not_checked")

    route_ids = [row.get("id", "") for row in route_rows]
    trip_ids = [row.get("id", "") for row in trip_rows]
    depart_histogram = _summarize_histogram(depart_times, bin_size=60.0)
    route_net_file = str(first.get("vehicle_net_file", "") or "")
    total_driveable_edges = 0
    used_vehicle_edges = 0
    unique_route_edges = 0
    coverage_ratio = 0.0
    route_bbox_area_ratio = ""
    grid_cells_used = ""
    grid_cells_total = ""
    grid_coverage_ratio = ""
    candidate_buffer_vehicle_share: float | str = ""
    dominant_crosswalk_id = ""
    dominant_crosswalk_share = 0.0
    crosswalk_distribution_summary = ""
    if route_net_file:
        net_file = Path(route_net_file)
        total_driveable_edges, edge_map, driveable_edge_map = _load_driveable_edge_count(net_file)
        route_edge_set = set(route_edges)
        used_vehicle_edges = len(route_edge_set & set(driveable_edge_map.keys()))
        unique_route_edges = len(route_edge_set)
        coverage_ratio = (unique_route_edges / total_driveable_edges) if total_driveable_edges else 0.0
        route_bbox = _route_bbox(list(route_edge_set), driveable_edge_map)
        all_bbox = None
        all_boxes = [box for edge in driveable_edge_map.values() if (box := _edge_bbox(edge)) is not None]
        if all_boxes:
            all_bbox = (
                min(box[0] for box in all_boxes),
                min(box[1] for box in all_boxes),
                max(box[2] for box in all_boxes),
                max(box[3] for box in all_boxes),
            )
        route_bbox_area_ratio = round(_bbox_area(route_bbox) / _bbox_area(all_bbox), 6) if all_bbox else ""
        grid_cells_used, grid_cells_total, grid_coverage_ratio = _grid_coverage(list(route_edge_set), driveable_edge_map, grid_size=4)
        candidate_edges = _load_candidate_buffer_edges(vehicle_route_file, output_dir)
        if candidate_edges:
            candidate_buffer_vehicle_share = round(len(route_edge_set & candidate_edges) / float(max(len(route_edge_set), 1)), 6)
    crosswalk_counts: Counter[str] = Counter()
    if "crosswalk_id" in group.columns:
        for crosswalk_id, count in group["crosswalk_id"].astype(str).value_counts().items():
            crosswalk_counts[str(crosswalk_id)] += int(count)
    if crosswalk_counts:
        dominant_crosswalk_id, dominant_crosswalk_count = max(crosswalk_counts.items(), key=lambda item: item[1])
        dominant_crosswalk_share = dominant_crosswalk_count / float(max(int(crosswalk_counts.total()), 1))
    crosswalk_distribution_summary = "|".join(
        f"{key}:{value}" for key, value in sorted(crosswalk_counts.items(), key=lambda item: (-item[1], item[0]))[:10]
    )

    candidate_share_value = _as_float(candidate_buffer_vehicle_share)
    candidate_share_warning = candidate_share_value is not None and candidate_share_value >= VEHICLE_CANDIDATE_BUFFER_WARNING_THRESHOLD
    coverage_warning = coverage_ratio < GLOBAL_COVERAGE_WARNING_THRESHOLD
    grid_warning = isinstance(grid_coverage_ratio, float) and grid_coverage_ratio < GLOBAL_COVERAGE_MIN_GRID_SHARE
    if candidate_share_warning or coverage_warning or grid_warning:
        warning = True
    if route_mismatch:
        fail = True
    if route_file_missing or trip_file_missing:
        fail = True

    if not fail and warning:
        status, severity = "warning", "warning"
    elif fail:
        status, severity = "fail", "critical"
    else:
        status, severity = "pass", "info"

    return {
        "validation_name": "vehicle_flow_policy_validation",
        "status": status,
        "severity": severity,
        "reason": _final_reason(reasons),
        "scenario_name": scenario_name,
        "seed": seed,
        "expected_count": expected_count,
        "generated_count": generated_count,
        "count_diff": count_diff,
        "count_diff_ratio": round(count_diff_ratio, 6),
        "policy_total_flow_vph": policy_total_flow_vph,
        "policy_total_count_600s": expected_count,
        "vehicle_type": vehicle_type,
        "passenger_ratio": passenger_ratio,
        "allocation_basis": allocation_basis,
        "road_group_allocation_sum": road_group_allocation_sum,
        "road_group_allocation_status": "ok" if road_group_allocation_sum == expected_count else "mismatch",
        "route_file": str(vehicle_route_file.resolve()) if vehicle_route_file.exists() else str(vehicle_route_file),
        "trip_file": str(vehicle_trip_file.resolve()) if vehicle_trip_file.exists() else str(vehicle_trip_file),
        "sumocfg_file": str(Path(baseline_sumocfg_file).resolve()) if baseline_sumocfg_file else "",
        "baseline_sumocfg_file": str(Path(baseline_sumocfg_file).resolve()) if baseline_sumocfg_file else "",
        "smart_sumocfg_file": str(Path(smart_sumocfg_file).resolve()) if smart_sumocfg_file else "",
        "baseline_route_file": summary.get("baseline_vehicle_route_file", ""),
        "smart_route_file": summary.get("smart_vehicle_route_file", ""),
        "baseline_trip_file": summary.get("baseline_vehicle_trip_file", ""),
        "smart_trip_file": summary.get("smart_vehicle_trip_file", ""),
        "baseline_net_file": summary.get("baseline_net_file", ""),
        "smart_net_file": summary.get("smart_net_file", ""),
        "baseline_smart_same_route_file": baseline_same_route,
        "baseline_smart_same_trip_file": baseline_same_trip,
        "baseline_smart_same_net_file": baseline_same_net,
        "unique_depart_edges": int(len({row["edges"][0] for row in route_rows if row.get("edges")})),
        "unique_arrival_edges": int(len({row["edges"][-1] for row in route_rows if row.get("edges")})),
        "unique_route_edges": int(unique_route_edges),
        "total_driveable_edges": int(total_driveable_edges),
        "used_vehicle_edges": int(used_vehicle_edges),
        "coverage_ratio": round(float(coverage_ratio), 6),
        "candidate_buffer_vehicle_share": candidate_share_value if candidate_share_value is not None else "",
        "route_bbox_area_ratio": route_bbox_area_ratio,
        "grid_cells_used": int(grid_cells_used) if grid_cells_used != "" else "",
        "grid_cells_total": int(grid_cells_total) if grid_cells_total != "" else "",
        "grid_coverage_ratio": round(float(grid_coverage_ratio), 6) if grid_coverage_ratio != "" else "",
        "depart_time_min": round(float(min(depart_times)), 3) if depart_times else "",
        "depart_time_max": round(float(max(depart_times)), 3) if depart_times else "",
        "depart_time_mean": round(float(sum(depart_times) / len(depart_times)), 3) if depart_times else "",
        "depart_time_p95": round(float(pd.Series(depart_times).quantile(0.95)), 3) if depart_times else "",
        "depart_time_histogram": depart_histogram,
        "route_type_summary": _final_reason(
            [
                f"passenger:{passenger_count}",
                f"suspicious:{suspicious_count}",
                f"non_passenger:{non_passenger_count}",
            ]
        ),
        "non_passenger_type_values": "|".join(sorted(set(non_passenger_values))) if non_passenger_values else "",
        "suspicious_type_values": "|".join(sorted(set(suspicious_values))) if suspicious_values else "",
        "road_group_count": int(group["road_group"].astype(str).nunique()) if "road_group" in group.columns else 0,
        "road_group_summary": "|".join(
            f"{name}:{count}" for name, count in sorted(group["road_group"].astype(str).value_counts().items(), key=lambda item: (-item[1], item[0]))[:12]
        ) if "road_group" in group.columns else "",
        "crosswalk_distribution_summary": crosswalk_distribution_summary,
        "dominant_crosswalk_id": dominant_crosswalk_id,
        "dominant_crosswalk_share": round(float(dominant_crosswalk_share), 6),
        "trip_count": int(len(trip_rows)),
        "trip_count_matches_route": int(trip_count_matches),
        "vehicle_type_violation_count": int(suspicious_count),
        "vehicle_type_warning_count": int(non_passenger_count),
        "candidate_buffer_warning": int(candidate_share_warning),
        "global_coverage_warning": int(coverage_warning),
        "grid_coverage_warning": int(grid_warning),
    }


def _summarize_global_coverage_row(vehicle_row: dict[str, Any]) -> dict[str, Any]:
    candidate_share = _as_float(vehicle_row.get("candidate_buffer_vehicle_share", ""))
    coverage_ratio = float(vehicle_row.get("coverage_ratio", 0.0) or 0.0)
    grid_ratio = float(vehicle_row.get("grid_coverage_ratio", 0.0) or 0.0)
    route_bbox_ratio = vehicle_row.get("route_bbox_area_ratio", "")
    warning = bool(
        (candidate_share is not None and candidate_share >= VEHICLE_CANDIDATE_BUFFER_WARNING_THRESHOLD)
        or coverage_ratio < GLOBAL_COVERAGE_WARNING_THRESHOLD
        or grid_ratio < GLOBAL_COVERAGE_MIN_GRID_SHARE
    )
    fail = bool(vehicle_row.get("status") == "fail")
    if not fail and warning:
        status, severity = "warning", "warning"
    elif fail:
        status, severity = "fail", "critical"
    else:
        status, severity = "pass", "info"
    reasons = []
    if coverage_ratio < GLOBAL_COVERAGE_WARNING_THRESHOLD:
        reasons.append("vehicle_global_coverage_low")
    if candidate_share is not None and candidate_share >= VEHICLE_CANDIDATE_BUFFER_WARNING_THRESHOLD:
        reasons.append("candidate_buffer_vehicle_share_high")
    if grid_ratio < GLOBAL_COVERAGE_MIN_GRID_SHARE:
        reasons.append("vehicle_grid_coverage_low")
    if vehicle_row.get("baseline_smart_same_route_file") == "false" or vehicle_row.get("baseline_smart_same_trip_file") == "false" or vehicle_row.get("baseline_smart_same_net_file") == "false":
        reasons.append("baseline_smart_sumocfg_mismatch")
    return {
        "validation_name": "vehicle_global_coverage_validation",
        "status": status,
        "severity": severity,
        "reason": _final_reason(reasons),
        "scenario_name": vehicle_row.get("scenario_name", ""),
        "seed": vehicle_row.get("seed", 0),
        "expected_count": vehicle_row.get("expected_count", 0),
        "generated_count": vehicle_row.get("generated_count", 0),
        "count_diff": vehicle_row.get("count_diff", 0),
        "count_diff_ratio": vehicle_row.get("count_diff_ratio", 0.0),
        "route_file": vehicle_row.get("route_file", ""),
        "trip_file": vehicle_row.get("trip_file", ""),
        "sumocfg_file": vehicle_row.get("sumocfg_file", ""),
        "baseline_route_file": vehicle_row.get("baseline_route_file", ""),
        "smart_route_file": vehicle_row.get("smart_route_file", ""),
        "baseline_trip_file": vehicle_row.get("baseline_trip_file", ""),
        "smart_trip_file": vehicle_row.get("smart_trip_file", ""),
        "baseline_smart_same_route_file": vehicle_row.get("baseline_smart_same_route_file", "not_checked"),
        "baseline_smart_same_trip_file": vehicle_row.get("baseline_smart_same_trip_file", "not_checked"),
        "baseline_smart_same_net_file": vehicle_row.get("baseline_smart_same_net_file", "not_checked"),
        "unique_depart_edges": vehicle_row.get("unique_depart_edges", 0),
        "unique_arrival_edges": vehicle_row.get("unique_arrival_edges", 0),
        "unique_route_edges": vehicle_row.get("unique_route_edges", 0),
        "total_driveable_edges": vehicle_row.get("total_driveable_edges", 0),
        "used_vehicle_edges": vehicle_row.get("used_vehicle_edges", 0),
        "coverage_ratio": coverage_ratio,
        "candidate_buffer_vehicle_share": candidate_share,
        "route_bbox_area_ratio": route_bbox_ratio,
        "grid_cells_used": vehicle_row.get("grid_cells_used", ""),
        "grid_cells_total": vehicle_row.get("grid_cells_total", ""),
        "grid_coverage_ratio": grid_ratio,
        "depart_time_histogram": vehicle_row.get("depart_time_histogram", ""),
        "crosswalk_distribution_summary": vehicle_row.get("crosswalk_distribution_summary", ""),
        "dominant_crosswalk_id": vehicle_row.get("dominant_crosswalk_id", ""),
        "dominant_crosswalk_share": vehicle_row.get("dominant_crosswalk_share", 0.0),
    }


def validate_demand_run(run_dir: str | Path | None = None, output_dir: str | Path | None = None) -> dict[str, Path]:
    run_path, output_path, nets_path = _resolve_run_paths(run_dir, output_dir)
    output_path.mkdir(parents=True, exist_ok=True)
    demand_params_path = output_path / "demand_params.csv"
    demand_df = _read_csv(demand_params_path)

    rows: list[dict[str, Any]] = []
    if demand_df.empty:
        fail_rows = [
            {
                "validation_name": name,
                "status": "fail",
                "severity": "critical",
                "reason": "demand_params_missing",
                "scenario_name": "",
                "seed": "",
                "expected_count": 0,
                "generated_count": 0,
                "count_diff": 0,
                "count_diff_ratio": 0.0,
                "route_file": "",
                "trip_file": "",
                "sumocfg_file": "",
                "baseline_route_file": "",
                "smart_route_file": "",
                "baseline_smart_same_route_file": "not_checked",
                "baseline_smart_same_trip_file": "not_checked",
                "baseline_smart_same_net_file": "not_checked",
                "unique_depart_edges": 0,
                "unique_arrival_edges": 0,
                "unique_route_edges": 0,
                "total_driveable_edges": 0,
                "coverage_ratio": 0.0,
                "candidate_buffer_vehicle_share": "",
            }
            for name in (
                "pedestrian_flow_policy_validation",
                "vehicle_flow_policy_validation",
                "vehicle_global_coverage_validation",
            )
        ]
        rows.extend(fail_rows)
    else:
        demand_df = _demand_rows_with_paths(demand_df, run_path, output_path, nets_path)
        ped_groups = []
        vehicle_groups = []
        for route_file, group in demand_df.groupby("pedestrian_route_file", sort=False):
            ped_groups.append(_summarize_pedestrian_validation_group(group, output_path))
        for route_file, group in demand_df.groupby("vehicle_route_file", sort=False):
            vehicle_rows = _summarize_vehicle_validation_group(group, output_path)
            vehicle_groups.append(vehicle_rows)
        coverage_rows = [_summarize_global_coverage_row(row) for row in vehicle_groups]
        rows.extend(ped_groups)
        rows.extend(vehicle_groups)
        rows.extend(coverage_rows)

    summary_df = pd.DataFrame(rows)
    if not summary_df.empty:
        summary_df = summary_df.sort_values(["validation_name", "seed", "route_file"], kind="stable").reset_index(drop=True)

    ped_df = summary_df[summary_df["validation_name"] == "pedestrian_flow_policy_validation"].copy() if not summary_df.empty else pd.DataFrame()
    veh_df = summary_df[summary_df["validation_name"] == "vehicle_flow_policy_validation"].copy() if not summary_df.empty else pd.DataFrame()
    cov_df = summary_df[summary_df["validation_name"] == "vehicle_global_coverage_validation"].copy() if not summary_df.empty else pd.DataFrame()

    summary_path = output_path / VALIDATION_SUMMARY_FILENAME
    ped_path = output_path / PEDESTRIAN_VALIDATION_FILENAME
    veh_path = output_path / VEHICLE_VALIDATION_FILENAME
    cov_path = output_path / GLOBAL_COVERAGE_FILENAME
    report_path = output_path / REPORT_FILENAME

    write_csv_utf8_sig(summary_df, summary_path)
    write_csv_utf8_sig(ped_df, ped_path)
    write_csv_utf8_sig(veh_df, veh_path)
    write_csv_utf8_sig(cov_df, cov_path)

    report_lines = [
        "# Demand Validation Report",
        "",
        f"- Run dir: `{run_path}`",
        f"- Output dir: `{output_path}`",
        f"- Demand params: `{demand_params_path}`",
        "",
        "## Notes",
        "",
        "- Pedestrian validation uses the generated pedestrian route XML only; a pedestrian trip XML is currently not produced by the demand pipeline, so that field is marked `not_applicable`.",
        "- Baseline/smart equality is checked by parsing the generated `sumocfg` files and comparing their `route-files` and `net-file` entries.",
        "- Vehicle count tolerance is set to `5` vehicles for pass and `35` vehicles for warning.",
        "- Candidate-buffer overlap is treated as a warning when it is at least `0.5` of used route edges.",
        "- Global coverage warns when edge coverage is below `0.01` or grid coverage is below `0.25`.",
        "",
        "## Summary",
        "",
        _render_table(summary_df, ["validation_name", "status", "severity", "reason", "scenario_name", "seed", "expected_count", "generated_count", "count_diff", "count_diff_ratio"]),
        "",
        "## Pedestrian",
        "",
        _render_table(ped_df, ["validation_name", "status", "severity", "reason", "scenario_name", "seed", "expected_count", "generated_count", "count_diff", "count_diff_ratio", "depart_time_histogram", "crosswalk_distribution_summary", "dominant_crosswalk_id", "dominant_crosswalk_share"]),
        "",
        "## Vehicle",
        "",
        _render_table(veh_df, ["validation_name", "status", "severity", "reason", "scenario_name", "seed", "expected_count", "generated_count", "count_diff", "count_diff_ratio", "trip_count", "road_group_allocation_sum", "baseline_smart_same_route_file", "baseline_smart_same_trip_file", "baseline_smart_same_net_file"]),
        "",
        "## Global Coverage",
        "",
        _render_table(cov_df, ["validation_name", "status", "severity", "reason", "scenario_name", "seed", "coverage_ratio", "candidate_buffer_vehicle_share", "route_bbox_area_ratio", "grid_cells_used", "grid_cells_total", "grid_coverage_ratio"]),
        "",
        "## Output Files",
        "",
        f"- `{summary_path.name}`",
        f"- `{ped_path.name}`",
        f"- `{veh_path.name}`",
        f"- `{cov_path.name}`",
        f"- `{report_path.name}`",
    ]
    report_path.write_text("\n".join(report_lines) + "\n", encoding="utf-8-sig")

    return {
        "demand_validation_summary": summary_path,
        "pedestrian_flow_policy_validation": ped_path,
        "vehicle_flow_policy_validation": veh_path,
        "vehicle_global_coverage_validation": cov_path,
        "demand_validation_report": report_path,
    }
