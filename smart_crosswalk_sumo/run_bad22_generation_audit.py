#!/usr/bin/env python3
from __future__ import annotations

import argparse
import math
import re
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

import pandas as pd

from smart_crosswalk_sumo.mpl_runtime import configure_matplotlib, ensure_matplotlib_env
from smart_crosswalk_sumo.network_utils import (
    edge_center,
    edge_function,
    point_segment_distance,
    read_net,
)


BASE_DIR = Path(__file__).resolve().parents[1]
RESULT_DIR = BASE_DIR / "result"
DEFAULT_DISTANCE_SUMMARY = RESULT_DIR / "phase_next_top50_mapping_visual_audit_20260515_174626" / "03_top50_mapping_distance_summary.csv"
DEFAULT_BAD22 = RESULT_DIR / "phase_next_top50_mapping_visual_audit_20260515_174626" / "top50_bad_location_review_targets.csv"
DEFAULT_STEPWISE = BASE_DIR / "smart_crosswalk_sumo" / "data" / "crosswalk_stepwise_result_50m.csv"
DEFAULT_PRIMARY_NET = RESULT_DIR / "phase_next_recovery_command_plan_20260514_200908" / "batch_03_run" / "recovery_tls_batch_network_v1.net.xml"
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


def _load_csv(path: Path) -> pd.DataFrame:
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


def _point_on_segment_projection(
    point: tuple[float, float],
    a: tuple[float, float],
    b: tuple[float, float],
) -> tuple[float, float]:
    px, py = point
    ax, ay = a
    bx, by = b
    dx = bx - ax
    dy = by - ay
    if dx == 0 and dy == 0:
        return a
    t = ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)
    t = max(0.0, min(1.0, t))
    return ax + t * dx, ay + t * dy


def _nearest_point_on_edge(edge: Any, xy: tuple[float, float]) -> tuple[tuple[float, float], float]:
    shape = [(float(x), float(y)) for x, y in edge.getShape()]
    if not shape:
        node = edge.getFromNode()
        nx, ny = node.getCoord()
        nearest = (float(nx), float(ny))
        return nearest, math.dist(xy, nearest)
    if len(shape) == 1:
        return shape[0], math.dist(xy, shape[0])
    best_pt = shape[0]
    best_dist = float("inf")
    for i in range(len(shape) - 1):
        proj = _point_on_segment_projection(xy, shape[i], shape[i + 1])
        dist = math.dist(xy, proj)
        if dist < best_dist:
            best_dist = dist
            best_pt = proj
    return best_pt, best_dist


def _edge_bearing(edge: Any) -> float | None:
    shape = [(float(x), float(y)) for x, y in edge.getShape()]
    if len(shape) < 2:
        return None
    x0, y0 = shape[0]
    x1, y1 = shape[-1]
    if x0 == x1 and y0 == y1:
        return None
    return (math.degrees(math.atan2(y1 - y0, x1 - x0)) + 360.0) % 360.0


def _bearing_delta(a: float | None, b: float | None) -> float | None:
    if a is None or b is None:
        return None
    diff = abs(a - b)
    return min(diff, 360.0 - diff)


def _edge_key(edge: Any) -> tuple[str, str]:
    return str(edge.getFromNode().getID()), str(edge.getToNode().getID())


def _iter_normal_vehicle_edges(net: Any) -> list[Any]:
    out = []
    for edge in net.getEdges():
        eid = str(edge.getID())
        if not eid or eid.startswith(":"):
            continue
        if edge_function(edge) != "normal":
            continue
        if not edge.allows("passenger"):
            continue
        out.append(edge)
    return out


def _find_nearest_vehicle_edge(net: Any, target_xy: tuple[float, float]) -> tuple[Any | None, tuple[float, float] | None, float]:
    best_edge = None
    best_pt = None
    best_dist = float("inf")
    for edge in _iter_normal_vehicle_edges(net):
        pt, dist = _nearest_point_on_edge(edge, target_xy)
        if dist < best_dist:
            best_dist = dist
            best_edge = edge
            best_pt = pt
    return best_edge, best_pt, best_dist


