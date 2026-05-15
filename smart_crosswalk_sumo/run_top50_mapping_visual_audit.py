#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import re
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

import pandas as pd

from smart_crosswalk_sumo.mpl_runtime import configure_matplotlib, ensure_matplotlib_env
from smart_crosswalk_sumo.network_utils import edge_center, edge_function, read_net


BASE_DIR = Path(__file__).resolve().parents[1]
RESULT_DIR = BASE_DIR / "result"
DEFAULT_STEPWISE_50M = BASE_DIR / "smart_crosswalk_sumo" / "data" / "crosswalk_stepwise_result_50m.csv"
DEFAULT_TOP50_AUDIT = RESULT_DIR / "phase_next_top50_tiered_recovery_audit" / "top50_tiered_recovery_audit.csv"
DEFAULT_READY = RESULT_DIR / "phase_next_top50_tiered_recovery_audit" / "top50_phase6_ready_pedestrian_only_candidates.csv"
DEFAULT_MIXED = RESULT_DIR / "phase_next_top50_tiered_recovery_audit" / "top50_mixed_phase_redesign_input.csv"
DEFAULT_NET_PRIMARY = RESULT_DIR / "phase_next_recovery_command_plan_20260514_200908" / "batch_03_run" / "recovery_tls_batch_network_v1.net.xml"
DEFAULT_NET_SECONDARY = RESULT_DIR / "phase_next_top50_mixed_phase_redesign" / "mixed_phase_redesign_network.net.xml"
DEFAULT_BOUNDARY = BASE_DIR / "smart_crosswalk_sumo" / "data" / "junggu_admin_boundary.geojson"


def _normalize_id(value: Any) -> str:
    text = str(value or "").strip()
    if not text or text.lower() in {"nan", "none", "nat"}:
        return ""
    if text.endswith(".0"):
        raw = text[:-2]
        if raw.replace("-", "", 1).isdigit():
            return raw
    return text


def _canonical_id(value: Any) -> str:
    text = _normalize_id(value)
    if not text:
        return ""
    if text.startswith("NODE_") or text.startswith("LINK_"):
        return _canonical_id(text.split("_", 1)[1])
    digits = "".join(ch for ch in text if ch.isdigit())
    return str(int(digits)) if digits else text


def _safe_float(value: Any) -> float:
    try:
        if pd.isna(value):
            return math.nan
        return float(value)
    except Exception:
        return math.nan


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


