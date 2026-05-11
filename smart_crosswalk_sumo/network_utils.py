from __future__ import annotations

import json
import math
import os
import xml.etree.ElementTree as ET
from collections import deque
from pathlib import Path
from typing import Any


def normalized_sumo_home() -> str | None:
    """Return the SUMO data/tools root even when SUMO_HOME points at the app root."""
    raw = os.environ.get("SUMO_HOME")
    if not raw:
        return None
    home = Path(raw)
    if (home / "data" / "typemap" / "osmNetconvert.typ.xml").exists():
        return str(home)
    nested = home / "share" / "sumo"
    if (nested / "data" / "typemap" / "osmNetconvert.typ.xml").exists():
        return str(nested)
    return raw


def proj_data_dir() -> str | None:
    raw = os.environ.get("SUMO_HOME")
    if not raw:
        return None
    roots = [Path(raw)]
    normalized = normalized_sumo_home()
    if normalized:
        roots.append(Path(normalized))
    for root in roots:
        candidates = [
            root / "share" / "proj",
            root / "proj",
            root / "framework" / "EclipseSUMO.framework" / "Versions" / "1.26.0" / "EclipseSUMO" / "share" / "proj",
        ]
        for candidate in candidates:
            if (candidate / "proj.db").exists():
                return str(candidate)
        try:
            found = next(root.rglob("proj.db"))
            return str(found.parent)
        except StopIteration:
            continue
    return None


def sumo_env() -> dict[str, str]:
    env = os.environ.copy()
    normalized = normalized_sumo_home()
    if normalized:
        env["SUMO_HOME"] = normalized
    proj_dir = proj_data_dir()
    if proj_dir:
        env["PROJ_LIB"] = proj_dir
        env["PROJ_DATA"] = proj_dir
    return env


def apply_sumo_environment() -> None:
    os.environ.update(sumo_env())


def require_sumolib():
    try:
        import sumolib  # type: ignore
    except ImportError as exc:
        raise RuntimeError(
            "sumolib가 필요합니다. SUMO 설치 후 SUMO_HOME과 PYTHONPATH를 확인하세요."
        ) from exc
    return sumolib


def read_net(net_file: str | Path):
    apply_sumo_environment()
    sumolib = require_sumolib()
    return sumolib.net.readNet(str(net_file), withInternal=True, withPrograms=True)


def edge_function(edge: Any) -> str:
    return edge.getFunction() or "normal"


def edge_allows(edge: Any, vclass: str) -> bool:
    try:
        return bool(edge.allows(vclass))
    except Exception:
        return False


def lane_allows(lane: Any, vclass: str) -> bool:
    try:
        return bool(lane.allows(vclass))
    except Exception:
        return False


def point_segment_distance(point: tuple[float, float], a: tuple[float, float], b: tuple[float, float]) -> float:
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


def distance_to_edge_shape(edge: Any, xy: tuple[float, float]) -> float:
    shape = [(float(x), float(y)) for x, y in edge.getShape()]
    if not shape:
        return float("inf")
    if len(shape) == 1:
        return math.dist(xy, shape[0])
    return min(point_segment_distance(xy, shape[i], shape[i + 1]) for i in range(len(shape) - 1))


def edge_center(edge: Any) -> tuple[float, float]:
    shape = edge.getShape()
    if not shape:
        return edge.getFromNode().getCoord()
    xs = [float(x) for x, _ in shape]
    ys = [float(y) for _, y in shape]
    return sum(xs) / len(xs), sum(ys) / len(ys)


