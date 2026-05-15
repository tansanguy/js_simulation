#!/usr/bin/env python3
from __future__ import annotations

import json
import math
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path
from typing import Any

import pandas as pd

from smart_crosswalk_sumo.network_utils import (
    choose_crossing_edge,
    discover_network_metadata_from_net,
    find_tls_id,
    pedestrian_link_indices,
    pedestrian_route_from_crossing,
    read_net,
)
from smart_crosswalk_sumo.run_phase6_recovery_smoke import _route_pair_from_crossing_edge, _validate_route_pair


BASE_DIR = Path(__file__).resolve().parents[1]
RESULT_DIR = BASE_DIR / "result"
STEPWISE_50M = BASE_DIR / "smart_crosswalk_sumo" / "data" / "crosswalk_stepwise_result_50m.csv"
FINAL_237 = RESULT_DIR / "phase_next_recovery_command_plan_20260514_200908" / "phase6_ready_candidates_total_FINAL_237.csv"
INTEGRITY_237 = RESULT_DIR / "phase6_transition_after_recovery_20260514_220549" / "phase6_ready_candidates_total_FINAL_237_integrity_check.csv"
TOP33_PED_ONLY = RESULT_DIR / "phase_next_top33_recovery_20260515_155623" / "top33_phase6_pedestrian_only_candidates.csv"
NET_FILE = RESULT_DIR / "phase_next_recovery_command_plan_20260514_200908" / "batch_03_run" / "recovery_tls_batch_network_v1.net.xml"
OUT_DIR = RESULT_DIR / "phase_next_top50_tiered_recovery_audit"


def _normalize_id(value: Any) -> str:
    text = str(value or "").strip()
    if not text or text in {"nan", "None"}:
        return ""
    if text.endswith(".0"):
        raw = text[:-2]
        if raw.replace("-", "", 1).isdigit():
            return raw
    return text


def _canonical_id(value: Any) -> str:
    text = _normalize_id(value)
    if text.startswith("NODE_") or text.startswith("LINK_"):
        return _canonical_id(text.split("_", 1)[1])
    digits = "".join(ch for ch in text if ch.isdigit())
    return str(int(digits)) if digits else text


def _load_csv(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame()
    return pd.read_csv(path)


def _safe_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if pd.isna(value):
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "y"}


def _parse_int_list(value: Any) -> list[int]:
    if value is None:
        return []
    if isinstance(value, list):
        out: list[int] = []
        for item in value:
            try:
                out.append(int(float(str(item).strip())))
            except Exception:
                continue
        return sorted(set(out))
    text = str(value).strip()
    if not text or text in {"nan", "None", "[]"}:
        return []
    if text.startswith("[") and text.endswith("]"):
        text = text[1:-1]
    text = text.replace(",", " ").replace("|", " ")
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


def _phase_flags(phase_states: list[str], ped_indices: list[int]) -> dict[str, bool]:
    ped_has_green = False
    ped_has_red = False
    pedestrian_only = False
    mixed = False
    if not phase_states or not ped_indices:
        return {
            "ped_has_green": False,
            "ped_has_red": False,
            "pedestrian_only_phase_available": False,
            "mixed_phase_only": False,
        }
    for state in phase_states:
        if any(idx < len(state) and state[idx] in {"G", "g"} for idx in ped_indices):
            ped_has_green = True
            non_ped_green = any(
                idx < len(state) and state[idx] in {"G", "g"} and idx not in ped_indices
                for idx in range(len(state))
            )
            if non_ped_green:
                mixed = True
            else:
                pedestrian_only = True
        if any(idx < len(state) and state[idx] in {"r", "R"} for idx in ped_indices):
            ped_has_red = True
    return {
        "ped_has_green": ped_has_green,
        "ped_has_red": ped_has_red,
        "pedestrian_only_phase_available": pedestrian_only,
        "mixed_phase_only": mixed and not pedestrian_only,
    }


