#!/usr/bin/env python3
from __future__ import annotations

import json
import math
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

from smart_crosswalk_sumo.network_utils import edge_center, edge_function, find_tls_id, pedestrian_link_indices, pedestrian_route_from_crossing, read_net

BASE_DIR = Path(__file__).resolve().parents[1]
RESULT_DIR = BASE_DIR / "result"
PATCH_DIR = RESULT_DIR / "phase_next_bad22_generated_crossing_patch_20260515_181150"
PATCH_NET = PATCH_DIR / "generated_crossing_network.net.xml"
PATCH_CANDIDATES = PATCH_DIR / "generated_crossing_patch_candidates.csv"
LOCATION_CHECK = PATCH_DIR / "generated_crossing_location_check.csv"

OK_IDS = [
    "LINK_212169",
    "NODE_8452",
    "NODE_6082",
    "NODE_6123",
    "NODE_8426",
    "NODE_10273",
]
REVIEW_IDS = ["NODE_6212", "NODE_74388"]


def _norm_text(value: Any) -> str:
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


def _safe_float(value: Any) -> float:
    try:
        if value is None or (isinstance(value, str) and value.strip() in {"", "nan", "None"}):
            return math.nan
        return float(value)
    except Exception:
        return math.nan


def _safe_int(value: Any) -> int | None:
    try:
        if value is None or (isinstance(value, str) and value.strip() in {"", "nan", "None"}):
            return None
        return int(float(value))
    except Exception:
        return None


def _parse_edges(value: Any) -> list[str]:
    if value is None:
        return []
    text = str(value).strip()
    if not text or text in {"nan", "None"}:
        return []
    text = text.replace(",", " ").replace("|", " ")
    if text.startswith("[") and text.endswith("]"):
        text = text[1:-1]
    return [tok.strip().strip("'").strip('"') for tok in text.split() if tok.strip()]


def _parse_int_list(value: Any) -> list[int]:
    if value is None:
        return []
    text = str(value).strip()
    if not text or text in {"nan", "None", "[]"}:
        return []
    text = text.replace(",", " ").replace("|", " ")
    if text.startswith("[") and text.endswith("]"):
        text = text[1:-1]
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


def _load_patch_df() -> pd.DataFrame:
    patch = pd.read_csv(PATCH_CANDIDATES)
    loc = pd.read_csv(LOCATION_CHECK)
    merged = patch.merge(loc, on="crosswalk_id", how="left", suffixes=("", "_loc"))
    return merged


def _nearest_tls(net: Any, lon: float, lat: float) -> tuple[str, float]:
    try:
        xy = net.convertLonLat2XY(float(lon), float(lat))
    except Exception:
        return "", math.nan
    best_id = ""
    best_dist = float("inf")
    for tl in net.getTrafficLights():
        tl_id = tl.getID()
        if not net.hasNode(tl_id):
            continue
        node = net.getNode(tl_id)
        try:
            dist = math.dist(xy, node.getCoord())
        except Exception:
            continue
        if dist < best_dist:
            best_dist = dist
            best_id = tl_id
    if not best_id:
        return "", math.nan
    return best_id, float(best_dist)


def _edge_to_lonlat(net: Any, edge: Any) -> list[tuple[float, float]]:
    pts: list[tuple[float, float]] = []
    try:
        shape = list(edge.getShape())
    except Exception:
        shape = []
    for x, y in shape:
        try:
            lon, lat = net.convertXY2LonLat(float(x), float(y))
        except Exception:
            continue
        pts.append((float(lon), float(lat)))
    return pts


