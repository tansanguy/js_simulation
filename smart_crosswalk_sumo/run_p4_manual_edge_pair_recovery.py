#!/usr/bin/env python3
from __future__ import annotations

import argparse
import html
import json
import math
from datetime import datetime
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

import pandas as pd

from smart_crosswalk_sumo.network_utils import (
    apply_sumo_environment,
    edge_function,
    edge_center,
    pedestrian_link_indices,
    pedestrian_route_from_crossing,
    read_net,
)


BASE_DIR = Path(__file__).resolve().parents[1]
RESULT_DIR = BASE_DIR / "result"
DEFAULT_P4_CANDIDATES = (
    RESULT_DIR
    / "phase_next_remaining22_p1_signal_fix_20260516_010953"
    / "remaining_p4_manual_review_candidates.csv"
)
DEFAULT_MAPPING_SUMMARY = RESULT_DIR / "phase_next_top50_mapping_visual_audit_20260515_174626" / "03_top50_mapping_distance_summary.csv"
DEFAULT_GENERATED_CHECK = RESULT_DIR / "phase_next_bad22_generated_crossing_patch_20260515_181150" / "generated_crossing_location_check.csv"
DEFAULT_GENERATED_PATCH = RESULT_DIR / "phase_next_bad22_generated_crossing_patch_20260515_181150" / "generated_crossing_patch_candidates.csv"
DEFAULT_BASE_NET = RESULT_DIR / "phase_next_recovery_command_plan_20260514_200908" / "batch_03_run" / "recovery_tls_batch_network_v1.net.xml"

NEARBY_NODE_LIMIT = 8
NEARBY_EDGE_LIMIT = 8


def _now_tag() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def _norm(value: Any) -> str:
    if value is None:
        return ""
    try:
        if pd.isna(value):
            return ""
    except Exception:
        pass
    text = str(value).strip()
    if not text or text.lower() in {"nan", "none", "nat"}:
        return ""
    if text.endswith(".0"):
        raw = text[:-2]
        if raw.replace("-", "", 1).isdigit():
            return raw
    return text


def _safe_float(value: Any, default: float = math.nan) -> float:
    try:
        if value is None or pd.isna(value):
            return default
        return float(value)
    except Exception:
        return default


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        if value is None or pd.isna(value):
            return default
        return int(float(value))
    except Exception:
        return default


def _csv(path: Path) -> pd.DataFrame:
    if not path.exists():
        raise FileNotFoundError(path)
    return pd.read_csv(path)


def _xy_from_lonlat(net: Any, lon: float, lat: float) -> tuple[float, float] | None:
    try:
        x, y = net.convertLonLat2XY(float(lon), float(lat))
        return float(x), float(y)
    except Exception:
        return None


def _lonlat_from_xy(net: Any, x: float, y: float) -> tuple[float, float] | None:
    try:
        lon, lat = net.convertXY2LonLat(float(x), float(y))
        return float(lon), float(lat)
    except Exception:
        return None


def _haversine_m(lon1: float, lat1: float, lon2: float, lat2: float) -> float:
    if any(pd.isna(v) for v in [lon1, lat1, lon2, lat2]):
        return math.nan
    r = 6371000.0
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (
        math.sin(dlat / 2) ** 2
        + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2) ** 2
    )
    return 2 * r * math.asin(math.sqrt(a))


def _node_lonlat(net: Any, node_id: str) -> tuple[float, float] | None:
    try:
        node = net.getNode(node_id)
    except Exception:
        return None
    try:
        return _lonlat_from_xy(net, *node.getCoord())
    except Exception:
        return None


def _node_xy(net: Any, node_id: str) -> tuple[float, float] | None:
    try:
        node = net.getNode(node_id)
    except Exception:
        return None
    try:
        x, y = node.getCoord()
        return float(x), float(y)
    except Exception:
        return None


def _edge_lonlat_shape(net: Any, edge_id: str) -> list[tuple[float, float]]:
    try:
        edge = net.getEdge(edge_id)
    except Exception:
        return []
    out: list[tuple[float, float]] = []
    for x, y in edge.getShape():
        lonlat = _lonlat_from_xy(net, float(x), float(y))
        if lonlat is not None:
            out.append(lonlat)
    return out


def _edge_distance_m(net: Any, edge: Any, lon: float, lat: float) -> float:
    xy = _xy_from_lonlat(net, lon, lat)
    if xy is None:
        return math.nan
    shape = [(float(x), float(y)) for x, y in edge.getShape()]
    if not shape:
        try:
            return math.dist(xy, edge.getFromNode().getCoord())
        except Exception:
            return math.nan
    if len(shape) == 1:
        return math.dist(xy, shape[0])
    best = float("inf")
    px, py = xy
    for i in range(len(shape) - 1):
        ax, ay = shape[i]
        bx, by = shape[i + 1]
        dx = bx - ax
        dy = by - ay
        if dx == 0 and dy == 0:
            dist = math.dist(xy, (ax, ay))
        else:
            t = ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)
            t = max(0.0, min(1.0, t))
            dist = math.dist(xy, (ax + t * dx, ay + t * dy))
        best = min(best, dist)
    return best


def _node_distance_m(net: Any, node_id: str, lon: float, lat: float) -> float:
    xy = _xy_from_lonlat(net, lon, lat)
    node_xy = _node_xy(net, node_id)
    if xy is None or node_xy is None:
        return math.nan
    return math.dist(xy, node_xy)


def _edge_angle_deg(edge: Any) -> float | str:
    try:
        fx, fy = edge.getFromNode().getCoord()
        tx, ty = edge.getToNode().getCoord()
    except Exception:
        return ""
    dx = float(tx) - float(fx)
    dy = float(ty) - float(fy)
    if dx == 0 and dy == 0:
        return ""
    return round((math.degrees(math.atan2(dy, dx)) + 360.0) % 360.0, 2)


def _traffic_light_ids(net: Any) -> set[str]:
    try:
        return {str(t.getID()) for t in net.getTrafficLights()}
    except Exception:
        return set()


def _nearest_tls(net: Any, lon: float, lat: float) -> tuple[str, float, tuple[float, float] | None]:
    xy = _xy_from_lonlat(net, lon, lat)
    if xy is None:
        return "", math.nan, None
    tls_ids = _traffic_light_ids(net)
    best_id = ""
    best_dist = float("inf")
    best_xy: tuple[float, float] | None = None
    for tls_id in tls_ids:
        try:
            node = net.getNode(tls_id)
        except Exception:
            continue
        try:
            node_xy = (float(node.getCoord()[0]), float(node.getCoord()[1]))
        except Exception:
            continue
        dist = math.dist(xy, node_xy)
        if dist < best_dist:
            best_id = tls_id
            best_dist = dist
            best_xy = node_xy
    if not best_id or best_xy is None:
        return "", math.nan, None
    return best_id, round(best_dist, 3), _lonlat_from_xy(net, *best_xy)


def _edge_pair_from_text(value: Any) -> list[str]:
    text = _norm(value)
    if not text or text in {"[]", "nan", "None"}:
        return []
    if text.startswith("[") and text.endswith("]"):
        text = text[1:-1]
    text = text.replace(",", " ").replace("|", " ")
    return [tok.strip().strip("'\"") for tok in text.split() if tok.strip()]


def _default_manual_strategy(failure_category: str, manual_review_type: str) -> str:
    failure_category = _norm(failure_category).upper()
    manual_review_type = _norm(manual_review_type).upper()
    if failure_category == "MANUAL_REVIEW_NEEDED" or manual_review_type == "MANUAL_EDGE_PAIR_SELECTION":
        return "EXISTING_CROSSING_REMAP"
    if failure_category == "GENERATED_CROSSING_RETRY_POSSIBLE":
        return "MANUAL_CROSSING_ADD"
    if failure_category == "LOCATION_TOO_FAR":
        return "HOLD"
    return "REJECT"