def _load_csv(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame()
    return pd.read_csv(path)


def _top50_candidates_from_dir(base_dir: Path) -> pd.DataFrame:
    preferred = [
        base_dir / "top50_tiered_recovery_audit.csv",
        base_dir / "top50_requested_candidates.csv",
        base_dir / "top50_phase6_ready_pedestrian_only_candidates.csv",
        base_dir / "top50_mixed_phase_redesign_input.csv",
        base_dir / "top50_red_only_phase_fix_input.csv",
        base_dir / "top50_route_fix_input.csv",
        base_dir / "top50_manual_review_hold.csv",
    ]
    frames: list[tuple[int, pd.DataFrame]] = []
    for priority, path in enumerate(preferred):
        df = _load_csv(path)
        if not df.empty and {"crosswalk_id", "original_rank"}.issubset(df.columns):
            frames.append((priority, df.copy()))

    if not frames:
        return pd.DataFrame()

    merged: dict[str, dict[str, Any]] = {}
    row_source: dict[str, int] = {}
    for priority, df in sorted(frames, key=lambda x: x[0]):
        for _, row in df.iterrows():
            rec = row.to_dict()
            crosswalk_id = _normalize_id(rec.get("crosswalk_id"))
            canonical_id = _canonical_id(rec.get("canonical_crosswalk_id") or crosswalk_id)
            key = canonical_id or crosswalk_id
            if not key:
                continue
            rec["crosswalk_id"] = crosswalk_id or key
            rec["source_crosswalk_id"] = _normalize_id(rec.get("source_crosswalk_id")) or rec["crosswalk_id"]
            rec["canonical_crosswalk_id"] = canonical_id or _canonical_id(rec["crosswalk_id"])
            rec["original_rank"] = int(_safe_float(rec.get("original_rank")) or 0)
            if key not in merged or priority < row_source[key]:
                merged[key] = rec
                row_source[key] = priority

    out = pd.DataFrame(list(merged.values()))
    if out.empty:
        return out
    if "original_rank" in out.columns:
        out = out.sort_values(["original_rank", "canonical_crosswalk_id", "crosswalk_id"], kind="stable")
    return out.reset_index(drop=True)


def _infer_top50(df_candidates: pd.DataFrame, stepwise: pd.DataFrame) -> pd.DataFrame:
    if not df_candidates.empty:
        out = df_candidates.copy()
        if "lon" not in out.columns or "lat" not in out.columns:
            raise ValueError("top50 candidate rows need lon/lat columns")
        return out.head(50).reset_index(drop=True)
    if stepwise.empty:
        raise FileNotFoundError("Top 50 source CSV not found")
    out = stepwise.head(50).copy().reset_index(drop=True)
    if "original_rank" not in out.columns:
        out.insert(0, "original_rank", range(1, len(out) + 1))
    return out


def _parse_boundary(boundary_path: Path) -> list[list[tuple[float, float]]]:
    if not boundary_path.exists():
        return []
    raw = json.loads(boundary_path.read_text(encoding="utf-8"))
    rings: list[list[tuple[float, float]]] = []
    for feat in raw.get("features", []):
        geom = feat.get("geometry") or {}
        if geom.get("type") == "Polygon":
            for ring in geom.get("coordinates", []):
                rings.append([(float(lon), float(lat)) for lon, lat in ring])
        elif geom.get("type") == "MultiPolygon":
            for poly in geom.get("coordinates", []):
                for ring in poly:
                    rings.append([(float(lon), float(lat)) for lon, lat in ring])
    return rings


def _location_attrs(net_file: Path) -> dict[str, str]:
    root = ET.parse(net_file).getroot()
    loc = root.find("location")
    if loc is None:
        return {}
    return dict(loc.attrib)


def _xy_to_lonlat(net: Any, xy: tuple[float, float]) -> tuple[float, float] | None:
    try:
        lon, lat = net.convertXY2LonLat(float(xy[0]), float(xy[1]))
        return float(lon), float(lat)
    except Exception:
        return None


def _lonlat_to_xy(net: Any, lon: float, lat: float) -> tuple[float, float] | None:
    try:
        x, y = net.convertLonLat2XY(float(lon), float(lat))
        return float(x), float(y)
    except Exception:
        return None


def _edge_shape_lonlat(net: Any, edge_id: str) -> list[tuple[float, float]]:
    try:
        edge = net.getEdge(edge_id)
    except Exception:
        return []
    out: list[tuple[float, float]] = []
    for x, y in edge.getShape():
        lonlat = _xy_to_lonlat(net, (float(x), float(y)))
        if lonlat is not None:
            out.append(lonlat)
    return out


def _node_lonlat(net: Any, node_id: str) -> tuple[float, float] | None:
    try:
        node = net.getNode(node_id)
    except Exception:
        return None
    try:
        lon, lat = net.convertXY2LonLat(*node.getCoord())
        return float(lon), float(lat)
    except Exception:
        return None


def _traffic_light_ids(net: Any) -> set[str]:
    try:
        return {str(tls.getID()) for tls in net.getTrafficLights()}
    except Exception:
        return set()


def _nearest_traffic_light(net: Any, lon: float, lat: float) -> tuple[str, tuple[float, float] | None, float]:
    target_xy = _lonlat_to_xy(net, lon, lat)
    if target_xy is None:
        return "", None, math.nan
    tls_ids = _traffic_light_ids(net)
    best_id = ""
    best_xy: tuple[float, float] | None = None
    best_dist = float("inf")
    for tls_id in tls_ids:
        try:
            node = net.getNode(tls_id)
        except Exception:
            continue
        node_xy = node.getCoord()
        dist = math.dist(target_xy, node_xy)
        if dist < best_dist:
            best_dist = dist
            best_id = tls_id
            best_xy = (float(node_xy[0]), float(node_xy[1]))
    if not best_id:
        return "", None, math.nan
    lonlat = _xy_to_lonlat(net, best_xy) if best_xy is not None else None
    return best_id, lonlat, best_dist


def _crossing_center_from_net(net: Any, crossing_edge_id: str) -> tuple[tuple[float, float] | None, str]:
    if not crossing_edge_id:
        return None, ""
    try:
        edge = net.getEdge(crossing_edge_id)
    except Exception:
        return None, ""
    center = edge_center(edge)
    lonlat = _xy_to_lonlat(net, center)
    if lonlat is None:
        return None, ""
    return lonlat, "crossing_edge_center"


def _walkingarea_center_from_tls(net: Any, tls_id: str, target_lon: float, target_lat: float) -> tuple[tuple[float, float] | None, str]:
    if not tls_id:
        return None, ""
    try:
        node = net.getNode(tls_id)
    except Exception:
        return None, ""
    walking_edges: list[Any] = []
    for edge in list(node.getIncoming()) + list(node.getOutgoing()):
        if edge_function(edge) == "walkingarea":
            walking_edges.append(edge)
    if not walking_edges:
        return None, ""
    target_xy = _lonlat_to_xy(net, target_lon, target_lat)
    if target_xy is None:
        edge = walking_edges[0]
        lonlat = _xy_to_lonlat(net, edge_center(edge))
        return lonlat, "walkingarea_center"
    best: tuple[float, float] | None = None
    best_dist = float("inf")
    for edge in walking_edges:
        center = edge_center(edge)
        dist = math.dist(target_xy, center)
        if dist < best_dist:
            best_dist = dist
            best = _xy_to_lonlat(net, center)
    return best, "walkingarea_center"


def _choose_sumo_location(
    row: pd.Series,
    primary: tuple[str, Any],
    secondary: tuple[str, Any] | None,
) -> dict[str, Any]:
    primary_name, primary_net = primary
    secondary_name, secondary_net = secondary if secondary is not None else ("", None)
    real_lon = _safe_float(row.get("lon"))
    real_lat = _safe_float(row.get("lat"))
    crossing_edge_id = _normalize_id(row.get("crossing_edge_id"))
    tls_id_used = _normalize_id(row.get("tls_id_used")) or _normalize_id(row.get("nearest_junction_id"))

    for network_name, net in [(primary_name, primary_net), (secondary_name, secondary_net)]:
        if net is None:
            continue
        lonlat, source = _crossing_center_from_net(net, crossing_edge_id)
        if lonlat is not None:
            return {
                "sumo_lon": lonlat[0],
                "sumo_lat": lonlat[1],
                "sumo_position_source": source,
                "nearest_junction_id": tls_id_used,
                "tls_id_used": tls_id_used,
                "crossing_edge_id": crossing_edge_id,
                "ped_link_index": int(_safe_float(row.get("ped_link_index"))) if pd.notna(row.get("ped_link_index")) else "",
                "note": f"crossing edge center from {network_name}",
            }

        lonlat, source = _walkingarea_center_from_tls(net, tls_id_used, real_lon, real_lat)
        if lonlat is not None:
            return {
                "sumo_lon": lonlat[0],
                "sumo_lat": lonlat[1],
                "sumo_position_source": source,
                "nearest_junction_id": tls_id_used,
                "tls_id_used": tls_id_used,
                "crossing_edge_id": crossing_edge_id,
                "ped_link_index": int(_safe_float(row.get("ped_link_index"))) if pd.notna(row.get("ped_link_index")) else "",
                "note": f"walkingarea center from {network_name}",
            }

        nearest_id, nearest_lonlat, _ = _nearest_traffic_light(net, real_lon, real_lat)
        if nearest_lonlat is not None:
            return {
                "sumo_lon": nearest_lonlat[0],
                "sumo_lat": nearest_lonlat[1],
                "sumo_position_source": "nearest_junction",
                "nearest_junction_id": nearest_id,
                "tls_id_used": tls_id_used or nearest_id,
                "crossing_edge_id": crossing_edge_id,
                "ped_link_index": int(_safe_float(row.get("ped_link_index"))) if pd.notna(row.get("ped_link_index")) else "",
                "note": f"nearest junction fallback from {network_name}",
            }

    return {
        "sumo_lon": math.nan,
        "sumo_lat": math.nan,
        "sumo_position_source": "missing",
        "nearest_junction_id": tls_id_used,
        "tls_id_used": tls_id_used,
        "crossing_edge_id": crossing_edge_id,
        "ped_link_index": int(_safe_float(row.get("ped_link_index"))) if pd.notna(row.get("ped_link_index")) else "",
        "note": "no SUMO position",
    }


def _distance_grade(distance_m: float) -> str:
    if pd.isna(distance_m):
        return "MISSING"
    if distance_m <= 10:
        return "OK"
    if distance_m <= 20:
        return "GOOD"
    if distance_m <= 50:
        return "REVIEW"
    return "BAD"


def _grade_color(grade: str) -> str:
    return {
        "OK": "#1a9850",
        "GOOD": "#4575b4",
        "REVIEW": "#fdae61",
        "BAD": "#d73027",
        "MISSING": "#7f7f7f",
    }.get(grade, "#7f7f7f")


def _render_network_overview(
    out_path: Path,
    net: Any,
    boundary_rings: list[list[tuple[float, float]]],
    top50: pd.DataFrame,
) -> None:
    ensure_matplotlib_env()
    import matplotlib

    matplotlib.use("Agg")
    configure_matplotlib(matplotlib)
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(13, 13))
    ax.set_facecolor("#f7f5ef")

    for ring in boundary_rings:
        xs = [lon for lon, _ in ring]
        ys = [lat for _, lat in ring]
        ax.plot(xs, ys, color="#111111", linewidth=1.5, alpha=0.85)

    for edge in net.getEdges():
        if edge_function(edge) not in {"normal", "crossing", "walkingarea"}:
            continue
        coords = _edge_shape_lonlat(net, edge.getID())
        if len(coords) < 2:
            continue
        xs = [lon for lon, _ in coords]
        ys = [lat for _, lat in coords]
        if edge_function(edge) == "crossing":
            ax.plot(xs, ys, color="#c0392b", linewidth=1.1, alpha=0.55, zorder=2)
        elif edge_function(edge) == "walkingarea":
            ax.plot(xs, ys, color="#2c7fb8", linewidth=0.8, alpha=0.25, zorder=1)
        else:
            ax.plot(xs, ys, color="#6c757d", linewidth=0.45, alpha=0.18, zorder=0)

    pts = top50.dropna(subset=["real_lon", "real_lat"])
    ax.scatter(pts["real_lon"], pts["real_lat"], s=35, c="#ff8c00", edgecolors="white", linewidths=0.6, zorder=5)
    for row in pts.itertuples(index=False):
        ax.text(row.real_lon, row.real_lat, f" {row.crosswalk_id}", fontsize=6, color="#5a3e00", zorder=6)

    ax.set_title("01 Jung-gu Network Overview", fontsize=14, weight="bold")
    ax.set_xlabel("Longitude")
    ax.set_ylabel("Latitude")
    ax.set_aspect("equal", adjustable="box")
    ax.grid(alpha=0.12)
    fig.tight_layout()
    fig.savefig(out_path, dpi=180, bbox_inches="tight")
    plt.close(fig)


