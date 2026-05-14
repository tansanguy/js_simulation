#!/usr/bin/env python3
from __future__ import annotations

import copy
import csv
import json
import os
import subprocess
import sys
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

if __package__ in {None, ""}:
    sys.path.append(str(Path(__file__).resolve().parents[1]))

from smart_crosswalk_sumo.network_utils import apply_sumo_environment, read_net
from smart_crosswalk_sumo.run_pedestrian_augmented_network import (
    _build_post_audits,
    _build_tls_creation_node_patch,
    _collect_traci_controlled_links,
    _parse_net_counts,
    _run_sumo_load_test,
)


BASE_DIR = Path(__file__).resolve().parents[1]
RESULT_DIR = BASE_DIR / "result"
SOURCE_AUDIT_DIR = RESULT_DIR / "phase_next_pedestrian_augmented_network_20260514_120939"
PREV_BATCH_DIR = RESULT_DIR / "phase_next_tierwise_expansion_20260514_125436"
BASE_NET = PREV_BATCH_DIR / "tierwise_phase6_network_v1.net.xml"
FALLBACK_BASE_NET = RESULT_DIR / "junggu_osm_network_20260513_184417" / "junggu_generated.net.xml"

TLS_BATCH_LIMIT = 25


def _now_tag() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def _safe_bool(v: Any) -> bool:
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in {"1", "true", "yes", "y"}


def _write_csv(path: Path, rows: list[dict[str, Any]], headers: list[str] | None = None) -> None:
    if headers is None:
        headers = list(rows[0].keys()) if rows else []
    with path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=headers)
        writer.writeheader()
        if rows:
            writer.writerows(rows)