def _find_pair_edge(net: Any, edge: Any, target_xy: tuple[float, float], search_radius_m: float = 120.0) -> tuple[Any | None, tuple[float, float] | None, float]:
    from_id, to_id = _edge_key(edge)
    reverse_key = (to_id, from_id)
    reverse_matches = []
    for cand in _iter_normal_vehicle_edges(net):
        if _edge_key(cand) == reverse_key:
            pt, dist = _nearest_point_on_edge(cand, target_xy)
            reverse_matches.append((cand, pt, dist))
    if reverse_matches:
        return min(reverse_matches, key=lambda item: item[2])

    edge_bearing = _edge_bearing(edge)
    fallback: list[tuple[float, Any, tuple[float, float], float]] = []
    for cand in _iter_normal_vehicle_edges(net):
        if cand.getID() == edge.getID():
            continue
        pt, dist = _nearest_point_on_edge(cand, target_xy)
        if dist > search_radius_m:
            continue
        delta = _bearing_delta(edge_bearing, _edge_bearing(cand))
        if delta is None:
            continue
        if delta < 120.0:
            continue
        fallback.append((delta, cand, pt, dist))
    if fallback:
        fallback.sort(key=lambda item: (-item[0], item[3], str(item[1].getID())))
        _, cand, pt, dist = fallback[0]
        return cand, pt, dist
    return None, None, math.nan


def _nearest_tls(net: Any, target_xy: tuple[float, float]) -> tuple[str, tuple[float, float] | None, float]:
    best_id = ""
    best_pt = None
    best_dist = float("inf")
    try:
        tls_ids = {str(tls.getID()) for tls in net.getTrafficLights()}
    except Exception:
        tls_ids = set()
    for tls_id in tls_ids:
        try:
            node = net.getNode(tls_id)
        except Exception:
            continue
        node_xy = tuple(float(v) for v in node.getCoord())
        dist = math.dist(target_xy, node_xy)
        if dist < best_dist:
            best_dist = dist
            best_id = tls_id
            best_pt = node_xy
    return best_id, best_pt, best_dist


def _ped_link_possible(net: Any, tls_id: str) -> bool:
    if not tls_id:
        return False
    try:
        node = net.getNode(tls_id)
    except Exception:
        return False
    normal_edges = []
    walking_edges = []
    for edge in list(node.getIncoming()) + list(node.getOutgoing()):
        fn = edge_function(edge)
        if fn == "normal" and edge.allows("pedestrian"):
            normal_edges.append(edge)
        elif fn == "walkingarea":
            walking_edges.append(edge)
    return bool(normal_edges and walking_edges)


def _geometry_status(distance_m: float) -> str:
    if pd.isna(distance_m):
        return "GENERATION_REJECT"
    if distance_m <= 20:
        return "GENERATION_GEOMETRY_OK"
    if distance_m <= 50:
        return "GENERATION_REVIEW"
    return "GENERATION_REJECT"


def _signal_status(tls_distance_m: float, ped_link_possible: bool) -> str:
    if not pd.isna(tls_distance_m) and tls_distance_m <= 50 and ped_link_possible:
        return "SIGNAL_GENERATION_CANDIDATE"
    return "UNSIGNALIZED_CROSSING_ONLY"


def _final_tier(geometry_status: str, signal_status: str) -> str:
    if geometry_status == "GENERATION_REJECT":
        return "GENERATION_REJECT"
    if signal_status == "SIGNAL_GENERATION_CANDIDATE":
        if geometry_status == "GENERATION_GEOMETRY_OK":
            return "SIGNAL_GENERATION_CANDIDATE"
        return "SIGNAL_GENERATION_REVIEW"
    if geometry_status == "GENERATION_GEOMETRY_OK":
        return "UNSIGNALIZED_CROSSING_ONLY"
    return "UNSIGNALIZED_CROSSING_REVIEW"


def _recommended_action(final_tier: str) -> str:
    return {
        "SIGNAL_GENERATION_CANDIDATE": "prepare crossing patch and signal links",
        "SIGNAL_GENERATION_REVIEW": "review geometry before signal patch",
        "UNSIGNALIZED_CROSSING_ONLY": "generate unsignalized crossing only",
        "UNSIGNALIZED_CROSSING_REVIEW": "review geometry before unsignalized patch",
        "GENERATION_REJECT": "exclude from generation",
    }.get(final_tier, "review")


def _card_title(row: pd.Series) -> str:
    return f"{row['crosswalk_id']} | old {row['old_mapping_distance_m']:.1f}m -> new {row['generated_distance_m']:.1f}m"