def _render_mapping_overview(out_path: Path, top50: pd.DataFrame) -> None:
    ensure_matplotlib_env()
    import matplotlib

    matplotlib.use("Agg")
    configure_matplotlib(matplotlib)
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(13, 13))
    ax.set_facecolor("#fcfbf7")
    for grade in ["OK", "GOOD", "REVIEW", "BAD", "MISSING"]:
        sub = top50[top50["distance_grade"] == grade]
        if sub.empty:
            continue
        color = _grade_color(grade)
        ax.scatter(
            sub["real_lon"],
            sub["real_lat"],
            s=38,
            c=color,
            edgecolors="white",
            linewidths=0.6,
            label=f"Real {grade}",
            zorder=4,
        )
        valid = sub.dropna(subset=["sumo_lon", "sumo_lat"])
        if not valid.empty:
            ax.scatter(
                valid["sumo_lon"],
                valid["sumo_lat"],
                s=45,
                marker="s",
                c=color,
                edgecolors="#111111",
                linewidths=0.6,
                label=f"SUMO {grade}",
                zorder=5,
            )
        for row in valid.itertuples(index=False):
            ax.plot(
                [row.real_lon, row.sumo_lon],
                [row.real_lat, row.sumo_lat],
                color=color,
                linewidth=1.2,
                alpha=0.65,
                zorder=3,
            )

    missing = top50[top50["distance_grade"] == "MISSING"]
    if not missing.empty:
        for row in missing.itertuples(index=False):
            ax.text(row.real_lon, row.real_lat, f" {row.crosswalk_id}", fontsize=6, color="#666666")

    ax.set_title("02 Top 50 Real vs SUMO Overview", fontsize=14, weight="bold")
    ax.set_xlabel("Longitude")
    ax.set_ylabel("Latitude")
    ax.set_aspect("equal", adjustable="box")
    ax.grid(alpha=0.12)
    ax.legend(loc="best", fontsize=8, frameon=True)
    fig.tight_layout()
    fig.savefig(out_path, dpi=180, bbox_inches="tight")
    plt.close(fig)


