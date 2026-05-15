#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

from smart_crosswalk_sumo.network_utils import apply_sumo_environment, edge_function, read_net
from smart_crosswalk_sumo.run_p4_manual_edge_pair_recovery import (
    _default_manual_strategy,
    _csv,
    _edge_candidate_records,
    _norm,
    _safe_float,
    _safe_int,
)


BASE_DIR = Path(__file__).resolve().parents[1]
RESULT_DIR = BASE_DIR / "result"
DEFAULT_P1_CANDIDATE_TABLE = RESULT_DIR / "phase_next_remaining22_p1_signal_fix_20260516_010953" / "remaining_p1_signal_fix_candidate_table.csv"
DEFAULT_P1_NET = RESULT_DIR / "phase_next_remaining22_p1_signal_fix_20260516_010953" / "remaining_p1_signal_fix_net_v1.net.xml"
DEFAULT_P4_CANDIDATES = RESULT_DIR / "phase_next_remaining22_p1_signal_fix_20260516_010953" / "remaining_p4_manual_review_candidates.csv"
DEFAULT_MAPPING_SUMMARY = RESULT_DIR / "phase_next_top50_mapping_visual_audit_20260515_174626" / "03_top50_mapping_distance_summary.csv"
DEFAULT_GENERATED_CHECK = RESULT_DIR / "phase_next_bad22_generated_crossing_patch_20260515_181150" / "generated_crossing_location_check.csv"
DEFAULT_GENERATED_PATCH = RESULT_DIR / "phase_next_bad22_generated_crossing_patch_20260515_181150" / "generated_crossing_patch_candidates.csv"
DEFAULT_BASE_NET = RESULT_DIR / "phase_next_recovery_command_plan_20260514_200908" / "batch_03_run" / "recovery_tls_batch_network_v1.net.xml"


def _now_tag() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def _ensure_dirs(root: Path) -> dict[str, Path]:
    subdirs = {}
    for name in ["csv", "html", "commands", "readme", "patches", "manifests"]:
        path = root / name
        path.mkdir(parents=True, exist_ok=True)
        subdirs[name] = path
    return subdirs


def _manual_strategy(failure_category: str, manual_review_type: str) -> str:
    fc = _norm(failure_category).upper()
    mrt = _norm(manual_review_type).upper()
    if fc == "MANUAL_REVIEW_NEEDED" or mrt == "MANUAL_EDGE_PAIR_SELECTION":
        return "EXISTING_CROSSING_REMAP"
    if fc == "GENERATED_CROSSING_RETRY_POSSIBLE":
        return "MANUAL_CROSSING_ADD"
    if fc == "LOCATION_TOO_FAR":
        return "HOLD"
    return "REJECT"


def _recovery_strategy(failure_category: str, manual_review_type: str) -> str:
    strat = _manual_strategy(failure_category, manual_review_type)
    if strat in {"EXISTING_CROSSING_REMAP", "MANUAL_CROSSING_ADD"}:
        return "P4_MANUAL_EDGE_PAIR_REVIEW"
    if strat == "HOLD":
        return "P4_HOLD"
    return "P4_REJECT"


