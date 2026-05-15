#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import re
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

import pandas as pd

from smart_crosswalk_sumo.mpl_runtime import configure_matplotlib, ensure_matplotlib_env
from smart_crosswalk_sumo.network_utils import edge_function, read_net
from smart_crosswalk_sumo.run_pedestrian_augmented_network import _choose_crossing_pair, _road_edges_at_node


BASE_DIR = Path(__file__).resolve().parents[1]
RESULT_DIR = BASE_DIR / "result"
DEFAULT_AUDIT = RESULT_DIR / "phase_next_top50_bad_location_generation_audit_20260515_180050" / "bad22_generation_feasibility_audit.csv"
DEFAULT_NET = RESULT_DIR / "phase_next_recovery_command_plan_20260514_200908" / "batch_03_run" / "recovery_tls_batch_network_v1.net.xml"
DEFAULT_SECONDARY_NET = RESULT_DIR / "phase_next_top50_mixed_phase_redesign" / "mixed_phase_redesign_network.net.xml"


def _normalize_id(value: Any) -> str:
    text = str(value or "").strip()
    if not text or text.lower() in {"nan", "none", "nat"}:
        return ""
    if text.endswith(".0"):
        raw = text[:-2]
        if raw.replace("-", "", 1).isdigit():
            return raw
    return text


def _safe_float(value: Any) -> float:
    try:
        if pd.isna(value):
            return math.nan
        return float(value)
    except Exception:
        return math.nan


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


def _edge_bearing(edge: Any, node: Any) -> float | None:
    pts = [(float(x), float(y)) for x, y in edge.getShape()]
    if len(pts) < 2:
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


def _edge_distance(edge: Any, xy: tuple[float, float]) -> float:
    shape = [(float(x), float(y)) for x, y in edge.getShape()]
    if not shape:
        return float("inf")
    if len(shape) == 1:
        return math.dist(xy, shape[0])
    best = float("inf")
    for i in range(len(shape) - 1):
        ax, ay = shape[i]
        bx, by = shape[i + 1]
        px, py = xy
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


def _node_xy(node: Any) -> tuple[float, float]:
    x, y = node.getCoord()
    return float(x), float(y)


def _road_edges_at_node_ids(node: Any) -> list[str]:
    return [str(edge.getID()) for edge in _road_edges_at_node(node)]


def _candidate_nodes_from_edges(net: Any, edge_ids: list[str]) -> list[str]:
    seen: list[str] = []
    for eid in edge_ids:
        if not eid:
            continue
        try:
            edge = net.getEdge(eid)
        except Exception:
            continue
        for node in (edge.getFromNode(), edge.getToNode()):
            nid = str(node.getID())
            if nid and nid not in seen:
                seen.append(nid)
    return seen


def _pick_best_patch(
    net: Any,
    target_xy: tuple[float, float],
    nearest_road_edge_id: str,
    opposite_edge_id: str,
) -> dict[str, Any]:
    anchor_edge = net.getEdge(nearest_road_edge_id)
    candidate_nodes = _candidate_nodes_from_edges(net, [nearest_road_edge_id, opposite_edge_id])
    if not candidate_nodes:
        return {"patch_status": "rejected", "patch_reason": "no_candidate_node"}

    best: dict[str, Any] | None = None
    for nid in candidate_nodes:
        try:
            node = net.getNode(nid)
        except Exception:
            continue
        road_edges = _road_edges_at_node(node)
        if len(road_edges) < 2:
            continue
        e1, e2, pair_candidates = _choose_crossing_pair(node, target_xy)
        if not e1 or not e2:
            continue
        edge_ids = {str(e1), str(e2)}
        touches_anchor = nearest_road_edge_id in edge_ids
        touches_opposite = opposite_edge_id in edge_ids
        node_xy = _node_xy(node)
        avg_dist = (_edge_distance(net.getEdge(e1), target_xy) + _edge_distance(net.getEdge(e2), target_xy)) / 2.0
        angle = _angle_diff(_edge_bearing(net.getEdge(e1), node), _edge_bearing(net.getEdge(e2), node))
        score = 0.0
        score += 50.0 if touches_anchor else 0.0
        score += 10.0 if touches_opposite else 0.0
        score += max(0.0, 30.0 - avg_dist)
        score += max(0.0, 20.0 - math.dist(node_xy, target_xy))
        score += 10.0 if angle is not None and angle >= 120.0 else 0.0
        if best is None or score > float(best["score"]):
            best = {
                "score": score,
                "selected_node_id": nid,
                "selected_node_type": str(node.getType() or ""),
                "selected_node_x": target_xy[0],
                "selected_node_y": target_xy[1],
                "selected_crossing_edges": " ".join(sorted(edge_ids)),
                "pair_candidates_sample": json.dumps(pair_candidates[:5], ensure_ascii=False),
                "patch_status": "patched",
                "patch_reason": "selected via node pair search",
                "touches_anchor": touches_anchor,
                "touches_opposite": touches_opposite,
                "selected_edge1": e1,
                "selected_edge2": e2,
                "node_xy": node_xy,
            }
    if best is None:
        return {"patch_status": "rejected", "patch_reason": "no_valid_pair_node"}
    return best


