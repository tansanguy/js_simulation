#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import subprocess
import xml.etree.ElementTree as ET
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

from smart_crosswalk_sumo.network_utils import (
    apply_sumo_environment,
    pedestrian_link_indices,
    pedestrian_route_from_crossing,
    read_net,
)

BASE_DIR = Path(__file__).resolve().parents[1]
RESULT_DIR = BASE_DIR / "result"
BASE_NET = RESULT_DIR / "phase_next_recovery_command_plan_20260514_200908" / "batch_03_run" / "recovery_tls_batch_network_v1.net.xml"
RECOVERY_DIR = RESULT_DIR / "phase_next_top50_max_installation_recovery_20260516_002620"
MAPPING_SUMMARY = RESULT_DIR / "phase_next_top50_mapping_visual_audit_20260515_174626" / "03_top50_mapping_distance_summary.csv"
TOP50_STATUS = RECOVERY_DIR / "top50_recovery_status_after_signal_and_crossing_patch.csv"
TOP50_READY28 = RECOVERY_DIR / "recovered_ready_for_30seed_candidates.csv"
SCREENING = RESULT_DIR / "phase_next_top50_final_candidate_screening" / "top50_signal_and_location_combined_screening.csv"
GENERATED_CHECK = RESULT_DIR / "phase_next_bad22_generated_crossing_patch_20260515_181150" / "generated_crossing_location_check.csv"
GENERATED_DIAG = RESULT_DIR / "phase_next_generated_ok6_phase6_prep_20260515_182319" / "generated_phase6_candidate_table_diagnostics.csv"

P1_TARGETS = {"NODE_10060", "NODE_6342", "NODE_5647", "NODE_14937", "NODE_122781"}
P5_TARGETS = {"LINK_266939", "NODE_6122", "LINK_127869", "LINK_230879"}


def _now() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def _norm(v: Any) -> str:
    text = str(v or "").strip()
    if not text or text in {"nan", "None", "NaT"}:
        return ""
    if text.endswith(".0") and text[:-2].replace("-", "", 1).isdigit():
        return text[:-2]
    return text


def _bool(v: Any) -> bool:
    if isinstance(v, bool):
        return v
    return _norm(v).lower() in {"1", "true", "yes", "y"}


def _int(v: Any, default: int = 0) -> int:
    try:
        if pd.isna(v):
            return default
        return int(float(v))
    except Exception:
        return default


def _float(v: Any, default: float = float("nan")) -> float:
    try:
        if pd.isna(v):
            return default
        return float(v)
    except Exception:
        return default


def _csv(path: Path) -> pd.DataFrame:
    if not path.exists():
        raise FileNotFoundError(path)
    return pd.read_csv(path)


def _ped_only_available(states: list[str], ped_indices: list[int]) -> bool:
    if not states or not ped_indices:
        return False
    for s in states:
        for p in ped_indices:
            if p < 0 or p >= len(s):
                continue
            if s[p] not in {"g", "G"}:
                continue
            non_ped_green = any(i != p and i < len(s) and s[i] in {"g", "G"} for i in range(len(s)))
            if not non_ped_green:
                return True
    return False


def _route_from_crossing(net: Any, crossing_edge_id: str) -> tuple[str, str]:
    try:
        e = net.getEdge(crossing_edge_id)
        route = pedestrian_route_from_crossing(e)
        return _norm(route.get("from_edge")), _norm(route.get("to_edge"))
    except Exception:
        return "", ""