def choose_crossing_edge(net: Any, lon: float, lat: float) -> Any:
    crossings = [edge for edge in net.getEdges() if edge_function(edge) == "crossing"]
    if not crossings:
        raise RuntimeError("네트워크에서 crossing edge를 찾지 못했습니다.")
    signalized_crossings = [
        edge for edge in crossings if edge.getFromNode().getType() == "traffic_light"
    ]
    if signalized_crossings:
        crossings = signalized_crossings
    try:
        target_xy = net.convertLonLat2XY(float(lon), float(lat))
    except Exception:
        (xmin, ymin), (xmax, ymax) = net.getBBoxXY()
        target_xy = ((xmin + xmax) / 2, (ymin + ymax) / 2)
    
    # Prioritize crossings that have at least some connectivity to normal edges
    def connectivity_score(edge: Any) -> int:
        wa_in = [e for e in edge.getIncoming().keys() if edge_function(e) == "walkingarea"]
        wa_out = [e for e in edge.getOutgoing().keys() if edge_function(e) == "walkingarea"]
        if not wa_in or not wa_out:
            return 0
        has_in = any(any(edge_function(ee) == "normal" for ee in w.getIncoming().keys()) for w in wa_in)
        has_out = any(any(edge_function(ee) == "normal" for ee in w.getOutgoing().keys()) for w in wa_out)
        if has_in and has_out:
            return 2
        return 1

    sorted_crossings = sorted(crossings, key=lambda edge: (connectivity_score(edge) * -1, distance_to_edge_shape(edge, target_xy)))
    return sorted_crossings[0]


def normal_edges_at_node(node: Any, vclass: str | None = None) -> list[Any]:
    seen = {}
    for edge in list(node.getIncoming()) + list(node.getOutgoing()):
        if edge_function(edge) != "normal":
            continue
        if vclass and not edge_allows(edge, vclass):
            continue
        seen[edge.getID()] = edge
    return list(seen.values())


def pedestrian_route_from_crossing(crossing_edge: Any) -> dict[str, str]:
    """Find a plausible sidewalk route that uses the selected crossing."""
    incoming_walkareas = [
        edge
        for edge in crossing_edge.getIncoming().keys()
        if edge_function(edge) == "walkingarea"
    ]
    outgoing_walkareas = [
        edge
        for edge in crossing_edge.getOutgoing().keys()
        if edge_function(edge) == "walkingarea"
    ]

    from_candidates = []
    for walkingarea in incoming_walkareas:
        for edge in walkingarea.getIncoming().keys():
            if edge_function(edge) == "normal" and edge_allows(edge, "pedestrian"):
                from_candidates.append(edge)

    to_candidates = []
    for walkingarea in outgoing_walkareas:
        for edge in walkingarea.getOutgoing().keys():
            if edge_function(edge) == "normal" and edge_allows(edge, "pedestrian"):
                to_candidates.append(edge)

    if not from_candidates or not to_candidates:
        node = crossing_edge.getFromNode()
        normal_ped_edges = normal_edges_at_node(node, "pedestrian")
        if len(normal_ped_edges) >= 2:
            from_candidates = normal_ped_edges[:1]
            to_candidates = normal_ped_edges[1:2]

    if not from_candidates or not to_candidates:
        raise RuntimeError(f"{crossing_edge.getID()} 주변 보행자 경로를 찾지 못했습니다.")

    return {
        "from_edge": from_candidates[0].getID(),
        "to_edge": to_candidates[0].getID(),
    }


def attempt_route_repair(net: Any, crossing_edge_id: str) -> dict[str, Any] | None:
    crossing_edge = _safe_get_edge(net, crossing_edge_id)
    if not crossing_edge:
        return None

    candidates: set[Any] = set()
    node = crossing_edge.getFromNode()

    # Add from incoming/outgoing walkingareas
    for wa in list(crossing_edge.getIncoming().keys()):
        if edge_function(wa) == "walkingarea":
            for e in list(wa.getIncoming().keys()):
                if edge_function(e) == "normal" and edge_allows(e, "pedestrian"):
                    candidates.add(e)
    for wa in list(crossing_edge.getOutgoing().keys()):
        if edge_function(wa) == "walkingarea":
            for e in list(wa.getOutgoing().keys()):
                if edge_function(e) == "normal" and edge_allows(e, "pedestrian"):
                    candidates.add(e)

    # Also add normal edges at the node
    for e in list(node.getIncoming()) + list(node.getOutgoing()):
        if edge_function(e) == "normal" and edge_allows(e, "pedestrian"):
            candidates.add(e)

    # Also look at neighbors of vehicle conflict edges
    for veh_e in list(node.getIncoming()):
        if edge_function(veh_e) == "normal" and edge_allows(veh_e, "passenger"):
            veh_node = veh_e.getFromNode()
            for ped_e in list(veh_node.getIncoming()) + list(veh_node.getOutgoing()):
                if edge_function(ped_e) == "normal" and edge_allows(ped_e, "pedestrian"):
                    candidates.add(ped_e)
    for veh_e in list(node.getOutgoing()):
        if edge_function(veh_e) == "normal" and edge_allows(veh_e, "passenger"):
            veh_node = veh_e.getToNode()
            for ped_e in list(veh_node.getIncoming()) + list(veh_node.getOutgoing()):
                if edge_function(ped_e) == "normal" and edge_allows(ped_e, "pedestrian"):
                    candidates.add(ped_e)

    best_cost = float("inf")
    best_path: list[str] | None = None
    best_pair: tuple[str, str] | None = None

    for f in list(candidates):
        for t in list(candidates):
            if f.getID() == t.getID():
                continue
            try:
                path_edges, path_cost = net.getShortestPath(f, t, vClass="pedestrian", withInternal=True)
                if path_edges:
                    path_ids = _edge_id_list(path_edges)
                    if crossing_edge_id in path_ids:
                        if path_cost < best_cost and len(path_ids) <= 15:
                            best_cost = path_cost
                            best_path = path_ids
                            best_pair = (f.getID(), t.getID())
            except Exception:
                pass

    if best_pair and best_path is not None:
        return {
            "from_edge": best_pair[0],
            "to_edge": best_pair[1],
            "path_cost": best_cost,
            "path_edge_count": len(best_path),
            "path_edge_sequence": "|".join(best_path),
        }
    return None