def _geometry_status(distance_m: float) -> str:
    if pd.isna(distance_m):
        return "GENERATION_REJECT"
    if distance_m <= 20:
        return "GENERATION_GEOMETRY_OK"
    if distance_m <= 50:
        return "GENERATION_REVIEW"
    return "GENERATION_REJECT"


def _write_patch_files(rows: pd.DataFrame, net: Any, nodes_path: Path, con_path: Path) -> None:
    nodes_root = ET.Element("nodes")
    con_root = ET.Element("connections")
    seen_nodes: set[str] = set()
    seen_crossings: set[tuple[str, str]] = set()
    for row in rows.itertuples(index=False):
        node_id = str(row.selected_node_id)
        edges_text = str(row.selected_crossing_edges)
        if not node_id or not edges_text:
            continue
        key = (node_id, " ".join(sorted(set(edges_text.split()))))
        if key in seen_crossings:
            continue
        seen_crossings.add(key)
        try:
            node = net.getNode(node_id)
        except Exception:
            continue
        if node_id not in seen_nodes:
            seen_nodes.add(node_id)
            ET.SubElement(
                nodes_root,
                "node",
                {
                    "id": node_id,
                    "x": f"{float(row.selected_node_x):.2f}",
                    "y": f"{float(row.selected_node_y):.2f}",
                    "type": str(row.selected_node_type or node.getType() or "priority"),
                },
            )
        ET.SubElement(
            con_root,
            "crossing",
            {
                "node": node_id,
                "edges": key[1],
                "id": f"GEN_{row.crosswalk_id}",
                "width": "4.0",
                "priority": "true",
            },
        )
    ET.indent(nodes_root, space="  ")
    ET.indent(con_root, space="  ")
    nodes_path.write_text(ET.tostring(nodes_root, encoding="unicode", xml_declaration=True), encoding="utf-8")
    con_path.write_text(ET.tostring(con_root, encoding="unicode", xml_declaration=True), encoding="utf-8")


def _match_generated_crossing(root: ET.Element, node_id: str, edges_text: str) -> ET.Element | None:
    wanted = sorted(set(edges_text.split()))
    best_score: tuple[int, int, int] | None = None
    best_elem: ET.Element | None = None
    for crossing in root.findall("edge"):
        if str(crossing.attrib.get("function", "")).lower() != "crossing":
            continue
        edge_id = str(crossing.attrib.get("id", ""))
        if not edge_id.startswith(f":{node_id}_c"):
            continue
        found = sorted(set(str(crossing.attrib.get("crossingEdges", "")).split()))
        exact = 1 if found == wanted else 0
        shared = len(set(found) & set(wanted))
        symdiff = len(set(found) ^ set(wanted))
        score = (exact, shared, -symdiff)
        if best_score is None or score > best_score:
            best_score = score
            best_elem = crossing
    return best_elem