def _make_lookup(df: pd.DataFrame) -> dict[str, dict[str, Any]]:
    lookup: dict[str, dict[str, Any]] = {}
    if df.empty or "crosswalk_id" not in df.columns:
        return lookup
    for _, row in df.iterrows():
        rec = row.to_dict()
        keys = {
            _normalize_id(rec.get("crosswalk_id")),
            _canonical_id(rec.get("crosswalk_id")),
            _normalize_id(rec.get("source_crosswalk_id")),
            _canonical_id(rec.get("source_crosswalk_id")),
            _normalize_id(rec.get("canonical_crosswalk_id")),
            _canonical_id(rec.get("canonical_crosswalk_id")),
        }
        for key in {k for k in keys if k}:
            lookup.setdefault(key, rec)
    return lookup


def _phase_aligned_depart_plan(
    xml_root: ET.Element,
    candidate_row: pd.Series,
    repeat_count: int,
    repeat_spacing_sec: float,
    depart_offset_sec: float | None = None,
) -> dict[str, Any]:
    tls_id = _normalize_id(candidate_row.get("tls_id_used")) or _normalize_id(candidate_row.get("tls_id"))
    ped_link_index = int(candidate_row.get("ped_link_index") or 0)
    tl_elem = xml_root.find(f".//tlLogic[@id='{tls_id}']")
    depart_time = float(depart_offset_sec if depart_offset_sec is not None else candidate_row.get("ped_depart_offset_sec", 0.0) or 0.0)
    selected_idx = ""
    selected_state = ""
    cycle_duration = ""
    first_green_start_time = ""
    depart_strategy = "explicit_offset"
    reason = "ped_depart_offset_sec"
    if tl_elem is not None:
        offset = float(tl_elem.attrib.get("offset", "0") or 0.0)
        phases = list(tl_elem.findall("phase"))
        durations = [float(phase.attrib.get("duration", "0") or 0.0) for phase in phases]
        states = [str(phase.attrib.get("state", "") or "") for phase in phases]
        cycle_duration_val = sum(durations)
        if cycle_duration_val > 0:
            cycle_duration = round(cycle_duration_val, 3)
            phase_start = offset
            for idx, (duration, state) in enumerate(zip(durations, states)):
                link_state = state[ped_link_index] if 0 <= ped_link_index < len(state) else ""
                ped_green = link_state in {"G", "g"}
                ped_only = ped_green and all(
                    not (j != ped_link_index and ch in {"G", "g"})
                    for j, ch in enumerate(state)
                )
                if ped_only:
                    selected_idx = idx
                    selected_state = state
                    first_green_start_time = round(phase_start, 3)
                    depart_time = max(0.0, phase_start - 1.5)
                    depart_strategy = "phase_aligned_green_start_minus_1p5s"
                    reason = "pedestrian_only_phase_match"
                    break
                phase_start += duration
    return {
        "crosswalk_id": _normalize_id(candidate_row.get("crosswalk_id")),
        "tls_id": tls_id,
        "ped_link_index": ped_link_index,
        "selected_ped_green_phase_index": selected_idx,
        "selected_ped_green_phase_state": selected_state,
        "cycle_duration": cycle_duration,
        "first_green_start_time": first_green_start_time,
        "depart_time": round(depart_time, 3),
        "depart_strategy": depart_strategy,
        "ped_repeat_count": int(repeat_count),
        "ped_repeat_spacing_sec": float(repeat_spacing_sec),
        "reason": reason,
    }


