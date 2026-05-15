#!/usr/bin/env python3
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

import pandas as pd

BASE_DIR = Path(__file__).resolve().parents[1]
RESULT_DIR = BASE_DIR / "result"
DEFAULT_RECOVERY_DIR = RESULT_DIR / "phase_next_top50_max_installation_recovery_20260516_002620"
DEFAULT_BASE_NET = RESULT_DIR / "phase_next_recovery_command_plan_20260514_200908" / "batch_03_run" / "recovery_tls_batch_network_v1.net.xml"
DEFAULT_SCREENING_DIR = RESULT_DIR / "phase_next_top50_final_candidate_screening"
DEFAULT_MAPPING_SUMMARY = RESULT_DIR / "phase_next_top50_mapping_visual_audit_20260515_174626" / "03_top50_mapping_distance_summary.csv"
DEFAULT_GENERATED_CHECK = RESULT_DIR / "phase_next_bad22_generated_crossing_patch_20260515_181150" / "generated_crossing_location_check.csv"
DEFAULT_GENERATED_DIAG = RESULT_DIR / "phase_next_generated_ok6_phase6_prep_20260515_182319" / "generated_phase6_candidate_table_diagnostics.csv"


def _safe_bool(v: Any) -> bool:
    if isinstance(v, bool):
        return v
    if pd.isna(v):
        return False
    return str(v).strip().lower() in {"1", "true", "yes", "y"}


def _safe_int(v: Any, default: int = 0) -> int:
    try:
        if pd.isna(v):
            return default
        return int(float(v))
    except Exception:
        return default


def _safe_float(v: Any, default: float = float("nan")) -> float:
    try:
        if pd.isna(v):
            return default
        return float(v)
    except Exception:
        return default


def _norm(v: Any) -> str:
    text = str(v or "").strip()
    if not text or text.lower() in {"nan", "none", "nat"}:
        return ""
    if text.endswith(".0"):
        raw = text[:-2]
        if raw.replace("-", "", 1).isdigit():
            return raw
    return text


def _csv(path: Path) -> pd.DataFrame:
    if not path.exists():
        raise FileNotFoundError(path)
    return pd.read_csv(path)


def _is_internal_edge(edge_id: str) -> bool:
    return edge_id.startswith(":")


@dataclass
class TLInfo:
    phase_states: list[str]
    phase_count: int
    controlled_indices: set[int]
    ped_indices: set[int]
    controlled_links_count: int


@dataclass
class NetAudit:
    net_file: Path
    metrics: dict[str, Any]
    tl_info: dict[str, TLInfo]
    edge_functions: dict[str, str]
    edge_ids: set[str]
    crossing_edges: set[str]
    tls_touching_crossing: dict[str, set[str]]
    crossing_route_pairs: dict[str, tuple[str, str]]