def _build_phase_patch(source_net: Path, feasibility_df: pd.DataFrame, out_tll: Path) -> pd.DataFrame:
    root = ET.parse(source_net).getroot()
    tl_by_id = {_norm(t.attrib.get("id")): t for t in root.findall("tlLogic") if _norm(t.attrib.get("id"))}

    patch_root = ET.Element("tlLogics")
    rows: list[dict[str, Any]] = []

    valid = feasibility_df[feasibility_df["signal_fix_possible"].astype(bool)].copy()
    for tls_id, grp in valid.groupby(valid["tls_id_used"].astype(str), sort=False):
        tls_id = _norm(tls_id)
        tl = tl_by_id.get(tls_id)
        cid_join = "|".join(sorted(grp["crosswalk_id"].astype(str).tolist()))

        if tl is None:
            rows.append({"crosswalk_id": cid_join, "tls_id": tls_id, "patch_status": "REJECT", "reason": "missing_tlLogic"})
            continue

        ped_indices = sorted({_int(v, -1) for v in grp["ped_link_index_recovered"].tolist() if _int(v, -1) >= 0})
        if not ped_indices:
            rows.append({"crosswalk_id": cid_join, "tls_id": tls_id, "patch_status": "REJECT", "reason": "missing_ped_link_index"})
            continue

        tl_copy = ET.fromstring(ET.tostring(tl, encoding="utf-8"))
        phases = tl_copy.findall("phase")
        if not phases:
            rows.append({"crosswalk_id": cid_join, "tls_id": tls_id, "patch_status": "REJECT", "reason": "missing_phase"})
            continue

        max_len = max(len(_norm(p.attrib.get("state"))) for p in phases)
        bad = [p for p in ped_indices if p >= max_len]
        if bad:
            rows.append({"crosswalk_id": cid_join, "tls_id": tls_id, "patch_status": "REJECT", "reason": "ped_link_index_out_of_range"})
            continue

        target = None
        target_idx = -1
        for i, ph in enumerate(phases):
            st = _norm(ph.attrib.get("state"))
            if st and set(st.lower()) == {"r"}:
                target = ph
                target_idx = i
                break

        if target is None:
            target = ET.Element("phase", {"duration": "6", "state": "r" * max_len})
            tl_copy.append(target)
            target_idx = len(phases)
            action = "append_pedestrian_only_phase"
        else:
            action = "reuse_all_red_phase_as_pedestrian_only_phase"

        chars = list(_norm(target.attrib.get("state")))
        if len(chars) < max_len:
            chars += ["r"] * (max_len - len(chars))
        for p in ped_indices:
            chars[p] = "G"
        target.set("state", "".join(chars))
        if not _norm(target.attrib.get("duration")):
            target.set("duration", "6")

        patch_root.append(tl_copy)
        rows.append(
            {
                "crosswalk_id": cid_join,
                "tls_id": tls_id,
                "ped_link_indices": json.dumps(ped_indices, ensure_ascii=False),
                "patch_status": "PATCHED",
                "patch_action": action,
                "patched_phase_index": target_idx,
                "reason": "ped_link_index_only_green_vehicle_links_red",
            }
        )

    ET.indent(patch_root, space="  ")
    out_tll.write_text(ET.tostring(patch_root, encoding="unicode", xml_declaration=True), encoding="utf-8")
    return pd.DataFrame(rows)