def _render_card(out_path: Path, row: pd.Series) -> None:
    ensure_matplotlib_env()
    import matplotlib

    matplotlib.use("Agg")
    configure_matplotlib(matplotlib)
    import matplotlib.pyplot as plt

    fig = plt.figure(figsize=(9.5, 10.5))
    gs = fig.add_gridspec(2, 1, height_ratios=[1.0, 2.2])
    ax_text = fig.add_subplot(gs[0, 0])
    ax_map = fig.add_subplot(gs[1, 0])

    ax_text.axis("off")
    lines = [
        f"{row.crosswalk_id}  |  rank {int(row.original_rank)}  |  tier {row.current_recovery_tier}",
        f"distance {row.mapping_distance_m:.1f} m  |  grade {row.distance_grade}  |  review {bool(row.visual_review_needed)}",
        f"real {row.real_lon:.6f}, {row.real_lat:.6f}",
        f"sumo {'' if pd.isna(row.sumo_lon) else f'{row.sumo_lon:.6f}'}, {'' if pd.isna(row.sumo_lat) else f'{row.sumo_lat:.6f}'}",
        f"source {row.sumo_position_source}  |  tls {row.tls_id_used}  |  crossing {row.crossing_edge_id}",
    ]
    y = 0.95
    for line in lines:
        ax_text.text(0.01, y, line, family="monospace", fontsize=9.5, va="top", color="#1f1f1f")
        y -= 0.19

    color = _grade_color(row.distance_grade)
    ax_map.set_facecolor("#fbfaf6")
    ax_map.scatter([row.real_lon], [row.real_lat], s=120, c="#1b9e77", edgecolors="white", linewidths=0.8, zorder=4, label="Real")
    if pd.notna(row.sumo_lon) and pd.notna(row.sumo_lat):
        ax_map.scatter([row.sumo_lon], [row.sumo_lat], s=120, c=color, marker="s", edgecolors="#111111", linewidths=0.8, zorder=5, label="SUMO")
        ax_map.plot([row.real_lon, row.sumo_lon], [row.real_lat, row.sumo_lat], color=color, linewidth=2.0, alpha=0.8, zorder=3)
    else:
        ax_map.text(0.5, 0.5, "SUMO position missing", transform=ax_map.transAxes, ha="center", va="center", fontsize=12)

    lon_pad = 0.0013
    lat_pad = 0.0011
    ax_map.set_xlim(min(row.real_lon, row.sumo_lon if pd.notna(row.sumo_lon) else row.real_lon) - lon_pad, max(row.real_lon, row.sumo_lon if pd.notna(row.sumo_lon) else row.real_lon) + lon_pad)
    ax_map.set_ylim(min(row.real_lat, row.sumo_lat if pd.notna(row.sumo_lat) else row.real_lat) - lat_pad, max(row.real_lat, row.sumo_lat if pd.notna(row.sumo_lat) else row.real_lat) + lat_pad)
    ax_map.set_aspect("equal", adjustable="box")
    ax_map.grid(alpha=0.15)
    ax_map.set_xlabel("Longitude")
    ax_map.set_ylabel("Latitude")
    ax_map.set_title(f"{row.crosswalk_id} distance {row.mapping_distance_m:.1f} m")
    ax_map.legend(loc="upper right", fontsize=8)
    fig.tight_layout()
    fig.savefig(out_path, dpi=180, bbox_inches="tight")
    plt.close(fig)