def parse_net_for_audit(net_file: Path) -> NetAudit:
    total_edges = 0
    total_nodes = 0
    total_crossings = 0
    total_walkingareas = 0
    total_tllogic = 0
    total_tls_nodes = 0
    total_connections = 0
    total_controlled_links = 0

    edge_functions: dict[str, str] = {}
    edge_ids: set[str] = set()
    crossing_edges: set[str] = set()

    tls_nodes: set[str] = set()
    tls_conn_count: dict[str, int] = defaultdict(int)
    tls_conn_indices: dict[str, set[int]] = defaultdict(set)
    tls_ped_indices: dict[str, set[int]] = defaultdict(set)
    tls_touching_crossing: dict[str, set[str]] = defaultdict(set)

    tl_states: dict[str, list[str]] = {}

    crossing_in_routes: dict[str, set[str]] = defaultdict(set)
    crossing_out_routes: dict[str, set[str]] = defaultdict(set)

    for _, elem in ET.iterparse(net_file, events=("end",)):
        tag = elem.tag

        if tag == "edge":
            eid = _norm(elem.attrib.get("id"))
            if eid:
                edge_ids.add(eid)
                fn = _norm(elem.attrib.get("function"))
                edge_functions[eid] = fn
                total_edges += 1
                if fn == "crossing":
                    total_crossings += 1
                    crossing_edges.add(eid)
                elif fn == "walkingarea":
                    total_walkingareas += 1

        elif tag == "node":
            total_nodes += 1
            nid = _norm(elem.attrib.get("id"))
            ntype = _norm(elem.attrib.get("type")).lower()
            if nid and "traffic_light" in ntype:
                tls_nodes.add(nid)
                total_tls_nodes += 1

        elif tag == "crossing":
            cid = _norm(elem.attrib.get("id"))
            if cid:
                crossing_edges.add(cid)

        elif tag == "connection":
            total_connections += 1
            tl = _norm(elem.attrib.get("tl"))
            if tl:
                tls_conn_count[tl] += 1
                total_controlled_links += 1
                link_idx = _safe_int(elem.attrib.get("linkIndex"), -1)
                if link_idx >= 0:
                    tls_conn_indices[tl].add(link_idx)

                from_edge = _norm(elem.attrib.get("from"))
                to_edge = _norm(elem.attrib.get("to"))
                from_fn = edge_functions.get(from_edge, "")
                to_fn = edge_functions.get(to_edge, "")
                is_ped_link = from_fn in {"crossing", "walkingarea"} or to_fn in {"crossing", "walkingarea"}
                if is_ped_link and link_idx >= 0:
                    tls_ped_indices[tl].add(link_idx)

                if from_edge in crossing_edges or to_edge in crossing_edges:
                    if from_edge in crossing_edges:
                        tls_touching_crossing[from_edge].add(tl)
                        if to_edge and not _is_internal_edge(to_edge):
                            crossing_out_routes[from_edge].add(to_edge)
                    if to_edge in crossing_edges:
                        tls_touching_crossing[to_edge].add(tl)
                        if from_edge and not _is_internal_edge(from_edge):
                            crossing_in_routes[to_edge].add(from_edge)

                if from_edge in crossing_edges and to_edge and not _is_internal_edge(to_edge):
                    crossing_out_routes[from_edge].add(to_edge)
                if to_edge in crossing_edges and from_edge and not _is_internal_edge(from_edge):
                    crossing_in_routes[to_edge].add(from_edge)

        elif tag == "tlLogic":
            total_tllogic += 1
            tlid = _norm(elem.attrib.get("id"))
            if tlid:
                states = [_norm(phase.attrib.get("state")) for phase in elem.findall("phase")]
                tl_states[tlid] = states

        elem.clear()

    missing_tllogic = set(tls_conn_count.keys()) - set(tl_states.keys())
    orphan_tl = set(tl_states.keys()) - set(tls_conn_count.keys())

    tls_with_no_controlled_links_count = sum(1 for tl in tl_states if tls_conn_count.get(tl, 0) == 0)

    tls_phase_state_length_mismatch_count = 0
    tls_without_green_phase_count = 0
    tls_without_pedestrian_red_count = 0

    tl_info: dict[str, TLInfo] = {}
    for tlid, states in tl_states.items():
        controlled_idx = tls_conn_indices.get(tlid, set())
        ped_idx = tls_ped_indices.get(tlid, set())
        controlled_count = tls_conn_count.get(tlid, 0)

        mismatch = False
        if controlled_count > 0:
            mismatch = any(len(s) != controlled_count for s in states if s)
        if mismatch:
            tls_phase_state_length_mismatch_count += 1

        has_green = any(any(ch in {"g", "G"} for ch in s) for s in states)
        if not has_green:
            tls_without_green_phase_count += 1

        if ped_idx:
            has_ped_red = any(any(i < len(s) and s[i] in {"r", "R"} for i in ped_idx) for s in states)
            if not has_ped_red:
                tls_without_pedestrian_red_count += 1

        tl_info[tlid] = TLInfo(
            phase_states=states,
            phase_count=len(states),
            controlled_indices=controlled_idx,
            ped_indices=ped_idx,
            controlled_links_count=controlled_count,
        )

    crossing_route_pairs: dict[str, tuple[str, str]] = {}
    for cid in crossing_edges:
        ins = sorted(crossing_in_routes.get(cid, set()))
        outs = sorted(crossing_out_routes.get(cid, set()))
        if ins and outs:
            crossing_route_pairs[cid] = (ins[0], outs[0])

    metrics = {
        "total_edges": total_edges,
        "total_nodes": total_nodes,
        "total_crossings": total_crossings,
        "total_walkingareas": total_walkingareas,
        "total_tlLogic": total_tllogic,
        "total_tls_nodes": total_tls_nodes,
        "total_connections": total_connections,
        "total_controlled_links": total_controlled_links,
        "missing_tlLogic_count": len(missing_tllogic),
        "orphan_tls_reference_count": len(orphan_tl),
        "tls_with_no_controlled_links_count": tls_with_no_controlled_links_count,
        "tls_phase_state_length_mismatch_count": tls_phase_state_length_mismatch_count,
        "tls_without_green_phase_count": tls_without_green_phase_count,
        "tls_without_pedestrian_red_count": tls_without_pedestrian_red_count,
    }

    return NetAudit(
        net_file=net_file,
        metrics=metrics,
        tl_info=tl_info,
        edge_functions=edge_functions,
        edge_ids=edge_ids,
        crossing_edges=crossing_edges,
        tls_touching_crossing=tls_touching_crossing,
        crossing_route_pairs=crossing_route_pairs,
    )


def validate_seed1_verdict(verdict_df: pd.DataFrame) -> tuple[pd.DataFrame, int]:
    rows: list[dict[str, Any]] = []
    ready_count = 0
    for _, r in verdict_df.iterrows():
        smart_completed = _safe_bool(r.get("smart_completed"))
        smart_ped = _safe_int(r.get("smart_ped_crossing_person_count"), 0)
        smart_ext = _safe_int(r.get("smart_extension_count"), 0)
        smart_success = _safe_bool(r.get("smart_smoke_success"))

        baseline_completed = _safe_bool(r.get("baseline_completed"))
        baseline_ped = _safe_int(r.get("baseline_ped_crossing_person_count"), 0)
        baseline_ext = _safe_int(r.get("baseline_extension_count"), 0)
        baseline_success = _safe_bool(r.get("baseline_smoke_success"))

        ready = _safe_bool(r.get("ready_for_30seed"))

        checks = {
            "smart_completed": smart_completed,
            "smart_ped_crossing_person_count>0": smart_ped > 0,
            "smart_extension_count>0": smart_ext > 0,
            "smart_smoke_success": smart_success,
            "baseline_completed": baseline_completed,
            "baseline_ped_crossing_person_count>0": baseline_ped > 0,
            "baseline_extension_count==0": baseline_ext == 0,
            "baseline_smoke_success": baseline_success,
            "ready_for_30seed": ready,
        }
        failed = [k for k, ok in checks.items() if not ok]
        if ready:
            ready_count += 1

        rows.append(
            {
                "crosswalk_id": _norm(r.get("crosswalk_id")),
                "smart_completed": smart_completed,
                "smart_ped_crossing_person_count": smart_ped,
                "smart_extension_count": smart_ext,
                "smart_smoke_success": smart_success,
                "baseline_completed": baseline_completed,
                "baseline_ped_crossing_person_count": baseline_ped,
                "baseline_extension_count": baseline_ext,
                "baseline_smoke_success": baseline_success,
                "ready_for_30seed": ready,
                "validation_status": "PASS" if not failed else "FAIL",
                "validation_reason": "all checks passed" if not failed else "failed: " + ", ".join(failed),
            }
        )

    return pd.DataFrame(rows), ready_count