def _template_rows(p4_df: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for _, row in p4_df.sort_values(["original_rank", "crosswalk_id"]).iterrows():
        failure_category = _norm(row.get("failure_category"))
        manual_review_type = _norm(row.get("manual_review_type"))
        strategy = _manual_strategy(failure_category, manual_review_type)
        rows.append(
            {
                "crosswalk_id": _norm(row.get("crosswalk_id")),
                "original_rank": _safe_int(row.get("original_rank")),
                "real_lon": _safe_float(row.get("real_lon")),
                "real_lat": _safe_float(row.get("real_lat")),
                "mapping_distance_m": round(_safe_float(row.get("mapping_distance_m")), 3),
                "failure_category": failure_category,
                "recovery_strategy": _recovery_strategy(failure_category, manual_review_type),
                "manual_strategy": strategy,
                "selected_node_id": "",
                "selected_tls_id": "",
                "selected_crossing_edges": "",
                "selected_from_edge": "",
                "selected_to_edge": "",
                "needs_new_signal": "TRUE" if strategy == "NEW_SIGNAL_INSTALLATION" else "FALSE",
                "confidence": "",
                "manual_reason": "",
                "review_status": "NEEDS_MANUAL_SELECTION",
            }
        )
    return pd.DataFrame(rows)


def _nearby_rows(net: Any, p4_df: pd.DataFrame) -> tuple[pd.DataFrame, list[dict[str, Any]]]:
    rows: list[dict[str, Any]] = []
    map_records: list[dict[str, Any]] = []

    for _, row in p4_df.sort_values(["original_rank", "crosswalk_id"]).iterrows():
        edge_rows, context = _edge_candidate_records(net, row)
        edge_rows = edge_rows[:8]
        for candidate_rank, item in enumerate(edge_rows, start=1):
            suggested_pair_rank = 1 if candidate_rank <= 2 else ""
            item = dict(item)
            item["candidate_rank"] = candidate_rank
            item["suggested_pair_rank"] = suggested_pair_rank
            item["note"] = (
                f"real-point neighborhood; top2 usually pair seed; "
                f"strategy={_manual_strategy(item['failure_category'], row.get('manual_review_type'))}"
            )
            rows.append(
                {
                    "crosswalk_id": item["crosswalk_id"],
                    "original_rank": item["original_rank"],
                    "real_lon": item["real_lon"],
                    "real_lat": item["real_lat"],
                    "current_sumo_lon": item["current_sumo_lon"],
                    "current_sumo_lat": item["current_sumo_lat"],
                    "generated_lon": item["generated_lon"],
                    "generated_lat": item["generated_lat"],
                    "mapping_distance_m": item["mapping_distance_m"],
                    "failure_category": item["failure_category"],
                    "nearest_tls_id": item["nearest_tls_id"],
                    "nearest_tls_distance_m": item["nearest_tls_distance_m"],
                    "nearby_node_id": item["nearby_node_id"],
                    "nearby_node_distance_m": item["nearby_node_distance_m"],
                    "candidate_edge_id": item["candidate_edge_id"],
                    "candidate_edge_distance_m": item["candidate_edge_distance_m"],
                    "candidate_edge_function": item["candidate_edge_function"],
                    "candidate_edge_from_node": item["candidate_edge_from_node"],
                    "candidate_edge_to_node": item["candidate_edge_to_node"],
                    "candidate_edge_angle": item["candidate_edge_angle"],
                    "candidate_rank": candidate_rank,
                    "suggested_pair_rank": suggested_pair_rank,
                    "note": item["note"],
                }
            )
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
                "manual_strategy": _manual_strategy(row.get("failure_category"), row.get("manual_review_type")),
                "nearest_tls_id": context["nearest_tls_id"],
                "nearest_tls_distance_m": context["nearest_tls_distance_m"],
                "nearest_tls_lon": context["nearest_tls_lon"],
                "nearest_tls_lat": context["nearest_tls_lat"],
                "nearby_nodes_json": context["nearby_nodes_json"],
                "candidate_edges_json": context["candidate_edges_json"],
                "suggested_selected_node_id": (json.loads(context["nearby_nodes_json"])[0]["node_id"] if context["nearby_nodes_json"] not in {"", "[]"} else ""),
                "suggested_selected_tls_id": context["nearest_tls_id"],
            }
        )

    return pd.DataFrame(rows), map_records


def _html_escape(v: Any) -> str:
    return (
        str(v)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#39;")
    )


