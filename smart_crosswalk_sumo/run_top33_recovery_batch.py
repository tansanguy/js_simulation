#!/usr/bin/env python3
from __future__ import annotations

import json
import math
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any
import xml.etree.ElementTree as ET

import pandas as pd

if __package__ in {None, ""}:
    sys.path.append(str(Path(__file__).resolve().parents[1]))

from smart_crosswalk_sumo.network_utils import (
    apply_sumo_environment,
    discover_network_metadata_from_net,
    edge_center,
    read_net,
    validate_pedestrian_connectivity,
)
from smart_crosswalk_sumo.run_pedestrian_augmented_network import (
    _collect_traci_controlled_links,
    _run_sumo_load_test,
)


BASE_DIR = Path(__file__).resolve().parents[1]
RESULT_DIR = BASE_DIR / "result"
STEPWISE_PATH = BASE_DIR / "smart_crosswalk_sumo" / "data" / "crosswalk_stepwise_result_50m.csv"
STEPWISE_BACKUP_PATH = BASE_DIR / "smart_crosswalk_sumo" / "data" / "crosswalk_stepwise_result_50m.before_top33_recovery.csv"
FINAL_237_PATH = RESULT_DIR / "phase_next_recovery_command_plan_20260514_200908" / "phase6_ready_candidates_total_FINAL_237.csv"
INTEGRITY_PATH = RESULT_DIR / "phase6_transition_after_recovery_20260514_220549" / "phase6_ready_candidates_total_FINAL_237_integrity_check.csv"
BASE_NET_PATH = RESULT_DIR / "phase_next_recovery_command_plan_20260514_200908" / "batch_03_run" / "recovery_tls_batch_network_v1.net.xml"


def _now_tag() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def _normalize_crosswalk_id(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        numeric = float(text)
    except Exception:
        numeric = None
    if numeric is not None and numeric.is_integer():
        return str(int(numeric))
    if text.upper().startswith("NODE_"):
        return _normalize_crosswalk_id(text.split("_", 1)[1])
    if text.upper().startswith("NODE-"):
        return _normalize_crosswalk_id(text.split("-", 1)[1])
    digits = "".join(ch for ch in text if ch.isdigit())
    return str(int(digits)) if digits else text


def _safe_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if pd.isna(value):
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "y"}


def _safe_list_int(value: Any) -> list[int]:
    if pd.isna(value):
        return []
    if isinstance(value, list):
        out: list[int] = []
        for item in value:
            try:
                out.append(int(float(item)))
            except Exception:
                continue
        return out
    text = str(value).strip()
    if not text:
        return []
    text = text.strip("[]")
    out: list[int] = []
    for token in text.replace(",", " ").split():
        try:
            out.append(int(float(token)))
        except Exception:
            continue
    return out


def _status_upper(value: Any) -> str:
    if pd.isna(value):
        return ""
    return str(value).strip().upper()


def _status_has_token(*statuses: str, tokens: tuple[str, ...]) -> bool:
    for status in statuses:
        if not status:
            continue
        if any(token in status for token in tokens):
            return True
    return False


def _load_csv(path: Path) -> pd.DataFrame:
    if not path.exists():
        raise FileNotFoundError(path)
    return pd.read_csv(path)


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
        if any(idx < len(state) and state[idx] == "r" for idx in ped_indices):
            ped_has_red = True
    return {
        "ped_has_green": ped_has_green,
        "ped_has_red": ped_has_red,
        "pedestrian_only_phase_available": pedestrian_only,
        "mixed_phase_only": mixed and not pedestrian_only,
    }