def _distance_summary(top50: pd.DataFrame) -> dict[str, int]:
    counts = Counter(top50["distance_grade"].astype(str).tolist())
    return {grade: int(counts.get(grade, 0)) for grade in ["OK", "GOOD", "REVIEW", "BAD", "MISSING"]}


def _build_report(out_dir: Path, top50: pd.DataFrame, net_meta: dict[str, str], primary_net: Path, secondary_net: Path | None) -> str:
    counts = _distance_summary(top50)
    good_mask = top50["distance_grade"].isin(["OK", "GOOD"])
    suspicious_mask = top50["distance_grade"].isin(["REVIEW", "BAD", "MISSING"]) | top50["visual_review_needed"].astype(bool)
    good_ids = top50.loc[good_mask, "crosswalk_id"].astype(str).tolist()
    suspicious_ids = top50.loc[suspicious_mask].sort_values(["mapping_distance_m", "crosswalk_id"], ascending=[False, True])["crosswalk_id"].astype(str).tolist()
    top10 = top50.sort_values(["mapping_distance_m", "crosswalk_id"], ascending=[False, True]).head(10)
    missing_count = int((top50["distance_grade"] == "MISSING").sum())
    crossing_count = int((top50["sumo_position_source"].astype(str).str.contains("crossing_edge_center")).sum())
    fallback_count = int((top50["sumo_position_source"].astype(str).str.contains("nearest_junction")).sum())

    lines = [
        "# 05 Mapping Visual Audit",
        "",
        "## Summary",
        f"- total candidates: {len(top50)}",
        f"- crossing center matches: {crossing_count}",
        f"- nearest junction fallback: {fallback_count}",
        f"- SUMO missing: {missing_count}",
        f"- 10m or less: {counts['OK']}",
        f"- 10-20m: {counts['GOOD']}",
        f"- 20-50m: {counts['REVIEW']}",
        f"- 50m over: {counts['BAD']}",
        "",
        "## Grade Counts",
        f"- OK: {counts['OK']}",
        f"- GOOD: {counts['GOOD']}",
        f"- REVIEW: {counts['REVIEW']}",
        f"- BAD: {counts['BAD']}",
        f"- MISSING: {counts['MISSING']}",
        "",
        "## Good Looking Candidates",
        "- " + (", ".join(good_ids) if good_ids else "none"),
        "",
        "## Suspicious Candidates",
        "- " + (", ".join(suspicious_ids) if suspicious_ids else "none"),
        "",
        "## Top 10 Worst Distance",
    ]
    for row in top10.itertuples(index=False):
        lines.append(
            f"- {row.crosswalk_id}: {row.mapping_distance_m:.1f}m, {row.distance_grade}, {row.sumo_position_source}, review={bool(row.visual_review_needed)}"
        )

    lines.extend(
        [
            "",
            "## Network Check",
            f"- primary net: {primary_net}",
            f"- secondary net: {secondary_net if secondary_net else 'none'}",
            f"- location attrs: {json.dumps(net_meta, ensure_ascii=False)}",
            "",
            "## 1st Pass Judgment",
            "- Jung-gu boundary overlay and SUMO edge spread look plausible if edge cloud stays inside boundary polygon and follows main road axes.",
            "- Real vs SUMO proximity looks acceptable for OK/GOOD rows; REVIEW/BAD rows need manual check.",
            "",
            "## Report Sentence",
            "- Batch03 net used as primary anchor, mixed-phase net used only as fallback. Top 50 shows which candidates have crossing-center geometry aligned, and which still rely on nearest-junction fallback or miss SUMO location entirely.",
        ]
    )
    return "\n".join(lines) + "\n"


