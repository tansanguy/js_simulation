#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import xml.etree.ElementTree as ET
from collections import defaultdict, deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any


def _parse_net_location(root: ET.Element) -> dict[str, Any]:
    location = root.find("location")
    out: dict[str, Any] = {}
    if location is None:
        return out
    for key in ("netOffset", "convBoundary", "origBoundary", "projParameter"):
        out[key] = location.attrib.get(key)
    conv = location.attrib.get("convBoundary")
    if conv:
        vals = [float(x) for x in conv.split(",")]
        if len(vals) == 4:
            out["bbox_xy"] = {"xmin": vals[0], "ymin": vals[1], "xmax": vals[2], "ymax": vals[3]}
    orig = location.attrib.get("origBoundary")
    if orig:
        vals = [float(x) for x in orig.split(",")]
        if len(vals) == 4:
            out["bbox_lonlat"] = {"xmin": vals[0], "ymin": vals[1], "xmax": vals[2], "ymax": vals[3]}
    return out


def _edge_length(edge_elem: ET.Element) -> float:
    # Prefer explicit lane length when present (SUMO authoritative).
    lane_lengths = []
    for lane in edge_elem.findall("lane"):
        try:
            lane_lengths.append(float(lane.attrib.get("length", "0")))
        except ValueError:
            pass
    if lane_lengths:
        return max(lane_lengths)
    try:
        return float(edge_elem.attrib.get("length", "0"))
    except ValueError:
        return 0.0


def _parse_shape(shape_text: str) -> list[tuple[float, float]]:
    pts: list[tuple[float, float]] = []
    for tok in (shape_text or "").split():
        parts = tok.split(",")
        if len(parts) < 2:
            continue
        try:
            pts.append((float(parts[0]), float(parts[1])))
        except ValueError:
            continue
    return pts


@dataclass
class ComponentInfo:
    size_nodes: int
    size_edges: int
    edge_ids: list[str]


def _build_vehicle_edge_graph(edge_rows: list[dict[str, Any]]) -> tuple[dict[str, set[str]], dict[str, list[str]]]:
    adj_nodes: dict[str, set[str]] = defaultdict(set)
    node_to_edges: dict[str, list[str]] = defaultdict(list)
    for e in edge_rows:
        fn = e["function"]
        if fn != "normal":
            continue
        if e["id"].startswith(":"):
            continue
        fr = e.get("from")
        to = e.get("to")
        if not fr or not to:
            continue
        adj_nodes[fr].add(to)
        adj_nodes[to].add(fr)
        node_to_edges[fr].append(e["id"])
        node_to_edges[to].append(e["id"])
    return adj_nodes, node_to_edges


def _components(edge_rows: list[dict[str, Any]]) -> tuple[list[ComponentInfo], list[str]]:
    adj_nodes, node_to_edges = _build_vehicle_edge_graph(edge_rows)
    visited: set[str] = set()
    comps: list[ComponentInfo] = []
    for node in adj_nodes:
        if node in visited:
            continue
        q = deque([node])
        visited.add(node)
        comp_nodes = set()
        comp_edges = set()
        while q:
            cur = q.popleft()
            comp_nodes.add(cur)
            for eid in node_to_edges.get(cur, []):
                comp_edges.add(eid)
            for nxt in adj_nodes[cur]:
                if nxt not in visited:
                    visited.add(nxt)
                    q.append(nxt)
        comps.append(ComponentInfo(len(comp_nodes), len(comp_edges), sorted(comp_edges)))

    comps.sort(key=lambda c: c.size_edges, reverse=True)
    giant = set(comps[0].edge_ids) if comps else set()
    disconnected = sorted([e["id"] for e in edge_rows if e["function"] == "normal" and not e["id"].startswith(":") and e["id"] not in giant])
    return comps, disconnected