def _render_review_map(out_html: Path, records: list[dict[str, Any]]) -> None:
    center = [37.56, 126.98]
    for rec in records:
        if rec.get("real_lat") != "" and rec.get("real_lon") != "":
            center = [float(rec["real_lat"]), float(rec["real_lon"])]
            break

    payload = json.dumps(records, ensure_ascii=False)
    sidebar_items = []
    for rec in records:
        warn = "LOCATION_TOO_FAR" if _norm(rec.get("failure_category")).upper() == "LOCATION_TOO_FAR" else ""
        sidebar_items.append(
            f"""<div class="cand-item {warn.lower()}" data-cid="{_html_escape(rec['crosswalk_id'])}" onclick="focusCandidate('{_html_escape(rec['crosswalk_id'])}')">
  <div class="cand-title">{_html_escape(rec['crosswalk_id'])}</div>
  <div class="cand-sub">rank {_html_escape(rec['original_rank'])} | {_html_escape(rec['failure_category'])}</div>
  <div class="cand-sub">strategy {_html_escape(rec['manual_strategy'])}</div>
</div>"""
        )

    doc = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>P4 manual approval review map</title>
  <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
  <style>
    html, body {{ height: 100%; margin: 0; }}
    body {{ background: #0f172a; font-family: Inter, ui-sans-serif, system-ui, sans-serif; }}
    #map {{ position: absolute; inset: 0; }}
    .sidebar {{
      position: absolute; top: 12px; left: 12px; z-index: 1000; width: 300px; max-height: calc(100vh - 24px);
      overflow: auto; background: rgba(15, 23, 42, 0.94); color: #e2e8f0; border: 1px solid rgba(148, 163, 184, 0.25);
      border-radius: 14px; box-shadow: 0 16px 40px rgba(0,0,0,0.24);
    }}
    .sidebar h1 {{ margin: 0; padding: 14px 14px 8px; font-size: 16px; }}
    .sidebar p {{ margin: 0; padding: 0 14px 12px; font-size: 12px; color: #cbd5e1; line-height: 1.4; }}
    .cand-item {{ margin: 8px 10px; padding: 10px; border-radius: 12px; background: rgba(30,41,59,.85); cursor: pointer; border: 1px solid rgba(148,163,184,.18); }}
    .cand-item:hover {{ border-color: rgba(56,189,248,.7); }}
    .cand-item.location_too_far {{ background: rgba(69,10,10,.85); }}
    .cand-title {{ font-size: 13px; font-weight: 700; }}
    .cand-sub {{ font-size: 11px; color: #cbd5e1; margin-top: 3px; }}
    .legend {{ display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 6px 8px; padding: 0 14px 14px; font-size: 11px; color: #cbd5e1; }}
    .dot {{ display: inline-block; width: 10px; height: 10px; border-radius: 999px; margin-right: 6px; vertical-align: middle; }}
    .real {{ background: #22c55e; }}
    .current {{ background: #94a3b8; }}
    .generated {{ background: #38bdf8; }}
    .tls {{ background: #f43f5e; }}
    .node {{ background: #f59e0b; }}
    .edge {{ background: #a855f7; }}
    .review-warning {{ color: #fca5a5; font-weight: 700; }}
    .leaflet-popup-content {{ max-width: 520px; }}
    table {{ border-collapse: collapse; width: 100%; }}
    th, td {{ border: 1px solid rgba(148,163,184,.25); padding: 3px 5px; font-size: 12px; vertical-align: top; }}
    th {{ text-align: left; background: rgba(30,41,59,.72); color: #cbd5e1; }}
    td {{ color: #f8fafc; }}
  </style>
</head>
<body>
<div class="sidebar">
  <h1>P4 manual approval review</h1>
  <p>Real point first. current SUMO point only reference. generated point only extra hint. P4 stays out of ready until manual approval.</p>
  <div class="legend">
    <span><i class="dot real"></i> real point</span>
    <span><i class="dot current"></i> current SUMO</span>
    <span><i class="dot generated"></i> generated point</span>
    <span><i class="dot tls"></i> nearest TLS</span>
    <span><i class="dot node"></i> nearby nodes</span>
    <span><i class="dot edge"></i> nearby edges</span>
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

function esc(v) {{
  return String(v ?? '').replace(/[&<>"']/g, s => ({{
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
  }}[s]));
}}

function point(lat, lon) {{
  return [Number(lat), Number(lon)];
}}

function popupHtml(r) {{
  const nodes = JSON.parse(r.nearby_nodes_json || '[]').slice(0, 5);
  const edges = JSON.parse(r.candidate_edges_json || '[]').slice(0, 8);
  const rows = [
    ['crosswalk_id', r.crosswalk_id],
    ['original_rank', r.original_rank],
    ['failure_category', r.failure_category],
    ['mapping_distance_m', r.mapping_distance_m],
    ['nearest_tls_id', r.nearest_tls_id],
    ['nearest_tls_distance_m', r.nearest_tls_distance_m],
    ['suggested_selected_node_id', r.suggested_selected_node_id || ''],
    ['suggested_selected_tls_id', r.suggested_selected_tls_id || ''],
    ['suggested_selected_from_edge', edges[0] ? edges[0].edge_id : ''],
    ['suggested_selected_to_edge', edges[1] ? edges[1].edge_id : ''],
    ['suggested_selected_crossing_edges', edges.length >= 2 ? `${{edges[0].edge_id}} ${{edges[1].edge_id}}` : ''],
    ['manual_strategy', r.manual_strategy],
    ['approval_warning', 'P4 not ready until review_status=APPROVED and seed1 smoke passed'],
  ].map(([k, v]) => `<tr><th>${{esc(k)}}</th><td>${{esc(v)}}</td></tr>`).join('');
  const nodeList = nodes.map(n => `<li>${{esc(n.node_id)}} (${{esc(n.distance_m)}}m)</li>`).join('');
  const edgeList = edges.map(e => `<li>${{esc(e.edge_id)}} (${{esc(e.distance_m)}}m, ${{esc(e.from_node)}} → ${{esc(e.to_node)}})</li>`).join('');
  return `<table>${{rows}}</table><div style="margin-top:6px;font-size:12px;font-weight:700;">nearby_node_id top 5</div><ul style="margin:4px 0 0 18px;">${{nodeList}}</ul><div style="margin-top:6px;font-size:12px;font-weight:700;">candidate_edge_id top 8</div><ul style="margin:4px 0 0 18px;">${{edgeList}}</ul>`;
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

const colors = ['#22c55e', '#38bdf8', '#f59e0b', '#a855f7', '#f43f5e', '#84cc16', '#e879f9', '#60a5fa'];
for (const r of data) {{
  const cid = r.crosswalk_id;
  const color = colors[(Number(r.original_rank) - 1) % colors.length];
  const group = L.layerGroup().addTo(map);
  layers[cid] = group;

  if (r.real_lat !== '' && r.real_lon !== '') {{
    const m = L.circleMarker(point(r.real_lat, r.real_lon), {{radius: 8, color: '#22c55e', fillColor: '#22c55e', fillOpacity: 0.95}}).addTo(group);
    m.bindPopup(popupHtml(r));
    bounds.push(point(r.real_lat, r.real_lon));
  }}
  if (r.current_sumo_lat !== '' && r.current_sumo_lon !== '') {{
    const m = L.circleMarker(point(r.current_sumo_lat, r.current_sumo_lon), {{radius: 7, color: '#94a3b8', fillColor: '#94a3b8', fillOpacity: 0.9}}).addTo(group);
    m.bindPopup(popupHtml(r));
    bounds.push(point(r.current_sumo_lat, r.current_sumo_lon));
  }}
  if (r.generated_lat !== '' && r.generated_lon !== '' && !Number.isNaN(Number(r.generated_lat)) && !Number.isNaN(Number(r.generated_lon))) {{
    const m = L.circleMarker(point(r.generated_lat, r.generated_lon), {{radius: 7, color: '#38bdf8', fillColor: '#38bdf8', fillOpacity: 0.9}}).addTo(group);
    m.bindPopup(popupHtml(r));
    bounds.push(point(r.generated_lat, r.generated_lon));
  }}
  if (r.nearest_tls_lat !== '' && r.nearest_tls_lon !== '' && !Number.isNaN(Number(r.nearest_tls_lat)) && !Number.isNaN(Number(r.nearest_tls_lon))) {{
    const m = L.circleMarker(point(r.nearest_tls_lat, r.nearest_tls_lon), {{radius: 7, color: '#f43f5e', fillColor: '#f43f5e', fillOpacity: 0.9}}).addTo(group);
    m.bindPopup(popupHtml(r));
    bounds.push(point(r.nearest_tls_lat, r.nearest_tls_lon));
  }}

  const nodes = JSON.parse(r.nearby_nodes_json || '[]').slice(0, 5);
  for (const n of nodes) {{
    if (n.lat === '' || n.lon === '' || Number.isNaN(Number(n.lat)) || Number.isNaN(Number(n.lon))) continue;
    const m = L.circleMarker(point(n.lat, n.lon), {{radius: 4, color: '#f59e0b', fillColor: '#f59e0b', fillOpacity: 0.8}}).addTo(group);
    m.bindPopup(popupHtml(r));
    bounds.push(point(n.lat, n.lon));
  }}

  const edges = JSON.parse(r.candidate_edges_json || '[]').slice(0, 8);
  edges.forEach((e, idx) => {{
    const shape = e.shape || [];
    if (!shape.length) return;
    const poly = L.polyline(shape.map(pt => [Number(pt[1]), Number(pt[0])]), {{
      color: idx < 2 ? color : '#a855f7',
      weight: idx < 2 ? 5 : 3,
      opacity: idx < 2 ? 0.8 : 0.35,
      dashArray: idx < 2 ? '' : '4 6',
    }}).addTo(group);
    poly.bindPopup(popupHtml(r));
    for (const pt of shape) bounds.push([Number(pt[1]), Number(pt[0])]);
  }});
}}

if (bounds.length) {{
  map.fitBounds(bounds, {{padding: [24, 24]}});
}}
</script>
</body>
</html>
"""
    out_html.write_text(doc, encoding="utf-8")


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="Prepare P1+P4 recovery experiment plan without building any net.")
    parser.add_argument("--output-dir", default="", help="Optional output dir. Default: timestamped result dir.")
    parser.add_argument("--base-net", default=str(DEFAULT_BASE_NET))
    parser.add_argument("--p1-candidate-table", default=str(DEFAULT_P1_CANDIDATE_TABLE))
    parser.add_argument("--p1-net", default=str(DEFAULT_P1_NET))
    parser.add_argument("--p4-candidates", default=str(DEFAULT_P4_CANDIDATES))
    parser.add_argument("--mapping-summary", default=str(DEFAULT_MAPPING_SUMMARY))
    parser.add_argument("--generated-check", default=str(DEFAULT_GENERATED_CHECK))
    parser.add_argument("--generated-patch", default=str(DEFAULT_GENERATED_PATCH))
    args = parser.parse_args()

    apply_sumo_environment()
    out_dir = Path(args.output_dir).expanduser().resolve() if args.output_dir else RESULT_DIR / f"phase_next_p1_p4_recovery_experiment_plan_{_now_tag()}"
    out_dir.mkdir(parents=True, exist_ok=True)
    dirs = _ensure_dirs(out_dir)

    base_net = Path(args.base_net).expanduser().resolve()
    p1_table = Path(args.p1_candidate_table).expanduser().resolve()
    p1_net = Path(args.p1_net).expanduser().resolve()
    p4_csv = Path(args.p4_candidates).expanduser().resolve()
    if not base_net.exists():
        raise FileNotFoundError(base_net)
    if not p1_table.exists():
        raise FileNotFoundError(p1_table)
    if not p1_net.exists():
        raise FileNotFoundError(p1_net)
    if not p4_csv.exists():
        raise FileNotFoundError(p4_csv)

    p1_df = pd.read_csv(p1_table)
    p4_df = pd.read_csv(p4_csv)
    if p4_df.empty:
        raise RuntimeError("P4 candidate input empty")

    net = read_net(base_net)
    template_df = _template_rows(p4_df)
    nearby_df, map_records = _nearby_rows(net, p4_df)

    template_cols = [
        "crosswalk_id",
        "original_rank",
        "real_lon",
        "real_lat",
        "mapping_distance_m",
        "failure_category",
        "recovery_strategy",
        "manual_strategy",
        "selected_node_id",
        "selected_tls_id",
        "selected_crossing_edges",
        "selected_from_edge",
        "selected_to_edge",
        "needs_new_signal",
        "confidence",
        "manual_reason",
        "review_status",
    ]
    nearby_cols = [
        "crosswalk_id",
        "original_rank",
        "real_lon",
        "real_lat",
        "current_sumo_lon",
        "current_sumo_lat",
        "generated_lon",
        "generated_lat",
        "mapping_distance_m",
        "failure_category",
        "nearest_tls_id",
        "nearest_tls_distance_m",
        "nearby_node_id",
        "nearby_node_distance_m",
        "candidate_edge_id",
        "candidate_edge_distance_m",
        "candidate_edge_function",
        "candidate_edge_from_node",
        "candidate_edge_to_node",
        "candidate_edge_angle",
        "candidate_rank",
        "suggested_pair_rank",
        "note",
    ]
    template_df = template_df.reindex(columns=template_cols)
    nearby_df = nearby_df.reindex(columns=nearby_cols)

    template_path = dirs["csv"] / "manual_edge_pair_selection_template.csv"
    nearby_path = dirs["csv"] / "p4_nearby_edge_tls_candidates.csv"
    template_df.to_csv(template_path, index=False)
    nearby_df.to_csv(nearby_path, index=False)

    _render_review_map(dirs["html"] / "p4_manual_approval_review_map.html", map_records)

    plan_md = f"""# P1 + P4 recovery experiment map plan

- reason split: default 28 ready candidates are already split into `current_main_12`, `signal_fix_9`, `generated_signal_7`. Do not merge them into new map.
- P1 path: reuse existing `remaining_p1_signal_fix_net_v1.net.xml` and its candidate table. Do not mark ready before seed1 smoke.
- P4 path: manual only. Real point first. current SUMO point only reference. generated point only auxiliary.
- P4 approval gate: `review_status=APPROVED` + non-empty `selected_crossing_edges` + non-empty `selected_node_id` + non-empty `selected_tls_id` or explicit new-signal intent.
- P4 net build: plain XML patch + netconvert only. No direct net.xml editing.
- P4 after approval: build separate P4 manual recovery net first, then use approved P4 on top of P1 net for combined P1+P4 experiment.
- seed1 smoke: not ready until seed1 smoke command completes successfully.
- 30 seed: only after seed1 smoke and separate baseline built from P1+P4 recovery net.
- baseline: separate baseline per experiment net. Do not reuse baseline from `current_main_12`, `signal_fix_9`, or `generated_signal_7`.
- generated/new signal candidates: keep separate from main experiment.

## Inputs
- P1 candidate table rows: {len(p1_df)}
- P4 candidate rows: {len(p4_df)}
- base net: `{base_net}`
- P1 net: `{p1_net}`

## Outputs
- csv/manual_edge_pair_selection_template.csv
- csv/p4_nearby_edge_tls_candidates.csv
- html/p4_manual_approval_review_map.html
- readme/p1_p4_recovery_map_plan.md
- readme/p4_manual_approval_guide.md
"""
    _write(dirs["readme"] / "p1_p4_recovery_map_plan.md", plan_md + "\n")

    guide_md = """# P4 manual approval guide

1. Open HTML map from `html/p4_manual_approval_review_map.html`.
2. Judge by real point first. current SUMO mapped point can be wrong.
3. generated point, if any, is only secondary hint.
4. Choose `selected_node_id` from nearby nodes near real point.
5. Choose `selected_tls_id` from nearest TLS or exact node TLS when applicable.
6. Choose `selected_from_edge` and `selected_to_edge` as actual road pair crossed by pedestrian crossing.
7. Write `selected_crossing_edges` as pair text, usually two edge ids separated by space.
8. `manual_strategy`:
   - `EXISTING_CROSSING_REMAP` for existing crossing re-anchor.
   - `MANUAL_CROSSING_ADD` for new crossing geometry.
   - `EXISTING_TLS_RETROFIT` for existing TLS phase fix.
   - `NEW_SIGNAL_INSTALLATION` only when new traffic light install is necessary.
   - `HOLD` for uncertain or location-too-far cases.
   - `REJECT` for out-of-scope cases.
9. `confidence`: `HIGH`, `MEDIUM`, `LOW`.
10. `review_status`: `APPROVED`, `NEEDS_MORE_REVIEW`, `HOLD`, `REJECT`.
11. `APPROVED` does not mean ready. Seed1 smoke must pass first.
12. Do not put P4 into 30-seed until seed1 smoke passes.
"""
    _write(dirs["readme"] / "p4_manual_approval_guide.md", guide_md + "\n")

    open_map = f"""#!/usr/bin/env bash
set -euo pipefail
open "{(dirs['html'] / 'p4_manual_approval_review_map.html').resolve()}"
"""
    copy_template = f"""#!/usr/bin/env bash
set -euo pipefail
cp "{template_path.resolve()}" "{(dirs['csv'] / 'manual_edge_pair_selection.csv').resolve()}"
echo "{(dirs['csv'] / 'manual_edge_pair_selection.csv').resolve()}"
"""

    validate_manual = f"""#!/usr/bin/env bash
set -euo pipefail
python3 - <<'PY'
from pathlib import Path
import pandas as pd

csv_path = Path(r"{(dirs['csv'] / 'manual_edge_pair_selection.csv').resolve()}")
if not csv_path.exists():
    raise SystemExit(f"missing manual csv: {{csv_path}}")
df = pd.read_csv(csv_path)
required = ['crosswalk_id','original_rank','real_lon','real_lat','mapping_distance_m','failure_category','recovery_strategy','manual_strategy','selected_node_id','selected_tls_id','selected_crossing_edges','selected_from_edge','selected_to_edge','needs_new_signal','confidence','manual_reason','review_status']
missing = [c for c in required if c not in df.columns]
if missing:
    raise SystemExit(f"missing columns: {{missing}}")
allowed_manual = {{'EXISTING_CROSSING_REMAP','MANUAL_CROSSING_ADD','EXISTING_TLS_RETROFIT','NEW_SIGNAL_INSTALLATION','HOLD','REJECT'}}
allowed_status = {{'APPROVED','NEEDS_MORE_REVIEW','HOLD','REJECT','NEEDS_MANUAL_SELECTION'}}
allowed_conf = {{'HIGH','MEDIUM','LOW',''}}
bad_manual = sorted(set(x for x in df['manual_strategy'].astype(str).str.upper().tolist() if x and x not in allowed_manual))
bad_status = sorted(set(x for x in df['review_status'].astype(str).str.upper().tolist() if x and x not in allowed_status))
bad_conf = sorted(set(x for x in df['confidence'].astype(str).str.upper().tolist() if x and x not in allowed_conf))
if bad_manual:
    raise SystemExit(f"invalid manual_strategy: {{bad_manual}}")
if bad_status:
    raise SystemExit(f"invalid review_status: {{bad_status}}")
if bad_conf:
    raise SystemExit(f"invalid confidence: {{bad_conf}}")
approved = df[df['review_status'].astype(str).str.upper() == 'APPROVED']
if not approved.empty:
    empty_edges = approved['selected_crossing_edges'].astype(str).str.strip().eq('')
    if bool(empty_edges.any()):
        raise SystemExit('APPROVED rows must have selected_crossing_edges')
print('manual_csv_ok', len(df), 'approved', len(approved))
PY
"""

    build_p4 = f"""#!/usr/bin/env bash
set -euo pipefail
cd "{BASE_DIR}"
MANUAL_CSV="{(dirs['csv'] / 'manual_edge_pair_selection.csv').resolve()}"
if [ ! -f "$MANUAL_CSV" ]; then
  echo "missing manual csv: $MANUAL_CSV" >&2
  exit 1
fi
APPROVED_COUNT=$(python3 - <<'PY'
from pathlib import Path
import pandas as pd
csv_path = Path(r"{(dirs['csv'] / 'manual_edge_pair_selection.csv').resolve()}")
df = pd.read_csv(csv_path)
print(int((df['review_status'].astype(str).str.upper() == 'APPROVED').sum()))
PY
)
if [ "$APPROVED_COUNT" -lt 1 ]; then
  echo "no APPROVED rows; stop" >&2
  exit 1
fi
BUILD_DIR="{(dirs['patches'] / 'p4_manual_recovery_after_approval').resolve()}"
mkdir -p "$BUILD_DIR"
python3 -m smart_crosswalk_sumo.run_p4_manual_edge_pair_recovery \
  --output-dir "$BUILD_DIR" \
  --manual-selection-csv "$MANUAL_CSV" \
  --base-net "{base_net}" \
  --p4-candidates "{p4_csv}" \
  --mapping-summary "{Path(args.mapping_summary).expanduser().resolve()}" \
  --generated-check "{Path(args.generated_check).expanduser().resolve()}" \
  --generated-patch "{Path(args.generated_patch).expanduser().resolve()}"
cp "$BUILD_DIR/p4_manual_crossing_patch.nod.xml" "{(dirs['patches'] / 'p4_manual_crossing_patch.nod.xml').resolve()}"
cp "$BUILD_DIR/p4_manual_tllogic_patch.tll.xml" "{(dirs['patches'] / 'p4_manual_tllogic_patch.tll.xml').resolve()}"
cp "$BUILD_DIR/p4_manual_recovery_patch_plan.csv" "{(dirs['csv'] / 'p4_manual_recovery_patch_plan.csv').resolve()}"
cp "$BUILD_DIR/p4_manual_recovery_phase_audit.csv" "{(dirs['csv'] / 'p4_manual_recovery_phase_audit.csv').resolve()}"
cp "$BUILD_DIR/p4_manual_recovery_candidate_table.csv" "{(dirs['csv'] / 'p4_manual_recovery_candidate_table.csv').resolve()}"
cp "$BUILD_DIR/p4_manual_recovery_net.net.xml" "{(out_dir / 'p4_manual_recovery_net.net.xml').resolve()}"
"""

    build_p1_p4 = f"""#!/usr/bin/env bash
set -euo pipefail
cd "{BASE_DIR}"
MANUAL_CSV="{(dirs['csv'] / 'manual_edge_pair_selection.csv').resolve()}"
P1_CSV="{p1_table}"
P1_NET="{p1_net}"
if [ ! -f "$MANUAL_CSV" ]; then
  echo "missing manual csv: $MANUAL_CSV" >&2
  exit 1
fi
if [ ! -f "$P1_CSV" ]; then
  echo "missing P1 candidate table: $P1_CSV" >&2
  exit 1
fi
if [ ! -f "$P1_NET" ]; then
  echo "missing P1 net: $P1_NET" >&2
  exit 1
fi
APPROVED_COUNT=$(python3 - <<'PY'
from pathlib import Path
import pandas as pd
csv_path = Path(r"{(dirs['csv'] / 'manual_edge_pair_selection.csv').resolve()}")
df = pd.read_csv(csv_path)
print(int((df['review_status'].astype(str).str.upper() == 'APPROVED').sum()))
PY
)
if [ "$APPROVED_COUNT" -lt 1 ]; then
  echo "no APPROVED rows; stop" >&2
  exit 1
fi
BUILD_DIR="{(dirs['patches'] / 'p1_p4_recovery_after_approval').resolve()}"
mkdir -p "$BUILD_DIR"
python3 -m smart_crosswalk_sumo.run_p4_manual_edge_pair_recovery \
  --output-dir "$BUILD_DIR" \
  --manual-selection-csv "$MANUAL_CSV" \
  --base-net "$P1_NET" \
  --p4-candidates "{p4_csv}" \
  --mapping-summary "{Path(args.mapping_summary).expanduser().resolve()}" \
  --generated-check "{Path(args.generated_check).expanduser().resolve()}" \
  --generated-patch "{Path(args.generated_patch).expanduser().resolve()}"
cp "$BUILD_DIR/p4_manual_recovery_net.net.xml" "{(out_dir / 'p1_p4_recovery_net.net.xml').resolve()}"
"""

    run_smoke = f"""#!/usr/bin/env bash
set -euo pipefail
cd "{BASE_DIR}"
CSV="{(dirs['csv'] / 'p4_manual_recovery_candidate_table.csv').resolve()}"
NET="{(out_dir / 'p4_manual_recovery_net.net.xml').resolve()}"
python3 -m smart_crosswalk_sumo.run_phase6_recovery_smoke \
  --candidate-csv "$CSV" \
  --net-file "$NET" \
  --scenario smart \
  --seed 1 \
  --sim-duration 300 \
  --phase-aligned-ped-depart \
  --output-dir "{(dirs['patches'] / 'p4_manual_seed1_smart_smoke').resolve()}"

python3 -m smart_crosswalk_sumo.run_phase6_recovery_smoke \
  --candidate-csv "$CSV" \
  --net-file "$NET" \
  --scenario baseline \
  --seed 1 \
  --sim-duration 300 \
  --phase-aligned-ped-depart \
  --output-dir "{(dirs['patches'] / 'p4_manual_seed1_baseline_smoke').resolve()}"
"""

    check_smoke = f"""#!/usr/bin/env bash
set -euo pipefail
cd "{BASE_DIR}"
python3 - <<'PY'
from pathlib import Path
import pandas as pd

out_dir = Path(r"{out_dir.resolve()}")
cand = out_dir / "csv" / "p4_manual_recovery_candidate_table.csv"
smart = out_dir / "patches" / "p4_manual_seed1_smart_smoke" / "phase6_smoke_summary.csv"
base = out_dir / "patches" / "p4_manual_seed1_baseline_smoke" / "phase6_smoke_summary.csv"

def b(v):
    return str(v).strip().lower() in ("1", "true", "yes", "y")

def i(v, default=0):
    try:
        if pd.isna(v):
            return default
        return int(float(v))
    except Exception:
        return default

if not cand.exists():
    raise SystemExit(f"missing candidate table: {{cand}}")
if not smart.exists() or not base.exists():
    raise SystemExit("missing smoke summary")
cand_df = pd.read_csv(cand)
smart_df = pd.read_csv(smart)
base_df = pd.read_csv(base)
rows = []
for cid in cand_df["crosswalk_id"].astype(str).tolist():
    s = smart_df[smart_df["crosswalk_id"].astype(str) == cid]
    t = base_df[base_df["crosswalk_id"].astype(str) == cid]
    sr = s.iloc[0].to_dict() if not s.empty else {{}}
    tr = t.iloc[0].to_dict() if not t.empty else {{}}
    smart_ok = bool(sr) and b(sr.get("completed")) and i(sr.get("ped_crossing_person_count")) > 0 and i(sr.get("extension_count")) > 0
    base_ok = bool(tr) and b(tr.get("completed")) and i(tr.get("ped_crossing_person_count")) > 0 and i(tr.get("extension_count"), -1) == 0
    rows.append({{
        "crosswalk_id": cid,
        "smart_smoke_success": smart_ok,
        "baseline_smoke_success": base_ok,
        "ready_for_30seed": bool(smart_ok and base_ok),
    }})
out = pd.DataFrame(rows)
out.to_csv(out_dir / "manifests" / "p4_manual_seed1_smoke_verdict.csv", index=False)
print("verdict_csv=", out_dir / "manifests" / "p4_manual_seed1_smoke_verdict.csv")
print("rows=", len(out))
print("ready=", int(out["ready_for_30seed"].sum()) if not out.empty else 0)
PY
"""

    _write(dirs["commands"] / "command_to_open_p4_review_map.sh", open_map)
    _write(dirs["commands"] / "command_to_copy_manual_template_for_editing.sh", copy_template)
    _write(dirs["commands"] / "command_to_validate_manual_edge_pair_selection.sh", validate_manual)
    _write(dirs["commands"] / "command_to_build_p4_manual_recovery_after_approval.sh", build_p4)
    _write(dirs["commands"] / "command_to_build_p1_p4_recovery_net_after_p4_approval.sh", build_p1_p4)
    _write(dirs["commands"] / "command_to_run_p4_manual_seed1_smoke.sh", run_smoke)
    _write(dirs["commands"] / "command_to_check_p4_manual_seed1_smoke.sh", check_smoke)
    for p in (dirs["commands"]).iterdir():
        if p.is_file():
            p.chmod(0o755)

    manifest = {
        "output_dir": str(out_dir),
        "base_net": str(base_net),
        "p1_candidate_table": str(p1_table),
        "p1_net": str(p1_net),
        "p4_candidate_rows": int(len(p4_df)),
        "p4_manual_rows": int((p4_df["manual_review_type"].astype(str).str.upper() == "MANUAL_EDGE_PAIR_SELECTION").sum()),
        "template_csv": str(template_path),
        "nearby_csv": str(nearby_path),
        "review_map": str(dirs["html"] / "p4_manual_approval_review_map.html"),
        "plan_md": str(dirs["readme"] / "p1_p4_recovery_map_plan.md"),
        "guide_md": str(dirs["readme"] / "p4_manual_approval_guide.md"),
        "commands": sorted(str(p) for p in dirs["commands"].iterdir() if p.is_file()),
        "p1_baseline_rule": "baseline must be built from P1+P4 recovery net, not shared with current_main_12/signal_fix_9/generated_signal_7",
    }
    _write(dirs["manifests"] / "p1_p4_recovery_experiment_plan_manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")

    print(f"output_dir: {out_dir}")
    print(f"p4_candidate_rows: {len(p4_df)}")
    print(f"p4_manual_rows: {manifest['p4_manual_rows']}")
    print(f"template_csv: {template_path}")
    print(f"review_map: {dirs['html'] / 'p4_manual_approval_review_map.html'}")
    print(f"plan_md: {dirs['readme'] / 'p1_p4_recovery_map_plan.md'}")
    print(f"guide_md: {dirs['readme'] / 'p4_manual_approval_guide.md'}")


if __name__ == "__main__":
    main()