def _vehicle_signature(states: list[str], ped_indices: set[int]) -> tuple[str, ...]:
    sig = []
    for s in states:
        sig.append("".join(ch for i, ch in enumerate(s) if i not in ped_indices))
    return tuple(sig)


def compare_tls(base: NetAudit, patched: NetAudit, candidate_tls: set[str], network_group: str) -> tuple[pd.DataFrame, dict[str, Any]]:
    rows: list[dict[str, Any]] = []

    base_tls = set(base.tl_info.keys())
    patched_tls = set(patched.tl_info.keys())
    all_tls = sorted(base_tls | patched_tls)

    changed = 0
    unchanged = 0
    new_tls = 0
    removed_tls = 0
    changed_candidate = 0
    changed_non_candidate = 0
    high_risk = 0

    for tlid in all_tls:
        b = base.tl_info.get(tlid)
        p = patched.tl_info.get(tlid)
        is_candidate = tlid in candidate_tls

        change_type = "unchanged"
        vehicle_phase_changed = False
        risk_level = "LOW"
        risk_reason = ""

        b_states = b.phase_states if b else []
        p_states = p.phase_states if p else []
        b_ctrl = b.controlled_links_count if b else 0
        p_ctrl = p.controlled_links_count if p else 0

        if b and not p:
            change_type = "removed"
            removed_tls += 1
            changed += 1
        elif not b and p:
            change_type = "new"
            new_tls += 1
            changed += 1
        else:
            b_sig = "|".join(b_states)
            p_sig = "|".join(p_states)
            if b_sig != p_sig or b_ctrl != p_ctrl:
                change_type = "changed"
                changed += 1
                ped_union = (b.ped_indices if b else set()) | (p.ped_indices if p else set())
                vehicle_phase_changed = _vehicle_signature(b_states, ped_union) != _vehicle_signature(p_states, ped_union)
            else:
                unchanged += 1

        if change_type != "unchanged":
            if is_candidate:
                changed_candidate += 1
            else:
                changed_non_candidate += 1

            if network_group == "signal_fix_net_v1" and not is_candidate:
                risk_level = "HIGH_RISK"
                risk_reason = "non-candidate TLS changed in signal_fix net"
            elif vehicle_phase_changed and not is_candidate:
                risk_level = "HIGH_RISK"
                risk_reason = "vehicle phase changed outside candidate TLS"
            elif network_group == "generated_signal_net_v1" and change_type == "new":
                risk_level = "REVIEW_NEEDED"
                risk_reason = "new signal installation requires separate baseline"
            else:
                risk_level = "MEDIUM" if change_type == "changed" else "LOW"
                risk_reason = "candidate-scoped or expected topology change"

        if risk_level == "HIGH_RISK":
            high_risk += 1

        rows.append(
            {
                "network_group": network_group,
                "tls_id": tlid,
                "change_type": change_type,
                "is_candidate_tls": is_candidate,
                "vehicle_phase_changed": vehicle_phase_changed,
                "base_phase_count": len(b_states),
                "patched_phase_count": len(p_states),
                "base_controlled_links": b_ctrl,
                "patched_controlled_links": p_ctrl,
                "risk_level": risk_level,
                "risk_reason": risk_reason,
            }
        )

    summary = {
        "candidate_tls_count": len(candidate_tls),
        "patched_tls_count": len(patched_tls),
        "unchanged_tls_count": unchanged,
        "changed_tls_count": changed,
        "new_tls_count": new_tls,
        "removed_tls_count": removed_tls,
        "changed_candidate_tls_count": changed_candidate,
        "changed_non_candidate_tls_count": changed_non_candidate,
        "high_risk_tls_count": high_risk,
    }
    return pd.DataFrame(rows), summary


def _pedestrian_only_phase_available(tlinfo: TLInfo, ped_idx: int) -> bool:
    if ped_idx < 0 or not tlinfo.phase_states:
        return False
    non_ped_indices = {i for i in tlinfo.controlled_indices if i != ped_idx}
    for state in tlinfo.phase_states:
        if ped_idx >= len(state):
            continue
        ped_green = state[ped_idx] in {"g", "G"}
        if not ped_green:
            continue
        any_vehicle_green = any((i < len(state) and state[i] in {"g", "G"}) for i in non_ped_indices)
        if not any_vehicle_green:
            return True
    return False