def _crossing_center_lonlat(net: Any, crossing_edge: ET.Element) -> tuple[float, float] | None:
    edge_id = str(crossing_edge.attrib.get("id", ""))
    try:
        edge = net.getEdge(edge_id)
    except Exception:
        return None
    shape = [(float(x), float(y)) for x, y in edge.getShape()]
    if not shape:
        try:
            node = net.getNode(str(crossing_edge.attrib.get("from", "")))
            return _lonlat_from_xy(net, *_node_xy(node))
        except Exception:
            return None
    cx = sum(x for x, _ in shape) / len(shape)
    cy = sum(y for _, y in shape) / len(shape)
    return _lonlat_from_xy(net, cx, cy)


def _lonlat_distance_m(net: Any, lon1: float, lat1: float, lon2: float, lat2: float) -> float:
    p1 = _xy_from_lonlat(net, lon1, lat1)
    p2 = _xy_from_lonlat(net, lon2, lat2)
    if p1 is None or p2 is None:
        return math.nan
    return math.dist(p1, p2)


def _render_overview(out_path: Path, df: pd.DataFrame) -> None:
    ensure_matplotlib_env()
    import matplotlib

    matplotlib.use("Agg")
    configure_matplotlib(matplotlib)
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(13, 13))
    ax.set_facecolor("#fbfaf6")
    ax.scatter(df["real_lon"], df["real_lat"], s=45, c="#1a9850", edgecolors="white", linewidths=0.7, label="real")
    ax.scatter(df["old_sumo_lon"], df["old_sumo_lat"], s=45, marker="s", c="#6c757d", edgecolors="#111111", linewidths=0.7, label="old mapped")
    success = df[df["patch_status"] == "patched"]
    fail = df[df["patch_status"] != "patched"]
    if not success.empty:
        ax.scatter(success["generated_crossing_candidate_lon"], success["generated_crossing_candidate_lat"], s=55, marker="^", c="#1a9850", edgecolors="#111111", linewidths=0.7, label="generated")
        for row in success.itertuples(index=False):
            ax.plot([row.real_lon, row.old_sumo_lon], [row.real_lat, row.old_sumo_lat], color="#9e9e9e", linestyle="--", linewidth=1.0, alpha=0.5)
            ax.plot([row.real_lon, row.generated_crossing_candidate_lon], [row.real_lat, row.generated_crossing_candidate_lat], color="#1a9850", linewidth=1.6, alpha=0.7)
    if not fail.empty:
        ax.scatter(fail["generated_crossing_candidate_lon"], fail["generated_crossing_candidate_lat"], s=55, marker="^", c="#d73027", edgecolors="#111111", linewidths=0.7, label="failed generation")
    ax.set_title("Bad 22 Generated Crossing Patch Overview", fontsize=14, weight="bold")
    ax.set_xlabel("Longitude")
    ax.set_ylabel("Latitude")
    ax.set_aspect("equal", adjustable="box")
    ax.grid(alpha=0.12)
    ax.legend(loc="best", fontsize=8)
    fig.tight_layout()
    fig.savefig(out_path, dpi=180, bbox_inches="tight")
    plt.close(fig)


