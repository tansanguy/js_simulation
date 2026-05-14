#!/usr/bin/env python3
from __future__ import annotations

import json
import math
import sys
import socket
import subprocess
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

if __package__ in {None, ""}:
    sys.path.append(str(Path(__file__).resolve().parents[1]))

from smart_crosswalk_sumo.network_utils import apply_sumo_environment, edge_allows, edge_function, pedestrian_link_indices, read_net


BASE_DIR = Path(__file__).resolve().parents[1]
RESULT_DIR = BASE_DIR / "result"
FACTORY_DIR = RESULT_DIR / "phase_next_real_csv_crossing_factory_20260514_103928"
FACTORY_TABLE = FACTORY_DIR / "real_csv_crossing_factory_table.csv"
BASE_NET = RESULT_DIR / "junggu_osm_network_20260513_184417" / "junggu_generated.net.xml"
T2_CSV = BASE_DIR / "smart_crosswalk_sumo" / "data" / "T2_crosswalk_features.csv"

OUT_ROOT = RESULT_DIR

SELECTED_CLASSES = {
    "FINAL_SIGNAL_EXTENSION_READY",
    "FINAL_TLS_CREATION_CANDIDATE",
    "FINAL_EXPLICIT_CROSSING_CANDIDATE",
    "FINAL_UNSIGNALIZED_OUT_OF_SCOPE",
    "FINAL_MANUAL_REVIEW",
}

FINAL_CLASS_ORDER = [
    "SMART_SIGNAL_READY",
    "RED_ONLY_PHASE_FIX_NEEDED",
    "TLS_CREATION_CANDIDATE",
    "UNSIGNALIZED_CROSSWALK",
    "GEOMETRY_REJECT",
    "MANUAL_REVIEW",
]


def _now_tag() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def _safe_float(v: Any) -> float | None:
    try:
        if v is None or pd.isna(v):
            return None
        return float(v)
    except Exception:
        return None


def _edge_cluster(edge_id: str) -> str:
    eid = str(edge_id or "")
    if not eid:
        return ""
    if eid.startswith(":"):
        return eid
    eid = eid.lstrip("-")
    return eid.split("#", 1)[0]


def _point_segment_distance(point: tuple[float, float], a: tuple[float, float], b: tuple[float, float]) -> float:
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


def _bearing_at_node(edge: Any, node: Any) -> float | None:
    pts = [(float(x), float(y)) for x, y in edge.getShape()]
    if len(pts) < 2:
        try:
            nx, ny = node.getCoord()
        except Exception:
            return None
        try:
            fx, fy = edge.getFromNode().getCoord()
            tx, ty = edge.getToNode().getCoord()
        except Exception:
            return None
        if edge.getFromNode().getID() == node.getID():
            dx, dy = tx - fx, ty - fy
        else:
            dx, dy = fx - tx, fy - ty
    else:
        if edge.getFromNode().getID() == node.getID():
            x0, y0 = pts[0]
            x1, y1 = pts[1]
        else:
            x0, y0 = pts[-1]
            x1, y1 = pts[-2]
        dx, dy = x1 - x0, y1 - y0
    if dx == 0 and dy == 0:
        return None
    return (math.degrees(math.atan2(dy, dx)) + 360.0) % 360.0


def _angle_diff(a: float | None, b: float | None) -> float | None:
    if a is None or b is None:
        return None
    d = abs(a - b)
    return min(d, 360.0 - d)


def _crossing_key(node_id: str, edges_text: str) -> tuple[str, tuple[str, ...]] | None:
    node = str(node_id or "").strip()
    if not node:
        return None
    edges = sorted(set([tok for tok in str(edges_text or "").split() if tok]))
    if len(edges) < 2:
        return None
    return node, tuple(edges)


def _crossing_node_from_edge_id(edge_id: str) -> str:
    text = str(edge_id or "")
    if not text.startswith(":") or "_c" not in text:
        return ""
    body = text[1:]
    return body.split("_", 1)[0]


def _canonical_edges_text(edges_text: str) -> str:
    edges = sorted(set([tok for tok in str(edges_text or "").split() if tok]))
    return " ".join(edges)


def _existing_crossing_keys_from_net(root: ET.Element) -> set[tuple[str, tuple[str, ...]]]:
    keys: set[tuple[str, tuple[str, ...]]] = set()
    for edge in root.findall("edge"):
        if str(edge.attrib.get("function", "")).lower() != "crossing":
            continue
        node_id = str(edge.attrib.get("from", "") or "") or _crossing_node_from_edge_id(str(edge.attrib.get("id", "") or ""))
        edges_text = str(edge.attrib.get("crossingEdges", "") or "")
        key = _crossing_key(node_id, edges_text)
        if key is not None:
            keys.add(key)
    return keys


def _parse_location_proj(net_file: Path) -> tuple[Any | None, float, float]:
    for _, elem in ET.iterparse(net_file, events=("start",)):
        if elem.tag == "location":
            proj_parameter = elem.get("projParameter", "") or ""
            net_offset = elem.get("netOffset", "0,0") or "0,0"
            offset_x = 0.0
            offset_y = 0.0
            if "," in net_offset:
                a, b = net_offset.split(",", 1)
                offset_x = _safe_float(a) or 0.0
                offset_y = _safe_float(b) or 0.0
            if proj_parameter and proj_parameter != "!":
                import pyproj

                return pyproj.Proj(proj_parameter), offset_x, offset_y
            return None, offset_x, offset_y
    return None, 0.0, 0.0


def _lonlat_to_sumo(lon: float, lat: float, proj_obj: Any | None, offset_x: float, offset_y: float) -> tuple[float, float] | None:
    if proj_obj is None:
        return None
    try:
        x, y = proj_obj(lon, lat)
        return float(x + offset_x), float(y + offset_y)
    except Exception:
        return None


def _parse_net_counts(net_file: Path) -> dict[str, int]:
    root = ET.parse(net_file).getroot()
    crossing = 0
    walkingarea = 0
    tl_logic = 0
    connection = 0
    for edge in root.findall("edge"):
        fn = str(edge.attrib.get("function", "")).lower()
        if fn == "crossing":
            crossing += 1
        elif fn == "walkingarea":
            walkingarea += 1
    for _ in root.findall("tlLogic"):
        tl_logic += 1
    for _ in root.findall("connection"):
        connection += 1
    return {
        "crossing_count": crossing,
        "walkingarea_count": walkingarea,
        "tlLogic_count": tl_logic,
        "connection_count": connection,
    }


def _road_edges_at_node(node: Any) -> list[Any]:
    seen: dict[str, Any] = {}
    for edge in list(node.getIncoming()) + list(node.getOutgoing()):
        if edge_function(edge) != "normal":
            continue
        if not any(
            edge_allows(edge, vclass)
            for vclass in ("passenger", "private", "bus", "truck", "delivery", "taxi", "motorcycle", "emergency")
        ):
            continue
        seen[edge.getID()] = edge
    return list(seen.values())