def _default_recovery_strategy(failure_category: str, manual_review_type: str) -> str:
    failure_category = _norm(failure_category).upper()
    manual_review_type = _norm(manual_review_type).upper()
    if failure_category in {"MANUAL_REVIEW_NEEDED", "GENERATED_CROSSING_RETRY_POSSIBLE"} or manual_review_type == "MANUAL_EDGE_PAIR_SELECTION":
        return "P4_MANUAL_EDGE_PAIR_REVIEW"
    if failure_category == "LOCATION_TOO_FAR":
        return "P4_HOLD"
    return "P4_REJECT"


def _edge_candidate_records(net: Any, row: pd.Series, edge_limit: int = NEARBY_EDGE_LIMIT, node_limit: int = NEARBY_NODE_LIMIT) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    real_lon = _safe_float(row.get("real_lon"))
    real_lat = _safe_float(row.get("real_lat"))
    current_lon = _safe_float(row.get("current_sumo_lon"))
    current_lat = _safe_float(row.get("current_sumo_lat"))
    generated_lon = _safe_float(row.get("generated_lon"))
    generated_lat = _safe_float(row.get("generated_lat"))

    candidate_nodes: list[tuple[float, str, tuple[float, float], Any]] = []
    for node in net.getNodes():
        nid = _norm(node.getID())
        if not nid:
            continue
        try:
            node_xy = node.getCoord()
        except Exception:
            continue
        try:
            target_xy = _xy_from_lonlat(net, real_lon, real_lat)
            if target_xy is None:
                continue
            dist = math.dist(target_xy, (float(node_xy[0]), float(node_xy[1])))
        except Exception:
            continue
        candidate_nodes.append((dist, nid, (float(node_xy[0]), float(node_xy[1])), node))
    candidate_nodes.sort(key=lambda x: (x[0], x[1]))

    tls_id, tls_dist, tls_lonlat = _nearest_tls(net, real_lon, real_lat)

    edge_pool: list[tuple[float, str, Any]] = []
    for edge in net.getEdges():
        eid = _norm(edge.getID())
        if not eid or eid.startswith(":"):
            continue
        if edge_function(edge) != "normal":
            continue
        dist = _edge_distance_m(net, edge, real_lon, real_lat)
        if math.isnan(dist):
            continue
        edge_pool.append((dist, eid, edge))
    edge_pool.sort(key=lambda x: (x[0], x[1]))
    edge_pool = edge_pool[:edge_limit]

    records: list[dict[str, Any]] = []
    for rank, (edge_dist, edge_id, edge) in enumerate(edge_pool, start=1):
        from_id = _norm(edge.getFromNode().getID())
        to_id = _norm(edge.getToNode().getID())
        from_dist = _node_distance_m(net, from_id, real_lon, real_lat)
        to_dist = _node_distance_m(net, to_id, real_lon, real_lat)
        if math.isnan(from_dist) and math.isnan(to_dist):
            nearby_node_id = from_id or to_id
            nearby_node_dist = ""
        elif math.isnan(to_dist) or (not math.isnan(from_dist) and from_dist <= to_dist):
            nearby_node_id = from_id
            nearby_node_dist = round(from_dist, 3)
        else:
            nearby_node_id = to_id
            nearby_node_dist = round(to_dist, 3)

        records.append(
            {
                "crosswalk_id": _norm(row.get("crosswalk_id")),
                "original_rank": _safe_int(row.get("original_rank")),
                "real_lon": real_lon,
                "real_lat": real_lat,
                "current_sumo_lon": current_lon,
                "current_sumo_lat": current_lat,
                "generated_lon": generated_lon,
                "generated_lat": generated_lat,
                "mapping_distance_m": round(_safe_float(row.get("mapping_distance_m")), 3),
                "failure_category": _norm(row.get("failure_category")),
                "nearest_tls_id": tls_id,
                "nearest_tls_distance_m": tls_dist,
                "nearby_node_id": nearby_node_id,
                "nearby_node_distance_m": nearby_node_dist,
                "candidate_edge_id": edge_id,
                "candidate_edge_distance_m": round(edge_dist, 3),
                "candidate_edge_function": _norm(edge_function(edge)),
                "candidate_edge_from_node": from_id,
                "candidate_edge_to_node": to_id,
                "candidate_edge_angle": _edge_angle_deg(edge),
                "note": f"rank {rank}; nearest node {nearby_node_id or ''}; nearest TLS {tls_id or ''}",
                "candidate_rank": rank,
                "nearby_node_rank": next((i + 1 for i, (_d, nid, _xy, _n) in enumerate(candidate_nodes[:node_limit]) if nid == nearby_node_id), ""),
                "nearby_nodes_json": json.dumps(
                    [
                        {
                            "node_id": nid,
                            "distance_m": round(dist, 3),
                            "type": _norm(node.getType()) if node is not None else "",
                            "lon": _node_lonlat(net, nid)[0] if _node_lonlat(net, nid) else "",
                            "lat": _node_lonlat(net, nid)[1] if _node_lonlat(net, nid) else "",
                        }
                        for dist, nid, _xy, node in candidate_nodes[:node_limit]
                    ],
                    ensure_ascii=False,
                ),
                "candidate_edges_json": "",
                "real_point": json.dumps({"lon": real_lon, "lat": real_lat}, ensure_ascii=False),
                "current_sumo_point": json.dumps({"lon": current_lon, "lat": current_lat}, ensure_ascii=False),
                "generated_point": json.dumps({"lon": generated_lon, "lat": generated_lat}, ensure_ascii=False),
                "nearest_tls_lon": tls_lonlat[0] if tls_lonlat else "",
                "nearest_tls_lat": tls_lonlat[1] if tls_lonlat else "",
            }
        )

    candidate_edges_json = json.dumps(
        [
            {
                "edge_id": rec["candidate_edge_id"],
                "distance_m": rec["candidate_edge_distance_m"],
                "from_node": rec["candidate_edge_from_node"],
                "to_node": rec["candidate_edge_to_node"],
                "shape": _edge_lonlat_shape(net, rec["candidate_edge_id"]),
            }
            for rec in records
        ],
        ensure_ascii=False,
    )
    for rec in records:
        rec["candidate_edges_json"] = candidate_edges_json

    context = {
        "nearest_tls_id": tls_id,
        "nearest_tls_distance_m": tls_dist,
        "nearest_tls_lon": tls_lonlat[0] if tls_lonlat else "",
        "nearest_tls_lat": tls_lonlat[1] if tls_lonlat else "",
        "nearby_nodes_json": records[0]["nearby_nodes_json"] if records else "[]",
        "candidate_edges_json": candidate_edges_json,
        "candidate_edge_records": records,
    }
    return records, context