def _render_overview(out_path: Path, df: pd.DataFrame) -> None:
    ensure_matplotlib_env()
    import matplotlib

    matplotlib.use("Agg")
    configure_matplotlib(matplotlib)
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(13, 13))
    colors = {
        "GENERATION_GEOMETRY_OK": "#1a9850",
        "GENERATION_REVIEW": "#fdae61",
        "GENERATION_REJECT": "#d73027",
    }
    ax.set_facecolor("#fbfaf6")
    for status, group in df.groupby("geometry_generation_status", dropna=False):
        color = colors.get(str(status), "#7f7f7f")
        ax.scatter(group["real_lon"], group["real_lat"], s=40, c=color, edgecolors="white", linewidths=0.6, label=f"real {status}", zorder=4)
        ax.scatter(group["old_sumo_lon"], group["old_sumo_lat"], s=42, marker="s", c="#6c757d", edgecolors="#111111", linewidths=0.6, label="old sumo" if status == df["geometry_generation_status"].iloc[0] else None, zorder=3, alpha=0.8)
        good = group.dropna(subset=["generated_crossing_candidate_lon", "generated_crossing_candidate_lat"])
        ax.scatter(good["generated_crossing_candidate_lon"], good["generated_crossing_candidate_lat"], s=50, marker="^", c=color, edgecolors="#111111", linewidths=0.6, label=f"generated {status}", zorder=5)
        for row in good.itertuples(index=False):
            ax.plot([row.real_lon, row.old_sumo_lon], [row.real_lat, row.old_sumo_lat], color="#9e9e9e", linewidth=1.0, alpha=0.35, zorder=1)
            ax.plot([row.real_lon, row.generated_crossing_candidate_lon], [row.real_lat, row.generated_crossing_candidate_lat], color=color, linewidth=1.4, alpha=0.65, zorder=2)
    ax.set_title("Bad 22 Generation Overview", fontsize=14, weight="bold")
    ax.set_xlabel("Longitude")
    ax.set_ylabel("Latitude")
    ax.set_aspect("equal", adjustable="box")
    ax.grid(alpha=0.12)
    handles, labels = ax.get_legend_handles_labels()
    unique = dict(zip(labels, handles))
    ax.legend(unique.values(), unique.keys(), fontsize=8, loc="best", frameon=True)
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
        _card_title(row),
        f"tier {row['final_generation_tier']} | signal {row['signal_generation_status']} | action {row['recommended_action']}",
        f"real {row['real_lon']:.6f}, {row['real_lat']:.6f}",
        f"old {row['old_sumo_lon']:.6f}, {row['old_sumo_lat']:.6f} | old edge {row['old_crossing_edge_id']}",
        f"new {row['generated_crossing_candidate_lon']:.6f}, {row['generated_crossing_candidate_lat']:.6f}",
        f"road edge {row['nearest_road_edge_id']} | pair edge {row['opposite_or_pair_edge_id'] or 'none'}",
        f"TLS {row['nearest_tls_id'] or 'none'} | tls dist {row['nearest_tls_distance_m']:.1f}m",
    ]
    y = 0.96
    for line in lines:
        ax_text.text(0.01, y, line, family="monospace", fontsize=9.3, va="top")
        y -= 0.16

    ax_map.set_facecolor("#fcfbf7")
    target_xy = _xy_from_lonlat(net, row["real_lon"], row["real_lat"])
    if target_xy is not None:
        try:
            road_edge = net.getEdge(str(row["nearest_road_edge_id"])) if row["nearest_road_edge_id"] else None
        except Exception:
            road_edge = None
        try:
            pair_edge = net.getEdge(str(row["opposite_or_pair_edge_id"])) if row["opposite_or_pair_edge_id"] else None
        except Exception:
            pair_edge = None
        old_xy = _xy_from_lonlat(net, row["old_sumo_lon"], row["old_sumo_lat"])
        new_xy = _xy_from_lonlat(net, row["generated_crossing_candidate_lon"], row["generated_crossing_candidate_lat"])
        if road_edge is not None:
            xs, ys = zip(*[_lonlat_from_xy(net, x, y) or (math.nan, math.nan) for x, y in road_edge.getShape()])
            ax_map.plot(xs, ys, color="#ef8a62", linewidth=2.0, alpha=0.8, label="nearest road edge")
        if pair_edge is not None:
            xs, ys = zip(*[_lonlat_from_xy(net, x, y) or (math.nan, math.nan) for x, y in pair_edge.getShape()])
            ax_map.plot(xs, ys, color="#756bb1", linewidth=1.7, alpha=0.7, label="pair edge")
        if row["nearest_tls_id"]:
            try:
                node = net.getNode(str(row["nearest_tls_id"]))
                tl_lonlat = _lonlat_from_xy(net, *node.getCoord())
                if tl_lonlat is not None:
                    ax_map.scatter([tl_lonlat[0]], [tl_lonlat[1]], s=130, marker="*", c="#2c7fb8", edgecolors="white", linewidths=0.8, zorder=6, label="nearest TLS")
            except Exception:
                pass
        ax_map.scatter([row["real_lon"]], [row["real_lat"]], s=130, c="#1a9850", edgecolors="white", linewidths=0.8, zorder=7, label="real")
        ax_map.scatter([row["old_sumo_lon"]], [row["old_sumo_lat"]], s=130, marker="s", c="#6c757d", edgecolors="#111111", linewidths=0.8, zorder=7, label="old mapped")
        ax_map.scatter([row["generated_crossing_candidate_lon"]], [row["generated_crossing_candidate_lat"]], s=130, marker="^", c="#d73027" if row["geometry_generation_status"] == "GENERATION_REJECT" else "#fdae61" if row["geometry_generation_status"] == "GENERATION_REVIEW" else "#1a9850", edgecolors="#111111", linewidths=0.8, zorder=7, label="generated")
        if old_xy is not None:
            old_pt = _lonlat_from_xy(net, *old_xy)
            if old_pt is not None:
                ax_map.plot([row["real_lon"], row["old_sumo_lon"]], [row["real_lat"], row["old_sumo_lat"]], color="#9e9e9e", linestyle="--", linewidth=1.3, alpha=0.55, zorder=2)
        if new_xy is not None:
            new_pt = _lonlat_from_xy(net, *new_xy)
            if new_pt is not None:
                ax_map.plot([row["real_lon"], row["generated_crossing_candidate_lon"]], [row["real_lat"], row["generated_crossing_candidate_lat"]], color="#d73027", linewidth=1.8, alpha=0.7, zorder=3)
        pad = 0.0018
        xs = [row["real_lon"], row["old_sumo_lon"], row["generated_crossing_candidate_lon"]]
        ys = [row["real_lat"], row["old_sumo_lat"], row["generated_crossing_candidate_lat"]]
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