def _read_network(net_path: Path) -> dict[str, Any]:
    root = ET.parse(net_path).getroot()
    edge_rows: list[dict[str, Any]] = []
    edge_shapes: dict[str, list[tuple[float, float]]] = {}
    lane_count = 0
    total_road_length_m = 0.0

    for e in root.findall("edge"):
        eid = e.attrib.get("id", "")
        fn = e.attrib.get("function", "normal")
        row = {
            "id": eid,
            "from": e.attrib.get("from"),
            "to": e.attrib.get("to"),
            "function": fn,
            "priority": e.attrib.get("priority"),
            "numLanes": len(e.findall("lane")),
            "length": _edge_length(e),
        }
        edge_rows.append(row)
        lane0 = e.find("lane")
        if lane0 is not None:
            edge_shapes[eid] = _parse_shape(lane0.attrib.get("shape", ""))
        lane_count += row["numLanes"]
        if fn == "normal" and not eid.startswith(":"):
            total_road_length_m += float(row["length"])

    junctions = root.findall("junction")
    tls = root.findall("tlLogic")
    crossings = [e for e in edge_rows if e["function"] == "crossing"]

    components, disconnected = _components(edge_rows)

    edge_func_count: dict[str, int] = defaultdict(int)
    for e in edge_rows:
        edge_func_count[e["function"]] += 1

    return {
        "location": _parse_net_location(root),
        "edge_rows": edge_rows,
        "edge_count": len(edge_rows),
        "lane_count": lane_count,
        "junction_count": len(junctions),
        "traffic_light_count": len(tls),
        "crossing_edge_count": len(crossings),
        "total_road_length_m": total_road_length_m,
        "edge_function_counts": dict(sorted(edge_func_count.items())),
        "edge_shapes": edge_shapes,
        "components": components,
        "disconnected_edges": disconnected,
        "isolated_components": [
            {
                "component_rank": i + 1,
                "size_nodes": c.size_nodes,
                "size_edges": c.size_edges,
                "edge_ids": c.edge_ids,
            }
            for i, c in enumerate(components[1:])
        ],
        "connections": root.findall("connection"),
        "tl_logic_map": {tl.attrib.get("id", ""): tl for tl in tls},
        "junction_types": [j.attrib.get("type", "") for j in junctions],
    }


def _crossing_connections(connections: list[ET.Element], crossing_edge_id: str) -> list[ET.Element]:
    out = []
    for conn in connections:
        if conn.attrib.get("from") == crossing_edge_id or conn.attrib.get("to") == crossing_edge_id:
            out.append(conn)
    return out


def _ped_phase_exists(tl_logic_elem: ET.Element | None, link_indices: list[int]) -> bool:
    if tl_logic_elem is None or not link_indices:
        return False
    for ph in tl_logic_elem.findall("phase"):
        state = ph.attrib.get("state", "")
        for idx in link_indices:
            if 0 <= idx < len(state):
                if state[idx] in ("g", "G", "s", "S"):
                    return True
    return False


def _candidate_row(cw_id: str, metadata: dict[str, Any], net_info: dict[str, Any]) -> dict[str, Any]:
    crossing_edge = str(metadata.get("crossing_edge") or "")
    tls_id = metadata.get("tls_id")
    ped_links = [int(x) for x in metadata.get("ped_link_indices", []) if isinstance(x, int) or str(x).isdigit()]

    conns = _crossing_connections(net_info["connections"], crossing_edge) if crossing_edge else []
    junction_ids = sorted(set([c.attrib.get("from") for c in conns if c.attrib.get("from", "").startswith(":")] + [c.attrib.get("to") for c in conns if c.attrib.get("to", "").startswith(":")]))
    tl_elem = net_info["tl_logic_map"].get(str(tls_id)) if tls_id else None
    if isinstance(tl_elem, str):
        tl_elem = None
    ped_phase = _ped_phase_exists(tl_elem, ped_links)

    return {
        "crosswalk_id": cw_id,
        "matched_crossing_edge": crossing_edge,
        "matched_tls_id": tls_id,
        "matched_ped_link_indices": "|".join(str(x) for x in sorted(set(ped_links))),
        "connection_count_on_crossing": len(conns),
        "junction_internal_ids_on_crossing": "|".join(junction_ids),
        "has_signal": bool(tls_id),
        "has_pedestrian_phase": bool(ped_phase),
        "ped_route_from_edge": (metadata.get("ped_route") or {}).get("from_edge"),
        "ped_route_to_edge": (metadata.get("ped_route") or {}).get("to_edge"),
        "vehicle_conflict_edges": "|".join(metadata.get("vehicle_conflict_edges", []) or []),
        "approach_lanes": "|".join(metadata.get("approach_lanes", []) or []),
    }


def _write_gui_sumocfg(cw_dir: Path, net_path: Path) -> Path:
    cfg = cw_dir / "network_validation_gui.sumocfg"
    cfg.write_text(
        """<configuration>\n"
        "  <input>\n"
        f"    <net-file value=\"{net_path.name}\"/>\n"
        "  </input>\n"
        "  <time>\n"
        "    <begin value=\"0\"/>\n"
        "    <end value=\"120\"/>\n"
        "    <step-length value=\"1\"/>\n"
        "  </time>\n"
        "  <processing>\n"
        "    <ignore-route-errors value=\"true\"/>\n"
        "  </processing>\n"
        "  <gui_only>\n"
        "    <start value=\"true\"/>\n"
        "  </gui_only>\n"
        "</configuration>\n"
        """,
        encoding="utf-8",
    )
    return cfg