def audit_main12_on_net(main12_df: pd.DataFrame, master_df: pd.DataFrame, net: NetAudit, net_group: str) -> tuple[pd.DataFrame, bool]:
    master_map = { _norm(r.get("crosswalk_id")): r for _, r in master_df.iterrows() }

    rows: list[dict[str, Any]] = []
    all_preserved = True

    for _, row in main12_df.iterrows():
        cid = _norm(row.get("crosswalk_id"))
        ref = master_map.get(cid, {})

        crossing_edge_id = _norm(ref.get("crossing_edge_id"))
        tls_id_used = _norm(ref.get("tls_id_used")) or _norm(ref.get("nearest_junction_id"))
        ped_link_index = _safe_int(ref.get("ped_link_index"), -1)

        crossing_exists = crossing_edge_id in net.edge_ids
        tls_exists = tls_id_used in net.tl_info
        tls_connected = crossing_exists and tls_exists and tls_id_used in net.tls_touching_crossing.get(crossing_edge_id, set())

        ped_link_index_available = False
        pedestrian_only_phase_available = False
        if tls_exists:
            tli = net.tl_info[tls_id_used]
            ped_link_index_available = ped_link_index in tli.controlled_indices
            if ped_link_index_available:
                pedestrian_only_phase_available = _pedestrian_only_phase_available(tli, ped_link_index)

        route_from_edge = ""
        route_to_edge = ""
        if crossing_exists and crossing_edge_id in net.crossing_route_pairs:
            route_from_edge, route_to_edge = net.crossing_route_pairs[crossing_edge_id]

        route_ok = bool(route_from_edge and route_to_edge)

        checks = {
            "crossing_exists": crossing_exists,
            "tls_connected": tls_connected,
            "ped_link_index_available": ped_link_index_available,
            "pedestrian_only_phase_available": pedestrian_only_phase_available,
            "route_from_edge": bool(route_from_edge),
            "route_to_edge": bool(route_to_edge),
        }
        failed = [k for k, ok in checks.items() if not ok]

        preserved = not failed
        if not preserved:
            all_preserved = False

        rows.append(
            {
                "crosswalk_id": cid,
                "original_rank": _safe_int(row.get("original_rank"), 0),
                "net_group": net_group,
                "crossing_edge_id": crossing_edge_id,
                "tls_id_used": tls_id_used,
                "crossing_exists": crossing_exists,
                "tls_connected": tls_connected,
                "ped_link_index_available": ped_link_index_available,
                "pedestrian_only_phase_available": pedestrian_only_phase_available,
                "route_from_edge": route_from_edge,
                "route_to_edge": route_to_edge,
                "preservation_status": "PRESERVED" if preserved else "BROKEN",
                "preservation_reason": "all checks passed" if preserved else "failed: " + ", ".join(failed),
                "route_available": route_ok,
            }
        )

    out_df = pd.DataFrame(rows).sort_values(["original_rank", "crosswalk_id"])
    return out_df.drop(columns=["route_available"]), all_preserved


def _failure_classification(row: dict[str, Any]) -> tuple[str, str, str, str]:
    location_grade = _norm(row.get("location_grade")).upper()
    dist = _safe_float(row.get("mapping_distance_m"))

    crossing_exists = _safe_bool(row.get("crossing_exists"))
    tls_connected = _safe_bool(row.get("tls_connected"))
    ped_link = _safe_bool(row.get("ped_link_index_available"))
    ped_only = _safe_bool(row.get("pedestrian_only_phase_available"))
    route_available = _safe_bool(row.get("route_available"))

    generated_crossing_exists = _safe_bool(row.get("generated_crossing_exists"))
    nearest_tls_dist = _safe_float(row.get("nearest_tls_distance_m"))

    final_status = _norm(row.get("final_recovery_status")).upper()
    reason = _norm(row.get("reason")).lower()

    if "NEEDS_MANUAL_REVIEW" in final_status or "manual" in reason or "location review" in reason:
        return "MANUAL_REVIEW_NEEDED", "MANUAL_EDGE_PAIR_SELECTION", "P4", "manual location/network review required"

    if not crossing_exists:
        if not generated_crossing_exists:
            return "GENERATED_CROSSING_NOT_FOUND", "GENERATED_CROSSING_RETRY", "P2", "generated crossing missing in audit"
        return "CROSSING_NOT_FOUND", "GENERATED_CROSSING_RETRY", "P2", "crossing not present in current net"

    if location_grade == "BAD" or (not pd.isna(dist) and dist > 80.0):
        if not pd.isna(dist) and dist > 200.0:
            return "LOCATION_TOO_FAR", "REJECT", "P5", "mapping distance too large for safe automation"
        return "LOCATION_TOO_FAR", "HOLD", "P4", "location quality too weak for immediate recovery"

    if not tls_connected:
        if not pd.isna(nearest_tls_dist) and nearest_tls_dist <= 80.0:
            return "TLS_NOT_CONNECTED", "EXISTING_TLS_RETROFIT", "P2", "nearby TLS exists but not connected"
        return "NEW_SIGNAL_TOO_RISKY", "NEW_SIGNAL_INSTALLATION_REVIEW", "P3", "requires new signal installation"

    if not ped_link:
        return "PED_LINK_INDEX_MISSING", "SIGNAL_PHASE_FIX", "P1", "ped link index missing while crossing/tls exist"

    if not ped_only:
        return "PEDESTRIAN_ONLY_PHASE_MISSING", "SIGNAL_PHASE_FIX", "P1", "pedestrian-only phase not found"

    if not route_available:
        return "ROUTE_GENERATION_FAIL", "MANUAL_EDGE_PAIR_SELECTION", "P2", "route pair not available on crossing"

    if "REJECT" in final_status:
        return "REJECT_LOW_PRIORITY", "REJECT", "P5", "explicitly rejected in final status"

    return "MANUAL_REVIEW_NEEDED", "HOLD", "P4", "needs additional audit evidence"