def _build_summary_md(df: pd.DataFrame) -> str:
    counts = Counter(df["geometry_generation_status"].astype(str).tolist())
    signal_counts = Counter(df["signal_generation_status"].astype(str).tolist())
    final_counts = Counter(df["final_generation_tier"].astype(str).tolist())
    improvement_count = int((df["generated_distance_m"] < df["old_mapping_distance_m"]).sum())
    ok20_count = int((df["generated_distance_m"] <= 20).sum())
    signal_candidate_count = int((df["signal_generation_status"] == "SIGNAL_GENERATION_CANDIDATE").sum())
    unsignalized_count = int((df["signal_generation_status"] == "UNSIGNALIZED_CROSSING_ONLY").sum())
    reject_count = int((df["final_generation_tier"] == "GENERATION_REJECT").sum())
    candidates = df.sort_values(["generated_distance_m", "nearest_road_distance_m", "nearest_tls_distance_m"], ascending=[True, True, True])
    lines = [
        "# BAD 22 Generation Audit Summary",
        "",
        "## Counts",
        f"- total bad candidates: {len(df)}",
        f"- generation possible candidates: {len(df) - reject_count}",
        f"- signal experiment candidates: {signal_candidate_count}",
        f"- unsignalized only candidates: {unsignalized_count}",
        f"- generation rejected: {reject_count}",
        f"- generated distance better than old mapping: {improvement_count}",
        f"- generated distance <= 20m: {ok20_count}",
        "",
        "## Geometry Status",
        f"- GENERATION_GEOMETRY_OK: {int(counts.get('GENERATION_GEOMETRY_OK', 0))}",
        f"- GENERATION_REVIEW: {int(counts.get('GENERATION_REVIEW', 0))}",
        f"- GENERATION_REJECT: {int(counts.get('GENERATION_REJECT', 0))}",
        "",
        "## Signal Status",
        f"- SIGNAL_GENERATION_CANDIDATE: {int(signal_counts.get('SIGNAL_GENERATION_CANDIDATE', 0))}",
        f"- UNSIGNALIZED_CROSSING_ONLY: {int(signal_counts.get('UNSIGNALIZED_CROSSING_ONLY', 0))}",
        "",
        "## Final Generation Tier",
        f"- SIGNAL_GENERATION_CANDIDATE: {int(final_counts.get('SIGNAL_GENERATION_CANDIDATE', 0))}",
        f"- SIGNAL_GENERATION_REVIEW: {int(final_counts.get('SIGNAL_GENERATION_REVIEW', 0))}",
        f"- UNSIGNALIZED_CROSSING_ONLY: {int(final_counts.get('UNSIGNALIZED_CROSSING_ONLY', 0))}",
        f"- UNSIGNALIZED_CROSSING_REVIEW: {int(final_counts.get('UNSIGNALIZED_CROSSING_REVIEW', 0))}",
        f"- GENERATION_REJECT: {int(final_counts.get('GENERATION_REJECT', 0))}",
        f"- final signal-ready candidates: {int(final_counts.get('SIGNAL_GENERATION_CANDIDATE', 0))}",
        f"- final signal-review candidates: {int(final_counts.get('SIGNAL_GENERATION_REVIEW', 0))}",
        "",
        "## First Candidates",
    ]
    for row in candidates.head(10).itertuples(index=False):
        lines.append(
            f"- {row.crosswalk_id}: new {row.generated_distance_m:.1f}m, old {row.old_mapping_distance_m:.1f}m, road {row.nearest_road_distance_m:.1f}m, tls {row.nearest_tls_distance_m:.1f}m, {row.final_generation_tier}"
        )
    lines.extend(
        [
            "",
            "## Report Sentence",
            "- BAD 22 kept out of old crossing-mapping path; each candidate was re-scored from real lon/lat against nearest drivable road edge and nearby TLS, then split into signal-ready, unsignalized-only, review, or reject buckets.",
        ]
    )
    return "\n".join(lines) + "\n"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="BAD 22 generation feasibility audit")
    parser.add_argument("--distance-summary", default=str(DEFAULT_DISTANCE_SUMMARY))
    parser.add_argument("--bad22-csv", default=str(DEFAULT_BAD22))
    parser.add_argument("--stepwise-csv", default=str(DEFAULT_STEPWISE))
    parser.add_argument("--primary-net", default=str(DEFAULT_PRIMARY_NET))
    parser.add_argument("--secondary-net", default=str(DEFAULT_SECONDARY_NET))
    parser.add_argument(
        "--output-dir",
        default=str(RESULT_DIR / f"phase_next_top50_bad_location_generation_audit_{datetime.now().strftime('%Y%m%d_%H%M%S')}"),
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    distance_df = _load_csv(Path(args.distance_summary)).copy()
    bad_df = _load_csv(Path(args.bad22_csv)).copy()

    distance_df["crosswalk_id"] = distance_df["crosswalk_id"].map(_normalize_id)
    bad_df["crosswalk_id"] = bad_df["crosswalk_id"].map(_normalize_id)

    merge_cols = [
        "crosswalk_id",
        "original_rank",
        "real_lon",
        "real_lat",
        "sumo_lon",
        "sumo_lat",
        "mapping_distance_m",
        "distance_grade",
        "current_recovery_tier",
        "sumo_position_source",
        "nearest_junction_id",
        "tls_id_used",
        "crossing_edge_id",
        "ped_link_index",
    ]
    merged = bad_df.merge(
        distance_df[merge_cols].rename(
            columns={
                "sumo_lon": "old_sumo_lon",
                "sumo_lat": "old_sumo_lat",
                "mapping_distance_m": "old_mapping_distance_m",
                "crossing_edge_id": "old_crossing_edge_id",
            }
        ),
        on="crosswalk_id",
        how="left",
        suffixes=("", "_summary"),
    )

    stepwise = _load_csv(Path(args.stepwise_csv)).copy()
    stepwise["crosswalk_id"] = stepwise["crosswalk_id"].map(_normalize_id)
    if "canonical_crosswalk_id" in stepwise.columns:
        stepwise["canonical_crosswalk_id"] = stepwise["canonical_crosswalk_id"].map(_normalize_id)

    primary_net_path = Path(args.primary_net)
    secondary_net_path = Path(args.secondary_net) if args.secondary_net else None
    if not primary_net_path.exists():
        raise FileNotFoundError(primary_net_path)
    if secondary_net_path is not None and not secondary_net_path.exists():
        secondary_net_path = None

    primary_net = read_net(primary_net_path)
    secondary_net = read_net(secondary_net_path) if secondary_net_path is not None else None

    rows: list[dict[str, Any]] = []
    for _, row in merged.iterrows():
        lon = _safe_float(row.get("real_lon"))
        lat = _safe_float(row.get("real_lat"))
        target_xy = _xy_from_lonlat(primary_net, lon, lat)
        net_used = primary_net
        if target_xy is None and secondary_net is not None:
            target_xy = _xy_from_lonlat(secondary_net, lon, lat)
            net_used = secondary_net if target_xy is not None else primary_net
        if target_xy is None:
            rows.append(
                {
                    "crosswalk_id": _normalize_id(row.get("crosswalk_id")),
                    "original_rank": int(_safe_float(row.get("original_rank")) or 0),
                    "real_lon": lon,
                    "real_lat": lat,
                    "old_sumo_lon": _safe_float(row.get("old_sumo_lon")),
                    "old_sumo_lat": _safe_float(row.get("old_sumo_lat")),
                    "old_mapping_distance_m": _safe_float(row.get("old_mapping_distance_m")),
                    "old_crossing_edge_id": _normalize_id(row.get("old_crossing_edge_id")),
                    "nearest_road_edge_id": "",
                    "nearest_road_distance_m": math.nan,
                    "opposite_or_pair_edge_id": "",
                    "generated_crossing_candidate_lon": math.nan,
                    "generated_crossing_candidate_lat": math.nan,
                    "generated_distance_m": math.nan,
                    "nearest_tls_id": "",
                    "nearest_tls_distance_m": math.nan,
                    "geometry_generation_status": "GENERATION_REJECT",
                    "signal_generation_status": "UNSIGNALIZED_CROSSING_ONLY",
                    "final_generation_tier": "GENERATION_REJECT",
                    "recommended_action": "exclude from generation",
                    "reason": "coordinate conversion failed",
                }
            )
            continue

        nearest_edge, nearest_pt_xy, nearest_road_distance = _find_nearest_vehicle_edge(net_used, target_xy)
        opposite_edge = None
        opposite_pt_xy = None
        opposite_dist = math.nan
        if nearest_edge is not None:
            opposite_edge, opposite_pt_xy, opposite_dist = _find_pair_edge(net_used, nearest_edge, target_xy)

        if nearest_pt_xy is not None and opposite_pt_xy is not None:
            gen_xy = ((nearest_pt_xy[0] + opposite_pt_xy[0]) / 2.0, (nearest_pt_xy[1] + opposite_pt_xy[1]) / 2.0)
        elif nearest_pt_xy is not None:
            gen_xy = nearest_pt_xy
        else:
            gen_xy = None

        gen_lonlat = _lonlat_from_xy(net_used, *gen_xy) if gen_xy is not None else None
        generated_distance_m = math.dist(target_xy, gen_xy) if gen_xy is not None else math.nan
        nearest_tls_id, nearest_tls_xy, nearest_tls_distance = _nearest_tls(net_used, target_xy)
        ped_link_possible = _ped_link_possible(net_used, nearest_tls_id)
        geometry_status = _geometry_status(generated_distance_m)
        signal_status = _signal_status(nearest_tls_distance, ped_link_possible)
        final_tier = _final_tier(geometry_status, signal_status)
        old_dist = _safe_float(row.get("old_mapping_distance_m"))
        old_crossing = _normalize_id(row.get("old_crossing_edge_id"))
        reason_bits = [
            f"old mapping {old_dist:.1f}m away",
            f"nearest road edge {nearest_road_distance:.1f}m",
            f"pair edge {'found' if opposite_edge is not None else 'missing'}",
            f"tls {'near' if not pd.isna(nearest_tls_distance) and nearest_tls_distance <= 50 else 'far or missing'}",
            f"generated {generated_distance_m:.1f}m",
        ]
        if old_crossing:
            reason_bits.append(f"old crossing {old_crossing}")
        if not pd.isna(generated_distance_m) and not pd.isna(old_dist) and generated_distance_m < old_dist:
            reason_bits.append("generated improves old mapping")
        elif not pd.isna(generated_distance_m) and not pd.isna(old_dist):
            reason_bits.append("generated not better than old mapping")

        rows.append(
            {
                "crosswalk_id": _normalize_id(row.get("crosswalk_id")),
                "original_rank": int(_safe_float(row.get("original_rank")) or 0),
                "real_lon": lon,
                "real_lat": lat,
                "old_sumo_lon": _safe_float(row.get("old_sumo_lon")),
                "old_sumo_lat": _safe_float(row.get("old_sumo_lat")),
                "old_mapping_distance_m": old_dist,
                "old_crossing_edge_id": old_crossing,
                "nearest_road_edge_id": _normalize_id(nearest_edge.getID()) if nearest_edge is not None else "",
                "nearest_road_distance_m": float(nearest_road_distance) if not pd.isna(nearest_road_distance) else math.nan,
                "opposite_or_pair_edge_id": _normalize_id(opposite_edge.getID()) if opposite_edge is not None else "",
                "generated_crossing_candidate_lon": gen_lonlat[0] if gen_lonlat is not None else math.nan,
                "generated_crossing_candidate_lat": gen_lonlat[1] if gen_lonlat is not None else math.nan,
                "generated_distance_m": float(generated_distance_m) if not pd.isna(generated_distance_m) else math.nan,
                "nearest_tls_id": _normalize_id(nearest_tls_id),
                "nearest_tls_distance_m": float(nearest_tls_distance) if not pd.isna(nearest_tls_distance) else math.nan,
                "geometry_generation_status": geometry_status,
                "signal_generation_status": signal_status,
                "final_generation_tier": final_tier,
                "recommended_action": _recommended_action(final_tier),
                "reason": "; ".join(reason_bits),
            }
        )

    out = pd.DataFrame(rows)
    out["geometry_generation_status"] = pd.Categorical(
        out["geometry_generation_status"],
        categories=["GENERATION_GEOMETRY_OK", "GENERATION_REVIEW", "GENERATION_REJECT"],
        ordered=True,
    )
    out["signal_generation_status"] = pd.Categorical(
        out["signal_generation_status"],
        categories=["SIGNAL_GENERATION_CANDIDATE", "UNSIGNALIZED_CROSSING_ONLY"],
        ordered=True,
    )
    out["final_generation_tier"] = pd.Categorical(
        out["final_generation_tier"],
        categories=[
            "SIGNAL_GENERATION_CANDIDATE",
            "SIGNAL_GENERATION_REVIEW",
            "UNSIGNALIZED_CROSSING_ONLY",
            "UNSIGNALIZED_CROSSING_REVIEW",
            "GENERATION_REJECT",
        ],
        ordered=True,
    )
    out = out.sort_values(["final_generation_tier", "generated_distance_m", "nearest_road_distance_m", "nearest_tls_distance_m", "original_rank"], ascending=[True, True, True, True, True], kind="stable").reset_index(drop=True)

    audit_path = out_dir / "bad22_generation_feasibility_audit.csv"
    priority_path = out_dir / "bad22_generation_priority.csv"
    overview_path = out_dir / "bad22_generation_visual_overview.png"
    summary_path = out_dir / "bad22_generation_audit_summary.md"
    cards_dir = out_dir / "bad22_generation_zoom_cards"
    cards_dir.mkdir(parents=True, exist_ok=True)

    out.to_csv(audit_path, index=False)
    priority_cols = [
        "crosswalk_id",
        "original_rank",
        "real_lon",
        "real_lat",
        "old_mapping_distance_m",
        "nearest_road_distance_m",
        "nearest_tls_distance_m",
        "generated_distance_m",
        "geometry_generation_status",
        "signal_generation_status",
        "final_generation_tier",
        "recommended_action",
    ]
    priority = out.copy()
    priority["priority_score"] = (
        (priority["old_mapping_distance_m"].fillna(9999) - priority["generated_distance_m"].fillna(9999))
        + (50.0 - priority["nearest_tls_distance_m"].fillna(50.0)).clip(lower=0)
        + (40.0 - priority["nearest_road_distance_m"].fillna(40.0)).clip(lower=0)
    )
    priority = priority.sort_values(
        ["priority_score", "generated_distance_m", "nearest_tls_distance_m", "nearest_road_distance_m", "original_rank"],
        ascending=[False, True, True, True, True],
        kind="stable",
    )
    priority.to_csv(priority_path, index=False)

    for row in out.itertuples(index=False):
        safe_id = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(row.crosswalk_id))
        _render_card(cards_dir / f"crosswalk_{safe_id}.png", pd.Series(row._asdict()), net_used=primary_net)

    _render_overview(overview_path, out)
    summary_md = _build_summary_md(out)
    summary_path.write_text(summary_md, encoding="utf-8")

    print(f"output_dir={out_dir}")
    print(f"audit_csv={audit_path}")
    print(f"priority_csv={priority_path}")
    print(f"overview_png={overview_path}")
    print(f"cards_dir={cards_dir}")
    print(f"bad22_total={len(out)}")
    print(f"generation_possible={int((out['final_generation_tier'] != 'GENERATION_REJECT').sum())}")
    print(f"signal_candidate={int((out['signal_generation_status'] == 'SIGNAL_GENERATION_CANDIDATE').sum())}")
    print(f"unsignalized_only={int((out['signal_generation_status'] == 'UNSIGNALIZED_CROSSING_ONLY').sum())}")
    print(f"generated_better_than_old={int((out['generated_distance_m'] < out['old_mapping_distance_m']).sum())}")
    print(f"generated_le_20m={int((out['generated_distance_m'] <= 20).sum())}")
    print(f"reject_count={int((out['final_generation_tier'] == 'GENERATION_REJECT').sum())}")