def _prepare_rows(
    top50: pd.DataFrame,
    primary: tuple[str, Any],
    secondary: tuple[str, Any] | None,
) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for _, row in top50.iterrows():
        real_lon = _safe_float(row.get("lon"))
        real_lat = _safe_float(row.get("lat"))
        resolved = _choose_sumo_location(row, primary, secondary)
        sumo_lon = _safe_float(resolved.get("sumo_lon"))
        sumo_lat = _safe_float(resolved.get("sumo_lat"))
        dist = _haversine_m(real_lon, real_lat, sumo_lon, sumo_lat)
        grade = _distance_grade(dist)
        source = str(resolved.get("sumo_position_source", "missing"))
        visual_review_needed = grade in {"REVIEW", "BAD", "MISSING"} or "nearest_junction" in source
        current_tier = _normalize_id(row.get("current_recovery_tier")) or _normalize_id(row.get("final_tier")) or _normalize_id(row.get("current_recovery_class"))
        if not current_tier:
            current_tier = _normalize_id(row.get("final_tier")) or _normalize_id(row.get("current_recovery_class"))
        rows.append(
            {
                "crosswalk_id": _normalize_id(row.get("crosswalk_id")),
                "source_crosswalk_id": _normalize_id(row.get("source_crosswalk_id")) or _normalize_id(row.get("crosswalk_id")),
                "canonical_crosswalk_id": _normalize_id(row.get("canonical_crosswalk_id")) or _canonical_id(row.get("crosswalk_id")),
                "original_rank": int(_safe_float(row.get("original_rank")) or 0),
                "real_lon": real_lon,
                "real_lat": real_lat,
                "sumo_lon": sumo_lon if not pd.isna(sumo_lon) else math.nan,
                "sumo_lat": sumo_lat if not pd.isna(sumo_lat) else math.nan,
                "sumo_position_source": source,
                "nearest_junction_id": _normalize_id(resolved.get("nearest_junction_id")),
                "tls_id_used": _normalize_id(resolved.get("tls_id_used")),
                "crossing_edge_id": _normalize_id(resolved.get("crossing_edge_id")),
                "ped_link_index": resolved.get("ped_link_index", ""),
                "mapping_distance_m": round(dist, 3) if not pd.isna(dist) else math.nan,
                "distance_grade": grade,
                "current_recovery_tier": current_tier,
                "visual_review_needed": bool(visual_review_needed),
                "note": _normalize_id(resolved.get("note")) or "",
            }
        )
    out = pd.DataFrame(rows)
    order = ["OK", "GOOD", "REVIEW", "BAD", "MISSING"]
    out["distance_grade"] = pd.Categorical(out["distance_grade"], categories=order, ordered=True)
    return out.sort_values(["original_rank", "crosswalk_id"], kind="stable").reset_index(drop=True)