def _write_simple_svg(net_info: dict[str, Any], out_svg: Path, highlight_edges: set[str] | None = None) -> None:
    highlight_edges = highlight_edges or set()
    edges = [e for e in net_info["edge_rows"] if e["function"] == "normal" and not e["id"].startswith(":")]
    # fallback to lane shapes is unavailable in edge attrs, so draw using node graph proxy from from/to coords in junctions would require extra parse.
    # Here we render a simple topology table as SVG text for fast visual inspection.
    w = 1400
    h = 900
    lines = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}">',
        '<rect width="100%" height="100%" fill="#f8fafc"/>',
        '<text x="24" y="36" font-family="monospace" font-size="24" fill="#0f172a">SUMO Network Validation Snapshot</text>',
        f'<text x="24" y="70" font-family="monospace" font-size="16" fill="#334155">normal edges: {len(edges)}, disconnected: {len(net_info["disconnected_edges"])}</text>',
    ]
    y = 110
    lines.append('<text x="24" y="'+str(y)+'" font-family="monospace" font-size="14" fill="#0f172a">Disconnected edge ids (first 60):</text>')
    y += 24
    for eid in net_info["disconnected_edges"][:60]:
        color = "#dc2626" if eid in highlight_edges else "#475569"
        lines.append(f'<text x="36" y="{y}" font-family="monospace" font-size="12" fill="{color}">{eid}</text>')
        y += 16
        if y > h - 20:
            break
    lines.append('</svg>')
    out_svg.write_text("\n".join(lines), encoding="utf-8")


def _write_network_png(net_info: dict[str, Any], out_png: Path, crossing_edge: str | None = None) -> None:
    try:
        from smart_crosswalk_sumo.mpl_runtime import configure_matplotlib, ensure_matplotlib_env

        ensure_matplotlib_env()
        import matplotlib

        matplotlib.use("Agg")
        configure_matplotlib(matplotlib)
        import matplotlib.pyplot as plt
    except Exception:
        return

    edge_shapes: dict[str, list[tuple[float, float]]] = net_info.get("edge_shapes", {})
    disconnected = set(net_info.get("disconnected_edges", []))

    fig, ax = plt.subplots(figsize=(12, 12))
    ax.set_facecolor("#f8fafc")
    fig.patch.set_facecolor("#f8fafc")

    for eid, shape in edge_shapes.items():
        if not shape or eid.startswith(":"):
            continue
        xs = [p[0] for p in shape]
        ys = [p[1] for p in shape]
        color = "#94a3b8"
        width = 0.5
        alpha = 0.5
        if eid in disconnected:
            color = "#dc2626"
            width = 1.1
            alpha = 0.9
        if crossing_edge and eid == crossing_edge:
            color = "#2563eb"
            width = 2.0
            alpha = 1.0
        ax.plot(xs, ys, color=color, linewidth=width, alpha=alpha)

    ax.set_title("SUMO Network Validation Map\n(red: disconnected, blue: matched crossing)", fontsize=11)
    ax.set_aspect("equal", adjustable="box")
    ax.axis("off")
    fig.tight_layout()
    fig.savefig(out_png, dpi=220)
    plt.close(fig)


