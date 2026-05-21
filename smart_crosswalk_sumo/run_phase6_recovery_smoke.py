#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import math
import random
import shutil
import socket
import subprocess
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from smart_crosswalk_sumo.network_utils import (
    distance_to_edge_shape,
    edge_center,
    discover_network_metadata_from_net,
    edge_function,
    lane_allows,
    pedestrian_link_indices,
    pedestrian_route_from_crossing,
    read_net,
)

try:
    import traci  # type: ignore
except Exception:  # pragma: no cover
    traci = None

BASE_DIR = Path(__file__).resolve().parents[1]
RESULT_DIR = BASE_DIR / "result"
FACTORY_TABLE = RESULT_DIR / "phase_next_real_csv_crossing_factory_20260514_103928" / "real_csv_crossing_factory_table.csv"
LAST_ROUTE_PAIR_DIAGNOSTICS: pd.DataFrame | None = None


@dataclass
class Candidate:
    crosswalk_id: str
    nearest_junction_id: str
    tls_id_used: str
    crossing_id: str
    ped_link_index: int
    ped_depart_offset_sec: float
    crossing_edge_id: str
    route_from_edge: str
    route_to_edge: str
    ped_repeat_count: int
    ped_repeat_spacing_sec: float
    source_file: str
    batch_network_file: str


def _mean(values: list[float]) -> float:
    arr = np.asarray(values, dtype=float)
    return float(np.nanmean(arr)) if arr.size else float("nan")


def _sha256_file(path: Path | None) -> str:
    if path is None or not path.exists():
        return ""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _percentile(values: list[float], q: float) -> float:
    arr = np.asarray(values, dtype=float)
    if not arr.size:
        return float("nan")
    return float(np.nanpercentile(arr, q))


def _parse_edges(value: Any) -> list[str]:
    if value is None:
        return []
    text = str(value).strip()
    if not text or text in {"nan", "None"}:
        return []
    if text.startswith("[") and text.endswith("]"):
        try:
            parsed = json.loads(text.replace("'", '"'))
            if isinstance(parsed, list):
                return [str(x).strip() for x in parsed if str(x).strip()]
        except Exception:
            pass
        text = text.strip("[]")
    text = text.replace(",", " ").replace("|", " ")
    return [tok.strip().strip("'").strip('"') for tok in text.split() if tok.strip()]


def _load_csv(path: Path) -> pd.DataFrame:
    if not path.exists():
        raise FileNotFoundError(path)
    return pd.read_csv(path)


def _load_csv_optional(path: Path | None) -> pd.DataFrame | None:
    if path is None:
        return None
    if not path.exists():
        return None
    return pd.read_csv(path)


def _load_xml_root(net_file: Path) -> ET.Element:
    return ET.parse(net_file).getroot()