def _safe_get_edge(net: Any, edge_id: str | None) -> Any | None:
    if not edge_id:
        return None
    try:
        return net.getEdge(str(edge_id))
    except Exception:
        return None


def _edge_id_list(edges: list[Any] | tuple[Any, ...] | None) -> list[str]:
    if not edges:
        return []
    return [edge.getID() for edge in edges]


def validate_pedestrian_connectivity(
    net_file: str | Path,
    metadata: dict[str, Any],
    cw_id: str | int | None = None,
    net: Any | None = None,
) -> dict[str, Any]:
    if net is None:
        net = read_net(net_file)
    ped_route = metadata.get("ped_route") or {}
    crossing_edge_id = str(metadata.get("crossing_edge", "") or "")
    from_edge_id = str(ped_route.get("from_edge", "") or "")
    to_edge_id = str(ped_route.get("to_edge", "") or "")

    row: dict[str, Any] = {
        "crosswalk_id": str(cw_id if cw_id is not None else metadata.get("cw_id", "")),
        "net_file": str(net_file),
        "crossing_edge": crossing_edge_id,
        "from_edge": from_edge_id,
        "to_edge": to_edge_id,
        "validation_status": "valid",
        "invalid_reason": "",
        "crossing_edge_exists": False,
        "from_edge_exists": False,
        "to_edge_exists": False,
        "crossing_edge_function": "",
        "from_edge_allows_pedestrian": False,
        "to_edge_allows_pedestrian": False,
        "incoming_walkingarea_count": 0,
        "outgoing_walkingarea_count": 0,
        "incoming_walkingareas": "",
        "outgoing_walkingareas": "",
        "path_exists": False,
        "path_uses_crossing": False,
        "path_uses_walkingarea": False,
        "path_cost": math.nan,
        "path_edge_count": 0,
        "path_edge_sequence": "",
    }

    errors: list[str] = []
    crossing_edge = _safe_get_edge(net, crossing_edge_id)
    from_edge = _safe_get_edge(net, from_edge_id)
    to_edge = _safe_get_edge(net, to_edge_id)

    if crossing_edge is None:
        errors.append("missing_crossing_edge")
    else:
        row["crossing_edge_exists"] = True
        row["crossing_edge_function"] = edge_function(crossing_edge)
        if edge_function(crossing_edge) != "crossing":
            errors.append("crossing_edge_not_crossing_function")
        incoming_walkareas = [
            edge.getID()
            for edge in crossing_edge.getIncoming().keys()
            if edge_function(edge) == "walkingarea"
        ]
        outgoing_walkareas = [
            edge.getID()
            for edge in crossing_edge.getOutgoing().keys()
            if edge_function(edge) == "walkingarea"
        ]
        row["incoming_walkingarea_count"] = len(incoming_walkareas)
        row["outgoing_walkingarea_count"] = len(outgoing_walkareas)
        row["incoming_walkingareas"] = "|".join(sorted(incoming_walkareas))
        row["outgoing_walkingareas"] = "|".join(sorted(outgoing_walkareas))
        if not incoming_walkareas:
            errors.append("missing_incoming_walkingarea")
        if not outgoing_walkareas:
            errors.append("missing_outgoing_walkingarea")

    if from_edge is None:
        errors.append("missing_from_edge")
    else:
        row["from_edge_exists"] = True
        row["from_edge_allows_pedestrian"] = edge_allows(from_edge, "pedestrian")
        if not row["from_edge_allows_pedestrian"]:
            errors.append("from_edge_disallows_pedestrian")

    if to_edge is None:
        errors.append("missing_to_edge")
    else:
        row["to_edge_exists"] = True
        row["to_edge_allows_pedestrian"] = edge_allows(to_edge, "pedestrian")
        if not row["to_edge_allows_pedestrian"]:
            errors.append("to_edge_disallows_pedestrian")

    if from_edge is not None and to_edge is not None:
        try:
            path_edges, path_cost = net.getShortestPath(
                from_edge,
                to_edge,
                vClass="pedestrian",
                withInternal=True,
            )
        except Exception:
            path_edges, path_cost = None, math.nan
            errors.append("pedestrian_shortest_path_exception")
        if not path_edges:
            errors.append("no_pedestrian_path")
        else:
            path_ids = _edge_id_list(path_edges)
            row["path_exists"] = True
            row["path_cost"] = float(path_cost) if path_cost is not None else math.nan
            row["path_edge_count"] = len(path_ids)
            row["path_edge_sequence"] = "|".join(path_ids)
            row["path_uses_crossing"] = crossing_edge_id in path_ids
            row["path_uses_walkingarea"] = any(
                edge_function(edge) == "walkingarea" for edge in path_edges
            )
            if crossing_edge_id and crossing_edge_id not in path_ids:
                errors.append("path_missing_crossing_edge")
            if not row["path_uses_walkingarea"]:
                errors.append("path_missing_walkingarea")

    if errors:
        row["validation_status"] = "invalid_pedestrian_candidate"
        row["invalid_reason"] = ";".join(errors)
    return row