def _resolve_candidate_metadata(
    net: Any,
    xml_root: ET.Element,
    row: pd.Series,
) -> dict[str, Any]:
    lon = float(row.get("lon"))
    lat = float(row.get("lat"))
    try:
        md = discover_network_metadata_from_net(net, NET_FILE, lon, lat, cw_id=str(row.get("crosswalk_id")), xml_root=xml_root)
        source = "discover_network_metadata_from_net"
    except Exception:
        crossing_edge = choose_crossing_edge(net, lon, lat)
        tls_id = find_tls_id(net, crossing_edge, xml_root=xml_root)
        md = {
            "crossing_edge": crossing_edge.getID(),
            "tls_id": tls_id,
            "ped_link_indices": pedestrian_link_indices(NET_FILE, tls_id, crossing_edge.getID(), xml_root=xml_root),
            "ped_route": pedestrian_route_from_crossing(crossing_edge),
            "vehicle_conflict_edges": [],
            "approach_lanes": [],
        }
        source = "choose_crossing_edge_fallback"

    crossing_edge_id = _normalize_id(md.get("crossing_edge"))
    crossing_edge = net.getEdge(crossing_edge_id) if crossing_edge_id else None
    tls_id = _normalize_id(md.get("tls_id"))
    ped_indices = _parse_int_list(md.get("ped_link_indices"))
    route_from = _normalize_id((md.get("ped_route") or {}).get("from_edge"))
    route_to = _normalize_id((md.get("ped_route") or {}).get("to_edge"))

    route_candidates = []
    if crossing_edge_id:
        try:
            route_candidates.extend(_route_pair_from_crossing_edge(net, crossing_edge_id))
        except Exception:
            pass
    if route_from and route_to:
        route_candidates.append((route_from, route_to, "metadata_route"))

    route_valid = False
    contains_crossing_edge = False
    generated_route_edges = ""
    selected_reason = ""
    if crossing_edge_id:
        for frm, to, method in route_candidates:
            valid, reason, path_ids = _validate_route_pair(net, crossing_edge_id, frm, to)
            if valid:
                route_from, route_to = frm, to
                route_valid = True
                contains_crossing_edge = True
                generated_route_edges = "|".join(path_ids)
                selected_reason = f"{method}:{reason}"
                break
        if not route_valid and route_candidates:
            frm, to, method = route_candidates[0]
            valid, reason, path_ids = _validate_route_pair(net, crossing_edge_id, frm, to)
            route_from, route_to = frm, to
            route_valid = bool(valid)
            contains_crossing_edge = bool(valid)
            generated_route_edges = "|".join(path_ids)
            selected_reason = f"{method}:{reason}"

    if crossing_edge is not None:
        incoming_walk = any(getattr(edge, "getFunction", lambda: "")() == "walkingarea" for edge in crossing_edge.getIncoming().keys())
        outgoing_walk = any(getattr(edge, "getFunction", lambda: "")() == "walkingarea" for edge in crossing_edge.getOutgoing().keys())
    else:
        incoming_walk = False
        outgoing_walk = False

    tl_elem = xml_root.find(f".//tlLogic[@id='{tls_id}']") if tls_id else None
    phase_states = [str(p.attrib.get("state", "") or "") for p in tl_elem.findall("phase")] if tl_elem is not None else []
    phase_flags = _phase_flags(phase_states, ped_indices)
    ped_link_index = ped_indices[0] if ped_indices else None
    return {
        "source": source,
        "crossing_edge_id": crossing_edge_id,
        "crossing_present": bool(crossing_edge_id),
        "walkingarea_present": bool(incoming_walk and outgoing_walk),
        "tlLogic_present": bool(tls_id and tl_elem is not None),
        "tls_id_used": tls_id,
        "ped_link_index": ped_link_index,
        "ped_link_indices": ped_indices,
        "route_from_edge": route_from,
        "route_to_edge": route_to,
        "route_valid": bool(route_valid),
        "contains_crossing_edge": bool(contains_crossing_edge),
        "generated_route_edges": generated_route_edges,
        "phase_states": phase_states,
        "phase_flags": phase_flags,
        "route_reason": selected_reason,
    }