def _normalize_id_text(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    if not text or text in {"nan", "None"}:
        return ""
    if text.endswith(".0"):
        raw = text[:-2]
        if raw.replace("-", "", 1).isdigit():
            return raw
    return text


def _parse_int_list(value: Any) -> list[int]:
    if value is None:
        return []
    if isinstance(value, list):
        out: list[int] = []
        for item in value:
            try:
                out.append(int(float(str(item).strip())))
            except Exception:
                continue
        return sorted(set(out))
    text = str(value).strip()
    if not text or text in {"nan", "None", "[]"}:
        return []
    if text.startswith("[") and text.endswith("]"):
        text = text[1:-1]
    text = text.replace(",", " ").replace("|", " ")
    out: list[int] = []
    for token in text.split():
        token = token.strip().strip("'").strip('"')
        if not token:
            continue
        try:
            out.append(int(float(token)))
        except Exception:
            continue
    return sorted(set(out))


def _first_present(row: pd.Series, names: list[str]) -> Any:
    for name in names:
        if name in row.index:
            value = row[name]
            if pd.notna(value):
                return value
    return None


def _first_nonempty_text(row: pd.Series, names: list[str]) -> str:
    value = _first_present(row, names)
    return _normalize_id_text(value)


def _route_pair_from_factory_row(factory_row: pd.Series) -> tuple[str, str]:
    incident_edges = _parse_edges(factory_row.get("incident_road_edges"))
    nearest_road_edge = _normalize_id_text(factory_row.get("nearest_road_edge_id"))
    return _choose_route_pair(incident_edges, nearest_road_edge)


def _resolve_batch_network(source_file: str, override: str | None) -> str:
    if override:
        return str(Path(override).expanduser().resolve())
    if "batch_03_run/batch_promoted.csv" in source_file:
        return str((RESULT_DIR / "phase_next_recovery_command_plan_20260514_200908" / "batch_03_run" / "recovery_tls_batch_network_v1.net.xml").resolve())
    if "batch_02_run/batch_promoted.csv" in source_file:
        return str((RESULT_DIR / "phase_next_recovery_command_plan_20260514_200908" / "batch_02_run" / "recovery_tls_batch_network_v1.net.xml").resolve())
    if "batch_01_run/batch_promoted.csv" in source_file:
        return str((RESULT_DIR / "phase_next_recovery_command_plan_20260514_200908" / "batch_01_run" / "recovery_tls_batch_network_v1.net.xml").resolve())
    return str((RESULT_DIR / "phase_next_recovery_command_plan_20260514_200908" / "batch_03_run" / "recovery_tls_batch_network_v1.net.xml").resolve())


def _choose_route_pair(incident_edges: list[str], nearest_road_edge: str) -> tuple[str, str]:
    if not incident_edges:
        raise ValueError("incident road edges missing")
    from_edge = nearest_road_edge if nearest_road_edge else incident_edges[0]
    to_edge = None
    from_core = from_edge.lstrip("-")
    for edge in incident_edges:
        if edge == from_edge:
            continue
        if edge.lstrip("-") == from_core:
            continue
        if edge.startswith("-") != from_edge.startswith("-"):
            to_edge = edge
            break
    if to_edge is None:
        for edge in incident_edges:
            if edge != from_edge:
                to_edge = edge
                break
    if to_edge is None:
        to_edge = from_edge
    return from_edge, to_edge


def _find_crossing_edge(node_id: str, route_from_edge: str, inventory: pd.DataFrame) -> str:
    node_rows = inventory[inventory["node_id"].astype(str) == str(node_id)].copy()
    if node_rows.empty:
        return ""
    core = route_from_edge.lstrip("-")
    canon = node_rows["crossingEdges_canonical"].astype(str).str.replace("-", "", regex=False)
    match = node_rows[canon == core]
    if not match.empty:
        return str(match.iloc[0]["crossing_edge_id"])
    return str(node_rows.iloc[0]["crossing_edge_id"])


def _crossing_edge_candidates_for_tls(xml_root: ET.Element, tls_id: str) -> list[str]:
    prefix = f":{tls_id}_"
    candidates: list[str] = []
    seen: set[str] = set()

    for edge in xml_root.findall(".//edge"):
        if str(edge.attrib.get("function", "")).lower() != "crossing":
            continue
        edge_id = str(edge.attrib.get("id", "") or "")
        if edge_id.startswith(prefix) and edge_id not in seen:
            candidates.append(edge_id)
            seen.add(edge_id)

    if candidates:
        return candidates

    for conn in xml_root.findall(f".//connection[@tl='{tls_id}']"):
        for attr in ("from", "to"):
            edge_id = _normalize_id_text(conn.attrib.get(attr))
            if edge_id.startswith(prefix) and edge_id not in seen:
                candidates.append(edge_id)
                seen.add(edge_id)
        via = _normalize_id_text(conn.attrib.get("via"))
        if via.startswith(prefix):
            edge_id = via.rsplit("_", 1)[0] if "_" in via else via
            if edge_id.startswith(prefix) and edge_id not in seen:
                candidates.append(edge_id)
                seen.add(edge_id)

    return candidates


def _audit_crossing_edge_from_net(
    net: Any,
    net_file: Path,
    xml_root: ET.Element,
    tls_id: str,
    ped_link_indices: list[int],
    route_from_edge: str,
    route_to_edge: str,
) -> tuple[str, list[int], str, str, list[str], str]:
    candidate_edges = _crossing_edge_candidates_for_tls(xml_root, tls_id)
    if not candidate_edges:
        return "", ped_link_indices, route_from_edge, route_to_edge, [], "crossing_edge_not_found_in_net"

    ped_set = set(int(v) for v in ped_link_indices)
    scored: list[tuple[tuple[int, int, int, int], str, list[int], str, str, list[str], str]] = []

    for edge_id in candidate_edges:
        try:
            inferred_indices = _parse_int_list(pedestrian_link_indices(net_file, tls_id, edge_id, xml_root=xml_root))
        except Exception:
            inferred_indices = []
        inferred_set = set(inferred_indices)

        route_candidates: list[tuple[str, str, str]] = []
        try:
            route_candidates = _route_pair_from_crossing_edge(net, edge_id)
        except Exception:
            route_candidates = []

        validated_routes: list[tuple[str, str, list[str]]] = []
        for frm, to, _method in route_candidates:
            valid, _reason, path_ids = _validate_route_pair(net, edge_id, frm, to)
            if valid:
                validated_routes.append((frm, to, path_ids))

        route_match = False
        selected_from = route_from_edge
        selected_to = route_to_edge
        selected_path_ids: list[str] = []
        if route_from_edge and route_to_edge:
            valid, _reason, path_ids = _validate_route_pair(net, edge_id, route_from_edge, route_to_edge)
            if valid:
                route_match = True
                selected_path_ids = path_ids
        if not route_match and validated_routes:
            selected_from, selected_to, selected_path_ids = validated_routes[0]
            route_match = True

        score = (
            2 if ped_set and inferred_set == ped_set else 1 if ped_set and (ped_set & inferred_set) else 0,
            1 if route_match and (route_from_edge and route_to_edge) else 0,
            1 if route_match else 0,
            len(inferred_set & ped_set) if ped_set else len(inferred_set),
        )
        scored.append((score, edge_id, inferred_indices, selected_from, selected_to, selected_path_ids, "net_audit"))

    scored.sort(key=lambda item: item[0], reverse=True)
    best_score, best_edge_id, best_ped_indices, best_from, best_to, best_path_ids, best_reason = scored[0]
    if best_score == (0, 0, 0, 0):
        return "", ped_link_indices, route_from_edge, route_to_edge, [], "crossing_edge_audit_failed"

    if not best_ped_indices:
        try:
            best_ped_indices = _parse_int_list(pedestrian_link_indices(net_file, tls_id, best_edge_id, xml_root=xml_root))
        except Exception:
            best_ped_indices = ped_link_indices

    if not best_from or not best_to:
        try:
            for frm, to, _method in _route_pair_from_crossing_edge(net, best_edge_id):
                valid, _reason, path_ids = _validate_route_pair(net, best_edge_id, frm, to)
                if valid:
                    best_from, best_to, best_path_ids = frm, to, path_ids
                    best_reason = "net_audit_route_discovery"
                    break
        except Exception:
            pass

    return best_edge_id, best_ped_indices, best_from, best_to, best_path_ids, best_reason


def _candidate_crossing_roads(node_id: str, crossing_edge_id: str) -> set[str]:
    roads = {str(crossing_edge_id)} if crossing_edge_id else set()
    roads.add(f":{node_id}_c0")
    roads.add(f":{node_id}_c1")
    roads.add(f":{node_id}_c2")
    roads.add(f":{node_id}_w0")
    roads.add(f":{node_id}_w1")
    roads.add(f":{node_id}_w2")
    roads.discard("")
    roads.discard("nan")
    roads.discard("None")
    return {road for road in roads if road}


def _safe_net_edge(net: Any, edge_id: str) -> Any | None:
    if not net or not edge_id:
        return None
    try:
        return net.getEdge(str(edge_id))
    except Exception:
        return None


def _passenger_depart_allowed(edge: Any | None) -> bool:
    if edge is None:
        return False
    if str(edge_function(edge)).lower() != "normal":
        return False
    try:
        lanes = list(edge.getLanes())
    except Exception:
        lanes = []
    return any(lane_allows(lane, "passenger") for lane in lanes)


def _nearby_passenger_edges(
    net: Any,
    seed_edge_ids: list[str],
    *,
    max_depth: int = 2,
) -> list[str]:
    candidates: list[str] = []
    seen_edges: set[str] = set()
    seen_nodes: set[str] = set()
    queue: list[tuple[Any, int]] = []

    def enqueue_node(node: Any, depth: int) -> None:
        if node is None:
            return
        node_id = str(getattr(node, "getID", lambda: "")() or "")
        if not node_id or node_id in seen_nodes or depth > max_depth:
            return
        seen_nodes.add(node_id)
        queue.append((node, depth))

    for edge_id in seed_edge_ids:
        edge = _safe_net_edge(net, edge_id)
        if edge is None:
            continue
        if _passenger_depart_allowed(edge):
            normalized_id = str(edge.getID())
            if normalized_id not in seen_edges:
                seen_edges.add(normalized_id)
                candidates.append(normalized_id)
        enqueue_node(edge.getFromNode(), 0)
        enqueue_node(edge.getToNode(), 0)

    idx = 0
    while idx < len(queue):
        node, depth = queue[idx]
        idx += 1
        connected_edges = list(node.getIncoming()) + list(node.getOutgoing())
        for edge in connected_edges:
            edge_id = str(edge.getID())
            if _passenger_depart_allowed(edge) and edge_id not in seen_edges:
                seen_edges.add(edge_id)
                candidates.append(edge_id)
            if depth >= max_depth:
                continue
            try:
                neighbor_nodes = [edge.getFromNode(), edge.getToNode()]
            except Exception:
                neighbor_nodes = []
            for neighbor in neighbor_nodes:
                enqueue_node(neighbor, depth + 1)

    return candidates


def _vehicle_route_path(net: Any, from_edge_id: str, to_edge_id: str) -> list[str]:
    from_edge = _safe_net_edge(net, from_edge_id)
    to_edge = _safe_net_edge(net, to_edge_id)
    if from_edge is None or to_edge is None:
        return []
    if not _passenger_depart_allowed(from_edge) or not _passenger_depart_allowed(to_edge):
        return []
    try:
        path_edges, _cost = net.getShortestPath(from_edge, to_edge, vClass="passenger", withInternal=False)
    except Exception:
        return []
    if not path_edges:
        return []
    return [str(edge.getID()) for edge in path_edges]


def _passenger_lane_count_for_route(net: Any, route_edges_text: str) -> int:
    count = 0
    seen: set[str] = set()
    for edge_id in str(route_edges_text or "").split():
        edge = _safe_net_edge(net, edge_id)
        if edge is None:
            continue
        normalized_edge_id = str(edge.getID())
        if normalized_edge_id in seen:
            continue
        seen.add(normalized_edge_id)
        try:
            lanes = list(edge.getLanes())
        except Exception:
            lanes = []
        count += sum(1 for lane in lanes if lane_allows(lane, "passenger"))
    return count


def _vehicle_route_diversity_metrics(route_file: Path | None) -> dict[str, float | int]:
    if route_file is None or not route_file.exists():
        return {
            "vehicle_route_count": 0,
            "unique_vehicle_route_count": 0,
            "duplicate_factor": 0.0,
            "unique_vehicle_route_ratio": 0.0,
            "used_vehicle_edges": 0,
        }
    try:
        root = ET.parse(route_file).getroot()
    except Exception:
        return {
            "vehicle_route_count": 0,
            "unique_vehicle_route_count": 0,
            "duplicate_factor": 0.0,
            "unique_vehicle_route_ratio": 0.0,
            "used_vehicle_edges": 0,
        }
    route_defs = {
        str(route.attrib.get("id", "")): str(route.attrib.get("edges", "")).strip()
        for route in root.findall("route")
    }
    routes: list[str] = []
    used_edges: set[str] = set()
    for vehicle in root.findall("vehicle"):
        route_elem = vehicle.find("route")
        if route_elem is not None:
            edges = str(route_elem.attrib.get("edges", "")).strip()
        else:
            edges = route_defs.get(str(vehicle.attrib.get("route", "")), "")
        if not edges:
            continue
        routes.append(edges)
        used_edges.update(part for part in edges.split() if part)
    route_count = len(routes)
    unique_count = len(set(routes))
    return {
        "vehicle_route_count": int(route_count),
        "unique_vehicle_route_count": int(unique_count),
        "duplicate_factor": float(route_count / max(unique_count, 1)) if route_count else 0.0,
        "unique_vehicle_route_ratio": float(unique_count / max(route_count, 1)) if route_count else 0.0,
        "used_vehicle_edges": int(len(used_edges)),
    }


def _resolve_vehicle_route(
    net: Any,
    row: Any,
    seed: int,
    vehicle_index: int,
) -> dict[str, Any]:
    vehicle_id = f"veh_{vehicle_index}_{getattr(row, 'crosswalk_id')}"
    original_depart = str(getattr(row, "route_from_edge", "") or "")
    original_arrive = str(getattr(row, "route_to_edge", "") or "")
    crossing_edge = str(getattr(row, "crossing_edge_id", "") or getattr(row, "crossing_id", "") or "")

    depart_edge = _safe_net_edge(net, original_depart)
    depart_exists = depart_edge is not None
    passenger_allowed = _passenger_depart_allowed(depart_edge)

    depart_candidates = _nearby_passenger_edges(net, [original_depart, crossing_edge, original_arrive])
    arrive_candidates = _nearby_passenger_edges(net, [original_arrive, crossing_edge, original_depart])
    if not arrive_candidates:
        arrive_candidates = list(depart_candidates)

    chosen_depart = ""
    chosen_arrive = ""
    chosen_path: list[str] = []
    reason = ""

    for depart_candidate in depart_candidates:
        for arrive_candidate in arrive_candidates:
            if depart_candidate == arrive_candidate:
                continue
            path_ids = _vehicle_route_path(net, depart_candidate, arrive_candidate)
            if path_ids:
                chosen_depart = depart_candidate
                chosen_arrive = arrive_candidate
                chosen_path = path_ids
                reason = (
                    "original_depart_valid"
                    if depart_candidate == original_depart and arrive_candidate == original_arrive
                    else "replaced_with_nearby_passenger_path"
                )
                break
        if chosen_path:
            break

    if not chosen_path:
        for depart_candidate in depart_candidates:
            path_ids = _vehicle_route_path(net, depart_candidate, depart_candidate)
            if path_ids:
                chosen_depart = depart_candidate
                chosen_arrive = depart_candidate
                chosen_path = path_ids
                reason = "single_edge_passenger_fallback"
                break

    action = "generate_vehicle" if chosen_path else "skip_vehicle"
    if not reason:
        reason = "no_passenger_allowed_route_near_candidate"

    return {
        "crosswalk_id": str(getattr(row, "crosswalk_id")),
        "seed": int(seed),
        "vehicle_id": vehicle_id,
        "depart_edge": original_depart,
        "depart_edge_exists": bool(depart_exists),
        "passenger_allowed": bool(passenger_allowed),
        "replacement_edge": chosen_depart,
        "action": action,
        "reason": reason,
        "vehicle_route_from_edge": chosen_depart,
        "vehicle_route_to_edge": chosen_arrive,
        "vehicle_route_edges": " ".join(chosen_path),
    }


def _prepare_vehicle_route_plan(
    candidate_df: pd.DataFrame,
    net_file: Path,
    seed: int,
    out_dir: Path,
    include_vehicles: bool,
) -> pd.DataFrame:
    out = candidate_df.copy()
    out["vehicle_route_from_edge"] = ""
    out["vehicle_route_to_edge"] = ""
    out["vehicle_route_edges"] = ""
    out["vehicle_generation_action"] = "skip_vehicle"
    out["vehicle_generation_reason"] = "include_vehicles_disabled"

    audit_columns = [
        "crosswalk_id",
        "seed",
        "vehicle_id",
        "depart_edge",
        "depart_edge_exists",
        "passenger_allowed",
        "replacement_edge",
        "action",
        "reason",
    ]
    if not include_vehicles:
        pd.DataFrame(columns=audit_columns).to_csv(out_dir / "sampled10_vehicle_depart_edge_audit.csv", index=False)
        return out

    net = read_net(net_file)
    audit_rows: list[dict[str, Any]] = []
    for pos, row in enumerate(out.itertuples(index=False), start=1):
        plan = _resolve_vehicle_route(net, row, seed, pos)
        row_index = out.index[pos - 1]
        audit_rows.append({column: plan[column] for column in audit_columns})
        out.at[row_index, "vehicle_route_from_edge"] = str(plan["vehicle_route_from_edge"])
        out.at[row_index, "vehicle_route_to_edge"] = str(plan["vehicle_route_to_edge"])
        out.at[row_index, "vehicle_route_edges"] = str(plan["vehicle_route_edges"])
        out.at[row_index, "vehicle_generation_action"] = str(plan["action"])
        out.at[row_index, "vehicle_generation_reason"] = str(plan["reason"])

    pd.DataFrame(audit_rows, columns=audit_columns).to_csv(
        out_dir / "sampled10_vehicle_depart_edge_audit.csv",
        index=False,
    )
    return out


def _normalize_road_alias(road_id: str) -> set[str]:
    road = _normalize_id_text(road_id)
    if not road:
        return set()
    aliases = {road}
    if road.startswith(":") and "_" in road:
        head, tail = road.rsplit("_", 1)
        if tail.isdigit():
            aliases.add(head)
    return {alias for alias in aliases if alias}


def _route_path_text(path_ids: list[str]) -> str:
    return "|".join(path_ids)


def _edge_id_aliases(edge_id: str) -> set[str]:
    return _normalize_road_alias(edge_id)


def _build_local_scope(
    net: Any,
    crossing_edge_id: str,
    radius_m: float,
    conflict_radius_m: float = 35.0,
) -> dict[str, Any]:
    crossing_edge = _safe_net_edge(net, crossing_edge_id)
    if crossing_edge is None:
        return {
            "crossing_xy": (0.0, 0.0),
            "local_lane_ids": set(),
            "local_edge_ids": set(),
            "local_edge_aliases": set(),
            "conflict_edge_ids": set(),
            "conflict_edge_aliases": set(),
            "bbox": None,
        }
    crossing_xy = edge_center(crossing_edge)
    local_lane_ids: set[str] = set()
    local_edge_ids: set[str] = set()
    conflict_edge_ids: set[str] = set()
    for edge in net.getEdges():
        if str(edge_function(edge)).lower() != "normal":
            continue
        if not _passenger_depart_allowed(edge):
            continue
        edge_id = str(edge.getID())
        distance_m = distance_to_edge_shape(edge, crossing_xy)
        if distance_m <= float(radius_m):
            local_edge_ids.add(edge_id)
            for lane in edge.getLanes():
                if lane_allows(lane, "passenger"):
                    local_lane_ids.add(str(lane.getID()))
        if distance_m <= float(conflict_radius_m):
            conflict_edge_ids.add(edge_id)
    bbox = (
        float(crossing_xy[0] - radius_m),
        float(crossing_xy[1] - radius_m),
        float(crossing_xy[0] + radius_m),
        float(crossing_xy[1] + radius_m),
    )
    local_edge_aliases: set[str] = set()
    conflict_edge_aliases: set[str] = set()
    for edge_id in local_edge_ids:
        local_edge_aliases.update(_edge_id_aliases(edge_id))
    for edge_id in conflict_edge_ids:
        conflict_edge_aliases.update(_edge_id_aliases(edge_id))
    return {
        "crossing_xy": crossing_xy,
        "local_lane_ids": local_lane_ids,
        "local_edge_ids": local_edge_ids,
        "local_edge_aliases": local_edge_aliases,
        "conflict_edge_ids": conflict_edge_ids,
        "conflict_edge_aliases": conflict_edge_aliases,
        "bbox": bbox,
    }


def _network_passenger_edge_ids(net: Any) -> set[str]:
    edge_ids: set[str] = set()
    for edge in net.getEdges():
        if str(edge_function(edge)).lower() != "normal":
            continue
        if _passenger_depart_allowed(edge):
            edge_ids.add(str(edge.getID()))
    return edge_ids


def _write_tripinfo_ready_sumocfg(
    cfg_path: Path,
    net_file: Path,
    ped_file: Path,
    veh_file: Path | None,
    duration: int,
    step_length: float,
    out_dir: Path,
    scenario: str,
    enable_ssm: bool = True,
) -> Path:
    route_files = [str(ped_file)]
    if veh_file is not None:
        route_files.append(str(veh_file))
    tripinfo_path = out_dir / f"phase6_smoke_{scenario}_tripinfo.xml"
    statistics_path = out_dir / f"phase6_smoke_{scenario}_statistics.xml"
    collision_path = out_dir / f"phase6_smoke_{scenario}_collisions.xml"
    ssm_path = out_dir / f"phase6_smoke_{scenario}_ssm.xml"
    ssm_content = (
        f'''
  <ssm_device>
    <device.ssm.probability value="1"/>
    <device.ssm.deterministic value="true"/>
    <device.ssm.measures value="PET"/>
    <device.ssm.extratime value="5"/>
    <device.ssm.range value="50"/>
    <device.ssm.write-na value="true"/>
    <device.ssm.file value="{ssm_path}"/>
  </ssm_device>'''
        if enable_ssm
        else ""
    )
    content = f'''<?xml version="1.0" encoding="UTF-8"?>
<configuration>
  <input>
    <net-file value="{net_file}"/>
    <route-files value="{','.join(route_files)}"/>
  </input>
  <time>
    <begin value="0"/>
    <end value="{duration}"/>
    <step-length value="{step_length}"/>
  </time>
  <output>
    <tripinfo-output value="{tripinfo_path}"/>
    <statistic-output value="{statistics_path}"/>
    <collision-output value="{collision_path}"/>
  </output>
  <processing>
    <collision.action value="warn"/>
    <intermodal-collision.action value="warn"/>
    <time-to-teleport value="-1"/>
  </processing>
  <report>
    <no-step-log value="true"/>
    <no-warnings value="false"/>
  </report>
{ssm_content}
</configuration>
'''
    cfg_path.write_text(content, encoding="utf-8")
    return cfg_path


def _parse_tripinfo_metrics(path: Path) -> dict[str, float]:
    metrics = {
        "network_mean_travel_time": float("nan"),
        "network_mean_time_loss": float("nan"),
        "network_avg_delay_sec": float("nan"),
    }
    if not path.exists():
        return metrics
    durations: list[float] = []
    time_losses: list[float] = []
    waiting_times: list[float] = []
    try:
        root = ET.parse(path).getroot()
    except Exception:
        return metrics
    for tripinfo in root.findall(".//tripinfo"):
        try:
            durations.append(float(tripinfo.attrib.get("duration", "nan")))
        except Exception:
            pass
        try:
            time_losses.append(float(tripinfo.attrib.get("timeLoss", "nan")))
        except Exception:
            pass
        try:
            waiting_times.append(float(tripinfo.attrib.get("waitingTime", "nan")))
        except Exception:
            pass
    metrics["network_mean_travel_time"] = _mean(durations)
    metrics["network_mean_time_loss"] = _mean(time_losses)
    metrics["network_avg_delay_sec"] = _mean(waiting_times)
    return metrics


def _parse_collision_count(path: Path) -> int:
    if not path.exists():
        return 0
    try:
        root = ET.parse(path).getroot()
    except Exception:
        return 0
    return int(len(root.findall(".//collision")))


def _extract_ssm_attr(attrs: dict[str, str], *names: str) -> str:
    lowered = {str(key).lower(): str(value) for key, value in attrs.items()}
    for name in names:
        value = lowered.get(name.lower())
        if value is not None:
            return value
    return ""


def _parse_ssm_pet_rows(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not path.exists():
        return rows
    try:
        root = ET.parse(path).getroot()
    except Exception:
        return rows
    for elem in root.iter():
        attrs = {str(k): str(v) for k, v in elem.attrib.items()}
        if not attrs:
            continue
        pet_text = _extract_ssm_attr(attrs, "PET", "pet", "PPET", "ppet")
        if not pet_text or pet_text in {"NA", "nan", "None"}:
            continue
        try:
            pet_value = float(pet_text)
        except Exception:
            continue
        ped_id = _extract_ssm_attr(
            attrs,
            "pedestrian",
            "pedestrian_id",
            "person",
            "person_id",
            "foe",
            "foeID",
            "foe_id",
        )
        vehicle_id = _extract_ssm_attr(
            attrs,
            "vehicle",
            "vehicle_id",
            "ego",
            "egoID",
            "ego_id",
        )
        rows.append(
            {
                "pet": float(pet_value),
                "pedestrian_id": _normalize_id_text(ped_id),
                "vehicle_id": _normalize_id_text(vehicle_id),
                "edge_id": _normalize_id_text(_extract_ssm_attr(attrs, "edge", "edge_id", "lane", "lane_id")),
                "time_s": _extract_ssm_attr(attrs, "time", "time_s", "begin", "start"),
            }
        )
    return rows


def _interval_gap(a_start: float, a_end: float, b_start: float, b_end: float) -> float | None:
    if a_end < b_start:
        return float(b_start - a_end)
    if b_end < a_start:
        return float(a_start - b_end)
    if a_start <= b_end and b_start <= a_end:
        return 0.0
    return None


def _summarize_pet_values(
    pet_values: list[float],
    pedestrian_crossing_count: int,
    conflict_edge_count: int,
    *,
    pet_source: str,
    unavailable_reason: str = "",
) -> dict[str, Any]:
    values = [float(v) for v in pet_values if pd.notna(v)]
    very_risky = int(sum(1 for value in values if value <= 1.5))
    risky = int(sum(1 for value in values if 1.5 < value <= 3.0))
    safe = int(sum(1 for value in values if value > 3.0))
    low_pet = int(very_risky + risky)
    pet_event_count = int(len(values))
    pet_coverage_ratio = round(pet_event_count / pedestrian_crossing_count, 3) if pedestrian_crossing_count > 0 else 0.0
    full_coverage = pedestrian_crossing_count > 0 and pet_event_count == pedestrian_crossing_count
    if pedestrian_crossing_count <= 0 and not unavailable_reason:
        unavailable_reason = "no_pedestrian_crossings_observed"
    elif pet_event_count <= 0 and not unavailable_reason:
        unavailable_reason = f"no_vehicle_person_pet_from_{pet_source}"
    elif not full_coverage and not unavailable_reason:
        unavailable_reason = f"partial_pet_coverage_from_{pet_source}"
    return {
        "pet_source": pet_source,
        "pet_event_count": pet_event_count,
        "very_risky_crossing_count": very_risky,
        "risky_crossing_count": risky,
        "safe_crossing_count": safe,
        "low_pet_event_count": low_pet,
        "low_pet_per_100_crossings": (
            float(low_pet * 100.0 / pedestrian_crossing_count) if pedestrian_crossing_count > 0 else float("nan")
        ),
        "low_pet_per_100_conflict_candidates": (
            float(low_pet * 100.0 / max(conflict_edge_count, 1))
        ),
        "pet_min": min(values) if values else float("nan"),
        "pet_p10": _percentile(values, 10.0),
        "pet_mean": _mean(values),
        "pet_available": bool(full_coverage),
        "pet_coverage_ratio": pet_coverage_ratio,
        "pet_unavailable_reason": "" if full_coverage else unavailable_reason,
        "accident_risk_estimate": (
            float((very_risky * 1.0 + risky * 0.5) / pedestrian_crossing_count)
            if pedestrian_crossing_count > 0
            else float("nan")
        ),
    }


def _summarize_local_watcher_pet(
    ped_intervals: list[dict[str, Any]],
    veh_intervals: list[dict[str, Any]],
    pedestrian_crossing_count: int,
    conflict_edge_count: int,
) -> dict[str, Any]:
    pet_values: list[float] = []
    for ped in ped_intervals:
        ped_start = float(ped.get("enter_time", 0.0))
        ped_end = float(ped.get("exit_time", ped_start))
        min_pet: float | None = None
        for veh in veh_intervals:
            gap = _interval_gap(
                ped_start,
                ped_end,
                float(veh.get("enter_time", 0.0)),
                float(veh.get("exit_time", 0.0)),
            )
            if gap is None:
                continue
            if min_pet is None or gap < min_pet:
                min_pet = float(gap)
        if min_pet is not None:
            pet_values.append(float(min_pet))
    return _summarize_pet_values(
        pet_values,
        pedestrian_crossing_count,
        conflict_edge_count,
        pet_source="local_watcher",
    )


def _compute_pet_event_audit_rows(
    crosswalk_id: str,
    scenario: str,
    ped_intervals: list[dict[str, Any]],
    veh_intervals: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for ped in ped_intervals:
        ped_id  = str(ped.get("pedestrian_id", ""))
        p_enter = float(ped.get("enter_time", 0.0))
        p_exit  = float(ped.get("exit_time", p_enter))
        for veh in veh_intervals:
            v_id    = str(veh.get("vehicle_id", ""))
            v_enter = float(veh.get("enter_time", 0.0))
            v_exit  = float(veh.get("exit_time", 0.0))
            gap = _interval_gap(p_enter, p_exit, v_enter, v_exit)
            if gap is None:
                pet_case  = "undefined"
                pet_value = float("nan")
            elif gap == 0.0:
                pet_case  = "overlap"
                pet_value = 0.0
            elif p_exit <= v_enter:
                pet_case  = "ped_before_veh"
                pet_value = gap
            else:
                pet_case  = "veh_before_ped"
                pet_value = gap
            rows.append({
                "crosswalk_id": crosswalk_id,
                "scenario":     scenario,
                "ped_id":       ped_id,
                "vehicle_id":   v_id,
                "ped_enter":    round(p_enter, 3),
                "ped_exit":     round(p_exit, 3),
                "veh_enter":    round(v_enter, 3),
                "veh_exit":     round(v_exit, 3),
                "pet_value":    round(pet_value, 4) if pet_value == pet_value else float("nan"),
                "pet_case":     pet_case,
            })
    return rows


def _compute_pet_pair_summary(
    ped_intervals: list[dict[str, Any]],
    veh_intervals: list[dict[str, Any]],
) -> dict[str, Any]:
    """
    All (ped, veh) pair 레벨 분포 지표.
    per-ped minimum PET summary의 대체가 아닌 diagnostic 보조 지표.
    overlap 포화 상황에서 extension 효과를 pair 분포 shift로 확인.
    """
    pet_vals: list[float] = []
    nz_vals:  list[float] = []
    ov = pbv = vbp = 0

    for ped in ped_intervals:
        p_enter = float(ped.get("enter_time", 0.0))
        p_exit  = float(ped.get("exit_time", p_enter))
        for veh in veh_intervals:
            v_enter = float(veh.get("enter_time", 0.0))
            v_exit  = float(veh.get("exit_time", 0.0))
            gap = _interval_gap(p_enter, p_exit, v_enter, v_exit)
            if gap is None:
                continue
            pet_vals.append(gap)
            if gap == 0.0:
                ov += 1
            elif p_exit <= v_enter:
                pbv += 1
                nz_vals.append(gap)
            else:
                vbp += 1
                nz_vals.append(gap)

    total = len(pet_vals)

    def _med(lst: list[float]) -> float:
        if not lst:
            return float("nan")
        s = sorted(lst)
        n = len(s)
        return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2.0

    def _avg(lst: list[float]) -> float:
        return sum(lst) / len(lst) if lst else float("nan")

    return {
        "pair_total_count":          total,
        "pair_overlap_count":        ov,
        "pair_ped_before_veh_count": pbv,
        "pair_veh_before_ped_count": vbp,
        "pair_overlap_rate":         round(ov  / total, 4) if total else float("nan"),
        "pair_ped_before_veh_rate":  round(pbv / total, 4) if total else float("nan"),
        "pair_veh_before_ped_rate":  round(vbp / total, 4) if total else float("nan"),
        "pair_pet_mean":             round(_avg(pet_vals), 4),
        "pair_pet_median":           round(_med(pet_vals), 4),
        "pair_nonzero_pet_mean":     round(_avg(nz_vals), 4),
        "pair_nonzero_pet_median":   round(_med(nz_vals), 4),
    }


def _route_pair_from_crossing_edge(net: Any, crossing_edge_id: str) -> list[tuple[str, str, str]]:
    crossing_edge = net.getEdge(str(crossing_edge_id))
    base_route = pedestrian_route_from_crossing(crossing_edge)
    candidates: list[tuple[str, str, str]] = [
        (str(base_route["from_edge"]), str(base_route["to_edge"]), "crossing_walkarea_forward"),
        (str(base_route["to_edge"]), str(base_route["from_edge"]), "crossing_walkarea_reverse"),
    ]

    incoming_walkareas = [
        edge for edge in crossing_edge.getIncoming().keys() if getattr(edge, "getFunction", lambda: "")() == "walkingarea"
    ]
    outgoing_walkareas = [
        edge for edge in crossing_edge.getOutgoing().keys() if getattr(edge, "getFunction", lambda: "")() == "walkingarea"
    ]
    for in_wa in incoming_walkareas:
        in_normals = [edge for edge in in_wa.getIncoming().keys() if getattr(edge, "getFunction", lambda: "")() == "normal" and edge.allows("pedestrian")]
        for out_wa in outgoing_walkareas:
            out_normals = [edge for edge in out_wa.getOutgoing().keys() if getattr(edge, "getFunction", lambda: "")() == "normal" and edge.allows("pedestrian")]
            for from_edge in in_normals:
                for to_edge in out_normals:
                    candidates.append((from_edge.getID(), to_edge.getID(), "crossing_walkingarea_bruteforce"))
                    candidates.append((to_edge.getID(), from_edge.getID(), "crossing_walkingarea_bruteforce_reverse"))
    return candidates


def _validate_route_pair(net: Any, crossing_edge_id: str, from_edge_id: str, to_edge_id: str) -> tuple[bool, str, list[str]]:
    from_edge = None
    to_edge = None
    try:
        from_edge = net.getEdge(str(from_edge_id))
        to_edge = net.getEdge(str(to_edge_id))
    except Exception:
        return False, "missing_route_edge", []
    if from_edge is None or to_edge is None:
        return False, "missing_route_edge", []
    if not from_edge.allows("pedestrian"):
        return False, "from_edge_disallows_pedestrian", []
    if not to_edge.allows("pedestrian"):
        return False, "to_edge_disallows_pedestrian", []
    try:
        path_edges, _path_cost = net.getShortestPath(from_edge, to_edge, vClass="pedestrian", withInternal=True)
    except Exception as exc:
        return False, f"shortest_path_exception:{exc}", []
    if not path_edges:
        return False, "no_pedestrian_path", []
    path_ids = [edge.getID() for edge in path_edges]
    if crossing_edge_id and crossing_edge_id not in path_ids:
        return False, "path_missing_crossing_edge", path_ids
    if not any(getattr(edge, "getFunction", lambda: "")() == "walkingarea" for edge in path_edges):
        return False, "path_missing_walkingarea", path_ids
    return True, "ok", path_ids


def _route_match_details(
    road_id: str,
    lane_id: str,
    crossing_edge_set: set[str],
    route_path_edges: set[str],
    tls_id: str,
) -> tuple[bool, str]:
    road_aliases = _normalize_road_alias(road_id)
    lane_aliases = _normalize_road_alias(lane_id)
    if road_aliases & route_path_edges:
        return True, "road_in_route_path"
    if lane_aliases & route_path_edges:
        return True, "lane_in_route_path"
    if road_aliases & crossing_edge_set:
        return True, "road_in_crossing_set"
    if lane_aliases & crossing_edge_set:
        return True, "lane_in_crossing_set"
    if any(alias.startswith(f":{tls_id}_") for alias in road_aliases | lane_aliases):
        return True, "internal_tls_edge"
    return False, ""


def _count_green_links(state: str, ped_link_index: int) -> int:
    return sum(1 for idx, ch in enumerate(state) if idx != ped_link_index and ch in {"G", "g"})


def _extension_skip_reason(
    scenario: str,
    tls_id: str,
    state: str,
    ped_link_index: int,
    ped_link_state: str,
    remaining: float,
    ped_near: int,
    already_extended: bool,
) -> str:
    if not tls_id:
        return "missing_tls"
    if ped_link_index < 0 or ped_link_index >= len(state):
        return "missing_ped_link_index"
    if scenario == "baseline":
        return "scenario_baseline"
    if ped_link_state not in {"G", "g"}:
        return "ped_link_not_green"
    if remaining > 12.0:
        return "outside_extension_window"
    if ped_near <= 0:
        return "no_ped_near"
    if already_extended:
        return "already_extended_this_phase"
    return ""


def _phase_aligned_depart_plan(
    net_file: Path,
    candidate_df: pd.DataFrame,
    ped_repeat_count: int | None = None,
    ped_repeat_spacing_sec: float | None = None,
    ped_depart_offset_sec: float | None = None,
) -> pd.DataFrame:
    root = ET.parse(net_file).getroot()
    tl_map = {
        str(tl.attrib.get("id", "") or ""): tl
        for tl in root.findall("tlLogic")
        if str(tl.attrib.get("id", "") or "")
    }
    rows: list[dict[str, Any]] = []

    for row in candidate_df.itertuples(index=False):
        tls_id = _normalize_id_text(getattr(row, "tls_id", "") or getattr(row, "tls_id_used", ""))
        ped_link_index = int(getattr(row, "ped_link_index", 0) or 0)
        repeat_count = int(ped_repeat_count if ped_repeat_count is not None else getattr(row, "ped_repeat_count", 1) or 1)
        repeat_spacing = float(ped_repeat_spacing_sec if ped_repeat_spacing_sec is not None else getattr(row, "ped_repeat_spacing_sec", 1.5) or 1.5)

        depart_time = float(ped_depart_offset_sec if ped_depart_offset_sec is not None else getattr(row, "ped_depart_offset_sec", 0.0) or 0.0)
        selected_idx: int | None = None
        selected_state = ""
        cycle_duration = ""
        first_green_start_time = ""
        depart_strategy = "explicit_offset"
        reason = "ped_depart_offset_sec"

        tl_elem = tl_map.get(tls_id)
        if tl_elem is not None:
            offset = float(tl_elem.attrib.get("offset", "0") or 0.0)
            phases = list(tl_elem.findall("phase"))
            durations = [float(phase.attrib.get("duration", "0") or 0.0) for phase in phases]
            states = [str(phase.attrib.get("state", "") or "") for phase in phases]
            cycle_duration_val = sum(durations)
            cycle_duration = round(cycle_duration_val, 3)
            phase_start = offset if cycle_duration_val > 0 else 0.0
            if cycle_duration_val > 0:
                for idx, (duration, state) in enumerate(zip(durations, states)):
                    link_state = state[ped_link_index] if 0 <= ped_link_index < len(state) else ""
                    ped_green = link_state in {"G", "g"}
                    ped_only = ped_green and _count_green_links(state, ped_link_index) == 0
                    if ped_only:
                        selected_idx = idx
                        selected_state = state
                        first_green_start_time = round(phase_start, 3)
                        depart_time = max(0.0, phase_start - 1.5)
                        depart_strategy = "phase_aligned_green_start_minus_1p5s"
                        reason = "pedestrian_only_phase_match"
                        break
                    phase_start += duration

        rows.append(
            {
                "crosswalk_id": str(row.crosswalk_id),
                "tls_id": tls_id,
                "ped_link_index": ped_link_index,
                "selected_ped_green_phase_index": selected_idx if selected_idx is not None else "",
                "selected_ped_green_phase_state": selected_state,
                "cycle_duration": cycle_duration,
                "first_green_start_time": first_green_start_time,
                "depart_time": round(depart_time, 3),
                "depart_strategy": depart_strategy,
                "ped_repeat_count": repeat_count,
                "ped_repeat_spacing_sec": repeat_spacing,
                "reason": reason,
            }
        )

    return pd.DataFrame(rows)


def _safe_get(row: Any, col: str, default: Any = None) -> Any:
    if row is None:
        return default
    try:
        value = row[col]
    except Exception:
        return default
    if pd.isna(value):
        return default
    return value


def _write_compare_warning(compare_dir: Path, scenario: str, reason: str, details: dict[str, Any]) -> None:
    compare_dir.mkdir(parents=True, exist_ok=True)
    warning = {
        "scenario": scenario,
        "reason": reason,
        "details": details,
    }
    (compare_dir / "phase6_smoke_compare_warning.json").write_text(json.dumps(warning, ensure_ascii=False, indent=2), encoding="utf-8")
    (compare_dir / "phase6_smoke_compare_warning.md").write_text(
        "\n".join(
            [
                "# phase 6 smoke compare warning",
                "",
                f"- scenario: {scenario}",
                f"- reason: {reason}",
                "",
                "## details",
                "",
                *[f"- {k}: {v}" for k, v in details.items()],
            ]
        )
        + "\n",
        encoding="utf-8",
    )


def _build_candidate_table(
    candidate_csv: Path,
    net_file: Path | None,
    crossing_inventory: Path | None = None,
    output_dir: Path | None = None,
    factory_table: Path | None = FACTORY_TABLE,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    raw = _load_csv(candidate_csv)
    factory = _load_csv_optional(factory_table)
    inventory_df = _load_csv_optional(crossing_inventory)
    inventory_used = inventory_df is not None and not inventory_df.empty
    factory_table_used = factory is not None and not factory.empty
    factory_table_missing = not factory_table_used
    fallback_used = factory_table_missing
    fallback_reasons: list[str] = ["factory_table_missing"] if factory_table_missing else []
    net = read_net(net_file) if net_file is not None else None
    xml_root = _load_xml_root(net_file) if net_file is not None else None
    route_diag_rows: list[dict[str, Any]] = []

    rows: list[dict[str, Any]] = []
    for _, r in raw.iterrows():
        cid = _normalize_id_text(r.get("crosswalk_id"))
        if not cid:
            raise ValueError("candidate csv missing crosswalk_id")

        factory_row = None
        if factory is not None and not factory.empty:
            frow_df = factory[factory["crosswalk_id"].astype(str) == cid]
            factory_row = frow_df.iloc[0] if not frow_df.empty else None

        source_crosswalk_id = _normalize_id_text(_first_present(r, ["source_crosswalk_id", "crosswalk_id"])) or cid
        canonical_crosswalk_id = _normalize_id_text(_first_present(r, ["canonical_crosswalk_id"])) or cid
        nearest_junction_id = _normalize_id_text(_first_present(r, ["nearest_junction_id"]))
        tls_id = _normalize_id_text(_first_present(r, ["tls_id", "tls_id_used", "nearest_tls_id"]))
        crossing_id = _normalize_id_text(_first_present(r, ["crossing_id", "crossing_edge", "crossing_edge_id"]))
        ped_indices = _parse_int_list(_first_present(r, ["ped_link_indices", "inferred_ped_link_indices"]))
        ped_depart_offset_sec = float(_first_present(r, ["ped_depart_offset_sec"]) or 0)
        ped_repeat_count = int(float(_first_present(r, ["ped_repeat_count"]) or 1))
        ped_repeat_spacing_sec = float(_first_present(r, ["ped_repeat_spacing_sec"]) or 1.5)

        route_from_edge = _normalize_id_text(_first_present(r, ["route_from_edge"]))
        route_to_edge = _normalize_id_text(_first_present(r, ["route_to_edge"]))
        incident_edges = _parse_edges(_first_present(r, ["incident_road_edges"]))
        nearest_road_edge = _normalize_id_text(_first_present(r, ["nearest_road_edge_id"]))

        if factory_row is not None:
            nearest_junction_id = nearest_junction_id or _normalize_id_text(factory_row.get("nearest_junction_id"))
            tls_id = tls_id or nearest_junction_id or _normalize_id_text(factory_row.get("nearest_tls_id"))
            crossing_id = crossing_id or _normalize_id_text(_first_present(factory_row, ["crossing_id", "crossing_edge", "crossing_edge_id"]))
            route_from_edge = route_from_edge or _normalize_id_text(_first_present(factory_row, ["route_from_edge", "nearest_road_edge_id"]))
            route_to_edge = route_to_edge or _normalize_id_text(_first_present(factory_row, ["route_to_edge"]))
            nearest_road_edge = nearest_road_edge or _normalize_id_text(factory_row.get("nearest_road_edge_id"))
            if not incident_edges:
                incident_edges = _parse_edges(_first_present(factory_row, ["incident_road_edges"]))
            if not ped_indices:
                ped_indices = _parse_int_list(_first_present(factory_row, ["ped_link_indices", "inferred_ped_link_indices"]))

        if net is not None and xml_root is not None and (not crossing_id or not route_from_edge or not route_to_edge or not ped_indices):
            audit_tls_id = tls_id or nearest_junction_id
            if audit_tls_id:
                audited_crossing_id, audited_ped_indices, audited_from_edge, audited_to_edge, audited_path_ids, audited_reason = _audit_crossing_edge_from_net(
                    net,
                    net_file,
                    xml_root,
                    audit_tls_id,
                    ped_indices,
                    route_from_edge,
                    route_to_edge,
                )
                if audited_crossing_id:
                    fallback_used = True
                    fallback_reasons.append("net_file_audit_used")
                    if not crossing_id:
                        crossing_id = audited_crossing_id
                    if not ped_indices and audited_ped_indices:
                        ped_indices = audited_ped_indices
                    if not route_from_edge and audited_from_edge:
                        route_from_edge = audited_from_edge
                    if not route_to_edge and audited_to_edge:
                        route_to_edge = audited_to_edge
                    if audited_path_ids:
                        route_diag_rows.append(
                            {
                                "crosswalk_id": cid,
                                "crossing_edge_id": crossing_id or audited_crossing_id,
                                "candidate_from_edge": audited_from_edge,
                                "candidate_to_edge": audited_to_edge,
                                "selected": True,
                                "valid": True,
                                "contains_crossing_edge": bool(crossing_id or audited_crossing_id),
                                "generated_route_edges": _route_path_text(audited_path_ids),
                                "reason": f"net_audit:{audited_reason}",
                            }
                        )
                else:
                    fallback_used = True
                    fallback_reasons.append(audited_reason)
            else:
                fallback_used = True
                fallback_reasons.append("net_file_audit_skipped_missing_tls_id")

        if net_file is not None:
            route_candidates: list[tuple[str, str, str]] = []
            if crossing_id:
                try:
                    route_candidates.extend(_route_pair_from_crossing_edge(net, crossing_id))
                except Exception as exc:
                    route_diag_rows.append(
                        {
                            "crosswalk_id": cid,
                            "crossing_edge_id": crossing_id,
                            "candidate_from_edge": "",
                            "candidate_to_edge": "",
                            "selected": False,
                            "valid": False,
                            "reason": f"crossing_route_discovery_failed:{exc}",
                        }
                    )
            if route_from_edge and route_to_edge:
                route_candidates.append((route_from_edge, route_to_edge, "candidate_csv"))
            if incident_edges:
                factory_from, factory_to = _choose_route_pair(incident_edges, nearest_road_edge)
                route_candidates.append((factory_from, factory_to, "factory_incident_edges"))

            deduped: list[tuple[str, str, str]] = []
            seen_pairs: set[tuple[str, str]] = set()
            for frm, to, method in route_candidates:
                frm_n = _normalize_id_text(frm)
                to_n = _normalize_id_text(to)
                if not frm_n or not to_n:
                    continue
                key = (frm_n, to_n)
                if key in seen_pairs:
                    continue
                seen_pairs.add(key)
                deduped.append((frm_n, to_n, method))

            selected_pair: tuple[str, str, str] | None = None
            selected_path_ids: list[str] = []
            selected_reason = ""
            if crossing_id and net is not None:
                for frm, to, method in deduped:
                    valid, reason, _path_ids = _validate_route_pair(net, crossing_id, frm, to)
                    route_diag_rows.append(
                        {
                            "crosswalk_id": cid,
                            "crossing_edge_id": crossing_id,
                            "candidate_from_edge": frm,
                            "candidate_to_edge": to,
                            "selected": bool(valid and selected_pair is None),
                            "valid": bool(valid),
                            "contains_crossing_edge": bool(crossing_id and crossing_id in _path_ids),
                            "generated_route_edges": _route_path_text(_path_ids),
                            "reason": f"{method}:{reason}",
                        }
                    )
                    if valid:
                        selected_pair = (frm, to, method)
                        selected_path_ids = list(_path_ids)
                        break
            if selected_pair is not None:
                route_from_edge, route_to_edge, _method = selected_pair
            else:
                attempted = [
                    f"{frm}->{to}:{method}"
                    for frm, to, method in deduped
                ]
                raise ValueError(
                    f"no valid pedestrian route pair for {cid}; crossing_edge_id={crossing_id}; attempted={attempted}; "
                    "recommended_action=inspect crossing_edge route connectivity and choose a pair whose shortest path includes the crossing edge"
                )

        if not route_from_edge or not route_to_edge:
            raise ValueError(f"route_from_edge/route_to_edge missing for {cid}")
        if not crossing_id:
            if inventory_df is not None and not inventory_df.empty:
                fallback_used = True
                fallback_reasons.append("crossing_inventory_used_for_crossing_edge")
                crossing_id = _find_crossing_edge(nearest_junction_id or tls_id, route_from_edge, inventory_df)
            if not crossing_id:
                raise ValueError(f"crossing_edge missing for {cid}")
        if not tls_id:
            tls_id = nearest_junction_id
        if not ped_indices:
            if net is not None and tls_id and crossing_id:
                fallback_used = True
                fallback_reasons.append("net_file_audit_used_for_ped_link_indices")
                try:
                    ped_indices = _parse_int_list(
                        pedestrian_link_indices(net_file, tls_id, crossing_id, xml_root=xml_root)
                    )
                except Exception:
                    ped_indices = []
            if not ped_indices and inventory_df is not None and not inventory_df.empty:
                fallback_used = True
                fallback_reasons.append("crossing_inventory_used_for_ped_link_indices")
                if net is not None and tls_id and factory_row is not None:
                    try:
                        ped_indices = _parse_int_list(
                            discover_network_metadata_from_net(
                                net,
                                net_file,
                                float(factory_row.get("lon")),
                                float(factory_row.get("lat")),
                                cw_id=cid,
                            ).get("ped_link_indices")
                        )
                    except Exception:
                        ped_indices = []
            if not ped_indices:
                raise ValueError(f"ped_link_indices missing for {cid}")

        ped_link_index = int(ped_indices[0])
        rows.append(
            {
                "crosswalk_id": cid,
                "source_crosswalk_id": source_crosswalk_id,
                "canonical_crosswalk_id": canonical_crosswalk_id,
                "nearest_junction_id": nearest_junction_id,
                "tls_id": tls_id,
                "tls_id_used": tls_id,
                "crossing_id": crossing_id,
                "crossing_edge_id": crossing_id,
                "ped_link_index": ped_link_index,
                "ped_link_indices": "|".join(str(v) for v in ped_indices),
                "ped_depart_offset_sec": ped_depart_offset_sec,
                "route_from_edge": route_from_edge,
                "route_to_edge": route_to_edge,
                "generated_route_edges": _route_path_text(selected_path_ids),
                "contains_crossing_edge": bool(crossing_id and crossing_id in selected_path_ids),
                "ped_repeat_count": ped_repeat_count,
                "ped_repeat_spacing_sec": ped_repeat_spacing_sec,
                "route_reason": "from_nearest_road_edge_to_other_incident_edge",
                "incident_road_edges": "|".join(incident_edges),
                "nearest_road_edge_id": nearest_road_edge,
                "source_file": _normalize_id_text(r.get("source_file")),
                "batch_network_file": str(Path(net_file).expanduser().resolve()) if net_file else "",
                "final_verdict": _normalize_id_text(r.get("final_verdict")),
                "step_test_ok": _normalize_id_text(r.get("step_test_ok")),
                "controlled_links_count": _normalize_id_text(r.get("controlled_links_count")),
                "crossing_inventory_used": bool(inventory_used),
            }
        )

    out = pd.DataFrame(rows)
    if out["batch_network_file"].nunique() != 1:
        raise ValueError("smoke candidates span multiple net files; split run required")
    fallback_reason_parts = list(dict.fromkeys(fallback_reasons))
    if not inventory_used:
        fallback_reason_parts.append("crossing_inventory_missing")
    fallback_reason = ";".join(fallback_reason_parts)
    diagnostics = pd.DataFrame(
        [
            {
                "candidate_csv": str(candidate_csv),
                "net_file": str(net_file) if net_file else "",
                "factory_table_path": str(factory_table) if factory_table is not None else "",
                "factory_table_used": bool(factory_table_used),
                "factory_table_missing": bool(factory_table_missing),
                "crossing_inventory_used": bool(inventory_used),
                "fallback_used": bool(fallback_used or not inventory_used),
                "fallback_reason": fallback_reason,
                "candidate_rows": int(len(out)),
            }
        ]
    )
    route_diag = pd.DataFrame(route_diag_rows)
    global LAST_ROUTE_PAIR_DIAGNOSTICS
    LAST_ROUTE_PAIR_DIAGNOSTICS = route_diag
    if output_dir is not None:
        output_dir.mkdir(parents=True, exist_ok=True)
        route_diag.to_csv(output_dir / "route_pair_diagnostics.csv", index=False)
    return out, diagnostics


def _write_routes(
    candidate_df: pd.DataFrame,
    out_dir: Path,
    seed: int,
    duration: int,
    include_vehicles: bool,
    global_vehicle_file: "Path | None" = None,
    write_debug: bool = True,
) -> "tuple[Path, Path | None, Path, list[dict[str, Any]], bool]":
    import shutil as _shutil
    rng = random.Random(seed)
    ped_path = out_dir / "demand_pedestrian.rou.xml"
    ped_lines = ['<?xml version="1.0" encoding="UTF-8"?>', '<routes xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">', '    <vType id="pedestrian_type" vClass="pedestrian"/>']
    veh_path = out_dir / "demand_vehicle.rou.xml" if include_vehicles else None
    use_global_veh = include_vehicles and global_vehicle_file is not None and Path(global_vehicle_file).exists()
    veh_lines = (
        ['<?xml version="1.0" encoding="UTF-8"?>', '<routes xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">', '    <vType id="car" vClass="passenger" maxSpeed="15.0" accel="2.6" decel="4.5"/>']
        if include_vehicles and not use_global_veh
        else []
    )
    ped_records: list[dict[str, Any]] = []

    for idx, row in enumerate(candidate_df.itertuples(index=False), start=1):
        repeat_count = max(1, int(getattr(row, "ped_repeat_count", 1) or 1))
        repeat_spacing = float(getattr(row, "ped_repeat_spacing_sec", 1.5) or 1.5)
        base_depart = getattr(row, "ped_depart_time", None)
        has_absolute_depart = base_depart is not None and str(base_depart) not in {"", "nan", "None"}
        for rep_idx in range(repeat_count):
            if has_absolute_depart:
                ped_depart = round(float(base_depart) + (rep_idx * repeat_spacing), 1)
            else:
                ped_depart = round(
                    10.0
                    + (idx - 1) * 40.0
                    + float(getattr(row, "ped_depart_offset_sec", 0.0))
                    + (rep_idx * repeat_spacing)
                    + rng.uniform(-1.0, 1.0),
                    1,
                )
            ped_records.append(
                {
                    "person_id": f"ped_{idx}_{rep_idx}_{row.crosswalk_id}",
                    "crosswalk_id": str(row.crosswalk_id),
                    "depart": max(1.0, ped_depart),
                    "route_from_edge": str(row.route_from_edge),
                    "route_to_edge": str(row.route_to_edge),
                    "crossing_edge_id": str(row.crossing_edge_id),
                    "generated_route_edges": str(getattr(row, "generated_route_edges", "")),
                    "contains_crossing_edge": bool(getattr(row, "contains_crossing_edge", False)),
                }
            )
        if include_vehicles and not use_global_veh:
            vehicle_route_edges = str(getattr(row, "vehicle_route_edges", "") or "").strip()
            vehicle_action = str(getattr(row, "vehicle_generation_action", "") or "")
            if vehicle_action != "generate_vehicle" or not vehicle_route_edges:
                continue
            veh_depart = round(5.0 + (idx - 1) * 40.0 + rng.uniform(-2.0, 2.0), 1)
            veh_lines.append(
                f'    <route id="veh_route_{idx}" edges="{vehicle_route_edges}"/>'
            )
            veh_lines.append(
                f'    <vehicle id="veh_{idx}_{row.crosswalk_id}" type="car" route="veh_route_{idx}" depart="{max(1.0, veh_depart)}"/>'
            )

    ped_records.sort(key=lambda rec: (float(rec["depart"]), str(rec["person_id"])))
    for rec in ped_records:
        ped_lines.append(
            f'    <person id="{rec["person_id"]}" type="pedestrian_type" depart="{rec["depart"]}">'
        )
        ped_lines.append(
            f'        <walk from="{rec["route_from_edge"]}" to="{rec["route_to_edge"]}"/>'
        )
        ped_lines.append("    </person>")
    ped_lines.append("</routes>")
    ped_path.write_text("\n".join(ped_lines) + "\n", encoding="utf-8")
    if include_vehicles and veh_path is not None:
        if use_global_veh:
            _shutil.copy2(global_vehicle_file, veh_path)
        else:
            veh_lines.append("</routes>")
            veh_path.write_text("\n".join(veh_lines) + "\n", encoding="utf-8")
    ped_summary_path = out_dir / "pedestrian_route_order_debug.csv"
    ped_summary_df = pd.DataFrame(
        [
            {
                "sorted_index": idx + 1,
                "person_id": rec["person_id"],
                "crosswalk_id": rec["crosswalk_id"],
                "depart": rec["depart"],
                "route_from_edge": rec["route_from_edge"],
                "route_to_edge": rec["route_to_edge"],
            }
            for idx, rec in enumerate(ped_records)
        ]
    )
    ped_summary_df["depart_sorted_ok"] = ped_summary_df["depart"].is_monotonic_increasing
    route_order_ok = bool(ped_summary_df["depart_sorted_ok"].iloc[0]) if not ped_summary_df.empty else False
    if write_debug:
        ped_summary_df.to_csv(ped_summary_path, index=False)
    return ped_path, veh_path, ped_summary_path, ped_records, route_order_ok


def _write_sumocfg(
    cfg_path: Path,
    net_file: Path,
    ped_file: Path,
    veh_file: Path | None,
    duration: int,
    step_length: float,
    out_dir: Path,
    scenario: str,
    enable_ssm: bool = True,
) -> Path:
    return _write_tripinfo_ready_sumocfg(
        cfg_path,
        net_file,
        ped_file,
        veh_file,
        duration,
        step_length,
        out_dir,
        scenario,
        enable_ssm=enable_ssm,
    )


def _sumo_binary() -> str:
    path = shutil.which("sumo")
    if not path:
        raise RuntimeError("sumo binary not found in PATH")
    return path


def _person_routes_crossing(candidate_df: pd.DataFrame) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for row in candidate_df.itertuples(index=False):
        tls_id = _normalize_id_text(getattr(row, "tls_id_used", "")) or _normalize_id_text(getattr(row, "tls_id", ""))
        out[str(row.crosswalk_id)] = {
            "crossing_id": str(getattr(row, "crossing_id", "")),
            "crossing_edge_id": str(row.crossing_edge_id),
            "route_from_edge": str(row.route_from_edge),
            "route_to_edge": str(row.route_to_edge),
            "generated_route_edges": str(getattr(row, "generated_route_edges", "")),
            "contains_crossing_edge": bool(getattr(row, "contains_crossing_edge", False)),
            "tls_id": tls_id,
            "ped_link_index": int(row.ped_link_index),
            "route_reason": str(row.route_reason),
            "batch_network_file": str(row.batch_network_file),
        }
    return out


def _run_scenario(
    candidate_df: pd.DataFrame,
    net_file: Path,
    scenario: str,
    seed: int,
    duration: int,
    warmup: int,
    step_length: float,
    out_dir: Path,
    include_vehicles: bool,
    extension_sec: float,
    global_vehicle_file: "Path | None" = None,
    output_profile: str = "full",
    enable_ssm: bool = True,
) -> pd.DataFrame:
    if traci is None:
        raise RuntimeError("traci not importable")

    run_name = f"phase6_smoke_{scenario}_seed{seed}"
    run_start_time = datetime.now(timezone.utc).isoformat(timespec="seconds")
    run_start_perf = time.perf_counter()

    light_output = str(output_profile).strip().lower() == "light"

    candidate_df = _prepare_vehicle_route_plan(candidate_df, net_file, seed, out_dir, include_vehicles)
    ped_file, veh_file, ped_summary_path, ped_records, route_order_ok = _write_routes(
        candidate_df, out_dir, seed, duration, include_vehicles,
        global_vehicle_file=global_vehicle_file,
        write_debug=not light_output,
    )
    cfg_path = _write_sumocfg(
        out_dir / f"phase6_smoke_{scenario}.sumocfg",
        net_file,
        ped_file,
        veh_file,
        duration,
        step_length,
        out_dir,
        scenario,
        enable_ssm=bool(enable_ssm) and not light_output,
    )

    cmd = [_sumo_binary(), "-c", str(cfg_path), "--no-step-log", "--collision.action", "warn", "--time-to-teleport", "-1"]
    _sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    _sock.bind(("127.0.0.1", 0))
    traci_port = _sock.getsockname()[1]
    _sock.close()
    net = read_net(net_file)
    all_network_passenger_edges = _network_passenger_edge_ids(net)
    network_bbox = None
    try:
        bbox_xy = net.getBBoxXY()
        network_bbox = (
            float(bbox_xy[0][0]),
            float(bbox_xy[0][1]),
            float(bbox_xy[1][0]),
            float(bbox_xy[1][1]),
        )
    except Exception:
        network_bbox = None
    traci.start(cmd, port=traci_port)

    candidate_meta = _person_routes_crossing(candidate_df)
    route_path_by_cid = {
        str(row.crosswalk_id): set(str(getattr(row, "generated_route_edges", "")).split("|")) - {""}
        for row in candidate_df.itertuples(index=False)
    }
    rows: list[dict[str, Any]] = []
    extension_events: list[dict[str, Any]] = []
    debug_rows: list[dict[str, Any]] = []
    extension_trigger_debug_rows: list[dict[str, Any]] = []
    signal_phase_rows: list[dict[str, Any]] = []
    route_diag_by_person: dict[str, dict[str, Any]] = {}
    generated_vehicle_count = 0
    network_arrived_vehicle_ids: set[str] = set()
    network_departed_vehicle_ids: set[str] = set()
    network_seen_vehicle_edges: set[str] = set()
    network_teleport_count = 0
    surrounding_lane_count = 0
    generated_vehicle_route_file = str(veh_file) if veh_file is not None and veh_file.exists() else ""
    generated_vehicle_count = int(
        (candidate_df.get("vehicle_generation_action", pd.Series(dtype=str)) == "generate_vehicle").sum()
    )
    vehicle_route_diversity = _vehicle_route_diversity_metrics(veh_file)
    try:
        surrounding_lane_count = int(
            sum(
                _passenger_lane_count_for_route(net, str(getattr(row, "vehicle_route_edges", "") or ""))
                for row in candidate_df.itertuples(index=False)
                if str(getattr(row, "vehicle_generation_action", "") or "") == "generate_vehicle"
            )
        )
    except Exception:
        surrounding_lane_count = 0

    # per-candidate tracking
    departure_times: dict[str, float] = {}
    wait_recorded: set[str] = set()
    extended_this_phase: dict[str, bool] = {cid: False for cid in candidate_df["crosswalk_id"].astype(str).tolist()}
    last_phase_for_cid: dict[str, int] = {cid: -1 for cid in candidate_df["crosswalk_id"].astype(str).tolist()}
    ped_presence_steps: dict[str, int] = {cid: 0 for cid in candidate_df["crosswalk_id"].astype(str).tolist()}
    ped_people_seen: dict[str, set[str]] = {cid: set() for cid in candidate_df["crosswalk_id"].astype(str).tolist()}
    expected_repeat_counts: dict[str, int] = {
        str(row.crosswalk_id): int(getattr(row, "ped_repeat_count", 1) or 1)
        for row in candidate_df.itertuples(index=False)
    }
    crossing_roads: dict[str, set[str]] = {}
    candidate_watch: dict[str, dict[str, Any]] = {}
    for row in candidate_df.itertuples(index=False):
        cid = str(row.crosswalk_id)
        crossing_roads[cid] = _candidate_crossing_roads(str(row.nearest_junction_id), str(row.crossing_edge_id))
        scope = _build_local_scope(net, str(row.crossing_edge_id), 500.0)
        candidate_watch[cid] = {
            "local_lane_ids": set(scope["local_lane_ids"]),
            "local_edge_ids": set(scope["local_edge_ids"]),
            "local_edge_aliases": set(scope["local_edge_aliases"]),
            "conflict_edge_ids": set(scope["conflict_edge_ids"]),
            "conflict_edge_aliases": set(scope["conflict_edge_aliases"]),
            "bbox": scope["bbox"],
            "local_seen_vehicle_ids": set(),
            "local_speed_samples": [],
            "local_time_loss_samples": [],
            "local_delay_samples": [],
            "local_queue_samples": [],
            "local_stop_count": 0,
            "prev_vehicle_stop_state": {},
            "active_ped_entries": {},
            "ped_intervals": [],
            "active_vehicle_entries": {},
            "vehicle_intervals": [],
        }
    veh_delays: dict[str, list[float]] = {cid: [] for cid in candidate_df["crosswalk_id"].astype(str).tolist()}
    next_metric_sample_t = 0.0

    step = 0
    t = 0.0
    try:
        while t < float(duration):
            traci.simulationStep()
            t = float(traci.simulation.getTime())
            ped_ids = list(traci.person.getIDList())
            veh_ids = list(traci.vehicle.getIDList())
            step_crossing_hit: dict[str, bool] = {cid: False for cid in candidate_meta}
            step_crossing_person_ids: dict[str, set[str]] = {cid: set() for cid in candidate_meta}
            current_conflict_vehicle_ids: dict[str, set[str]] = {cid: set() for cid in candidate_meta}
            metric_sample_due = t + 1e-9 >= next_metric_sample_t
            if metric_sample_due:
                next_metric_sample_t += 10.0

            for pid in traci.simulation.getDepartedPersonIDList():
                departure_times[str(pid)] = t
            for vid in traci.simulation.getDepartedIDList():
                network_departed_vehicle_ids.add(str(vid))
            for vid in traci.simulation.getArrivedIDList():
                network_arrived_vehicle_ids.add(str(vid))
            try:
                network_teleport_count += len(set(traci.simulation.getStartingTeleportIDList()))
            except Exception:
                pass

            lane_halting_by_id: dict[str, int] = {}
            if metric_sample_due:
                for cid, watch in candidate_watch.items():
                    local_queue = 0
                    for lane_id in watch["local_lane_ids"]:
                        try:
                            halted = int(traci.lane.getLastStepHaltingNumber(lane_id))
                        except Exception:
                            halted = 0
                        lane_halting_by_id[lane_id] = halted
                        local_queue += halted
                    watch["local_queue_samples"].append(int(local_queue))

            vehicle_scope_hits: dict[str, list[str]] = {cid: [] for cid in candidate_meta}
            for vid in veh_ids:
                try:
                    road_id = str(traci.vehicle.getRoadID(vid))
                except Exception:
                    continue
                lane_id = ""
                speed = float("nan")
                try:
                    lane_id = str(traci.vehicle.getLaneID(vid))
                except Exception:
                    lane_id = ""
                try:
                    speed = float(traci.vehicle.getSpeed(vid))
                except Exception:
                    speed = float("nan")
                road_aliases = _normalize_road_alias(road_id) | _normalize_road_alias(lane_id)
                if road_id in all_network_passenger_edges:
                    network_seen_vehicle_edges.add(road_id)
                for cid, watch in candidate_watch.items():
                    in_local_scope = lane_id in watch["local_lane_ids"] or bool(road_aliases & watch["local_edge_aliases"])
                    if in_local_scope:
                        vehicle_scope_hits[cid].append(str(vid))
                        watch["local_seen_vehicle_ids"].add(str(vid))
                        if metric_sample_due:
                            if pd.notna(speed):
                                watch["local_speed_samples"].append(float(speed))
                            try:
                                watch["local_time_loss_samples"].append(float(traci.vehicle.getTimeLoss(vid)))
                            except Exception:
                                pass
                            try:
                                watch["local_delay_samples"].append(float(traci.vehicle.getAccumulatedWaitingTime(vid)))
                            except Exception:
                                pass
                            stop_now = bool(pd.notna(speed) and float(speed) <= 0.1)
                            prev_stop = bool(watch["prev_vehicle_stop_state"].get(str(vid), False))
                            if stop_now and not prev_stop:
                                watch["local_stop_count"] += 1
                            watch["prev_vehicle_stop_state"][str(vid)] = stop_now
                    in_conflict_zone = bool(road_aliases & watch["conflict_edge_aliases"])
                    if in_conflict_zone:
                        current_conflict_vehicle_ids[cid].add(str(vid))
                        if str(vid) not in watch["active_vehicle_entries"]:
                            watch["active_vehicle_entries"][str(vid)] = float(t)

            for cid, meta in candidate_meta.items():
                tls_id = meta["tls_id"]
                ped_link_index = meta["ped_link_index"]
                crossing_edge_id = meta["crossing_edge_id"]
                crossing_edge_set = crossing_roads.get(cid, set()) | {crossing_edge_id}
                route_path_edges = route_path_by_cid.get(cid, set())

                try:
                    state = traci.trafficlight.getRedYellowGreenState(tls_id)
                    phase = traci.trafficlight.getPhase(tls_id)
                    remaining = float(traci.trafficlight.getNextSwitch(tls_id) - t)
                except Exception:
                    if not light_output:
                        extension_trigger_debug_rows.append(
                            {
                                "sim_time": round(t, 1),
                                "crosswalk_id": cid,
                                "tls_id": tls_id,
                                "ped_link_index": ped_link_index,
                                "current_phase_index": "",
                                "current_tls_state": "",
                                "ped_link_state": "",
                                "is_ped_green": False,
                                "is_pedestrian_only_phase": False,
                                "ped_near": 0,
                                "detected_person_ids": "",
                                "remaining_phase_time": "",
                                "next_switch_time": "",
                                "extension_allowed": False,
                                "extension_decision": False,
                                "extension_skip_reason": "missing_tls",
                            }
                        )
                        signal_phase_rows.append(
                            {
                                "sim_time": round(t, 1),
                                "crosswalk_id": cid,
                                "tls_id": tls_id,
                                "ped_link_index": ped_link_index,
                                "phase_index": "",
                                "phase_state": "",
                                "ped_link_state": "",
                                "ped_has_green": False,
                                "ped_has_red": False,
                                "pedestrian_only_phase": False,
                                "non_ped_green_count": "",
                            }
                        )
                    continue

                ped_link_state = state[ped_link_index] if 0 <= ped_link_index < len(state) else ""
                is_ped_green = ped_link_state in {"G", "g"}
                non_ped_green_count = _count_green_links(state, ped_link_index) if state else 0
                is_pedestrian_only_phase = bool(is_ped_green and non_ped_green_count == 0)
                detected_person_ids: list[str] = []
                ped_near = 0

                for pid in ped_ids:
                    try:
                        road = traci.person.getRoadID(pid)
                    except Exception:
                        continue
                    try:
                        lane_id = traci.person.getLaneID(pid)
                    except Exception:
                        lane_id = ""
                    hit, _ = _route_match_details(str(road), str(lane_id), crossing_edge_set, route_path_edges, str(tls_id))
                    if hit:
                        ped_near += 1
                        detected_person_ids.append(str(pid))

                if int(phase) != last_phase_for_cid.get(cid, -1):
                    last_phase_for_cid[cid] = int(phase)
                    extended_this_phase[cid] = False
                skip_reason = _extension_skip_reason(
                    scenario,
                    str(tls_id),
                    state,
                    int(ped_link_index),
                    ped_link_state,
                    remaining,
                    ped_near,
                    extended_this_phase[cid],
                )
                extension_allowed = skip_reason == ""
                extension_decision = extension_allowed

                if not light_output:
                    extension_trigger_debug_rows.append(
                        {
                            "sim_time": round(t, 1),
                            "crosswalk_id": cid,
                            "tls_id": tls_id,
                            "ped_link_index": ped_link_index,
                            "current_phase_index": int(phase),
                            "current_tls_state": state,
                            "ped_link_state": ped_link_state,
                            "is_ped_green": bool(is_ped_green),
                            "is_pedestrian_only_phase": bool(is_pedestrian_only_phase),
                            "ped_near": int(ped_near),
                            "detected_person_ids": "|".join(sorted(set(detected_person_ids))),
                            "remaining_phase_time": round(remaining, 1),
                            "next_switch_time": round(t + remaining, 1),
                            "extension_allowed": bool(extension_allowed),
                            "extension_decision": bool(extension_decision),
                            "extension_skip_reason": skip_reason,
                        }
                    )
                    signal_phase_rows.append(
                        {
                            "sim_time": round(t, 1),
                            "crosswalk_id": cid,
                            "tls_id": tls_id,
                            "ped_link_index": ped_link_index,
                            "phase_index": int(phase),
                            "phase_state": state,
                            "ped_link_state": ped_link_state,
                            "ped_has_green": bool(is_ped_green),
                            "ped_has_red": bool(ped_link_state in {"r", "R"}),
                            "pedestrian_only_phase": bool(is_pedestrian_only_phase),
                            "non_ped_green_count": int(non_ped_green_count),
                        }
                    )

                if extension_allowed and extension_sec > 0:
                    try:
                        traci.trafficlight.setPhaseDuration(tls_id, remaining + extension_sec)
                        extended_this_phase[cid] = True
                        extension_events.append(
                            {
                                "time": round(t, 1),
                                "crosswalk_id": cid,
                                "tls_id": tls_id,
                                "linkIndex": ped_link_index,
                                "phase": phase,
                                "state": state,
                                "remaining_before": round(remaining, 1),
                                "extension_sec": extension_sec,
                                "ped_near": ped_near,
                            }
                        )
                    except Exception as exc:
                        extension_events.append(
                            {
                                "time": round(t, 1),
                                "crosswalk_id": cid,
                                "tls_id": tls_id,
                                "linkIndex": ped_link_index,
                                "phase": phase,
                                "state": state,
                                "error": str(exc),
                            }
                        )

                # wait / crossing stats
                for pid in ped_ids:
                    try:
                        road = traci.person.getRoadID(pid)
                    except Exception:
                        continue
                    try:
                        lane_id = traci.person.getLaneID(pid)
                    except Exception:
                        lane_id = ""
                    road_match, match_reason = _route_match_details(str(road), str(lane_id), crossing_edge_set, route_path_edges, str(tls_id))
                    if not light_output:
                        debug_rows.append(
                            {
                                "time": round(t, 1),
                                "scenario": scenario,
                                "crosswalk_id": cid,
                                "person_id": pid,
                                "road_id": road,
                                "lane_id": lane_id,
                                "tls_id": tls_id,
                                "phase": phase,
                                "state": state,
                                "remaining_time": round(remaining, 1),
                                "ped_link_index": ped_link_index,
                                "crossing_edge_id": crossing_edge_id,
                                "route_path_edges": _route_path_text(sorted(route_path_edges)),
                                "crossing_match": bool(road_match),
                                "match_reason": match_reason,
                            }
                        )
                    if road_match:
                        step_crossing_hit[cid] = True
                        step_crossing_person_ids[cid].add(str(pid))
                        ped_people_seen[cid].add(str(pid))
                        watch = candidate_watch[cid]
                        if str(pid) not in watch["active_ped_entries"]:
                            watch["active_ped_entries"][str(pid)] = float(t)
                        if pid not in wait_recorded:
                            depart_t = departure_times.get(str(pid), t)
                            wait_recorded.add(pid)
                            rows.append(
                                {
                                    "crosswalk_id": cid,
                                    "scenario": scenario,
                                    "seed": seed,
                                    "tls_id_used": tls_id,
                                    "ped_link_index": ped_link_index,
                                    "crossing_edge_id": crossing_edge_id,
                                    "route_from_edge": meta["route_from_edge"],
                                    "route_to_edge": meta["route_to_edge"],
                                    "ped_wait_time": round(t - depart_t, 2),
                                    "ped_crossing_count": 1,
                                    "veh_delay_sample": None,
                                }
                            )
                            route_diag_by_person[str(pid)] = {
                                "crosswalk_id": cid,
                                "person_id": str(pid),
                                "depart": round(depart_t, 1),
                                "route_from_edge": meta["route_from_edge"],
                                "route_to_edge": meta["route_to_edge"],
                                "crossing_edge_id": crossing_edge_id,
                                "generated_route_edges": meta["generated_route_edges"],
                                "contains_crossing_edge": bool(meta["contains_crossing_edge"]),
                                "detected_crossing_presence": True,
                                "reason": match_reason,
                            }

            for cid, hit in step_crossing_hit.items():
                if hit:
                    ped_presence_steps[cid] += 1
                watch = candidate_watch[cid]
                seen_peds = step_crossing_person_ids.get(cid, set())
                for ped_id in list(watch["active_ped_entries"].keys()):
                    if ped_id in seen_peds:
                        continue
                    enter_time = float(watch["active_ped_entries"].pop(ped_id))
                    watch["ped_intervals"].append(
                        {
                            "pedestrian_id": ped_id,
                            "enter_time": enter_time,
                            "exit_time": float(t),
                        }
                    )
                seen_vehicles = current_conflict_vehicle_ids.get(cid, set())
                for veh_id in list(watch["active_vehicle_entries"].keys()):
                    if veh_id in seen_vehicles:
                        continue
                    enter_time = float(watch["active_vehicle_entries"].pop(veh_id))
                    watch["vehicle_intervals"].append(
                        {
                            "vehicle_id": veh_id,
                            "enter_time": enter_time,
                            "exit_time": float(t),
                        }
                    )

            if step % 20 == 0:
                for cid in candidate_meta:
                    for vid in vehicle_scope_hits.get(cid, veh_ids):
                        try:
                            veh_delays[cid].append(float(traci.vehicle.getAccumulatedWaitingTime(vid)))
                        except Exception:
                            pass

            step += 1
    finally:
        try:
            traci.close(False)
        except Exception:
            pass
    run_end_time = datetime.now(timezone.utc).isoformat(timespec="seconds")
    elapsed_sec = round(time.perf_counter() - run_start_perf, 3)
    tripinfo_metrics = _parse_tripinfo_metrics(out_dir / f"phase6_smoke_{scenario}_tripinfo.xml")
    network_collision_count = _parse_collision_count(out_dir / f"phase6_smoke_{scenario}_collisions.xml")
    ssm_pet_rows = _parse_ssm_pet_rows(out_dir / f"phase6_smoke_{scenario}_ssm.xml")
    net_sha256 = _sha256_file(net_file)
    vehicle_route_sha256 = _sha256_file(veh_file)
    pedestrian_route_sha256 = _sha256_file(ped_file)

    for cid, watch in candidate_watch.items():
        for ped_id, enter_time in list(watch["active_ped_entries"].items()):
            watch["ped_intervals"].append(
                {
                    "pedestrian_id": ped_id,
                    "enter_time": float(enter_time),
                    "exit_time": float(t),
                }
            )
        for veh_id, enter_time in list(watch["active_vehicle_entries"].items()):
            watch["vehicle_intervals"].append(
                {
                    "vehicle_id": veh_id,
                    "enter_time": float(enter_time),
                    "exit_time": float(t),
                }
            )
        watch["active_ped_entries"].clear()
        watch["active_vehicle_entries"].clear()

    for rec in ped_records:
        if rec["person_id"] not in route_diag_by_person:
            route_diag_by_person[rec["person_id"]] = {
                "crosswalk_id": rec["crosswalk_id"],
                "person_id": rec["person_id"],
                "depart": rec["depart"],
                "route_from_edge": rec["route_from_edge"],
                "route_to_edge": rec["route_to_edge"],
                "crossing_edge_id": rec["crossing_edge_id"],
                "generated_route_edges": rec["generated_route_edges"],
                "contains_crossing_edge": bool(rec["contains_crossing_edge"]),
                "detected_crossing_presence": False,
                "reason": "not_observed_on_crossing_path",
            }

    pd.DataFrame(list(route_diag_by_person.values()), columns=[
        "crosswalk_id",
        "person_id",
        "depart",
        "route_from_edge",
        "route_to_edge",
        "crossing_edge_id",
        "generated_route_edges",
        "contains_crossing_edge",
        "detected_crossing_presence",
        "reason",
    ]).to_csv(out_dir / "pedestrian_route_diagnostics.csv", index=False)

    ssm_pet_by_candidate: dict[str, list[float]] = {cid: [] for cid in candidate_meta}
    for rec in ssm_pet_rows:
        ped_id = str(rec.get("pedestrian_id", "") or "")
        if not ped_id or ped_id not in route_diag_by_person:
            continue
        cid = str(route_diag_by_person[ped_id]["crosswalk_id"])
        ssm_pet_by_candidate.setdefault(cid, []).append(float(rec["pet"]))

    out_rows: list[dict[str, Any]] = []
    for cid, meta in candidate_meta.items():
        waits = [r["ped_wait_time"] for r in rows if r["crosswalk_id"] == cid and r["ped_wait_time"] is not None]
        delays = veh_delays.get(cid, [])
        watch = candidate_watch[cid]
        pedestrian_crossing_count = int(len(ped_people_seen.get(cid, set())))
        expected_ped_repeat_count = int(expected_repeat_counts.get(cid, 1))
        unfinished_crossing_count = max(0, expected_ped_repeat_count - pedestrian_crossing_count)
        ssm_values = [float(value) for value in ssm_pet_by_candidate.get(cid, [])]
        if pedestrian_crossing_count > 0 and len(ssm_values) == pedestrian_crossing_count:
            pet_metrics = _summarize_pet_values(
                ssm_values,
                pedestrian_crossing_count,
                len(watch["conflict_edge_ids"]),
                pet_source="ssm_pet",
            )
        else:
            pet_metrics = _summarize_local_watcher_pet(
                watch["ped_intervals"],
                watch["vehicle_intervals"],
                pedestrian_crossing_count,
                len(watch["conflict_edge_ids"]),
            )
            if ssm_values and not pet_metrics["pet_available"]:
                pet_metrics["pet_unavailable_reason"] = "ssm_pet_partial_or_unmapped__fallback_local_watcher"
        local_bbox = watch.get("bbox")
        if local_bbox is not None and network_bbox is not None:
            local_bbox_coverage = float(
                max(0.0, min(local_bbox[2], network_bbox[2]) - max(local_bbox[0], network_bbox[0]))
                * max(0.0, min(local_bbox[3], network_bbox[3]) - max(local_bbox[1], network_bbox[1]))
            )
            denom = float(max((local_bbox[2] - local_bbox[0]) * (local_bbox[3] - local_bbox[1]), 1e-9))
            vehicle_bbox_coverage = float(local_bbox_coverage / denom)
        else:
            vehicle_bbox_coverage = float("nan")
        out_rows.append(
            {
                "run_name": run_name,
                "crosswalk_id": cid,
                "scenario": scenario,
                "seed": seed,
                "output_dir": str(out_dir),
                "run_start_time": run_start_time,
                "run_end_time": run_end_time,
                "elapsed_sec": elapsed_sec,
                "completed": True,
                "sim_duration": int(duration),
                "warmup": int(warmup),
                "step_length": float(step_length),
                "tls_id_used": meta["tls_id"],
                "ped_link_index": meta["ped_link_index"],
                "crossing_edge_id": meta["crossing_edge_id"],
                "route_from_edge": meta["route_from_edge"],
                "route_to_edge": meta["route_to_edge"],
                "completed": True,
                "step_count": step,
                "ped_crossing_presence_steps": ped_presence_steps.get(cid, 0),
                "ped_crossing_person_count": pedestrian_crossing_count,
                "pedestrian_crossing_count": pedestrian_crossing_count,
                "expected_ped_repeat_count": expected_ped_repeat_count,
                "ped_repeat_count_match": pedestrian_crossing_count == expected_ped_repeat_count,
                "pedestrian_clearance_failure_count": unfinished_crossing_count,
                "unfinished_crossing_count": unfinished_crossing_count,
                "ped_wait_time_mean": round(sum(waits) / len(waits), 2) if waits else None,
                "ped_wait_time_max": round(max(waits), 2) if waits else None,
                "veh_delay_mean": round(sum(delays) / len(delays), 2) if delays else None,
                "veh_delay_max": round(max(delays), 2) if delays else None,
                "extension_count": len([e for e in extension_events if e.get("crosswalk_id") == cid]),
                "generated_vehicle_count": int(generated_vehicle_count),
                "vehicle_route_count": int(vehicle_route_diversity["vehicle_route_count"]),
                "unique_vehicle_route_count": int(vehicle_route_diversity["unique_vehicle_route_count"]),
                "duplicate_factor": float(vehicle_route_diversity["duplicate_factor"]),
                "unique_vehicle_route_ratio": float(vehicle_route_diversity["unique_vehicle_route_ratio"]),
                "used_vehicle_edges": int(vehicle_route_diversity["used_vehicle_edges"]),
                "network_arrived_vehicles": int(len(network_arrived_vehicle_ids)),
                "network_departed_vehicles": int(len(network_departed_vehicle_ids)),
                "network_mean_travel_time": tripinfo_metrics["network_mean_travel_time"],
                "network_mean_time_loss": tripinfo_metrics["network_mean_time_loss"],
                "network_avg_delay_sec": tripinfo_metrics["network_avg_delay_sec"],
                "network_vehicle_edge_coverage_ratio": (
                    float(len(network_seen_vehicle_edges) / max(len(all_network_passenger_edges), 1))
                ),
                "network_teleport_count": int(network_teleport_count),
                "network_collision_count": int(network_collision_count),
                "local_500m_vehicle_count": int(len(watch["local_seen_vehicle_ids"])),
                "local_500m_mean_speed": _mean(watch["local_speed_samples"]),
                "local_500m_mean_time_loss": _mean(watch["local_time_loss_samples"]),
                "local_500m_avg_delay_sec": _mean(watch["local_delay_samples"]),
                "local_500m_queue_proxy": _mean(watch["local_queue_samples"]),
                "local_500m_stop_count": int(watch["local_stop_count"]),
                "total_vehicle_arrivals": int(len(network_departed_vehicle_ids)),
                "generated_vehicle_route_file": generated_vehicle_route_file,
                "vehicle_route_sha256": vehicle_route_sha256,
                "pedestrian_route_file": str(ped_file),
                "pedestrian_route_sha256": pedestrian_route_sha256,
                "net_sha256": net_sha256,
                "surrounding_lane_count": int(surrounding_lane_count),
                "vehicle_bbox_coverage": vehicle_bbox_coverage,
                "batch_network_file": meta["batch_network_file"],
                "route_reason": meta["route_reason"],
                **pet_metrics,
            }
        )

    pd.DataFrame(out_rows).to_csv(out_dir / f"phase6_smoke_{scenario}_results.csv", index=False)
    (out_dir / f"phase6_smoke_{scenario}_extension_events.json").write_text(json.dumps(extension_events, ensure_ascii=False, indent=2), encoding="utf-8")
    ext_columns = ["time", "crosswalk_id", "tls_id", "linkIndex", "phase", "state", "remaining_before", "extension_sec", "ped_near", "error"]
    pd.DataFrame(extension_events, columns=ext_columns).to_csv(out_dir / f"phase6_smoke_{scenario}_extension_events.csv", index=False)
    if not light_output:
        pd.DataFrame(debug_rows).to_csv(out_dir / f"phase6_smoke_{scenario}_debug_trace.csv", index=False)
        pd.DataFrame(extension_trigger_debug_rows).to_csv(out_dir / "phase6_extension_trigger_debug.csv", index=False)
        pd.DataFrame(signal_phase_rows).to_csv(out_dir / "signal_phase_audit.csv", index=False)
        (out_dir / f"phase6_smoke_{scenario}_candidate_validation.json").write_text(json.dumps(candidate_meta, ensure_ascii=False, indent=2), encoding="utf-8")
    (out_dir / f"phase6_smoke_{scenario}_route_order_summary.json").write_text(
        json.dumps(
            {
                "scenario": scenario,
                "ped_route_file": str(ped_file),
                "ped_route_order_debug_csv": str(ped_summary_path) if not light_output else "",
                "ped_depart_sorted_ok": route_order_ok,
                "ped_person_rows": int(len(ped_records)),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    other_scenario = "smart" if scenario == "baseline" else "baseline"
    other_dir = out_dir.with_name(f"phase6_recovery_smoke_{other_scenario}_20260514_220549")
    other_results = other_dir / f"phase6_smoke_{other_scenario}_results.csv"
    current_results = out_dir / f"phase6_smoke_{scenario}_results.csv"
    compare_path = RESULT_DIR / "phase6_transition_after_recovery_20260514_220549" / "phase6_smoke_baseline_vs_smart_compare.csv"
    required_cols = [
        "completed",
        "ped_crossing_presence_steps",
        "ped_crossing_person_count",
        "expected_ped_repeat_count",
        "ped_repeat_count_match",
        "pedestrian_clearance_failure_count",
        "unfinished_crossing_count",
        "extension_count",
    ]
    compare_dir = RESULT_DIR / "phase6_transition_after_recovery_20260514_220549"
    if not current_results.exists():
        _write_compare_warning(compare_dir, scenario, "current_results_missing", {"path": str(current_results)})
        return pd.DataFrame(out_rows)
    if not other_results.exists():
        _write_compare_warning(compare_dir, scenario, "other_results_missing", {"path": str(other_results)})
        return pd.DataFrame(out_rows)

    current_df_raw = pd.read_csv(current_results)
    other_df_raw = pd.read_csv(other_results)
    missing_current = [c for c in required_cols if c not in current_df_raw.columns]
    missing_other = [c for c in required_cols if c not in other_df_raw.columns]
    if missing_current or missing_other:
        _write_compare_warning(
            compare_dir,
            scenario,
            "required_columns_missing",
            {
                "current_missing": missing_current,
                "other_missing": missing_other,
                "current_path": str(current_results),
                "other_path": str(other_results),
            },
        )
        return pd.DataFrame(out_rows)

    current_df = current_df_raw.set_index("crosswalk_id")
    other_df = other_df_raw.set_index("crosswalk_id")
    ordered = candidate_df["crosswalk_id"].astype(str).tolist()
    compare_rows: list[dict[str, Any]] = []
    for cid in ordered:
        cur = current_df.loc[cid] if cid in current_df.index else None
        oth = other_df.loc[cid] if cid in other_df.index else None
        baseline_row = cur if scenario == "baseline" else oth
        smart_row = cur if scenario == "smart" else oth
        baseline_vehicle_delay = _safe_get(baseline_row, "veh_delay_mean")
        if baseline_vehicle_delay is None:
            baseline_vehicle_delay = _safe_get(baseline_row, "veh_waiting_time_mean")
        smart_vehicle_delay = _safe_get(smart_row, "veh_delay_mean")
        if smart_vehicle_delay is None:
            smart_vehicle_delay = _safe_get(smart_row, "veh_waiting_time_mean")
        compare_rows.append(
            {
                "crosswalk_id": cid,
                "completed_baseline": bool(_safe_get(cur, "completed")) if scenario == "baseline" else bool(_safe_get(oth, "completed")) if other_scenario == "baseline" else None,
                "completed_smart": bool(_safe_get(cur, "completed")) if scenario == "smart" else bool(_safe_get(oth, "completed")) if other_scenario == "smart" else None,
                "ped_crossing_presence_steps_baseline": int(_safe_get(cur, "ped_crossing_presence_steps")) if scenario == "baseline" else int(_safe_get(oth, "ped_crossing_presence_steps")) if other_scenario == "baseline" else None,
                "ped_crossing_presence_steps_smart": int(_safe_get(cur, "ped_crossing_presence_steps")) if scenario == "smart" else int(_safe_get(oth, "ped_crossing_presence_steps")) if other_scenario == "smart" else None,
                "ped_crossing_person_count_baseline": int(_safe_get(cur, "ped_crossing_person_count")) if scenario == "baseline" else int(_safe_get(oth, "ped_crossing_person_count")) if other_scenario == "baseline" else None,
                "ped_crossing_person_count_smart": int(_safe_get(cur, "ped_crossing_person_count")) if scenario == "smart" else int(_safe_get(oth, "ped_crossing_person_count")) if other_scenario == "smart" else None,
                "expected_ped_repeat_count_baseline": int(_safe_get(cur, "expected_ped_repeat_count")) if scenario == "baseline" else int(_safe_get(oth, "expected_ped_repeat_count")) if other_scenario == "baseline" else None,
                "expected_ped_repeat_count_smart": int(_safe_get(cur, "expected_ped_repeat_count")) if scenario == "smart" else int(_safe_get(oth, "expected_ped_repeat_count")) if other_scenario == "smart" else None,
                "ped_repeat_count_match_baseline": bool(_safe_get(cur, "ped_repeat_count_match")) if scenario == "baseline" else bool(_safe_get(oth, "ped_repeat_count_match")) if other_scenario == "baseline" else None,
                "ped_repeat_count_match_smart": bool(_safe_get(cur, "ped_repeat_count_match")) if scenario == "smart" else bool(_safe_get(oth, "ped_repeat_count_match")) if other_scenario == "smart" else None,
                "pedestrian_clearance_failure_count_baseline": int(_safe_get(cur, "pedestrian_clearance_failure_count")) if scenario == "baseline" else int(_safe_get(oth, "pedestrian_clearance_failure_count")) if other_scenario == "baseline" else None,
                "pedestrian_clearance_failure_count_smart": int(_safe_get(cur, "pedestrian_clearance_failure_count")) if scenario == "smart" else int(_safe_get(oth, "pedestrian_clearance_failure_count")) if other_scenario == "smart" else None,
                "unfinished_crossing_count_baseline": int(_safe_get(cur, "unfinished_crossing_count")) if scenario == "baseline" else int(_safe_get(oth, "unfinished_crossing_count")) if other_scenario == "baseline" else None,
                "unfinished_crossing_count_smart": int(_safe_get(cur, "unfinished_crossing_count")) if scenario == "smart" else int(_safe_get(oth, "unfinished_crossing_count")) if other_scenario == "smart" else None,
                "extension_count_baseline": int(_safe_get(cur, "extension_count")) if scenario == "baseline" else int(_safe_get(oth, "extension_count")) if other_scenario == "baseline" else None,
                "extension_count_smart": int(_safe_get(cur, "extension_count")) if scenario == "smart" else int(_safe_get(oth, "extension_count")) if other_scenario == "smart" else None,
                "baseline_output_dir": _safe_get(baseline_row, "output_dir"),
                "smart_output_dir": _safe_get(smart_row, "output_dir"),
                "baseline_run_name": _safe_get(baseline_row, "run_name"),
                "smart_run_name": _safe_get(smart_row, "run_name"),
                "baseline_run_start_time": _safe_get(baseline_row, "run_start_time"),
                "smart_run_start_time": _safe_get(smart_row, "run_start_time"),
                "baseline_run_end_time": _safe_get(baseline_row, "run_end_time"),
                "smart_run_end_time": _safe_get(smart_row, "run_end_time"),
                "baseline_elapsed_sec": _safe_get(baseline_row, "elapsed_sec"),
                "smart_elapsed_sec": _safe_get(smart_row, "elapsed_sec"),
                "baseline_ped_count": _safe_get(baseline_row, "ped_crossing_person_count"),
                "smart_ped_count": _safe_get(smart_row, "ped_crossing_person_count"),
                "baseline_vehicle_delay_mean": baseline_vehicle_delay,
                "smart_vehicle_delay_mean": smart_vehicle_delay,
                "comparison_status": "ready" if bool(_safe_get(cur, "completed")) and bool(_safe_get(oth, "completed")) else "incomplete_run",
            }
        )
    pd.DataFrame(compare_rows).to_csv(compare_path, index=False)
    return pd.DataFrame(out_rows)


def main() -> None:
    parser = argparse.ArgumentParser(description="Phase 6 recovery smoke runner using final 237 recovery candidates directly.")
    parser.add_argument("--candidate-csv", required=True, help="Prepared smoke candidate CSV.")
    parser.add_argument("--net-file", required=True, help="Recovery batch network XML for candidates.")
    parser.add_argument("--factory-table", default=str(FACTORY_TABLE), help="Optional factory table CSV; if absent, fall back to candidate CSV + net audit.")
    parser.add_argument("--crossing-inventory", default=None, help="Optional crossing inventory CSV. Used only as fallback if present.")
    parser.add_argument("--scenario", choices=["baseline", "smart"], required=True)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--sim-duration", type=int, default=300)
    parser.add_argument("--warmup", type=int, default=0)
    parser.add_argument("--step-length", type=float, default=0.5)
    parser.add_argument("--extension-sec", type=float, default=5.0, help="Smart pedestrian green extension in seconds.")
    parser.add_argument("--ped-repeat-count", type=int, default=None, help="Override pedestrian repeat count for smoke injection.")
    parser.add_argument("--ped-repeat-spacing-sec", type=float, default=None, help="Override pedestrian depart spacing in seconds.")
    parser.add_argument("--ped-depart-offset-sec", type=float, default=None, help="Override pedestrian depart offset in seconds.")
    parser.add_argument("--phase-aligned-ped-depart", action="store_true", help="Align first pedestrian depart with pedestrian-only green phase.")
    parser.add_argument("--include-vehicles", action="store_true", help="Also emit vehicle routes and include them in the SUMO config.")
    parser.add_argument("--metric-sample-interval", type=float, default=0.0, help="Accepted for CLI parity; smoke runner does not use lane sampling.")
    parser.add_argument("--output-profile", choices=["full", "light"], default="full")
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args()

    candidate_csv = Path(args.candidate_csv).expanduser().resolve()
    net_file = Path(args.net_file).expanduser().resolve()
    factory_table = Path(args.factory_table).expanduser().resolve() if args.factory_table else None
    crossing_inventory = Path(args.crossing_inventory).expanduser().resolve() if args.crossing_inventory else None
    out_dir = Path(args.output_dir).expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    candidate_df, diagnostics_df = _build_candidate_table(candidate_csv, net_file, crossing_inventory, out_dir, factory_table)
    if not net_file.exists():
        raise FileNotFoundError(net_file)

    if args.ped_repeat_count is not None:
        candidate_df["ped_repeat_count"] = int(args.ped_repeat_count)
    if args.ped_repeat_spacing_sec is not None:
        candidate_df["ped_repeat_spacing_sec"] = float(args.ped_repeat_spacing_sec)

    depart_plan_df = pd.DataFrame()
    if args.phase_aligned_ped_depart:
        depart_plan_df = _phase_aligned_depart_plan(
            net_file,
            candidate_df,
            ped_repeat_count=args.ped_repeat_count,
            ped_repeat_spacing_sec=args.ped_repeat_spacing_sec,
            ped_depart_offset_sec=args.ped_depart_offset_sec,
        )
        depart_plan_df.to_csv(out_dir / "pedestrian_depart_plan.csv", index=False)
        if not depart_plan_df.empty:
            candidate_df = candidate_df.merge(
                depart_plan_df[["crosswalk_id", "depart_time"]].rename(columns={"depart_time": "ped_depart_time"}),
                on="crosswalk_id",
                how="left",
            )
    elif args.ped_depart_offset_sec is not None:
        candidate_df["ped_depart_offset_sec"] = float(args.ped_depart_offset_sec)

    metadata = {
        "candidate_csv": str(candidate_csv),
        "net_file": str(net_file),
        "scenario": args.scenario,
        "seed": args.seed,
        "sim_duration": args.sim_duration,
        "warmup": args.warmup,
        "step_length": args.step_length,
        "extension_sec": args.extension_sec,
        "include_vehicles": args.include_vehicles,
        "metric_sample_interval": float(args.metric_sample_interval),
        "output_profile": str(args.output_profile),
        "output_dir": str(out_dir),
        "candidate_rows": int(len(candidate_df)),
        "candidate_ids": candidate_df["crosswalk_id"].astype(str).tolist(),
    }
    (out_dir / "run_metadata.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    candidate_df.to_csv(out_dir / "smoke_candidates_resolved.csv", index=False)
    diagnostics_df.to_csv(out_dir / "candidate_table_source_diagnostics.csv", index=False)

    summary = _run_scenario(
        candidate_df,
        net_file,
        args.scenario,
        args.seed,
        args.sim_duration,
        args.warmup,
        args.step_length,
        out_dir,
        args.include_vehicles,
        args.extension_sec,
        output_profile=str(args.output_profile),
    )

    metadata.update(
        {
            "run_name": summary["run_name"].iloc[0] if not summary.empty and "run_name" in summary.columns else f"phase6_smoke_{args.scenario}_seed{args.seed}",
            "run_start_time": summary["run_start_time"].iloc[0] if not summary.empty and "run_start_time" in summary.columns else "",
            "run_end_time": summary["run_end_time"].iloc[0] if not summary.empty and "run_end_time" in summary.columns else "",
            "elapsed_sec": float(summary["elapsed_sec"].iloc[0]) if not summary.empty and "elapsed_sec" in summary.columns and pd.notna(summary["elapsed_sec"].iloc[0]) else None,
            "completed": bool(summary["completed"].iloc[0]) if not summary.empty and "completed" in summary.columns else True,
        }
    )
    (out_dir / "run_metadata.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    summary.to_csv(out_dir / "phase6_smoke_summary.csv", index=False)
    route_pair_path = out_dir / "route_pair_diagnostics.csv"
    if route_pair_path.exists() and args.output_profile != "light":
        try:
            pd.read_csv(route_pair_path).to_csv(out_dir / "route_generation_audit.csv", index=False)
        except Exception:
            pass
    if args.output_profile != "light":
        candidate_signal_phase_rows = []
        for row in candidate_df.itertuples(index=False):
            candidate_signal_phase_rows.append(
                {
                    "crosswalk_id": str(row.crosswalk_id),
                    "scenario": args.scenario,
                    "seed": args.seed,
                    "tls_id": getattr(row, "tls_id", ""),
                    "ped_link_index": getattr(row, "ped_link_index", ""),
                    "crossing_edge_id": getattr(row, "crossing_edge_id", ""),
                    "route_from_edge": getattr(row, "route_from_edge", ""),
                    "route_to_edge": getattr(row, "route_to_edge", ""),
                    "route_reason": getattr(row, "route_reason", ""),
                    "source_file": getattr(row, "source_file", ""),
                    "batch_network_file": getattr(row, "batch_network_file", ""),
                    "final_verdict": getattr(row, "final_verdict", ""),
                    "step_test_ok": getattr(row, "step_test_ok", ""),
                    "controlled_links_count": getattr(row, "controlled_links_count", ""),
                    "crossing_inventory_used": bool(getattr(row, "crossing_inventory_used", False)),
                }
            )
        pd.DataFrame(candidate_signal_phase_rows).to_csv(out_dir / "candidate_signal_phase_audit.csv", index=False)
    route_alias = out_dir / "pedestrian_route_diagnostics.csv"
    if route_alias.exists():
        try:
            ped_route_df = pd.read_csv(route_alias)
            ped_route_df.to_csv(out_dir / "pedestrian_route_connectivity_audit.csv", index=False)
            if "detected_crossing_presence" in ped_route_df.columns:
                skipped_mask = pd.to_numeric(ped_route_df["detected_crossing_presence"], errors="coerce").fillna(False).astype(bool)
            else:
                skipped_mask = pd.Series([False] * len(ped_route_df))
            skipped_df = ped_route_df[~skipped_mask].copy()
            if skipped_df.empty:
                skipped_df = ped_route_df.copy().iloc[0:0]
            skipped_df.to_csv(out_dir / "skipped_pedestrian_routes.csv", index=False)
        except Exception:
            pass
    source_inventory_rows = [
        {
            "source_type": "candidate_csv",
            "path": str(candidate_csv),
            "exists": candidate_csv.exists(),
        },
        {
            "source_type": "net_file",
            "path": str(net_file),
            "exists": net_file.exists(),
        },
        {
            "source_type": "factory_table",
            "path": str(factory_table) if factory_table else "",
            "exists": bool(factory_table and factory_table.exists()),
        },
    ]
    if crossing_inventory is not None:
        source_inventory_rows.append(
            {
                "source_type": "crossing_inventory",
                "path": str(crossing_inventory),
                "exists": crossing_inventory.exists(),
            }
        )
    pd.DataFrame(source_inventory_rows).to_csv(out_dir / "source_file_inventory.csv", index=False)
    (out_dir / "phase6_smoke_summary.md").write_text(
        "\n".join(
            [
                "# phase 6 recovery smoke summary",
                "",
                f"- scenario: {args.scenario}",
                f"- candidate_rows: {len(candidate_df)}",
                f"- seed: {args.seed}",
                f"- sim_duration: {args.sim_duration}",
                f"- warmup: {args.warmup}",
                f"- net_file: `{net_file}`",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"output_dir: {out_dir}")
    print(f"candidate_rows: {len(candidate_df)}")
    print(f"scenario: {args.scenario}")


if __name__ == "__main__":
    main()
