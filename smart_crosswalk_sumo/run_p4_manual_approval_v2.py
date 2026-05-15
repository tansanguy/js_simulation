#!/usr/bin/env python3
from __future__ import annotations

import argparse
import html
import json
import math
from collections import defaultdict
from pathlib import Path
from typing import Any

import pandas as pd

from smart_crosswalk_sumo.network_utils import edge_function, normal_edges_at_node, read_net
from smart_crosswalk_sumo.run_p4_manual_edge_pair_recovery import (
    DEFAULT_BASE_NET,
    DEFAULT_P4_CANDIDATES,
    _edge_angle_deg,
    _edge_distance_m,
    _haversine_m,
    _nearest_tls,
    _node_distance_m,
    _norm,
    _safe_float,
    _safe_int,
)


BASE_DIR = Path(__file__).resolve().parents[1]
RESULT_DIR = BASE_DIR / "result"
DEFAULT_PLAN_ROOT = RESULT_DIR
DEFAULT_PLAN_GLOB = "phase_next_p1_p4_recovery_experiment_plan_*"


def _latest_plan_dir() -> Path:
    candidates = sorted(DEFAULT_PLAN_ROOT.glob(DEFAULT_PLAN_GLOB))
    if not candidates:
        raise FileNotFoundError(f"no plan dir found under {DEFAULT_PLAN_ROOT}")
    return candidates[-1]


def _csv(path: Path) -> pd.DataFrame:
    if not path.exists():
        raise FileNotFoundError(path)
    return pd.read_csv(path)


def _ensure_dirs(root: Path) -> dict[str, Path]:
    subdirs: dict[str, Path] = {}
    for name in ["csv", "html", "readme"]:
        path = root / name
        path.mkdir(parents=True, exist_ok=True)
        subdirs[name] = path
    return subdirs


def _node_xy(net: Any, node_id: str) -> tuple[float, float] | None:
    try:
        node = net.getNode(node_id)
        x, y = node.getCoord()
        return float(x), float(y)
    except Exception:
        return None


def _node_type(net: Any, node_id: str) -> str:
    try:
        return _norm(net.getNode(node_id).getType()) or "priority"
    except Exception:
        return ""


def _traffic_light_set(net: Any) -> set[str]:
    try:
        return {str(t.getID()) for t in net.getTrafficLights()}
    except Exception:
        return set()


def _edge_root(edge_id: str) -> str:
    text = _norm(edge_id)
    if text.startswith("-"):
        text = text[1:]
    if "#" in text:
        text = text.split("#", 1)[0]
    return text


def _pair_key(edge_a: str, edge_b: str) -> tuple[str, str]:
    return tuple(sorted((_norm(edge_a), _norm(edge_b))))


def _pair_angle_diff(edge_a: Any, edge_b: Any) -> float:
    a = _edge_angle_deg(edge_a)
    b = _edge_angle_deg(edge_b)
    if a == "" or b == "":
        return math.nan
    diff = abs(float(a) - float(b))
    if diff > 180.0:
        diff = 360.0 - diff
    return round(diff, 3)


def _incident_edges(net: Any, node_id: str) -> list[Any]:
    try:
        node = net.getNode(node_id)
    except Exception:
        return []
    ped_edges = normal_edges_at_node(node, "pedestrian")
    if len(ped_edges) >= 2:
        return ped_edges
    return normal_edges_at_node(node)


def _pair_score(row: dict[str, Any]) -> tuple:
    # Lower is better.
    tls_anchor_bonus = 0 if bool(row.get("tls_anchor_match")) else 1
    review_rank = {"APPROVE": 0, "HOLD": 1, "REJECT": 2}.get(_norm(row.get("recommended_review_status")).upper(), 9)
    same_root_bonus = 0 if _norm(row.get("edge_a_root")) == _norm(row.get("edge_b_root")) else 1
    angle = _safe_float(row.get("edge_pair_angle_diff"), default=math.nan)
    angle_penalty = 999.0 if math.isnan(angle) else abs(180.0 - angle)
    dist_a = _safe_float(row.get("edge_a_distance_to_real_m"), default=math.nan)
    dist_b = _safe_float(row.get("edge_b_distance_to_real_m"), default=math.nan)
    dist_sum = (dist_a if not math.isnan(dist_a) else 1e9) + (dist_b if not math.isnan(dist_b) else 1e9)
    return (
        tls_anchor_bonus,
        review_rank,
        same_root_bonus,
        angle_penalty,
        dist_sum,
        _norm(row.get("selected_node_id_candidate")),
        _norm(row.get("edge_a")),
        _norm(row.get("edge_b")),
    )