def vehicle_edges_at_crossing(crossing_edge: Any) -> list[str]:
    node = crossing_edge.getFromNode()
    return [edge.getID() for edge in normal_edges_at_node(node, "passenger")]


def approach_lanes_at_crossing(crossing_edge: Any) -> list[str]:
    node = crossing_edge.getFromNode()
    lanes: list[str] = []
    for edge in node.getIncoming():
        if edge_function(edge) != "normal" or not edge_allows(edge, "passenger"):
            continue
        for lane in edge.getLanes():
            if lane_allows(lane, "passenger"):
                lanes.append(lane.getID())
    if lanes:
        return lanes

    for edge in normal_edges_at_node(node, "passenger"):
        for lane in edge.getLanes():
            if lane_allows(lane, "passenger"):
                lanes.append(lane.getID())
    return lanes


def find_tls_id(net: Any, crossing_edge: Any, xml_root: ET.Element | None = None) -> str | None:
    node = crossing_edge.getFromNode()
    node_id = node.getID()
    # verify if the node is actually a traffic light with logic
    tls_ids = {tls.getID() for tls in net.getTrafficLights()}
    if node.getType() == "traffic_light" and node_id in tls_ids:
        return node_id
    if not tls_ids:
        return None
    # joined TLS fallback: scan connections for from/to/via referencing this node's internal edges.
    # joinedS_A_B TLS IDs don't match node IDs directly, and net.hasNode("joinedS_...") returns False,
    # so the distance fallback below cannot reach them. XML scan is the only reliable path.
    if xml_root is not None:
        node_prefix = f":{node_id}_"
        for conn in xml_root.findall("connection"):
            tl = conn.attrib.get("tl")
            if not tl or tl not in tls_ids:
                continue
            from_edge = conn.attrib.get("from", "")
            to_edge = conn.attrib.get("to", "")
            via_edge = conn.attrib.get("via", "")
            if (
                from_edge.startswith(node_prefix)
                or to_edge.startswith(node_prefix)
                or via_edge.startswith(node_prefix)
            ):
                return tl
    node_xy = node.getCoord()
    closest_id = min(
        tls_ids,
        key=lambda tls_id: math.dist(node_xy, net.getNode(tls_id).getCoord())
        if net.hasNode(tls_id)
        else float("inf"),
    )
    # also check distance for the fallback closest TL
    if closest_id and net.hasNode(closest_id):
        dist = math.dist(node_xy, net.getNode(closest_id).getCoord())
        if dist < 50: # Only fallback if within 50m
            return closest_id
    return None


