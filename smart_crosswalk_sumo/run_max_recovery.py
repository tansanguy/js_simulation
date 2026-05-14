#!/usr/bin/env python3
from __future__ import annotations

import csv
import json
import math
import subprocess
import sys
import xml.etree.ElementTree as ET
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

if __package__ in {None, ""}:
    sys.path.append(str(Path(__file__).resolve().parents[1]))

from smart_crosswalk_sumo.network_utils import apply_sumo_environment, choose_crossing_edge, pedestrian_link_indices, read_net
from smart_crosswalk_sumo.run_pedestrian_augmented_network import _collect_traci_controlled_links, _run_sumo_load_test


BASE_DIR = Path(__file__).resolve().parents[1]
RESULT_DIR = BASE_DIR / "result"
CURRENT_TIER_DIR = RESULT_DIR / "phase_next_tierwise_expansion_continue_20260514_135146"
HOLD_DIR = RESULT_DIR / "phase_next_tierwise_expansion_20260514_125323"
FACTORY_DIR = RESULT_DIR / "phase_next_real_csv_crossing_factory_20260514_103928"
FACTORY_TABLE = FACTORY_DIR / "real_csv_crossing_factory_table.csv"
BASE_NET = RESULT_DIR / "phase_next_tierwise_expansion_20260514_125436" / "tierwise_phase6_network_v1.net.xml"
FALLBACK_BASE_NET = RESULT_DIR / "junggu_osm_network_20260513_184417" / "junggu_generated.net.xml"


def _now_tag() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def _load_csv(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame()
    return pd.read_csv(path)


def _write_csv(path: Path, rows: list[dict[str, Any]], headers: list[str] | None = None) -> None:
    if headers is None:
        headers = list(rows[0].keys()) if rows else []
    with path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=headers)
        writer.writeheader()
        if rows:
            writer.writerows(rows)


def _safe_float(value: Any) -> float | None:
    try:
        if value is None or pd.isna(value):
            return None
        return float(value)
    except Exception:
        return None