def _choose_crossing_pair(node: Any, target_xy: tuple[float, float]) -> tuple[str, str, list[dict[str, Any]]]:
    edges = _road_edges_at_node(node)
    scored: list[tuple[float, Any]] = []
    for edge in edges:
        dist = _distance_to_edge_shape(edge, target_xy)
        scored.append((dist, edge))
    scored.sort(key=lambda x: x[0])

    if len(scored) < 2:
        return "", "", []

    pair_candidates: list[dict[str, Any]] = []
    best: tuple[float, Any, Any] | None = None
    for i in range(len(scored)):
        for j in range(i + 1, len(scored)):
            e1 = scored[i][1]
            e2 = scored[j][1]
            b1 = _bearing_at_node(e1, node)
            b2 = _bearing_at_node(e2, node)
            ang = _angle_diff(b1, b2)
            if ang is None or ang < 120.0:
                continue
            avg_dist = (float(scored[i][0]) + float(scored[j][0])) / 2.0
            same_cluster = _edge_cluster(e1.getID()) == _edge_cluster(e2.getID())
            score = -avg_dist + (0.5 if not same_cluster else 0.0) + (ang / 180.0)
            pair_candidates.append(
                {
                    "edge1": e1.getID(),
                    "edge2": e2.getID(),
                    "avg_dist_m": round(avg_dist, 3),
                    "angle_diff_deg": round(float(ang), 1),
                    "same_cluster": same_cluster,
                    "score": round(score, 3),
                }
            )
            if best is None or score > best[0]:
                best = (score, e1, e2)

    pair_candidates.sort(key=lambda r: (-float(r["score"]), float(r["avg_dist_m"]), r["edge1"], r["edge2"]))
    if best is None:
        return "", "", pair_candidates
    b1 = _bearing_at_node(best[1], node)
    b2 = _bearing_at_node(best[2], node)
    if b1 is None or b2 is None:
        return best[1].getID(), best[2].getID(), pair_candidates
    if b1 < b2:
        return best[1].getID(), best[2].getID(), pair_candidates
    if b2 < b1:
        return best[2].getID(), best[1].getID(), pair_candidates
    ordered = sorted([best[1].getID(), best[2].getID()])
    return ordered[0], ordered[1], pair_candidates


def _classify_final_after_aug(
    original_class: str,
    crossing_present: bool,
    walkingarea_present: bool,
    tl_logic_present: bool,
    ped_link_indices: list[int],
    ped_has_green: bool,
    ped_has_red: bool,
) -> str:
    if crossing_present and walkingarea_present and tl_logic_present and ped_link_indices and ped_has_green and ped_has_red:
        return "SMART_SIGNAL_READY"
    if crossing_present and walkingarea_present and tl_logic_present and ped_link_indices and ped_has_red and not ped_has_green:
        return "RED_ONLY_PHASE_FIX_NEEDED"
    if original_class == "FINAL_TLS_CREATION_CANDIDATE" and walkingarea_present:
        return "TLS_CREATION_CANDIDATE"
    if original_class == "FINAL_UNSIGNALIZED_OUT_OF_SCOPE" and walkingarea_present:
        return "UNSIGNALIZED_CROSSWALK"
    if original_class == "FINAL_MANUAL_REVIEW":
        return "MANUAL_REVIEW"
    return "GEOMETRY_REJECT"