def pedestrian_link_indices(
    net_file: str | Path,
    tl_id: str | None,
    crossing_edge_id: str,
    xml_root: ET.Element | None = None,
) -> list[int]:
    if not tl_id:
        return []
    root = xml_root if xml_root is not None else ET.parse(net_file).getroot()
    edge_functions = {
        edge.attrib["id"]: edge.attrib.get("function", "normal")
        for edge in root.findall("edge")
    }
    indices = []
    for conn in root.findall("connection"):
        if conn.attrib.get("tl") != tl_id:
            continue
        from_edge = conn.attrib.get("from", "")
        to_edge = conn.attrib.get("to", "")
        # Only include connections that directly involve this specific crossing_edge
        # (walkingarea→crossing or crossing→walkingarea). The old broad condition
        # edge_functions.get(from/to) == "crossing" captured all crossings under the
        # TLS, not just the target one, and silently returned [] when edge_functions
        # lookup missed internal edges.
        if to_edge != crossing_edge_id and from_edge != crossing_edge_id:
            continue
        from_fn = edge_functions.get(from_edge, "normal")
        to_fn = edge_functions.get(to_edge, "normal")
        if from_fn not in {"walkingarea", "crossing"} and to_fn not in {"walkingarea", "crossing"}:
            continue
        link_index = conn.attrib.get("linkIndex")
        if link_index is not None:
            indices.append(int(link_index))
    return sorted(set(indices))


def discover_network_metadata_from_net(
    net: Any,
    net_file: str | Path,
    lon: float,
    lat: float,
    cw_id: str | int | None = None,
    xml_root: ET.Element | None = None,
) -> dict[str, Any]:
    crossing_edge = choose_crossing_edge(net, float(lon), float(lat))
    crossing_xy = edge_center(crossing_edge)
    try:
        crossing_lon, crossing_lat = net.convertXY2LonLat(*crossing_xy)
    except Exception:
        crossing_lon, crossing_lat = None, None
    tl_id = find_tls_id(net, crossing_edge, xml_root=xml_root)
    ped_route = pedestrian_route_from_crossing(crossing_edge)
    vehicle_edges = vehicle_edges_at_crossing(crossing_edge)
    approach_lanes = approach_lanes_at_crossing(crossing_edge)

    if not vehicle_edges:
        raise RuntimeError(f"{crossing_edge.getID()} 주변 차량 edge를 찾지 못했습니다.")
    if not approach_lanes:
        raise RuntimeError(f"{crossing_edge.getID()} 주변 접근 차선을 찾지 못했습니다.")

    return {
        "cw_id": str(cw_id) if cw_id is not None else None,
        "net_file": str(net_file),
        "crossing_edge": crossing_edge.getID(),
        "crossing_lon": float(crossing_lon) if crossing_lon is not None else None,
        "crossing_lat": float(crossing_lat) if crossing_lat is not None else None,
        "tls_id": tl_id,
        "ped_link_indices": pedestrian_link_indices(net_file, tl_id, crossing_edge.getID(), xml_root=xml_root),
        "ped_route": ped_route,
        "vehicle_conflict_edges": vehicle_edges,
        "approach_lanes": approach_lanes,
    }


def discover_network_metadata(net_file: str | Path, lon: float, lat: float, cw_id: str | int | None = None) -> dict[str, Any]:
    net = read_net(net_file)
    return discover_network_metadata_from_net(net, net_file, lon, lat, cw_id=cw_id)


def load_metadata(path: str | Path) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def save_metadata(path: str | Path, metadata: dict[str, Any]) -> None:
    Path(path).write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def recent_values(values: deque[float], now: float, window: float) -> list[float]:
    while values and now - values[0] > window:
        values.popleft()
    return list(values)