def _node_candidates(net: Any, lon: float, lat: float, limit: int = 5) -> list[dict[str, Any]]:
    xy = None
    try:
        xy = net.convertLonLat2XY(float(lon), float(lat))
    except Exception:
        return []

    rows: list[dict[str, Any]] = []
    for node in net.getNodes():
        node_id = _norm(node.getID())
        if not node_id or node_id.startswith(":"):
            continue
        try:
            node_xy = node.getCoord()
        except Exception:
            continue
        dist = math.dist((float(xy[0]), float(xy[1])), (float(node_xy[0]), float(node_xy[1])))
        rows.append(
            {
                "node_id": node_id,
                "distance_m": round(dist, 3),
                "type": _norm(node.getType()) or "priority",
                "lon": float(lon),
                "lat": float(lat),
            }
        )
    rows.sort(key=lambda r: (r["distance_m"], r["node_id"]))
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        if row["node_id"] in seen:
            continue
        if len(_incident_edges(net, row["node_id"])) < 2:
            continue
        seen.add(row["node_id"])
        out.append(row)
        if len(out) >= limit:
            break
    return out


def _candidate_rows_for_crosswalk(net: Any, row: pd.Series) -> list[dict[str, Any]]:
    crosswalk_id = _norm(row.get("crosswalk_id"))
    original_rank = _safe_int(row.get("original_rank"))
    failure_category = _norm(row.get("failure_category"))
    real_lon = _safe_float(row.get("real_lon"))
    real_lat = _safe_float(row.get("real_lat"))
    current_sumo_lon = _safe_float(row.get("current_sumo_lon"))
    current_sumo_lat = _safe_float(row.get("current_sumo_lat"))
    generated_lon = _safe_float(row.get("generated_lon"))
    generated_lat = _safe_float(row.get("generated_lat"))
    source_tls_id = _norm(row.get("nearest_tls_id"))
    source_tls_dist = _safe_float(row.get("nearest_tls_distance_m"))

    calc_tls_id, calc_tls_dist, _ = _nearest_tls(net, real_lon, real_lat)
    tls_warning = ""
    if source_tls_id and calc_tls_id and source_tls_id != calc_tls_id:
        tls_warning = f"source_tls={source_tls_id};calc_tls={calc_tls_id}"

    node_candidates = _node_candidates(net, real_lon, real_lat, limit=5)
    tls_node = None
    if calc_tls_id:
        tls_node = {
            "node_id": calc_tls_id,
            "distance_m": round(calc_tls_dist, 3) if not math.isnan(calc_tls_dist) else calc_tls_dist,
            "type": _node_type(net, calc_tls_id),
            "lon": real_lon,
            "lat": real_lat,
            "is_tls_candidate": True,
        }
        if tls_node["type"] == "traffic_light":
            # put the nearest TLS first
            node_candidates = [tls_node] + [n for n in node_candidates if n["node_id"] != calc_tls_id]

    candidate_nodes: list[dict[str, Any]] = []
    seen_nodes: set[str] = set()
    for node in node_candidates:
        node_id = _norm(node["node_id"])
        if not node_id or node_id in seen_nodes:
            continue
        seen_nodes.add(node_id)
        candidate_nodes.append(node)

    rows: list[dict[str, Any]] = []
    for node_order, node in enumerate(candidate_nodes, start=1):
        node_id = _norm(node["node_id"])
        node_type = _norm(node.get("type")) or _node_type(net, node_id) or "priority"
        incident = _incident_edges(net, node_id)
        if len(incident) < 2:
            continue
        incident_sorted = sorted(
            incident,
            key=lambda e: (
                _edge_distance_m(net, e, real_lon, real_lat),
                _norm(e.getID()),
            ),
        )
        pair_rows: list[dict[str, Any]] = []
        for i in range(len(incident_sorted)):
            for j in range(i + 1, len(incident_sorted)):
                edge_a = incident_sorted[i]
                edge_b = incident_sorted[j]
                edge_a_id = _norm(edge_a.getID())
                edge_b_id = _norm(edge_b.getID())
                edge_a_dist = round(_edge_distance_m(net, edge_a, real_lon, real_lat), 3)
                edge_b_dist = round(_edge_distance_m(net, edge_b, real_lon, real_lat), 3)
                angle_diff = _pair_angle_diff(edge_a, edge_b)
                same_root = _edge_root(edge_a_id) == _edge_root(edge_b_id)
                root_reason = "same-road opposite pair" if same_root else "incident pair on selected node"
                source_reason = "nearest_tls incident pair" if node_id == calc_tls_id and node_type == "traffic_light" else "nearby node incident pair"

                recommended_review_status = "HOLD"
                recommended_manual_strategy = "EXISTING_CROSSING_REMAP"
                warning_parts: list[str] = []
                if failure_category == "LOCATION_TOO_FAR":
                    recommended_review_status = "HOLD"
                    recommended_manual_strategy = "HOLD"
                    warning_parts.append("LOCATION_TOO_FAR")
                if node_type == "dead_end":
                    recommended_review_status = "HOLD"
                    recommended_manual_strategy = "HOLD"
                    warning_parts.append("dead_end")
                if not math.isnan(calc_tls_dist) and calc_tls_dist > 100:
                    recommended_review_status = "HOLD"
                    recommended_manual_strategy = "HOLD"
                    warning_parts.append("nearest_tls_distance_gt_100m")
                if (
                    failure_category != "LOCATION_TOO_FAR"
                    and node_type == "traffic_light"
                    and not math.isnan(calc_tls_dist)
                    and calc_tls_dist <= 50
                ):
                    recommended_review_status = "APPROVE"
                    recommended_manual_strategy = "EXISTING_TLS_RETROFIT"
                if recommended_review_status != "APPROVE" and node_type == "traffic_light":
                    recommended_manual_strategy = "EXISTING_TLS_RETROFIT"
                if node_type != "traffic_light" and recommended_review_status != "REJECT":
                    recommended_manual_strategy = "EXISTING_CROSSING_REMAP"

                if math.isnan(angle_diff):
                    warning_parts.append("missing_edge_angle")
                if edge_a_id == edge_b_id:
                    warning_parts.append("duplicate_edge_id")

                rows.append(
                    {
                        "crosswalk_id": crosswalk_id,
                        "original_rank": original_rank,
                        "failure_category": failure_category,
                        "real_lon": real_lon,
                        "real_lat": real_lat,
                        "selected_node_id_candidate": node_id,
                        "selected_node_type": node_type,
                        "selected_tls_id_candidate": calc_tls_id,
                        "selected_tls_distance_m": round(calc_tls_dist, 3) if not math.isnan(calc_tls_dist) else "",
                        "edge_a": edge_a_id,
                        "edge_b": edge_b_id,
                        "edge_a_distance_to_real_m": edge_a_dist,
                        "edge_b_distance_to_real_m": edge_b_dist,
                        "edge_pair_angle_diff": angle_diff if not math.isnan(angle_diff) else "",
                        "both_edges_incident_to_selected_node": True,
                        "candidate_pair_reason": f"{source_reason}; {root_reason}; node_order={node_order}",
                        "recommended_manual_strategy": recommended_manual_strategy,
                        "recommended_review_status": recommended_review_status,
                        "warning": "; ".join([w for w in [tls_warning] + warning_parts if w]),
                        "selected_crossing_edges": f"{edge_a_id} {edge_b_id}",
                        "tls_anchor_match": node_id == calc_tls_id,
                        "_pair_score": _pair_score(
                            {
                                "recommended_review_status": recommended_review_status,
                                "selected_node_id_candidate": node_id,
                                "tls_anchor_match": node_id == calc_tls_id,
                                "edge_a_root": _edge_root(edge_a_id),
                                "edge_b_root": _edge_root(edge_b_id),
                                "edge_pair_angle_diff": angle_diff,
                                "edge_a_distance_to_real_m": edge_a_dist,
                                "edge_b_distance_to_real_m": edge_b_dist,
                                "edge_a": edge_a_id,
                                "edge_b": edge_b_id,
                            }
                        ),
                        "_node_order": node_order,
                    }
                )
                pair_rows.append(rows[-1])

        pair_rows.sort(key=lambda r: r["_pair_score"])
        for idx, rec in enumerate(pair_rows, start=1):
            rec["candidate_rank"] = idx

        # Keep only the top 8 pairs per selected node candidate.
        for rec in pair_rows[:8]:
            rec["candidate_pair_reason"] = f"{rec['candidate_pair_reason']}; pair_rank={rec['candidate_rank']}"

    return rows