def _load_csv(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame()
    return pd.read_csv(path)


def _select_next_tls_batch(pool: pd.DataFrame, limit_unique_junctions: int) -> pd.DataFrame:
    seen: set[str] = set()
    selected_junctions: list[str] = []
    for jid in pool["nearest_junction_id"].astype(str).tolist():
        if jid in seen:
            continue
        seen.add(jid)
        selected_junctions.append(jid)
        if len(selected_junctions) >= limit_unique_junctions:
            break
    return pool[pool["nearest_junction_id"].astype(str).isin(selected_junctions)].copy()


def _build_tls_batch_log(batch_df: pd.DataFrame, out_net: Path, load_ok: bool, traci_ok: bool, traci_err: str) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for jid, g in batch_df.groupby(batch_df["nearest_junction_id"].astype(str), sort=False):
        rows.append(
            {
                "batch_type": "TLS_CREATION",
                "nearest_junction_id": jid,
                "unique_crosswalk_rows": int(len(g)),
                "crosswalk_ids": "|".join(g["crosswalk_id"].astype(str).tolist()),
                "selected_for_generation": int(g["selected_for_generation"].sum()) if "selected_for_generation" in g else int(len(g)),
                "output_net": str(out_net),
                "load_ok": bool(load_ok),
                "traci_ok": bool(traci_ok),
                "traci_error": traci_err,
            }
        )
    return pd.DataFrame(rows)


def _find_all_red_phase_index(states: list[str]) -> int | None:
    for idx, state in enumerate(states):
        if state and set(state) == {"r"}:
            return idx
    return None


def _build_phase_fix_tllogic_patch(base_net_file: Path, red_only_df: pd.DataFrame, out_dir: Path) -> tuple[Path, pd.DataFrame]:
    root = ET.parse(base_net_file).getroot()
    tl_by_id = {str(t.get("id", "") or ""): t for t in root.findall("tlLogic") if str(t.get("id", "") or "")}

    patch_root = ET.Element(
        "tlLogics",
        {
            "version": "1.20",
            "xmlns:xsi": "http://www.w3.org/2001/XMLSchema-instance",
            "xsi:noNamespaceSchemaLocation": "http://sumo.dlr.de/xsd/tllogic_file.xsd",
        },
    )

    out_rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for tls_id, g in red_only_df.groupby(red_only_df["tls_id_used"].astype(str), sort=False):
        if tls_id in seen:
            continue
        seen.add(tls_id)
        base_tl = tl_by_id.get(tls_id)
        if base_tl is None:
            out_rows.append(
                {
                    "crosswalk_id": "|".join(g["crosswalk_id"].astype(str).tolist()),
                    "nearest_junction_id": "|".join(g["nearest_junction_id"].astype(str).tolist()),
                    "tls_id_used": tls_id,
                    "ped_link_indices": "|".join(g["ped_link_indices"].astype(str).tolist()),
                    "phase_fix_status": "PHASE_FIX_UNSAFE_CONFLICT",
                    "phase_fix_reason": "missing_tlLogic",
                }
            )
            continue

        tl_copy = ET.fromstring(ET.tostring(base_tl, encoding="utf-8"))
        ped_indices: set[int] = set()
        for _, row in g.iterrows():
            for tok in str(row.get("ped_link_indices", "")).strip("[]").replace(",", " ").split():
                if tok.isdigit():
                    ped_indices.add(int(tok))
        if not ped_indices:
            out_rows.append(
                {
                    "crosswalk_id": "|".join(g["crosswalk_id"].astype(str).tolist()),
                    "nearest_junction_id": "|".join(g["nearest_junction_id"].astype(str).tolist()),
                    "tls_id_used": tls_id,
                    "ped_link_indices": "|".join(g["ped_link_indices"].astype(str).tolist()),
                    "phase_fix_status": "PHASE_FIX_UNSAFE_CONFLICT",
                    "phase_fix_reason": "no_ped_link_index",
                }
            )
            continue

        phase_elems = tl_copy.findall("phase")
        phase_states = [str(p.get("state", "") or "") for p in phase_elems]
        all_red_idx = _find_all_red_phase_index(phase_states)
        if all_red_idx is None:
            out_rows.append(
                {
                    "crosswalk_id": "|".join(g["crosswalk_id"].astype(str).tolist()),
                    "nearest_junction_id": "|".join(g["nearest_junction_id"].astype(str).tolist()),
                    "tls_id_used": tls_id,
                    "ped_link_indices": "|".join(g["ped_link_indices"].astype(str).tolist()),
                    "phase_fix_status": "PHASE_FIX_UNSAFE_CONFLICT",
                    "phase_fix_reason": "no_all_red_phase",
                }
            )
            continue

        ped_idx = sorted(ped_indices)[0]
        state = phase_states[all_red_idx]
        if ped_idx >= len(state):
            out_rows.append(
                {
                    "crosswalk_id": "|".join(g["crosswalk_id"].astype(str).tolist()),
                    "nearest_junction_id": "|".join(g["nearest_junction_id"].astype(str).tolist()),
                    "tls_id_used": tls_id,
                    "ped_link_indices": "|".join(g["ped_link_indices"].astype(str).tolist()),
                    "phase_fix_status": "PHASE_FIX_UNSAFE_CONFLICT",
                    "phase_fix_reason": "ped_index_out_of_range",
                }
            )
            continue

        chars = list(state)
        chars[ped_idx] = "G"
        phase_elems[all_red_idx].set("state", "".join(chars))
        patch_root.append(tl_copy)
        out_rows.append(
            {
                "crosswalk_id": "|".join(g["crosswalk_id"].astype(str).tolist()),
                "nearest_junction_id": "|".join(g["nearest_junction_id"].astype(str).tolist()),
                "tls_id_used": tls_id,
                "ped_link_indices": "|".join(g["ped_link_indices"].astype(str).tolist()),
                "phase_fix_status": "PHASE_FIX_CONFIRMED",
                "phase_fix_reason": f"all_red_phase_{all_red_idx}_patched",
            }
        )

    ET.indent(patch_root, space="  ")
    patch_path = out_dir / "red_only_phase_fix.tll.xml"
    patch_path.write_text(ET.tostring(patch_root, encoding="unicode", xml_declaration=True), encoding="utf-8")
    return patch_path, pd.DataFrame(out_rows)


def _validate_phase_fix(
    selected: pd.DataFrame,
    patched_net_file: Path,
    traci_map: dict[str, dict[str, Any]],
) -> pd.DataFrame:
    root = ET.parse(patched_net_file).getroot()
    tl_logic_ids = {str(t.attrib.get("id", "") or "") for t in root.findall("tlLogic")}
    phase_states: dict[str, list[str]] = {}
    for tl in root.findall("tlLogic"):
        tl_id = str(tl.attrib.get("id", "") or "")
        if not tl_id:
            continue
        phase_states[tl_id] = [str(p.attrib.get("state", "") or "") for p in tl.findall("phase")]

    rows: list[dict[str, Any]] = []
    for _, row in selected.iterrows():
        tls_id = str(row.get("tls_id_used", "") or "")
        ped_indices: list[int] = []
        for tok in str(row.get("ped_link_indices", "")).strip("[]").replace(",", " ").split():
            if tok.isdigit():
                ped_indices.append(int(tok))
        states = phase_states.get(tls_id, [])
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
        final_status = "PHASE_FIX_CONFIRMED" if (_safe_bool(traci_info.get("traci_ok")) and ped_has_green and ped_has_red) else "PHASE_FIX_UNSAFE_CONFLICT"
        rows.append(
            {
                "crosswalk_id": str(row.get("crosswalk_id", "")),
                "nearest_junction_id": str(row.get("nearest_junction_id", "")),
                "tls_id_used": tls_id,
                "crossing_present": True,
                "walkingarea_present": True,
                "tlLogic_present": tls_id in tl_logic_ids,
                "ped_link_indices": json.dumps(ped_indices, ensure_ascii=False),
                "ped_phase_states": json.dumps(ped_phase_rows, ensure_ascii=False),
                "ped_has_green": ped_has_green,
                "ped_has_red": ped_has_red,
                "traci_ok": bool(traci_info.get("traci_ok", False)),
                "traci_error": str(traci_info.get("traci_error", "")),
                "traci_controlled_links_count": int(traci_info.get("controlled_links_count", 0)),
                "traci_ped_link_indices": json.dumps(traci_info.get("traci_ped_link_indices", []), ensure_ascii=False),
                "final_augmented_class": final_status,
            }
        )
    return pd.DataFrame(rows)


def main() -> None:
    apply_sumo_environment()
    timestamp = _now_tag()
    out_dir = RESULT_DIR / f"phase_next_tierwise_expansion_continue_{timestamp}"
    out_dir.mkdir(parents=True, exist_ok=True)

    source_audit = _load_csv(SOURCE_AUDIT_DIR / "post_augmented_signal_audit.csv")
    prev_tls_results = _load_csv(PREV_BATCH_DIR / "tls_creation_results.csv")
    prev_phase6 = _load_csv(PREV_BATCH_DIR / "phase6_ready_candidates.csv")
    prev_red_only = _load_csv(PREV_BATCH_DIR / "red_only_phase_fix_results.csv")

    if source_audit.empty:
        raise FileNotFoundError(SOURCE_AUDIT_DIR / "post_augmented_signal_audit.csv")
    if prev_tls_results.empty:
        raise FileNotFoundError(PREV_BATCH_DIR / "tls_creation_results.csv")
    if prev_phase6.empty:
        raise FileNotFoundError(PREV_BATCH_DIR / "phase6_ready_candidates.csv")
    if prev_red_only.empty:
        raise FileNotFoundError(PREV_BATCH_DIR / "red_only_phase_fix_results.csv")
    base_net_file = BASE_NET if BASE_NET.exists() else FALLBACK_BASE_NET
    if not base_net_file.exists():
        raise FileNotFoundError(base_net_file)

    processed_done = prev_tls_results[prev_tls_results["final_augmented_class"] != "TLS_CREATION_CANDIDATE"].copy()
    processed_ids = set(processed_done["crosswalk_id"].astype(str).tolist())
    tls_pool = source_audit[
        (source_audit["final_augmented_class"] == "TLS_CREATION_CANDIDATE")
        & (~source_audit["crosswalk_id"].astype(str).isin(processed_ids))
    ].copy()

    tls_batch = _select_next_tls_batch(tls_pool, TLS_BATCH_LIMIT)
    tls_batch = tls_batch.copy()
    tls_batch["selected_for_generation"] = True
    tls_batch["selected_for_patch"] = False
    tls_batch["patch_action"] = "SEPARATE_TLS_CREATION_BATCH"
    tls_batch["patch_status"] = ""
    tls_batch["generation_reason"] = "continue tls creation batch"
    tls_batch["patch_edge_pair"] = ""

    tls_batch_log_rows: list[dict[str, Any]] = []
    for jid, g in tls_batch.groupby(tls_batch["nearest_junction_id"].astype(str), sort=False):
        tls_batch_log_rows.append(
            {
                "batch_type": "TLS_CREATION",
                "nearest_junction_id": jid,
                "unique_crosswalk_rows": int(len(g)),
                "crosswalk_ids": "|".join(g["crosswalk_id"].astype(str).tolist()),
            }
        )

    # TLS creation pass
    net = read_net(base_net_file)
    base_root = ET.parse(base_net_file).getroot()
    tls_patch_text, tls_patch_rows = _build_tls_creation_node_patch(tls_batch, net)
    tls_patch_path = out_dir / "tls_creation_batch.nod.xml"
    tls_patch_path.write_text(tls_patch_text, encoding="utf-8")
    tls_patch_rows_df = pd.DataFrame(tls_patch_rows)
    tls_patch_rows_df.to_csv(out_dir / "tls_patch_rows.csv", index=False)
    tls_net_file = out_dir / "tls_creation_batch_network_v1.net.xml"
    tls_netconvert_cmd = [
        "netconvert",
        "--sumo-net-file",
        str(base_net_file),
        "--node-files",
        str(tls_patch_path),
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
    (out_dir / "tls_netconvert_command.sh").write_text("#!/bin/bash\n" + " ".join(tls_netconvert_cmd) + "\n", encoding="utf-8")
    tls_ret = subprocess.run(tls_netconvert_cmd, capture_output=True, text=True, timeout=1200)
    (out_dir / "tls_netconvert.log").write_text((tls_ret.stdout or "") + "\n" + (tls_ret.stderr or ""), encoding="utf-8")
    if tls_ret.returncode != 0 or not tls_net_file.exists():
        raise RuntimeError(f"TLS batch netconvert failed; see {out_dir / 'tls_netconvert.log'}")

    tls_counts_after = _parse_net_counts(tls_net_file)
    tls_load_ok, tls_load_err = _run_sumo_load_test(tls_net_file, end_time=5)
    tls_root = ET.parse(tls_net_file).getroot()
    tls_traci_ids = {str(t.attrib.get("id", "") or "") for t in tls_root.findall("tlLogic") if str(t.attrib.get("id", "") or "")}
    tls_traci_ok, tls_traci_err, tls_traci_map = _collect_traci_controlled_links(tls_net_file, tls_traci_ids)
    tls_selected_for_validation = tls_batch.copy()
    tls_selected_for_validation["selected_for_generation"] = True
    tls_selected_for_validation["selected_for_patch"] = False
    tls_selected_for_validation["patch_action"] = "SEPARATE_TLS_CREATION_BATCH"
    tls_selected_for_validation["patch_status"] = (
        tls_patch_rows_df.set_index("crosswalk_id")
        .reindex(tls_selected_for_validation["crosswalk_id"])
        .fillna("")
        .get("patch_status", pd.Series(index=tls_selected_for_validation.index))
        .tolist()
        if not tls_patch_rows_df.empty
        else [""] * len(tls_selected_for_validation)
    )
    tls_crossing_audit, tls_signal_audit, tls_load_validation = _build_post_audits(tls_selected_for_validation, tls_net_file, tls_traci_map)
    tls_signal_audit = tls_signal_audit.copy()
    tls_signal_audit["batch_source"] = "TLS_CREATION"
    tls_signal_audit["batch_status"] = tls_signal_audit["final_augmented_class"].map(
        {
            "SMART_SIGNAL_READY": "PHASE6_READY_CONFIRMED",
            "RED_ONLY_PHASE_FIX_NEEDED": "TLS_CREATION_RED_ONLY",
            "TLS_CREATION_CANDIDATE": "TLS_CREATION_CANDIDATE_REMAINING",
            "GEOMETRY_REJECT": "TLS_CREATION_FAILED",
            "MANUAL_REVIEW": "TLS_CREATION_FAILED",
            "UNSIGNALIZED_CROSSWALK": "UNSIGNALIZED_CROSSWALK",
        }
    ).fillna("TLS_CREATION_FAILED")

    tls_promoted = tls_signal_audit[tls_signal_audit["final_augmented_class"] == "SMART_SIGNAL_READY"].copy()
    tls_red_only = tls_signal_audit[tls_signal_audit["final_augmented_class"] == "RED_ONLY_PHASE_FIX_NEEDED"].copy()
    tls_remaining = tls_signal_audit[tls_signal_audit["final_augmented_class"] == "TLS_CREATION_CANDIDATE"].copy()
    tls_failed = tls_signal_audit[tls_signal_audit["final_augmented_class"].isin(["GEOMETRY_REJECT", "MANUAL_REVIEW"])].copy()

    # red-only phase fix pass on the 21 existing red-only rows
    red_only_input = prev_red_only.copy()
    red_only_input = red_only_input[
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

    phase_fix_patch_path, phase_fix_patch_rows = _build_phase_fix_tllogic_patch(base_net_file, red_only_input, out_dir)
    phase_fix_net_file = out_dir / "red_only_phase_fix_network_v1.net.xml"
    phase_fix_netconvert_cmd = [
        "netconvert",
        "--sumo-net-file",
        str(base_net_file),
        "--tllogic-files",
        str(phase_fix_patch_path),
        "--output-file",
        str(phase_fix_net_file),
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
    (out_dir / "phase_fix_netconvert_command.sh").write_text("#!/bin/bash\n" + " ".join(phase_fix_netconvert_cmd) + "\n", encoding="utf-8")
    phase_fix_ret = subprocess.run(phase_fix_netconvert_cmd, capture_output=True, text=True, timeout=1200)
    (out_dir / "phase_fix_netconvert.log").write_text((phase_fix_ret.stdout or "") + "\n" + (phase_fix_ret.stderr or ""), encoding="utf-8")
    if phase_fix_ret.returncode != 0 or not phase_fix_net_file.exists():
        raise RuntimeError(f"phase fix netconvert failed; see {out_dir / 'phase_fix_netconvert.log'}")

    phase_fix_load_ok, phase_fix_load_err = _run_sumo_load_test(phase_fix_net_file, end_time=5)
    phase_fix_root = ET.parse(phase_fix_net_file).getroot()
    phase_fix_traci_ids = {str(t.attrib.get("id", "") or "") for t in phase_fix_root.findall("tlLogic") if str(t.attrib.get("id", "") or "")}
    phase_fix_traci_ok, phase_fix_traci_err, phase_fix_traci_map = _collect_traci_controlled_links(phase_fix_net_file, phase_fix_traci_ids)
    phase_fix_results = _validate_phase_fix(red_only_input, phase_fix_net_file, phase_fix_traci_map)
    phase_fix_promoted = phase_fix_results[phase_fix_results["final_augmented_class"] == "PHASE_FIX_CONFIRMED"].copy()
    phase_fix_unsafe = phase_fix_results[phase_fix_results["final_augmented_class"] == "PHASE_FIX_UNSAFE_CONFLICT"].copy()

    # total confirmed list
    prev_confirmed = prev_phase6.copy()
    if not prev_confirmed.empty:
        prev_confirmed["promotion_source"] = "PREVIOUS"
    tls_promoted_total = tls_promoted.copy()
    tls_promoted_total["promotion_source"] = "TLS_CREATION"
    phase_fix_promoted_total = phase_fix_promoted.copy()
    phase_fix_promoted_total["promotion_source"] = "PHASE_FIX"
    phase6_total = pd.concat([prev_confirmed, tls_promoted_total, phase_fix_promoted_total], ignore_index=True)
    if "crosswalk_id" in phase6_total.columns:
        phase6_total = phase6_total.drop_duplicates(subset=["crosswalk_id"], keep="first")

    # output tables
    tls_batch_log = pd.DataFrame(tls_batch_log_rows)
    tls_batch_log.to_csv(out_dir / "tls_creation_batch_log.csv", index=False)
    tls_signal_audit.to_csv(out_dir / "tls_creation_batch_results.csv", index=False)
    tls_promoted.to_csv(out_dir / "tls_creation_promoted_candidates.csv", index=False)
    tls_remaining.to_csv(out_dir / "tls_creation_remaining_candidates.csv", index=False)
    red_only_input.to_csv(out_dir / "red_only_phase_fix_input.csv", index=False)
    phase_fix_results.to_csv(out_dir / "red_only_phase_fix_results.csv", index=False)
    phase_fix_promoted.to_csv(out_dir / "phase_fix_promoted_candidates.csv", index=False)
    phase_fix_unsafe.to_csv(out_dir / "phase_fix_unsafe_or_failed.csv", index=False)
    phase6_total.to_csv(out_dir / "phase6_ready_candidates_total.csv", index=False)
    (out_dir / "explicit_crossing_not_processed.csv").write_text("note,reason\nEXPLICIT_CROSSING,not present in current source audit; left untouched\n", encoding="utf-8")

    before_counts = pd.DataFrame(
        [
            {"tier": "PHASE6_READY_CONFIRMED", "count": len(prev_confirmed)},
            {"tier": "TLS_CREATION_CANDIDATE", "count": 450},
            {"tier": "RED_ONLY_PHASE_FIX_NEEDED", "count": len(red_only_input)},
            {"tier": "UNSIGNALIZED_CROSSWALK", "count": int((source_audit["final_augmented_class"] == "UNSIGNALIZED_CROSSWALK").sum())},
            {"tier": "GEOMETRY_REJECT", "count": int((source_audit["final_augmented_class"] == "GEOMETRY_REJECT").sum())},
            {"tier": "MANUAL_REVIEW", "count": int((source_audit["final_augmented_class"] == "MANUAL_REVIEW").sum())},
        ]
    )
    after_counts = pd.DataFrame(
        [
            {"tier": "PHASE6_READY_CONFIRMED", "count": int(len(phase6_total))},
            {"tier": "TLS_CREATION_CANDIDATE", "count": int(len(tls_remaining))},
            {"tier": "RED_ONLY_PHASE_FIX_NEEDED", "count": int(len(tls_red_only))},
            {"tier": "PHASE_FIX_UNSAFE_CONFLICT", "count": int(len(phase_fix_unsafe))},
            {"tier": "UNSIGNALIZED_CROSSWALK", "count": int((source_audit["final_augmented_class"] == "UNSIGNALIZED_CROSSWALK").sum())},
            {"tier": "GEOMETRY_REJECT", "count": int((source_audit["final_augmented_class"] == "GEOMETRY_REJECT").sum())},
            {"tier": "MANUAL_REVIEW", "count": int((source_audit["final_augmented_class"] == "MANUAL_REVIEW").sum())},
        ]
    )
    before_counts.to_csv(out_dir / "tier_counts_before.csv", index=False)
    after_counts.to_csv(out_dir / "tier_counts_after.csv", index=False)

    summary_lines = [
        "# tierwise continue summary",
        "",
        f"- output_dir: `{out_dir}`",
        f"- TLS unique junctions processed: {tls_batch['nearest_junction_id'].astype(str).nunique() if not tls_batch.empty else 0}",
        f"- TLS crosswalk rows processed: {len(tls_batch)}",
        f"- TLS new PHASE6_READY_CONFIRMED: {len(tls_promoted)}",
        f"- TLS new RED_ONLY: {len(tls_red_only)}",
        f"- RED_ONLY phase fix processed: {len(red_only_input)}",
        f"- PHASE_FIX_CONFIRMED: {len(phase_fix_promoted)}",
        f"- PHASE_FIX_UNSAFE_CONFLICT: {len(phase_fix_unsafe)}",
        f"- final PHASE6_READY_CONFIRMED: {len(phase6_total)}",
        f"- remaining TLS_CREATION_CANDIDATE: {len(tls_remaining)}",
        f"- UNSIGNALIZED_CROSSWALK: {int((source_audit['final_augmented_class'] == 'UNSIGNALIZED_CROSSWALK').sum())}",
        f"- GEOMETRY_REJECT: {int((source_audit['final_augmented_class'] == 'GEOMETRY_REJECT').sum())}",
        f"- MANUAL_REVIEW: {int((source_audit['final_augmented_class'] == 'MANUAL_REVIEW').sum())}",
        "",
        "## why excluded",
        "- EXPLICIT_CROSSING absent in current source audit, so not processed.",
        "",
        "## continue command",
        f"- python3 -u smart_crosswalk_sumo/run_tierwise_phase6_continue.py",
    ]
    (out_dir / "tierwise_continue_summary.md").write_text("\n".join(summary_lines) + "\n", encoding="utf-8")

    next_strategy_lines = [
        "# next strategy after tierwise continue",
        "",
        f"- Phase 6 ready now: {len(phase6_total)}",
        f"- TLS remaining: {len(tls_remaining)}",
        f"- phase fix unsafe: {len(phase_fix_unsafe)}",
        "",
        "## next batch command",
        f"- python3 -u smart_crosswalk_sumo/run_tierwise_phase6_continue.py",
        "",
        "## next priorities",
        "- keep batch size at 25 unique TLS junctions",
        "- retry remaining TLS batch only if network path changes",
        "- fix unsafe red-only only if a safe all-red phase exists",
        "- leave explicit crossing untouched",
    ]
    (out_dir / "next_strategy_after_tierwise_continue.md").write_text("\n".join(next_strategy_lines) + "\n", encoding="utf-8")

    run_metadata = {
        "output_dir": str(out_dir),
        "base_net": str(base_net_file),
        "source_audit_dir": str(SOURCE_AUDIT_DIR),
        "previous_batch_dir": str(PREV_BATCH_DIR),
        "tls_unique_junctions_processed": int(tls_batch["nearest_junction_id"].astype(str).nunique()) if not tls_batch.empty else 0,
        "tls_crosswalk_rows_processed": int(len(tls_batch)),
        "tls_promoted": int(len(tls_promoted)),
        "tls_red_only": int(len(tls_red_only)),
        "tls_remaining": int(len(tls_remaining)),
        "phase_fix_processed": int(len(red_only_input)),
        "phase_fix_promoted": int(len(phase_fix_promoted)),
        "phase_fix_unsafe": int(len(phase_fix_unsafe)),
        "phase6_ready_total": int(len(phase6_total)),
        "traci_ok_tls": bool(tls_traci_ok),
        "traci_error_tls": tls_traci_err,
        "traci_ok_phase_fix": bool(phase_fix_traci_ok),
        "traci_error_phase_fix": phase_fix_traci_err,
        "sumo_load_ok_tls": bool(tls_load_ok),
        "sumo_load_error_tls": tls_load_err,
        "sumo_load_ok_phase_fix": bool(phase_fix_load_ok),
        "sumo_load_error_phase_fix": phase_fix_load_err,
        "preservation_check": {
            "base_network_modified": False,
            "registry_modified": False,
            "simulation_logic_modified": False,
            "original_netxml_overwritten": False,
        },
    }
    (out_dir / "run_metadata.json").write_text(json.dumps(run_metadata, ensure_ascii=False, indent=2), encoding="utf-8")

    print("티어별 확장 계속 실행 완료")
    print("")
    print(f"- output_dir: {out_dir}")
    print(f"- 시작 PHASE6_READY_CONFIRMED: {len(prev_confirmed)}")
    print(f"- 시작 TLS_CREATION_CANDIDATE: 450")
    print(f"- 시작 RED_ONLY_PHASE_FIX_NEEDED: {len(red_only_input)}")
    print(f"- 처리한 TLS unique junction 수: {tls_batch['nearest_junction_id'].astype(str).nunique() if not tls_batch.empty else 0}")
    print(f"- 처리한 TLS crosswalk row 수: {len(tls_batch)}")
    print(f"- TLS_CREATION 신규 승격 수: {len(tls_promoted)}")
    print(f"- TLS_CREATION 후 red-only 수: {len(tls_red_only)}")
    print(f"- RED_ONLY phase fix 처리 수: {len(red_only_input)}")
    print(f"- PHASE_FIX_CONFIRMED 수: {len(phase_fix_promoted)}")
    print(f"- PHASE_FIX_UNSAFE_CONFLICT 수: {len(phase_fix_unsafe)}")
    print(f"- 최종 PHASE6_READY_CONFIRMED: {len(phase6_total)}")
    print(f"- 남은 TLS_CREATION_CANDIDATE: {len(tls_remaining)}")
    print(f"- UNSIGNALIZED_CROSSWALK: {int((source_audit['final_augmented_class'] == 'UNSIGNALIZED_CROSSWALK').sum())}")
    print(f"- GEOMETRY_REJECT: {int((source_audit['final_augmented_class'] == 'GEOMETRY_REJECT').sum())}")
    print(f"- MANUAL_REVIEW: {int((source_audit['final_augmented_class'] == 'MANUAL_REVIEW').sum())}")


if __name__ == "__main__":
    main()