def _build_template_rows(p4_df: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for _, row in p4_df.sort_values(["original_rank", "crosswalk_id"]).iterrows():
        failure_category = _norm(row.get("failure_category"))
        manual_review_type = _norm(row.get("manual_review_type"))
        manual_strategy = _default_manual_strategy(failure_category, manual_review_type)
        rows.append(
            {
                "crosswalk_id": _norm(row.get("crosswalk_id")),
                "original_rank": _safe_int(row.get("original_rank")),
                "real_lon": _safe_float(row.get("real_lon")),
                "real_lat": _safe_float(row.get("real_lat")),
                "mapping_distance_m": round(_safe_float(row.get("mapping_distance_m")), 3),
                "failure_category": failure_category,
                "recovery_strategy": _default_recovery_strategy(failure_category, manual_review_type),
                "manual_strategy": manual_strategy,
                "selected_node_id": "",
                "selected_tls_id": "",
                "selected_crossing_edges": "",
                "selected_from_edge": "",
                "selected_to_edge": "",
                "needs_new_signal": "TRUE" if manual_strategy == "NEW_SIGNAL_INSTALLATION" else "FALSE",
                "confidence": "",
                "manual_reason": "",
                "review_status": "NEEDS_MANUAL_SELECTION",
            }
        )
    return pd.DataFrame(rows)


def _pick_node_type(net: Any, node_id: str, manual_strategy: str) -> str:
    if manual_strategy == "NEW_SIGNAL_INSTALLATION":
        return "traffic_light"
    try:
        return _norm(net.getNode(node_id).getType()) or "priority"
    except Exception:
        return "priority"


def _parse_crossing_edges(row: pd.Series) -> list[str]:
    edges = _edge_pair_from_text(row.get("selected_crossing_edges"))
    if len(edges) >= 2:
        return edges[:2]
    from_edge = _norm(row.get("selected_from_edge"))
    to_edge = _norm(row.get("selected_to_edge"))
    if from_edge and to_edge:
        return [from_edge, to_edge]
    return []


def _find_crossing_edge_id(net: Any, node_id: str, pair: list[str]) -> str:
    wanted = {e for e in pair if e}
    if not wanted:
        return ""
    for edge in net.getEdges():
        if edge_function(edge) != "crossing":
            continue
        try:
            if _norm(edge.getFromNode().getID()) != node_id:
                continue
        except Exception:
            continue
        try:
            route = pedestrian_route_from_crossing(edge)
            actual = {route.get("from_edge", ""), route.get("to_edge", "")}
            if actual == wanted:
                return _norm(edge.getID())
        except Exception:
            continue
    for edge in net.getEdges():
        if edge_function(edge) != "crossing":
            continue
        try:
            if _norm(edge.getFromNode().getID()) == node_id:
                return _norm(edge.getID())
        except Exception:
            continue
    return ""


def _build_crossing_patch(selected: pd.DataFrame, net: Any, out_dir: Path) -> tuple[Path, pd.DataFrame]:
    root = ET.Element("nodes")
    rows: list[dict[str, Any]] = []
    seen_nodes: set[str] = set()
    seen_pairs: set[tuple[str, tuple[str, str]]] = set()

    for _, row in selected.iterrows():
        cid = _norm(row.get("crosswalk_id"))
        manual_strategy = _norm(row.get("manual_strategy")).upper()
        node_id = _norm(row.get("selected_node_id"))
        if not cid or not node_id:
            rows.append(
                {
                    "crosswalk_id": cid,
                    "patch_kind": "crossing",
                    "patch_status": "REJECT",
                    "patch_reason": "missing_selected_node_id",
                }
            )
            continue
        edges = _parse_crossing_edges(row)
        if len(edges) < 2:
            rows.append(
                {
                    "crosswalk_id": cid,
                    "patch_kind": "crossing",
                    "patch_status": "SKIP",
                    "patch_reason": "missing_selected_crossing_edges",
                }
            )
            continue
        pair_key = (node_id, tuple(sorted(edges)))
        if pair_key in seen_pairs:
            rows.append(
                {
                    "crosswalk_id": cid,
                    "patch_kind": "crossing",
                    "patch_status": "SKIP",
                    "patch_reason": "duplicate_crossing_pair_in_selection",
                }
            )
            continue
        seen_pairs.add(pair_key)
        try:
            node = net.getNode(node_id)
        except Exception:
            rows.append(
                {
                    "crosswalk_id": cid,
                    "patch_kind": "crossing",
                    "patch_status": "REJECT",
                    "patch_reason": "selected_node_missing_in_base_net",
                }
            )
            continue
        if node_id not in seen_nodes:
            seen_nodes.add(node_id)
            try:
                nx, ny = node.getCoord()
            except Exception:
                nx, ny = (0.0, 0.0)
            ET.SubElement(
                root,
                "node",
                {
                    "id": node_id,
                    "x": f"{float(nx):.2f}",
                    "y": f"{float(ny):.2f}",
                    "type": _pick_node_type(net, node_id, manual_strategy),
                },
            )
        ET.SubElement(
            root,
            "crossing",
            {
                "node": node_id,
                "edges": " ".join(edges),
                "id": f"P4_{cid}",
                "width": "4.0",
                "priority": "true",
            },
        )
        rows.append(
            {
                "crosswalk_id": cid,
                "patch_kind": "crossing",
                "patch_status": "PATCHED",
                "patch_reason": "manual crossing patch prepared",
                "selected_node_id": node_id,
                "selected_crossing_edges": " ".join(edges),
                "selected_from_edge": _norm(row.get("selected_from_edge")) or edges[0],
                "selected_to_edge": _norm(row.get("selected_to_edge")) or edges[1],
                "manual_strategy": manual_strategy,
            }
        )

    ET.indent(root, space="  ")
    patch_path = out_dir / "p4_manual_crossing_patch.nod.xml"
    patch_path.write_text(ET.tostring(root, encoding="unicode", xml_declaration=True), encoding="utf-8")
    return patch_path, pd.DataFrame(rows)


def _base_tl_logic(root: ET.Element, tls_id: str) -> ET.Element | None:
    for tl in root.findall("tlLogic"):
        if _norm(tl.attrib.get("id")) == tls_id:
            return tl
    return None


def _phase_summary(root: ET.Element, tls_id: str, ped_indices: list[int]) -> dict[str, Any]:
    tl = _base_tl_logic(root, tls_id)
    if tl is None:
        return {
            "tlLogic_present": False,
            "ped_has_green": False,
            "ped_has_red": False,
            "pedestrian_only_phase_available": False,
            "pedestrian_only_phase_indices": "",
            "phase_states": "[]",
        }
    states = [_norm(p.attrib.get("state")) for p in tl.findall("phase")]
    ped_has_green = False
    ped_has_red = False
    ped_only: list[int] = []
    for idx, state in enumerate(states):
        if not state:
            continue
        for ped_idx in ped_indices:
            if ped_idx < 0 or ped_idx >= len(state):
                continue
            ch = state[ped_idx]
            if ch in {"g", "G"}:
                ped_has_green = True
            if ch in {"r", "R"}:
                ped_has_red = True
            if ch in {"g", "G"} and all(i == ped_idx or state[i] not in {"g", "G"} for i in range(len(state))):
                ped_only.append(idx)
    return {
        "tlLogic_present": True,
        "ped_has_green": bool(ped_has_green),
        "ped_has_red": bool(ped_has_red),
        "pedestrian_only_phase_available": bool(ped_only),
        "pedestrian_only_phase_indices": "|".join(str(v) for v in sorted(set(ped_only))),
        "phase_states": json.dumps(states, ensure_ascii=False),
    }


def _build_tllogic_patch(source_net: Path, selected: pd.DataFrame, out_path: Path, pre_root: ET.Element) -> pd.DataFrame:
    base_root = ET.parse(source_net).getroot()
    base_net = read_net(source_net)
    patch_root = ET.Element("tlLogics")
    rows: list[dict[str, Any]] = []

    for _, row in selected.iterrows():
        manual_strategy = _norm(row.get("manual_strategy")).upper()
        if manual_strategy not in {"EXISTING_TLS_RETROFIT", "NEW_SIGNAL_INSTALLATION"}:
            continue
        cid = _norm(row.get("crosswalk_id"))
        node_id = _norm(row.get("selected_node_id"))
        tls_id = _norm(row.get("selected_tls_id")) or (node_id if manual_strategy == "NEW_SIGNAL_INSTALLATION" else "")
        edges = _parse_crossing_edges(row)
        crossing_edge_id = _find_crossing_edge_id(base_net, node_id, edges)
        if not tls_id or not crossing_edge_id:
            rows.append(
                {
                    "crosswalk_id": cid,
                    "patch_kind": "tls",
                    "tls_id": tls_id,
                    "crossing_edge_id": crossing_edge_id,
                    "patch_status": "SKIP",
                    "patch_reason": "missing_tls_id_or_crossing_edge",
                }
            )
            continue
        try:
            ped_indices = pedestrian_link_indices(source_net, tls_id, crossing_edge_id, xml_root=pre_root)
        except Exception:
            ped_indices = []
        if not ped_indices:
            rows.append(
                {
                    "crosswalk_id": cid,
                    "patch_kind": "tls",
                    "tls_id": tls_id,
                    "crossing_edge_id": crossing_edge_id,
                    "patch_status": "SKIP",
                    "patch_reason": "missing_ped_link_indices",
                }
            )
            continue

        base_tl = _base_tl_logic(base_root, tls_id)
        if base_tl is None:
            rows.append(
                {
                    "crosswalk_id": cid,
                    "patch_kind": "tls",
                    "tls_id": tls_id,
                    "crossing_edge_id": crossing_edge_id,
                    "patch_status": "SKIP",
                    "patch_reason": "base_tlLogic_missing",
                }
            )
            continue
        tl_copy = ET.fromstring(ET.tostring(base_tl, encoding="utf-8"))

        phases = tl_copy.findall("phase")
        if not phases:
            rows.append(
                {
                    "crosswalk_id": cid,
                    "patch_kind": "tls",
                    "tls_id": tls_id,
                    "crossing_edge_id": crossing_edge_id,
                    "patch_status": "SKIP",
                    "patch_reason": "base_phase_missing",
                }
            )
            continue

        max_len = max(len(_norm(p.attrib.get("state"))) for p in phases if _norm(p.attrib.get("state")))
        if max_len <= 0:
            rows.append(
                {
                    "crosswalk_id": cid,
                    "patch_kind": "tls",
                    "tls_id": tls_id,
                    "crossing_edge_id": crossing_edge_id,
                    "patch_status": "SKIP",
                    "patch_reason": "invalid_phase_state_length",
                }
            )
            continue
        target = None
        target_idx = -1
        for idx, phase in enumerate(phases):
            state = _norm(phase.attrib.get("state"))
            if state and set(state) == {"r"}:
                target = phase
                target_idx = idx
                break
        if target is None:
            target = ET.Element("phase", {"duration": "6", "state": "r" * max_len})
            tl_copy.append(target)
            target_idx = len(phases)
            patch_action = "append_pedestrian_only_phase"
        else:
            patch_action = "reuse_all_red_phase_as_pedestrian_only_phase"
        chars = list(_norm(target.attrib.get("state")))
        if len(chars) < max_len:
            chars.extend(["r"] * (max_len - len(chars)))
        for idx in ped_indices:
            if 0 <= idx < len(chars):
                chars[idx] = "G"
        target.set("state", "".join(chars))
        if not _norm(target.attrib.get("duration")):
            target.set("duration", "6")
        ET.indent(tl_copy, space="  ")
        patch_root.append(tl_copy)
        rows.append(
            {
                "crosswalk_id": cid,
                "patch_kind": "tls",
                "tls_id": tls_id,
                "crossing_edge_id": crossing_edge_id,
                "ped_link_indices": "|".join(str(v) for v in ped_indices),
                "patch_status": "PATCHED",
                "patch_action": patch_action,
                "patched_phase_index": target_idx,
                "patch_reason": "manual tls retrofit prepared" if manual_strategy == "EXISTING_TLS_RETROFIT" else "manual new signal prepared",
                "manual_strategy": manual_strategy,
            }
        )

    ET.indent(patch_root, space="  ")
    out_path.write_text(ET.tostring(patch_root, encoding="unicode", xml_declaration=True), encoding="utf-8")
    return pd.DataFrame(rows)


def _netconvert_cmd(source_net: Path, node_patch: Path, out_net: Path) -> list[str]:
    return [
        "netconvert",
        "--sumo-net-file",
        str(source_net),
        "--node-files",
        str(node_patch),
        "--output-file",
        str(out_net),
        "--no-turnarounds",
        "true",
        "--walkingareas",
        "true",
        "--sidewalks.guess",
        "true",
        "--sidewalks.guess.min-speed",
        "0",
        "--sidewalks.guess.max-speed",
        "40",
        "--tls.crossing-min.time",
        "6",
        "--tls.crossing-clearance.time",
        "3",
    ]


def _run_cmd(cmd: list[str], log_path: Path) -> tuple[bool, str]:
    import subprocess

    try:
        ret = subprocess.run(cmd, capture_output=True, text=True, timeout=1200)
        log_path.write_text((ret.stdout or "") + "\n" + (ret.stderr or ""), encoding="utf-8")
        msg = (ret.stderr or ret.stdout or "").strip()
        return ret.returncode == 0, msg[:1200]
    except Exception as exc:
        log_path.write_text(str(exc), encoding="utf-8")
        return False, str(exc)


def _netconvert_tll_cmd(source_net: Path, tll_path: Path, out_net: Path) -> list[str]:
    return [
        "netconvert",
        "--sumo-net-file",
        str(source_net),
        "--tllogic-files",
        str(tll_path),
        "--output-file",
        str(out_net),
        "--no-turnarounds",
        "true",
        "--walkingareas",
        "true",
        "--sidewalks.guess",
        "true",
        "--sidewalks.guess.min-speed",
        "0",
        "--sidewalks.guess.max-speed",
        "40",
        "--tls.crossing-min.time",
        "6",
        "--tls.crossing-clearance.time",
        "3",
    ]


def _crossing_audit(net_file: Path, selected: pd.DataFrame) -> pd.DataFrame:
    net = read_net(net_file)
    root = ET.parse(net_file).getroot()
    rows: list[dict[str, Any]] = []
    for _, row in selected.iterrows():
        cid = _norm(row.get("crosswalk_id"))
        node_id = _norm(row.get("selected_node_id"))
        tls_id = _norm(row.get("selected_tls_id")) or (node_id if _norm(row.get("manual_strategy")).upper() == "NEW_SIGNAL_INSTALLATION" else "")
        edges = _parse_crossing_edges(row)
        crossing_edge_id = _find_crossing_edge_id(net, node_id, edges)
        crossing_exists = bool(crossing_edge_id and crossing_edge_id in {e.getID() for e in net.getEdges()})
        tls_exists = bool(tls_id and tls_id in {t.getID() for t in net.getTrafficLights()})
        tls_connected = False
        ped_indices: list[int] = []
        route_from = ""
        route_to = ""
        if crossing_exists:
            try:
                route = pedestrian_route_from_crossing(net.getEdge(crossing_edge_id))
                route_from = _norm(route.get("from_edge"))
                route_to = _norm(route.get("to_edge"))
            except Exception:
                route_from = ""
                route_to = ""
        if crossing_exists and tls_exists:
            try:
                ped_indices = pedestrian_link_indices(net_file, tls_id, crossing_edge_id, xml_root=root)
            except Exception:
                ped_indices = []
            tls_connected = bool(ped_indices)
        phase = _phase_summary(root, tls_id, ped_indices) if tls_id else _phase_summary(root, "", [])
        rows.append(
            {
                "crosswalk_id": cid,
                "original_rank": _safe_int(row.get("original_rank")),
                "manual_strategy": _norm(row.get("manual_strategy")),
                "review_status": _norm(row.get("review_status")),
                "selected_node_id": node_id,
                "selected_tls_id": tls_id,
                "selected_crossing_edges": " ".join(edges),
                "crossing_edge_id": crossing_edge_id,
                "crossing_exists": crossing_exists,
                "tls_exists": tls_exists,
                "tls_connected": tls_connected,
                "ped_link_indices": "|".join(str(v) for v in ped_indices),
                "ped_link_index": ped_indices[0] if ped_indices else "",
                "ped_link_index_available": bool(ped_indices),
                "route_from_edge": route_from,
                "route_to_edge": route_to,
                "route_available": bool(route_from and route_to),
                "selected_crossing_edges_count": len(edges),
                **phase,
            }
        )
    return pd.DataFrame(rows)


def _candidate_table_from_audit(audit_df: pd.DataFrame, selection_df: pd.DataFrame, out_net: Path) -> pd.DataFrame:
    sel_map = { _norm(row.get("crosswalk_id")): row for _, row in selection_df.iterrows() }
    rows: list[dict[str, Any]] = []
    for _, row in audit_df.iterrows():
        cid = _norm(row.get("crosswalk_id"))
        sel = sel_map.get(cid)
        if sel is None:
            continue
        if not bool(row.get("crossing_exists")) or not bool(row.get("route_available")) or not bool(row.get("tls_connected")):
            continue
        ped_idx_text = _norm(row.get("ped_link_indices"))
        controlled_links_count = ped_idx_text.count("|") + 1 if ped_idx_text else 0
        rows.append(
            {
                "crosswalk_id": cid,
                "source_crosswalk_id": cid,
                "canonical_crosswalk_id": cid,
                "nearest_junction_id": _norm(sel.get("selected_node_id")),
                "tls_id": _norm(sel.get("selected_tls_id")) or _norm(sel.get("selected_node_id")),
                "tls_id_used": _norm(sel.get("selected_tls_id")) or _norm(sel.get("selected_node_id")),
                "crossing_id": _norm(row.get("crossing_edge_id")),
                "crossing_edge_id": _norm(row.get("crossing_edge_id")),
                "ped_link_index": _safe_int(row.get("ped_link_index"), -1),
                "ped_link_indices": _norm(row.get("ped_link_indices")),
                "ped_depart_offset_sec": 0,
                "route_from_edge": _norm(row.get("route_from_edge")),
                "route_to_edge": _norm(row.get("route_to_edge")),
                "generated_route_edges": _norm(row.get("route_from_edge")) + " " + _norm(row.get("route_to_edge")),
                "contains_crossing_edge": bool(row.get("crossing_exists")),
                "ped_repeat_count": 1,
                "ped_repeat_spacing_sec": 1.5,
                "route_reason": "manual edge pair approval",
                "incident_road_edges": _norm(sel.get("selected_crossing_edges")),
                "nearest_road_edge_id": _norm(sel.get("selected_from_edge")) or _norm(row.get("route_from_edge")),
                "source_file": "manual_edge_pair_selection.csv",
                "batch_network_file": str(out_net.resolve()),
                "final_verdict": "APPROVED",
                "step_test_ok": "PENDING",
                "controlled_links_count": controlled_links_count,
                "crossing_inventory_used": False,
                "manual_strategy": _norm(sel.get("manual_strategy")),
            }
        )
    return pd.DataFrame(rows)


def _write_smoke_command(out_dir: Path, name: str, candidate_csv: Path, net_file: Path) -> tuple[Path, Path]:
    run_script = out_dir / f"command_to_run_{name}_seed1_smoke.sh"
    check_script = out_dir / f"command_to_check_{name}_seed1_smoke.sh"
    smart_dir = out_dir / f"{name}_seed1_smart_smoke"
    base_dir = out_dir / f"{name}_seed1_baseline_smoke"
    run_script.write_text(
        f"""#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${{BASH_SOURCE[0]}}")/../../.." && pwd)"
cd "$PROJECT_ROOT"

if [[ -z "${{SUMO_HOME:-}}" ]]; then
  SUMO_HOME="$(python3 - <<'PY'
from smart_crosswalk_sumo.network_utils import resolve_sumo_home
print(resolve_sumo_home() or "")
PY
)"
fi
if [[ -z "$SUMO_HOME" ]]; then
  echo "SUMO_HOME not found" >&2
  exit 1
fi
export SUMO_HOME
export PATH="$SUMO_HOME/bin:$PATH"
if [[ -z "${{PROJ_LIB:-}}" ]]; then
  if [[ -d "$SUMO_HOME/share/proj" ]]; then
    export PROJ_LIB="$SUMO_HOME/share/proj"
  elif [[ -d "$SUMO_HOME/proj" ]]; then
    export PROJ_LIB="$SUMO_HOME/proj"
  fi
fi
export PYTHONPATH="$PROJECT_ROOT:${{PYTHONPATH:-}}"

CSV="{candidate_csv.resolve()}"
NET="{net_file.resolve()}"

python3 -m smart_crosswalk_sumo.run_phase6_recovery_smoke \
  --candidate-csv "$CSV" \
  --net-file "$NET" \
  --scenario smart \
  --seed 1 \
  --sim-duration 300 \
  --phase-aligned-ped-depart \
  --output-dir "{smart_dir.resolve()}"

python3 -m smart_crosswalk_sumo.run_phase6_recovery_smoke \
  --candidate-csv "$CSV" \
  --net-file "$NET" \
  --scenario baseline \
  --seed 1 \
  --sim-duration 300 \
  --phase-aligned-ped-depart \
  --output-dir "{base_dir.resolve()}"
""",
        encoding="utf-8",
    )
    run_script.chmod(0o755)

    check_script_text = """#!/usr/bin/env bash
set -euo pipefail

cd __BASE_DIR__

python3 - <<'PY'
import pandas as pd
from pathlib import Path

out_dir = Path(r"__OUT_DIR__")
cand = pd.read_csv(out_dir / 'p4_manual_recovery_candidate_table.csv') if (out_dir / 'p4_manual_recovery_candidate_table.csv').exists() else pd.DataFrame(columns=['crosswalk_id'])
smart = out_dir / 'p4_manual_seed1_smart_smoke' / 'phase6_smoke_summary.csv'
base = out_dir / 'p4_manual_seed1_baseline_smoke' / 'phase6_smoke_summary.csv'

smart_df = pd.read_csv(smart) if smart.exists() else pd.DataFrame()
base_df = pd.read_csv(base) if base.exists() else pd.DataFrame()

def b(v):
    return str(v).strip().lower() in ('1','true','yes','y')

def i(v, d=0):
    try:
        if pd.isna(v):
            return d
        return int(float(v))
    except Exception:
        return d

rows = []
for cid in cand['crosswalk_id'].astype(str).tolist():
    s = smart_df[smart_df['crosswalk_id'].astype(str) == cid]
    t = base_df[base_df['crosswalk_id'].astype(str) == cid]
    sr = s.iloc[0].to_dict() if not s.empty else {}
    tr = t.iloc[0].to_dict() if not t.empty else {}
    smart_ok = bool(sr) and b(sr.get('completed')) and i(sr.get('ped_crossing_person_count')) > 0 and i(sr.get('extension_count')) > 0
    base_ok = bool(tr) and b(tr.get('completed')) and i(tr.get('ped_crossing_person_count')) > 0 and i(tr.get('extension_count'), -1) == 0
    rows.append({
        'crosswalk_id': cid,
        'smart_completed': b(sr.get('completed')) if sr else False,
        'smart_ped_crossing_person_count': i(sr.get('ped_crossing_person_count')) if sr else 0,
        'smart_extension_count': i(sr.get('extension_count')) if sr else 0,
        'smart_smoke_success': smart_ok,
        'baseline_completed': b(tr.get('completed')) if tr else False,
        'baseline_ped_crossing_person_count': i(tr.get('ped_crossing_person_count')) if tr else 0,
        'baseline_extension_count': i(tr.get('extension_count'), -1) if tr else -1,
        'baseline_smoke_success': base_ok,
        'ready_for_30seed': bool(smart_ok and base_ok),
        'reason': 'seed1 smart/baseline passed' if smart_ok and base_ok else 'seed1 not passed',
    })

out = pd.DataFrame(rows)
out.to_csv(out_dir / 'p4_manual_seed1_smoke_verdict.csv', index=False)
print('verdict_csv=', out_dir / 'p4_manual_seed1_smoke_verdict.csv')
print('rows=', len(out))
print('ready=', int(out['ready_for_30seed'].sum()) if not out.empty else 0)
PY
"""
    check_script_text = check_script_text.replace("__BASE_DIR__", str(BASE_DIR)).replace("__OUT_DIR__", str(out_dir.resolve()))
    check_script.write_text(check_script_text, encoding="utf-8")
    check_script.chmod(0o755)
    return run_script, check_script


def _render_map(out_html: Path, records: list[dict[str, Any]]) -> None:
    center = [37.56, 126.98]
    for rec in records:
        real_lat = rec.get("real_lat")
        real_lon = rec.get("real_lon")
        if real_lat != "" and real_lon != "":
            center = [float(real_lat), float(real_lon)]
            break
    payload = json.dumps(records, ensure_ascii=False)
    doc = f"""<!DOCTYPE html>
<html lang="ko">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>P4 manual edge pair review map</title>
  <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
  <style>
    html, body, #map {{ height: 100%; margin: 0; }}
    body {{ background: #0f172a; }}
    .panel {{
      position: absolute; z-index: 1000; top: 12px; left: 12px;
      background: rgba(15, 23, 42, 0.92); color: #e2e8f0; padding: 12px 14px;
      border: 1px solid rgba(148, 163, 184, 0.28); border-radius: 12px;
      max-width: 520px; font-family: Inter, ui-sans-serif, system-ui, sans-serif;
      box-shadow: 0 16px 40px rgba(0,0,0,0.22);
    }}
    .panel h1 {{ margin: 0 0 6px; font-size: 16px; }}
    .panel p {{ margin: 0; font-size: 12px; line-height: 1.45; color: #cbd5e1; }}
    .legend {{ margin-top: 8px; display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 6px 10px; font-size: 12px; }}
    .legend span {{ display: inline-flex; align-items: center; gap: 6px; }}
    .dot {{ width: 10px; height: 10px; border-radius: 999px; display: inline-block; }}
    .dot.real {{ background: #22c55e; }}
    .dot.current {{ background: #94a3b8; }}
    .dot.generated {{ background: #38bdf8; }}
    .dot.tls {{ background: #f43f5e; }}
    .dot.node {{ background: #f59e0b; }}
    .dot.edge {{ background: #a855f7; }}
    table {{ border-collapse: collapse; width: 100%; }}
    th, td {{ border: 1px solid rgba(148,163,184,.22); padding: 3px 5px; font-size: 12px; vertical-align: top; }}
    th {{ text-align: left; color: #cbd5e1; background: rgba(30,41,59,.72); }}
    td {{ color: #f8fafc; }}
    .small {{ font-size: 11px; color: #cbd5e1; }}
  </style>
</head>
<body>
<div id="map"></div>
<div class="panel">
  <h1>P4 manual edge pair review</h1>
  <p>Real point 기준 판단. current SUMO point 틀릴 수 있음. generated point 있으면 보조만 사용.</p>
  <div class="legend">
    <span><i class="dot real"></i> real point</span>
    <span><i class="dot current"></i> current SUMO</span>
    <span><i class="dot generated"></i> generated point</span>
    <span><i class="dot tls"></i> nearest TLS</span>
    <span><i class="dot node"></i> nearby nodes</span>
    <span><i class="dot edge"></i> candidate edges</span>
  </div>
</div>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script>
const data = {payload};
const map = L.map('map').setView([{center[0]}, {center[1]}], 14);
L.tileLayer('https://{{s}}.tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png', {{ maxZoom: 19, attribution: '&copy; OpenStreetMap' }}).addTo(map);

function esc(v) {{
  return String(v ?? '').replace(/[&<>"']/g, s => ({{
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
  }}[s]));
}}

function point(lat, lon) {{
  return [Number(lat), Number(lon)];
}}

function popupHtml(r) {{
  const nodes = JSON.parse(r.nearby_nodes_json || '[]');
  const edges = JSON.parse(r.candidate_edges_json || '[]');
  const rows = [
    ['crosswalk_id', r.crosswalk_id],
    ['original_rank', r.original_rank],
    ['mapping_distance_m', r.mapping_distance_m],
    ['failure_category', r.failure_category],
    ['recommended_manual_strategy', r.recommended_manual_strategy || ''],
    ['nearest_tls_id', r.nearest_tls_id],
    ['nearest_tls_distance_m', r.nearest_tls_distance_m],
  ].map(([k, v]) => `<tr><th>${{esc(k)}}</th><td>${{esc(v)}}</td></tr>`).join('');
  const edgeList = edges.slice(0, 8).map(e => `<li>${{esc(e.edge_id)}} (${{esc(e.distance_m)}}m, ${{esc(e.from_node)}} → ${{esc(e.to_node)}})</li>`).join('');
  const nodeList = nodes.slice(0, 8).map(n => `<li>${{esc(n.node_id)}} (${{esc(n.distance_m)}}m, ${{esc(n.type)}})</li>`).join('');
  return `
    <table>${{rows}}</table>
    <div class="small" style="margin-top:6px;">selected_node 후보</div>
    <ul class="small" style="margin:4px 0 0 18px;">${{nodeList}}</ul>
    <div class="small" style="margin-top:6px;">candidate edge list</div>
    <ul class="small" style="margin:4px 0 0 18px;">${{edgeList}}</ul>
  `;
}}

const colors = ['#a855f7', '#22d3ee', '#f97316', '#84cc16', '#f43f5e', '#e879f9', '#38bdf8', '#f59e0b'];
const bounds = [];

for (const r of data) {{
  const color = colors[(Number(r.original_rank) - 1) % colors.length];
  const group = L.layerGroup().addTo(map);

  if (r.real_lat !== '' && r.real_lon !== '') {{
    const m = L.circleMarker(point(r.real_lat, r.real_lon), {{ radius: 7, color: '#22c55e', fillColor: '#22c55e', fillOpacity: 0.9 }}).addTo(group);
    m.bindPopup(popupHtml(r));
    bounds.push(point(r.real_lat, r.real_lon));
  }}
  if (r.current_sumo_lat !== '' && r.current_sumo_lon !== '') {{
    const m = L.circleMarker(point(r.current_sumo_lat, r.current_sumo_lon), {{ radius: 6, color: '#94a3b8', fillColor: '#94a3b8', fillOpacity: 0.85 }}).addTo(group);
    m.bindPopup(popupHtml(r));
    bounds.push(point(r.current_sumo_lat, r.current_sumo_lon));
  }}
  if (r.generated_lat !== '' && r.generated_lon !== '' && !Number.isNaN(Number(r.generated_lat)) && !Number.isNaN(Number(r.generated_lon))) {{
    const m = L.circleMarker(point(r.generated_lat, r.generated_lon), {{ radius: 6, color: '#38bdf8', fillColor: '#38bdf8', fillOpacity: 0.9 }}).addTo(group);
    m.bindPopup(popupHtml(r));
    bounds.push(point(r.generated_lat, r.generated_lon));
  }}
  if (r.nearest_tls_lat !== '' && r.nearest_tls_lon !== '' && !Number.isNaN(Number(r.nearest_tls_lat)) && !Number.isNaN(Number(r.nearest_tls_lon))) {{
    const m = L.circleMarker(point(r.nearest_tls_lat, r.nearest_tls_lon), {{ radius: 7, color: '#f43f5e', fillColor: '#f43f5e', fillOpacity: 0.9 }}).addTo(group);
    m.bindPopup(popupHtml(r));
    bounds.push(point(r.nearest_tls_lat, r.nearest_tls_lon));
  }}

  const nodes = JSON.parse(r.nearby_nodes_json || '[]');
  for (const n of nodes.slice(0, 8)) {{
    if (n.lat === '' || n.lon === '' || Number.isNaN(Number(n.lat)) || Number.isNaN(Number(n.lon))) continue;
    const m = L.circleMarker(point(n.lat, n.lon), {{
      radius: 4,
      color: '#f59e0b',
      fillColor: '#f59e0b',
      fillOpacity: 0.7,
      opacity: 0.7,
    }}).addTo(group);
    m.bindPopup(popupHtml(r));
    bounds.push(point(n.lat, n.lon));
  }}
  const edgeRecords = JSON.parse(r.candidate_edges_json || '[]');
  for (let i = 0; i < edgeRecords.length; i++) {{
    const edge = edgeRecords[i];
    const shape = edge.shape || [];
    if (!shape.length) continue;
    const poly = L.polyline(shape.map(pt => [Number(pt[1]), Number(pt[0])]), {{
      color: i === 0 ? color : '#a855f7',
      weight: i === 0 ? 5 : 3,
      opacity: i === 0 ? 0.75 : 0.35,
      dashArray: i === 0 ? '' : '4 6',
    }}).addTo(group);
    poly.bindPopup(popupHtml(r));
    for (const pt of shape) bounds.push([Number(pt[1]), Number(pt[0])]);
  }}
}}

if (bounds.length) {{
  map.fitBounds(bounds, {{ padding: [28, 28] }});
}}
</script>
</body>
</html>
"""
    out_html.write_text(doc, encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="Prepare P4 manual edge pair recovery workflow.")
    parser.add_argument("--output-dir", default="", help="Optional output directory. Default: timestamped result dir.")
    parser.add_argument("--manual-selection-csv", default="", help="Optional reviewer-filled selection CSV. If omitted, generated template is used.")
    parser.add_argument("--base-net", default=str(DEFAULT_BASE_NET))
    parser.add_argument("--p4-candidates", default=str(DEFAULT_P4_CANDIDATES))
    parser.add_argument("--mapping-summary", default=str(DEFAULT_MAPPING_SUMMARY))
    parser.add_argument("--generated-check", default=str(DEFAULT_GENERATED_CHECK))
    parser.add_argument("--generated-patch", default=str(DEFAULT_GENERATED_PATCH))
    args = parser.parse_args()

    apply_sumo_environment()
    out_dir = Path(args.output_dir).expanduser().resolve() if args.output_dir else RESULT_DIR / f"phase_next_p4_manual_edge_pair_recovery_{_now_tag()}"
    out_dir.mkdir(parents=True, exist_ok=True)

    base_net = Path(args.base_net).expanduser().resolve()
    if not base_net.exists():
        raise FileNotFoundError(base_net)

    p4_df = _csv(Path(args.p4_candidates).expanduser().resolve())
    if p4_df.empty:
        raise RuntimeError("P4 input empty")

    net = read_net(base_net)
    template_df = _build_template_rows(p4_df)
    template_path = out_dir / "manual_edge_pair_selection_template.csv"
    template_df.to_csv(template_path, index=False)

    nearby_rows: list[dict[str, Any]] = []
    map_records: list[dict[str, Any]] = []
    for _, row in p4_df.sort_values(["original_rank", "crosswalk_id"]).iterrows():
        edge_rows, context = _edge_candidate_records(net, row)
        nearby_rows.extend(edge_rows)
        map_records.append(
            {
                "crosswalk_id": _norm(row.get("crosswalk_id")),
                "original_rank": _safe_int(row.get("original_rank")),
                "real_lon": _safe_float(row.get("real_lon")),
                "real_lat": _safe_float(row.get("real_lat")),
                "current_sumo_lon": _safe_float(row.get("current_sumo_lon")),
                "current_sumo_lat": _safe_float(row.get("current_sumo_lat")),
                "generated_lon": _safe_float(row.get("generated_lon")),
                "generated_lat": _safe_float(row.get("generated_lat")),
                "mapping_distance_m": round(_safe_float(row.get("mapping_distance_m")), 3),
                "failure_category": _norm(row.get("failure_category")),
                "recommended_manual_strategy": _default_manual_strategy(_norm(row.get("failure_category")), _norm(row.get("manual_review_type"))),
                "nearest_tls_id": context["nearest_tls_id"],
                "nearest_tls_distance_m": context["nearest_tls_distance_m"],
                "nearest_tls_lon": context["nearest_tls_lon"],
                "nearest_tls_lat": context["nearest_tls_lat"],
                "nearby_nodes_json": context["nearby_nodes_json"],
                "candidate_edges_json": context["candidate_edges_json"],
            }
        )

    nearby_df = pd.DataFrame(nearby_rows)
    nearby_path = out_dir / "p4_nearby_edge_tls_candidates.csv"
    nearby_df.to_csv(nearby_path, index=False)

    _render_map(out_dir / "p4_manual_edge_pair_review_map.html", map_records)

    guide = [
        "# P4 manual edge pair review guide",
        "",
        "- 판단 기준은 real point다.",
        "- current SUMO mapped point는 틀릴 수 있다.",
        "- generated point 있으면 보조 증거만 본다.",
        "- selected_crossing_edges는 실제 횡단보도가 가로지르는 edge pair다.",
        "- EXISTING_TLS_RETROFIT: 기존 TLS 유지, phase만 맞춘다.",
        "- NEW_SIGNAL_INSTALLATION: 새 TLS 설치 쪽. base net 그대로 덮어쓰지 않는다.",
        "- confidence는 `LOW`/`MEDIUM`/`HIGH`처럼 사람이 확신한 정도를 적는다.",
        "- seed1 smoke 통과 전에는 ready가 아니다.",
        "- generated/new signal 후보는 main experiment와 분리한다.",
        "- HOLD면 후보 보류. REJECT면 제외.",
        "",
        "워크플로우:",
        "1. 지도에서 real point와 nearby node/edge를 보고 node id를 고른다.",
        "2. selected_crossing_edges에 실제 pair를 채운다.",
        "3. selected_tls_id를 고른다. NEW_SIGNAL_INSTALLATION이면 new TLS id도 가능.",
        "4. review_status를 APPROVED로 바꾼다.",
        "5. recovery script로 patch plan/net/candidate table을 만든 뒤 seed1 smoke는 로컬에서 실행한다.",
    ]
    (out_dir / "p4_manual_edge_pair_review_guide.md").write_text("\n".join(guide) + "\n", encoding="utf-8")

    manual_selection_csv = Path(args.manual_selection_csv).expanduser().resolve() if args.manual_selection_csv else None
    if manual_selection_csv is not None and manual_selection_csv.exists():
        selection_df = pd.read_csv(manual_selection_csv)
    else:
        selection_df = template_df.copy()

    for col in template_df.columns:
        if col not in selection_df.columns:
            selection_df[col] = template_df[col].iloc[0] if len(template_df) else ""
    selection_df = selection_df[template_df.columns]

    approved = selection_df[selection_df["review_status"].astype(str).str.upper() == "APPROVED"].copy()
    if approved.empty:
        crossing_patch_path = out_dir / "p4_manual_crossing_patch.nod.xml"
        tllogic_patch_path = out_dir / "p4_manual_tllogic_patch.tll.xml"
        crossing_patch_path.write_text('<?xml version="1.0" encoding="UTF-8"?><nodes/>\\n', encoding="utf-8")
        tllogic_patch_path.write_text('<?xml version="1.0" encoding="UTF-8"?><tlLogics/>\\n', encoding="utf-8")
        crossing_patch_plan = pd.DataFrame(columns=["crosswalk_id", "patch_kind", "patch_status", "patch_reason"])
        tllogic_plan = pd.DataFrame(columns=["crosswalk_id", "patch_kind", "patch_status", "patch_reason"])
        audit_df = pd.DataFrame(columns=[
            "crosswalk_id",
            "original_rank",
            "manual_strategy",
            "review_status",
            "selected_node_id",
            "selected_tls_id",
            "selected_crossing_edges",
            "crossing_edge_id",
            "crossing_exists",
            "tls_exists",
            "tls_connected",
            "ped_link_indices",
            "ped_link_index",
            "ped_link_index_available",
            "route_from_edge",
            "route_to_edge",
            "route_available",
            "selected_crossing_edges_count",
            "tlLogic_present",
            "ped_has_green",
            "ped_has_red",
            "pedestrian_only_phase_available",
            "pedestrian_only_phase_indices",
            "phase_states",
        ])
        candidate_df = pd.DataFrame(columns=[
            "crosswalk_id",
            "source_crosswalk_id",
            "canonical_crosswalk_id",
            "nearest_junction_id",
            "tls_id",
            "tls_id_used",
            "crossing_id",
            "crossing_edge_id",
            "ped_link_index",
            "ped_link_indices",
            "ped_depart_offset_sec",
            "route_from_edge",
            "route_to_edge",
            "generated_route_edges",
            "contains_crossing_edge",
            "ped_repeat_count",
            "ped_repeat_spacing_sec",
            "route_reason",
            "incident_road_edges",
            "nearest_road_edge_id",
            "source_file",
            "batch_network_file",
            "final_verdict",
            "step_test_ok",
            "controlled_links_count",
            "crossing_inventory_used",
            "manual_strategy",
        ])
        patch_plan = pd.DataFrame(columns=[
            "crosswalk_id",
            "patch_kind",
            "patch_status",
            "patch_reason",
            "selected_node_id",
            "selected_tls_id",
            "selected_crossing_edges",
            "selected_from_edge",
            "selected_to_edge",
            "manual_strategy",
            "tls_id",
            "crossing_edge_id",
            "ped_link_indices",
            "patch_action",
            "patched_phase_index",
        ])
        final_net = out_dir / "p4_manual_recovery_net.net.xml"
        final_ok = False
        netconvert_ok = False
        netconvert_msg = "no approved P4 rows"
        run_cmd = out_dir / "command_to_run_p4_manual_seed1_smoke.sh"
        check_cmd = out_dir / "command_to_check_p4_manual_seed1_smoke.sh"
        # smoke commands still generated below, using empty candidate table
    else:
        crossing_patch_path, crossing_patch_plan = _build_crossing_patch(approved, net, out_dir)
        crossing_pre_net = out_dir / "p4_manual_recovery_pre_tl.net.xml"
        crossing_cmd = _netconvert_cmd(base_net, crossing_patch_path, crossing_pre_net)
        netconvert_ok, netconvert_msg = _run_cmd(crossing_cmd, out_dir / "p4_manual_crossing_netconvert.log")
        if not netconvert_ok and not crossing_pre_net.exists():
            raise RuntimeError(f"crossing netconvert failed: {netconvert_msg}")
        if not crossing_pre_net.exists():
            raise FileNotFoundError(crossing_pre_net)

        pre_root = ET.parse(crossing_pre_net).getroot()
        tllogic_patch_path = out_dir / "p4_manual_tllogic_patch.tll.xml"
        tllogic_plan = _build_tllogic_patch(crossing_pre_net, approved, tllogic_patch_path, pre_root)

        final_net = out_dir / "p4_manual_recovery_net.net.xml"
        final_cmd = None
        final_ok = False
        if not tllogic_plan.empty and any(tllogic_plan["patch_status"].astype(str).str.upper() == "PATCHED"):
            final_cmd = _netconvert_tll_cmd(crossing_pre_net, tllogic_patch_path, final_net)
            final_ok, final_msg = _run_cmd(final_cmd, out_dir / "p4_manual_tllogic_netconvert.log")
            if not final_ok and not final_net.exists():
                raise RuntimeError(f"tllogic netconvert failed: {final_msg}")
        else:
            import shutil

            shutil.copyfile(crossing_pre_net, final_net)
            final_ok = True

        audit_df = _crossing_audit(final_net, approved)
    audit_columns = [
        "crosswalk_id",
        "original_rank",
        "manual_strategy",
        "review_status",
        "selected_node_id",
        "selected_tls_id",
        "selected_crossing_edges",
        "crossing_edge_id",
        "crossing_exists",
        "tls_exists",
        "tls_connected",
        "ped_link_indices",
        "ped_link_index",
        "ped_link_index_available",
        "route_from_edge",
        "route_to_edge",
        "route_available",
        "selected_crossing_edges_count",
        "tlLogic_present",
        "ped_has_green",
        "ped_has_red",
        "pedestrian_only_phase_available",
        "pedestrian_only_phase_indices",
        "phase_states",
    ]
    if audit_df.empty:
        audit_df = pd.DataFrame(columns=audit_columns)
    else:
        audit_df = audit_df.reindex(columns=[c for c in audit_columns if c in audit_df.columns] + [c for c in audit_df.columns if c not in audit_columns])
    audit_path = out_dir / "p4_manual_recovery_phase_audit.csv"
    audit_df.to_csv(audit_path, index=False)

    candidate_df = _candidate_table_from_audit(audit_df, approved, final_net)
    candidate_columns = [
        "crosswalk_id",
        "source_crosswalk_id",
        "canonical_crosswalk_id",
        "nearest_junction_id",
        "tls_id",
        "tls_id_used",
        "crossing_id",
        "crossing_edge_id",
        "ped_link_index",
        "ped_link_indices",
        "ped_depart_offset_sec",
        "route_from_edge",
        "route_to_edge",
        "generated_route_edges",
        "contains_crossing_edge",
        "ped_repeat_count",
        "ped_repeat_spacing_sec",
        "route_reason",
        "incident_road_edges",
        "nearest_road_edge_id",
        "source_file",
        "batch_network_file",
        "final_verdict",
        "step_test_ok",
        "controlled_links_count",
        "crossing_inventory_used",
        "manual_strategy",
    ]
    if candidate_df.empty:
        candidate_df = pd.DataFrame(columns=candidate_columns)
    else:
        candidate_df = candidate_df.reindex(columns=[c for c in candidate_columns if c in candidate_df.columns] + [c for c in candidate_df.columns if c not in candidate_columns])
    candidate_path = out_dir / "p4_manual_recovery_candidate_table.csv"
    candidate_df.to_csv(candidate_path, index=False)

    patch_plan = crossing_patch_plan.copy()
    if not tllogic_plan.empty:
        patch_plan = pd.concat([patch_plan, tllogic_plan], ignore_index=True, sort=False)
    patch_plan_columns = [
        "crosswalk_id",
        "patch_kind",
        "patch_status",
        "patch_reason",
        "selected_node_id",
        "selected_tls_id",
        "selected_crossing_edges",
        "selected_from_edge",
        "selected_to_edge",
        "manual_strategy",
        "tls_id",
        "crossing_edge_id",
        "ped_link_indices",
        "patch_action",
        "patched_phase_index",
    ]
    if patch_plan.empty:
        patch_plan = pd.DataFrame(columns=patch_plan_columns)
    else:
        patch_plan = patch_plan.reindex(columns=[c for c in patch_plan_columns if c in patch_plan.columns] + [c for c in patch_plan.columns if c not in patch_plan_columns])
    patch_plan_path = out_dir / "p4_manual_recovery_patch_plan.csv"
    patch_plan.to_csv(patch_plan_path, index=False)

    run_cmd, check_cmd = _write_smoke_command(out_dir, "p4_manual", candidate_path, final_net)

    summary = [
        "# P4 manual edge pair recovery summary",
        "",
        f"- output_dir: `{out_dir}`",
        f"- p4_input_rows: {len(p4_df)}",
        f"- nearby_edge_rows: {len(nearby_df)}",
        f"- template_rows: {len(template_df)}",
        f"- approved_rows: {len(approved)}",
        f"- crossing_patch_rows: {len(crossing_patch_plan)}",
        f"- tllogic_patch_rows: {len(tllogic_plan)}",
        f"- final_net_created: {final_ok}",
        f"- candidate_table_rows: {len(candidate_df)}",
        f"- netconvert_msg: {netconvert_msg}",
        "",
        "## outputs",
        f"- `{template_path.name}`",
        f"- `{nearby_path.name}`",
        f"- `{patch_plan_path.name}`",
        f"- `{candidate_path.name}`",
        f"- `{audit_path.name}`",
        f"- `{final_net.name}`",
        f"- `{run_cmd.name}`",
        f"- `{check_cmd.name}`",
    ]
    (out_dir / "p4_manual_edge_pair_recovery_summary.md").write_text("\n".join(summary) + "\n", encoding="utf-8")

    print(f"output_dir: {out_dir}")
    print(f"p4_input_rows: {len(p4_df)}")
    print(f"nearby_edge_rows: {len(nearby_df)}")
    print(f"template_rows: {len(template_df)}")
    print(f"approved_rows: {len(approved)}")
    print(f"candidate_table_rows: {len(candidate_df)}")
    print(f"template_csv: {template_path}")
    print(f"map_html: {out_dir / 'p4_manual_edge_pair_review_map.html'}")
    print(f"guide_md: {out_dir / 'p4_manual_edge_pair_review_guide.md'}")
    print(f"run_smoke_cmd: {run_cmd}")
    print(f"check_smoke_cmd: {check_cmd}")


if __name__ == "__main__":
    main()