def _render_card(out_path: Path, row: pd.Series, net: Any) -> None:
    ensure_matplotlib_env()
    import matplotlib

    matplotlib.use("Agg")
    configure_matplotlib(matplotlib)
    import matplotlib.pyplot as plt

    fig, (ax_text, ax_map) = plt.subplots(2, 1, figsize=(10.5, 11), gridspec_kw={"height_ratios": [1.0, 2.2]})
    ax_text.axis("off")
    lines = [
        f"{row['crosswalk_id']} | rank {int(row['original_rank'])}",
        f"patch {row['patch_status']} | reason {row['patch_reason']}",
        f"geometry {row['geometry_generation_status']} | final {row['final_generation_tier']}",
        f"real {row['real_lon']:.6f}, {row['real_lat']:.6f}",
        f"old {row['old_sumo_lon']:.6f}, {row['old_sumo_lat']:.6f}",
        f"generated {row['generated_crossing_candidate_lon']:.6f}, {row['generated_crossing_candidate_lat']:.6f}",
        f"node {row.get('selected_node_id', '')} | edges {row.get('selected_crossing_edges', '')}",
    ]
    y = 0.95
    for line in lines:
        ax_text.text(0.01, y, line, family="monospace", fontsize=9.2, va="top")
        y -= 0.15

    ax_map.set_facecolor("#fcfbf7")
    ax_map.scatter([row["real_lon"]], [row["real_lat"]], s=130, c="#1a9850", edgecolors="white", linewidths=0.8, zorder=7, label="real")
    ax_map.scatter([row["old_sumo_lon"]], [row["old_sumo_lat"]], s=130, marker="s", c="#6c757d", edgecolors="#111111", linewidths=0.8, zorder=7, label="old mapped")
    ax_map.scatter([row["generated_crossing_candidate_lon"]], [row["generated_crossing_candidate_lat"]], s=130, marker="^", c="#1a9850" if row["patch_status"] == "patched" else "#d73027", edgecolors="#111111", linewidths=0.8, zorder=7, label="generated")
    ax_map.plot([row["real_lon"], row["old_sumo_lon"]], [row["real_lat"], row["old_sumo_lat"]], color="#9e9e9e", linestyle="--", linewidth=1.2, alpha=0.55)
    ax_map.plot([row["real_lon"], row["generated_crossing_candidate_lon"]], [row["real_lat"], row["generated_crossing_candidate_lat"]], color="#1a9850", linewidth=1.6, alpha=0.75)
    xs = [row["real_lon"], row["old_sumo_lon"], row["generated_crossing_candidate_lon"]]
    ys = [row["real_lat"], row["old_sumo_lat"], row["generated_crossing_candidate_lat"]]
    pad = 0.0018
    ax_map.set_xlim(min(xs) - pad, max(xs) + pad)
    ax_map.set_ylim(min(ys) - pad, max(ys) + pad)
    ax_map.set_aspect("equal", adjustable="box")
    ax_map.grid(alpha=0.12)
    ax_map.set_title(row["crosswalk_id"])
    ax_map.set_xlabel("Longitude")
    ax_map.set_ylabel("Latitude")
    ax_map.legend(loc="best", fontsize=8)
    fig.tight_layout()
    fig.savefig(out_path, dpi=180, bbox_inches="tight")
    plt.close(fig)