def _pick_final_tier(audit: dict[str, Any], in_final_237: bool) -> tuple[str, str, str]:
    crossing_present = bool(audit["crossing_present"])
    walkingarea_present = bool(audit["walkingarea_present"])
    tl_logic_present = bool(audit["tlLogic_present"])
    ped_link_index = audit["ped_link_index"]
    route_valid = bool(audit["route_valid"])
    phase_flags = audit["phase_flags"]
    ped_has_green = bool(phase_flags["ped_has_green"])
    ped_has_red = bool(phase_flags["ped_has_red"])
    ped_only = bool(phase_flags["pedestrian_only_phase_available"])
    mixed_only = bool(phase_flags["mixed_phase_only"])

    if not crossing_present or not walkingarea_present or not tl_logic_present or ped_link_index is None:
        tier = "GEOMETRY_OR_TLS_MISSING"
        action = "manual geometry/TLS review"
        reason = "missing_crossing_walkingarea_tls_or_ped_link"
    elif route_valid and ped_only:
        tier = "PHASE6_READY_PEDESTRIAN_ONLY"
        action = "queue smart/baseline seed 1 smoke"
        reason = "pedestrian_only_phase_and_valid_route"
    elif not route_valid:
        tier = "ROUTE_FIX_NEEDED"
        action = "rebuild route pair using crossing-edge audit"
        reason = "route_pair_not_valid"
    elif ped_link_index is not None and not ped_has_green:
        tier = "RED_ONLY_PHASE_FIX_NEEDED"
        action = "run red-only phase fix batch"
        reason = "ped_link_present_but_no_green"
    elif ped_has_green and mixed_only:
        tier = "MIXED_PHASE_REDESIGN_NEEDED"
        action = "phase redesign only; no policy smoke"
        reason = "ped_green_only_in_mixed_phase"
    elif in_final_237 and route_valid:
        tier = "NEEDS_SMOKE_VALIDATION"
        action = "run smoke validation after depart tuning"
        reason = "final237_route_valid_but_not_smoke_validated"
    else:
        tier = "MANUAL_REVIEW"
        action = "manual review"
        reason = "ambiguous_or_conflicting_signals"
    return tier, action, reason