def _normalize_duplicate_warnings(rows: list[dict[str, Any]]) -> None:
    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for rec in rows:
        groups[_pair_key(rec["edge_a"], rec["edge_b"])].append(rec)

    for key, group in groups.items():
        crosswalk_ids = sorted({rec["crosswalk_id"] for rec in group if _norm(rec.get("crosswalk_id"))})
        if len(crosswalk_ids) <= 1:
            continue
        cids = "|".join(crosswalk_ids)
        warning_text = f"duplicate_pair_shared_by={cids}"
        best = sorted(group, key=lambda r: r["_pair_score"])[0]
        for rec in group:
            if rec.get("warning"):
                rec["warning"] = f"{rec['warning']}; {warning_text}"
            else:
                rec["warning"] = warning_text
        for rec in group:
            if rec is best:
                continue
            if _norm(rec.get("recommended_review_status")).upper() == "APPROVE":
                rec["recommended_review_status"] = "HOLD"
                if _norm(rec.get("recommended_manual_strategy")).upper() == "EXISTING_TLS_RETROFIT":
                    rec["recommended_manual_strategy"] = "EXISTING_CROSSING_REMAP"


def _limit_approvals_per_crosswalk(rows: list[dict[str, Any]]) -> None:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for rec in rows:
        groups[_norm(rec.get("crosswalk_id"))].append(rec)
    for crosswalk_id, group in groups.items():
        approved = [rec for rec in group if _norm(rec.get("recommended_review_status")).upper() == "APPROVE"]
        if len(approved) <= 1:
            continue
        best = sorted(approved, key=lambda r: r["_pair_score"])[0]
        for rec in approved:
            if rec is best:
                continue
            rec["recommended_review_status"] = "HOLD"
            if _norm(rec.get("recommended_manual_strategy")).upper() == "EXISTING_TLS_RETROFIT":
                rec["recommended_manual_strategy"] = "EXISTING_CROSSING_REMAP"
            warn = _norm(rec.get("warning"))
            extra = f"additional_approve_demoted_for_crosswalk={crosswalk_id}"
            rec["warning"] = f"{warn}; {extra}" if warn else extra