def _build_plan_md(out_dir: Path, df: pd.DataFrame, net_path: Path, netconvert_ok: bool) -> str:
    success = int((df["patch_status"] == "patched").sum())
    failed = len(df) - success
    lines = [
        "# Bad22 Generated Crossing Patch Plan",
        "",
        f"- base net: `{net_path}`",
        f"- patch candidates: {len(df)}",
        f"- patch written: {success}",
        f"- patch rejected: {failed}",
        f"- netconvert attempted: {netconvert_ok}",
        "",
        "## rules",
        "- Use real-coordinate generated crossing candidate as node move target.",
        "- Prefer node where road pair shares one junction.",
        "- Do not touch base net.xml.",
        "- Do not run smoke or seed experiments.",
        "",
        "## failed candidates",
    ]
    failed_df = df[df["patch_status"] != "patched"]
    if failed_df.empty:
        lines.append("- none")
    else:
        for row in failed_df.itertuples(index=False):
            lines.append(f"- {row.crosswalk_id}: {row.patch_reason}")
    lines.extend(
        [
            "",
            "## next step",
            "- If netconvert succeeded, inspect generated crossing location check and then decide which patched crossings are safe to promote to smoke prep.",
        ]
    )
    return "\n".join(lines) + "\n"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate crossing patch for BAD 22 geometry-ok candidates")
    parser.add_argument("--audit-csv", default=str(DEFAULT_AUDIT))
    parser.add_argument("--base-net", default=str(DEFAULT_NET))
    parser.add_argument("--secondary-net", default=str(DEFAULT_SECONDARY_NET))
    parser.add_argument(
        "--output-dir",
        default=str(RESULT_DIR / f"phase_next_bad22_generated_crossing_patch_{datetime.now().strftime('%Y%m%d_%H%M%S')}"),
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    audit = pd.read_csv(args.audit_csv)
    audit["crosswalk_id"] = audit["crosswalk_id"].map(_normalize_id)
    selected = audit[(audit["geometry_generation_status"] == "GENERATION_GEOMETRY_OK") | (audit["final_generation_tier"] == "UNSIGNALIZED_CROSSING_ONLY")].copy()
    if len(selected) != 12:
        raise RuntimeError(f"Expected 12 patch candidates, got {len(selected)}")

    base_net_path = Path(args.base_net)
    if not base_net_path.exists():
        raise FileNotFoundError(base_net_path)
    secondary_net_path = Path(args.secondary_net) if args.secondary_net else None
    if secondary_net_path is not None and not secondary_net_path.exists():
        secondary_net_path = None

    net = read_net(base_net_path)
    secondary_net = read_net(secondary_net_path) if secondary_net_path is not None else None

    rows: list[dict[str, Any]] = []
    for _, row in selected.iterrows():
        real_lon = _safe_float(row.get("real_lon"))
        real_lat = _safe_float(row.get("real_lat"))
        gen_lon = _safe_float(row.get("generated_crossing_candidate_lon"))
        gen_lat = _safe_float(row.get("generated_crossing_candidate_lat"))
        target_xy = _xy_from_lonlat(net, gen_lon, gen_lat)
        net_used = net
        if target_xy is None and secondary_net is not None:
            target_xy = _xy_from_lonlat(secondary_net, gen_lon, gen_lat)
            net_used = secondary_net if target_xy is not None else net
        if target_xy is None:
            rows.append(
                {
                    "crosswalk_id": _normalize_id(row.get("crosswalk_id")),
                    "original_rank": int(_safe_float(row.get("original_rank")) or 0),
                    "real_lon": real_lon,
                    "real_lat": real_lat,
                    "old_sumo_lon": _safe_float(row.get("old_sumo_lon")),
                    "old_sumo_lat": _safe_float(row.get("old_sumo_lat")),
                    "generated_crossing_candidate_lon": gen_lon,
                    "generated_crossing_candidate_lat": gen_lat,
                    "nearest_road_edge_id": _normalize_id(row.get("nearest_road_edge_id")),
                    "opposite_or_pair_edge_id": _normalize_id(row.get("opposite_or_pair_edge_id")),
                    "selected_node_id": "",
                    "selected_node_type": "",
                    "selected_node_x": math.nan,
                    "selected_node_y": math.nan,
                    "selected_crossing_edges": "",
                    "patch_status": "rejected",
                    "patch_reason": "coordinate_conversion_failed",
                    "geometry_generation_status": _normalize_id(row.get("geometry_generation_status")),
                    "final_generation_tier": _normalize_id(row.get("final_generation_tier")),
                }
            )
            continue

        best = _pick_best_patch(net_used, target_xy, _normalize_id(row.get("nearest_road_edge_id")), _normalize_id(row.get("opposite_or_pair_edge_id")))
        rows.append(
            {
                "crosswalk_id": _normalize_id(row.get("crosswalk_id")),
                "original_rank": int(_safe_float(row.get("original_rank")) or 0),
                "real_lon": real_lon,
                "real_lat": real_lat,
                "old_sumo_lon": _safe_float(row.get("old_sumo_lon")),
                "old_sumo_lat": _safe_float(row.get("old_sumo_lat")),
                "generated_crossing_candidate_lon": gen_lon,
                "generated_crossing_candidate_lat": gen_lat,
                "nearest_road_edge_id": _normalize_id(row.get("nearest_road_edge_id")),
                "opposite_or_pair_edge_id": _normalize_id(row.get("opposite_or_pair_edge_id")),
                "selected_node_id": _normalize_id(best.get("selected_node_id")),
                "selected_node_type": _normalize_id(best.get("selected_node_type")),
                "selected_node_x": best.get("selected_node_x", math.nan),
                "selected_node_y": best.get("selected_node_y", math.nan),
                "selected_crossing_edges": _normalize_id(best.get("selected_crossing_edges")),
                "patch_status": _normalize_id(best.get("patch_status")),
                "patch_reason": _normalize_id(best.get("patch_reason")),
                "touches_anchor": bool(best.get("touches_anchor", False)),
                "touches_opposite": bool(best.get("touches_opposite", False)),
                "geometry_generation_status": _normalize_id(row.get("geometry_generation_status")),
                "final_generation_tier": _normalize_id(row.get("final_generation_tier")),
                "old_mapping_distance_m": _safe_float(row.get("old_mapping_distance_m")),
                "generated_distance_m": _safe_float(row.get("generated_distance_m")),
            }
        )

    df = pd.DataFrame(rows)
    df["patch_status"] = df["patch_status"].fillna("rejected")
    df["patch_reason"] = df["patch_reason"].fillna("")
    df["selected_node_id"] = df["selected_node_id"].fillna("")
    df["selected_crossing_edges"] = df["selected_crossing_edges"].fillna("")
    df["generated_distance_m"] = df["generated_distance_m"].astype(float)
    df["old_mapping_distance_m"] = df["old_mapping_distance_m"].astype(float)

    patch_df = df[df["patch_status"] == "patched"].copy()
    patch_candidates_path = out_dir / "generated_crossing_patch_candidates.csv"
    node_patch_path = out_dir / "generated_crossing_nodes.add.xml"
    con_patch_path = out_dir / "generated_crossing_connections.con.xml"
    plan_path = out_dir / "generated_crossing_patch_plan.md"
    netconvert_script_path = out_dir / "command_to_build_generated_crossing_net.sh"
    generated_net_path = out_dir / "generated_crossing_network.net.xml"
    check_path = out_dir / "generated_crossing_location_check.csv"
    cards_dir = out_dir / "generated_crossing_patch_cards"

    df.to_csv(patch_candidates_path, index=False)
    _write_patch_files(patch_df, net, node_patch_path, con_patch_path)
    netconvert_cmd = [
        "netconvert",
        "--sumo-net-file",
        str(base_net_path),
        "--node-files",
        str(node_patch_path),
        "--connection-files",
        str(con_patch_path),
        "--output-file",
        str(generated_net_path),
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
    netconvert_script_path.write_text("#!/bin/bash\n" + " ".join(netconvert_cmd) + "\n", encoding="utf-8")
    netconvert_ok = False
    log_path = out_dir / "netconvert.log"
    try:
        ret = subprocess.run(netconvert_cmd, capture_output=True, text=True, timeout=600)
        log_path.write_text((ret.stdout or "") + "\n" + (ret.stderr or ""), encoding="utf-8")
        netconvert_ok = ret.returncode == 0 and generated_net_path.exists()
    except Exception as exc:
        log_path.write_text(str(exc), encoding="utf-8")
        netconvert_ok = False

    check_rows: list[dict[str, Any]] = []
    if netconvert_ok:
        generated_net = read_net(generated_net_path)
        root = ET.parse(generated_net_path).getroot()
        for row in df.itertuples(index=False):
            if row.patch_status != "patched":
                check_rows.append(
                    {
                        "crosswalk_id": row.crosswalk_id,
                        "selected_node_id": row.selected_node_id,
                        "selected_crossing_edges": row.selected_crossing_edges,
                        "generated_crossing_edge_id": "",
                        "generated_crossing_center_lon": math.nan,
                        "generated_crossing_center_lat": math.nan,
                        "generated_to_real_distance_m": math.nan,
                        "generated_to_candidate_distance_m": math.nan,
                        "generated_crossing_exists": False,
                        "location_check_status": "PATCH_NOT_WRITTEN",
                        "reason": row.patch_reason,
                    }
                )
                continue
            matched = _match_generated_crossing(root, str(row.selected_node_id), str(row.selected_crossing_edges))
            if matched is None:
                check_rows.append(
                    {
                        "crosswalk_id": row.crosswalk_id,
                        "selected_node_id": row.selected_node_id,
                        "selected_crossing_edges": row.selected_crossing_edges,
                        "generated_crossing_edge_id": "",
                        "generated_crossing_center_lon": math.nan,
                        "generated_crossing_center_lat": math.nan,
                        "generated_to_real_distance_m": math.nan,
                        "generated_to_candidate_distance_m": math.nan,
                        "generated_crossing_exists": False,
                        "location_check_status": "CROSSING_NOT_FOUND",
                        "reason": "netconvert produced no matching crossing edge",
                    }
                )
                continue
            center = _crossing_center_lonlat(generated_net, matched)
            if center is None:
                check_rows.append(
                    {
                        "crosswalk_id": row.crosswalk_id,
                        "selected_node_id": row.selected_node_id,
                        "selected_crossing_edges": row.selected_crossing_edges,
                        "generated_crossing_edge_id": str(matched.attrib.get("id", "")),
                        "generated_crossing_center_lon": math.nan,
                        "generated_crossing_center_lat": math.nan,
                        "generated_to_real_distance_m": math.nan,
                        "generated_to_candidate_distance_m": math.nan,
                        "generated_crossing_exists": True,
                        "location_check_status": "CENTER_CONVERSION_FAILED",
                        "reason": "unable to convert crossing center",
                    }
                )
                continue
            real_dist = _lonlat_distance_m(generated_net, row.real_lon, row.real_lat, center[0], center[1])
            cand_dist = _lonlat_distance_m(generated_net, row.generated_crossing_candidate_lon, row.generated_crossing_candidate_lat, center[0], center[1])
            check_rows.append(
                {
                    "crosswalk_id": row.crosswalk_id,
                    "selected_node_id": row.selected_node_id,
                    "selected_crossing_edges": row.selected_crossing_edges,
                    "generated_crossing_edge_id": str(matched.attrib.get("id", "")),
                    "generated_crossing_center_lon": center[0],
                    "generated_crossing_center_lat": center[1],
                    "generated_to_real_distance_m": real_dist,
                    "generated_to_candidate_distance_m": cand_dist,
                    "generated_crossing_exists": True,
                    "location_check_status": "OK" if real_dist <= 20 else "REVIEW",
                    "reason": "generated crossing found",
                }
            )
    else:
        for row in df.itertuples(index=False):
            check_rows.append(
                {
                    "crosswalk_id": row.crosswalk_id,
                    "selected_node_id": row.selected_node_id,
                    "selected_crossing_edges": row.selected_crossing_edges,
                    "generated_crossing_edge_id": "",
                    "generated_crossing_center_lon": math.nan,
                    "generated_crossing_center_lat": math.nan,
                    "generated_to_real_distance_m": math.nan,
                    "generated_to_candidate_distance_m": math.nan,
                    "generated_crossing_exists": False,
                    "location_check_status": "NETCONVERT_FAILED",
                    "reason": "netconvert failed",
                }
            )

    check_df = pd.DataFrame(check_rows)
    check_df.to_csv(check_path, index=False)

    plan_md = _build_plan_md(out_dir, df, base_net_path, netconvert_ok)
    plan_path.write_text(plan_md, encoding="utf-8")

    _render_overview(out_dir / "generated_crossing_patch_overview.png", df)
    cards_dir.mkdir(parents=True, exist_ok=True)

    for row in df.itertuples(index=False):
        safe_id = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(row.crosswalk_id))
        _render_card(cards_dir / f"crosswalk_{safe_id}.png", pd.Series(row._asdict()), net)

    success_count = int((df["patch_status"] == "patched").sum())
    location_ok_count = int((check_df["generated_to_real_distance_m"] <= 20).sum()) if "generated_to_real_distance_m" in check_df.columns else 0
    crossing_exists_count = int((check_df["generated_crossing_exists"] == True).sum())  # noqa: E712

    print(f"output_dir={out_dir}")
    print(f"patch_candidates={len(df)}")
    print(f"patch_written={success_count}")
    print(f"netconvert_success={netconvert_ok}")
    print(f"generated_crossing_exists={crossing_exists_count}")
    print(f"location_le_20m={location_ok_count}")
    print(f"check_csv={check_path}")
    print(f"patch_csv={patch_candidates_path}")
    print(f"node_patch={node_patch_path}")
    print(f"connection_patch={con_patch_path}")
    print(f"netconvert_cmd={netconvert_script_path}")


if __name__ == "__main__":
    main()