def _previous_action(top33_class: str) -> str:
    mapping = {
        "TOP33_ALREADY_RESTORED": "phase 6 smoke validation",
        "TOP33_RED_ONLY_PHASE_FIX_TARGET": "red-only phase fix batch",
        "TOP33_MIXED_PHASE_FIX_TARGET": "mixed-phase redesign",
        "TOP33_MANUAL_REVIEW": "manual review",
        "NOT_IN_TOP33": "add to top50 audit",
    }
    return mapping.get(top33_class, "manual review")


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    stepwise = _load_csv(STEPWISE_50M)
    final_237 = _load_csv(FINAL_237)
    integrity_237 = _load_csv(INTEGRITY_237)
    top33_ped_only = _load_csv(TOP33_PED_ONLY)
    if stepwise.empty:
        raise FileNotFoundError(STEPWISE_50M)
    if final_237.empty:
        raise FileNotFoundError(FINAL_237)
    if integrity_237.empty:
        raise FileNotFoundError(INTEGRITY_237)
    if not NET_FILE.exists():
        raise FileNotFoundError(NET_FILE)

    top50 = stepwise.head(50).copy().reset_index(drop=True)
    top50.insert(0, "original_rank", range(1, len(top50) + 1))
    top50["team_pick_priority"] = top50["original_rank"] <= 28
    top50["backup_priority"] = top50["original_rank"] > 28
    top50["canonical_crosswalk_id"] = top50["canonical_crosswalk_id"].map(_normalize_id)
    top50["source_crosswalk_id"] = top50["source_crosswalk_id"].map(_normalize_id)
    top50["crosswalk_id"] = top50["crosswalk_id"].map(_normalize_id)

    final_lookup = _make_lookup(final_237)
    integrity_lookup = _make_lookup(integrity_237)
    top33_lookup = _make_lookup(top33_ped_only)
    final_ids = set(final_237["crosswalk_id"].astype(str).map(_normalize_id).tolist())

    net = read_net(NET_FILE)
    xml_root = ET.parse(NET_FILE).getroot()

    signal_phase_rows: list[dict[str, Any]] = []
    audits: list[dict[str, Any]] = []
    depart_plan_rows: list[dict[str, Any]] = []

    for _, row in top50.iterrows():
        crosswalk_id = _normalize_id(row["crosswalk_id"])
        source_crosswalk_id = _normalize_id(row["source_crosswalk_id"])
        canonical_crosswalk_id = _normalize_id(row["canonical_crosswalk_id"])
        in_final_237 = crosswalk_id in final_ids or canonical_crosswalk_id in final_ids
        final_row = final_lookup.get(crosswalk_id) or final_lookup.get(canonical_crosswalk_id) or {}
        integrity_row = integrity_lookup.get(crosswalk_id) or integrity_lookup.get(canonical_crosswalk_id) or {}
        top33_row = top33_lookup.get(crosswalk_id) or top33_lookup.get(canonical_crosswalk_id) or {}
        previous_tier_source_parts = ["smart_crosswalk_sumo/data/crosswalk_stepwise_result_50m.csv"]
        if in_final_237:
            previous_tier_source_parts.append("result/phase_next_recovery_command_plan_20260514_200908/phase6_ready_candidates_total_FINAL_237.csv")
            previous_tier_source_parts.append("result/phase6_transition_after_recovery_20260514_220549/phase6_ready_candidates_total_FINAL_237_integrity_check.csv")
        if top33_row:
            previous_tier_source_parts.append("result/phase_next_top33_recovery_20260515_155623/top33_phase6_pedestrian_only_candidates.csv")

        audit = _resolve_candidate_metadata(net, xml_root, row)
        phase_flags = audit["phase_flags"]
        phase_aligned_depart_possible = bool(
            audit["route_valid"]
            and audit["tlLogic_present"]
            and audit["ped_link_index"] is not None
            and phase_flags["pedestrian_only_phase_available"]
        )
        current_recovery_class, recommended_action, reason = _pick_final_tier(audit, in_final_237)
        if current_recovery_class == "PHASE6_READY_PEDESTRIAN_ONLY":
            final_tier = "PHASE6_READY_PEDESTRIAN_ONLY"
        elif current_recovery_class == "ROUTE_FIX_NEEDED" and in_final_237:
            final_tier = "ROUTE_FIX_NEEDED"
        elif current_recovery_class == "ROUTE_FIX_NEEDED":
            final_tier = "ROUTE_FIX_NEEDED"
        elif current_recovery_class == "RED_ONLY_PHASE_FIX_NEEDED":
            final_tier = "RED_ONLY_PHASE_FIX_NEEDED"
        elif current_recovery_class == "MIXED_PHASE_REDESIGN_NEEDED":
            final_tier = "MIXED_PHASE_REDESIGN_NEEDED"
        elif current_recovery_class == "NEEDS_SMOKE_VALIDATION":
            final_tier = "NEEDS_SMOKE_VALIDATION"
        elif current_recovery_class == "GEOMETRY_OR_TLS_MISSING":
            final_tier = "GEOMETRY_OR_TLS_MISSING"
        else:
            final_tier = "MANUAL_REVIEW"

        previous_top33_class = str(row.get("top33_recovery_status", "NOT_IN_TOP33"))
        previous_restored_level = str(row.get("sumo_restored_level", ""))
        previous_top33_recommended_action = _previous_action(previous_top33_class)

        if final_tier == "PHASE6_READY_PEDESTRIAN_ONLY" and phase_aligned_depart_possible:
            depart_plan_rows.append(
                _phase_aligned_depart_plan(
                    xml_root,
                    pd.Series(
                        {
                            "crosswalk_id": crosswalk_id,
                            "tls_id_used": audit["tls_id_used"],
                            "tls_id": audit["tls_id_used"],
                            "ped_link_index": audit["ped_link_index"],
                            "ped_depart_offset_sec": 0.0,
                        }
                    ),
                    repeat_count=5,
                    repeat_spacing_sec=2.0,
                    depart_offset_sec=None,
                )
            )

        phase_rows = audit["phase_states"]
        if phase_rows:
            for phase_index, phase_state in enumerate(phase_rows):
                ped_link_index = audit["ped_link_index"]
                ped_link_state = phase_state[ped_link_index] if ped_link_index is not None and ped_link_index < len(phase_state) else ""
                signal_phase_rows.append(
                    {
                        "crosswalk_id": crosswalk_id,
                        "tls_id": audit["tls_id_used"],
                        "ped_link_index": ped_link_index,
                        "phase_index": phase_index,
                        "phase_state": phase_state,
                        "ped_link_state": ped_link_state,
                        "ped_has_green": bool(ped_link_state in {"G", "g"}),
                        "ped_has_red": bool(ped_link_state in {"r", "R"}),
                        "pedestrian_only_phase": bool(
                            ped_link_state in {"G", "g"}
                            and all(not (idx != ped_link_index and ch in {"G", "g"}) for idx, ch in enumerate(phase_state))
                        ),
                        "non_ped_green_count": int(
                            sum(1 for idx, ch in enumerate(phase_state) if idx != ped_link_index and ch in {"G", "g"})
                        ),
                    }
                )
        else:
            signal_phase_rows.append(
                {
                    "crosswalk_id": crosswalk_id,
                    "tls_id": audit["tls_id_used"],
                    "ped_link_index": audit["ped_link_index"],
                    "phase_index": "",
                    "phase_state": "",
                    "ped_link_state": "",
                    "ped_has_green": False,
                    "ped_has_red": False,
                    "pedestrian_only_phase": False,
                    "non_ped_green_count": "",
                }
            )

        audits.append(
            {
                "original_rank": int(row["original_rank"]),
                "team_pick_priority": bool(row["team_pick_priority"]),
                "backup_priority": bool(row["backup_priority"]),
                "crosswalk_id": crosswalk_id,
                "source_crosswalk_id": source_crosswalk_id,
                "canonical_crosswalk_id": canonical_crosswalk_id,
                "lon": float(row.get("lon")) if pd.notna(row.get("lon")) else math.nan,
                "lat": float(row.get("lat")) if pd.notna(row.get("lat")) else math.nan,
                "risk_score": float(row.get("risk_score")) if pd.notna(row.get("risk_score")) else math.nan,
                "safety_index": float(row.get("risk_score")) if pd.notna(row.get("risk_score")) else math.nan,
                "score": float(row.get("risk_score")) if pd.notna(row.get("risk_score")) else math.nan,
                "in_FINAL_237": bool(in_final_237),
                "final237_verdict": str(final_row.get("final_verdict", "")) if final_row else "",
                "integrity_phase6_ready_confirmed": bool(integrity_row.get("phase6_ready_confirmed", False)) if integrity_row else False,
                "previous_restored_level": previous_restored_level,
                "previous_top33_class": previous_top33_class,
                "previous_top33_recommended_action": previous_top33_recommended_action,
                "previous_tier_source": "|".join(dict.fromkeys(previous_tier_source_parts)),
                "nearest_junction_id": audit["tls_id_used"],
                "tls_id_used": audit["tls_id_used"],
                "crossing_edge_id": audit["crossing_edge_id"],
                "crossing_present": bool(audit["crossing_present"]),
                "walkingarea_present": bool(audit["walkingarea_present"]),
                "tlLogic_present": bool(audit["tlLogic_present"]),
                "ped_link_index": audit["ped_link_index"],
                "ped_has_green": bool(phase_flags["ped_has_green"]),
                "ped_has_red": bool(phase_flags["ped_has_red"]),
                "pedestrian_only_phase_available": bool(phase_flags["pedestrian_only_phase_available"]),
                "mixed_phase_only": bool(phase_flags["mixed_phase_only"]),
                "red_only": bool(phase_flags["ped_has_red"] and not phase_flags["ped_has_green"]),
                "route_from_edge": audit["route_from_edge"],
                "route_to_edge": audit["route_to_edge"],
                "route_valid": bool(audit["route_valid"]),
                "contains_crossing_edge": bool(audit["contains_crossing_edge"]),
                "phase_aligned_depart_possible": bool(phase_aligned_depart_possible),
                "current_recovery_class": current_recovery_class,
                "final_tier": final_tier,
                "recommended_action": recommended_action,
                "reason": reason,
            }
        )

    audit_df = pd.DataFrame(audits)
    signal_phase_df = pd.DataFrame(signal_phase_rows)
    depart_plan_df = pd.DataFrame(depart_plan_rows)
    top50_requested = top50.copy()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    top50_requested.to_csv(OUT_DIR / "top50_requested_candidates.csv", index=False)
    audit_df.to_csv(OUT_DIR / "top50_tiered_recovery_audit.csv", index=False)
    signal_phase_df.to_csv(OUT_DIR / "signal_phase_audit.csv", index=False)
    if not depart_plan_df.empty:
        depart_plan_df.to_csv(OUT_DIR / "pedestrian_depart_plan.csv", index=False)

    ready_df = audit_df[audit_df["final_tier"] == "PHASE6_READY_PEDESTRIAN_ONLY"].copy()
    red_only_df = audit_df[audit_df["final_tier"] == "RED_ONLY_PHASE_FIX_NEEDED"].copy()
    mixed_df = audit_df[audit_df["final_tier"] == "MIXED_PHASE_REDESIGN_NEEDED"].copy()
    route_fix_df = audit_df[audit_df["final_tier"] == "ROUTE_FIX_NEEDED"].copy()
    manual_df = audit_df[audit_df["final_tier"] == "MANUAL_REVIEW"].copy()

    ready_df.to_csv(OUT_DIR / "top50_phase6_ready_pedestrian_only_candidates.csv", index=False)
    red_only_df.to_csv(OUT_DIR / "top50_red_only_phase_fix_input.csv", index=False)
    mixed_df.to_csv(OUT_DIR / "top50_mixed_phase_redesign_input.csv", index=False)
    route_fix_df.to_csv(OUT_DIR / "top50_route_fix_input.csv", index=False)
    manual_df.to_csv(OUT_DIR / "top50_manual_review_hold.csv", index=False)

    command_lines = [
        "# Top 50 next commands",
        "",
        "## Ready smoke candidates",
        "",
    ]
    if ready_df.empty:
        command_lines.append("- none")
    else:
        for row in ready_df.itertuples(index=False):
            cid = str(row.crosswalk_id)
            command_lines.extend(
                [
                    f"### {cid} smart seed 1",
                    "```bash",
                    "python3 -m smart_crosswalk_sumo.run_phase6_recovery_smoke \\",
                    f"  --candidate-csv result/phase_next_top50_tiered_recovery_audit/top50_phase6_ready_pedestrian_only_candidates.csv \\",
                    f"  --net-file {NET_FILE} \\",
                    "  --scenario smart \\",
                    "  --seed 1 \\",
                    "  --sim-duration 600 \\",
                    "  --warmup 0 \\",
                    "  --step-length 0.5 \\",
                    "  --extension-sec 5.0 \\",
                    "  --phase-aligned-ped-depart \\",
                    "  --ped-repeat-count 5 \\",
                    "  --ped-repeat-spacing-sec 2 \\",
                    f"  --output-dir result/phase_next_top50_tiered_recovery_audit/smoke_seed1_smart_{cid}",
                    "```",
                    "",
                    f"### {cid} baseline seed 1",
                    "```bash",
                    "python3 -m smart_crosswalk_sumo.run_phase6_recovery_smoke \\",
                    f"  --candidate-csv result/phase_next_top50_tiered_recovery_audit/top50_phase6_ready_pedestrian_only_candidates.csv \\",
                    f"  --net-file {NET_FILE} \\",
                    "  --scenario baseline \\",
                    "  --seed 1 \\",
                    "  --sim-duration 600 \\",
                    "  --warmup 0 \\",
                    "  --step-length 0.5 \\",
                    "  --extension-sec 5.0 \\",
                    "  --phase-aligned-ped-depart \\",
                    "  --ped-repeat-count 5 \\",
                    "  --ped-repeat-spacing-sec 2 \\",
                    f"  --output-dir result/phase_next_top50_tiered_recovery_audit/smoke_seed1_baseline_{cid}",
                    "```",
                    "",
                ]
            )

    command_lines.extend(
        [
            "## Red-only phase fix batch",
            "",
            "```bash",
            "python3 -m smart_crosswalk_sumo.run_red_only_phase_fix_batch \\",
            "  --input-csv result/phase_next_top50_tiered_recovery_audit/top50_red_only_phase_fix_input.csv \\",
            "  --output-dir result/phase_next_top50_tiered_recovery_audit/red_only_phase_fix_batch \\",
            "  --previous-ready result/phase_next_recovery_command_plan_20260514_200908/phase6_ready_candidates_total_FINAL_237.csv \\",
            f"  --base-net {NET_FILE}",
            "```",
            "",
            "## Route-fix revalidation",
            "",
            "```bash",
            "python3 -m smart_crosswalk_sumo.run_phase6_recovery_smoke \\",
            "  --candidate-csv result/phase_next_top50_tiered_recovery_audit/top50_route_fix_input.csv \\",
            f"  --net-file {NET_FILE} \\",
            "  --scenario smart \\",
            "  --seed 1 \\",
            "  --sim-duration 600 \\",
            "  --warmup 0 \\",
            "  --step-length 0.5 \\",
            "  --extension-sec 5.0 \\",
            "  --phase-aligned-ped-depart \\",
            "  --ped-repeat-count 5 \\",
            "  --ped-repeat-spacing-sec 2 \\",
            "  --output-dir result/phase_next_top50_tiered_recovery_audit/route_fix_revalidation_seed1",
            "```",
            "",
            "## Mixed phase redesign",
            "",
            "- execution prohibited; do not promote mixed-phase rows into policy smoke.",
        ]
    )
    (OUT_DIR / "top50_next_commands.md").write_text("\n".join(command_lines) + "\n", encoding="utf-8")

    summary_lines = [
        "# top50 tiered recovery summary",
        "",
        f"- top50 total: {len(audit_df)}",
        f"- team_pick_priority=True: {int(audit_df['team_pick_priority'].sum())}",
        f"- backup_priority=True: {int(audit_df['backup_priority'].sum())}",
        f"- in_FINAL_237: {int(audit_df['in_FINAL_237'].sum())}",
        "",
        "## previous_restored_level",
    ]
    for key, value in Counter(audit_df["previous_restored_level"].astype(str).tolist()).items():
        summary_lines.append(f"- {key}: {value}")
    summary_lines.extend(
        [
            "",
            "## final_tier",
        ]
    )
    for key, value in Counter(audit_df["final_tier"].astype(str).tolist()).items():
        summary_lines.append(f"- {key}: {value}")
    summary_lines.extend(
        [
            "",
            f"- 바로 seed 1 smoke 가능: {len(ready_df)}",
            f"- red-only phase fix 후보: {len(red_only_df)}",
            f"- mixed phase redesign 후보: {len(mixed_df)}",
            f"- route fix 후보: {len(route_fix_df)}",
            f"- manual review 후보: {len(manual_df)}",
            f"- team pick 28개 중 바로 smoke 가능: {int(ready_df['team_pick_priority'].sum())}",
            f"- team pick 28개 중 추가 복구 필요: {int(audit_df[audit_df['team_pick_priority'] & (audit_df['final_tier'] != 'PHASE6_READY_PEDESTRIAN_ONLY')].shape[0])}",
            "",
            "## NODE_74388 smoke 기준",
            "- smart extension event: phase=2, state=rrG, linkIndex=2, extension_sec=5.0",
            "- baseline extension_count=0",
        ]
    )
    (OUT_DIR / "top50_tiered_recovery_summary.md").write_text("\n".join(summary_lines) + "\n", encoding="utf-8")

    print(f"top50 total: {len(audit_df)}")
    print(f"team_pick_priority=True: {int(audit_df['team_pick_priority'].sum())}")
    print(f"backup_priority=True: {int(audit_df['backup_priority'].sum())}")
    print(f"in_FINAL_237: {int(audit_df['in_FINAL_237'].sum())}")
    print("previous_restored_level counts:")
    print(audit_df["previous_restored_level"].astype(str).value_counts(dropna=False).to_string())
    print("final_tier counts:")
    print(audit_df["final_tier"].astype(str).value_counts(dropna=False).to_string())
    print(f"seed1 smoke ready: {len(ready_df)}")
    print(f"red-only phase fix: {len(red_only_df)}")
    print(f"mixed phase redesign: {len(mixed_df)}")
    print(f"route fix: {len(route_fix_df)}")
    print(f"manual review: {len(manual_df)}")
    print(f"team pick 28 ready: {int(ready_df['team_pick_priority'].sum())}")
    print(f"team pick 28 extra recovery: {int(audit_df[audit_df['team_pick_priority'] & (audit_df['final_tier'] != 'PHASE6_READY_PEDESTRIAN_ONLY')].shape[0])}")
    print(f"output_dir: {OUT_DIR}")


if __name__ == "__main__":
    main()