def _generate_cards(top50: pd.DataFrame, out_dir: Path) -> Path:
    cards_dir = out_dir / "04_top50_zoom_cards"
    cards_dir.mkdir(parents=True, exist_ok=True)
    for row in top50.itertuples(index=False):
        safe_id = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(row.crosswalk_id))
        _render_card(cards_dir / f"crosswalk_{safe_id}.png", pd.Series(row._asdict()))
    return cards_dir


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Top 50 mapping visual audit")
    parser.add_argument("--top50-audit-csv", default=str(DEFAULT_TOP50_AUDIT))
    parser.add_argument("--stepwise-50m-csv", default=str(DEFAULT_STEPWISE_50M))
    parser.add_argument("--primary-net", default=str(DEFAULT_NET_PRIMARY))
    parser.add_argument("--secondary-net", default=str(DEFAULT_NET_SECONDARY))
    parser.add_argument("--boundary-geojson", default=str(DEFAULT_BOUNDARY))
    parser.add_argument(
        "--output-dir",
        default=str(RESULT_DIR / f"phase_next_top50_mapping_visual_audit_{datetime.now().strftime('%Y%m%d_%H%M%S')}"),
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    top50_audit = _load_csv(Path(args.top50_audit_csv))
    stepwise = _load_csv(Path(args.stepwise_50m_csv))
    top50_candidates = _top50_candidates_from_dir(Path(args.top50_audit_csv).parent)
    top50 = _infer_top50(top50_candidates if not top50_candidates.empty else top50_audit, stepwise)
    if top50.empty:
        raise FileNotFoundError("Top 50 candidate source empty")

    if "crosswalk_id" not in top50.columns:
        raise ValueError("missing crosswalk_id column")
    if "lon" not in top50.columns or "lat" not in top50.columns:
        raise ValueError("missing lon/lat columns")

    primary_net_path = Path(args.primary_net)
    secondary_net_path = Path(args.secondary_net) if args.secondary_net else None
    if not primary_net_path.exists():
        raise FileNotFoundError(primary_net_path)
    if secondary_net_path is not None and not secondary_net_path.exists():
        secondary_net_path = None

    primary_net = read_net(primary_net_path)
    secondary_net = read_net(secondary_net_path) if secondary_net_path is not None else None
    net_meta = _location_attrs(primary_net_path)
    boundary_rings = _parse_boundary(Path(args.boundary_geojson))

    prepared = _prepare_rows(top50, ("batch03", primary_net), ("mixed", secondary_net) if secondary_net is not None else None)
    prepared["visual_review_needed"] = prepared["visual_review_needed"].astype(bool)
    prepared["current_recovery_tier"] = prepared["current_recovery_tier"].astype(str)

    prepared.to_csv(out_dir / "03_top50_mapping_distance_summary.csv", index=False)
    _render_network_overview(out_dir / "01_junggu_network_overview.png", primary_net, boundary_rings, prepared)
    _render_mapping_overview(out_dir / "02_top50_real_vs_sumo_overview.png", prepared)
    cards_dir = _generate_cards(prepared, out_dir)
    report = _build_report(out_dir, prepared, net_meta, primary_net_path, secondary_net_path)
    (out_dir / "05_mapping_visual_audit.md").write_text(report, encoding="utf-8")

    print(f"output_dir={out_dir}")
    print(f"top50_total={len(prepared)}")
    print(f"cards_dir={cards_dir}")
    print(f"crossing_center_count={int((prepared['sumo_position_source'].astype(str).str.contains('crossing_edge_center')).sum())}")
    print(f"nearest_junction_fallback_count={int((prepared['sumo_position_source'].astype(str).str.contains('nearest_junction')).sum())}")
    print(f"missing_count={int((prepared['distance_grade'] == 'MISSING').sum())}")


if __name__ == "__main__":
    main()