def _finalize_rows(rows: list[dict[str, Any]]) -> pd.DataFrame:
    _normalize_duplicate_warnings(rows)
    _limit_approvals_per_crosswalk(rows)
    for rec in rows:
        rec.pop("_pair_score", None)
        rec.pop("_node_order", None)
        rec.pop("tls_anchor_match", None)

    df = pd.DataFrame(rows)
    if df.empty:
        return df
    cols = [
        "crosswalk_id",
        "original_rank",
        "failure_category",
        "real_lon",
        "real_lat",
        "selected_node_id_candidate",
        "selected_node_type",
        "selected_tls_id_candidate",
        "selected_tls_distance_m",
        "edge_a",
        "edge_b",
        "edge_a_distance_to_real_m",
        "edge_b_distance_to_real_m",
        "edge_pair_angle_diff",
        "both_edges_incident_to_selected_node",
        "candidate_pair_reason",
        "recommended_manual_strategy",
        "recommended_review_status",
        "warning",
    ]
    df = df.reindex(columns=cols)
    df = df.sort_values(
        ["original_rank", "crosswalk_id", "recommended_review_status", "selected_node_id_candidate", "edge_pair_angle_diff", "edge_a_distance_to_real_m", "edge_b_distance_to_real_m", "edge_a", "edge_b"],
        ascending=[True, True, True, True, False, True, True, True, True],
        kind="mergesort",
    )
    return df


def _html_escape(v: Any) -> str:
    return html.escape("" if v is None else str(v), quote=True)


def _group_rows(df: pd.DataFrame) -> list[dict[str, Any]]:
    groups: list[dict[str, Any]] = []
    if df.empty:
        return groups
    for cid, sub in df.groupby("crosswalk_id", sort=False):
        sub = sub.copy()
        sub["__score"] = sub.apply(
            lambda r: (
                {"APPROVE": 0, "HOLD": 1, "REJECT": 2}.get(_norm(r["recommended_review_status"]).upper(), 9),
                0 if _edge_root(r["edge_a"]) == _edge_root(r["edge_b"]) else 1,
                abs(180.0 - (_safe_float(r["edge_pair_angle_diff"], default=math.nan) if not pd.isna(r["edge_pair_angle_diff"]) else math.nan))
                if not pd.isna(r["edge_pair_angle_diff"])
                else 999.0,
                _safe_float(r["edge_a_distance_to_real_m"], default=1e9) + _safe_float(r["edge_b_distance_to_real_m"], default=1e9),
            ),
            axis=1,
        )
        sub = sub.sort_values("__score", kind="mergesort")
        best = sub.iloc[0].to_dict() if not sub.empty else {}
        meta = {
            "crosswalk_id": cid,
            "original_rank": _safe_int(sub.iloc[0]["original_rank"]) if not sub.empty else "",
            "failure_category": _norm(sub.iloc[0]["failure_category"]) if not sub.empty else "",
            "real_lon": _safe_float(sub.iloc[0]["real_lon"]) if not sub.empty else "",
            "real_lat": _safe_float(sub.iloc[0]["real_lat"]) if not sub.empty else "",
            "selected_tls_id_candidate": _norm(best.get("selected_tls_id_candidate")),
            "selected_tls_distance_m": best.get("selected_tls_distance_m", ""),
            "best_review_status": _norm(best.get("recommended_review_status")),
            "best_manual_strategy": _norm(best.get("recommended_manual_strategy")),
            "best_warning": _norm(best.get("warning")),
            "best_selected_node_id": _norm(best.get("selected_node_id_candidate")),
            "best_selected_crossing_edges": f"{_norm(best.get('edge_a'))} {_norm(best.get('edge_b'))}".strip(),
            "best_selected_from_edge": _norm(best.get("edge_a")),
            "best_selected_to_edge": _norm(best.get("edge_b")),
            "best_candidate_reason": _norm(best.get("candidate_pair_reason")),
            "candidate_rows": [
                {
                    "selected_node_id_candidate": _norm(r["selected_node_id_candidate"]),
                    "selected_node_type": _norm(r["selected_node_type"]),
                    "selected_tls_id_candidate": _norm(r["selected_tls_id_candidate"]),
                    "selected_tls_distance_m": r["selected_tls_distance_m"],
                    "edge_a": _norm(r["edge_a"]),
                    "edge_b": _norm(r["edge_b"]),
                    "edge_a_distance_to_real_m": r["edge_a_distance_to_real_m"],
                    "edge_b_distance_to_real_m": r["edge_b_distance_to_real_m"],
                    "edge_pair_angle_diff": r["edge_pair_angle_diff"],
                    "both_edges_incident_to_selected_node": bool(r["both_edges_incident_to_selected_node"]),
                    "candidate_pair_reason": _norm(r["candidate_pair_reason"]),
                    "recommended_manual_strategy": _norm(r["recommended_manual_strategy"]),
                    "recommended_review_status": _norm(r["recommended_review_status"]),
                    "warning": _norm(r["warning"]),
                    "selected_crossing_edges": f"{_norm(r['edge_a'])} {_norm(r['edge_b'])}".strip(),
                    "selected_from_edge": _norm(r["edge_a"]),
                    "selected_to_edge": _norm(r["edge_b"]),
                }
                for _, r in sub.iterrows()
            ],
        }
        groups.append(meta)
    return groups