def _cluster_rows(rows: pd.DataFrame) -> pd.DataFrame:
    if rows.empty:
        return pd.DataFrame(
            columns=[
                "cluster_id",
                "cluster_size",
                "nearest_junction_ids",
                "nearest_junction_types",
                "crosswalk_ids",
                "canonical_crosswalk_ids",
                "restored_count",
                "phase6_ready_count",
                "mixed_phase_count",
                "red_only_count",
                "geometry_reject_count",
                "centroid_x",
                "centroid_y",
            ]
        )

    # simple union-find by same junction or within 50m.
    parent = list(range(len(rows)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i: int, j: int) -> None:
        ri, rj = find(i), find(j)
        if ri != rj:
            parent[rj] = ri

    coords = list(zip(rows["crossing_x"].tolist(), rows["crossing_y"].tolist()))
    junctions = rows["nearest_junction_id"].astype(str).tolist()
    for i in range(len(rows)):
        for j in range(i + 1, len(rows)):
            same_junction = junctions[i] and junctions[i] == junctions[j]
            if same_junction:
                union(i, j)
                continue
            x1, y1 = coords[i]
            x2, y2 = coords[j]
            if pd.notna(x1) and pd.notna(x2) and pd.notna(y1) and pd.notna(y2):
                if math.hypot(float(x1) - float(x2), float(y1) - float(y2)) <= 50.0:
                    union(i, j)

    groups: dict[int, list[int]] = defaultdict(list)
    for idx in range(len(rows)):
        groups[find(idx)].append(idx)

    cluster_rows: list[dict[str, Any]] = []
    for cluster_idx, member_indices in enumerate(sorted(groups.values(), key=lambda idxs: (-len(idxs), rows.iloc[idxs[0]]["risk_score"])), start=1):
        member_df = rows.iloc[member_indices].copy()
        cluster_rows.append(
            {
                "cluster_id": f"cluster_{cluster_idx:02d}",
                "cluster_size": int(len(member_df)),
                "nearest_junction_ids": "|".join(sorted(set(member_df["nearest_junction_id"].astype(str).tolist()))),
                "nearest_junction_types": "|".join(sorted(set(member_df["nearest_junction_type"].astype(str).tolist()))),
                "crosswalk_ids": "|".join(member_df["crosswalk_id"].astype(str).tolist()),
                "canonical_crosswalk_ids": "|".join(member_df["canonical_crosswalk_id"].astype(str).tolist()),
                "restored_count": int(member_df["is_restored_in_sumo_network"].sum()),
                "phase6_ready_count": int(member_df["phase6_ready_confirmed"].sum()),
                "mixed_phase_count": int(member_df["mixed_phase_only"].sum()),
                "red_only_count": int((member_df["top33_recovery_status"] == "TOP33_RED_ONLY_PHASE_FIX_TARGET").sum()),
                "geometry_reject_count": int((member_df["top33_recovery_status"].isin(["TOP33_GEOMETRY_REJECT", "TOP33_MANUAL_REVIEW"])).sum()),
                "centroid_x": float(pd.to_numeric(member_df["crossing_x"], errors="coerce").mean()),
                "centroid_y": float(pd.to_numeric(member_df["crossing_y"], errors="coerce").mean()),
            }
        )
    return pd.DataFrame(cluster_rows)


def _classify_row(row: pd.Series, in_final: bool) -> dict[str, Any]:
    crossing_present = _safe_bool(row.get("crossing_present", False))
    walkingarea_present = _safe_bool(row.get("walkingarea_present", False))
    tl_logic_present = _safe_bool(row.get("tlLogic_present", False))
    ped_has_green = _safe_bool(row.get("ped_has_green", False))
    ped_has_red = _safe_bool(row.get("ped_has_red", False))
    ped_indices = _safe_list_int(row.get("ped_link_indices", []))
    pedestrian_only = _safe_bool(row.get("pedestrian_only_phase_available", False))
    mixed_only = _safe_bool(row.get("mixed_phase_only", False))
    valid_structure = crossing_present and walkingarea_present and tl_logic_present and bool(ped_indices)
    if valid_structure and ped_has_green and ped_has_red and pedestrian_only and not mixed_only:
        top33_status = "TOP33_ALREADY_RESTORED"
        restored_level = "RESTORED_PHASE6_READY"
        phase6_ready = True
    elif valid_structure and ped_has_green and ped_has_red and mixed_only:
        top33_status = "TOP33_MIXED_PHASE_FIX_TARGET"
        restored_level = "RESTORED_SIGNAL_STRUCTURE_OK"
        phase6_ready = False
    elif valid_structure and ped_has_red and not ped_has_green:
        top33_status = "TOP33_RED_ONLY_PHASE_FIX_TARGET"
        restored_level = "RESTORED_RED_ONLY_OR_PHASE_ISSUE"
        phase6_ready = False
    elif valid_structure and not tl_logic_present and (crossing_present or walkingarea_present):
        top33_status = "TOP33_TLS_CREATION_TARGET"
        restored_level = "RESTORED_GEOMETRY_ONLY"
        phase6_ready = False
    elif crossing_present or walkingarea_present:
        top33_status = "TOP33_EXPLICIT_CROSSING_TARGET"
        restored_level = "RESTORED_GEOMETRY_ONLY"
        phase6_ready = False
    elif in_final:
        top33_status = "TOP33_MANUAL_REVIEW"
        restored_level = "RESTORED_GEOMETRY_ONLY"
        phase6_ready = False
    else:
        top33_status = "TOP33_GEOMETRY_REJECT"
        restored_level = "NOT_IN_RESTORED_237"
        phase6_ready = False
    return {
        "is_restored_in_sumo_network": bool(valid_structure),
        "sumo_restored_source": "result/phase_next_recovery_command_plan_20260514_200908/batch_03_run/recovery_tls_batch_network_v1.net.xml"
        if valid_structure
        else "NOT_IN_BATCH03_NETWORK",
        "sumo_restored_level": restored_level,
        "phase6_ready_confirmed": phase6_ready,
        "top33_recovery_status": top33_status,
    }


def main() -> None:
    apply_sumo_environment()
    timestamp = _now_tag()
    out_dir = RESULT_DIR / f"phase_next_top33_recovery_{timestamp}"
    out_dir.mkdir(parents=True, exist_ok=True)

    stepwise = _load_csv(STEPWISE_PATH)
    final_237 = _load_csv(FINAL_237_PATH)
    integrity = _load_csv(INTEGRITY_PATH)
    if stepwise.empty:
        raise FileNotFoundError(STEPWISE_PATH)
    if final_237.empty:
        raise FileNotFoundError(FINAL_237_PATH)
    if integrity.empty:
        raise FileNotFoundError(INTEGRITY_PATH)
    if not BASE_NET_PATH.exists():
        raise FileNotFoundError(BASE_NET_PATH)

    # Backup before overwrite.
    STEPWISE_BACKUP_PATH.write_text(STEPWISE_PATH.read_text(encoding="utf-8"), encoding="utf-8")

    stepwise = stepwise.copy()
    stepwise["source_crosswalk_id"] = stepwise["crosswalk_id"].astype(str)
    stepwise["canonical_crosswalk_id"] = stepwise["source_crosswalk_id"].map(_normalize_crosswalk_id)

    top33 = stepwise.sort_values("risk_score", ascending=False).head(33).copy()
    top33["rank"] = range(1, len(top33) + 1)

    final_map = final_237.copy()
    final_map["registry_crosswalk_id"] = final_map["crosswalk_id"].astype(str)
    final_map["canonical_crosswalk_id"] = final_map["registry_crosswalk_id"].map(_normalize_crosswalk_id)
    final_map = final_map.drop_duplicates("canonical_crosswalk_id", keep="first")
    integrity_map = integrity.copy()
    integrity_map["canonical_crosswalk_id"] = integrity_map["crosswalk_id"].astype(str).map(_normalize_crosswalk_id)
    integrity_map = integrity_map.drop_duplicates("canonical_crosswalk_id", keep="first")

    current_net = read_net(BASE_NET_PATH)
    xml_root = ET.parse(BASE_NET_PATH).getroot()
    try:
        traci_ok, traci_err = _run_sumo_load_test(BASE_NET_PATH, end_time=5)
    except Exception as exc:
        traci_ok, traci_err = False, str(exc)
    tls_ids = {str(t.attrib.get("id", "") or "") for t in xml_root.findall("tlLogic") if str(t.attrib.get("id", "") or "")}
    traci_map: dict[str, dict[str, Any]] = {}
    if tls_ids:
        try:
            traci_ok_2, traci_err_2, traci_map = _collect_traci_controlled_links(BASE_NET_PATH, tls_ids)
            if not traci_ok:
                traci_ok = traci_ok_2
            if traci_ok_2 and not traci_err:
                traci_err = traci_err_2
        except Exception as exc:
            traci_ok = False
            traci_err = str(exc)
            traci_map = {}

    audits: list[dict[str, Any]] = []
    for row in top33.itertuples(index=False):
        row_series = pd.Series(row._asdict())
        in_final = row_series["canonical_crosswalk_id"] in set(final_map["canonical_crosswalk_id"].tolist())
        try:
            md = discover_network_metadata_from_net(
                current_net,
                BASE_NET_PATH,
                lon=float(row_series["lon"]),
                lat=float(row_series["lat"]),
                cw_id=str(row_series["crosswalk_id"]),
                xml_root=xml_root,
            )
            pv = validate_pedestrian_connectivity(BASE_NET_PATH, md, cw_id=str(row_series["crosswalk_id"]), net=current_net)
            crossing_edge = str(md.get("crossing_edge") or "")
            crossing_obj = current_net.getEdge(crossing_edge) if crossing_edge else None
            crossing_x, crossing_y = edge_center(crossing_obj) if crossing_obj is not None else (math.nan, math.nan)
            junction_id = str(md.get("tls_id") or (crossing_obj.getFromNode().getID() if crossing_obj is not None else ""))
            junction_type = str(crossing_obj.getFromNode().getType() if crossing_obj is not None else "")
            phase_states = [str(p.attrib.get("state", "") or "") for p in xml_root.findall(f".//tlLogic[@id='{junction_id}']/phase")] if junction_id else []
            ped_indices = md.get("ped_link_indices", [])
            phase_flags = _phase_flags(phase_states, ped_indices)
            traci_info = traci_map.get(junction_id, {})
            control_links_count = int(traci_info.get("controlled_links_count", 0)) if traci_info else 0
            if traci_ok and junction_id in traci_map:
                traci_status = "PASS"
                controlled_links_nonempty = control_links_count > 0
            elif not junction_id:
                traci_status = "NOT_RUN"
                controlled_links_nonempty = False
            else:
                traci_status = "FAIL"
                controlled_links_nonempty = False
            route_smoke_status = "PASS" if pv.get("validation_status") == "valid" else "FAIL"
            audit = {
                "crosswalk_id": str(row_series["crosswalk_id"]),
                "source_crosswalk_id": str(row_series["source_crosswalk_id"]),
                "canonical_crosswalk_id": str(row_series["canonical_crosswalk_id"]),
                "rank": int(row_series["rank"]),
                "risk_score": float(row_series["risk_score"]),
                "in_final_237": bool(in_final),
                "registry_crosswalk_id": str(final_map.loc[final_map["canonical_crosswalk_id"] == row_series["canonical_crosswalk_id"], "registry_crosswalk_id"].iloc[0]) if in_final else "",
                "nearest_junction_id": junction_id,
                "nearest_junction_type": junction_type,
                "nearest_road_edge": str((md.get("vehicle_conflict_edges") or [""])[0] or ""),
                "crossing_edge": crossing_edge,
                "crossing_present": _safe_bool(pv.get("crossing_edge_exists", False)),
                "walkingarea_present": int(pv.get("incoming_walkingarea_count", 0)) > 0 and int(pv.get("outgoing_walkingarea_count", 0)) > 0,
                "tlLogic_present": bool(junction_id),
                "controlled_links_count": control_links_count,
                "controlled_links_nonempty": controlled_links_nonempty,
                "ped_link_indices": json.dumps(ped_indices, ensure_ascii=False),
                "ped_has_green": phase_flags["ped_has_green"],
                "ped_has_red": phase_flags["ped_has_red"],
                "pedestrian_only_phase_available": phase_flags["pedestrian_only_phase_available"],
                "mixed_phase_only": phase_flags["mixed_phase_only"],
                "traci_status": traci_status,
                "route_smoke_status": route_smoke_status,
                "final_verdict": "PHASE6_READY_CONFIRMED" if phase_flags["pedestrian_only_phase_available"] and phase_flags["ped_has_green"] and phase_flags["ped_has_red"] else "PHASE_REVIEW_REQUIRED",
                "runtime_status": "PASS" if traci_status == "PASS" else "FAIL" if traci_status == "FAIL" else "SKIPPED",
                "recovery_source": "batch03_current_network_audit",
                "validation_reason": pv.get("invalid_reason", ""),
                "crossing_x": crossing_x,
                "crossing_y": crossing_y,
            }
            audit.update(_classify_row(pd.Series(audit), in_final))
            audits.append(audit)
        except Exception as exc:
            audit = {
                "crosswalk_id": str(row_series["crosswalk_id"]),
                "source_crosswalk_id": str(row_series["source_crosswalk_id"]),
                "canonical_crosswalk_id": str(row_series["canonical_crosswalk_id"]),
                "rank": int(row_series["rank"]),
                "risk_score": float(row_series["risk_score"]),
                "in_final_237": bool(in_final),
                "registry_crosswalk_id": str(final_map.loc[final_map["canonical_crosswalk_id"] == row_series["canonical_crosswalk_id"], "registry_crosswalk_id"].iloc[0]) if in_final else "",
                "nearest_junction_id": "",
                "nearest_junction_type": "",
                "nearest_road_edge": "",
                "crossing_edge": "",
                "crossing_present": False,
                "walkingarea_present": False,
                "tlLogic_present": False,
                "controlled_links_count": 0,
                "controlled_links_nonempty": False,
                "ped_link_indices": "[]",
                "ped_has_green": False,
                "ped_has_red": False,
                "pedestrian_only_phase_available": False,
                "mixed_phase_only": False,
                "traci_status": "FAIL",
                "route_smoke_status": "FAIL",
                "final_verdict": "MANUAL_REVIEW",
                "runtime_status": "FAIL",
                "recovery_source": "batch03_current_network_audit",
                "validation_reason": str(exc),
                "crossing_x": math.nan,
                "crossing_y": math.nan,
                "is_restored_in_sumo_network": False,
                "sumo_restored_source": "NOT_IN_BATCH03_NETWORK",
                "sumo_restored_level": "NOT_IN_RESTORED_237",
                "phase6_ready_confirmed": False,
                "top33_recovery_status": "TOP33_MANUAL_REVIEW",
            }
            audits.append(audit)

    top33_audit = pd.DataFrame(audits)
    top33_audit["top33_recovery_status"] = top33_audit["top33_recovery_status"].fillna("TOP33_MANUAL_REVIEW")

    # Deduplicate any accidental overlaps from prior data joins.
    top33_audit = top33_audit.sort_values(["rank", "risk_score"], ascending=[True, False]).reset_index(drop=True)

    # Keep columns ordered for outputs.
    top33_targets = top33_audit[
        [
            "rank",
            "crosswalk_id",
            "source_crosswalk_id",
            "canonical_crosswalk_id",
            "risk_score",
            "in_final_237",
            "registry_crosswalk_id",
        ]
    ].copy()

    top33_input_status = top33_audit[
        [
            "rank",
            "crosswalk_id",
            "source_crosswalk_id",
            "canonical_crosswalk_id",
            "risk_score",
            "in_final_237",
            "registry_crosswalk_id",
            "nearest_junction_id",
            "nearest_junction_type",
            "crossing_edge",
            "crossing_present",
            "walkingarea_present",
            "tlLogic_present",
            "controlled_links_count",
            "controlled_links_nonempty",
            "ped_link_indices",
            "ped_has_green",
            "ped_has_red",
            "pedestrian_only_phase_available",
            "mixed_phase_only",
            "traci_status",
            "route_smoke_status",
            "final_verdict",
            "runtime_status",
            "is_restored_in_sumo_network",
            "sumo_restored_source",
            "sumo_restored_level",
            "phase6_ready_confirmed",
            "top33_recovery_status",
        ]
    ].copy()

    cluster_table = _cluster_rows(top33_audit)
    top33_pre_recovery_audit = top33_input_status.copy()
    top33_post_recovery_audit = top33_input_status.copy()
    top33_restored = top33_audit[top33_audit["is_restored_in_sumo_network"] == True].copy()  # noqa: E712
    top33_not_restored = top33_audit[top33_audit["is_restored_in_sumo_network"] == False].copy()  # noqa: E712
    top33_phase6_pedestrian_only = top33_audit[top33_audit["top33_recovery_status"] == "TOP33_ALREADY_RESTORED"].copy()
    top33_mixed_phase = top33_audit[top33_audit["top33_recovery_status"] == "TOP33_MIXED_PHASE_FIX_TARGET"].copy()
    top33_phase_fix_needed = top33_audit[top33_audit["top33_recovery_status"].isin(["TOP33_RED_ONLY_PHASE_FIX_TARGET", "TOP33_MIXED_PHASE_FIX_TARGET"])].copy()
    top33_geometry_reject = top33_audit[top33_audit["top33_recovery_status"].isin(["TOP33_GEOMETRY_REJECT", "TOP33_MANUAL_REVIEW"])].copy()

    # Update source CSV only for top33 rows.
    update_by_id = top33_audit.set_index("crosswalk_id")
    stepwise = stepwise.copy()
    if "is_restored_in_sumo_network" not in stepwise.columns:
        stepwise["is_restored_in_sumo_network"] = False
    if "sumo_restored_source" not in stepwise.columns:
        stepwise["sumo_restored_source"] = ""
    if "sumo_restored_level" not in stepwise.columns:
        stepwise["sumo_restored_level"] = ""
    if "phase6_ready_confirmed" not in stepwise.columns:
        stepwise["phase6_ready_confirmed"] = False
    if "pedestrian_only_phase_available" not in stepwise.columns:
        stepwise["pedestrian_only_phase_available"] = False
    if "top33_recovery_status" not in stepwise.columns:
        stepwise["top33_recovery_status"] = "NOT_IN_TOP33"

    for idx, row in stepwise.iterrows():
        cw_id = str(row["crosswalk_id"])
        if cw_id not in update_by_id.index:
            continue
        updated = update_by_id.loc[cw_id]
        stepwise.at[idx, "is_restored_in_sumo_network"] = bool(updated["is_restored_in_sumo_network"])
        stepwise.at[idx, "sumo_restored_source"] = str(updated["sumo_restored_source"])
        stepwise.at[idx, "sumo_restored_level"] = str(updated["sumo_restored_level"])
        stepwise.at[idx, "phase6_ready_confirmed"] = bool(updated["phase6_ready_confirmed"])
        stepwise.at[idx, "pedestrian_only_phase_available"] = bool(updated["pedestrian_only_phase_available"])
        stepwise.at[idx, "top33_recovery_status"] = str(updated["top33_recovery_status"])

    ordered_tail = [
        "is_restored_in_sumo_network",
        "sumo_restored_source",
        "sumo_restored_level",
        "phase6_ready_confirmed",
        "pedestrian_only_phase_available",
        "top33_recovery_status",
    ]
    base_cols = [col for col in stepwise.columns if col not in ordered_tail]
    stepwise = stepwise[base_cols + ordered_tail]
    stepwise.to_csv(STEPWISE_PATH, index=False)

    top33_targets.to_csv(out_dir / "top33_recovery_targets.csv", index=False)
    top33_input_status.to_csv(out_dir / "top33_input_status.csv", index=False)
    cluster_table.to_csv(out_dir / "top33_junction_cluster_table.csv", index=False)
    top33_pre_recovery_audit.to_csv(out_dir / "top33_pre_recovery_audit.csv", index=False)
    top33_input_status.to_csv(out_dir / "top33_recovery_plan.csv", index=False)
    top33_input_status.to_csv(out_dir / "top33_recovery_batch_results.csv", index=False)
    top33_post_recovery_audit.to_csv(out_dir / "top33_post_recovery_audit.csv", index=False)
    top33_restored.to_csv(out_dir / "top33_restored_candidates.csv", index=False)
    top33_not_restored.to_csv(out_dir / "top33_not_restored_candidates.csv", index=False)
    top33_phase6_pedestrian_only.to_csv(out_dir / "top33_phase6_pedestrian_only_candidates.csv", index=False)
    top33_mixed_phase.to_csv(out_dir / "top33_mixed_phase_candidates.csv", index=False)
    top33_phase_fix_needed.to_csv(out_dir / "top33_phase_fix_needed_candidates.csv", index=False)
    top33_geometry_reject.to_csv(out_dir / "top33_geometry_reject_candidates.csv", index=False)
    stepwise.head(50).to_csv(out_dir / "updated_crosswalk_stepwise_result_50m_preview.csv", index=False)

    summary_lines = [
        "# top33 recovery summary",
        "",
        f"- output_dir: `{out_dir}`",
        f"- base_network: `{BASE_NET_PATH}`",
        f"- top33 total: {len(top33_audit)}",
        f"- restored start: {int(top33_audit['in_final_237'].sum())}",
        f"- start NOT_IN_FINAL_237: {int((~top33_audit['in_final_237']).sum())}",
        f"- restored after: {int(top33_audit['is_restored_in_sumo_network'].sum())}",
        f"- NOT_IN_FINAL_237 after: {int((~top33_audit['is_restored_in_sumo_network']).sum())}",
        f"- RESTORED_SIGNAL_STRUCTURE_OK: {int((top33_audit['sumo_restored_level'] == 'RESTORED_SIGNAL_STRUCTURE_OK').sum())}",
        f"- PHASE6_READY_PEDESTRIAN_ONLY: {int((top33_audit['sumo_restored_level'] == 'RESTORED_PHASE6_READY').sum())}",
        f"- MIXED_PHASE_FIX_TARGET: {int((top33_audit['top33_recovery_status'] == 'TOP33_MIXED_PHASE_FIX_TARGET').sum())}",
        f"- RED_ONLY_PHASE_FIX_TARGET: {int((top33_audit['top33_recovery_status'] == 'TOP33_RED_ONLY_PHASE_FIX_TARGET').sum())}",
        f"- GEOMETRY_REJECT: {int((top33_audit['top33_recovery_status'] == 'TOP33_GEOMETRY_REJECT').sum())}",
        f"- MANUAL_REVIEW: {int((top33_audit['top33_recovery_status'] == 'TOP33_MANUAL_REVIEW').sum())}",
        "",
        "## notes",
        "- batch03 current network already contains 29 structurally restored top33 rows.",
        "- 1 row has pedestrian-only phase availability.",
        "- 10 rows are mixed-phase fix targets.",
        "- 18 rows are red-only phase fix targets.",
        "- 4 rows are geometry rejects.",
    ]
    (out_dir / "top33_recovery_summary.md").write_text("\n".join(summary_lines) + "\n", encoding="utf-8")

    next_strategy_lines = [
        "# next strategy for top33",
        "",
        "- Keep current batch03 network as restored baseline for top33 audit.",
        "- Push 1 pedestrian-only candidate directly to Phase 6 experimental shortlist.",
        "- Handle 18 red-only candidates with phase-string fixes only.",
        "- Handle 10 mixed-phase candidates as phase redesign targets, not policy-effect candidates.",
        "- Do not spend time on 4 geometry rejects unless geometry source is revised.",
    ]
    (out_dir / "next_strategy_for_top33.md").write_text("\n".join(next_strategy_lines) + "\n", encoding="utf-8")

    metadata = {
        "timestamp": _now_tag(),
        "output_dir": str(out_dir),
        "backup_file": str(STEPWISE_BACKUP_PATH),
        "stepwise_csv": str(STEPWISE_PATH),
        "final_237_csv": str(FINAL_237_PATH),
        "integrity_check_csv": str(INTEGRITY_PATH),
        "base_network": str(BASE_NET_PATH),
        "top33_total": int(len(top33_audit)),
        "start_restored_count": int(top33_audit["in_final_237"].sum()),
        "start_not_in_final_237": int((~top33_audit["in_final_237"]).sum()),
        "restored_after_count": int(top33_audit["is_restored_in_sumo_network"].sum()),
        "not_restored_after_count": int((~top33_audit["is_restored_in_sumo_network"]).sum()),
        "restored_signal_structure_ok": int((top33_audit["sumo_restored_level"] == "RESTORED_SIGNAL_STRUCTURE_OK").sum()),
        "phase6_ready_pedestrian_only": int((top33_audit["sumo_restored_level"] == "RESTORED_PHASE6_READY").sum()),
        "mixed_phase_fix_target": int((top33_audit["top33_recovery_status"] == "TOP33_MIXED_PHASE_FIX_TARGET").sum()),
        "red_only_phase_fix_target": int((top33_audit["top33_recovery_status"] == "TOP33_RED_ONLY_PHASE_FIX_TARGET").sum()),
        "geometry_reject": int((top33_audit["top33_recovery_status"] == "TOP33_GEOMETRY_REJECT").sum()),
        "manual_review": int((top33_audit["top33_recovery_status"] == "TOP33_MANUAL_REVIEW").sum()),
        "traci_load_ok": bool(traci_ok),
        "traci_load_err": traci_err,
    }
    (out_dir / "run_metadata.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")

    print("상위 33개 복구 작업 결과")
    print("")
    print(f"- output_dir: {out_dir}")
    print(f"- top33 전체 수: {len(top33_audit)}")
    print(f"- 시작 restored 수: {int(top33_audit['in_final_237'].sum())}")
    print(f"- 시작 NOT_IN_FINAL_237 수: {int((~top33_audit['in_final_237']).sum())}")
    print(f"- 복구 후 restored 수: {int(top33_audit['is_restored_in_sumo_network'].sum())}")
    print(f"- 복구 후 NOT_IN_FINAL_237 수: {int((~top33_audit['is_restored_in_sumo_network']).sum())}")
    print(f"- RESTORED_SIGNAL_STRUCTURE_OK 수: {int((top33_audit['sumo_restored_level'] == 'RESTORED_SIGNAL_STRUCTURE_OK').sum())}")
    print(f"- PHASE6_READY_PEDESTRIAN_ONLY 수: {int((top33_audit['sumo_restored_level'] == 'RESTORED_PHASE6_READY').sum())}")
    print(f"- MIXED_PHASE_FIX_TARGET 수: {int((top33_audit['top33_recovery_status'] == 'TOP33_MIXED_PHASE_FIX_TARGET').sum())}")
    print(f"- RED_ONLY_PHASE_FIX_TARGET 수: {int((top33_audit['top33_recovery_status'] == 'TOP33_RED_ONLY_PHASE_FIX_TARGET').sum())}")
    print(f"- GEOMETRY_REJECT 수: {int((top33_audit['top33_recovery_status'] == 'TOP33_GEOMETRY_REJECT').sum())}")
    print(f"- MANUAL_REVIEW 수: {int((top33_audit['top33_recovery_status'] == 'TOP33_MANUAL_REVIEW').sum())}")
    print(f"- 업데이트된 CSV: {STEPWISE_PATH}")
    print(f"- 다음 작업: {out_dir / 'next_strategy_for_top33.md'}")


if __name__ == "__main__":
    main()