def _render_card(out_path: Path, row: pd.Series, net_used: Any) -> None:
    ensure_matplotlib_env()
    import matplotlib

    matplotlib.use("Agg")
    configure_matplotlib(matplotlib)
    import matplotlib.pyplot as plt

    fig, (ax_text, ax_map) = plt.subplots(2, 1, figsize=(10.5, 11), gridspec_kw={"height_ratios": [1.0, 2.2]})
    ax_text.axis("off")
    lines = [
        f"{row['crosswalk_id']} | rank {int(row['original_rank'])}",
        f"old {row['old_mapping_distance_m']:.1f}m -> new {row['generated_distance_m']:.1f}m",
        f"geometry {row['geometry_generation_status']} | signal {row['signal_generation_status']} | final {row['final_generation_tier']}",
        f"real {row['real_lon']:.6f}, {row['real_lat']:.6f}",
        f"old mapped {row['old_sumo_lon']:.6f}, {row['old_sumo_lat']:.6f}",
        f"generated {row['generated_crossing_candidate_lon']:.6f}, {row['generated_crossing_candidate_lat']:.6f}",
        f"road {row['nearest_road_edge_id']} @ {row['nearest_road_distance_m']:.1f}m",
        f"pair {row['opposite_or_pair_edge_id'] or 'none'} | tls {row['nearest_tls_id'] or 'none'} @ {row['nearest_tls_distance_m']:.1f}m",
    ]
    y = 0.95
    for line in lines:
        ax_text.text(0.01, y, line, family="monospace", fontsize=9.2, va="top")
        y -= 0.13

    ax_map.set_facecolor("#faf8f2")
    try:
        road_edge = net_used.getEdge(str(row["nearest_road_edge_id"])) if row["nearest_road_edge_id"] else None
    except Exception:
        road_edge = None
    try:
        pair_edge = net_used.getEdge(str(row["opposite_or_pair_edge_id"])) if row["opposite_or_pair_edge_id"] else None
    except Exception:
        pair_edge = None
    if road_edge is not None:
        coords = []
        for x, y in road_edge.getShape():
            lonlat = _lonlat_from_xy(net_used, x, y)
            if lonlat is not None:
                coords.append(lonlat)
        if len(coords) >= 2:
            ax_map.plot([c[0] for c in coords], [c[1] for c in coords], color="#ef8a62", linewidth=2.1, alpha=0.85, label="nearest road edge")
    if pair_edge is not None:
        coords = []
        for x, y in pair_edge.getShape():
            lonlat = _lonlat_from_xy(net_used, x, y)
            if lonlat is not None:
                coords.append(lonlat)
        if len(coords) >= 2:
            ax_map.plot([c[0] for c in coords], [c[1] for c in coords], color="#756bb1", linewidth=1.9, alpha=0.8, label="pair edge")
    if row["nearest_tls_id"]:
        try:
            node = net_used.getNode(str(row["nearest_tls_id"]))
            tl_lonlat = _lonlat_from_xy(net_used, *node.getCoord())
            if tl_lonlat is not None:
                ax_map.scatter([tl_lonlat[0]], [tl_lonlat[1]], s=150, marker="*", c="#2c7fb8", edgecolors="white", linewidths=0.8, zorder=6, label="nearest TLS")
        except Exception:
            pass
    ax_map.scatter([row["real_lon"]], [row["real_lat"]], s=140, c="#1a9850", edgecolors="white", linewidths=0.8, zorder=7, label="real")
    ax_map.scatter([row["old_sumo_lon"]], [row["old_sumo_lat"]], s=140, marker="s", c="#6c757d", edgecolors="#111111", linewidths=0.8, zorder=7, label="old crossing")
    ax_map.scatter([row["generated_crossing_candidate_lon"]], [row["generated_crossing_candidate_lat"]], s=140, marker="^", c="#d73027" if row["geometry_generation_status"] == "GENERATION_REJECT" else "#fdae61" if row["geometry_generation_status"] == "GENERATION_REVIEW" else "#1a9850", edgecolors="#111111", linewidths=0.8, zorder=7, label="generated")
    ax_map.plot([row["real_lon"], row["old_sumo_lon"]], [row["real_lat"], row["old_sumo_lat"]], color="#9e9e9e", linestyle="--", linewidth=1.2, alpha=0.55, zorder=2)
    if pd.notna(row["generated_crossing_candidate_lon"]) and pd.notna(row["generated_crossing_candidate_lat"]):
        ax_map.plot([row["real_lon"], row["generated_crossing_candidate_lon"]], [row["real_lat"], row["generated_crossing_candidate_lat"]], color="#d73027", linewidth=1.8, alpha=0.7, zorder=3)
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


if __name__ == "__main__":
    main()