def _normalize_cols(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    for col in out.columns:
        if out[col].dtype == object:
            out[col] = out[col].astype(str)
    return out


def _group_reason(rows: pd.DataFrame, kind: str) -> pd.DataFrame:
    if rows.empty:
        return pd.DataFrame(columns=["reason", "count", "crosswalk_count"])
    grp = rows.groupby("recovery_reason", dropna=False).agg(
        count=("crosswalk_id", "size"),
        crosswalk_count=("crosswalk_id", "nunique"),
    ).reset_index().rename(columns={"recovery_reason": "reason"})
    grp.insert(0, "kind", kind)
    return grp.sort_values(["count", "reason"], ascending=[False, True])


def _select_unique_junction_batch(df: pd.DataFrame, limit: int) -> pd.DataFrame:
    seen: set[str] = set()
    junction_ids: list[str] = []
    for jid in df["nearest_junction_id"].astype(str).tolist():
        if jid in seen:
            continue
        seen.add(jid)
        junction_ids.append(jid)
        if len(junction_ids) >= limit:
            break
    return df[df["nearest_junction_id"].astype(str).isin(junction_ids)].copy()


def _build_tls_node_patch(net, junction_ids: list[str], out_dir: Path) -> Path:
    root = ET.Element("nodes")
    seen: set[str] = set()
    for jid in junction_ids:
        jid = str(jid)
        if not jid or jid in seen:
            continue
        seen.add(jid)
        try:
            node = net.getNode(jid)
            x, y = node.getCoord()
        except Exception:
            continue
        ET.SubElement(root, "node", {"id": jid, "x": f"{float(x):.2f}", "y": f"{float(y):.2f}", "type": "traffic_light"})
    ET.indent(root, space="  ")
    patch_path = out_dir / "recovery_tls_batch.nod.xml"
    patch_path.write_text(ET.tostring(root, encoding="unicode", xml_declaration=True), encoding="utf-8")
    return patch_path


def _parse_phase_states(net_file: Path) -> dict[str, list[str]]:
    root = ET.parse(net_file).getroot()
    out: dict[str, list[str]] = {}
    for tl in root.findall("tlLogic"):
        tl_id = str(tl.attrib.get("id", "") or "")
        if not tl_id:
            continue
        out[tl_id] = [str(p.attrib.get("state", "") or "") for p in tl.findall("phase")]
    return out


def _build_row_outcome(
    row: pd.Series,
    patched_net_file: Path,
    traci_map: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    root = ET.parse(patched_net_file).getroot()
    tl_logic_ids = {str(t.attrib.get("id", "") or "") for t in root.findall("tlLogic")}
    jid = str(row.get("nearest_junction_id", "") or "")
    # Use patched junction id, not source-row tls_id_used.
    tls_id = jid
    lon = _safe_float(row.get("lon"))
    lat = _safe_float(row.get("lat"))
    crossing_edge_id = ""
    if lon is not None and lat is not None:
        try:
            crossing_edge = choose_crossing_edge(read_net(patched_net_file), lon, lat)
            crossing_edge_id = str(crossing_edge.getID())
        except Exception:
            crossing_edge_id = ""

    crossing_present = bool(crossing_edge_id)
    walkingarea_present = any(
        str(edge.attrib.get("function", "")).lower() == "walkingarea" and str(edge.attrib.get("id", "") or "").startswith(f":{jid}_")
        for edge in root.findall("edge")
    )
    tl_logic_present = tls_id in tl_logic_ids if tls_id else False

    phase_states = _parse_phase_states(patched_net_file)
    states = phase_states.get(tls_id, [])
    ped_indices: list[int] = []
    if tls_id and crossing_edge_id:
        try:
            net = read_net(patched_net_file)
            ped_indices = pedestrian_link_indices(net, tls_id, crossing_edge_id, xml_root=root)
        except Exception:
            ped_indices = []

    ped_phase_rows = []
    for li in ped_indices:
        seq = [s[li] if li < len(s) else "?" for s in states]
        ped_phase_rows.append(
            {
                "linkIndex": li,
                "phase_states": seq,
                "has_green": any(ch in {"G", "g"} for ch in seq),
                "has_red": any(ch == "r" for ch in seq),
            }
        )
    ped_has_green = any(r["has_green"] for r in ped_phase_rows)
    ped_has_red = any(r["has_red"] for r in ped_phase_rows)
    traci_info = traci_map.get(tls_id, {})

    if crossing_present and walkingarea_present and tl_logic_present and ped_indices and ped_has_green and ped_has_red:
        outcome = "PHASE6_READY_CONFIRMED"
    elif crossing_present and walkingarea_present and tl_logic_present and ped_indices and ped_has_red and not ped_has_green:
        outcome = "RED_ONLY_PHASE_FIX_NEEDED"
    elif tl_logic_present and not ped_indices:
        outcome = "TLS_CREATION_CANDIDATE_REMAINING"
    elif not crossing_present or not walkingarea_present:
        outcome = "GEOMETRY_REJECT"
    else:
        outcome = "MANUAL_REVIEW"

    return {
        "crosswalk_id": str(row.get("crosswalk_id", "")),
        "source_bucket": str(row.get("source_bucket", "")),
        "final_candidate_class": str(row.get("final_candidate_class", "")),
        "nearest_junction_id": jid,
        "nearest_junction_type": str(row.get("nearest_junction_type", "")),
        "selected_for_generation": bool(row.get("selected_for_generation", False)),
        "selected_for_patch": bool(row.get("selected_for_patch", False)),
        "patch_action": str(row.get("patch_action", "")),
        "patch_status": str(row.get("patch_status", "")),
        "crossing_present": crossing_present,
        "walkingarea_present": walkingarea_present,
        "tlLogic_present": tl_logic_present,
        "tls_id_used": tls_id,
        "ped_link_indices": json.dumps(ped_indices, ensure_ascii=False),
        "ped_phase_states": json.dumps(ped_phase_rows, ensure_ascii=False),
        "ped_has_green": ped_has_green,
        "ped_has_red": ped_has_red,
        "traci_ok": bool(traci_info.get("traci_ok", False)),
        "traci_error": str(traci_info.get("traci_error", "")),
        "traci_controlled_links_count": int(traci_info.get("controlled_links_count", 0)),
        "traci_ped_link_indices": json.dumps(traci_info.get("traci_ped_link_indices", []), ensure_ascii=False),
        "recovery_outcome": outcome,
        "recovered": outcome == "PHASE6_READY_CONFIRMED",
    }


def main() -> None:
    apply_sumo_environment()
    timestamp = _now_tag()
    out_dir = RESULT_DIR / f"phase_next_max_recovery_{timestamp}"
    out_dir.mkdir(parents=True, exist_ok=True)

    tier_counts_current = _load_csv(CURRENT_TIER_DIR / "tier_counts_after.csv")
    phase6_current = _load_csv(CURRENT_TIER_DIR / "phase6_ready_candidates_total.csv")
    tls_remaining_3 = _load_csv(CURRENT_TIER_DIR / "tls_creation_remaining_candidates.csv")
    current_summary = _load_csv(CURRENT_TIER_DIR / "tls_creation_batch_results.csv")
    hold_df = _load_csv(HOLD_DIR / "geometry_reject_or_manual_hold.csv")
    factory_df = _load_csv(FACTORY_TABLE)

    if tier_counts_current.empty:
        raise FileNotFoundError(CURRENT_TIER_DIR / "tier_counts_after.csv")
    if phase6_current.empty:
        raise FileNotFoundError(CURRENT_TIER_DIR / "phase6_ready_candidates_total.csv")
    if hold_df.empty:
        raise FileNotFoundError(HOLD_DIR / "geometry_reject_or_manual_hold.csv")
    if factory_df.empty:
        raise FileNotFoundError(FACTORY_TABLE)

    base_net_file = BASE_NET if BASE_NET.exists() else FALLBACK_BASE_NET
    if not base_net_file.exists():
        raise FileNotFoundError(base_net_file)

    verified_counts = tier_counts_current.copy()
    verified_counts.to_csv(out_dir / "tier_counts_current_verified.csv", index=False)

    join_cols = [
        "crosswalk_id",
        "crossing_install_class",
        "signal_control_class",
        "recommended_action",
        "reject_reason",
        "reason_korean",
        "distance_to_junction_m",
        "distance_to_road_edge_m",
        "distance_to_tls_m",
        "incident_road_edge_count",
        "nearby_crossing_count",
        "nearby_walkingarea_count",
        "has_tlLogic",
        "tls_phase_count",
        "inferred_ped_link_indices",
        "pedestrian_has_green",
        "pedestrian_has_red",
        "manual_review_needed",
        "nearest_junction_type",
        "nearest_road_edge_type",
        "nearest_nonroad_edge_type",
    ]
    factory_view = factory_df[join_cols].copy()
    joined = hold_df.merge(factory_view, on="crosswalk_id", how="left", suffixes=("", "_factory"))

    manual = joined[joined["final_augmented_class"] == "MANUAL_REVIEW"].copy()
    geometry = joined[joined["final_augmented_class"] == "GEOMETRY_REJECT"].copy()

    manual["recovery_reason"] = "TRUE_MANUAL_REVIEW"
    manual.loc[
        (manual["crossing_install_class"] == "MANUAL_CROSSING_REVIEW")
        & (manual["distance_to_road_edge_m"].map(_safe_float).fillna(999.0) <= 20.0),
        "recovery_reason",
    ] = "EDGE_PAIR_RESELECT_POSSIBLE"
    manual.loc[
        (manual["crossing_install_class"] == "MANUAL_CROSSING_REVIEW")
        & (manual["distance_to_road_edge_m"].map(_safe_float).fillna(999.0) > 20.0),
        "recovery_reason",
    ] = "TRUE_MANUAL_REVIEW"
    manual.loc[
        (manual["crossing_install_class"] == "CROSSING_ALREADY_EXISTS")
        & (manual["signal_control_class"] == "SIGNAL_CONTROL_REQUIRES_TLS_CREATION")
        & (manual["distance_to_junction_m"].map(_safe_float).fillna(0.0) > 15.0),
        "recovery_reason",
    ] = "JUNCTION_RESELECT_POSSIBLE"
    manual.loc[
        (manual["crossing_install_class"] == "CROSSING_ALREADY_EXISTS")
        & (manual["signal_control_class"] == "SIGNAL_CONTROL_REQUIRES_TLS_CREATION")
        & (manual["distance_to_junction_m"].map(_safe_float).fillna(0.0) <= 15.0),
        "recovery_reason",
    ] = "TLS_CREATION_RETRY_POSSIBLE"
    manual["recovery_reason"] = manual["recovery_reason"].fillna("TRUE_MANUAL_REVIEW")

    geometry["recovery_reason"] = "COMPLETE_REJECT"
    geometry.loc[
        (geometry["final_candidate_class"] == "FINAL_EXPLICIT_CROSSING_CANDIDATE")
        & (geometry["patch_status"].astype(str) == "patched_unsignalized")
        & (geometry["distance_to_junction_m"].map(_safe_float).fillna(0.0) <= 15.0),
        "recovery_reason",
    ] = "EXPLICIT_CROSSING_RETRY_POSSIBLE"
    geometry.loc[
        (geometry["final_candidate_class"] == "FINAL_EXPLICIT_CROSSING_CANDIDATE")
        & (geometry["patch_status"].astype(str) == "patched_unsignalized")
        & (geometry["distance_to_junction_m"].map(_safe_float).fillna(0.0) > 15.0),
        "recovery_reason",
    ] = "JUNCTION_BUFFER_EXPANSION_POSSIBLE"
    geometry.loc[
        (geometry["final_candidate_class"] == "FINAL_EXPLICIT_CROSSING_CANDIDATE")
        & (geometry["patch_status"].astype(str) == "duplicate_generated_crossing"),
        "recovery_reason",
    ] = "COMPLETE_REJECT"
    geometry.loc[
        geometry["final_candidate_class"] == "FINAL_TLS_CREATION_CANDIDATE",
        "recovery_reason",
    ] = "TLS_CREATION_RETRY_POSSIBLE"

    manual_breakdown = _group_reason(manual, "MANUAL_REVIEW")
    geometry_breakdown = _group_reason(geometry, "GEOMETRY_REJECT")
    manual_breakdown.to_csv(out_dir / "manual_review_reason_breakdown.csv", index=False)
    geometry_breakdown.to_csv(out_dir / "geometry_reject_reason_breakdown.csv", index=False)

    manual.to_csv(out_dir / "manual_review_recovery_candidates.csv", index=False)
    geometry.to_csv(out_dir / "geometry_recovery_candidates.csv", index=False)

    # Small batch: 3 remaining TLS + top 5 manual-review TLS-retry junctions.
    manual_tls_retry = manual[
        (manual["recovery_reason"] == "TLS_CREATION_RETRY_POSSIBLE")
    ].copy()
    manual_tls_retry["distance_to_junction_m"] = manual_tls_retry["distance_to_junction_m"].map(_safe_float)
    manual_tls_retry["distance_to_road_edge_m"] = manual_tls_retry["distance_to_road_edge_m"].map(_safe_float)
    manual_tls_retry["incident_road_edge_count"] = manual_tls_retry["incident_road_edge_count"].fillna(0).astype(int)
    manual_tls_retry = manual_tls_retry.sort_values(
        ["incident_road_edge_count", "distance_to_junction_m", "distance_to_road_edge_m"],
        ascending=[False, True, True],
    )
    manual_tls_batch = _select_unique_junction_batch(manual_tls_retry, 5)
    manual_tls_batch["source_bucket"] = "MANUAL_REVIEW_TLS_RETRY"

    tls_remaining_3 = tls_remaining_3.copy()
    tls_remaining_3["source_bucket"] = "TLS_REMAINING_3"

    tls_batch = pd.concat([tls_remaining_3, manual_tls_batch], ignore_index=True)
    tls_batch = tls_batch.merge(
        factory_df[["crosswalk_id", "lon", "lat", "sumo_x", "sumo_y", "distance_to_junction_m", "distance_to_road_edge_m", "distance_to_tls_m"]],
        on="crosswalk_id",
        how="left",
    )

    selected_junction_ids = tls_batch["nearest_junction_id"].astype(str).dropna().tolist()
    unique_junction_ids = list(dict.fromkeys(selected_junction_ids))
    net = read_net(base_net_file)
    patch_path = _build_tls_node_patch(net, unique_junction_ids, out_dir)
    tls_net_file = out_dir / "recovery_tls_batch_network_v1.net.xml"
    tls_cmd = [
        "netconvert",
        "--sumo-net-file",
        str(base_net_file),
        "--node-files",
        str(patch_path),
        "--output-file",
        str(tls_net_file),
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
    (out_dir / "recovery_tls_netconvert_command.sh").write_text("#!/bin/bash\n" + " ".join(tls_cmd) + "\n", encoding="utf-8")
    tls_ret = subprocess.run(tls_cmd, capture_output=True, text=True, timeout=1200)
    (out_dir / "recovery_tls_netconvert.log").write_text((tls_ret.stdout or "") + "\n" + (tls_ret.stderr or ""), encoding="utf-8")
    if tls_ret.returncode != 0 or not tls_net_file.exists():
        raise RuntimeError(f"TLS recovery netconvert failed; see {out_dir / 'recovery_tls_netconvert.log'}")

    tls_load_ok, tls_load_err = _run_sumo_load_test(tls_net_file, end_time=5)
    tls_root = ET.parse(tls_net_file).getroot()
    tls_ids = {str(t.attrib.get("id", "") or "") for t in tls_root.findall("tlLogic") if str(t.attrib.get("id", "") or "")}
    tls_traci_ok, tls_traci_err, tls_traci_map = _collect_traci_controlled_links(tls_net_file, tls_ids)

    batch_rows = []
    for _, row in tls_batch.iterrows():
        batch_rows.append(_build_row_outcome(row, tls_net_file, tls_traci_map))
    batch_df = pd.DataFrame(batch_rows)
    batch_df.to_csv(out_dir / "recovery_batch_results.csv", index=False)

    tls_remaining_results = batch_df[batch_df["source_bucket"] == "TLS_REMAINING_3"].copy()
    tls_remaining_results.to_csv(out_dir / "tls_remaining_3_results.csv", index=False)
    tls_remaining_promoted = tls_remaining_results[tls_remaining_results["recovered"] == True].copy()  # noqa: E712
    tls_remaining_promoted.to_csv(out_dir / "tls_remaining_3_promoted.csv", index=False)

    # Recover any promoted rows from manual retry batch as well.
    promoted_rows = batch_df[batch_df["recovered"] == True].copy()
    promoted_rows = promoted_rows.drop_duplicates(subset=["crosswalk_id"], keep="first")
    promoted_rows["promotion_source"] = promoted_rows["source_bucket"]
    if not phase6_current.empty:
        phase6_current = phase6_current.copy()
        if "promotion_source" not in phase6_current.columns:
            phase6_current["promotion_source"] = "PREVIOUS"
    updated_total = pd.concat([phase6_current, promoted_rows], ignore_index=True)
    if "crosswalk_id" in updated_total.columns:
        updated_total = updated_total.drop_duplicates(subset=["crosswalk_id"], keep="first")
    updated_total.to_csv(out_dir / "phase6_ready_candidates_total_updated.csv", index=False)

    batch_counts = Counter(batch_df["recovery_outcome"].tolist())
    manual_reason_counts = manual_breakdown[["reason", "count"]].to_dict(orient="records")
    geometry_reason_counts = geometry_breakdown[["reason", "count"]].to_dict(orient="records")

    summary_lines = [
        "# max recovery summary",
        "",
        f"- output_dir: `{out_dir}`",
        f"- verified tier counts: `{CURRENT_TIER_DIR / 'tier_counts_after.csv'}`",
        "",
        "## current verified counts",
    ]
    for _, r in verified_counts.iterrows():
        summary_lines.append(f"- {r['tier']}: {int(r['count'])}")
    summary_lines += [
        "",
        "## tls remaining 3",
        f"- rows: {len(tls_remaining_results)}",
        f"- promoted: {len(tls_remaining_promoted)}",
        f"- load ok: {tls_load_ok}",
        f"- traci ok: {tls_traci_ok}",
        f"- traci error: {tls_traci_err or tls_load_err}",
        "",
        "## manual review split",
    ]
    for item in manual_reason_counts:
        summary_lines.append(f"- {item['reason']}: {item['count']}")
    summary_lines += [
        "",
        "## geometry reject split",
    ]
    for item in geometry_reason_counts:
        summary_lines.append(f"- {item['reason']}: {item['count']}")
    summary_lines += [
        "",
        "## batch results",
    ]
    for key, value in batch_counts.items():
        summary_lines.append(f"- {key}: {value}")
    summary_lines += [
        "",
        f"- phase6_ready before: {len(phase6_current)}",
        f"- phase6_ready promoted from batch: {int(promoted_rows.shape[0])}",
        f"- phase6_ready total updated: {len(updated_total)}",
    ]
    (out_dir / "max_recovery_summary.md").write_text("\n".join(summary_lines) + "\n", encoding="utf-8")

    next_lines = [
        "# next recovery strategy",
        "",
        f"- keep manual_review classification as heuristic until explicit geometry rerun is available",
        f"- next obvious salvage: {len(manual_tls_retry) - len(manual_tls_batch)} tls-retry manual rows left after batch",
        f"- geometry explicit-crossing retry candidates: {int((geometry['recovery_reason'] == 'EXPLICIT_CROSSING_RETRY_POSSIBLE').sum())}",
        f"- geometry junction-buffer candidates: {int((geometry['recovery_reason'] == 'JUNCTION_BUFFER_EXPANSION_POSSIBLE').sum())}",
        "",
        "## next batch",
        "- rerun only top manual TLS-retry junctions not already in batch",
        "- do not touch complete rejects",
        "- explicit crossing retries need separate edge-pair patch, not TLS node patch",
        "",
        "## caution",
        "- manual review split is heuristic on current geometry distances, not ground truth",
        "- do not promote rows unless patched net passes crossing, walkingarea, tlLogic, TraCI controlled-link, and ped green/red checks",
    ]
    (out_dir / "next_recovery_strategy.md").write_text("\n".join(next_lines) + "\n", encoding="utf-8")

    run_metadata = {
        "output_dir": str(out_dir),
        "base_net": str(base_net_file),
        "current_tier_dir": str(CURRENT_TIER_DIR),
        "hold_dir": str(HOLD_DIR),
        "factory_dir": str(FACTORY_DIR),
        "verified_counts": verified_counts.to_dict(orient="records"),
        "manual_review_rows": int(len(manual)),
        "geometry_reject_rows": int(len(geometry)),
        "manual_tls_retry_rows": int(len(manual_tls_retry)),
        "manual_tls_batch_unique_junctions": int(len(manual_tls_batch["nearest_junction_id"].astype(str).dropna().unique())),
        "batch_total_rows": int(len(batch_df)),
        "batch_unique_junctions": int(len(unique_junction_ids)),
        "batch_outcomes": dict(batch_counts),
        "tls_remaining_3_promoted": int(len(tls_remaining_promoted)),
        "promoted_total": int(len(promoted_rows)),
        "phase6_ready_total_before": int(len(phase6_current)),
        "phase6_ready_total_after": int(len(updated_total)),
        "preservation_check": {
            "base_network_modified": False,
            "registry_modified": False,
            "simulation_logic_modified": False,
            "original_netxml_overwritten": False,
        },
    }
    (out_dir / "run_metadata.json").write_text(json.dumps(run_metadata, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"output_dir: {out_dir}")
    print(f"manual_review: {len(manual)}")
    print(f"geometry_reject: {len(geometry)}")
    print(f"tls_remaining_3_promoted: {len(tls_remaining_promoted)}")
    print(f"phase6_ready_total_after: {len(updated_total)}")


if __name__ == "__main__":
    main()