def _render_html(out_html: Path, grouped: list[dict[str, Any]]) -> None:
    center = [37.56, 126.98]
    for rec in grouped:
        if rec.get("real_lat") not in {"", None} and rec.get("real_lon") not in {"", None}:
            center = [float(rec["real_lat"]), float(rec["real_lon"])]
            break

    sidebar_items: list[str] = []
    for rec in grouped:
        warn = _norm(rec.get("best_warning"))
        cls = "location_too_far" if "LOCATION_TOO_FAR" in warn.upper() else ""
        if "dead_end" in warn.lower():
            cls = f"{cls} dead_end".strip()
        sidebar_items.append(
            f"""<div class="cand-item {cls}" onclick="focusCandidate('{_html_escape(rec['crosswalk_id'])}')">
  <div class="cand-title">{_html_escape(rec["crosswalk_id"])}</div>
  <div class="cand-sub">rank {_html_escape(rec["original_rank"])} | {_html_escape(rec["failure_category"])}</div>
  <div class="cand-sub">best {_html_escape(rec["best_review_status"])} / {_html_escape(rec["best_manual_strategy"])}</div>
  <div class="cand-sub">{_html_escape(rec["best_selected_crossing_edges"])}</div>
</div>"""
        )

    payload = json.dumps(grouped, ensure_ascii=False)
    doc = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>P4 manual approval review map v2</title>
  <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
  <style>
    html, body {{ height: 100%; margin: 0; }}
    body {{ background: #0f172a; font-family: Inter, ui-sans-serif, system-ui, sans-serif; }}
    #map {{ position: absolute; inset: 0; }}
    .sidebar {{
      position: absolute; top: 12px; left: 12px; z-index: 1000; width: 340px; max-height: calc(100vh - 24px);
      overflow: auto; background: rgba(15, 23, 42, 0.96); color: #e2e8f0; border: 1px solid rgba(148, 163, 184, 0.25);
      border-radius: 14px; box-shadow: 0 18px 48px rgba(0,0,0,0.24);
    }}
    .sidebar h1 {{ margin: 0; padding: 14px 14px 8px; font-size: 16px; }}
    .sidebar p {{ margin: 0; padding: 0 14px 10px; font-size: 12px; color: #cbd5e1; line-height: 1.45; }}
    .legend {{ display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 6px 8px; padding: 0 14px 14px; font-size: 11px; color: #cbd5e1; }}
    .dot {{ display: inline-block; width: 10px; height: 10px; border-radius: 999px; margin-right: 6px; vertical-align: middle; }}
    .real {{ background: #22c55e; }}
    .current {{ background: #94a3b8; }}
    .generated {{ background: #38bdf8; }}
    .tls {{ background: #f43f5e; }}
    .node {{ background: #f59e0b; }}
    .edge {{ background: #a855f7; }}
    .approve {{ color: #86efac; font-weight: 700; }}
    .hold {{ color: #fde68a; font-weight: 700; }}
    .reject {{ color: #fca5a5; font-weight: 700; }}
    .warn {{ color: #fca5a5; font-weight: 700; }}
    .cand-item {{ margin: 8px 10px; padding: 10px; border-radius: 12px; background: rgba(30,41,59,.85); cursor: pointer; border: 1px solid rgba(148,163,184,.18); }}
    .cand-item:hover {{ border-color: rgba(56,189,248,.7); }}
    .cand-item.location_too_far {{ background: rgba(69,10,10,.85); }}
    .cand-item.dead_end {{ background: rgba(92,55,8,.85); }}
    .cand-title {{ font-size: 13px; font-weight: 700; }}
    .cand-sub {{ font-size: 11px; color: #cbd5e1; margin-top: 3px; }}
    table {{ border-collapse: collapse; width: 100%; }}
    th, td {{ border: 1px solid rgba(148,163,184,.25); padding: 3px 5px; font-size: 12px; vertical-align: top; }}
    th {{ text-align: left; background: rgba(30,41,59,.72); color: #cbd5e1; }}
    td {{ color: #f8fafc; }}
    .section {{ margin-top: 8px; font-size: 12px; font-weight: 700; color: #e2e8f0; }}
    .copybox {{ white-space: pre-wrap; background: rgba(15,23,42,.8); border: 1px solid rgba(148,163,184,.18); padding: 8px; border-radius: 8px; font-size: 11px; color: #e2e8f0; }}
    .leaflet-popup-content {{ max-width: 720px; }}
  </style>
</head>
<body>
<div class="sidebar">
  <h1>P4 manual approval review v2</h1>
  <p>Real point first. current SUMO point only reference. generated point only auxiliary. selected_crossing_edges must come from selected_node incident edges only.</p>
  <div class="legend">
    <span><i class="dot real"></i> real point</span>
    <span><i class="dot current"></i> current SUMO</span>
    <span><i class="dot generated"></i> generated point</span>
    <span><i class="dot tls"></i> nearest TLS</span>
    <span><i class="dot node"></i> nearby nodes</span>
    <span><i class="dot edge"></i> candidate edges</span>
  </div>
  {''.join(sidebar_items)}
</div>
<div id="map"></div>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script>
const data = {payload};
const map = L.map('map').setView([{center[0]}, {center[1]}], 14);
L.tileLayer('https://{{s}}.tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png', {{maxZoom: 19, attribution: '&copy; OpenStreetMap'}}).addTo(map);
const layers = {{}};
const bounds = [];
const nodeCoords = window.__nodeCoords || {{}};

function esc(v) {{
  return String(v ?? '').replace(/[&<>"']/g, s => ({{
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
  }}[s]));
}}

function point(lat, lon) {{
  return [Number(lat), Number(lon)];
}}

function pairTable(rows) {{
  const shown = rows.slice(0, 8);
  if (!shown.length) return '<div class="section">No incident pair candidates</div>';
  const body = shown.map((r, idx) => {{
    const warnCls = String(r.warning || '').trim() ? 'warn' : '';
    return `
      <tr>
        <td>${{idx + 1}}</td>
        <td>${{esc(r.selected_node_id_candidate)}}</td>
        <td>${{esc(r.selected_node_type)}}</td>
        <td>${{esc(r.selected_tls_id_candidate)}}</td>
        <td>${{esc(r.selected_tls_distance_m)}}</td>
        <td>${{esc(r.edge_a)}} ${{esc(r.edge_b)}}</td>
        <td>${{esc(r.edge_pair_angle_diff)}}</td>
        <td class="${{esc(r.recommended_review_status).toLowerCase()}}">${{esc(r.recommended_review_status)}}</td>
        <td>${{esc(r.recommended_manual_strategy)}}</td>
        <td class="${{warnCls}}">${{esc(r.warning)}}</td>
      </tr>`;
  }}).join('');
  return `
    <table>
      <thead>
        <tr>
          <th>#</th><th>selected_node_id_candidate</th><th>type</th><th>tls</th><th>tls_dist_m</th>
          <th>edge_a edge_b</th><th>angle_diff</th><th>review</th><th>manual_strategy</th><th>warning</th>
        </tr>
      </thead>
      <tbody>${{body}}</tbody>
    </table>`;
}}

function selectedBox(r) {{
  return `
    <div class="section">Copy values</div>
    <div class="copybox">selected_node_id=${{esc(r.best_selected_node_id)}}
selected_tls_id=${{esc(r.selected_tls_id_candidate)}}
selected_crossing_edges=${{esc(r.best_selected_crossing_edges)}}
selected_from_edge=${{esc(r.best_selected_from_edge)}}
selected_to_edge=${{esc(r.best_selected_to_edge)}}</div>`;
}}

function popupHtml(r) {{
  const rows = [
    ['crosswalk_id', r.crosswalk_id],
    ['original_rank', r.original_rank],
    ['failure_category', r.failure_category],
    ['mapping_distance_m', r.mapping_distance_m],
    ['best_review_status', r.best_review_status],
    ['best_manual_strategy', r.best_manual_strategy],
    ['nearest_tls_id', r.selected_tls_id_candidate],
    ['nearest_tls_distance_m', r.selected_tls_distance_m],
    ['approval_warning', 'APPROVED only after human CSV edit and seed1 smoke'],
  ].map(([k, v]) => `<tr><th>${{esc(k)}}</th><td>${{esc(v)}}</td></tr>`).join('');
  const tlsPairs = (r.candidate_rows || []).filter(x => x.selected_node_id_candidate === r.selected_tls_id_candidate);
  const nearbyPairs = (r.candidate_rows || []).filter(x => x.selected_node_id_candidate !== r.selected_tls_id_candidate);
  return `
    <table>${{rows}}</table>
    ${{selectedBox(r)}}
    <div class="section">TLS incident edge pair candidates</div>
    ${{pairTable(tlsPairs)}}
    <div class="section">nearby node incident edge pair candidates</div>
    ${{pairTable(nearbyPairs)}}
  `;
}}

function focusCandidate(cid) {{
  const rec = data.find(r => r.crosswalk_id === cid);
  if (!rec) return;
  if (rec.real_lat !== '' && rec.real_lon !== '') {{
    map.setView([Number(rec.real_lat), Number(rec.real_lon)], 17);
  }}
  if (layers[cid]) {{
    layers[cid].openPopup();
  }}
}}

function drawEdge(edgeId, color, weight, opacity, dashArray, group) {{
  const edge = edgeShapes[edgeId];
  if (!edge || !edge.length) return;
  const poly = L.polyline(edge.map(pt => [Number(pt[1]), Number(pt[0])]), {{
    color: color,
    weight: weight,
    opacity: opacity,
    dashArray: dashArray || ''
  }}).addTo(group);
  for (const pt of edge) bounds.push([Number(pt[1]), Number(pt[0])]);
  return poly;
}}

const edgeShapes = {{}};
for (const r of data) {{
  for (const cand of (r.candidate_rows || [])) {{
    for (const edgeId of [cand.edge_a, cand.edge_b]) {{
      if (!edgeId || edgeShapes[edgeId]) continue;
      edgeShapes[edgeId] = cand.__shape_cache && cand.__shape_cache[edgeId] ? cand.__shape_cache[edgeId] : null;
    }}
  }}
}}
// shape cache is injected below from Python via edge_shape_cache.
Object.assign(edgeShapes, window.__edgeShapeCache || {{}} );

for (const r of data) {{
  const cid = r.crosswalk_id;
  const group = L.layerGroup().addTo(map);
  layers[cid] = group;
  const cands = (r.candidate_rows || []);
  const bestByNode = {{}};
  for (const cand of cands) {{
    const nid = cand.selected_node_id_candidate;
    if (!bestByNode[nid]) bestByNode[nid] = cand;
  }}

  if (r.real_lat !== '' && r.real_lon !== '') {{
    L.circleMarker(point(r.real_lat, r.real_lon), {{radius: 8, color: '#22c55e', fillColor: '#22c55e', fillOpacity: 0.95}}).addTo(group).bindPopup(popupHtml(r));
    bounds.push(point(r.real_lat, r.real_lon));
  }}
  if (r.current_sumo_lat !== '' && r.current_sumo_lon !== '') {{
    L.circleMarker(point(r.current_sumo_lat, r.current_sumo_lon), {{radius: 7, color: '#94a3b8', fillColor: '#94a3b8', fillOpacity: 0.9}}).addTo(group).bindPopup(popupHtml(r));
    bounds.push(point(r.current_sumo_lat, r.current_sumo_lon));
  }}
  if (r.generated_lat !== '' && r.generated_lon !== '' && !Number.isNaN(Number(r.generated_lat)) && !Number.isNaN(Number(r.generated_lon))) {{
    L.circleMarker(point(r.generated_lat, r.generated_lon), {{radius: 7, color: '#38bdf8', fillColor: '#38bdf8', fillOpacity: 0.9}}).addTo(group).bindPopup(popupHtml(r));
    bounds.push(point(r.generated_lat, r.generated_lon));
  }}
  for (const cand of cands) {{
    const nid = cand.selected_node_id_candidate;
    const color = cand.recommended_review_status === 'APPROVE' ? '#22c55e' : (cand.recommended_review_status === 'REJECT' ? '#ef4444' : '#f59e0b');
    if (cand.selected_node_type === 'traffic_light') {{
      const n = nodeCoords[nid];
      if (n) {{
        L.circleMarker([n[1], n[0]], {{radius: 7, color: '#f43f5e', fillColor: '#f43f5e', fillOpacity: 0.95}}).addTo(group).bindPopup(popupHtml(r));
        bounds.push([n[1], n[0]]);
      }}
    }} else if (nodeCoords[nid]) {{
      const n = nodeCoords[nid];
      L.circleMarker([n[1], n[0]], {{radius: 5, color: '#f59e0b', fillColor: '#f59e0b', fillOpacity: 0.8}}).addTo(group).bindPopup(popupHtml(r));
      bounds.push([n[1], n[0]]);
    }}
  }}

  for (const cand of cands) {{
    if (bestByNode[cand.selected_node_id_candidate] !== cand) continue;
    const polyA = drawEdge(cand.edge_a, cand.recommended_review_status === 'APPROVE' ? '#22c55e' : '#a855f7', 5, 0.8, '', group);
    const polyB = drawEdge(cand.edge_b, cand.recommended_review_status === 'APPROVE' ? '#22c55e' : '#a855f7', 5, 0.8, '4 6', group);
    if (polyA) polyA.bindPopup(popupHtml(r));
    if (polyB) polyB.bindPopup(popupHtml(r));
  }}
}}

if (bounds.length) {{
  map.fitBounds(bounds, {{padding: [24, 24]}});
}}
</script>
</body>
</html>
"""
    out_html.write_text(doc, encoding="utf-8")


def _edge_shape_cache(net: Any, rows: pd.DataFrame) -> tuple[dict[str, list[list[float]]], dict[str, tuple[float, float]]]:
    edge_cache: dict[str, list[list[float]]] = {}
    node_cache: dict[str, tuple[float, float]] = {}
    for _, row in rows.iterrows():
        for edge_id in [_norm(row.get("edge_a")), _norm(row.get("edge_b"))]:
            if edge_id and edge_id not in edge_cache:
                try:
                    edge = net.getEdge(edge_id)
                    shape = []
                    for x, y in edge.getShape():
                        lon, lat = net.convertXY2LonLat(float(x), float(y))
                        shape.append([float(lon), float(lat)])
                    edge_cache[edge_id] = shape
                except Exception:
                    edge_cache[edge_id] = []
        for node_id in [_norm(row.get("selected_node_id_candidate")), _norm(row.get("selected_tls_id_candidate"))]:
            if node_id and node_id not in node_cache:
                xy = _node_xy(net, node_id)
                if xy is not None:
                    lon, lat = net.convertXY2LonLat(*xy)
                    node_cache[node_id] = (float(lon), float(lat))
    return edge_cache, node_cache


def _write_guide(out_path: Path) -> None:
    text = """# P4 realistic connection decision guide

- Judge by the real point first.
- current SUMO mapped point can be wrong.
- generated point is only a secondary hint.
- `selected_node_id_candidate` must be a real node incident to the pair.
- `selected_crossing_edges` must be written as `edge_a edge_b`.
- Use the TLS incident pair shortlist first when `selected_node_type` is `traffic_light`.
- Use nearby node incident pair shortlist only when the TLS node is not obviously the right anchor.
- `nearest_tls_distance_m <= 50` and `selected_node_type == traffic_light` is the strongest APPROVE case.
- `nearest_tls_distance_m > 100` defaults to HOLD.
- `selected_node_type == dead_end` must not be APPROVED.
- `failure_category == LOCATION_TOO_FAR` must not be auto-APPROVED.
- duplicate edge pair warnings mean the same pair appears for more than one crosswalk; do not treat that as independent evidence.
- `EXISTING_TLS_RETROFIT` means reuse the existing TLS and patch its logic.
- `EXISTING_CROSSING_REMAP` means keep the crossing idea but move it to a more realistic incident pair.
- `NEW_SIGNAL_INSTALLATION` should be rare and only after explicit human confirmation.
- `confidence` should reflect human certainty, not the model score: `HIGH`, `MEDIUM`, `LOW`.
- `APPROVE` in the HTML means "good candidate for manual selection", not final readiness.
- seed1 smoke must still pass before anything can enter 30-seed.

Recommended reading order:
1. Open the v2 HTML.
2. Check TLS incident pair shortlist.
3. Check nearby node incident pair shortlist.
4. Prefer same-road opposite-direction pairs when the geometry supports it.
5. Reject any pair that depends on a dead-end anchor or an over-far TLS.
"""
    out_path.write_text(text + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="Prepare P4 manual approval v2 shortlist without running SUMO.")
    parser.add_argument("--plan-dir", default="", help="Latest plan dir. Default: newest phase_next_p1_p4_recovery_experiment_plan_*")
    parser.add_argument("--base-net", default=str(DEFAULT_BASE_NET))
    parser.add_argument("--p4-candidates", default=str(DEFAULT_P4_CANDIDATES))
    args = parser.parse_args()

    plan_dir = Path(args.plan_dir).expanduser().resolve() if args.plan_dir else _latest_plan_dir()
    dirs = _ensure_dirs(plan_dir)
    base_net = Path(args.base_net).expanduser().resolve()
    p4_csv = Path(args.p4_candidates).expanduser().resolve()
    if not base_net.exists():
        raise FileNotFoundError(base_net)
    if not p4_csv.exists():
        raise FileNotFoundError(p4_csv)

    net = read_net(base_net)
    p4_df = _csv(p4_csv)
    if p4_df.empty:
        raise RuntimeError("P4 candidate input empty")

    all_rows: list[dict[str, Any]] = []
    for _, row in p4_df.sort_values(["original_rank", "crosswalk_id"]).iterrows():
        all_rows.extend(_candidate_rows_for_crosswalk(net, row))

    df = _finalize_rows(all_rows)
    csv_path = dirs["csv"] / "p4_tls_incident_edge_pair_candidates.csv"
    df.to_csv(csv_path, index=False)

    grouped = _group_rows(df)
    edge_cache, node_cache = _edge_shape_cache(net, df)
    # Inject caches into HTML by serializing them as globals. The map is still static HTML.
    html_path = dirs["html"] / "p4_manual_approval_review_map_v2.html"
    _render_html(html_path, grouped)

    # Patch in edge/node caches after HTML generation so the file stays self-contained.
    # We keep this as a simple string replacement to avoid another large template pass.
    html_text = html_path.read_text(encoding="utf-8")
    html_text = html_text.replace(
        "const edgeShapes = {};",
        f"window.__edgeShapeCache = {json.dumps(edge_cache, ensure_ascii=False)};\nconst edgeShapes = {{}};",
    )
    html_text = html_text.replace(
        "const nodeCoords = window.__nodeCoords || {};",
        f"window.__nodeCoords = {json.dumps(node_cache, ensure_ascii=False)};\nconst nodeCoords = window.__nodeCoords || {{}};",
    )
    html_path.write_text(html_text, encoding="utf-8")

    guide_path = dirs["readme"] / "p4_realistic_connection_decision_guide.md"
    _write_guide(guide_path)

    print(f"plan_dir: {plan_dir}")
    print(f"p4_rows: {len(p4_df)}")
    print(f"pair_rows: {len(df)}")
    print(f"csv: {csv_path}")
    print(f"html: {html_path}")
    print(f"guide: {guide_path}")


if __name__ == "__main__":
    main()