def _netconvert_with_tll(source_net: Path, tll: Path, out_net: Path, log_path: Path) -> tuple[bool, list[str], str]:
    cmd = [
        "netconvert",
        "--sumo-net-file",
        str(source_net),
        "--tllogic-files",
        str(tll),
        "--output-file",
        str(out_net),
        "--no-turnarounds",
        "true",
        "--walkingareas",
        "true",
        "--crossings.guess",
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
    try:
        ret = subprocess.run(cmd, capture_output=True, text=True, timeout=1200)
        log_path.write_text((ret.stdout or "") + "\n" + (ret.stderr or ""), encoding="utf-8")
        msg = (ret.stderr or ret.stdout or "").strip()
        return ret.returncode == 0 and out_net.exists(), cmd, msg[:1000]
    except Exception as exc:
        log_path.write_text(str(exc), encoding="utf-8")
        return False, cmd, str(exc)


def _render_p4_map(out_html: Path, records: list[dict[str, Any]]) -> None:
    center = (37.56, 126.98)
    for r in records:
        if r.get("real_lat") != "" and r.get("real_lon") != "":
            center = (r["real_lat"], r["real_lon"])
            break

    payload = json.dumps(records, ensure_ascii=False)
    doc = f"""<!DOCTYPE html>
<html lang=\"en\">
<head>
  <meta charset=\"utf-8\"/>
  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\"/>
  <title>remaining_p4_manual_review_map_overlay</title>
  <link rel=\"stylesheet\" href=\"https://unpkg.com/leaflet@1.9.4/dist/leaflet.css\"/>
  <style>
    html, body, #map {{ height: 100%; margin: 0; }}
    .panel {{ position: absolute; z-index: 999; top: 12px; left: 12px; background: #111827; color: #f9fafb; padding: 10px 12px; border-radius: 8px; max-width: 460px; font-family: sans-serif; }}
    .panel h1 {{ margin: 0 0 4px; font-size: 15px; }}
    .panel p {{ margin: 0; font-size: 12px; color: #d1d5db; }}
    table {{ border-collapse: collapse; }}
    th, td {{ border: 1px solid #ddd; padding: 2px 4px; font-size: 12px; }}
  </style>
</head>
<body>
<div id=\"map\"></div>
<div class=\"panel\">
  <h1>P4 manual review map</h1>
  <p>green=real, gray=current SUMO, blue=generated (if any), red=nearest TLS.</p>
</div>
<script src=\"https://unpkg.com/leaflet@1.9.4/dist/leaflet.js\"></script>
<script>
const data = {payload};
const map = L.map('map').setView([{center[0]}, {center[1]}], 14);
L.tileLayer('https://{{s}}.tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png', {{maxZoom: 19}}).addTo(map);
const bounds = [];
function popup(r) {{
  const keys = ['crosswalk_id','original_rank','mapping_distance_m','failure_category','manual_review_type','recommended_action','nearest_tls_id','nearest_tls_distance_m'];
  const rows = keys.map(k => `<tr><th>${{k}}</th><td>${{r[k] ?? ''}}</td></tr>`).join('');
  return `<table>${{rows}}</table>`;
}}
for (const r of data) {{
  if (r.real_lat !== '' && r.real_lon !== '') {{
    L.circleMarker([r.real_lat, r.real_lon], {{radius:7, color:'#16a34a'}}).addTo(map).bindPopup(popup(r));
    bounds.push([r.real_lat, r.real_lon]);
  }}
  if (r.current_sumo_lat !== '' && r.current_sumo_lon !== '') {{
    L.circleMarker([r.current_sumo_lat, r.current_sumo_lon], {{radius:6, color:'#6b7280'}}).addTo(map);
    bounds.push([r.current_sumo_lat, r.current_sumo_lon]);
  }}
  if (r.generated_lat !== '' && r.generated_lon !== '') {{
    L.circleMarker([r.generated_lat, r.generated_lon], {{radius:6, color:'#2563eb'}}).addTo(map);
    bounds.push([r.generated_lat, r.generated_lon]);
  }}
  if (r.nearest_tls_lat !== '' && r.nearest_tls_lon !== '') {{
    L.circleMarker([r.nearest_tls_lat, r.nearest_tls_lon], {{radius:6, color:'#dc2626'}}).addTo(map);
    bounds.push([r.nearest_tls_lat, r.nearest_tls_lon]);
  }}
  if (r.real_lat !== '' && r.current_sumo_lat !== '') {{
    L.polyline([[r.real_lat, r.real_lon], [r.current_sumo_lat, r.current_sumo_lon]], {{color:'#64748b', dashArray:'4 4'}}).addTo(map);
  }}
  if (r.real_lat !== '' && r.generated_lat !== '') {{
    L.polyline([[r.real_lat, r.real_lon], [r.generated_lat, r.generated_lon]], {{color:'#f97316'}}).addTo(map);
  }}
  if (r.real_lat !== '' && r.nearest_tls_lat !== '') {{
    L.polyline([[r.real_lat, r.real_lon], [r.nearest_tls_lat, r.nearest_tls_lon]], {{color:'#dc2626', dashArray:'2 4'}}).addTo(map);
  }}
}}
if (bounds.length) map.fitBounds(bounds, {{padding:[40,40]}});
</script>
</body>
</html>
"""
    out_html.write_text(doc, encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="Recover remaining22 P1 candidates only (no long SUMO run).")
    parser.add_argument("--integrity-dir", type=Path, default=None, help="phase_next_top50_recovery_integrity_audit_* directory")
    parser.add_argument("--out-dir", type=Path, default=None)
    args = parser.parse_args()

    apply_sumo_environment()

    integrity_dir = args.integrity_dir
    if integrity_dir is None:
        cands = sorted(RESULT_DIR.glob("phase_next_top50_recovery_integrity_audit_*"))
        if not cands:
            raise FileNotFoundError("No integrity audit directory found")
        integrity_dir = cands[-1]

    out_dir = args.out_dir or (RESULT_DIR / f"phase_next_remaining22_p1_signal_fix_{_now()}")
    out_dir.mkdir(parents=True, exist_ok=True)

    diagnosis = _csv(integrity_dir / "remaining22_recovery_diagnosis.csv")
    roadmap_md = (integrity_dir / "remaining22_recovery_roadmap.md").read_text(encoding="utf-8")
    top50_status = _csv(TOP50_STATUS)
    top50_ready = _csv(TOP50_READY28)
    _ = _csv(SCREENING)
    mapping = _csv(MAPPING_SUMMARY)

    gen_check = _csv(GENERATED_CHECK) if GENERATED_CHECK.exists() else pd.DataFrame()
    gen_diag = _csv(GENERATED_DIAG) if GENERATED_DIAG.exists() else pd.DataFrame()

    net = read_net(BASE_NET)
    root = ET.parse(BASE_NET).getroot()

    tl_by_id = {_norm(t.attrib.get("id")): t for t in root.findall("tlLogic") if _norm(t.attrib.get("id"))}
    conn_by_tl: dict[str, list[ET.Element]] = defaultdict(list)
    for c in root.findall("connection"):
        tl = _norm(c.attrib.get("tl"))
        if tl:
            conn_by_tl[tl].append(c)

    edge_fn = {_norm(e.attrib.get("id")): _norm(e.attrib.get("function")) for e in root.findall("edge")}
    map_by_id = {_norm(r.get("crosswalk_id")): r for _, r in mapping.iterrows()}

    p1 = diagnosis[(diagnosis["recovery_priority"].astype(str) == "P1")].copy()
    p1 = p1[p1["crosswalk_id"].astype(str).isin(P1_TARGETS)].copy()

    feasibility_rows: list[dict[str, Any]] = []
    for _, row in p1.sort_values("original_rank").iterrows():
        cid = _norm(row.get("crosswalk_id"))
        m = map_by_id.get(cid, {})
        crossing_edge_id = _norm(m.get("crossing_edge_id"))
        tls_id = _norm(m.get("tls_id_used")) or _norm(m.get("nearest_junction_id"))

        crossing_exists = bool(crossing_edge_id and crossing_edge_id in edge_fn and edge_fn.get(crossing_edge_id) == "crossing")
        tl_exists = tls_id in tl_by_id
        conns = conn_by_tl.get(tls_id, [])
        controlled_links_count = len(conns)

        ped_orig = _int(m.get("ped_link_index"), -1)
        ped_rec = pedestrian_link_indices(BASE_NET, tls_id, crossing_edge_id, xml_root=root) if crossing_exists and tl_exists else []
        ped_recovered = ped_rec[0] if ped_rec else ""
        ped_available = bool(ped_rec)

        route_from_edge, route_to_edge = _route_from_crossing(net, crossing_edge_id) if crossing_exists else ("", "")

        states = [_norm(p.attrib.get("state")) for p in tl_by_id[tls_id].findall("phase")] if tl_exists else []
        ped_before = _ped_only_available(states, ped_rec)

        fail: list[str] = []
        if not crossing_exists:
            fail.append("crossing_not_found")
        if not tl_exists:
            fail.append("tlLogic_not_found")
        if controlled_links_count == 0:
            fail.append("controlled_links_missing")
        if not ped_available:
            fail.append("ped_link_index_recovery_failed")
        if not route_from_edge or not route_to_edge:
            fail.append("route_generation_failed")

        signal_fix_possible = len(fail) == 0
        rec_action = "SIGNAL_PHASE_FIX" if signal_fix_possible else "MANUAL_REVIEW"

        feasibility_rows.append(
            {
                "crosswalk_id": cid,
                "original_rank": _int(row.get("original_rank"), 0),
                "crossing_edge_id": crossing_edge_id,
                "tls_id_used": tls_id,
                "crossing_exists": crossing_exists,
                "tls_connected": tl_exists,
                "controlled_links_count": controlled_links_count,
                "ped_link_index_original": ped_orig if ped_orig >= 0 else "",
                "ped_link_index_recovered": ped_recovered,
                "ped_link_index_available": ped_available,
                "route_from_edge": route_from_edge,
                "route_to_edge": route_to_edge,
                "pedestrian_only_phase_available_before": ped_before,
                "signal_fix_possible": signal_fix_possible,
                "failure_reason": "" if signal_fix_possible else ";".join(fail),
                "recommended_action": rec_action,
            }
        )

    feasibility_df = pd.DataFrame(feasibility_rows).sort_values(["original_rank", "crosswalk_id"])
    feasibility_df.to_csv(out_dir / "remaining_p1_signal_fix_feasibility.csv", index=False)

    tll_file = out_dir / "remaining_p1_signal_fix_net_v1.tll.xml"
    patch_plan = _build_phase_patch(BASE_NET, feasibility_df, tll_file)
    patch_plan.to_csv(out_dir / "remaining_p1_signal_fix_patch_plan.csv", index=False)

    net_file = out_dir / "remaining_p1_signal_fix_net_v1.net.xml"
    net_ok, net_cmd, net_msg = _netconvert_with_tll(BASE_NET, tll_file, net_file, out_dir / "remaining_p1_signal_fix_netconvert.log")

    audit_net_file = net_file if net_ok else BASE_NET
    audit_root = ET.parse(audit_net_file).getroot()
    audit_net = read_net(audit_net_file)
    audit_tl_by_id = {_norm(t.attrib.get("id")): t for t in audit_root.findall("tlLogic") if _norm(t.attrib.get("id"))}

    phase_rows: list[dict[str, Any]] = []
    for _, row in feasibility_df.iterrows():
        cid = _norm(row.get("crosswalk_id"))
        tls_id = _norm(row.get("tls_id_used"))
        crossing = _norm(row.get("crossing_edge_id"))
        ped_idx = _int(row.get("ped_link_index_recovered"), -1)

        ped_indices = pedestrian_link_indices(audit_net_file, tls_id, crossing, xml_root=audit_root) if crossing else []
        if ped_idx >= 0 and ped_idx not in ped_indices:
            ped_indices.append(ped_idx)
        ped_indices = sorted(set(ped_indices))

        states = [_norm(p.attrib.get("state")) for p in audit_tl_by_id[tls_id].findall("phase")] if tls_id in audit_tl_by_id else []
        ped_only_after = _ped_only_available(states, ped_indices)
        r_from, r_to = _route_from_crossing(audit_net, crossing) if crossing else ("", "")

        phase_rows.append(
            {
                "crosswalk_id": cid,
                "tls_id": tls_id,
                "tls_id_used": tls_id,
                "crossing_edge_id": crossing,
                "crossing_id": crossing,
                "crossing_exists": bool(crossing),
                "tls_connected": tls_id in audit_tl_by_id,
                "ped_link_indices": json.dumps(ped_indices, ensure_ascii=False),
                "ped_link_index": ped_indices[0] if ped_indices else "",
                "ped_link_index_available": bool(ped_indices),
                "route_from_edge": r_from,
                "route_to_edge": r_to,
                "pedestrian_only_phase_available": ped_only_after,
                "phase_states": json.dumps(states, ensure_ascii=False),
            }
        )

    phase_df = pd.DataFrame(phase_rows).sort_values(["crosswalk_id"])
    phase_df.to_csv(out_dir / "remaining_p1_signal_fix_phase_audit.csv", index=False)

    cand = phase_df[
        phase_df["crossing_exists"].astype(bool)
        & phase_df["tls_connected"].astype(bool)
        & phase_df["ped_link_index_available"].astype(bool)
        & phase_df["pedestrian_only_phase_available"].astype(bool)
        & phase_df["route_from_edge"].astype(str).str.len().gt(0)
        & phase_df["route_to_edge"].astype(str).str.len().gt(0)
    ].copy()

    if not cand.empty:
        cand["source_crosswalk_id"] = cand["crosswalk_id"]
        cand["canonical_crosswalk_id"] = cand["crosswalk_id"]
        cand["nearest_junction_id"] = cand["tls_id_used"]
        cand["ped_depart_offset_sec"] = 0.0
        cand["ped_repeat_count"] = 5
        cand["ped_repeat_spacing_sec"] = 1.5
        cand["source_file"] = "remaining_p1_signal_fix_candidate_table.csv"
        cand["batch_network_file"] = str(net_file.resolve())
        cand["final_verdict"] = "CANDIDATE_TABLE_READY_PENDING_SEED1_SMOKE"
        cand["step_test_ok"] = True
        cand["controlled_links_count"] = ""
        cand["crossing_inventory_used"] = False
        cand = cand[
            [
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
                "source_file",
                "batch_network_file",
                "final_verdict",
                "step_test_ok",
                "controlled_links_count",
                "crossing_inventory_used",
            ]
        ]

    cand.to_csv(out_dir / "remaining_p1_signal_fix_candidate_table.csv", index=False)

    summary_lines = [
        "# remaining_p1_signal_fix summary",
        "",
        f"- integrity_input_dir: `{integrity_dir}`",
        f"- roadmap_loaded: {bool(roadmap_md)}",
        f"- p1_input_rows: {len(feasibility_df)}",
        f"- signal_fix_possible_rows: {int(feasibility_df['signal_fix_possible'].sum()) if not feasibility_df.empty else 0}",
        f"- patch_plan_rows: {len(patch_plan)}",
        f"- netconvert_ok: {net_ok}",
        f"- candidate_table_rows: {len(cand)}",
        f"- output_net: `{net_file}`",
        f"- netconvert_message: {net_msg}",
    ]
    (out_dir / "remaining_p1_signal_fix_summary.md").write_text("\n".join(summary_lines) + "\n", encoding="utf-8")

    run_smoke_sh = out_dir / "command_to_run_remaining_p1_seed1_smoke.sh"
    run_smoke_sh.write_text(
        f"""#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT=\"$(cd \"$(dirname \"${{BASH_SOURCE[0]}}\")/../../..\" && pwd)\"
cd \"$PROJECT_ROOT\"

if [[ -z \"${{SUMO_HOME:-}}\" ]]; then
  SUMO_HOME=\"$(python3 - <<'PY'
from smart_crosswalk_sumo.network_utils import resolve_sumo_home
print(resolve_sumo_home() or "")
PY
)\"
fi
if [[ -z \"$SUMO_HOME\" ]]; then
  echo \"SUMO_HOME not found\" >&2
  exit 1
fi
export SUMO_HOME
export PATH=\"$SUMO_HOME/bin:$PATH\"
if [[ -z \"${{PROJ_LIB:-}}\" ]]; then
  if [[ -d \"$SUMO_HOME/share/proj\" ]]; then
    export PROJ_LIB=\"$SUMO_HOME/share/proj\"
  elif [[ -d \"$SUMO_HOME/proj\" ]]; then
    export PROJ_LIB=\"$SUMO_HOME/proj\"
  fi
fi
export PYTHONPATH=\"$PROJECT_ROOT:${{PYTHONPATH:-}}\"

CSV=\"{(out_dir / 'remaining_p1_signal_fix_candidate_table.csv').resolve()}\"
NET=\"{net_file.resolve()}\"

echo \"[INFO] manual run only. this can take time.\"
python3 -m smart_crosswalk_sumo.run_phase6_recovery_smoke \\
  --candidate-csv \"$CSV\" \\
  --net-file \"$NET\" \\
  --scenario smart \\
  --seed 1 \\
  --sim-duration 600 \\
  --phase-aligned-ped-depart \\
  --output-dir \"{(out_dir / 'remaining_p1_seed1_smart_smoke').resolve()}\"

python3 -m smart_crosswalk_sumo.run_phase6_recovery_smoke \\
  --candidate-csv \"$CSV\" \\
  --net-file \"$NET\" \\
  --scenario baseline \\
  --seed 1 \\
  --sim-duration 600 \\
  --phase-aligned-ped-depart \\
  --output-dir \"{(out_dir / 'remaining_p1_seed1_baseline_smoke').resolve()}\"
""",
        encoding="utf-8",
    )
    run_smoke_sh.chmod(0o755)

    check_smoke_sh = out_dir / "command_to_check_remaining_p1_seed1_smoke.sh"
check_script = """#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
cd "$PROJECT_ROOT"

python3 - <<'PY'
import pandas as pd
from pathlib import Path

out_dir = Path(r"__OUT_DIR__")
cand = pd.read_csv(out_dir / 'remaining_p1_signal_fix_candidate_table.csv') if (out_dir / 'remaining_p1_signal_fix_candidate_table.csv').exists() else pd.DataFrame(columns=['crosswalk_id'])
smart = out_dir / 'remaining_p1_seed1_smart_smoke' / 'phase6_smoke_summary.csv'
base = out_dir / 'remaining_p1_seed1_baseline_smoke' / 'phase6_smoke_summary.csv'

smart_df = pd.read_csv(smart) if smart.exists() else pd.DataFrame()
base_df = pd.read_csv(base) if base.exists() else pd.DataFrame()

def b(v):
    return str(v).strip().lower() in ('1','true','yes','y')

def i(v,d=0):
    try:
        if pd.isna(v):
            return d
        return int(float(v))
    except Exception:
        return d

rows=[]
for cid in cand['crosswalk_id'].astype(str).tolist():
    s = smart_df[smart_df['crosswalk_id'].astype(str)==cid]
    b0 = base_df[base_df['crosswalk_id'].astype(str)==cid]
    sr = s.iloc[0].to_dict() if not s.empty else {}
    br = b0.iloc[0].to_dict() if not b0.empty else {}

    smart_ok = bool(sr) and b(sr.get('completed')) and i(sr.get('ped_crossing_person_count'))>0 and i(sr.get('extension_count'))>0
    base_ok = bool(br) and b(br.get('completed')) and i(br.get('ped_crossing_person_count'))>0 and i(br.get('extension_count'),-1)==0
    rows.append({
        'crosswalk_id':cid,
        'smart_completed': b(sr.get('completed')) if sr else False,
        'smart_ped_crossing_person_count': i(sr.get('ped_crossing_person_count')) if sr else 0,
        'smart_extension_count': i(sr.get('extension_count')) if sr else 0,
        'smart_smoke_success': smart_ok,
        'baseline_completed': b(br.get('completed')) if br else False,
        'baseline_ped_crossing_person_count': i(br.get('ped_crossing_person_count')) if br else 0,
        'baseline_extension_count': i(br.get('extension_count'),-1) if br else -1,
        'baseline_smoke_success': base_ok,
        'ready_for_30seed': bool(smart_ok and base_ok),
        'reason': 'seed1 smart/baseline passed' if smart_ok and base_ok else 'seed1 not passed',
    })

out = pd.DataFrame(rows)
out.to_csv(out_dir / 'remaining_p1_seed1_smoke_verdict.csv', index=False)
print('verdict_csv=', out_dir / 'remaining_p1_seed1_smoke_verdict.csv')
print('rows=', len(out))
print('ready=', int(out['ready_for_30seed'].sum()) if not out.empty else 0)
PY
"""
    check_smoke_sh.write_text(check_script.replace("__OUT_DIR__", str(out_dir.resolve())), encoding="utf-8")
    check_smoke_sh.chmod(0o755)

    p4 = diagnosis[diagnosis["recovery_priority"].astype(str) == "P4"].copy().sort_values(["original_rank", "crosswalk_id"])
    gen_check_map = {_norm(r.get("crosswalk_id")): r for _, r in gen_check.iterrows()} if not gen_check.empty else {}
    gen_diag_map = {_norm(r.get("crosswalk_id")): r for _, r in gen_diag.iterrows()} if not gen_diag.empty else {}

    p4_rows: list[dict[str, Any]] = []
    p4_map_records: list[dict[str, Any]] = []

    for _, row in p4.iterrows():
        cid = _norm(row.get("crosswalk_id"))
        m = map_by_id.get(cid, {})
        g1 = gen_check_map.get(cid, {})
        g2 = gen_diag_map.get(cid, {})

        generated_lon = _float(g1.get("generated_crossing_center_lon"), _float(g2.get("generated_crossing_center_lon")))
        generated_lat = _float(g1.get("generated_crossing_center_lat"), _float(g2.get("generated_crossing_center_lat")))
        generated_lon = "" if str(generated_lon) == "nan" else round(float(generated_lon), 6)
        generated_lat = "" if str(generated_lat) == "nan" else round(float(generated_lat), 6)

        generated_exists = _bool(g1.get("generated_crossing_exists")) or _bool(g2.get("generated_crossing_exists"))

        failure_category = _norm(row.get("failure_category"))
        strategy = _norm(row.get("recovery_strategy"))

        if strategy == "MANUAL_EDGE_PAIR_SELECTION":
            manual_type = "MANUAL_EDGE_PAIR_SELECTION"
        elif generated_exists:
            manual_type = "GENERATED_CROSSING_RETRY_POSSIBLE"
        elif failure_category == "LOCATION_TOO_FAR":
            manual_type = "LOCATION_TOO_FAR_HOLD"
        else:
            manual_type = "NEW_SIGNAL_INSTALLATION_REVIEW"

        nearest_tls_id = _norm(row.get("nearest_tls_id"))
        tls_lon = ""
        tls_lat = ""
        if nearest_tls_id and net.hasNode(nearest_tls_id):
            x, y = net.getNode(nearest_tls_id).getCoord()
            lon, lat = net.convertXY2LonLat(x, y)
            tls_lon = round(float(lon), 6)
            tls_lat = round(float(lat), 6)

        rec = {
            "crosswalk_id": cid,
            "original_rank": _int(row.get("original_rank"), 0),
            "previous_status": _norm(row.get("previous_status")),
            "final_recovery_status": _norm(row.get("final_recovery_status")),
            "failure_category": failure_category,
            "manual_review_type": manual_type,
            "recommended_action": strategy,
            "mapping_distance_m": _float(row.get("mapping_distance_m")),
            "real_lon": _float(m.get("real_lon")),
            "real_lat": _float(m.get("real_lat")),
            "current_sumo_lon": _float(m.get("sumo_lon")),
            "current_sumo_lat": _float(m.get("sumo_lat")),
            "generated_lon": generated_lon,
            "generated_lat": generated_lat,
            "nearest_tls_id": nearest_tls_id,
            "nearest_tls_distance_m": _float(row.get("nearest_tls_distance_m")),
            "nearest_tls_lon": tls_lon,
            "nearest_tls_lat": tls_lat,
            "reason": _norm(row.get("reason")),
        }
        p4_rows.append(rec)

        p4_map_records.append(
            {
                "crosswalk_id": cid,
                "original_rank": rec["original_rank"],
                "mapping_distance_m": "" if str(rec["mapping_distance_m"]) == "nan" else round(float(rec["mapping_distance_m"]), 3),
                "failure_category": failure_category,
                "manual_review_type": manual_type,
                "recommended_action": strategy,
                "nearest_tls_id": nearest_tls_id,
                "nearest_tls_distance_m": "" if str(rec["nearest_tls_distance_m"]) == "nan" else round(float(rec["nearest_tls_distance_m"]), 3),
                "real_lon": "" if str(rec["real_lon"]) == "nan" else round(float(rec["real_lon"]), 6),
                "real_lat": "" if str(rec["real_lat"]) == "nan" else round(float(rec["real_lat"]), 6),
                "current_sumo_lon": "" if str(rec["current_sumo_lon"]) == "nan" else round(float(rec["current_sumo_lon"]), 6),
                "current_sumo_lat": "" if str(rec["current_sumo_lat"]) == "nan" else round(float(rec["current_sumo_lat"]), 6),
                "generated_lon": generated_lon,
                "generated_lat": generated_lat,
                "nearest_tls_lon": tls_lon,
                "nearest_tls_lat": tls_lat,
            }
        )

    p4_df = pd.DataFrame(p4_rows).sort_values(["original_rank", "crosswalk_id"]) if p4_rows else pd.DataFrame()
    p4_df.to_csv(out_dir / "remaining_p4_manual_review_candidates.csv", index=False)
    _render_p4_map(out_dir / "remaining_p4_manual_review_map_overlay.html", p4_map_records)

    p4_summary = [
        "# remaining_p4_manual_review_summary",
        "",
        f"- total_p4_candidates: {len(p4_df)}",
        "",
        "## manual_review_type counts",
    ]
    if not p4_df.empty:
        for k, v in p4_df["manual_review_type"].value_counts().items():
            p4_summary.append(f"- {k}: {int(v)}")
    (out_dir / "remaining_p4_manual_review_summary.md").write_text("\n".join(p4_summary) + "\n", encoding="utf-8")

    p5 = diagnosis[(diagnosis["recovery_priority"].astype(str) == "P5")].copy()
    p5 = p5[p5["crosswalk_id"].astype(str).isin(P5_TARGETS)].sort_values(["original_rank", "crosswalk_id"])
    p5.to_csv(out_dir / "remaining_p5_reject_candidates.csv", index=False)

    p5_summary = [
        "# remaining_p5_reject_summary",
        "",
        f"- total_p5_candidates: {len(p5)}",
        "- policy: exclude from current step",
        "",
        "## candidates",
    ]
    for _, r in p5.iterrows():
        p5_summary.append(f"- {_norm(r.get('crosswalk_id'))} (rank={_int(r.get('original_rank'), 0)}): {_norm(r.get('failure_category'))}")
    (out_dir / "remaining_p5_reject_summary.md").write_text("\n".join(p5_summary) + "\n", encoding="utf-8")

    ready28 = set(top50_ready["crosswalk_id"].astype(str).str.strip().tolist())
    p1_ids = set(feasibility_df["crosswalk_id"].astype(str).tolist())
    p1_feasible_ids = set(feasibility_df[feasibility_df["signal_fix_possible"].astype(bool)]["crosswalk_id"].astype(str).tolist())
    p5_ids = set(p5["crosswalk_id"].astype(str).tolist())

    p1_verdict = out_dir / "remaining_p1_seed1_smoke_verdict.csv"
    p1_ready_after_seed1: set[str] = set()
    if p1_verdict.exists():
        vdf = pd.read_csv(p1_verdict)
        p1_ready_after_seed1 = set(vdf[vdf["ready_for_30seed"].astype(bool)]["crosswalk_id"].astype(str).tolist())

    pool_rows: list[dict[str, Any]] = []
    for _, r in top50_status.sort_values("original_rank").iterrows():
        cid = _norm(r.get("crosswalk_id"))
        rank = _int(r.get("original_rank"), 0)
        prev = _norm(r.get("final_recovery_status"))

        if cid in ready28:
            status = "READY_EXISTING_28"
            reason = "already ready in current/signal_fix_v1/generated_v1 stage"
        elif cid in p1_ids:
            if cid in p1_ready_after_seed1:
                status = "P1_SIGNAL_FIX_READY"
                reason = "p1 signal_fix seed1 passed"
            elif cid in p1_feasible_ids:
                status = "P1_SIGNAL_FIX_PENDING_SEED1"
                reason = "p1 signal_fix candidate; wait seed1 smoke"
            else:
                status = "P4_MANUAL_REVIEW"
                reason = "p1 target but not feasible in phase-only fix"
        elif cid in p5_ids:
            status = "P5_REJECT"
            reason = "exclude in current step"
        else:
            status = "P4_MANUAL_REVIEW"
            reason = "manual review queue"

        pool_rows.append(
            {
                "crosswalk_id": cid,
                "original_rank": rank,
                "previous_status": prev,
                "next_pool_status": status,
                "reason": reason,
            }
        )

    pool_df = pd.DataFrame(pool_rows).sort_values(["original_rank", "crosswalk_id"])
    pool_df.to_csv(out_dir / "next_integrated_map_candidate_pool.csv", index=False)

    pool_summary = [
        "# next_integrated_map_candidate_pool_summary",
        "",
        "- note: no integrated map generated in this step.",
        f"- pool_rows: {len(pool_df)}",
        "",
        "## status counts",
    ]
    for k, v in pool_df["next_pool_status"].value_counts().items():
        pool_summary.append(f"- {k}: {int(v)}")
    (out_dir / "next_integrated_map_candidate_pool_summary.md").write_text("\n".join(pool_summary) + "\n", encoding="utf-8")

    cmd_file = out_dir / "command_to_run_remaining_p1_signal_fix_generation.sh"
    cmd_file.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        "PROJECT_ROOT=\"$(cd \"$(dirname \"${BASH_SOURCE[0]}\")/../../..\" && pwd)\"\n"
        "cd \"$PROJECT_ROOT\"\n"
        "if [[ -z \"${SUMO_HOME:-}\" ]]; then\n"
        "  SUMO_HOME=\"$(python3 - <<'PY'\n"
        "from smart_crosswalk_sumo.network_utils import resolve_sumo_home\n"
        "print(resolve_sumo_home() or \"\")\n"
        "PY\n"
        ")\"\n"
        "fi\n"
        "if [[ -z \"$SUMO_HOME\" ]]; then\n"
        "  echo \"SUMO_HOME not found\" >&2\n"
        "  exit 1\n"
        "fi\n"
        "export SUMO_HOME\n"
        "export PATH=\"$SUMO_HOME/bin:$PATH\"\n"
        "if [[ -z \"${PROJ_LIB:-}\" ]]; then\n"
        "  if [[ -d \"$SUMO_HOME/share/proj\" ]]; then\n"
        "    export PROJ_LIB=\"$SUMO_HOME/share/proj\"\n"
        "  elif [[ -d \"$SUMO_HOME/proj\" ]]; then\n"
        "    export PROJ_LIB=\"$SUMO_HOME/proj\"\n"
        "  fi\n"
        "fi\n"
        "export PYTHONPATH=\"$PROJECT_ROOT:${PYTHONPATH:-}\"\n"
        f"python3 -m smart_crosswalk_sumo.run_remaining22_p1_signal_fix --integrity-dir {integrity_dir.resolve()}\n",
        encoding="utf-8",
    )
    cmd_file.chmod(0o755)

    print(f"output_dir={out_dir}")
    print(f"p1_rows={len(feasibility_df)}")
    print(f"p1_signal_fix_possible={int(feasibility_df['signal_fix_possible'].sum()) if not feasibility_df.empty else 0}")
    print(f"p1_candidate_table_rows={len(cand)}")
    print(f"p4_rows={len(p4_df)}")
    print(f"p5_rows={len(p5)}")


if __name__ == "__main__":
    main()