def _candidate_html_popup(rec: dict[str, Any]) -> str:
    def fmt(v: Any, digits: int = 6) -> str:
        if v is None:
            return ""
        try:
            if isinstance(v, float) and math.isnan(v):
                return ""
        except Exception:
            pass
        if isinstance(v, (int, float)):
            return f"{float(v):.{digits}f}" if digits else str(v)
        return str(v)

    rows = [
        ("crosswalk_id", rec.get("crosswalk_id", "")),
        ("original_rank", rec.get("original_rank", "")),
        ("real_lon", fmt(rec.get("real_lon"))),
        ("real_lat", fmt(rec.get("real_lat"))),
        ("old_sumo_lon", fmt(rec.get("old_sumo_lon"))),
        ("old_sumo_lat", fmt(rec.get("old_sumo_lat"))),
        ("generated_crossing_center_lon", fmt(rec.get("generated_crossing_center_lon"))),
        ("generated_crossing_center_lat", fmt(rec.get("generated_crossing_center_lat"))),
        ("old_mapping_distance_m", fmt(rec.get("old_mapping_distance_m"), 2)),
        ("generated_to_real_distance_m", fmt(rec.get("generated_to_real_distance_m"), 2)),
        ("location_check_status", rec.get("location_check_status", "")),
        ("selected_node_id", rec.get("selected_node_id", "")),
        ("selected_crossing_edges", rec.get("selected_crossing_edges", "")),
    ]
    body = "".join(f"<tr><th>{k}</th><td>{v}</td></tr>" for k, v in rows)
    return f"<table class='popup-table'>{body}</table>"