def build_remaining22_diagnosis(
    status_df: pd.DataFrame,
    ready_df: pd.DataFrame,
    screening_df: pd.DataFrame,
    mapping_df: pd.DataFrame,
    generated_check_df: pd.DataFrame,
    generated_diag_df: pd.DataFrame,
    master_df: pd.DataFrame,
    base_net: NetAudit,
) -> tuple[pd.DataFrame, dict[str, int]]:
    ready_ids = set(ready_df["crosswalk_id"].astype(str).str.strip().tolist())

    map_by_id = { _norm(r.get("crosswalk_id")): r for _, r in mapping_df.iterrows() }
    screening_by_id = { _norm(r.get("crosswalk_id")): r for _, r in screening_df.iterrows() }
    gen_check_by_id = { _norm(r.get("crosswalk_id")): r for _, r in generated_check_df.iterrows() }
    gen_diag_by_id = { _norm(r.get("crosswalk_id")): r for _, r in generated_diag_df.iterrows() }

    master_by_id: dict[str, dict[str, Any]] = {}
    for _, r in master_df.iterrows():
        cid = _norm(r.get("crosswalk_id"))
        if cid and cid not in master_by_id:
            master_by_id[cid] = r.to_dict()

    rows: list[dict[str, Any]] = []

    for _, srow in status_df.iterrows():
        cid = _norm(srow.get("crosswalk_id"))
        if cid in ready_ids:
            continue

        mrow = map_by_id.get(cid, {})
        scrow = screening_by_id.get(cid, {})
        gcheck = gen_check_by_id.get(cid, {})
        gdiag = gen_diag_by_id.get(cid, {})
        master = master_by_id.get(cid, {})

        crossing_edge_id = _norm(master.get("crossing_edge_id")) or _norm(mrow.get("crossing_edge_id"))
        crossing_exists = crossing_edge_id in base_net.edge_ids if crossing_edge_id else _safe_bool(master.get("crossing_exists"))

        tls_id = _norm(master.get("tls_id_used")) or _norm(master.get("nearest_junction_id")) or _norm(mrow.get("nearest_junction_id"))
        tls_connected = _safe_bool(master.get("tls_connected"))
        if crossing_exists and tls_id:
            tls_connected = tls_connected or (tls_id in base_net.tls_touching_crossing.get(crossing_edge_id, set()))

        ped_link_index = _safe_int(master.get("ped_link_index"), -1)
        ped_link_index_available = _safe_bool(master.get("ped_link_index_available"))
        ped_only = _safe_bool(master.get("pedestrian_only_phase_available"))

        if tls_id in base_net.tl_info and ped_link_index >= 0:
            tli = base_net.tl_info[tls_id]
            ped_link_index_available = ped_link_index_available or (ped_link_index in tli.controlled_indices)
            if ped_link_index_available:
                ped_only = ped_only or _pedestrian_only_phase_available(tli, ped_link_index)

        route_available = False
        if crossing_edge_id and crossing_edge_id in base_net.crossing_route_pairs:
            route_available = True

        nearest_tls_id = _norm(gdiag.get("nearest_tls_id")) or _norm(master.get("nearest_junction_id")) or _norm(mrow.get("nearest_junction_id"))
        nearest_tls_dist = _safe_float(gdiag.get("nearest_tls_distance_m"))
        if pd.isna(nearest_tls_dist):
            nearest_tls_dist = _safe_float(master.get("nearest_tls_distance_m"))

        rec = {
            "crosswalk_id": cid,
            "original_rank": _safe_int(srow.get("original_rank"), 0),
            "previous_status": _norm(srow.get("previous_status")),
            "final_recovery_status": _norm(srow.get("final_recovery_status")),
            "location_grade": _norm(mrow.get("distance_grade")) or _norm(scrow.get("distance_grade")),
            "mapping_distance_m": _safe_float(mrow.get("mapping_distance_m"), _safe_float(scrow.get("mapping_distance_m"))),
            "crossing_exists": crossing_exists,
            "tls_connected": tls_connected,
            "ped_link_index_available": ped_link_index_available,
            "pedestrian_only_phase_available": ped_only,
            "route_available": route_available,
            "nearest_tls_id": nearest_tls_id,
            "nearest_tls_distance_m": nearest_tls_dist,
            "generated_crossing_exists": _safe_bool(gcheck.get("generated_crossing_exists")) or _safe_bool(gdiag.get("generated_crossing_exists")),
            "reason": _norm(srow.get("reason")),
        }

        failure_category, strategy, priority, auto_reason = _failure_classification(rec)
        rec["failure_category"] = failure_category
        rec["recovery_strategy"] = strategy
        rec["recovery_priority"] = priority
        rec["reason"] = _norm(rec.get("reason")) or auto_reason

        rows.append(rec)

    out = pd.DataFrame(rows)
    if out.empty:
        return out, {}

    out = out[
        [
            "crosswalk_id",
            "original_rank",
            "previous_status",
            "final_recovery_status",
            "location_grade",
            "mapping_distance_m",
            "crossing_exists",
            "tls_connected",
            "ped_link_index_available",
            "pedestrian_only_phase_available",
            "route_available",
            "nearest_tls_id",
            "nearest_tls_distance_m",
            "failure_category",
            "recovery_strategy",
            "recovery_priority",
            "reason",
        ]
    ].sort_values(["original_rank", "crosswalk_id"])

    priority_counts = Counter(out["recovery_priority"].tolist())
    return out, dict(priority_counts)