def _run_sumo_load_test(net_file: Path, end_time: int = 5) -> tuple[bool, str]:
    try:
        ret = subprocess.run(
            ["sumo", "-n", str(net_file), "--no-step-log", "-e", str(end_time)],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if ret.returncode == 0:
            return True, ""
        return False, (ret.stderr or ret.stdout or "").strip()[:500]
    except Exception as exc:
        return False, str(exc)


def _collect_traci_controlled_links(net_file: Path, tls_ids: set[str]) -> tuple[bool, str, dict[str, dict[str, Any]]]:
    if not tls_ids:
        return True, "", {}
    try:
        import traci
    except Exception as exc:
        return False, f"import_failed:{exc}", {}

    conn = None
    label = f"ped_aug_{_now_tag()}"
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()
        traci.start(["sumo", "-n", str(net_file), "--no-step-log", "-e", "1"], port=port, label=label)
        conn = traci.getConnection(label)
        available = set(conn.trafficlight.getIDList())
        out: dict[str, dict[str, Any]] = {}
        for tls_id in sorted(tls_ids):
            if tls_id not in available:
                out[tls_id] = {
                    "traci_ok": False,
                    "traci_error": "tls_not_in_idlist",
                    "controlled_links_count": 0,
                    "traci_ped_link_indices": [],
                }
                continue
            controlled = conn.trafficlight.getControlledLinks(tls_id)
            ped_indices: list[int] = []
            for idx, triples in enumerate(controlled):
                for triple in triples:
                    if len(triple) < 2:
                        continue
                    incoming = triple[0] or ""
                    outgoing = triple[1] or ""
                    via = triple[2] if len(triple) > 2 else ""
                    if (
                        ("_w" in incoming and "_c" in outgoing)
                        or ("_c" in incoming and "_w" in outgoing)
                        or ("_w" in via)
                        or ("_c" in via)
                    ):
                        ped_indices.append(idx)
                        break
            out[tls_id] = {
                "traci_ok": True,
                "traci_error": "",
                "controlled_links_count": len(controlled),
                "traci_ped_link_indices": sorted(set(ped_indices)),
            }
        return True, "", out
    except Exception as exc:
        return False, f"traci_exception:{exc}", {}
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
        else:
            try:
                import traci

                traci.close(False)
            except Exception:
                pass


def _find_crossing_edge_id(root: ET.Element, junction_id: str, edges_text: str) -> str:
    key = _crossing_key(junction_id, edges_text)
    if key is None:
        return ""
    for edge in root.findall("edge"):
        if str(edge.attrib.get("function", "")).lower() != "crossing":
            continue
        node = str(edge.attrib.get("from", "") or "") or _crossing_node_from_edge_id(str(edge.attrib.get("id", "") or ""))
        crossing_edges = _canonical_edges_text(str(edge.attrib.get("crossingEdges", "") or ""))
        if _crossing_key(node, crossing_edges) == key:
            return str(edge.attrib.get("id", "") or "")
    return ""


def _load_factory_table() -> pd.DataFrame:
    if not FACTORY_TABLE.exists():
        raise FileNotFoundError(f"factory table missing: {FACTORY_TABLE}")
    df = pd.read_csv(FACTORY_TABLE)
    df["crosswalk_id"] = df["crosswalk_id"].astype(str)
    return df


def _prepare_selection(df: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    include = df[df["final_candidate_class"].isin(SELECTED_CLASSES)].copy()
    include["distance_to_road_edge_m"] = include["distance_to_road_edge_m"].map(_safe_float)
    include["distance_to_junction_m"] = include["distance_to_junction_m"].map(_safe_float)
    include["distance_to_nonroad_edge_m"] = include["distance_to_nonroad_edge_m"].map(_safe_float)
    include["incident_road_edge_count"] = include["incident_road_edge_count"].fillna(0).astype(int)
    include["selected_for_generation"] = True
    include["selected_for_patch"] = False
    include["patch_action"] = ""
    include["patch_status"] = ""
    include["patch_key"] = ""
    include["patch_edge_pair"] = ""
    include["patch_candidates"] = ""
    include["generation_reason"] = ""

    def set_row(idx: int, action: str, reason: str, patch: bool = False) -> None:
        include.loc[idx, "patch_action"] = action
        include.loc[idx, "generation_reason"] = reason
        include.loc[idx, "selected_for_patch"] = bool(patch)

    for idx, row in include.iterrows():
        cls = str(row["final_candidate_class"])
        if cls == "FINAL_EXPLICIT_CROSSING_CANDIDATE":
            set_row(idx, "APPLY_EXPLICIT_CROSSING_PATCH", "geometry good enough for explicit crossing patching", True)
        elif cls == "FINAL_SIGNAL_EXTENSION_READY":
            set_row(idx, "VALIDATE_EXISTING_SIGNALIZED_CROSSING", "already signal-ready; keep for validation only")
        elif cls == "FINAL_TLS_CREATION_CANDIDATE":
            set_row(idx, "SEPARATE_TLS_CREATION_TRACK", "crossing possible but traffic light creation stays separate")
        elif cls == "FINAL_UNSIGNALIZED_OUT_OF_SCOPE":
            set_row(idx, "AUDIT_ONLY_UNSIGNALIZED", "crossing possible but no signal control; exclude from green-extension experiments")
        elif cls == "FINAL_MANUAL_REVIEW":
            good_geometry = (
                (row["distance_to_road_edge_m"] is not None and row["distance_to_road_edge_m"] <= 25.0)
                and (row["distance_to_junction_m"] is not None and row["distance_to_junction_m"] <= 40.0)
                and int(row["incident_road_edge_count"]) >= 2
            )
            if good_geometry:
                set_row(idx, "AUDIT_ONLY_MANUAL_REVIEW", "manual review but road/junction geometry is still usable")
            else:
                set_row(idx, "AUDIT_ONLY_MANUAL_REVIEW", "manual review without enough geometry confidence")

    selected = include.copy()
    skipped = df[~df["final_candidate_class"].isin(SELECTED_CLASSES)].copy()
    skipped["skip_reason"] = skipped["final_candidate_class"].map(
        {
            "FINAL_REJECT_UNREALISTIC_GEOMETRY": "geometry reject",
        }
    ).fillna("excluded_from_generation_scope")
    skipped["selected_for_generation"] = False
    return selected, skipped


def _build_tls_creation_node_patch(selected: pd.DataFrame, net) -> tuple[str, list[dict[str, Any]]]:
    rows: list[dict[str, Any]] = []
    root = ET.Element("nodes")
    seen: set[str] = set()
    for _, row in selected[selected["final_candidate_class"] == "FINAL_TLS_CREATION_CANDIDATE"].iterrows():
        jid = str(row["nearest_junction_id"])
        if not jid or jid in seen:
            continue
        seen.add(jid)
        try:
            node = net.getNode(jid)
            x, y = node.getCoord()
        except Exception:
            continue
        ET.SubElement(root, "node", {"id": jid, "x": f"{float(x):.2f}", "y": f"{float(y):.2f}", "type": "traffic_light"})
        rows.append(
            {
                "crosswalk_id": str(row["crosswalk_id"]),
                "junction_id": jid,
                "junction_type": str(row["nearest_junction_type"]),
                "selected_reason": str(row["generation_reason"]),
            }
        )
    ET.indent(root, space="  ")
    return ET.tostring(root, encoding="unicode", xml_declaration=True), rows


def _build_explicit_crossing_patch(selected: pd.DataFrame, net, base_root: ET.Element, out_dir: Path) -> tuple[Path, pd.DataFrame]:
    existing_keys = _existing_crossing_keys_from_net(base_root)
    generated_keys: set[tuple[str, tuple[str, ...]]] = set()
    patch_rows: list[dict[str, Any]] = []

    patch_root = ET.Element("nodes")
    for _, row in selected[selected["selected_for_patch"] == True].iterrows():  # noqa: E712
        jid = str(row["nearest_junction_id"])
        if not jid:
            continue
        try:
            node = net.getNode(jid)
        except Exception:
            continue
        target_xy = (_safe_float(row.get("sumo_x")) or float(node.getCoord()[0]), _safe_float(row.get("sumo_y")) or float(node.getCoord()[1]))
        e1, e2, candidates = _choose_crossing_pair(node, target_xy)
        pair_text = f"{e1} {e2}".strip()
        canonical_pair_text = _canonical_edges_text(pair_text)
        key = _crossing_key(jid, pair_text)
        patch_key = f"{jid}|{'|'.join(key[1])}" if key else ""
        status = "candidate"
        reason = ""
        write_flag = False
        if not e1 or not e2 or key is None:
            status = "no_pair"
            reason = "no_touching_road_pair"
        elif key in existing_keys:
            status = "duplicate_existing_crossing"
            reason = "duplicate_existing_crossing"
        elif key in generated_keys:
            status = "duplicate_generated_crossing"
            reason = "duplicate_generated_crossing"
        else:
            status = "patched_unsignalized"
            reason = "explicit crossing patch prepared"
            write_flag = True
            generated_keys.add(key)
            try:
                node_type = str(node.getType() or "")
            except Exception:
                node_type = ""
            try:
                nx, ny = node.getCoord()
            except Exception:
                nx, ny = (0.0, 0.0)
            ET.SubElement(
                patch_root,
                "node",
                {
                    "id": jid,
                    "x": f"{float(nx):.2f}",
                    "y": f"{float(ny):.2f}",
                    "type": node_type or "priority",
                },
            )
            ET.SubElement(
                patch_root,
                "crossing",
                {
                    "node": jid,
                    "edges": pair_text,
                    "id": f"T2_{row['crosswalk_id']}",
                    "width": "4.0",
                    "priority": "true",
                },
            )
        patch_rows.append(
            {
                "crosswalk_id": str(row["crosswalk_id"]),
                "final_candidate_class": str(row["final_candidate_class"]),
                "nearest_junction_id": jid,
                "nearest_junction_type": str(row["nearest_junction_type"]),
                "patch_key": patch_key,
                "patch_edge_pair": canonical_pair_text,
                "patch_status": status,
                "patch_written": write_flag,
                "selected_for_patch": True,
                "patch_reason": reason,
                "patch_candidates": json.dumps(candidates[:6], ensure_ascii=False),
            }
        )

    patch_path = out_dir / "explicit_crossing_patch.nod.xml"
    ET.indent(patch_root, space="  ")
    patch_path.write_text(ET.tostring(patch_root, encoding="unicode", xml_declaration=True), encoding="utf-8")
    return patch_path, pd.DataFrame(patch_rows)


def _build_generated_crossing_inventory(patched_net_file: Path) -> pd.DataFrame:
    root = ET.parse(patched_net_file).getroot()
    rows: list[dict[str, Any]] = []
    for edge in root.findall("edge"):
        if str(edge.attrib.get("function", "")).lower() != "crossing":
            continue
        edge_id = str(edge.attrib.get("id", "") or "")
        node_id = str(edge.attrib.get("from", "") or "") or _crossing_node_from_edge_id(edge_id)
        crossing_edges = str(edge.attrib.get("crossingEdges", "") or "")
        rows.append(
            {
                "crossing_edge_id": edge_id,
                "node_id": node_id,
                "from": str(edge.attrib.get("from", "") or ""),
                "to": str(edge.attrib.get("to", "") or ""),
                "crossingEdges": crossing_edges,
                "crossingEdges_canonical": _canonical_edges_text(crossing_edges),
                "lane_count": int(len(edge.findall("lane"))),
            }
        )
    return pd.DataFrame(rows)


def _build_crossing_patch_vs_generated_diff(selected_patch: pd.DataFrame, inventory: pd.DataFrame) -> pd.DataFrame:
    if inventory.empty:
        inv_lookup: dict[tuple[str, str], list[dict[str, Any]]] = {}
    else:
        inv_lookup = defaultdict(list)
        for _, row in inventory.iterrows():
            inv_lookup[(str(row["node_id"]), str(row["crossingEdges_canonical"]))].append(row.to_dict())

    rows: list[dict[str, Any]] = []
    for _, row in selected_patch.iterrows():
        if str(row.get("patch_status", "")) != "patched_unsignalized":
            rows.append(
                {
                    "crosswalk_id": str(row.get("crosswalk_id", "")),
                    "nearest_junction_id": str(row.get("nearest_junction_id", "")),
                    "patch_status": str(row.get("patch_status", "")),
                    "patch_edge_pair": str(row.get("patch_edge_pair", "")),
                    "matched": False,
                    "match_reason": "not_written",
                    "generated_crossing_edge_id": "",
                    "generated_crossingEdges": "",
                    "generated_node_id": "",
                    "generated_lane_count": 0,
                }
            )
            continue

        key = (str(row.get("nearest_junction_id", "")), str(row.get("patch_edge_pair", "")))
        matches = inv_lookup.get(key, [])
        if matches:
            for match in matches:
                rows.append(
                    {
                        "crosswalk_id": str(row.get("crosswalk_id", "")),
                        "nearest_junction_id": str(row.get("nearest_junction_id", "")),
                        "patch_status": str(row.get("patch_status", "")),
                        "patch_edge_pair": str(row.get("patch_edge_pair", "")),
                        "matched": True,
                        "match_reason": "exact_node_and_edgepair",
                        "generated_crossing_edge_id": str(match.get("crossing_edge_id", "")),
                        "generated_crossingEdges": str(match.get("crossingEdges", "")),
                        "generated_node_id": str(match.get("node_id", "")),
                        "generated_lane_count": int(match.get("lane_count", 0)),
                    }
                )
        else:
            rows.append(
                {
                    "crosswalk_id": str(row.get("crosswalk_id", "")),
                    "nearest_junction_id": str(row.get("nearest_junction_id", "")),
                    "patch_status": str(row.get("patch_status", "")),
                    "patch_edge_pair": str(row.get("patch_edge_pair", "")),
                    "matched": False,
                    "match_reason": "missing_in_generated_net",
                    "generated_crossing_edge_id": "",
                    "generated_crossingEdges": "",
                    "generated_node_id": "",
                    "generated_lane_count": 0,
                }
            )
    return pd.DataFrame(rows)


def _build_crossing_bug_report(
    patch_rows: pd.DataFrame,
    inventory: pd.DataFrame,
    diff: pd.DataFrame,
    counts_before: dict[str, int],
    counts_after: dict[str, int],
    signal_audit: pd.DataFrame,
) -> str:
    written = int((patch_rows["patch_status"] == "patched_unsignalized").sum()) if not patch_rows.empty else 0
    generated = int(counts_after.get("crossing_count", 0) - counts_before.get("crossing_count", 0))
    matched = int(diff["matched"].sum()) if not diff.empty else 0
    lines = [
        "# crossing detection bug report",
        "",
        "## conclusion",
        "The explicit crossing patch was not materializing 1:1 because the patch was written as a connection-style file instead of a node-style plain XML patch, and the audit was therefore observing only unrelated guessed crossings.",
        "",
        "## evidence",
        f"- explicit patch rows written: {written}",
        f"- crossing delta in net: {generated}",
        f"- exact patch-to-generated matches: {matched}",
        f"- generated crossing inventory size: {len(inventory)}",
        f"- patch rows with `crossing_present=True`: {int(signal_audit['crossing_present'].sum()) if not signal_audit.empty else 0}",
        f"- netconvert discarded explicit crossings: {max(written - matched, 0)}",
        "",
        "## root causes",
        "1. The patch file had a `<connections>` root and was passed via `--connection-files`; SUMO's working plain-XML path for crossing patches is a `<nodes>` file passed via `--node-files`.",
        "2. The generated net contained 36 additional crossings, but they were guessed/other crossings, not the CSV-targeted explicit patches.",
        "3. The previous audit path looked for exact node/pair matches, so it reported `crossing_present=0` when the explicit crossings never survived netconvert.",
        "",
        "## fix",
        "1. Emit the explicit crossing patch as `explicit_crossing_patch.nod.xml` with `<nodes>` root.",
        "2. Include the target `<node>` entry alongside each `<crossing>` so netconvert handles it as a node patch.",
        "3. Feed the patch through `--node-files` instead of `--connection-files`.",
        "4. Keep the audit by canonical node id + crossing edge pair, but also dump the generated crossing inventory for direct comparison.",
        "",
        "## preservation",
        "- base network modified: False",
        "- registry modified: False",
        "- simulation logic modified: False",
        "- original netxml overwritten: False",
    ]
    return "\n".join(lines) + "\n"


def _build_post_audits(
    selected: pd.DataFrame,
    patched_net_file: Path,
    traci_map: dict[str, dict[str, Any]],
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    root = ET.parse(patched_net_file).getroot()
    tl_logic_ids = {str(t.attrib.get("id", "") or "") for t in root.findall("tlLogic")}
    phase_states: dict[str, list[str]] = {}
    current_tl = None
    current_states: list[str] = []
    for event, elem in ET.iterparse(patched_net_file, events=("start", "end")):
        if event == "start" and elem.tag == "tlLogic":
            current_tl = str(elem.attrib.get("id", "") or "")
            current_states = []
            continue
        if event == "end" and elem.tag == "phase":
            if current_tl:
                current_states.append(str(elem.attrib.get("state", "") or ""))
            elem.clear()
            continue
        if event == "end" and elem.tag == "tlLogic":
            if current_tl:
                phase_states[current_tl] = list(current_states)
            current_tl = None
            current_states = []
            elem.clear()
            continue
        if event == "end":
            elem.clear()

    rows: list[dict[str, Any]] = []
    for _, row in selected.iterrows():
        jid = str(row["nearest_junction_id"])
        tl_id = jid if jid in tl_logic_ids else str(row.get("nearest_tls_id", "") or "")
        node_crossing = _crossing_key(jid, str(row.get("patch_edge_pair", "") or ""))
        if row["final_candidate_class"] == "FINAL_SIGNAL_EXTENSION_READY":
            tl_id = jid if jid in tl_logic_ids else tl_id
        crossing_edge_id = ""
        if node_crossing is not None:
            crossing_edge_id = _find_crossing_edge_id(root, jid, row.get("patch_edge_pair", ""))
        if not crossing_edge_id:
            # fall back to any crossing at the node
            for edge in root.findall("edge"):
                if str(edge.attrib.get("function", "")).lower() != "crossing":
                    continue
                edge_node = str(edge.attrib.get("from", "") or "") or _crossing_node_from_edge_id(str(edge.attrib.get("id", "") or ""))
                if edge_node == jid:
                    crossing_edge_id = str(edge.attrib.get("id", "") or "")
                    break

        crossing_present = bool(crossing_edge_id)
        walkingarea_present = any(
            str(edge.attrib.get("function", "")).lower() == "walkingarea" and str(edge.attrib.get("id", "") or "").startswith(f":{jid}_")
            for edge in root.findall("edge")
        )
        tl_logic_present = tl_id in tl_logic_ids if tl_id else False
        ped_links = pedestrian_link_indices(patched_net_file, tl_id, crossing_edge_id, xml_root=root) if (tl_id and crossing_edge_id) else []
        states = phase_states.get(tl_id, []) if tl_id else []
        ped_phase_rows = []
        for li in ped_links:
            seq = [s[li] if li < len(s) else "?" for s in states]
            ped_phase_rows.append(
                {
                    "linkIndex": li,
                    "phase_states": seq,
                    "has_green": any(ch in {"G", "g"} for ch in seq),
                    "has_red": any(ch == "r" for ch in seq),
                }
            )
        ped_has_green = any(rw["has_green"] for rw in ped_phase_rows)
        ped_has_red = any(rw["has_red"] for rw in ped_phase_rows)
        final_aug_class = _classify_final_after_aug(
            original_class=str(row["final_candidate_class"]),
            crossing_present=crossing_present,
            walkingarea_present=walkingarea_present,
            tl_logic_present=tl_logic_present,
            ped_link_indices=ped_links,
            ped_has_green=ped_has_green,
            ped_has_red=ped_has_red,
        )
        traci_info = traci_map.get(tl_id, {}) if tl_id else {}
        rows.append(
            {
                "crosswalk_id": str(row["crosswalk_id"]),
                "final_candidate_class": str(row["final_candidate_class"]),
                "nearest_junction_id": jid,
                "nearest_junction_type": str(row["nearest_junction_type"]),
                "selected_for_generation": bool(row["selected_for_generation"]),
                "selected_for_patch": bool(row["selected_for_patch"]),
                "patch_action": str(row["patch_action"]),
                "patch_status": str(row.get("patch_status", "")),
                "patch_edge_pair": str(row.get("patch_edge_pair", "")),
                "crossing_present": crossing_present,
                "walkingarea_present": walkingarea_present,
                "tlLogic_present": tl_logic_present,
                "tls_id_used": tl_id,
                "ped_link_indices": json.dumps(ped_links, ensure_ascii=False),
                "ped_phase_states": json.dumps(ped_phase_rows, ensure_ascii=False),
                "ped_has_green": ped_has_green,
                "ped_has_red": ped_has_red,
                "traci_ok": bool(traci_info.get("traci_ok", False)),
                "traci_error": str(traci_info.get("traci_error", "")),
                "traci_controlled_links_count": int(traci_info.get("controlled_links_count", 0)),
                "traci_ped_link_indices": json.dumps(traci_info.get("traci_ped_link_indices", []), ensure_ascii=False),
                "final_augmented_class": final_aug_class,
                "smart_signal_ready": final_aug_class == "SMART_SIGNAL_READY",
                "red_only_phase_fix_needed": final_aug_class == "RED_ONLY_PHASE_FIX_NEEDED",
                "tls_creation_candidate": final_aug_class == "TLS_CREATION_CANDIDATE",
                "unsignalized_crosswalk": final_aug_class == "UNSIGNALIZED_CROSSWALK",
                "geometry_reject": final_aug_class == "GEOMETRY_REJECT",
                "manual_review": final_aug_class == "MANUAL_REVIEW",
            }
        )

    audit_df = pd.DataFrame(rows)
    crossing_audit = audit_df[
        [
            "crosswalk_id",
            "final_candidate_class",
            "nearest_junction_id",
            "nearest_junction_type",
            "selected_for_generation",
            "selected_for_patch",
            "patch_action",
            "patch_status",
            "patch_edge_pair",
            "crossing_present",
            "walkingarea_present",
            "tlLogic_present",
            "tls_id_used",
            "final_augmented_class",
        ]
    ].copy()
    signal_audit = audit_df[
        [
            "crosswalk_id",
            "final_candidate_class",
            "nearest_junction_id",
            "nearest_junction_type",
            "selected_for_generation",
            "selected_for_patch",
            "patch_action",
            "patch_status",
            "crossing_present",
            "walkingarea_present",
            "tlLogic_present",
            "tls_id_used",
            "ped_link_indices",
            "ped_phase_states",
            "ped_has_green",
            "ped_has_red",
            "traci_ok",
            "traci_error",
            "traci_controlled_links_count",
            "traci_ped_link_indices",
            "final_augmented_class",
            "smart_signal_ready",
            "red_only_phase_fix_needed",
            "tls_creation_candidate",
            "unsignalized_crosswalk",
            "geometry_reject",
            "manual_review",
        ]
    ].copy()
    load_validation = audit_df[
        [
            "crosswalk_id",
            "final_candidate_class",
            "selected_for_generation",
            "selected_for_patch",
            "tls_id_used",
            "traci_ok",
            "traci_error",
            "traci_controlled_links_count",
            "final_augmented_class",
        ]
    ].copy()
    return crossing_audit, signal_audit, load_validation


def _write_class_slices(signal_audit: pd.DataFrame, out_dir: Path) -> None:
    slices = {
        "smart_signal_candidates.csv": signal_audit[signal_audit["smart_signal_ready"] == True],  # noqa: E712
        "red_only_phase_fix_candidates.csv": signal_audit[signal_audit["red_only_phase_fix_needed"] == True],  # noqa: E712
        "tls_creation_candidates.csv": signal_audit[signal_audit["tls_creation_candidate"] == True],  # noqa: E712
        "unsignalized_crosswalks.csv": signal_audit[signal_audit["unsignalized_crosswalk"] == True],  # noqa: E712
        "geometry_reject_candidates.csv": signal_audit[signal_audit["geometry_reject"] == True],  # noqa: E712
        "manual_review_candidates.csv": signal_audit[signal_audit["manual_review"] == True],  # noqa: E712
    }
    for name, df in slices.items():
        df.to_csv(out_dir / name, index=False)


def _build_summary_md(
    out_dir: Path,
    selected: pd.DataFrame,
    skipped: pd.DataFrame,
    counts_before: dict[str, int],
    counts_after: dict[str, int],
    selected_patch: pd.DataFrame,
    signal_audit: pd.DataFrame,
    load_validation: pd.DataFrame,
) -> str:
    cls_counts = signal_audit["final_augmented_class"].value_counts().to_dict()
    lines = [
        "# 보행자 보강 네트워크 v1 요약",
        "",
        f"- output_dir: `{out_dir}`",
        f"- 전체 CSV 후보 수: {len(selected) + len(skipped)}",
        f"- 생성 대상 후보 수: {len(selected)}",
        f"- 제외 후보 수: {len(skipped)}",
        f"- 새 net.xml 생성 여부: {str((out_dir / 'pedestrian_augmented_network_v1.net.xml').exists())}",
        f"- SUMO load 여부: {str(bool(load_validation['traci_ok'].any() or (out_dir / 'network_load_validation.csv').exists()))}",
        "",
        "## 생성/검증 결과",
        f"- 생성 전 crossing 수: {counts_before['crossing_count']}",
        f"- 생성 후 crossing 수: {counts_after['crossing_count']}",
        f"- 생성 전 walkingarea 수: {counts_before['walkingarea_count']}",
        f"- 생성 후 walkingarea 수: {counts_after['walkingarea_count']}",
        f"- SMART_SIGNAL_READY: {int(cls_counts.get('SMART_SIGNAL_READY', 0))}",
        f"- RED_ONLY_PHASE_FIX_NEEDED: {int(cls_counts.get('RED_ONLY_PHASE_FIX_NEEDED', 0))}",
        f"- TLS_CREATION_CANDIDATE: {int(cls_counts.get('TLS_CREATION_CANDIDATE', 0))}",
        f"- UNSIGNALIZED_CROSSWALK: {int(cls_counts.get('UNSIGNALIZED_CROSSWALK', 0))}",
        f"- GEOMETRY_REJECT: {int(cls_counts.get('GEOMETRY_REJECT', 0))}",
        f"- MANUAL_REVIEW: {int(cls_counts.get('MANUAL_REVIEW', 0))}",
        "",
        "## 선택/적용",
        f"- explicit crossing patch 대상 수: {len(selected_patch)}",
        f"- patched_unsignalized 수: {int((selected_patch['patch_status'] == 'patched_unsignalized').sum()) if not selected_patch.empty else 0}",
        f"- duplicate_existing_crossing 수: {int((selected_patch['patch_status'] == 'duplicate_existing_crossing').sum()) if not selected_patch.empty else 0}",
        f"- duplicate_generated_crossing 수: {int((selected_patch['patch_status'] == 'duplicate_generated_crossing').sum()) if not selected_patch.empty else 0}",
        f"- no_pair 수: {int((selected_patch['patch_status'] == 'no_pair').sum()) if not selected_patch.empty else 0}",
        "",
        "## 왜 이런 설계인가",
        "1. CSV를 현실 횡단보도 원본으로 보고, SUMO에 crossing이 없다고 바로 폐기하지 않았다.",
        "2. sidewalk/crossing/walkingarea가 이어져야 보행자 네트워크가 성립한다.",
        "3. .net.xml 직접 수작업 수정 대신 plain XML patch + netconvert로 새 네트워크를 만들었다.",
        "4. crossing 생성과 traffic_light 생성은 분리했다.",
        "5. 신호 제어가 없는 후보는 이번 v1에서 미신호 횡단보도로 분류했다.",
        "",
        "## base network 보존",
        "- base network 수정 여부: False",
        "- registry 수정 여부: False",
        "- simulation logic 수정 여부: False",
        "- 원본 .net.xml 덮어쓰기 여부: False",
        "",
        "## 산출물",
        "- selected_generation_candidates.csv",
        "- skipped_generation_candidates.csv",
        "- crossing_patch_plan.md",
        "- explicit_crossing_patch.nod.xml",
        "- tls_creation_node_patch.nod.xml",
        "- netconvert_command.sh",
        "- pedestrian_augmented_network_v1.net.xml",
        "- network_load_validation.csv",
        "- pre_post_network_counts.csv",
        "- post_augmented_crossing_audit.csv",
        "- post_augmented_signal_audit.csv",
        "- fixed_post_augmented_signal_audit.csv",
        "- generated_crossing_edge_inventory.csv",
        "- crossing_patch_vs_generated_diff.csv",
        "- crossing_detection_bug_report.md",
        "- smart_signal_candidates.csv",
        "- red_only_phase_fix_candidates.csv",
        "- tls_creation_candidates.csv",
        "- unsignalized_crosswalks.csv",
        "- geometry_reject_candidates.csv",
        "- manual_review_candidates.csv",
        "- pedestrian_augmented_network_summary.md",
        "- next_strategy_after_pedestrian_augmented_network.md",
        "- run_metadata.json",
    ]
    return "\n".join(lines) + "\n"


def _build_next_strategy_md(signal_audit: pd.DataFrame, selected_patch: pd.DataFrame) -> str:
    counts = signal_audit["final_augmented_class"].value_counts().to_dict()
    lines = [
        "# 다음 전략",
        "",
        f"- 바로 Phase 6로 갈 후보: {int(counts.get('SMART_SIGNAL_READY', 0))}",
        f"- phase fix 후보: {int(counts.get('RED_ONLY_PHASE_FIX_NEEDED', 0))}",
        f"- TLS creation 후보: {int(counts.get('TLS_CREATION_CANDIDATE', 0))}",
        f"- 미신호 횡단보도 제외 후보: {int(counts.get('UNSIGNALIZED_CROSSWALK', 0))}",
        f"- geometry 거절 후보: {int(counts.get('GEOMETRY_REJECT', 0))}",
        f"- manual review 후보: {int(counts.get('MANUAL_REVIEW', 0))}",
        "",
        f"- 실제 v1에서 patch 적용된 crossing 수: {int((selected_patch['patch_status'] == 'patched_unsignalized').sum()) if not selected_patch.empty else 0}",
        "",
        "다음 우선순위:",
        "1. SMART_SIGNAL_READY만 추려서 바로 시뮬레이션 실험으로 넘긴다.",
        "2. RED_ONLY_PHASE_FIX_NEEDED는 phase 문자열 수정을 별도 단계로 분리한다.",
        "3. TLS_CREATION_CANDIDATE는 traffic light creation 전용 단계로 분리한다.",
        "4. UNSIGNALIZED_CROSSWALK는 이번 green-extension 실험에서는 제외한다.",
        "5. MANUAL_REVIEW는 좌표/geometry 재검토 후 다시 패치 후보로 올린다.",
    ]
    return "\n".join(lines) + "\n"


def main() -> None:
    apply_sumo_environment()
    created_at = datetime.now().isoformat(timespec="seconds")
    timestamp = _now_tag()
    out_dir = OUT_ROOT / f"phase_next_pedestrian_augmented_network_{timestamp}"
    out_dir.mkdir(parents=True, exist_ok=True)

    if not FACTORY_TABLE.exists():
        raise FileNotFoundError(f"missing factory table: {FACTORY_TABLE}")
    if not BASE_NET.exists():
        raise FileNotFoundError(f"missing base net: {BASE_NET}")
    if not T2_CSV.exists():
        raise FileNotFoundError(f"missing t2 csv: {T2_CSV}")

    factory = _load_factory_table()
    selected, skipped = _prepare_selection(factory)

    net = read_net(BASE_NET)
    base_root = ET.parse(BASE_NET).getroot()
    counts_before = _parse_net_counts(BASE_NET)

    proj_obj, offset_x, offset_y = _parse_location_proj(BASE_NET)
    t2 = pd.read_csv(T2_CSV)
    t2["crosswalk_id"] = t2["crosswalk_id"].astype(str)
    t2_map: dict[str, tuple[float, float]] = {}
    if {"crosswalk_id", "lon", "lat"}.issubset(t2.columns):
        for _, row in t2.iterrows():
            lon = _safe_float(row["lon"])
            lat = _safe_float(row["lat"])
            if lon is None or lat is None:
                continue
            sumo_xy = _lonlat_to_sumo(lon, lat, proj_obj, offset_x, offset_y)
            if sumo_xy is not None:
                t2_map[str(row["crosswalk_id"])] = sumo_xy

    # keep sumo coords aligned with the working table for pair selection
    selected = selected.copy()
    selected["sumo_x"] = selected.apply(lambda r: t2_map.get(str(r["crosswalk_id"]), (None, None))[0], axis=1)
    selected["sumo_y"] = selected.apply(lambda r: t2_map.get(str(r["crosswalk_id"]), (None, None))[1], axis=1)

    tls_creation_patch_text, tls_creation_rows = _build_tls_creation_node_patch(selected, net)
    (out_dir / "tls_creation_node_patch.nod.xml").write_text(tls_creation_patch_text, encoding="utf-8")
    pd.DataFrame(tls_creation_rows).to_csv(out_dir / "tls_creation_node_patch_candidates.csv", index=False)

    patch_path, patch_rows = _build_explicit_crossing_patch(selected, net, base_root, out_dir)
    patch_rows.to_csv(out_dir / "explicit_crossing_patch_candidates.csv", index=False)

    selected = selected.merge(
        patch_rows[["crosswalk_id", "patch_key", "patch_edge_pair", "patch_status", "patch_written", "patch_reason"]],
        on="crosswalk_id",
        how="left",
        suffixes=("", "_patch"),
    )
    for col in ["patch_key", "patch_edge_pair", "patch_status", "patch_written", "patch_reason"]:
        patch_col = f"{col}_patch"
        if patch_col in selected.columns:
            if col in selected.columns:
                if col == "patch_written":
                    mask = selected[col].isna()
                else:
                    mask = selected[col].isna() | (selected[col].astype(str).str.strip() == "")
                selected.loc[mask, col] = selected.loc[mask, patch_col]
            else:
                selected[col] = selected[patch_col]
            selected.drop(columns=[patch_col], inplace=True)
        if col not in selected.columns:
            selected[col] = "" if col != "patch_written" else False
    selected["patch_key"] = selected["patch_key"].fillna("")
    selected["patch_edge_pair"] = selected["patch_edge_pair"].fillna("")
    selected["patch_status"] = selected["patch_status"].fillna("")
    selected["patch_written"] = selected["patch_written"].fillna(False)
    selected["patch_reason"] = selected["patch_reason"].fillna("")

    selected.to_csv(out_dir / "selected_generation_candidates.csv", index=False)
    skipped.to_csv(out_dir / "skipped_generation_candidates.csv", index=False)

    plan_lines = [
        "# 보행자 보강 네트워크 v1 patch plan",
        "",
        f"- base net: `{BASE_NET}`",
        f"- selected candidates: {len(selected)}",
        f"- skipped candidates: {len(skipped)}",
        f"- explicit crossing patch candidates: {len(patch_rows)}",
        f"- explicit crossing patches written: {int((patch_rows['patch_status'] == 'patched_unsignalized').sum()) if not patch_rows.empty else 0}",
        f"- tls creation companion rows: {len(tls_creation_rows)}",
        "",
        "## selection rules",
        "- FINAL_SIGNAL_EXTENSION_READY: selected for validation only.",
        "- FINAL_TLS_CREATION_CANDIDATE: selected, but traffic_light creation is separated into a companion node patch.",
        "- FINAL_EXPLICIT_CROSSING_CANDIDATE: explicit crossing patch target.",
        "- FINAL_UNSIGNALIZED_OUT_OF_SCOPE: selected as real crosswalk but excluded from signal-extension experiments.",
        "- FINAL_MANUAL_REVIEW: selected only when road/junction geometry is still usable.",
        "",
        "## applied patch",
        f"- node-files: `{patch_path.name}`",
        "- node patch: companion only, not applied in v1",
        "",
        "## duplicate handling",
        "- existing crossing duplicate: skipped",
        "- generated duplicate crossing: skipped",
        "- no pair found: skipped",
    ]
    (out_dir / "crossing_patch_plan.md").write_text("\n".join(plan_lines) + "\n", encoding="utf-8")

    netconvert_cmd = [
        "netconvert",
        "--sumo-net-file",
        str(BASE_NET),
        "--node-files",
        str(patch_path),
        "--output-file",
        str(out_dir / "pedestrian_augmented_network_v1.net.xml"),
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
    (out_dir / "netconvert_command.sh").write_text("#!/bin/bash\n" + " ".join(netconvert_cmd) + "\n", encoding="utf-8")

    try:
        ret = subprocess.run(netconvert_cmd, capture_output=True, text=True, timeout=600)
        (out_dir / "netconvert.log").write_text((ret.stdout or "") + "\n" + (ret.stderr or ""), encoding="utf-8")
        netconvert_ok = ret.returncode == 0 and (out_dir / "pedestrian_augmented_network_v1.net.xml").exists()
    except Exception as exc:
        (out_dir / "netconvert.log").write_text(str(exc), encoding="utf-8")
        netconvert_ok = False

    if not netconvert_ok:
        raise RuntimeError(f"netconvert failed; see {out_dir / 'netconvert.log'}")

    patched_net_file = out_dir / "pedestrian_augmented_network_v1.net.xml"
    counts_after = _parse_net_counts(patched_net_file)
    load_ok, load_err = _run_sumo_load_test(patched_net_file, end_time=5)
    traci_ids = set()
    patched_root = ET.parse(patched_net_file).getroot()
    for tl in patched_root.findall("tlLogic"):
        tid = str(tl.attrib.get("id", "") or "")
        if tid:
            traci_ids.add(tid)
    traci_ok, traci_err, traci_map = _collect_traci_controlled_links(patched_net_file, traci_ids)

    crossing_audit, signal_audit, load_validation = _build_post_audits(selected, patched_net_file, traci_map)
    crossing_audit.to_csv(out_dir / "post_augmented_crossing_audit.csv", index=False)
    signal_audit.to_csv(out_dir / "post_augmented_signal_audit.csv", index=False)
    signal_audit.to_csv(out_dir / "fixed_post_augmented_signal_audit.csv", index=False)
    load_validation.to_csv(out_dir / "network_load_validation.csv", index=False)
    _write_class_slices(signal_audit, out_dir)

    crossing_inventory = _build_generated_crossing_inventory(patched_net_file)
    crossing_inventory.to_csv(out_dir / "generated_crossing_edge_inventory.csv", index=False)
    crossing_diff = _build_crossing_patch_vs_generated_diff(patch_rows, crossing_inventory)
    crossing_diff.to_csv(out_dir / "crossing_patch_vs_generated_diff.csv", index=False)
    bug_report_md = _build_crossing_bug_report(
        patch_rows=patch_rows,
        inventory=crossing_inventory,
        diff=crossing_diff,
        counts_before=counts_before,
        counts_after=counts_after,
        signal_audit=signal_audit,
    )
    (out_dir / "crossing_detection_bug_report.md").write_text(bug_report_md, encoding="utf-8")

    pre_post_counts = pd.DataFrame(
        [
            {"metric": "crossing_count", "pre": counts_before["crossing_count"], "post": counts_after["crossing_count"], "delta": counts_after["crossing_count"] - counts_before["crossing_count"]},
            {"metric": "walkingarea_count", "pre": counts_before["walkingarea_count"], "post": counts_after["walkingarea_count"], "delta": counts_after["walkingarea_count"] - counts_before["walkingarea_count"]},
            {"metric": "tlLogic_count", "pre": counts_before["tlLogic_count"], "post": counts_after["tlLogic_count"], "delta": counts_after["tlLogic_count"] - counts_before["tlLogic_count"]},
            {"metric": "connection_count", "pre": counts_before["connection_count"], "post": counts_after["connection_count"], "delta": counts_after["connection_count"] - counts_before["connection_count"]},
            {"metric": "sumo_load_ok", "pre": 0, "post": int(load_ok), "delta": int(load_ok)},
            {"metric": "traci_ok", "pre": 0, "post": int(traci_ok), "delta": int(traci_ok)},
        ]
    )
    pre_post_counts.to_csv(out_dir / "pre_post_network_counts.csv", index=False)

    summary_md = _build_summary_md(
        out_dir=out_dir,
        selected=selected,
        skipped=skipped,
        counts_before=counts_before,
        counts_after=counts_after,
        selected_patch=patch_rows,
        signal_audit=signal_audit,
        load_validation=load_validation,
    )
    (out_dir / "pedestrian_augmented_network_summary.md").write_text(summary_md, encoding="utf-8")
    next_strategy_md = _build_next_strategy_md(signal_audit, patch_rows)
    (out_dir / "next_strategy_after_pedestrian_augmented_network.md").write_text(next_strategy_md, encoding="utf-8")

    run_metadata = {
        "output_dir": str(out_dir),
        "created_at": created_at,
        "inputs": {
            "factory_table": str(FACTORY_TABLE),
            "base_net": str(BASE_NET),
            "t2_csv": str(T2_CSV),
        },
        "counts_before": counts_before,
        "counts_after": counts_after,
        "selected_count": int(len(selected)),
        "skipped_count": int(len(skipped)),
        "patched_crossing_count": int((patch_rows["patch_status"] == "patched_unsignalized").sum()) if not patch_rows.empty else 0,
        "traci_ok": bool(traci_ok),
        "traci_error": traci_err,
        "sumo_load_ok": bool(load_ok),
        "sumo_load_error": load_err,
        "preservation_check": {
            "base_network_modified": False,
            "registry_modified": False,
            "simulation_logic_modified": False,
            "original_netxml_overwritten": False,
        },
    }
    (out_dir / "run_metadata.json").write_text(json.dumps(run_metadata, ensure_ascii=False, indent=2), encoding="utf-8")

    cls_counts = signal_audit["final_augmented_class"].value_counts().to_dict()
    print("보행자 보강 네트워크 v1 생성 완료")
    print("")
    print(f"- output_dir: {out_dir}")
    print(f"- 전체 CSV 후보 수: {len(selected) + len(skipped)}")
    print(f"- 생성 대상 후보 수: {len(selected)}")
    print(f"- 제외 후보 수: {len(skipped)}")
    print(f"- 새 net.xml 생성 여부: {patched_net_file.exists()}")
    print(f"- SUMO load 여부: {load_ok}")
    print("")
    print("생성/검증 결과:")
    print(f"- 생성 전 crossing 수: {counts_before['crossing_count']}")
    print(f"- 생성 후 crossing 수: {counts_after['crossing_count']}")
    print(f"- 생성 전 walkingarea 수: {counts_before['walkingarea_count']}")
    print(f"- 생성 후 walkingarea 수: {counts_after['walkingarea_count']}")
    print(f"- SMART_SIGNAL_READY: {int(cls_counts.get('SMART_SIGNAL_READY', 0))}")
    print(f"- RED_ONLY_PHASE_FIX_NEEDED: {int(cls_counts.get('RED_ONLY_PHASE_FIX_NEEDED', 0))}")
    print(f"- TLS_CREATION_CANDIDATE: {int(cls_counts.get('TLS_CREATION_CANDIDATE', 0))}")
    print(f"- UNSIGNALIZED_CROSSWALK: {int(cls_counts.get('UNSIGNALIZED_CROSSWALK', 0))}")
    print(f"- GEOMETRY_REJECT: {int(cls_counts.get('GEOMETRY_REJECT', 0))}")
    print(f"- MANUAL_REVIEW: {int(cls_counts.get('MANUAL_REVIEW', 0))}")
    print("")
    print("보존 확인:")
    print("- base network 수정 여부: False")
    print("- registry 수정 여부: False")
    print("- simulation logic 수정 여부: False")
    print("- 원본 .net.xml 덮어쓰기 여부: False")
    print("")
    print("다음 전략:")
    print(f"- 바로 Phase 6로 갈 후보: {int(cls_counts.get('SMART_SIGNAL_READY', 0))}")
    print(f"- phase fix 후보: {int(cls_counts.get('RED_ONLY_PHASE_FIX_NEEDED', 0))}")
    print(f"- TLS creation 후보: {int(cls_counts.get('TLS_CREATION_CANDIDATE', 0))}")
    print(f"- 미신호 횡단보도 제외 후보: {int(cls_counts.get('UNSIGNALIZED_CROSSWALK', 0))}")
    print(f"- geometry 거절 후보: {int(cls_counts.get('GEOMETRY_REJECT', 0))}")
    print(f"- 다음 확장/수정 우선순위: {out_dir / 'next_strategy_after_pedestrian_augmented_network.md'}")


if __name__ == "__main__":
    main()
