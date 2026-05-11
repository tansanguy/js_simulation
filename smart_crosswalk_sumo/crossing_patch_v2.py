from __future__ import annotations

import argparse
import itertools
import hashlib
import json
import math
import re
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd
import sumolib

try:
    from .output_schema import write_csv_utf8_sig
except ImportError:
    from output_schema import write_csv_utf8_sig


DEFAULT_T2_PATH = Path(__file__).resolve().parent / "data" / "T2_crosswalk_features.csv"


def _as_str(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and math.isnan(value):
        return ""
    return str(value).strip()


def _as_float(value: Any) -> float | None:
    try:
        if value is None:
            return None
        num = float(value)
        if math.isnan(num):
            return None
        return num
    except Exception:
        return None


def _sha256_file(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            if not chunk:
                break
            hasher.update(chunk)
    return hasher.hexdigest()


def _utc_now_iso() -> str:
    return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def _edge_cluster(edge_id: str) -> str:
    eid = _as_str(edge_id)
    if not eid:
        return ""
    if eid.startswith(":"):
        return eid
    eid = eid.lstrip("-")
    return eid.split("#", 1)[0]


def _point_segment_distance(
    point: tuple[float, float],
    a: tuple[float, float],
    b: tuple[float, float],
) -> float:
    px, py = point
    ax, ay = a
    bx, by = b
    dx = bx - ax
    dy = by - ay
    if dx == 0 and dy == 0:
        return math.dist(point, a)
    t = ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)
    t = max(0.0, min(1.0, t))
    return math.dist(point, (ax + t * dx, ay + t * dy))


def _distance_to_edge_shape(edge: Any, xy: tuple[float, float]) -> float:
    shape = [(float(x), float(y)) for x, y in edge.getShape()]
    if not shape:
        return float("inf")
    if len(shape) == 1:
        return math.dist(xy, shape[0])
    return min(_point_segment_distance(xy, shape[i], shape[i + 1]) for i in range(len(shape) - 1))


def _bearing_deg(edge: Any) -> float | None:
    shape = [(float(x), float(y)) for x, y in edge.getShape()]
    if len(shape) < 2:
        return None
    x0, y0 = shape[0]
    x1, y1 = shape[-1]
    dx = x1 - x0
    dy = y1 - y0
    if dx == 0 and dy == 0:
        return None
    return (math.degrees(math.atan2(dy, dx)) + 360.0) % 360.0


def _edge_allows(edge: Any, vclass: str) -> bool:
    try:
        return bool(edge.allows(vclass))
    except Exception:
        return False


def _lane_allows(lane: Any, vclass: str) -> bool:
    try:
        return bool(lane.allows(vclass))
    except Exception:
        return False


def _safe_edge_allows(edge: Any, vclass: str) -> bool:
    try:
        return bool(edge.allows(vclass))
    except Exception:
        return False


def _find_first_column(columns: list[str], candidates: list[str]) -> str:
    lower_map = {c.lower(): c for c in columns}
    for cand in candidates:
        if cand in columns:
            return cand
        lc = cand.lower()
        if lc in lower_map:
            return lower_map[lc]
    return ""


def _load_t2_lonlat_map(path: Path) -> tuple[dict[str, tuple[float, float]], str, str, str]:
    if not path.exists():
        raise FileNotFoundError(f"T2 파일이 없습니다: {path}")
    df = pd.read_csv(path)
    cols = list(df.columns)

    id_col = _find_first_column(cols, ["crosswalk_id", "횡단보도ID", "crosswalkid"])
    lon_col = _find_first_column(cols, ["longitude", "lon"])
    lat_col = _find_first_column(cols, ["latitude", "lat"])
    if not id_col or not lon_col or not lat_col:
        return {}, id_col, lon_col, lat_col

    out: dict[str, tuple[float, float]] = {}
    for row in df.to_dict(orient="records"):
        cw_id = _as_str(row.get(id_col))
        lon = _as_float(row.get(lon_col))
        lat = _as_float(row.get(lat_col))
        if cw_id and lon is not None and lat is not None:
            out[cw_id] = (lon, lat)
    return out, id_col, lon_col, lat_col


def _resolve_target_lonlat(
    row: dict[str, Any],
    t2_map: dict[str, tuple[float, float]],
    t2_source_label: str,
) -> tuple[float | None, float | None, str]:
    cw_id = _as_str(row.get("crosswalk_id"))
    if cw_id in t2_map:
        lon, lat = t2_map[cw_id]
        return lon, lat, t2_source_label

    return None, None, "missing"


def _choose_vehicle_edges(
    net: sumolib.net.Net,
    target_xy: tuple[float, float],
    max_radius_m: float,
) -> tuple[list[tuple[Any, float]], dict[str, Any]]:
    x, y = target_xy
    nearby = net.getNeighboringEdges(x, y, float(max_radius_m))
    sorted_nearby = sorted(nearby, key=lambda item: float(item[1]))

    raw_neighbor_count = int(len(sorted_nearby))
    raw_nearest_edge_id = ""
    raw_nearest_edge_dist_m: float | None = None
    if sorted_nearby:
        raw_nearest_edge_id = _as_str(sorted_nearby[0][0].getID())
        raw_nearest_edge_dist_m = float(sorted_nearby[0][1])

    candidates: list[tuple[Any, float]] = []
    seen: set[str] = set()
    rejected_count = 0
    rejected_samples: list[str] = []

    for edge, dist in sorted_nearby:
        eid = _as_str(edge.getID())
        if not eid or eid in seen:
            continue
        seen.add(eid)
        fn = _as_str(edge.getFunction()).lower()

        if eid.startswith(":") or fn == "internal":
            rejected_count += 1
            if len(rejected_samples) < 5:
                rejected_samples.append(f"{eid}:internal:{float(dist):.2f}")
            continue
        if fn in {"walkingarea", "crossing"}:
            rejected_count += 1
            if len(rejected_samples) < 5:
                rejected_samples.append(f"{eid}:{fn}:{float(dist):.2f}")
            continue

        allows_ped = _safe_edge_allows(edge, "pedestrian")
        allows_vehicle = any(
            _safe_edge_allows(edge, v)
            for v in ("passenger", "private", "bus", "truck", "delivery", "taxi", "motorcycle", "emergency")
        )
        if allows_ped and not allows_vehicle:
            rejected_count += 1
            if len(rejected_samples) < 5:
                rejected_samples.append(f"{eid}:pedestrian_only:{float(dist):.2f}")
            continue

        candidates.append((edge, float(dist)))

    diag = {
        "raw_neighbor_edge_count_within_radius": raw_neighbor_count,
        "raw_nearest_edge_id": raw_nearest_edge_id,
        "raw_nearest_edge_dist_m": raw_nearest_edge_dist_m,
        "filtered_vehicle_edge_count_within_radius": int(len(candidates)),
        "vehicle_edge_filter_rejected_count": int(rejected_count),
        "vehicle_edge_filter_rejected_sample": "|".join(rejected_samples),
    }
    return candidates, diag


def _pick_edge_pair(candidates: list[tuple[Any, float]]) -> tuple[Any | None, Any | None]:
    if not candidates:
        return None, None
    first = candidates[0][0]
    first_cluster = _edge_cluster(first.getID())
    second: Any | None = None
    for edge, _ in candidates[1:]:
        if _edge_cluster(edge.getID()) != first_cluster:
            second = edge
            break
    if second is None and len(candidates) >= 2:
        second = candidates[1][0]
    return first, second


def _edge_touches_node(edge: Any, node_id: str) -> bool:
    nid = _as_str(node_id)
    if not nid:
        return False
    try:
        return _as_str(edge.getFromNode().getID()) == nid or _as_str(edge.getToNode().getID()) == nid
    except Exception:
        return False


def _touching_vehicle_edges(
    candidates: list[tuple[Any, float]],
    node_id: str,
) -> tuple[list[tuple[Any, float]], list[str]]:
    touching: list[tuple[Any, float]] = []
    non_touching_samples: list[str] = []
    for edge, dist in candidates:
        if _edge_touches_node(edge, node_id):
            touching.append((edge, float(dist)))
        elif len(non_touching_samples) < 5:
            non_touching_samples.append(f"{_as_str(edge.getID())}:{float(dist):.2f}")
    return touching, non_touching_samples


def _select_node_id(
    edges: list[Any],
    target_xy: tuple[float, float],
) -> str:
    nodes: dict[str, Any] = {}
    for edge in edges:
        for node in (edge.getFromNode(), edge.getToNode()):
            nid = _as_str(node.getID())
            if nid and nid not in nodes:
                nodes[nid] = node
    best_id = ""
    best_dist = float("inf")
    for nid, node in nodes.items():
        nx, ny = node.getCoord()
        d = math.hypot(float(nx) - target_xy[0], float(ny) - target_xy[1])
        if d < best_dist:
            best_dist = d
            best_id = nid
    return best_id


def _ped_edges_near_node(node: Any, target_xy: tuple[float, float]) -> list[Any]:
    candidates: dict[str, Any] = {}
    for edge in list(node.getIncoming()) + list(node.getOutgoing()):
        if _as_str(edge.getFunction()) != "normal":
            continue
        if not _edge_allows(edge, "pedestrian"):
            continue
        candidates[_as_str(edge.getID())] = edge

    scored = sorted(
        candidates.values(),
        key=lambda e: _distance_to_edge_shape(e, target_xy),
    )
    return scored


def _pick_ped_from_to(ped_edges: list[Any]) -> tuple[str, str]:
    if len(ped_edges) < 2:
        return "", ""
    first = ped_edges[0]
    first_cluster = _edge_cluster(first.getID())
    second = None
    for edge in ped_edges[1:]:
        if _edge_cluster(edge.getID()) != first_cluster:
            second = edge
            break
    if second is None:
        second = ped_edges[1]
    return _as_str(first.getID()), _as_str(second.getID())


def _conflict_vehicle_lanes(edges: list[Any]) -> list[str]:
    out: list[str] = []
    for edge in edges:
        for lane in edge.getLanes():
            if _lane_allows(lane, "passenger"):
                out.append(_as_str(lane.getID()))
    return sorted(set([x for x in out if x]))


def _build_tl_sets(net_root: ET.Element) -> tuple[set[str], set[str]]:
    tl_logic_ids = {
        _as_str(node.attrib.get("id"))
        for node in net_root.findall("tlLogic")
        if _as_str(node.attrib.get("id"))
    }
    connection_tl_ids = {
        _as_str(node.attrib.get("tl"))
        for node in net_root.findall("connection")
        if _as_str(node.attrib.get("tl"))
    }
    return tl_logic_ids, connection_tl_ids


def _crossing_key(node_id: str, edges_text: str) -> tuple[str, tuple[str, ...]] | None:
    node = _as_str(node_id)
    if not node:
        return None
    edges = sorted(set([tok for tok in _as_str(edges_text).split() if tok]))
    if len(edges) < 2:
        return None
    return node, tuple(edges)


def _crossing_key_str(node_id: str, edges_text: str) -> str:
    key = _crossing_key(node_id, edges_text)
    if key is None:
        return ""
    node, edges = key
    return f"{node}|{'|'.join(edges)}"


def _existing_crossing_keys_from_net(root: ET.Element) -> set[tuple[str, tuple[str, ...]]]:
    keys: set[tuple[str, tuple[str, ...]]] = set()
    for edge in root.findall("edge"):
        if _as_str(edge.attrib.get("function")).lower() != "crossing":
            continue
        node_id = _as_str(edge.attrib.get("from"))
        crossing_edges = _as_str(edge.attrib.get("crossingEdges"))
        key = _crossing_key(node_id, crossing_edges)
        if key is not None:
            keys.add(key)
    return keys


def _parse_crossing_node_id_from_edge_id(edge_id: str) -> str:
    # SUMO crossing edge id 예: :2959037339_c0
    m = re.match(r"^:([^_]+)_c\d+$", _as_str(edge_id))
    if not m:
        return ""
    return _as_str(m.group(1))


def _survived_crossing_keys_from_patched_net(root: ET.Element) -> set[tuple[str, tuple[str, ...]]]:
    keys: set[tuple[str, tuple[str, ...]]] = set()
    for edge in root.findall("edge"):
        if _as_str(edge.attrib.get("function")).lower() != "crossing":
            continue
        edge_id = _as_str(edge.attrib.get("id"))
        node_id = _parse_crossing_node_id_from_edge_id(edge_id)
        if not node_id:
            continue
        crossing_edges = _as_str(edge.attrib.get("crossingEdges"))
        key = _crossing_key(node_id, crossing_edges)
        if key is not None:
            keys.add(key)
    return keys


def _apply_crossing_duplicate_filters(
    candidate_rows: list[dict[str, Any]],
    net_root: ET.Element,
) -> list[dict[str, Any]]:
    existing_keys = _existing_crossing_keys_from_net(net_root)
    generated_keys: set[tuple[str, tuple[str, ...]]] = set()

    for row in candidate_rows:
        row["duplicate_crossing_key"] = ""
        row["duplicate_generated_crossing"] = False
        row["existing_crossing_duplicate"] = False
        row["written_to_patch_xml"] = False

        if not bool(row.get("can_generate_plain_con_xml_patch", False)):
            continue

        node_id = _as_str(row.get("candidate_junction_id"))
        edges_text = _as_str(row.get("candidate_crossing_road_edges"))
        key = _crossing_key(node_id, edges_text)
        row["duplicate_crossing_key"] = _crossing_key_str(node_id, edges_text)

        if key is None:
            row["can_generate_plain_con_xml_patch"] = False
            if not _as_str(row.get("rejected_reason")):
                row["rejected_reason"] = "invalid_crossing_key"
            continue

        if key in existing_keys:
            row["can_generate_plain_con_xml_patch"] = False
            row["existing_crossing_duplicate"] = True
            row["rejected_reason"] = "duplicate_existing_crossing"
            continue

        if key in generated_keys:
            row["can_generate_plain_con_xml_patch"] = False
            row["duplicate_generated_crossing"] = True
            row["rejected_reason"] = "duplicate_generated_crossing"
            continue

        generated_keys.add(key)
        row["written_to_patch_xml"] = True

    return candidate_rows


def _build_candidate_row(
    row: dict[str, Any],
    net: sumolib.net.Net,
    tl_logic_ids: set[str],
    connection_tl_ids: set[str],
    max_radius_m: float,
    t2_map: dict[str, tuple[float, float]],
    t2_source_label: str,
    selected_candidate_rank: int,
) -> dict[str, Any]:
    cw_id = _as_str(row.get("crosswalk_id"))
    lon, lat, target_source = _resolve_target_lonlat(row, t2_map, t2_source_label)

    out: dict[str, Any] = {
        "crosswalk_id": cw_id,
        "selected_candidate_rank": int(selected_candidate_rank),
        "registry_status": _as_str(row.get("registry_status")),
        "c_subtype": _as_str(row.get("c_subtype")),
        "match_distance_m": _as_float(row.get("match_distance_m")),
        "target_source": target_source,
        "original_csv_lon": lon,
        "original_csv_lat": lat,
        "target_lon": lon,
        "target_lat": lat,
        "target_x": None,
        "target_y": None,
        "coordinate_conversion_status": "pending",
        "matched_crossing_lon": _as_float(row.get("crossing_lon")),
        "matched_crossing_lat": _as_float(row.get("crossing_lat")),
        "previous_crossing_edge": _as_str(row.get("crossing_edge")),
        "nearest_vehicle_edge_1": "",
        "nearest_vehicle_edge_2": "",
        "candidate_junction_id": "",
        "candidate_crossing_road_edges": "",
        "crossing_edge_1_touches_node": False,
        "crossing_edge_2_touches_node": False,
        "all_crossing_edges_touch_node": False,
        "touching_vehicle_edge_count": 0,
        "touching_vehicle_edges_sample": "",
        "selected_crossing_edges_touch_node": False,
        "non_touching_selected_edges_sample": "",
        "from_edge": "",
        "to_edge": "",
        "vehicle_conflict_lanes": "",
        "vehicle_conflict_edges": "",
        "pedestrian_edge_count_near_junction": 0,
        "pedestrian_edge_status": "",
        "pedestrian_edge_warning": "",
        "raw_neighbor_edge_count_within_radius": 0,
        "raw_nearest_edge_id": "",
        "raw_nearest_edge_dist_m": None,
        "filtered_vehicle_edge_count_within_radius": 0,
        "vehicle_edge_filter_rejected_count": 0,
        "vehicle_edge_filter_rejected_sample": "",
        "vehicle_edge_count_within_radius": 0,
        "nearest_vehicle_edge_dist_m": None,
        "nearest_vehicle_edge_1_dist_m": None,
        "nearest_vehicle_edge_2_dist_m": None,
        "road_bearing_deg": None,
        "crossing_bearing_deg": None,
        "associated_tls_id": "",
        "patch_control_mode": "unsignalized",
        "proposed_registry_status": "",
        "plain_con_xml_patch_spec": "",
        "can_generate_plain_con_xml_patch": False,
        "rejected_reason": "",
    }

    if lon is None or lat is None:
        out["rejected_reason"] = "missing_original_csv_coordinates"
        out["coordinate_conversion_status"] = "missing_original_csv_coordinates"
        return out

    try:
        target_xy = net.convertLonLat2XY(float(lon), float(lat))
        out["target_x"] = float(target_xy[0])
        out["target_y"] = float(target_xy[1])
        out["coordinate_conversion_status"] = "ok"
    except Exception:
        out["rejected_reason"] = "coordinate_conversion_failed"
        out["coordinate_conversion_status"] = "coordinate_conversion_failed"
        return out

    vehicle_candidates, vehicle_diag = _choose_vehicle_edges(net, target_xy, max_radius_m)
    out.update(vehicle_diag)
    out["vehicle_edge_count_within_radius"] = int(out["filtered_vehicle_edge_count_within_radius"])
    seed_edge1, seed_edge2 = _pick_edge_pair(vehicle_candidates)
    if seed_edge1 is None or seed_edge2 is None:
        out["rejected_reason"] = "insufficient_vehicle_edges_within_radius"
        return out

    node_id = _select_node_id([seed_edge1, seed_edge2], target_xy)
    if not node_id:
        out["rejected_reason"] = "cannot_select_candidate_junction"
        return out
    out["candidate_junction_id"] = node_id

    touching_candidates, non_touching_samples = _touching_vehicle_edges(vehicle_candidates, node_id)
    out["touching_vehicle_edge_count"] = int(len(touching_candidates))
    out["touching_vehicle_edges_sample"] = "|".join(
        [f"{_as_str(edge.getID())}:{float(dist):.2f}" for edge, dist in touching_candidates[:5]]
    )
    out["non_touching_selected_edges_sample"] = "|".join(non_touching_samples)
    if len(touching_candidates) < 2:
        out["rejected_reason"] = "insufficient_touching_vehicle_edges_for_junction"
        return out

    edge1, edge2 = _pick_edge_pair(touching_candidates)
    if edge1 is None or edge2 is None:
        out["rejected_reason"] = "insufficient_touching_vehicle_edges_for_junction"
        return out

    out["nearest_vehicle_edge_1"] = _as_str(edge1.getID())
    out["nearest_vehicle_edge_2"] = _as_str(edge2.getID())
    out["nearest_vehicle_edge_1_dist_m"] = float(_distance_to_edge_shape(edge1, target_xy))
    out["nearest_vehicle_edge_2_dist_m"] = float(_distance_to_edge_shape(edge2, target_xy))
    out["nearest_vehicle_edge_dist_m"] = out["nearest_vehicle_edge_1_dist_m"]
    out["candidate_crossing_road_edges"] = f"{out['nearest_vehicle_edge_1']} {out['nearest_vehicle_edge_2']}"
    out["crossing_edge_1_touches_node"] = _edge_touches_node(edge1, node_id)
    out["crossing_edge_2_touches_node"] = _edge_touches_node(edge2, node_id)
    out["all_crossing_edges_touch_node"] = bool(
        out["crossing_edge_1_touches_node"] and out["crossing_edge_2_touches_node"]
    )
    out["selected_crossing_edges_touch_node"] = bool(out["all_crossing_edges_touch_node"])
    if not out["all_crossing_edges_touch_node"]:
        out["rejected_reason"] = "crossing_edges_do_not_touch_candidate_junction"
        return out

    try:
        node = net.getNode(node_id)
    except Exception:
        out["rejected_reason"] = "candidate_junction_not_found_in_net"
        return out

    ped_edges = _ped_edges_near_node(node, target_xy)
    out["pedestrian_edge_count_near_junction"] = int(len(ped_edges))
    ped_from, ped_to = _pick_ped_from_to(ped_edges)
    out["from_edge"] = ped_from
    out["to_edge"] = ped_to
    if ped_from and ped_to:
        out["pedestrian_edge_status"] = "ok"
    else:
        out["pedestrian_edge_status"] = "missing_or_insufficient"
        out["pedestrian_edge_warning"] = "insufficient_pedestrian_edges_near_junction"

    conflict_lanes = _conflict_vehicle_lanes([edge1, edge2])
    out["vehicle_conflict_lanes"] = "|".join(conflict_lanes)
    out["vehicle_conflict_edges"] = f"{out['nearest_vehicle_edge_1']}|{out['nearest_vehicle_edge_2']}"

    road_bearing = _bearing_deg(edge1)
    crossing_bearing = None if road_bearing is None else (road_bearing + 90.0) % 360.0
    out["road_bearing_deg"] = road_bearing
    out["crossing_bearing_deg"] = crossing_bearing

    tls_id = ""
    if node_id in tl_logic_ids and node_id in connection_tl_ids:
        tls_id = node_id
    out["associated_tls_id"] = tls_id
    out["patch_control_mode"] = "signalized" if tls_id else "unsignalized"

    patch_spec = f'<crossing node="{node_id}" edges="{out["candidate_crossing_road_edges"]}" priority="true"/>'
    out["plain_con_xml_patch_spec"] = patch_spec
    out["can_generate_plain_con_xml_patch"] = True
    out["proposed_registry_status"] = "crossing_patch_candidate"
    return out


def _write_connections_patch(rows: list[dict[str, Any]], out_path: Path) -> None:
    root = ET.Element("connections")
    for row in rows:
        if not bool(row.get("can_generate_plain_con_xml_patch", False)):
            continue
        node_id = _as_str(row.get("candidate_junction_id"))
        edges = _as_str(row.get("candidate_crossing_road_edges"))
        if not node_id or not edges:
            continue
        ET.SubElement(
            root,
            "crossing",
            {
                "node": node_id,
                "edges": edges,
                "priority": "true",
            },
        )
    ET.indent(root, space="  ")
    ET.ElementTree(root).write(out_path, encoding="utf-8", xml_declaration=True)


def _write_connections_patch_generic(
    rows: list[dict[str, Any]],
    out_path: Path,
    node_field: str,
    edges_field: str,
    write_flag_field: str,
) -> None:
    root = ET.Element("connections")
    for row in rows:
        if not bool(row.get(write_flag_field, False)):
            continue
        node_id = _as_str(row.get(node_field))
        edges = _as_str(row.get(edges_field))
        if not node_id or not edges:
            continue
        ET.SubElement(
            root,
            "crossing",
            {
                "node": node_id,
                "edges": edges,
                "priority": "true",
            },
        )
    ET.indent(root, space="  ")
    ET.ElementTree(root).write(out_path, encoding="utf-8", xml_declaration=True)


def _canonical_edges_text(edges_text: str) -> str:
    edges = sorted(set([tok for tok in _as_str(edges_text).split() if tok]))
    return " ".join(edges)


def _load_patch_crossing_keys_from_con(con_path: Path) -> set[tuple[str, tuple[str, ...]]]:
    if not con_path.exists():
        raise FileNotFoundError(f"patch connection 파일이 없습니다: {con_path}")
    root = ET.parse(con_path).getroot()
    keys: set[tuple[str, tuple[str, ...]]] = set()
    for crossing in root.findall("crossing"):
        key = _crossing_key(_as_str(crossing.attrib.get("node")), _as_str(crossing.attrib.get("edges")))
        if key is not None:
            keys.add(key)
    return keys


def _discard_reason_to_patch_result(reason: str) -> str:
    r = _as_str(reason).lower()
    if r == "no_walkingarea_found" or "no walkingarea found" in r:
        return "walkingarea_repair_required"
    if r == "has_no_target" or "has no target" in r:
        return "crossing_has_no_target"
    if r == "starts_and_ends_same_walkingarea" or "starts and ends at walkingarea" in r:
        return "pedestrian_topology_repair_required"
    return "netconvert_discarded_unclassified"


def _parse_netconvert_discard_reasons(
    log_path: Path,
    candidate_key_meta: dict[tuple[str, tuple[str, ...]], dict[str, Any]],
) -> dict[tuple[str, tuple[str, ...]], str]:
    if not log_path.exists():
        raise FileNotFoundError(f"netconvert 로그 파일이 없습니다: {log_path}")

    lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
    reasons: dict[tuple[str, tuple[str, ...]], str] = {}
    reason_phrase_to_code = {
        "no walkingarea found": "no_walkingarea_found",
        "has no target": "has_no_target",
        "starts and ends at walkingarea": "starts_and_ends_same_walkingarea",
    }
    candidate_keys = list(candidate_key_meta.keys())

    for raw_line in lines:
        line = _as_str(raw_line)
        low = line.lower()
        matched_phrase = ""
        matched_code = ""
        for phrase, code in reason_phrase_to_code.items():
            if phrase in low:
                matched_phrase = phrase
                matched_code = code
                break
        if not matched_phrase:
            continue

        node_hints: set[str] = set()
        m_junction = re.findall(r"junction '([^']+)'", line)
        for nid in m_junction:
            nid_s = _as_str(nid)
            if nid_s:
                node_hints.add(nid_s)
        m_crossing_id = re.findall(r":([^_'\s]+)_c\d+", line)
        for nid in m_crossing_id:
            nid_s = _as_str(nid)
            if nid_s:
                node_hints.add(nid_s)

        bracket_text = " ".join(re.findall(r"\[([^\]]+)\]", line))
        quoted_text = " ".join(re.findall(r"'([^']+)'", line))
        edge_text = f"{line} {bracket_text} {quoted_text}"

        matched_keys: list[tuple[str, tuple[str, ...]]] = []
        for key in candidate_keys:
            node_id, edge_tuple = key
            if node_hints and node_id not in node_hints:
                continue
            if all(edge_id in edge_text for edge_id in edge_tuple):
                matched_keys.append(key)

        if matched_keys:
            for key in matched_keys:
                reasons[key] = matched_code
            continue

        # starts-and-ends 경고는 edge 정보가 없는 경우가 많아 node-only fallback 허용
        if matched_code == "starts_and_ends_same_walkingarea" and node_hints:
            for key in candidate_keys:
                node_id, _ = key
                if node_id in node_hints:
                    reasons[key] = matched_code

    # fallback: if node/edge 매칭이 어려운 로그 포맷이면 quoted 토큰을 활용해 느슨히 매칭
    if len(reasons) < len(candidate_key_meta):
        for raw_line in lines:
            line = _as_str(raw_line)
            low = line.lower()
            matched_phrase = ""
            matched_code = ""
            for phrase, code in reason_phrase_to_code.items():
                if phrase in low:
                    matched_phrase = phrase
                    matched_code = code
                    break
            if not matched_phrase:
                continue
            quoted = re.findall(r"'([^']+)'", line)
            if not quoted:
                continue
            tokens = set()
            for q in quoted:
                for tok in q.split():
                    tokens.add(tok)
            for key in candidate_key_meta.keys():
                if key in reasons:
                    continue
                node_id, edge_tuple = key
                if node_id not in tokens and node_id not in line:
                    continue
                if all(e in tokens or e in line for e in edge_tuple):
                    reasons[key] = matched_code
                    break

    return reasons


def _bool_from_any(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    text = _as_str(value).lower()
    return text in {"1", "true", "t", "yes", "y"}


def _attempt_node_field(row: dict[str, Any]) -> str:
    return _as_str(row.get("node_id")) or _as_str(row.get("candidate_junction_id"))


def _attempt_edges_field(row: dict[str, Any]) -> str:
    return _as_str(row.get("crossing_edges")) or _as_str(row.get("candidate_crossing_road_edges"))


def _build_failed_key_set_from_previous(report_df: pd.DataFrame) -> set[tuple[str, tuple[str, ...]]]:
    if report_df.empty:
        return set()
    df = report_df.copy()
    if "patch_result" not in df.columns:
        return set()
    if "node_id" not in df.columns:
        return set()
    if "crossing_edges" not in df.columns:
        return set()
    out: set[tuple[str, tuple[str, ...]]] = set()
    for row in df.to_dict(orient="records"):
        result = _as_str(row.get("patch_result"))
        if result in {"patched_unsignalized", "duplicate_generated_crossing", "duplicate_existing_crossing"}:
            continue
        key = _crossing_key(_as_str(row.get("node_id")), _as_str(row.get("crossing_edges")))
        if key is not None:
            out.add(key)
    return out


def _iter_search_radii(max_radius_m: float, max_expanded_radius_m: float = 200.0) -> list[float]:
    seen: set[float] = set()
    radii: list[float] = []
    for r in sorted([float(max_radius_m), 125.0, 150.0, 175.0, float(max_expanded_radius_m)]):
        if r not in seen:
            seen.add(r)
            radii.append(r)
    return radii


def _collect_vehicle_candidates_with_optional_expansion(
    net: sumolib.net.Net,
    target_xy: tuple[float, float],
    max_radius_m: float,
    max_expanded_radius_m: float = 200.0,
    collect_all: bool = False,
) -> list[tuple[Any, float]]:
    merged: dict[str, tuple[Any, float]] = {}
    for radius in _iter_search_radii(max_radius_m, max_expanded_radius_m):
        candidates, _ = _choose_vehicle_edges(net, target_xy, radius)
        for edge, dist in candidates:
            eid = _as_str(edge.getID())
            if not eid:
                continue
            prev = merged.get(eid)
            if prev is None or float(dist) < float(prev[1]):
                merged[eid] = (edge, float(dist))
        if not collect_all and len(merged) >= 2:
            break
    return sorted(list(merged.values()), key=lambda item: float(item[1]))


def _build_junction_candidates_from_edges(edges_with_dist: list[tuple[Any, float]]) -> dict[str, float]:
    node_dist: dict[str, float] = {}
    for edge, dist in edges_with_dist:
        d = float(dist)
        for node in (edge.getFromNode(), edge.getToNode()):
            nid = _as_str(node.getID())
            if not nid:
                continue
            if nid not in node_dist or d < node_dist[nid]:
                node_dist[nid] = d
    return node_dist


def _edge_pair_score(
    net: sumolib.net.Net,
    target_xy: tuple[float, float],
    node_id: str,
    edge1: Any,
    edge2: Any,
    max_radius_m: float,
    prev_failed: set[tuple[str, tuple[str, ...]]],
) -> tuple[float, dict[str, Any]]:
    d1 = _distance_to_edge_shape(edge1, target_xy)
    d2 = _distance_to_edge_shape(edge2, target_xy)
    avg_dist = (float(d1) + float(d2)) / 2.0
    dist_score = max(0.0, (float(max_radius_m) - avg_dist) / max(1.0, float(max_radius_m))) * 100.0

    same_cluster = _edge_cluster(_as_str(edge1.getID())) == _edge_cluster(_as_str(edge2.getID()))
    base_diversity_bonus = 10.0 if not same_cluster else 0.0

    b1 = _bearing_deg(edge1)
    b2 = _bearing_deg(edge2)
    angle_sep_deg = None
    angle_bonus = 0.0
    if b1 is not None and b2 is not None:
        raw = abs(float(b1) - float(b2))
        angle_sep_deg = min(raw, 360.0 - raw)
        angle_bonus = (float(angle_sep_deg) / 180.0) * 20.0

    node_dist = float("inf")
    try:
        node = net.getNode(node_id)
        nx, ny = node.getCoord()
        node_dist = math.hypot(float(nx) - float(target_xy[0]), float(ny) - float(target_xy[1]))
    except Exception:
        node_dist = float(max_radius_m)
    node_bonus = max(0.0, (float(max_radius_m) - node_dist) / max(1.0, float(max_radius_m))) * 20.0

    pair_key = _crossing_key(node_id, f"{_as_str(edge1.getID())} {_as_str(edge2.getID())}")
    prev_fail_penalty = -30.0 if pair_key is not None and pair_key in prev_failed else 0.0

    score = dist_score + base_diversity_bonus + angle_bonus + node_bonus + prev_fail_penalty
    components = {
        "dist_score": round(dist_score, 6),
        "base_diversity_bonus": round(base_diversity_bonus, 6),
        "angle_bonus": round(angle_bonus, 6),
        "angle_sep_deg": None if angle_sep_deg is None else round(float(angle_sep_deg), 6),
        "node_bonus": round(node_bonus, 6),
        "prev_fail_penalty": round(prev_fail_penalty, 6),
        "avg_edge_dist_m": round(avg_dist, 6),
        "node_dist_m": round(node_dist, 6),
    }
    return float(score), components


def _select_crosswalk_ids_for_multi(
    reg_df: pd.DataFrame,
    previous_report_df: pd.DataFrame | None,
) -> set[str]:
    base = reg_df[
        (reg_df["registry_status"].astype(str) == "manual_review")
        & (reg_df["c_subtype"].astype(str) == "C1_distance_match_failed")
    ].copy()
    base["crosswalk_id"] = base["crosswalk_id"].astype(str)
    base_ids = set(base["crosswalk_id"].tolist())
    if previous_report_df is None or previous_report_df.empty or "crosswalk_id" not in previous_report_df.columns:
        return base_ids

    prev = previous_report_df.copy()
    prev["crosswalk_id"] = prev["crosswalk_id"].astype(str)
    allowed_fail = {
        "netconvert_discarded_unclassified",
        "walkingarea_repair_required",
        "pedestrian_topology_repair_required",
        "crossing_has_no_target",
        "duplicate_generated_crossing",
    }

    selected: set[str] = set()
    for cw_id, grp in prev.groupby("crosswalk_id"):
        if cw_id not in base_ids:
            continue
        results = set([_as_str(v) for v in grp.get("patch_result", pd.Series(dtype=str)).tolist()])
        if "patched_unsignalized" in results:
            continue
        if not results:
            selected.add(cw_id)
            continue
        if results.issubset({"duplicate_generated_crossing"}):
            # alias-only 후보는 대표 실패가 확인되지 않으면 재생성하지 않음
            continue
        if any(r in allowed_fail for r in results):
            selected.add(cw_id)
    return selected


def _representative_by_key(rows: list[dict[str, Any]]) -> dict[tuple[str, tuple[str, ...]], str]:
    rep: dict[tuple[str, tuple[str, ...]], str] = {}
    for row in rows:
        key = _crossing_key(_as_str(row.get("node_id")), _as_str(row.get("crossing_edges")))
        if key is None:
            continue
        cw = _as_str(row.get("crosswalk_id"))
        prev = rep.get(key, "")
        if not prev or cw < prev:
            rep[key] = cw
    return rep


def build_final_c1_report(args: argparse.Namespace) -> dict[str, Path]:
    v2_report_path = Path(args.v2_survival_report)
    v3_best_path = Path(args.v3_best_report)
    v3_attempt_path = Path(args.v3_attempt_report)
    registry_path = Path(args.registry_path)
    output_dir = Path(args.output_dir)

    for path_obj, label in (
        (v2_report_path, "v2 survival report"),
        (v3_best_path, "v3 best-by-crosswalk report"),
        (v3_attempt_path, "v3 survival-attempt report"),
        (registry_path, "registry"),
    ):
        if not path_obj.exists():
            raise FileNotFoundError(f"{label} 파일이 없습니다: {path_obj}")

    output_dir.mkdir(parents=True, exist_ok=True)

    v2_df = pd.read_csv(v2_report_path)
    v3_best_df = pd.read_csv(v3_best_path)
    v3_attempt_df = pd.read_csv(v3_attempt_path)
    reg_df = pd.read_csv(registry_path)

    for df in (v2_df, v3_best_df, v3_attempt_df):
        if "crosswalk_id" in df.columns:
            df["crosswalk_id"] = df["crosswalk_id"].astype(str)
        if "node_id" in df.columns:
            df["node_id"] = df["node_id"].map(_as_str)
        if "crossing_edges" in df.columns:
            df["crossing_edges"] = df["crossing_edges"].map(_canonical_edges_text)

    reg_df["crosswalk_id"] = reg_df["crosswalk_id"].astype(str)
    c1_df = reg_df[
        (reg_df["registry_status"].astype(str) == "manual_review")
        & (reg_df["c_subtype"].astype(str) == "C1_distance_match_failed")
    ][["crosswalk_id"]].copy()

    v2_map = {row["crosswalk_id"]: row for row in v2_df.to_dict(orient="records")}
    v3_best_map = {row["crosswalk_id"]: row for row in v3_best_df.to_dict(orient="records")}

    patched_v2_ids = set(
        v2_df.loc[v2_df.get("patch_result", pd.Series(dtype=str)).astype(str) == "patched_unsignalized", "crosswalk_id"]
    )
    patched_v3_ids = set(
        v3_best_df.loc[
            v3_best_df.get("patch_result", pd.Series(dtype=str)).astype(str) == "patched_unsignalized",
            "crosswalk_id",
        ]
    )
    patched_ids = patched_v2_ids | patched_v3_ids

    key_to_representative_any: dict[str, str] = {}
    for row in v3_attempt_df.to_dict(orient="records"):
        key = _as_str(row.get("duplicate_crossing_key")) or _crossing_key_str(
            _as_str(row.get("node_id")),
            _as_str(row.get("crossing_edges")),
        )
        if not key:
            continue
        cw_id = _as_str(row.get("crosswalk_id"))
        prev = key_to_representative_any.get(key, "")
        if not prev or cw_id < prev:
            key_to_representative_any[key] = cw_id

    patched_rows_for_key = pd.concat(
        [
            v2_df[v2_df.get("patch_result", pd.Series(dtype=str)).astype(str) == "patched_unsignalized"],
            v3_attempt_df[v3_attempt_df.get("patch_result", pd.Series(dtype=str)).astype(str) == "patched_unsignalized"],
            v3_best_df[v3_best_df.get("patch_result", pd.Series(dtype=str)).astype(str) == "patched_unsignalized"],
        ],
        ignore_index=True,
    )
    key_to_patched_representative: dict[str, str] = {}
    for row in patched_rows_for_key.to_dict(orient="records"):
        key = _as_str(row.get("duplicate_crossing_key")) or _crossing_key_str(
            _as_str(row.get("node_id")),
            _as_str(row.get("crossing_edges")),
        )
        cw_id = _as_str(row.get("crosswalk_id"))
        if not key or not cw_id:
            continue
        prev = key_to_patched_representative.get(key, "")
        if not prev or cw_id < prev:
            key_to_patched_representative[key] = cw_id

    repair_required_statuses = {
        "netconvert_discarded_unclassified",
        "pedestrian_topology_repair_required",
        "crossing_has_no_target",
        "walkingarea_repair_required",
    }

    report_rows: list[dict[str, Any]] = []
    for row in c1_df.to_dict(orient="records"):
        cw_id = _as_str(row.get("crosswalk_id"))
        src = v3_best_map.get(cw_id) or v2_map.get(cw_id) or {"crosswalk_id": cw_id}

        patch_result = _as_str(src.get("patch_result"))
        patch_survived = _bool_from_any(src.get("patch_survived"))
        node_id = _as_str(src.get("node_id"))
        crossing_edges = _canonical_edges_text(_as_str(src.get("crossing_edges")))
        duplicate_crossing_key = _as_str(src.get("duplicate_crossing_key")) or _crossing_key_str(node_id, crossing_edges)
        representative_crosswalk_id = _as_str(src.get("representative_crosswalk_id"))
        if not representative_crosswalk_id and duplicate_crossing_key:
            representative_crosswalk_id = _as_str(key_to_representative_any.get(duplicate_crossing_key, ""))

        final_status = patch_result or "unknown"
        if cw_id in patched_v2_ids:
            final_status = "patched_unsignalized_v2"
            patch_survived = True
        elif cw_id in patched_v3_ids:
            final_status = "patched_unsignalized_v3"
            patch_survived = True
        elif patch_result == "duplicate_generated_crossing":
            inferred_patched_rep = _as_str(key_to_patched_representative.get(duplicate_crossing_key, ""))
            if representative_crosswalk_id and representative_crosswalk_id in patched_ids:
                final_status = "represented_by_patched_crossing"
            elif inferred_patched_rep and inferred_patched_rep in patched_ids:
                representative_crosswalk_id = inferred_patched_rep
                final_status = "represented_by_patched_crossing"
            else:
                final_status = "duplicate_unresolved"
            patch_survived = False
        elif patch_result in repair_required_statuses:
            final_status = patch_result
            patch_survived = False

        report_rows.append(
            {
                "crosswalk_id": cw_id,
                "final_status": final_status,
                "patch_result": patch_result,
                "patch_survived": bool(patch_survived),
                "node_id": node_id,
                "crossing_edges": crossing_edges,
                "representative_crosswalk_id": representative_crosswalk_id,
                "duplicate_crossing_key": duplicate_crossing_key,
                "original_csv_lon": _as_float(src.get("original_csv_lon")),
                "original_csv_lat": _as_float(src.get("original_csv_lat")),
                "target_x": _as_float(src.get("target_x")),
                "target_y": _as_float(src.get("target_y")),
            }
        )

    report_df = pd.DataFrame(report_rows)
    report_path = output_dir / "c1_final_coverage_report.csv"
    write_csv_utf8_sig(report_df, report_path)

    patched_v2_count = int((report_df["final_status"] == "patched_unsignalized_v2").sum())
    patched_v3_count = int((report_df["final_status"] == "patched_unsignalized_v3").sum())
    represented_count = int((report_df["final_status"] == "represented_by_patched_crossing").sum())
    duplicate_unresolved_count = int((report_df["final_status"] == "duplicate_unresolved").sum())
    repair_required_count = int(report_df["final_status"].isin(repair_required_statuses).sum())
    summary_df = pd.DataFrame(
        [
            {
                "total_c1_count": int(len(report_df)),
                "patched_unsignalized_v2_count": patched_v2_count,
                "patched_unsignalized_v3_count": patched_v3_count,
                "represented_by_patched_crossing_count": represented_count,
                "duplicate_unresolved_count": duplicate_unresolved_count,
                "repair_required_count": repair_required_count,
                "final_covered_count": int(patched_v2_count + patched_v3_count + represented_count),
            }
        ]
    )
    summary_path = output_dir / "c1_final_summary.csv"
    write_csv_utf8_sig(summary_df, summary_path)

    return {
        "c1_final_coverage_report": report_path,
        "c1_final_summary": summary_path,
    }


def generate(args: argparse.Namespace) -> dict[str, Path]:
    registry_path = Path(args.registry_path)
    net_file = Path(args.net_file)
    output_dir = Path(args.output_dir)
    t2_path = Path(args.t2_path)

    if not registry_path.exists():
        raise FileNotFoundError(f"registry 파일이 없습니다: {registry_path}")
    if not net_file.exists():
        raise FileNotFoundError(f"network 파일이 없습니다: {net_file}")

    output_dir.mkdir(parents=True, exist_ok=True)

    reg_df = pd.read_csv(registry_path)
    filtered = reg_df[
        (reg_df["registry_status"].astype(str) == "manual_review")
        & (reg_df["c_subtype"].astype(str) == "C1_distance_match_failed")
    ].copy()

    filtered["crosswalk_id"] = filtered["crosswalk_id"].astype(str)
    filtered["match_distance_m_num"] = pd.to_numeric(filtered["match_distance_m"], errors="coerce")
    filtered["has_from_to"] = filtered.apply(
        lambda r: bool(_as_str(r.get("from_edge")) and _as_str(r.get("to_edge"))),
        axis=1,
    )
    filtered["has_vehicle_ctx"] = filtered.apply(
        lambda r: bool(_as_str(r.get("vehicle_conflict_edges")) or _as_str(r.get("approach_lanes"))),
        axis=1,
    )
    filtered["has_crossing_edge"] = filtered.apply(
        lambda r: bool(_as_str(r.get("crossing_edge"))),
        axis=1,
    )
    filtered = filtered.sort_values(
        ["match_distance_m_num", "has_from_to", "has_vehicle_ctx", "has_crossing_edge", "crosswalk_id"],
        ascending=[True, False, False, False, True],
        kind="stable",
    )
    filtered["selected_candidate_rank"] = range(1, len(filtered) + 1)
    if int(args.limit) > 0:
        filtered = filtered.head(int(args.limit)).copy()

    net = sumolib.net.readNet(str(net_file), withInternal=True, withPedestrianConnections=True)
    net_root = ET.parse(net_file).getroot()
    tl_logic_ids, connection_tl_ids = _build_tl_sets(net_root)

    t2_map, t2_id_col, t2_lon_col, t2_lat_col = _load_t2_lonlat_map(t2_path)
    t2_source_label = (
        f"t2:{t2_path}:{t2_id_col}/{t2_lon_col}/{t2_lat_col}"
        if t2_id_col and t2_lon_col and t2_lat_col
        else f"t2:{t2_path}:missing_coordinate_columns"
    )

    candidate_rows: list[dict[str, Any]] = []
    for row in filtered.to_dict(orient="records"):
        candidate_rows.append(
            _build_candidate_row(
                row=row,
                net=net,
                tl_logic_ids=tl_logic_ids,
                connection_tl_ids=connection_tl_ids,
                max_radius_m=float(args.max_radius_m),
                t2_map=t2_map,
                t2_source_label=t2_source_label,
                selected_candidate_rank=int(row.get("selected_candidate_rank", 0)),
            )
        )
    candidate_rows = _apply_crossing_duplicate_filters(candidate_rows, net_root)

    candidates_df = pd.DataFrame(candidate_rows)
    candidates_path = output_dir / "crossing_patch_candidates.csv"
    write_csv_utf8_sig(candidates_df, candidates_path)

    patch_rows = [row for row in candidate_rows if bool(row.get("can_generate_plain_con_xml_patch", False))]
    patch_con_path = output_dir / "patched_crossings.con.xml"
    _write_connections_patch(patch_rows, patch_con_path)

    reg_v2_df = reg_df.copy()
    reg_v2_df["crosswalk_id"] = reg_v2_df["crosswalk_id"].astype(str)
    generated_ids = {str(row.get("crosswalk_id")) for row in patch_rows}
    mask = reg_v2_df["crosswalk_id"].astype(str).isin(generated_ids)
    reg_v2_df.loc[mask, "registry_status"] = "crossing_patch_candidate"
    registry_v2_path = output_dir / "junggu_crosswalk_sumo_registry_v2.csv"
    write_csv_utf8_sig(reg_v2_df, registry_v2_path)

    provenance = {
        "base_network_path": str(net_file),
        "base_network_sha256": _sha256_file(net_file),
        "registry_path": str(registry_path),
        "registry_sha256": _sha256_file(registry_path),
        "generated_at": _utc_now_iso(),
        "candidate_count": int(len(generated_ids)),
        "max_radius_m": float(args.max_radius_m),
    }
    provenance_path = output_dir / "patched_network_provenance.json"
    provenance_path.write_text(json.dumps(provenance, ensure_ascii=False, indent=2), encoding="utf-8")

    return {
        "crossing_patch_candidates": candidates_path,
        "patched_crossings_con": patch_con_path,
        "patched_network_provenance": provenance_path,
        "registry_v2": registry_v2_path,
    }


def generate_multi(args: argparse.Namespace) -> dict[str, Path]:
    registry_path = Path(args.registry_path)
    net_file = Path(args.net_file)
    output_dir = Path(args.output_dir)
    t2_path = Path(args.t2_path)
    previous_survival_report = Path(args.previous_survival_report) if _as_str(args.previous_survival_report) else None
    target_ids_csv = Path(args.target_ids_csv) if _as_str(getattr(args, "target_ids_csv", "")) else None
    target_id_col = _as_str(getattr(args, "target_id_col", "crosswalk_id")) or "crosswalk_id"
    max_expanded_radius_m = float(getattr(args, "max_expanded_radius_m", 200.0))
    exclude_previous_failed_keys = _bool_from_any(getattr(args, "exclude_previous_failed_keys", "true"))

    if not registry_path.exists():
        raise FileNotFoundError(f"registry 파일이 없습니다: {registry_path}")
    if not net_file.exists():
        raise FileNotFoundError(f"network 파일이 없습니다: {net_file}")
    if previous_survival_report and not previous_survival_report.exists():
        raise FileNotFoundError(f"previous survival report 파일이 없습니다: {previous_survival_report}")
    if target_ids_csv is not None and not target_ids_csv.exists():
        raise FileNotFoundError(f"target_ids_csv 파일이 없습니다: {target_ids_csv}")

    output_dir.mkdir(parents=True, exist_ok=True)

    reg_df = pd.read_csv(registry_path)
    reg_df["crosswalk_id"] = reg_df["crosswalk_id"].astype(str)
    prev_df = pd.read_csv(previous_survival_report) if previous_survival_report else None
    if prev_df is not None and "crosswalk_id" in prev_df.columns:
        prev_df["crosswalk_id"] = prev_df["crosswalk_id"].astype(str)

    if target_ids_csv is not None:
        ids_df = pd.read_csv(target_ids_csv)
        if target_id_col not in ids_df.columns:
            raise ValueError(f"target_ids_csv에 '{target_id_col}' 컬럼이 없습니다: {target_ids_csv}")
        csv_ids = set(ids_df[target_id_col].astype(str).tolist())
        c1_base_ids = set(
            reg_df[
                (reg_df["registry_status"].astype(str) == "manual_review")
                & (reg_df["c_subtype"].astype(str) == "C1_distance_match_failed")
            ]["crosswalk_id"].astype(str).tolist()
        )
        target_ids = csv_ids & c1_base_ids
        is_residual_mode = True
    else:
        target_ids = _select_crosswalk_ids_for_multi(reg_df, prev_df)
        is_residual_mode = False

    target_df = reg_df[
        (reg_df["registry_status"].astype(str) == "manual_review")
        & (reg_df["c_subtype"].astype(str) == "C1_distance_match_failed")
        & (reg_df["crosswalk_id"].astype(str).isin(target_ids))
    ].copy()
    target_df["match_distance_m_num"] = pd.to_numeric(target_df["match_distance_m"], errors="coerce")
    target_df = target_df.sort_values(["match_distance_m_num", "crosswalk_id"], ascending=[True, True], kind="stable")

    net = sumolib.net.readNet(str(net_file), withInternal=True, withPedestrianConnections=True)
    net_root = ET.parse(net_file).getroot()
    existing_keys = _existing_crossing_keys_from_net(net_root)
    prev_failed_keys = _build_failed_key_set_from_previous(prev_df if prev_df is not None else pd.DataFrame())

    t2_map, t2_id_col, t2_lon_col, t2_lat_col = _load_t2_lonlat_map(t2_path)
    t2_source_label = (
        f"t2:{t2_path}:{t2_id_col}/{t2_lon_col}/{t2_lat_col}"
        if t2_id_col and t2_lon_col and t2_lat_col
        else f"t2:{t2_path}:missing_coordinate_columns"
    )

    attempts_per_crosswalk = max(1, int(args.attempts_per_crosswalk))
    attempt_rows: list[dict[str, Any]] = []

    for reg_row in target_df.to_dict(orient="records"):
        cw_id = _as_str(reg_row.get("crosswalk_id"))
        lon, lat, target_source = _resolve_target_lonlat(reg_row, t2_map, t2_source_label)

        if lon is None or lat is None:
            attempt_rows.append(
                {
                    "crosswalk_id": cw_id,
                    "attempt_id": f"{cw_id}_a0",
                    "attempt_rank": 1,
                    "node_id": "",
                    "crossing_edges": "",
                    "original_csv_lon": None,
                    "original_csv_lat": None,
                    "target_lon": None,
                    "target_lat": None,
                    "target_x": None,
                    "target_y": None,
                    "target_source": target_source,
                    "score": None,
                    "score_components": "",
                    "plain_con_xml_patch_spec": "",
                    "written_to_patch_xml": False,
                    "can_generate_plain_con_xml_patch": False,
                    "duplicate_crossing_key": "",
                    "representative_crosswalk_id": "",
                    "duplicate_generated_crossing": False,
                    "existing_crossing_duplicate": False,
                    "rejected_reason": "missing_original_csv_coordinates",
                }
            )
            continue

        try:
            tx, ty = net.convertLonLat2XY(float(lon), float(lat))
            target_xy = (float(tx), float(ty))
        except Exception:
            attempt_rows.append(
                {
                    "crosswalk_id": cw_id,
                    "attempt_id": f"{cw_id}_a0",
                    "attempt_rank": 1,
                    "node_id": "",
                    "crossing_edges": "",
                    "original_csv_lon": float(lon),
                    "original_csv_lat": float(lat),
                    "target_lon": float(lon),
                    "target_lat": float(lat),
                    "target_x": None,
                    "target_y": None,
                    "target_source": target_source,
                    "score": None,
                    "score_components": "",
                    "plain_con_xml_patch_spec": "",
                    "written_to_patch_xml": False,
                    "can_generate_plain_con_xml_patch": False,
                    "duplicate_crossing_key": "",
                    "representative_crosswalk_id": "",
                    "duplicate_generated_crossing": False,
                    "existing_crossing_duplicate": False,
                    "rejected_reason": "coordinate_conversion_failed",
                }
            )
            continue

        vehicle_candidates = _collect_vehicle_candidates_with_optional_expansion(
            net, target_xy, float(args.max_radius_m),
            max_expanded_radius_m=max_expanded_radius_m,
            collect_all=is_residual_mode,
        )
        if len(vehicle_candidates) < 2:
            attempt_rows.append(
                {
                    "crosswalk_id": cw_id,
                    "attempt_id": f"{cw_id}_a0",
                    "attempt_rank": 1,
                    "node_id": "",
                    "crossing_edges": "",
                    "original_csv_lon": float(lon),
                    "original_csv_lat": float(lat),
                    "target_lon": float(lon),
                    "target_lat": float(lat),
                    "target_x": float(target_xy[0]),
                    "target_y": float(target_xy[1]),
                    "target_source": target_source,
                    "score": None,
                    "score_components": "",
                    "plain_con_xml_patch_spec": "",
                    "written_to_patch_xml": False,
                    "can_generate_plain_con_xml_patch": False,
                    "duplicate_crossing_key": "",
                    "representative_crosswalk_id": "",
                    "duplicate_generated_crossing": False,
                    "existing_crossing_duplicate": False,
                    "rejected_reason": "insufficient_vehicle_edges_within_radius",
                }
            )
            continue

        junction_seed = _build_junction_candidates_from_edges(vehicle_candidates)
        touching_by_node: dict[str, list[tuple[Any, float]]] = {}
        for node_id in sorted(junction_seed.keys(), key=lambda n: float(junction_seed.get(n, float("inf")))):
            touching, _ = _touching_vehicle_edges(vehicle_candidates, node_id)
            if len(touching) >= 2:
                touching_by_node[node_id] = touching

        scored_candidates: list[dict[str, Any]] = []
        seen_local: set[tuple[str, tuple[str, ...]]] = set()

        for node_id, touch_list in touching_by_node.items():
            touch_sorted = sorted(touch_list, key=lambda item: float(item[1]))
            edges = [edge for edge, _ in touch_sorted]
            for edge1, edge2 in itertools.combinations(edges, 2):
                e1 = _as_str(edge1.getID())
                e2 = _as_str(edge2.getID())
                if not e1 or not e2 or e1 == e2:
                    continue
                pair_key = _crossing_key(node_id, f"{e1} {e2}")
                if pair_key is None or pair_key in seen_local:
                    continue
                seen_local.add(pair_key)
                if pair_key in existing_keys:
                    continue
                score, components = _edge_pair_score(
                    net=net,
                    target_xy=target_xy,
                    node_id=node_id,
                    edge1=edge1,
                    edge2=edge2,
                    max_radius_m=float(args.max_radius_m),
                    prev_failed=prev_failed_keys,
                )
                edges_text = " ".join(pair_key[1])
                scored_candidates.append(
                    {
                        "crosswalk_id": cw_id,
                        "node_id": node_id,
                        "crossing_edges": edges_text,
                        "original_csv_lon": float(lon),
                        "original_csv_lat": float(lat),
                        "target_lon": float(lon),
                        "target_lat": float(lat),
                        "target_x": float(target_xy[0]),
                        "target_y": float(target_xy[1]),
                        "target_source": target_source,
                        "score": float(score),
                        "score_components": json.dumps(components, ensure_ascii=False, sort_keys=True),
                        "plain_con_xml_patch_spec": f'<crossing node="{node_id}" edges="{edges_text}" priority="true"/>',
                        "can_generate_plain_con_xml_patch": True,
                        "duplicate_crossing_key": _crossing_key_str(node_id, edges_text),
                        "duplicate_generated_crossing": False,
                        "existing_crossing_duplicate": False,
                        "written_to_patch_xml": False,
                        "representative_crosswalk_id": "",
                        "rejected_reason": "",
                    }
                )

        scored_candidates = sorted(
            scored_candidates,
            key=lambda r: (-float(r.get("score", -1e18)), _as_str(r.get("node_id")), _as_str(r.get("crossing_edges"))),
        )

        if exclude_previous_failed_keys and prev_failed_keys:
            filtered_candidates = [
                c for c in scored_candidates
                if _crossing_key(_as_str(c.get("node_id")), _as_str(c.get("crossing_edges"))) not in prev_failed_keys
            ]
            if filtered_candidates:
                scored_candidates = filtered_candidates

        scored_candidates = scored_candidates[:attempts_per_crosswalk]

        if not scored_candidates:
            attempt_rows.append(
                {
                    "crosswalk_id": cw_id,
                    "attempt_id": f"{cw_id}_a0",
                    "attempt_rank": 1,
                    "node_id": "",
                    "crossing_edges": "",
                    "original_csv_lon": float(lon),
                    "original_csv_lat": float(lat),
                    "target_lon": float(lon),
                    "target_lat": float(lat),
                    "target_x": float(target_xy[0]),
                    "target_y": float(target_xy[1]),
                    "target_source": target_source,
                    "score": None,
                    "score_components": "",
                    "plain_con_xml_patch_spec": "",
                    "written_to_patch_xml": False,
                    "can_generate_plain_con_xml_patch": False,
                    "duplicate_crossing_key": "",
                    "representative_crosswalk_id": "",
                    "duplicate_generated_crossing": False,
                    "existing_crossing_duplicate": False,
                    "rejected_reason": "no_valid_edge_pairs_for_candidate_junctions",
                }
            )
            continue

        for idx, cand in enumerate(scored_candidates, start=1):
            cand["attempt_rank"] = int(idx)
            cand["attempt_id"] = f"{cw_id}_a{idx}"
            attempt_rows.append(cand)

    rep_by_key = _representative_by_key(attempt_rows)
    generated_keys: set[tuple[str, tuple[str, ...]]] = set()
    for row in sorted(attempt_rows, key=lambda r: (_as_str(r.get("crosswalk_id")), int(r.get("attempt_rank", 10**9)))):
        key = _crossing_key(_as_str(row.get("node_id")), _as_str(row.get("crossing_edges")))
        if key is None:
            row["written_to_patch_xml"] = False
            continue
        row["representative_crosswalk_id"] = _as_str(rep_by_key.get(key, _as_str(row.get("crosswalk_id"))))
        if key in generated_keys:
            row["duplicate_generated_crossing"] = True
            row["written_to_patch_xml"] = False
            row["can_generate_plain_con_xml_patch"] = False
            row["rejected_reason"] = "duplicate_generated_crossing"
            continue
        generated_keys.add(key)
        row["written_to_patch_xml"] = True

    attempts_df = pd.DataFrame(attempt_rows)
    attempts_path = output_dir / "crossing_patch_attempts.csv"
    write_csv_utf8_sig(attempts_df, attempts_path)

    patch_con_path = output_dir / "patched_crossings.con.xml"
    _write_connections_patch_generic(
        rows=attempt_rows,
        out_path=patch_con_path,
        node_field="node_id",
        edges_field="crossing_edges",
        write_flag_field="written_to_patch_xml",
    )

    provenance = {
        "base_network_path": str(net_file),
        "base_network_sha256": _sha256_file(net_file),
        "registry_path": str(registry_path),
        "registry_sha256": _sha256_file(registry_path),
        "previous_survival_report": "" if previous_survival_report is None else str(previous_survival_report),
        "generated_at": _utc_now_iso(),
        "crosswalk_count_selected": int(len(set([_as_str(r.get("crosswalk_id")) for r in attempt_rows if _as_str(r.get("crosswalk_id"))]))),
        "attempt_count_total": int(len(attempt_rows)),
        "attempts_per_crosswalk": int(attempts_per_crosswalk),
        "max_radius_m": float(args.max_radius_m),
        "max_expanded_radius_m": float(max_expanded_radius_m),
        "target_ids_csv": "" if target_ids_csv is None else str(target_ids_csv),
        "target_id_count": int(len(target_ids)),
        "exclude_previous_failed_keys": bool(exclude_previous_failed_keys),
        "residual_mode": bool(is_residual_mode),
    }
    provenance_path = output_dir / "patched_network_provenance.json"
    provenance_path.write_text(json.dumps(provenance, ensure_ascii=False, indent=2), encoding="utf-8")

    return {
        "crossing_patch_attempts": attempts_path,
        "patched_crossings_con": patch_con_path,
        "patched_network_provenance": provenance_path,
    }


def validate_patch(args: argparse.Namespace) -> dict[str, Path]:
    candidates_path = Path(args.candidates_path)
    patch_con_path = Path(args.patch_con_path)
    patched_net_file = Path(args.patched_net_file)
    netconvert_log_path = Path(args.netconvert_log_path)
    output_dir = Path(args.output_dir)

    if not candidates_path.exists():
        raise FileNotFoundError(f"candidate csv 파일이 없습니다: {candidates_path}")
    if not patch_con_path.exists():
        raise FileNotFoundError(f"patch con xml 파일이 없습니다: {patch_con_path}")
    if not patched_net_file.exists():
        raise FileNotFoundError(f"patched net 파일이 없습니다: {patched_net_file}")
    if not netconvert_log_path.exists():
        raise FileNotFoundError(f"netconvert log 파일이 없습니다: {netconvert_log_path}")

    output_dir.mkdir(parents=True, exist_ok=True)

    candidates_df = pd.read_csv(candidates_path)
    if "crosswalk_id" in candidates_df.columns:
        candidates_df["crosswalk_id"] = candidates_df["crosswalk_id"].astype(str)

    patch_keys = _load_patch_crossing_keys_from_con(patch_con_path)
    patched_root = ET.parse(patched_net_file).getroot()
    survived_keys = _survived_crossing_keys_from_patched_net(patched_root)

    has_attempt_columns = ("attempt_id" in candidates_df.columns) and ("node_id" in candidates_df.columns)

    candidate_rows = candidates_df.to_dict(orient="records")
    candidate_key_meta: dict[tuple[str, tuple[str, ...]], dict[str, Any]] = {}
    for row in candidate_rows:
        node_id = _attempt_node_field(row)
        edges_text = _attempt_edges_field(row)
        key = _crossing_key(node_id, edges_text)
        if key is None:
            continue
        candidate_key_meta[key] = row

    discard_reason_map = _parse_netconvert_discard_reasons(netconvert_log_path, candidate_key_meta)

    report_rows: list[dict[str, Any]] = []
    for row in candidate_rows:
        node_id = _attempt_node_field(row)
        crossing_edges_raw = _attempt_edges_field(row)
        crossing_edges = _canonical_edges_text(crossing_edges_raw)
        key = _crossing_key(node_id, crossing_edges_raw)

        patch_written = _bool_from_any(row.get("written_to_patch_xml", False))
        if key is not None:
            patch_written = patch_written and (key in patch_keys)

        patch_survived = bool(key is not None and key in survived_keys)

        rejected_reason = _as_str(row.get("rejected_reason"))
        discard_reason = ""
        patch_result = "netconvert_discarded_unclassified"

        if rejected_reason == "duplicate_generated_crossing":
            patch_result = "duplicate_generated_crossing"
            discard_reason = rejected_reason
            patch_written = False
            patch_survived = False
        elif rejected_reason == "duplicate_existing_crossing" or bool(row.get("existing_crossing_duplicate", False)):
            patch_result = "duplicate_existing_crossing"
            discard_reason = "duplicate_existing_crossing"
            patch_written = False
            patch_survived = True
        elif patch_survived:
            patch_result = "patched_unsignalized"
            discard_reason = ""
        else:
            reason = ""
            if key is not None:
                reason = _as_str(discard_reason_map.get(key))
            discard_reason = reason
            if reason:
                patch_result = _discard_reason_to_patch_result(reason)
            else:
                patch_result = "netconvert_discarded_unclassified"

        report_rows.append(
            {
                "crosswalk_id": _as_str(row.get("crosswalk_id")),
                "attempt_id": _as_str(row.get("attempt_id")),
                "attempt_rank": _as_float(row.get("attempt_rank")),
                "score": _as_float(row.get("score")),
                "node_id": node_id,
                "crossing_edges": crossing_edges,
                "patch_written": bool(patch_written),
                "patch_survived": bool(patch_survived),
                "patch_result": patch_result,
                "discard_reason": discard_reason,
                "original_csv_lon": _as_float(row.get("original_csv_lon")),
                "original_csv_lat": _as_float(row.get("original_csv_lat")),
                "target_x": _as_float(row.get("target_x")),
                "target_y": _as_float(row.get("target_y")),
            }
        )

    report_df = pd.DataFrame(report_rows)
    report_path = (
        output_dir / "crossing_patch_survival_attempt_report.csv"
        if has_attempt_columns
        else output_dir / "crossing_patch_survival_report.csv"
    )
    write_csv_utf8_sig(report_df, report_path)
    out_paths: dict[str, Path] = {"crossing_patch_survival_report": report_path}

    if has_attempt_columns and not report_df.empty:
        best_rows: list[dict[str, Any]] = []
        failure_priority = [
            "walkingarea_repair_required",
            "crossing_has_no_target",
            "pedestrian_topology_repair_required",
            "netconvert_discarded_unclassified",
            "duplicate_generated_crossing",
            "duplicate_existing_crossing",
        ]
        priority_rank = {k: i for i, k in enumerate(failure_priority)}
        for cw_id, grp in report_df.groupby("crosswalk_id", dropna=False):
            survived = grp[grp["patch_survived"] == True]  # noqa: E712
            chosen = None
            if not survived.empty:
                chosen = survived.sort_values(["attempt_rank", "score"], ascending=[True, False], kind="stable").iloc[0]
                chosen_row = dict(chosen)
                chosen_row["patch_result"] = "patched_unsignalized"
                best_rows.append(chosen_row)
                continue

            grp2 = grp.copy()
            grp2["prio"] = grp2["patch_result"].map(lambda v: priority_rank.get(_as_str(v), 10**6))
            grp2["attempt_rank_num"] = pd.to_numeric(grp2["attempt_rank"], errors="coerce").fillna(10**6)
            grp2["score_num"] = pd.to_numeric(grp2["score"], errors="coerce").fillna(-10**6)
            chosen = grp2.sort_values(
                ["prio", "attempt_rank_num", "score_num"],
                ascending=[True, True, False],
                kind="stable",
            ).iloc[0]
            best_rows.append(dict(chosen))

        best_df = pd.DataFrame(best_rows)
        drop_cols = [c for c in ("prio", "attempt_rank_num", "score_num") if c in best_df.columns]
        if drop_cols:
            best_df = best_df.drop(columns=drop_cols)
        best_path = output_dir / "crossing_patch_best_by_crosswalk.csv"
        write_csv_utf8_sig(best_df, best_path)
        out_paths["crossing_patch_best_by_crosswalk"] = best_path

    return out_paths


def build_final_registry(args: argparse.Namespace) -> dict[str, Path]:
    registry_path = Path(args.registry_path)
    c1_final_report_path = Path(args.c1_final_report)
    output_dir = Path(args.output_dir)
    net_xml_path: Path | None = Path(args.net_xml) if getattr(args, "net_xml", None) else None

    if not registry_path.exists():
        raise FileNotFoundError(f"registry 파일이 없습니다: {registry_path}")
    if not c1_final_report_path.exists():
        raise FileNotFoundError(f"c1_final_report 파일이 없습니다: {c1_final_report_path}")

    output_dir.mkdir(parents=True, exist_ok=True)

    reg_df = pd.read_csv(registry_path)
    reg_df["crosswalk_id"] = reg_df["crosswalk_id"].astype(str)

    c1_df = pd.read_csv(c1_final_report_path)
    c1_df["crosswalk_id"] = c1_df["crosswalk_id"].astype(str)

    for col in (
        "registry_status", "implementation_status", "control_mode",
        "runnable_for_signal_extension", "runnable_for_unsignalized_sim",
        "requires_patch", "c_subtype",
    ):
        if col not in reg_df.columns:
            reg_df[col] = ""

    c1_provenance_cols = [
        "c1_final_status", "c1_patch_result", "c1_patch_survived",
        "c1_node_id", "c1_crossing_edges",
        "representative_crosswalk_id", "duplicate_crossing_key",
        "original_csv_lon", "original_csv_lat", "target_x", "target_y",
    ]
    for col in c1_provenance_cols:
        if col not in reg_df.columns:
            reg_df[col] = ""

    c1_map = {row["crosswalk_id"]: row for row in c1_df.to_dict(orient="records")}

    # 통합 net.xml이 제공된 경우 TLS 커버리지를 평가하여 Tier를 분류한다.
    # Tier 1: crossing connection에 tl=X 있고 tlLogic[X] 존재 → signalized 승격
    # Tier 2: tl= 없음 → synthetic TLS 주입 후 승격
    _conn_tl_map: dict[str, str] = {}                        # crossing_edge → tls_id
    _conn_pli_map: dict[tuple[str, str], list[int]] = {}     # (crossing_edge, tls_id) → [linkIndex]
    _tl_logic_ids: set[str] = set()
    _synthetic_injection_result: dict[str, dict] = {}

    def _load_conn_maps(xml_root: Any) -> None:
        """connection 요소에서 _conn_tl_map / _conn_pli_map / _tl_logic_ids를 갱신한다."""
        _tl_logic_ids.clear()
        _tl_logic_ids.update(tl.get("id") for tl in xml_root.findall("tlLogic"))
        for _c in xml_root.findall("connection"):
            _tl = _c.get("tl")
            _to = _c.get("to", "")
            _li = _c.get("linkIndex")
            if _tl and _to:
                _conn_tl_map[_to] = _tl
                if _li is not None:
                    try:
                        _conn_pli_map.setdefault((_to, _tl), []).append(int(_li))
                    except ValueError:
                        pass
        # 중복 제거 및 정렬
        for _k in list(_conn_pli_map):
            _conn_pli_map[_k] = sorted(set(_conn_pli_map[_k]))

    if net_xml_path is not None and net_xml_path.exists():
        import xml.etree.ElementTree as _ET
        _net_root = _ET.parse(str(net_xml_path)).getroot()
        _load_conn_maps(_net_root)

        # Tier 2 crossing edges 찾기: 신호 연장 재분류 대상 행만 대상으로 한다.
        # (A/recovered 처럼 이미 signalized인 행은 제외)
        _reclassify_statuses = {"manual_review", "unsignalized", "distance_mapped"}
        try:
            from .synthetic_tls_injector import inject_synthetic_tls as _inject_tls
        except ImportError:
            from synthetic_tls_injector import inject_synthetic_tls as _inject_tls

        _reclassify_ces = {
            _as_str(row.get("crossing_edge"))
            for row in reg_df.to_dict(orient="records")
            if _as_str(row.get("crossing_edge"))
            and _as_str(row.get("registry_status")) in _reclassify_statuses
            and not _bool_from_any(row.get("runnable_for_signal_extension"))
        }
        _tier2_ces = [
            ce for ce in _reclassify_ces
            if ce and not _conn_tl_map.get(ce)
        ]
        if _tier2_ces:
            _signal_net_path = net_xml_path.parent / "network_with_signal.net.xml"
            _synthetic_injection_result = _inject_tls(
                net_xml_path=net_xml_path,
                tier2_crossing_edges=_tier2_ces,
                output_path=_signal_net_path,
            )
            # 주입 후 맵 전체 재로드 (synthetic connection 포함)
            _net_root2 = _ET.parse(str(_signal_net_path)).getroot()
            _conn_tl_map.clear()
            _conn_pli_map.clear()
            _load_conn_maps(_net_root2)

    def _tls_tier(crossing_edge: str) -> str:
        """crossing edge의 TLS 커버리지 계층을 반환한다."""
        if not _conn_tl_map and not _tl_logic_ids:
            return "unknown"
        tl = _conn_tl_map.get(crossing_edge)
        if not tl:
            return "tier2_no_tl_conn"
        if tl in _tl_logic_ids:
            return "tier1"
        return "tier2_no_tllogic"

    def _normalize_ped_link_indices(row: dict) -> dict:
        """runnable_for_signal_extension=True 행의 ped_link_indices를 net.xml 기준으로 정규화한다.

        - 기존 값이 있어도 crossing_edge+tls_id 직접 조회 결과로 덮어쓴다.
        - joinedS 병합 TLS 처리: conn_tl_map[crossing_edge]가 실제 tls_id 기준이므로
          레지스트리 tls_id와 다를 경우 conn_tl_map 값으로 업데이트한다.
        - synthetic 행도 post-injection 맵(_conn_tl_map/_conn_pli_map이 재로드됨)으로 정규화한다.
        - 직접 인덱스가 없으면 diagnostic 컬럼을 기록하고 기존 값을 제거한다.
        """
        if not _conn_tl_map and not _conn_pli_map:
            return row
        ce = _as_str(row.get("crossing_edge"))
        if not ce:
            return row

        # conn_tl_map[crossing_edge] 를 권위적 tls_id로 사용
        actual_tl = _conn_tl_map.get(ce, "")
        tls_id = _as_str(row.get("tls_id"))
        if actual_tl and actual_tl != tls_id:
            # joinedS 병합 또는 tls_id 미설정 → 실제 값으로 갱신
            row["tls_id"] = actual_tl
            tls_id = actual_tl
        elif not tls_id and actual_tl:
            row["tls_id"] = actual_tl
            tls_id = actual_tl

        if not tls_id:
            row["ped_link_indices_repair_status"] = "no_direct_connection_link_index"
            row["signal_extension_mapping_warning"] = (
                "missing_direct_ped_link_indices_for_crossing_edge_tls"
            )
            return row

        direct_indices = _conn_pli_map.get((ce, tls_id), [])
        if direct_indices:
            row["ped_link_indices"] = "|".join(str(i) for i in direct_indices)
            row["ped_link_indices_repair_status"] = "normalized_from_net_xml"
        else:
            row["ped_link_indices_repair_status"] = "no_direct_connection_link_index"
            row["signal_extension_mapping_warning"] = (
                "missing_direct_ped_link_indices_for_crossing_edge_tls"
            )
        return row

    signalized_statuses = {"A", "recovered"}
    unsignalized_statuses = {"unsignalized", "distance_mapped"}

    result_rows: list[dict[str, Any]] = []
    for row in reg_df.to_dict(orient="records"):
        cw_id = _as_str(row.get("crosswalk_id"))
        r_status = _as_str(row.get("registry_status"))
        c_sub = _as_str(row.get("c_subtype"))
        is_c1 = r_status == "manual_review" and c_sub == "C1_distance_match_failed"

        if is_c1 and cw_id in c1_map:
            c1_row = c1_map[cw_id]
            final_status = _as_str(c1_row.get("final_status"))

            row["c1_final_status"] = final_status
            row["c1_patch_result"] = _as_str(c1_row.get("patch_result"))
            row["c1_patch_survived"] = _as_str(c1_row.get("patch_survived"))
            row["c1_node_id"] = _as_str(c1_row.get("node_id"))
            row["c1_crossing_edges"] = _as_str(c1_row.get("crossing_edges"))
            row["representative_crosswalk_id"] = _as_str(c1_row.get("representative_crosswalk_id"))
            row["duplicate_crossing_key"] = _as_str(c1_row.get("duplicate_crossing_key"))
            row["original_csv_lon"] = c1_row.get("original_csv_lon")
            row["original_csv_lat"] = c1_row.get("original_csv_lat")
            row["target_x"] = c1_row.get("target_x")
            row["target_y"] = c1_row.get("target_y")

            if final_status in {"patched_unsignalized_v2", "patched_unsignalized_v3"}:
                row["registry_status"] = "patched_unsignalized"
                row["runnable_for_unsignalized_sim"] = True
                row["requires_patch"] = False
                ce = _as_str(row.get("crossing_edge"))
                tier = _tls_tier(ce) if ce else "unknown"
                if tier == "tier1" or ce in _synthetic_injection_result:
                    # 통합 net.xml에 유효한 TLS 있음 → 신호 연장형으로 승격
                    row["control_mode"] = "signalized"
                    row["implementation_status"] = "ready_for_simulation"
                    row["runnable_for_signal_extension"] = True
                    if ce in _synthetic_injection_result:
                        row["installation_assumption"] = "synthetic_pedestrian_signal"
                        synth = _synthetic_injection_result[ce]
                        if "tls_id" in synth:
                            row["tls_id"] = synth["tls_id"]
                        if "ped_link_indices" in synth:
                            row["ped_link_indices"] = "|".join(str(i) for i in synth["ped_link_indices"])
                    else:
                        row.setdefault("installation_assumption", "")
                        # Tier 1: tls_id/ped_link_indices는 net.xml conn_tl_map에서 보완
                        if not _as_str(row.get("tls_id")) and ce:
                            tl = _conn_tl_map.get(ce)
                            if tl:
                                row["tls_id"] = tl
                else:
                    row["control_mode"] = "proxy_unsignalized"
                    row["implementation_status"] = "ready_for_unsignalized_simulation"
                    row["runnable_for_signal_extension"] = False
            elif final_status == "represented_by_patched_crossing":
                row["registry_status"] = "represented_by_patched_crossing"
                row["runnable_for_unsignalized_sim"] = True
                row["requires_patch"] = False
                ce = _as_str(row.get("crossing_edge"))
                tier = _tls_tier(ce) if ce else "unknown"
                if tier == "tier1" or ce in _synthetic_injection_result:
                    row["control_mode"] = "signalized"
                    row["implementation_status"] = "ready_for_simulation"
                    row["runnable_for_signal_extension"] = True
                    if ce in _synthetic_injection_result:
                        row["installation_assumption"] = "synthetic_pedestrian_signal"
                        synth = _synthetic_injection_result[ce]
                        if "tls_id" in synth:
                            row["tls_id"] = synth["tls_id"]
                        if "ped_link_indices" in synth:
                            row["ped_link_indices"] = "|".join(str(i) for i in synth["ped_link_indices"])
                    else:
                        row.setdefault("installation_assumption", "")
                        if not _as_str(row.get("tls_id")) and ce:
                            tl = _conn_tl_map.get(ce)
                            if tl:
                                row["tls_id"] = tl
                else:
                    row["control_mode"] = "proxy_unsignalized"
                    row["implementation_status"] = "ready_via_representative_crossing"
                    row["runnable_for_signal_extension"] = False
            elif final_status == "netconvert_discarded_unclassified":
                row["registry_status"] = "pedestrian_topology_repair_required"
                row["implementation_status"] = "netconvert_discarded_unclassified"
                row["runnable_for_unsignalized_sim"] = False
                row["runnable_for_signal_extension"] = False
                row["requires_patch"] = True
        else:
            if r_status in signalized_statuses:
                row["runnable_for_signal_extension"] = True
                if not _bool_from_any(row.get("runnable_for_unsignalized_sim")):
                    row["runnable_for_unsignalized_sim"] = False
            elif r_status in unsignalized_statuses:
                row["runnable_for_unsignalized_sim"] = True
                # net.xml이 제공된 경우 TLS 커버리지 재판정 → 신호 연장형 승격
                ce = _as_str(row.get("crossing_edge"))
                tier = _tls_tier(ce) if ce else "unknown"
                if tier == "tier1" or ce in _synthetic_injection_result:
                    row["control_mode"] = "signalized"
                    row["implementation_status"] = "ready_for_simulation"
                    row["runnable_for_signal_extension"] = True
                    if ce in _synthetic_injection_result:
                        row["installation_assumption"] = "synthetic_pedestrian_signal"
                        synth = _synthetic_injection_result[ce]
                        if "tls_id" in synth:
                            row["tls_id"] = synth["tls_id"]
                        if "ped_link_indices" in synth:
                            row["ped_link_indices"] = "|".join(str(i) for i in synth["ped_link_indices"])
                    else:
                        row.setdefault("installation_assumption", "")
                        if not _as_str(row.get("tls_id")) and ce:
                            tl = _conn_tl_map.get(ce)
                            if tl:
                                row["tls_id"] = tl

        # runnable_for_signal_extension=True 인 모든 행의 tls_id / ped_link_indices를
        # net.xml 직접 조회로 정규화한다 (기존 값도 덮어씀).
        if _bool_from_any(row.get("runnable_for_signal_extension")):
            row = _normalize_ped_link_indices(row)

        result_rows.append(row)

    final_df = pd.DataFrame(result_rows)

    existing_extra = [c for c in c1_provenance_cols if c in final_df.columns]
    base_cols = [c for c in final_df.columns if c not in existing_extra]
    final_df = final_df[base_cols + existing_extra]

    final_registry_path = output_dir / "junggu_crosswalk_sumo_registry_final.csv"
    write_csv_utf8_sig(final_df, final_registry_path)

    summary_dims = [
        "registry_status", "implementation_status", "control_mode",
        "runnable_for_signal_extension", "runnable_for_unsignalized_sim",
        "requires_patch",
    ]
    summary_rows_out: list[dict[str, Any]] = []
    for col in summary_dims:
        if col not in final_df.columns:
            continue
        for val, grp in final_df.groupby(col, dropna=False):
            summary_rows_out.append(
                {"dimension": col, "value": _as_str(val), "count": int(len(grp))}
            )
    summary_df = pd.DataFrame(summary_rows_out)
    summary_path = output_dir / "final_registry_summary.csv"
    write_csv_utf8_sig(summary_df, summary_path)

    runnable_col_order = [
        "crosswalk_id", "registry_status", "implementation_status", "control_mode",
        "runnable_for_signal_extension", "runnable_for_unsignalized_sim",
        "representative_crosswalk_id", "c1_node_id", "c1_crossing_edges",
    ]
    runnable_col_order = [c for c in runnable_col_order if c in final_df.columns]
    runnable_mask = (
        final_df["runnable_for_signal_extension"].apply(_bool_from_any)
        | final_df["runnable_for_unsignalized_sim"].apply(_bool_from_any)
    )
    runnable_df = final_df.loc[runnable_mask, runnable_col_order].copy()
    runnable_path = output_dir / "runnable_crosswalk_ids_final.csv"
    write_csv_utf8_sig(runnable_df, runnable_path)

    return {
        "junggu_crosswalk_sumo_registry_final": final_registry_path,
        "final_registry_summary": summary_path,
        "runnable_crosswalk_ids_final": runnable_path,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="crossing patch v2 small-batch generator")
    sub = parser.add_subparsers(dest="command", required=True)

    p_gen = sub.add_parser("generate", help="generate crossing patch candidates and .con.xml")
    p_gen.add_argument("--registry_path", required=True)
    p_gen.add_argument("--net_file", required=True)
    p_gen.add_argument("--max_radius_m", type=float, default=100.0)
    p_gen.add_argument("--limit", type=int, default=20)
    p_gen.add_argument("--t2_path", default=str(DEFAULT_T2_PATH))
    p_gen.add_argument("--output_dir", required=True)

    p_gen_multi = sub.add_parser("generate-multi", help="generate multi-attempt crossing patch .con.xml")
    p_gen_multi.add_argument("--registry_path", required=True)
    p_gen_multi.add_argument("--t2_path", default=str(DEFAULT_T2_PATH))
    p_gen_multi.add_argument("--net_file", required=True)
    p_gen_multi.add_argument("--previous_survival_report", default="")
    p_gen_multi.add_argument("--max_radius_m", type=float, default=100.0)
    p_gen_multi.add_argument("--attempts_per_crosswalk", type=int, default=8)
    p_gen_multi.add_argument("--target_ids_csv", default="")
    p_gen_multi.add_argument("--target_id_col", default="crosswalk_id")
    p_gen_multi.add_argument("--exclude_previous_failed_keys", default="true")
    p_gen_multi.add_argument("--max_expanded_radius_m", type=float, default=200.0)
    p_gen_multi.add_argument("--output_dir", required=True)

    p_val = sub.add_parser("validate-patch", help="validate patch survival against patched net and netconvert log")
    p_val.add_argument("--candidates_path", required=True)
    p_val.add_argument("--patch_con_path", required=True)
    p_val.add_argument("--patched_net_file", required=True)
    p_val.add_argument("--netconvert_log_path", required=True)
    p_val.add_argument("--output_dir", required=True)

    p_final = sub.add_parser("final-c1-report", help="build final merged C1 coverage report from v2/v3 outputs")
    p_final.add_argument("--v2_survival_report", required=True)
    p_final.add_argument("--v3_best_report", required=True)
    p_final.add_argument("--v3_attempt_report", required=True)
    p_final.add_argument("--registry_path", required=True)
    p_final.add_argument("--output_dir", required=True)

    p_freg = sub.add_parser("final-registry", help="build final full registry CSV with all 670 crosswalks")
    p_freg.add_argument("--registry_path", required=True)
    p_freg.add_argument("--c1_final_report", required=True)
    p_freg.add_argument("--output_dir", required=True)
    p_freg.add_argument(
        "--net_xml",
        default=None,
        help="통합 net.xml 경로. 제공 시 patched/unsignalized 후보의 TLS 커버리지를 재판정하여 "
             "runnable_for_signal_extension을 갱신한다.",
    )

    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.command == "generate":
        paths = generate(args)
        for key, path in paths.items():
            print(f"{key}: {path}")
    elif args.command == "generate-multi":
        paths = generate_multi(args)
        for key, path in paths.items():
            print(f"{key}: {path}")
    elif args.command == "validate-patch":
        paths = validate_patch(args)
        for key, path in paths.items():
            print(f"{key}: {path}")
    elif args.command == "final-c1-report":
        paths = build_final_c1_report(args)
        for key, path in paths.items():
            print(f"{key}: {path}")
    elif args.command == "final-registry":
        paths = build_final_registry(args)
        for key, path in paths.items():
            print(f"{key}: {path}")


if __name__ == "__main__":
    main()