def write_roadmap_md(path: Path, diagnosis_df: pd.DataFrame, priority_counts: dict[str, int]) -> None:
    lines: list[str] = []
    lines.append("# Remaining 22 Recovery Roadmap")
    lines.append("")
    lines.append("## Priority Summary")
    for p in ["P1", "P2", "P3", "P4", "P5"]:
        lines.append(f"- {p}: {priority_counts.get(p, 0)}")

    lines.append("")
    lines.append("## Failure Category Summary")
    if diagnosis_df.empty:
        lines.append("- no remaining22 rows found")
    else:
        cat_counts = diagnosis_df["failure_category"].value_counts()
        for cat, cnt in cat_counts.items():
            lines.append(f"- {cat}: {cnt}")

    lines.append("")
    lines.append("## Candidate-by-Candidate Plan")
    if diagnosis_df.empty:
        lines.append("- no rows")
    else:
        for _, r in diagnosis_df.iterrows():
            lines.append(
                f"- {r['crosswalk_id']} (rank={int(r['original_rank'])}, {r['recovery_priority']}): "
                f"{r['failure_category']} -> {r['recovery_strategy']} | {r['reason']}"
            )

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_promotion_md(
    path: Path,
    signal_fix_main12_preserved: bool,
    generated_main12_preserved: bool,
    signal_fix_ready_count: int,
    generated_ready_count: int,
    signal_fix_high_risk: int,
    generated_high_risk: int,
) -> None:
    signal_fix_default_ok = signal_fix_main12_preserved and signal_fix_ready_count > 0 and signal_fix_high_risk == 0
    generated_default_ok = False

    lines = [
        "# Default Network Promotion Decision",
        "",
        "## Decision",
        f"- signal_fix_net_v1 default candidate: {'YES' if signal_fix_default_ok else 'NO'}",
        "- generated_signal_net_v1 default candidate: NO (treat as expansion net due to NEW_SIGNAL_INSTALLATION)",
        "",
        "## Evidence",
        f"- signal_fix main12 preservation: {signal_fix_main12_preserved}",
        f"- generated_signal main12 preservation: {generated_main12_preserved}",
        f"- signal_fix seed1 ready count: {signal_fix_ready_count} (expected 9)",
        f"- generated_signal seed1 ready count: {generated_ready_count} (expected 7)",
        f"- signal_fix HIGH_RISK non-candidate TLS changes: {signal_fix_high_risk}",
        f"- generated_signal HIGH_RISK non-candidate TLS changes: {generated_high_risk}",
        "",
        "## Recommendation",
        "- Keep current net main12 as the primary experiment baseline group.",
        "- Use signal_fix_net_v1 as additive recovery net only if non-candidate TLS risk is zero and main12 remains preserved.",
        "- Keep generated_signal_net_v1 as expansion experiment net, not default main net.",
        "",
        "## Why Baseline Must Be Separate By Net",
        "- Signal plan and controlled-link structure differ by net.",
        "- baseline_extension_count==0 must be verified per net-specific tlLogic.",
        "- Mixing baselines across current/signal_fix/generated nets can bias smart-vs-baseline comparisons.",
        "",
        "## Pending Before 30 Seed",
        "- Confirm network_integrity_comparison.csv and tls_diff_summary.csv show no unacceptable HIGH_RISK on non-candidate TLS.",
        "- Confirm seed1 validation CSVs stay PASS for all recovered candidates.",
        "- Run per-net baseline independently for current, signal_fix, and generated_signal groups.",
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_integrity_summary_md(
    path: Path,
    signal_fix_ready_count: int,
    generated_ready_count: int,
    signal_fix_main12_preserved: bool,
    generated_main12_preserved: bool,
    signal_fix_diff_summary: dict[str, Any],
    generated_diff_summary: dict[str, Any],
    remaining22_count: int,
) -> None:
    lines = [
        "# Signal Integrity Audit Summary",
        "",
        "## Seed1 Validation",
        f"- signal_fix ready_for_30seed True count: {signal_fix_ready_count} (expected 9)",
        f"- generated_signal ready_for_30seed True count: {generated_ready_count} (expected 7)",
        "",
        "## Main12 Preservation",
        f"- signal_fix_net_v1: {'MAIN12_PRESERVED' if signal_fix_main12_preserved else 'DO_NOT_PROMOTE_AS_DEFAULT_NET'}",
        f"- generated_signal_net_v1: {'MAIN12_PRESERVED' if generated_main12_preserved else 'DO_NOT_PROMOTE_AS_DEFAULT_NET'}",
        "",
        "## TLS Diff Risk Snapshot",
        f"- signal_fix changed TLS: {signal_fix_diff_summary.get('changed_tls_count', 0)} (HIGH_RISK={signal_fix_diff_summary.get('high_risk_tls_count', 0)})",
        f"- generated_signal changed TLS: {generated_diff_summary.get('changed_tls_count', 0)} (HIGH_RISK={generated_diff_summary.get('high_risk_tls_count', 0)})",
        "",
        "## Remaining 22",
        f"- candidates requiring further recovery: {remaining22_count}",
        "",
        "## Notes",
        "- This audit reads existing net/xml/csv only. No long SUMO execution is performed by this script.",
        "- generated_signal_net_v1 is treated as NEW_SIGNAL_INSTALLATION expansion net and requires separate baseline.",
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def build_network_integrity_rows(
    base: NetAudit,
    signal_fix: NetAudit,
    generated: NetAudit,
    signal_fix_summary: dict[str, Any],
    generated_summary: dict[str, Any],
    signal_fix_candidate_tls: set[str],
    generated_candidate_tls: set[str],
) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []

    base_row = {
        **base.metrics,
        "candidate_tls_count": len(signal_fix_candidate_tls | generated_candidate_tls),
        "patched_tls_count": len(base.tl_info),
        "unchanged_tls_count": len(base.tl_info),
        "changed_tls_count": 0,
        "new_tls_count": 0,
        "removed_tls_count": 0,
        "net_file": str(base.net_file),
        "network_group": "base_net",
    }
    rows.append(base_row)

    sf_row = {
        **signal_fix.metrics,
        "candidate_tls_count": len(signal_fix_candidate_tls),
        "patched_tls_count": signal_fix_summary.get("patched_tls_count", 0),
        "unchanged_tls_count": signal_fix_summary.get("unchanged_tls_count", 0),
        "changed_tls_count": signal_fix_summary.get("changed_tls_count", 0),
        "new_tls_count": signal_fix_summary.get("new_tls_count", 0),
        "removed_tls_count": signal_fix_summary.get("removed_tls_count", 0),
        "net_file": str(signal_fix.net_file),
        "network_group": "signal_fix_net_v1",
    }
    rows.append(sf_row)

    gen_row = {
        **generated.metrics,
        "candidate_tls_count": len(generated_candidate_tls),
        "patched_tls_count": generated_summary.get("patched_tls_count", 0),
        "unchanged_tls_count": generated_summary.get("unchanged_tls_count", 0),
        "changed_tls_count": generated_summary.get("changed_tls_count", 0),
        "new_tls_count": generated_summary.get("new_tls_count", 0),
        "removed_tls_count": generated_summary.get("removed_tls_count", 0),
        "net_file": str(generated.net_file),
        "network_group": "generated_signal_net_v1",
    }
    rows.append(gen_row)

    cols = [
        "total_edges",
        "total_nodes",
        "total_crossings",
        "total_walkingareas",
        "total_tlLogic",
        "total_tls_nodes",
        "total_connections",
        "total_controlled_links",
        "missing_tlLogic_count",
        "orphan_tls_reference_count",
        "tls_with_no_controlled_links_count",
        "tls_phase_state_length_mismatch_count",
        "tls_without_green_phase_count",
        "tls_without_pedestrian_red_count",
        "candidate_tls_count",
        "patched_tls_count",
        "unchanged_tls_count",
        "changed_tls_count",
        "new_tls_count",
        "removed_tls_count",
        "net_file",
        "network_group",
    ]
    return pd.DataFrame(rows)[cols]


def main() -> None:
    parser = argparse.ArgumentParser(description="Top50 recovery integrity audit (no long SUMO run).")
    parser.add_argument("--recovery-dir", type=Path, default=DEFAULT_RECOVERY_DIR)
    parser.add_argument("--base-net", type=Path, default=DEFAULT_BASE_NET)
    parser.add_argument("--screening-dir", type=Path, default=DEFAULT_SCREENING_DIR)
    parser.add_argument("--mapping-summary", type=Path, default=DEFAULT_MAPPING_SUMMARY)
    parser.add_argument("--generated-check", type=Path, default=DEFAULT_GENERATED_CHECK)
    parser.add_argument("--generated-diag", type=Path, default=DEFAULT_GENERATED_DIAG)
    parser.add_argument("--out-dir", type=Path, default=None)
    args = parser.parse_args()

    recovery_dir = args.recovery_dir
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = args.out_dir or (RESULT_DIR / f"phase_next_top50_recovery_integrity_audit_{ts}")
    out_dir.mkdir(parents=True, exist_ok=True)

    signal_fix_net_file = recovery_dir / "signal_fix_net_v1.net.xml"
    generated_net_file = recovery_dir / "generated_signal_net_v1.net.xml"

    main12_csv = recovery_dir / "main_verified_current_net_12.csv"
    signal_fix_candidate_csv = recovery_dir / "signal_fix_candidate_table.csv"
    generated_candidate_csv = recovery_dir / "generated_signal_candidate_table.csv"
    master_csv = recovery_dir / "top50_max_installation_master.csv"

    signal_fix_verdict_csv = recovery_dir / "signal_fix_seed1_smoke_verdict.csv"
    generated_verdict_csv = recovery_dir / "generated_signal_seed1_smoke_verdict.csv"

    status_csv = recovery_dir / "top50_recovery_status_after_signal_and_crossing_patch.csv"
    ready_csv = recovery_dir / "recovered_ready_for_30seed_candidates.csv"

    screening_csv = args.screening_dir / "top50_signal_and_location_combined_screening.csv"

    # Input load
    main12_df = _csv(main12_csv)
    signal_fix_candidate_df = _csv(signal_fix_candidate_csv)
    generated_candidate_df = _csv(generated_candidate_csv)
    master_df = _csv(master_csv)

    signal_fix_verdict_df = _csv(signal_fix_verdict_csv)
    generated_verdict_df = _csv(generated_verdict_csv)

    status_df = _csv(status_csv)
    ready_df = _csv(ready_csv)

    screening_df = _csv(screening_csv)
    mapping_df = _csv(args.mapping_summary)
    generated_check_df = _csv(args.generated_check)
    generated_diag_df = _csv(args.generated_diag)

    # Seed1 validation
    sf_validation_df, sf_ready_count = validate_seed1_verdict(signal_fix_verdict_df)
    gen_validation_df, gen_ready_count = validate_seed1_verdict(generated_verdict_df)

    sf_validation_out = out_dir / "signal_fix_seed1_smoke_validation_check.csv"
    gen_validation_out = out_dir / "generated_signal_seed1_smoke_validation_check.csv"
    sf_validation_df.to_csv(sf_validation_out, index=False)
    gen_validation_df.to_csv(gen_validation_out, index=False)

    # Network parse
    base_net = parse_net_for_audit(args.base_net)
    signal_fix_net = parse_net_for_audit(signal_fix_net_file)
    generated_net = parse_net_for_audit(generated_net_file)

    signal_fix_candidate_tls = set(signal_fix_candidate_df["tls_id_used"].astype(str).map(_norm).tolist())
    generated_candidate_tls = set(generated_candidate_df["tls_id_used"].astype(str).map(_norm).tolist())

    # TLS diffs
    sf_tls_diff_df, sf_diff_summary = compare_tls(base_net, signal_fix_net, signal_fix_candidate_tls, "signal_fix_net_v1")
    gen_tls_diff_df, gen_diff_summary = compare_tls(base_net, generated_net, generated_candidate_tls, "generated_signal_net_v1")
    tls_detail_df = pd.concat([sf_tls_diff_df, gen_tls_diff_df], ignore_index=True)

    tls_summary_rows = [
        {
            "network_group": "signal_fix_net_v1",
            **sf_diff_summary,
            "high_risk_comment": "candidate 외 TLS 변경은 HIGH_RISK",
        },
        {
            "network_group": "generated_signal_net_v1",
            **gen_diff_summary,
            "high_risk_comment": "new TLS allowed but requires separate baseline",
        },
    ]
    tls_summary_df = pd.DataFrame(tls_summary_rows)

    # Network integrity table
    network_integrity_df = build_network_integrity_rows(
        base_net,
        signal_fix_net,
        generated_net,
        sf_diff_summary,
        gen_diff_summary,
        signal_fix_candidate_tls,
        generated_candidate_tls,
    )

    network_integrity_df.to_csv(out_dir / "network_integrity_comparison.csv", index=False)
    tls_summary_df.to_csv(out_dir / "tls_diff_summary.csv", index=False)
    tls_detail_df.to_csv(out_dir / "tls_changed_detail.csv", index=False)

    # Main12 preservation on patched nets
    main12_signal_fix_df, sf_main12_preserved = audit_main12_on_net(main12_df, master_df, signal_fix_net, "signal_fix_net_v1")
    main12_generated_df, gen_main12_preserved = audit_main12_on_net(main12_df, master_df, generated_net, "generated_signal_net_v1")

    main12_signal_fix_df.to_csv(out_dir / "main12_preservation_on_signal_fix_net.csv", index=False)
    main12_generated_df.to_csv(out_dir / "main12_preservation_on_generated_signal_net.csv", index=False)

    # Remaining22 diagnosis
    remaining22_df, priority_counts = build_remaining22_diagnosis(
        status_df=status_df,
        ready_df=ready_df,
        screening_df=screening_df,
        mapping_df=mapping_df,
        generated_check_df=generated_check_df,
        generated_diag_df=generated_diag_df,
        master_df=master_df,
        base_net=base_net,
    )
    remaining22_df.to_csv(out_dir / "remaining22_recovery_diagnosis.csv", index=False)
    write_roadmap_md(out_dir / "remaining22_recovery_roadmap.md", remaining22_df, priority_counts)

    # Promotion decision + overall summary
    write_promotion_md(
        out_dir / "default_network_promotion_decision.md",
        signal_fix_main12_preserved=sf_main12_preserved,
        generated_main12_preserved=gen_main12_preserved,
        signal_fix_ready_count=sf_ready_count,
        generated_ready_count=gen_ready_count,
        signal_fix_high_risk=sf_diff_summary.get("high_risk_tls_count", 0),
        generated_high_risk=gen_diff_summary.get("high_risk_tls_count", 0),
    )
    write_integrity_summary_md(
        out_dir / "signal_integrity_audit_summary.md",
        signal_fix_ready_count=sf_ready_count,
        generated_ready_count=gen_ready_count,
        signal_fix_main12_preserved=sf_main12_preserved,
        generated_main12_preserved=gen_main12_preserved,
        signal_fix_diff_summary=sf_diff_summary,
        generated_diff_summary=gen_diff_summary,
        remaining22_count=len(remaining22_df),
    )

    print(f"[DONE] integrity audit outputs: {out_dir}")


if __name__ == "__main__":
    main()