def validate(nets_dir: Path, output_dir: Path) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    cw_dirs = sorted([p for p in nets_dir.glob("cw_*") if p.is_dir()])
    if not cw_dirs:
        raise FileNotFoundError(f"cw_* 디렉토리를 찾지 못했습니다: {nets_dir}")

    overall_rows = []
    candidate_rows = []
    no_signal_rows = []
    no_ped_phase_rows = []
    disconnected_rows = []

    aggregate_nodes = 0
    aggregate_edges = 0
    aggregate_lanes = 0
    aggregate_length = 0.0

    for cw in cw_dirs:
        cw_id = cw.name.replace("cw_", "")
        net_path = cw / "network.net.xml"
        md_path = cw / "metadata.json"
        if not net_path.exists():
            continue
        metadata = json.loads(md_path.read_text(encoding="utf-8")) if md_path.exists() else {}
        net_info = _read_network(net_path)
        gui_cfg = _write_gui_sumocfg(cw, net_path)
        _write_simple_svg(net_info, cw / "network_validation_snapshot.svg", set(net_info["disconnected_edges"]))
        _write_network_png(net_info, cw / "network_validation_map.png", metadata.get("crossing_edge"))

        aggregate_edges += int(net_info["edge_count"])
        aggregate_lanes += int(net_info["lane_count"])
        aggregate_length += float(net_info["total_road_length_m"])
        # node count proxy = unique node ids from edges
        node_ids = set()
        for e in net_info["edge_rows"]:
            if e.get("from"):
                node_ids.add(e["from"])
            if e.get("to"):
                node_ids.add(e["to"])
        aggregate_nodes += len(node_ids)

        cand = _candidate_row(cw_id, metadata, net_info)
        candidate_rows.append(cand)
        if not cand["has_signal"]:
            no_signal_rows.append(cand)
        if not cand["has_pedestrian_phase"]:
            no_ped_phase_rows.append(cand)

        for eid in net_info["disconnected_edges"]:
            disconnected_rows.append({"crosswalk_id": cw_id, "edge_id": eid})

        overall_rows.append(
            {
                "crosswalk_id": cw_id,
                "network_mode": metadata.get("network_mode"),
                "network_bbox": metadata.get("network_bbox"),
                "bbox_lonlat": json.dumps(net_info["location"].get("bbox_lonlat", {}), ensure_ascii=False),
                "bbox_xy": json.dumps(net_info["location"].get("bbox_xy", {}), ensure_ascii=False),
                "node_count_estimate": len(node_ids),
                "edge_count": net_info["edge_count"],
                "lane_count": net_info["lane_count"],
                "junction_count": net_info["junction_count"],
                "traffic_light_count": net_info["traffic_light_count"],
                "crossing_edge_count": net_info["crossing_edge_count"],
                "total_road_length_m": round(net_info["total_road_length_m"], 2),
                "disconnected_edge_count": len(net_info["disconnected_edges"]),
                "isolated_component_count": max(len(net_info["components"]) - 1, 0),
                "largest_component_edges": net_info["components"][0].size_edges if net_info["components"] else 0,
                "gui_sumocfg": str(gui_cfg),
            }
        )

        (output_dir / f"cw_{cw_id}_isolated_components.json").write_text(
            json.dumps(net_info["isolated_components"], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
        if not rows:
            path.write_text("", encoding="utf-8")
            return
        with path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            writer.writerows(rows)

    _write_csv(output_dir / "network_overview.csv", overall_rows)
    _write_csv(output_dir / "crosswalk_candidate_matching.csv", candidate_rows)
    _write_csv(output_dir / "no_signal_candidates.csv", no_signal_rows)
    _write_csv(output_dir / "no_pedestrian_phase_candidates.csv", no_ped_phase_rows)
    _write_csv(output_dir / "disconnected_edges.csv", disconnected_rows)

    summary = {
        "candidate_count": len(candidate_rows),
        "aggregate": {
            "node_count_estimate_sum": aggregate_nodes,
            "edge_count_sum": aggregate_edges,
            "lane_count_sum": aggregate_lanes,
            "total_road_length_m_sum": round(aggregate_length, 2),
        },
        "no_signal_count": len(no_signal_rows),
        "no_ped_phase_count": len(no_ped_phase_rows),
        "disconnected_edge_count": len(disconnected_rows),
    }
    (output_dir / "network_validation_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    md_lines = [
        "# SUMO 도로망 1단계 검증 리포트",
        "",
        "## 요약",
        f"- 후보지 수: {len(candidate_rows)}",
        f"- 합계 노드 수(추정): {aggregate_nodes}",
        f"- 합계 엣지 수: {aggregate_edges}",
        f"- 합계 lane 수: {aggregate_lanes}",
        f"- 합계 도로 길이(m): {round(aggregate_length, 2)}",
        f"- 신호 없음 후보지: {len(no_signal_rows)}",
        f"- 보행자 phase 없음 후보지: {len(no_ped_phase_rows)}",
        f"- disconnected edge 총계: {len(disconnected_rows)}",
        "",
        "## 산출물",
        "- network_overview.csv",
        "- disconnected_edges.csv",
        "- crosswalk_candidate_matching.csv",
        "- no_signal_candidates.csv",
        "- no_pedestrian_phase_candidates.csv",
        "- network_validation_summary.json",
        "",
        "## 비고",
        "- 각 cw 디렉토리마다 `network_validation_gui.sumocfg`와 `network_validation_snapshot.svg`를 생성했습니다.",
        "- isolated component 상세는 `cw_<id>_isolated_components.json`에 저장됩니다.",
    ]
    (output_dir / "network_validation_report.md").write_text("\n".join(md_lines), encoding="utf-8")
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="SUMO 네트워크 검증")
    parser.add_argument("--nets_dir", required=True, help="sumo_nets 디렉토리")
    parser.add_argument("--output_dir", required=True, help="검증 결과 출력 디렉토리")
    args = parser.parse_args()

    summary = validate(Path(args.nets_dir), Path(args.output_dir))
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