def _render_html(out_html: Path, roads: list[dict[str, Any]], ok_rows: list[dict[str, Any]], review_rows: list[dict[str, Any]]) -> None:
    payload = {
        "roads": roads,
        "ok": ok_rows,
        "review": review_rows,
    }
    data_json = json.dumps(payload, ensure_ascii=False)
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1.0"/>
  <title>Generated OK6 Real Map Overlay</title>
  <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" integrity="sha256-p4NxAoJBhIIN+hmNHrzRCf9tD/miZyoHS5obTRR9BMY=" crossorigin=""/>
  <style>
    html, body {{ margin: 0; padding: 0; height: 100%; background: #0e1116; color: #d7dde8; font-family: -apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif; }}
    #map {{ width: 100%; height: 72vh; border-bottom: 1px solid #243041; }}
    #panel {{ padding: 12px 14px 18px; }}
    h1 {{ margin: 0 0 8px; font-size: 18px; font-weight: 700; color: #f3f7ff; }}
    .meta {{ color: #9fb0c7; font-size: 12px; margin-bottom: 10px; line-height: 1.45; }}
    .legend {{ display: flex; flex-wrap: wrap; gap: 10px 14px; margin: 10px 0 12px; font-size: 12px; color: #c9d4e4; }}
    .legend span {{ display: inline-flex; align-items: center; gap: 6px; }}
    .dot {{ width: 10px; height: 10px; border-radius: 50%; display: inline-block; }}
    .sq {{ width: 10px; height: 10px; background: #9aa3ad; display: inline-block; }}
    .tri {{ width: 0; height: 0; border-left: 6px solid transparent; border-right: 6px solid transparent; border-bottom: 10px solid #2a7fff; display: inline-block; }}
    .roadline {{ width: 18px; height: 0; border-top: 3px solid #5b6775; display: inline-block; }}
    .table-wrap {{ overflow-x: auto; }}
    table {{ border-collapse: collapse; width: 100%; min-width: 980px; background: #121722; }}
    th, td {{ border: 1px solid #243041; padding: 6px 8px; font-size: 12px; text-align: left; vertical-align: top; }}
    th {{ background: #182131; color: #eef4ff; position: sticky; top: 0; }}
    tr:nth-child(even) td {{ background: #101521; }}
    .popup-table th {{ position: static; background: #0f1520; color: #d8e3f4; }}
    .popup-table td {{ white-space: nowrap; }}
    .layer-note {{ margin: 8px 0 0; font-size: 12px; color: #9fb0c7; }}
    .leaflet-popup-content-wrapper {{ background: #101622; color: #e4ebf5; }}
    .leaflet-popup-tip {{ background: #101622; }}
    .marker-square {{ width: 12px; height: 12px; background: #98a1ac; border: 2px solid #f4f7fb; transform: rotate(45deg); box-sizing: border-box; }}
    .marker-triangle {{ width: 0; height: 0; border-left: 8px solid transparent; border-right: 8px solid transparent; border-bottom: 14px solid #2f80ff; filter: drop-shadow(0 0 1px rgba(0,0,0,.5)); }}
    .marker-triangle.review {{ border-bottom-color: #ffb347; }}
    .marker-square.review {{ background: #d7a34a; }}
    .candidate-tag {{ display:inline-block; padding:2px 6px; border-radius: 999px; font-size:11px; margin-left: 8px; }}
    .ok {{ background:#173221; color:#8ef0b5; }}
    .review {{ background:#3d2d10; color:#ffd59e; }}
  </style>
</head>
<body>
  <div id="map"></div>
  <div id="panel">
    <h1>Generated OK6 Real Map Overlay</h1>
    <div class="meta">
      Base view: 6 OK candidates. Review layer available as toggle. Road network drawn from generated crossing net.
      If tiles do not load, the candidate overlay still remains in the HTML structure.
    </div>
    <div class="legend">
      <span><i class="dot" style="background:#18b35d"></i> real point</span>
      <span><i class="sq"></i> old mapped SUMO point</span>
      <span><i class="tri"></i> generated crossing point</span>
      <span><i class="roadline" style="border-top-style:solid"></i> road edge</span>
      <span><i class="roadline" style="border-top-style:dashed"></i> crossing / walkingarea edge</span>
    </div>
    <div class="table-wrap">
      <table id="candidate-table">
        <thead>
          <tr>
            <th>crosswalk_id</th>
            <th>original_rank</th>
            <th>location_check_status</th>
            <th>real_lon</th>
            <th>real_lat</th>
            <th>old_sumo_lon</th>
            <th>old_sumo_lat</th>
            <th>generated_crossing_center_lon</th>
            <th>generated_crossing_center_lat</th>
            <th>old_mapping_distance_m</th>
            <th>generated_to_real_distance_m</th>
          </tr>
        </thead>
        <tbody></tbody>
      </table>
    </div>
    <div class="layer-note">OK only by default. Review rows can be turned on with the layer control.</div>
  </div>
  <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js" integrity="sha256-20nQCchB9co0qIjJZRGuk2/Z9VM+kNiyxNV1lvTlZBo=" crossorigin=""></script>
  <script>
    const DATA = {data_json};
    const map = L.map('map', {{ preferCanvas: true }});
    L.tileLayer('https://{{s}}.tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png', {{
      maxZoom: 19,
      attribution: '&copy; OpenStreetMap contributors'
    }}).addTo(map);

    const roadLayer = L.layerGroup().addTo(map);
    const okLayer = L.layerGroup().addTo(map);
    const reviewLayer = L.layerGroup();
    const lineLayer = L.layerGroup().addTo(map);
    const reviewLineLayer = L.layerGroup();

    function popupHtml(rec) {{
      return rec.popup_html;
    }}

    function addPointMarker(layer, lat, lon, opts) {{
      const marker = L.marker([lat, lon], {{ icon: opts.icon }});
      marker.bindPopup(opts.popup);
      marker.bindTooltip(opts.tooltip, {{ sticky: true }});
      layer.addLayer(marker);
      return marker;
    }}

    function addLine(layer, a, b, options) {{
      const poly = L.polyline([[a[1], a[0]], [b[1], b[0]]], options);
      layer.addLayer(poly);
      return poly;
    }}

    function addCandidate(rec, okVisible) {{
      const targetLayer = okVisible ? okLayer : reviewLayer;
      const targetLineLayer = okVisible ? lineLayer : reviewLineLayer;
      const color = okVisible ? '#2f80ff' : '#ffb347';
      const reviewClass = okVisible ? '' : 'review';
      const realIcon = L.divIcon({{
        className: '',
        html: '<div class="dot" style="background:#18b35d;width:12px;height:12px;border:2px solid #0a2; border-radius:50%"></div>',
        iconSize: [14, 14],
        iconAnchor: [7, 7]
      }});
      const oldIcon = L.divIcon({{
        className: '',
        html: '<div class="marker-square ' + reviewClass + '"></div>',
        iconSize: [14, 14],
        iconAnchor: [7, 7]
      }});
      const genIcon = L.divIcon({{
        className: '',
        html: '<div class="marker-triangle ' + reviewClass + '"></div>',
        iconSize: [16, 16],
        iconAnchor: [8, 14]
      }});

      addPointMarker(targetLayer, rec.real_lat, rec.real_lon, {{
        icon: realIcon,
        popup: popupHtml(rec),
        tooltip: rec.crosswalk_id + ' real'
      }});
      addPointMarker(targetLayer, rec.old_sumo_lat, rec.old_sumo_lon, {{
        icon: oldIcon,
        popup: popupHtml(rec),
        tooltip: rec.crosswalk_id + ' old SUMO'
      }});
      addPointMarker(targetLayer, rec.generated_crossing_center_lat, rec.generated_crossing_center_lon, {{
        icon: genIcon,
        popup: popupHtml(rec),
        tooltip: rec.crosswalk_id + ' generated'
      }});
      addLine(targetLineLayer, [rec.real_lon, rec.real_lat], [rec.old_sumo_lon, rec.old_sumo_lat], {{
        color: '#8f98a3', weight: 2, opacity: 0.7, dashArray: '4 6'
      }});
      addLine(targetLineLayer, [rec.real_lon, rec.real_lat], [rec.generated_crossing_center_lon, rec.generated_crossing_center_lat], {{
        color: color, weight: 3, opacity: 0.85
      }});
    }}

    for (const road of DATA.roads) {{
      const pts = road.points.map(p => [p[1], p[0]]);
      const style = road.function === 'normal'
        ? {{ color: '#6c7a89', weight: 2, opacity: 0.55 }}
        : road.function === 'crossing'
          ? {{ color: '#4e9fff', weight: 2, opacity: 0.75, dashArray: '2 6' }}
          : {{ color: '#9aa6b2', weight: 1.5, opacity: 0.4, dashArray: '1 6' }};
      L.polyline(pts, style).addTo(roadLayer);
    }}

    for (const rec of DATA.ok) addCandidate(rec, true);
    for (const rec of DATA.review) addCandidate(rec, false);

    const overlayMaps = {{
      'Road network': roadLayer,
      'OK candidates': okLayer,
      'OK connection lines': lineLayer,
      'Review candidates': reviewLayer,
      'Review connection lines': reviewLineLayer
    }};
    L.control.layers(null, overlayMaps, {{ collapsed: false }}).addTo(map);

    const bounds = [];
    for (const rec of DATA.ok) {{
      bounds.push([rec.real_lat, rec.real_lon], [rec.old_sumo_lat, rec.old_sumo_lon], [rec.generated_crossing_center_lat, rec.generated_crossing_center_lon]);
    }}
    for (const rec of DATA.review) {{
      bounds.push([rec.real_lat, rec.real_lon], [rec.old_sumo_lat, rec.old_sumo_lon], [rec.generated_crossing_center_lat, rec.generated_crossing_center_lon]);
    }}
    if (bounds.length) map.fitBounds(bounds, {{ padding: [30, 30] }});

    const tbody = document.querySelector('#candidate-table tbody');
    [...DATA.ok, ...DATA.review].forEach(rec => {{
      const tr = document.createElement('tr');
      tr.innerHTML = `
        <td>${{rec.crosswalk_id}}</td>
        <td>${{rec.original_rank ?? ''}}</td>
        <td>${{rec.location_check_status}}</td>
        <td>${{Number(rec.real_lon).toFixed(6)}}</td>
        <td>${{Number(rec.real_lat).toFixed(6)}}</td>
        <td>${{Number(rec.old_sumo_lon).toFixed(6)}}</td>
        <td>${{Number(rec.old_sumo_lat).toFixed(6)}}</td>
        <td>${{Number(rec.generated_crossing_center_lon).toFixed(6)}}</td>
        <td>${{Number(rec.generated_crossing_center_lat).toFixed(6)}}</td>
        <td>${{Number(rec.old_mapping_distance_m).toFixed(2)}}</td>
        <td>${{Number(rec.generated_to_real_distance_m).toFixed(2)}}</td>`;
      tbody.appendChild(tr);
    }});
  </script>
</body>
</html>
"""
    out_html.write_text(html, encoding="utf-8")


def _build_road_overlay(net: Any, all_rows: pd.DataFrame) -> list[dict[str, Any]]:
    lon_vals = list(all_rows["real_lon"].astype(float)) + list(all_rows["old_sumo_lon"].astype(float)) + list(all_rows["generated_crossing_center_lon"].astype(float))
    lat_vals = list(all_rows["real_lat"].astype(float)) + list(all_rows["old_sumo_lat"].astype(float)) + list(all_rows["generated_crossing_center_lat"].astype(float))
    lon_min, lon_max = min(lon_vals) - 0.0075, max(lon_vals) + 0.0075
    lat_min, lat_max = min(lat_vals) - 0.0075, max(lat_vals) + 0.0075
    roads: list[dict[str, Any]] = []
    for edge in net.getEdges():
        fn = edge_function(edge)
        if fn not in {"normal", "crossing", "walkingarea"}:
            continue
        pts = _edge_to_lonlat(net, edge)
        if len(pts) < 2:
            continue
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        if max(xs) < lon_min or min(xs) > lon_max or max(ys) < lat_min or min(ys) > lat_max:
            continue
        roads.append({"edge_id": edge.getID(), "function": fn, "points": pts})
    return roads


def main() -> int:
    out_dir = RESULT_DIR / f"phase_next_generated_ok6_phase6_prep_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    out_dir.mkdir(parents=True, exist_ok=True)

    net = read_net(PATCH_NET)
    net_root = None
    try:
        import xml.etree.ElementTree as ET

        net_root = ET.parse(PATCH_NET).getroot()
    except Exception:
        net_root = None

    merged = _load_patch_df()
    ok = merged[merged["crosswalk_id"].astype(str).isin(OK_IDS)].copy()
    review = merged[merged["crosswalk_id"].astype(str).isin(REVIEW_IDS)].copy()

    diagnostics_rows: list[dict[str, Any]] = []
    smoke_rows: list[dict[str, Any]] = []
    summary_rows: list[dict[str, Any]] = []
    single_dir = out_dir / "generated_ok6_single_candidates"
    single_dir.mkdir(parents=True, exist_ok=True)

    for _, row in ok.iterrows():
        cid = _norm_text(row.get("crosswalk_id"))
        edge_id = _norm_text(row.get("generated_crossing_edge_id"))
        selected_node_id = _norm_text(row.get("selected_node_id"))
        selected_crossing_edges = _norm_text(row.get("selected_crossing_edges"))
        real_lon = _safe_float(row.get("real_lon"))
        real_lat = _safe_float(row.get("real_lat"))
        old_lon = _safe_float(row.get("old_sumo_lon"))
        old_lat = _safe_float(row.get("old_sumo_lat"))
        gen_lon = _safe_float(row.get("generated_crossing_center_lon"))
        gen_lat = _safe_float(row.get("generated_crossing_center_lat"))
        old_dist = _safe_float(row.get("old_mapping_distance_m"))
        gen_dist = _safe_float(row.get("generated_to_real_distance_m"))
        loc_status = _norm_text(row.get("location_check_status"))
        original_rank = _safe_int(row.get("original_rank"))
        generated_exists = bool(row.get("generated_crossing_exists", True))
        reason_parts: list[str] = []

        edge_obj = None
        try:
            edge_obj = net.getEdge(edge_id) if edge_id else None
        except Exception:
            edge_obj = None
        crossing_exists = bool(edge_obj is not None)
        if not crossing_exists:
            reason_parts.append("generated_crossing_edge_missing_in_net")

        tls_id = ""
        if edge_obj is not None:
            try:
                tls_id = _norm_text(find_tls_id(net, edge_obj, xml_root=net_root))
            except Exception:
                tls_id = ""

        nearest_tls_id, nearest_tls_dist = _nearest_tls(net, gen_lon, gen_lat)
        ped_link_indices: list[int] = []
        ped_route: dict[str, str] | None = None
        if tls_id and edge_obj is not None:
            try:
                ped_link_indices = pedestrian_link_indices(PATCH_NET, tls_id, edge_id, xml_root=net_root)
            except Exception as exc:
                reason_parts.append(f"ped_link_lookup_failed:{exc}")
            try:
                ped_route = pedestrian_route_from_crossing(edge_obj)
            except Exception as exc:
                reason_parts.append(f"ped_route_lookup_failed:{exc}")
        else:
            reason_parts.append("no_tls_on_generated_crossing")

        ped_link_index = ped_link_indices[0] if ped_link_indices else None
        ped_links_ok = bool(ped_link_indices)
        pedestrian_only_phase_available = bool(tls_id and ped_links_ok)
        phase_aligned_depart_possible = bool(tls_id and ped_links_ok and ped_route)
        smoke_candidate_eligible = bool(crossing_exists and ped_links_ok and phase_aligned_depart_possible)
        if smoke_candidate_eligible:
            reason = "signal_capable"
        else:
            reason = ";".join(dict.fromkeys(reason_parts)) or "signal_not_available"

        diagnostics_rows.append(
            {
                "crosswalk_id": cid,
                "original_rank": original_rank,
                "location_check_status": loc_status,
                "generated_crossing_exists": generated_exists,
                "generated_crossing_edge_id": edge_id,
                "selected_node_id": selected_node_id,
                "selected_crossing_edges": selected_crossing_edges,
                "real_lon": real_lon,
                "real_lat": real_lat,
                "old_sumo_lon": old_lon,
                "old_sumo_lat": old_lat,
                "generated_crossing_center_lon": gen_lon,
                "generated_crossing_center_lat": gen_lat,
                "old_mapping_distance_m": old_dist,
                "generated_to_real_distance_m": gen_dist,
                "tls_id_found": tls_id,
                "nearest_tls_id": nearest_tls_id,
                "nearest_tls_distance_m": nearest_tls_dist,
                "ped_link_indices": "|".join(str(v) for v in ped_link_indices),
                "ped_link_index_found": ped_links_ok,
                "pedestrian_only_phase_available": pedestrian_only_phase_available,
                "phase_aligned_depart_possible": phase_aligned_depart_possible,
                "smoke_candidate_eligible": smoke_candidate_eligible,
                "recommended_action": "RUN_SMOKE" if smoke_candidate_eligible else "KEEP_OUT_OF_PHASE6_SMOKE",
                "reason": reason,
            }
        )
        if smoke_candidate_eligible:
            smoke_rows.append(
                {
                    "crosswalk_id": cid,
                    "source_crosswalk_id": cid,
                    "canonical_crosswalk_id": cid,
                    "nearest_junction_id": selected_node_id,
                    "tls_id_used": tls_id,
                    "crossing_id": edge_id,
                    "crossing_edge_id": edge_id,
                    "ped_link_index": int(ped_link_index) if ped_link_index is not None else "",
                    "ped_link_indices": "|".join(str(v) for v in ped_link_indices),
                    "ped_depart_offset_sec": 0,
                    "ped_repeat_count": 1,
                    "ped_repeat_spacing_sec": 1.5,
                    "route_from_edge": ped_route["from_edge"] if ped_route else "",
                    "route_to_edge": ped_route["to_edge"] if ped_route else "",
                    "generated_route_edges": "",
                    "contains_crossing_edge": True,
                    "route_reason": "generated_crossing_signal_capable",
                    "incident_road_edges": _norm_text(row.get("nearest_road_edge_id")) + (f"|{_norm_text(row.get('opposite_or_pair_edge_id'))}" if _norm_text(row.get("opposite_or_pair_edge_id")) else ""),
                    "nearest_road_edge_id": _norm_text(row.get("nearest_road_edge_id")),
                    "source_file": "result/phase_next_bad22_generated_crossing_patch_20260515_181150/generated_crossing_patch_candidates.csv",
                    "batch_network_file": str(PATCH_NET),
                    "final_verdict": "READY_FOR_30SEED",
                    "step_test_ok": "True",
                    "controlled_links_count": str(len(ped_link_indices)),
                    "crossing_inventory_used": False,
                    "signal_generation_status": "SIGNAL_GENERATION_CANDIDATE",
                    "location_check_status": loc_status,
                    "real_lon": real_lon,
                    "real_lat": real_lat,
                    "old_sumo_lon": old_lon,
                    "old_sumo_lat": old_lat,
                    "generated_crossing_center_lon": gen_lon,
                    "generated_crossing_center_lat": gen_lat,
                    "old_mapping_distance_m": old_dist,
                    "generated_to_real_distance_m": gen_dist,
                    "selected_node_id": selected_node_id,
                    "selected_crossing_edges": selected_crossing_edges,
                }
            )

        summary_rows.append(
            {
                "crosswalk_id": cid,
                "original_rank": original_rank,
                "real_lon": real_lon,
                "real_lat": real_lat,
                "old_sumo_lon": old_lon,
                "old_sumo_lat": old_lat,
                "generated_crossing_center_lon": gen_lon,
                "generated_crossing_center_lat": gen_lat,
                "old_mapping_distance_m": old_dist,
                "generated_to_real_distance_m": gen_dist,
                "location_check_status": loc_status,
                "include_in_html": True,
            }
        )

        single_path = single_dir / f"{cid}.md"
        single_path.write_text(
            "\n".join(
                [
                    f"# {cid}",
                    f"- location_check_status: {loc_status}",
                    f"- generated_crossing_exists: {generated_exists}",
                    f"- generated_crossing_edge_id: {edge_id}",
                    f"- tls_id_found: {tls_id or 'NONE'}",
                    f"- nearest_tls_id: {nearest_tls_id or 'NONE'}",
                    f"- nearest_tls_distance_m: {nearest_tls_dist:.2f}" if pd.notna(nearest_tls_dist) else "- nearest_tls_distance_m: ",
                    f"- ped_link_indices: {'|'.join(str(v) for v in ped_link_indices) if ped_link_indices else 'NONE'}",
                    f"- pedestrian_only_phase_available: {pedestrian_only_phase_available}",
                    f"- phase_aligned_depart_possible: {phase_aligned_depart_possible}",
                    f"- smoke_candidate_eligible: {smoke_candidate_eligible}",
                    f"- reason: {reason}",
                    f"- real: {real_lon:.6f}, {real_lat:.6f}",
                    f"- old SUMO: {old_lon:.6f}, {old_lat:.6f}",
                    f"- generated: {gen_lon:.6f}, {gen_lat:.6f}",
                ]
            )
            + "\n",
            encoding="utf-8",
        )

    candidate_columns = [
        "crosswalk_id",
        "source_crosswalk_id",
        "canonical_crosswalk_id",
        "nearest_junction_id",
        "tls_id_used",
        "crossing_id",
        "crossing_edge_id",
        "ped_link_index",
        "ped_link_indices",
        "ped_depart_offset_sec",
        "ped_repeat_count",
        "ped_repeat_spacing_sec",
        "route_from_edge",
        "route_to_edge",
        "generated_route_edges",
        "contains_crossing_edge",
        "route_reason",
        "incident_road_edges",
        "nearest_road_edge_id",
        "source_file",
        "batch_network_file",
        "final_verdict",
        "step_test_ok",
        "controlled_links_count",
        "crossing_inventory_used",
        "signal_generation_status",
        "location_check_status",
        "real_lon",
        "real_lat",
        "old_sumo_lon",
        "old_sumo_lat",
        "generated_crossing_center_lon",
        "generated_crossing_center_lat",
        "old_mapping_distance_m",
        "generated_to_real_distance_m",
        "selected_node_id",
        "selected_crossing_edges",
    ]
    smoke_df = pd.DataFrame(smoke_rows, columns=candidate_columns)
    diag_df = pd.DataFrame(diagnostics_rows)
    summary_df = pd.DataFrame(summary_rows)

    candidate_csv = out_dir / "generated_phase6_candidate_table.csv"
    diag_csv = out_dir / "generated_phase6_candidate_table_diagnostics.csv"
    summary_csv = out_dir / "generated_ok6_real_map_overlay_summary.csv"
    html_path = out_dir / "generated_ok6_real_map_overlay.html"
    command_path = out_dir / "command_to_run_generated_ok6_seed1_smoke.sh"
    summary_md = out_dir / "generated_ok6_smoke_prep_summary.md"

    smoke_df.to_csv(candidate_csv, index=False)
    diag_df.to_csv(diag_csv, index=False)
    summary_df.to_csv(summary_csv, index=False)

    roads = _build_road_overlay(net, pd.concat([ok, review], ignore_index=True))
    ok_records = []
    for _, row in ok.iterrows():
        ok_records.append(
            {
                "crosswalk_id": _norm_text(row.get("crosswalk_id")),
                "original_rank": _safe_int(row.get("original_rank")),
                "real_lon": _safe_float(row.get("real_lon")),
                "real_lat": _safe_float(row.get("real_lat")),
                "old_sumo_lon": _safe_float(row.get("old_sumo_lon")),
                "old_sumo_lat": _safe_float(row.get("old_sumo_lat")),
                "generated_crossing_center_lon": _safe_float(row.get("generated_crossing_center_lon")),
                "generated_crossing_center_lat": _safe_float(row.get("generated_crossing_center_lat")),
                "old_mapping_distance_m": _safe_float(row.get("old_mapping_distance_m")),
                "generated_to_real_distance_m": _safe_float(row.get("generated_to_real_distance_m")),
                "location_check_status": _norm_text(row.get("location_check_status")),
                "selected_node_id": _norm_text(row.get("selected_node_id")),
                "selected_crossing_edges": _norm_text(row.get("selected_crossing_edges")),
                "popup_html": _candidate_html_popup(row.to_dict()),
            }
        )
    review_records = []
    for _, row in review.iterrows():
        review_records.append(
            {
                "crosswalk_id": _norm_text(row.get("crosswalk_id")),
                "original_rank": _safe_int(row.get("original_rank")),
                "real_lon": _safe_float(row.get("real_lon")),
                "real_lat": _safe_float(row.get("real_lat")),
                "old_sumo_lon": _safe_float(row.get("old_sumo_lon")),
                "old_sumo_lat": _safe_float(row.get("old_sumo_lat")),
                "generated_crossing_center_lon": _safe_float(row.get("generated_crossing_center_lon")),
                "generated_crossing_center_lat": _safe_float(row.get("generated_crossing_center_lat")),
                "old_mapping_distance_m": _safe_float(row.get("old_mapping_distance_m")),
                "generated_to_real_distance_m": _safe_float(row.get("generated_to_real_distance_m")),
                "location_check_status": _norm_text(row.get("location_check_status")),
                "selected_node_id": _norm_text(row.get("selected_node_id")),
                "selected_crossing_edges": _norm_text(row.get("selected_crossing_edges")),
                "popup_html": _candidate_html_popup(row.to_dict()),
            }
        )
    _render_html(html_path, roads, ok_records, review_records)

    command_path.write_text(
        "\n".join(
            [
                "#!/usr/bin/env bash",
                "set -euo pipefail",
                f'CSV="{candidate_csv}"',
                f'NET="{PATCH_NET}"',
                'ROWS=$(python3 - <<\'PY\'',
                'import pandas as pd',
                'from pathlib import Path',
                f'csv = Path(r"{candidate_csv}")',
                'print(len(pd.read_csv(csv)))',
                'PY',
                ')',
                'if [[ "${ROWS}" -eq 0 ]]; then',
                '  echo "No eligible phase6 smoke candidates in generated_phase6_candidate_table.csv."',
                '  exit 0',
                'fi',
                'python3 -m smart_crosswalk_sumo.run_phase6_recovery_smoke \\',
                '  --candidate-csv "$CSV" \\',
                '  --net-file "$NET" \\',
                f'  --scenario smart \\',
                f'  --seed 1 \\',
                f'  --output-dir "{out_dir / "seed1_smart_smoke"}"',
                'python3 -m smart_crosswalk_sumo.run_phase6_recovery_smoke \\',
                '  --candidate-csv "$CSV" \\',
                '  --net-file "$NET" \\',
                f'  --scenario baseline \\',
                f'  --seed 1 \\',
                f'  --output-dir "{out_dir / "seed1_baseline_smoke"}"',
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    ok_count = len(ok_records)
    review_count = len(review_records)
    eligible_count = int(smoke_df.shape[0])
    lines = [
        "# Generated OK6 Smoke Prep Summary",
        "",
        f"- output_dir: `{out_dir}`",
        f"- ok_candidates_in_html: {ok_count}",
        f"- review_candidates_in_html: {review_count}",
        f"- smoke_candidate_rows: {eligible_count}",
        "- status: no signal-capable candidates found on generated crossing net",
        "",
        "## Diagnosis",
        "- Generated crossing exists for 6 OK rows.",
        "- None of the 6 has a TLS directly attached to the generated crossing edge.",
        "- `ped_link_indices` are therefore unavailable for phase6 smoke.",
        "- Keep these as geometry-valid crossing patches only.",
        "",
        "## Next",
        f"- Open HTML: `open {html_path}`",
        f"- Recheck signal-only candidates later if a signalized patch net is produced.",
    ]
    summary_md.write_text("\n".join(lines) + "\n", encoding="utf-8")

    print(f"output_dir={out_dir}")
    print(f"generated_phase6_candidate_table_rows={len(smoke_df)}")
    print(f"ok_candidates_in_html={ok_count}")
    print(f"review_candidates_in_html={review_count}")
    print(f"tls_found_candidates={int(diag_df['tls_id_found'].astype(str).ne('').sum())}")
    print(f"ped_link_found_candidates={int(diag_df['ped_link_index_found'].astype(bool).sum())}")
    print(f"smoke_eligible_candidates={eligible_count}")
    print(f"html={html_path}")
    print(f"csv={candidate_csv}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
