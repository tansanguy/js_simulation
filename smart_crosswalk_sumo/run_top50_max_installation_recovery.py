#!/usr/bin/env python3
from __future__ import annotations

import argparse
import html
import json
import math
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

if __package__ in {None, ""}:
    sys.path.append(str(Path(__file__).resolve().parents[1]))

from smart_crosswalk_sumo.network_utils import (
    apply_sumo_environment,
    edge_center,
    edge_function,
    pedestrian_link_indices,
    pedestrian_route_from_crossing,
    read_net,
)
from smart_crosswalk_sumo.run_pedestrian_augmented_network import (
    _collect_traci_controlled_links,
    _run_sumo_load_test,
)


BASE_DIR = Path(__file__).resolve().parents[1]
RESULT_DIR = BASE_DIR / "result"
TOP50_DIR = RESULT_DIR / "phase_next_top50_final_candidate_screening"
GEN_OK6_DIR = RESULT_DIR / "phase_next_generated_ok6_phase6_prep_20260515_182319"
GEN_PATCH_DIR = RESULT_DIR / "phase_next_bad22_generated_crossing_patch_20260515_181150"
BASE_NET = (
    RESULT_DIR
    / "phase_next_recovery_command_plan_20260514_200908"
    / "batch_03_run"
    / "recovery_tls_batch_network_v1.net.xml"
)
TOP50_COMBINED = TOP50_DIR / "top50_signal_and_location_combined_screening.csv"
TOP50_GOOD = TOP50_DIR / "top50_final_good_mapping_good_result_candidates.csv"
TOP50_MAP = RESULT_DIR / "phase_next_top50_mapping_visual_audit_20260515_174626" / "03_top50_mapping_distance_summary.csv"
GEN_DIAG = GEN_OK6_DIR / "generated_phase6_candidate_table_diagnostics.csv"
GEN_PATCH_CANDIDATES = GEN_PATCH_DIR / "generated_crossing_patch_candidates.csv"
GEN_PATCH_LOCATION = GEN_PATCH_DIR / "generated_crossing_location_check.csv"

REQUIRED_SPLIT_COLS = [
    "crosswalk_id",
    "original_rank",
    "current_status",
    "real_lon",
    "real_lat",
    "current_sumo_lon",
    "current_sumo_lat",
    "mapping_distance_m",
    "location_verified",
    "crossing_exists",
    "tls_connected",
    "ped_link_index_available",
    "pedestrian_only_phase_available",
    "smart_smoke_success",
    "baseline_smoke_success",
    "target_recovery_group",
    "recommended_action",
    "reason",
]


def _now_tag() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def _norm(value: Any) -> str:
    if value is None:
        return ""
    try:
        if pd.isna(value):
            return ""
    except Exception:
        pass
    text = str(value).strip()
    if text.endswith(".0") and text[:-2].replace("-", "", 1).isdigit():
        return text[:-2]
    return "" if text in {"nan", "None"} else text


def _bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    text = _norm(value).lower()
    return text in {"1", "true", "yes", "y"}


def _int_or_blank(value: Any) -> int | str:
    try:
        if value is None or pd.isna(value):
            return ""
        return int(float(value))
    except Exception:
        return ""


def _float_or_blank(value: Any, digits: int = 6) -> float | str:
    try:
        if value is None or pd.isna(value):
            return ""
        return round(float(value), digits)
    except Exception:
        return ""


def _parse_int_list(value: Any) -> list[int]:
    if isinstance(value, list):
        vals = value
    else:
        text = _norm(value)
        if not text or text == "[]":
            return []
        text = text.strip("[]").replace(",", " ").replace("|", " ")
        vals = text.split()
    out: set[int] = set()
    for token in vals:
        try:
            out.add(int(float(str(token).strip().strip("'\""))))
        except Exception:
            continue
    return sorted(out)


def _parse_edges(value: Any) -> list[str]:
    text = _norm(value).replace(",", " ").replace("|", " ")
    if text.startswith("[") and text.endswith("]"):
        text = text[1:-1]
    return [tok.strip().strip("'\"") for tok in text.split() if tok.strip()]


def _read_csv(path: Path) -> pd.DataFrame:
    if not path.exists():
        raise FileNotFoundError(path)
    return pd.read_csv(path)


def _write_csv(path: Path, df: pd.DataFrame, columns: list[str] | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if columns is not None:
        out = df.copy()
        for col in columns:
            if col not in out.columns:
                out[col] = ""
        out = out[columns]
    else:
        out = df
    out.to_csv(path, index=False)


def _phase_summary(root: ET.Element, tls_id: str, ped_indices: list[int]) -> dict[str, Any]:
    tl = next((t for t in root.findall("tlLogic") if _norm(t.attrib.get("id")) == tls_id), None)
    if tl is None:
        return {
            "tlLogic_present": False,
            "ped_has_green": False,
            "ped_has_red": False,
            "pedestrian_only_phase_available": False,
            "pedestrian_only_phase_indices": "",
            "mixed_phase_only": False,
            "red_only": False,
            "phase_states": "[]",
        }
    states = [_norm(p.attrib.get("state")) for p in tl.findall("phase")]
    ped_has_green = False
    ped_has_red = False
    ped_only: list[int] = []
    mixed = False
    red_only = False
    for idx, state in enumerate(states):
        if state and set(state) == {"r"}:
            red_only = True
        for ped_idx in ped_indices:
            if ped_idx < 0 or ped_idx >= len(state):
                continue
            ch = state[ped_idx]
            ped_has_green = ped_has_green or ch in {"G", "g"}
            ped_has_red = ped_has_red or ch == "r"
            non_ped_green = sum(1 for i, c in enumerate(state) if i != ped_idx and c in {"G", "g"})
            if ch in {"G", "g"} and non_ped_green == 0:
                ped_only.append(idx)
            if ch in {"G", "g"} and non_ped_green > 0:
                mixed = True
    return {
        "tlLogic_present": True,
        "ped_has_green": bool(ped_has_green),
        "ped_has_red": bool(ped_has_red),
        "pedestrian_only_phase_available": bool(ped_only),
        "pedestrian_only_phase_indices": "|".join(str(v) for v in sorted(set(ped_only))),
        "mixed_phase_only": bool(mixed and not ped_only),
        "red_only": bool(red_only and not ped_has_green),
        "phase_states": json.dumps(states, ensure_ascii=False),
    }


def _crossing_center_lonlat(net: Any, edge_id: str) -> tuple[float | str, float | str]:
    try:
        edge = net.getEdge(edge_id)
        x, y = edge_center(edge)
        lon, lat = net.convertXY2LonLat(x, y)
        return round(float(lon), 6), round(float(lat), 6)
    except Exception:
        return "", ""


def _nearest_tls(net: Any, lon: Any, lat: Any) -> tuple[str, float | str]:
    try:
        xy = net.convertLonLat2XY(float(lon), float(lat))
    except Exception:
        return "", ""
    best_id = ""
    best_dist = float("inf")
    for tl in net.getTrafficLights():
        tls_id = tl.getID()
        if not net.hasNode(tls_id):
            continue
        try:
            dist = math.dist(xy, net.getNode(tls_id).getCoord())
        except Exception:
            continue
        if dist < best_dist:
            best_id = tls_id
            best_dist = dist
    return best_id, round(best_dist, 3) if best_id else ""


def _route_for_crossing(net: Any, crossing_edge_id: str) -> tuple[str, str, str]:
    try:
        crossing = net.getEdge(crossing_edge_id)
        route = pedestrian_route_from_crossing(crossing)
        return _norm(route.get("from_edge")), _norm(route.get("to_edge")), "pedestrian_route_from_crossing"
    except Exception as exc:
        return "", "", f"route_discovery_failed:{exc}"


def _audit_candidates(net_file: Path, rows: pd.DataFrame, id_cols: dict[str, str] | None = None) -> pd.DataFrame:
    id_cols = id_cols or {}
    root = ET.parse(net_file).getroot()
    net = read_net(net_file)
    tl_ids = {_norm(t.attrib.get("id")) for t in root.findall("tlLogic") if _norm(t.attrib.get("id"))}
    audited: list[dict[str, Any]] = []
    for _, row in rows.iterrows():
        cid = _norm(row.get("crosswalk_id"))
        crossing_edge_id = _norm(row.get(id_cols.get("crossing_edge_id", "crossing_edge_id")))
        tls_id = _norm(row.get(id_cols.get("tls_id", "tls_id_used"))) or _norm(row.get("nearest_junction_id"))
        ped_indices = _parse_int_list(row.get("ped_link_indices"))
        ped_idx = _int_or_blank(row.get("ped_link_index"))
        if isinstance(ped_idx, int) and ped_idx not in ped_indices:
            ped_indices.append(ped_idx)
        if crossing_edge_id and not ped_indices:
            try:
                ped_indices = pedestrian_link_indices(net_file, tls_id, crossing_edge_id, xml_root=root)
            except Exception:
                ped_indices = []
        crossing_exists = False
        crossing_function = ""
        if crossing_edge_id:
            try:
                crossing_function = edge_function(net.getEdge(crossing_edge_id))
                crossing_exists = crossing_function == "crossing"
            except Exception:
                crossing_exists = False
        route_from, route_to, route_reason = _route_for_crossing(net, crossing_edge_id) if crossing_exists else ("", "", "missing_crossing")
        phase = _phase_summary(root, tls_id, sorted(set(ped_indices)))
        center_lon, center_lat = _crossing_center_lonlat(net, crossing_edge_id) if crossing_exists else ("", "")
        audited.append(
            {
                "crosswalk_id": cid,
                "tls_id": tls_id,
                "tls_id_used": tls_id,
                "nearest_junction_id": _norm(row.get("nearest_junction_id")) or tls_id,
                "crossing_edge_id": crossing_edge_id,
                "crossing_id": crossing_edge_id,
                "crossing_exists": bool(crossing_exists),
                "crossing_function": crossing_function,
                "tls_connected": bool(tls_id in tl_ids),
                "ped_link_indices": json.dumps(sorted(set(ped_indices)), ensure_ascii=False),
                "ped_link_index": sorted(set(ped_indices))[0] if ped_indices else "",
                "ped_link_index_available": bool(ped_indices),
                "route_from_edge": route_from,
                "route_to_edge": route_to,
                "route_reason": route_reason,
                "fixed_crossing_lon": center_lon,
                "fixed_crossing_lat": center_lat,
                **phase,
            }
        )
    return pd.DataFrame(audited)


def _build_split_row(
    row: pd.Series,
    map_row: pd.Series | None,
    status: str,
    group: str,
    action: str,
    reason: str,
    audit: dict[str, Any] | None = None,
) -> dict[str, Any]:
    audit = audit or {}
    distance_grade = _norm(row.get("distance_grade")).upper()
    status_text = _norm(row.get("final_screening_status"))
    inferred_location_verified = distance_grade in {"OK", "GOOD"} or status_text in {
        "FINAL_GOOD_MAPPING_AND_GOOD_RESULT",
        "LOCATION_OK_BUT_SMART_RESULT_FAIL",
    }
    return {
        "crosswalk_id": _norm(row.get("crosswalk_id")),
        "original_rank": _int_or_blank(row.get("original_rank")),
        "current_status": status,
        "real_lon": _float_or_blank(map_row.get("real_lon") if map_row is not None else row.get("real_lon")),
        "real_lat": _float_or_blank(map_row.get("real_lat") if map_row is not None else row.get("real_lat")),
        "current_sumo_lon": _float_or_blank(map_row.get("sumo_lon") if map_row is not None else row.get("old_sumo_lon")),
        "current_sumo_lat": _float_or_blank(map_row.get("sumo_lat") if map_row is not None else row.get("old_sumo_lat")),
        "mapping_distance_m": _float_or_blank(row.get("mapping_distance_m", row.get("old_mapping_distance_m")), 3),
        "location_verified": _bool(audit.get("location_verified", row.get("location_verified", inferred_location_verified)))
        or inferred_location_verified,
        "crossing_exists": _bool(audit.get("crossing_exists", False)),
        "tls_connected": _bool(audit.get("tls_connected", False)),
        "ped_link_index_available": _bool(audit.get("ped_link_index_available", False)),
        "pedestrian_only_phase_available": _bool(audit.get("pedestrian_only_phase_available", False)),
        "smart_smoke_success": _bool(row.get("smart_extension_count", 0)) and _bool(row.get("smart_completed", False)),
        "baseline_smoke_success": _bool(row.get("baseline_completed", False)) and int(float(row.get("baseline_extension_count", 0) or 0)) == 0,
        "target_recovery_group": group,
        "recommended_action": action,
        "reason": reason,
    }


def _make_splits(out_dir: Path, base_audit: pd.DataFrame) -> dict[str, pd.DataFrame]:
    combined = _read_csv(TOP50_COMBINED)
    good = _read_csv(TOP50_GOOD)
    mapping = _read_csv(TOP50_MAP)
    gen_diag = _read_csv(GEN_DIAG)
    map_by_id = {str(r.crosswalk_id): r for r in mapping.itertuples(index=False)}
    audit_by_id = {str(r.crosswalk_id): r._asdict() for r in base_audit.itertuples(index=False)}

    split_defs = [
        ("main_verified_current_net_12.csv", good, "main_verified_current_net", "keep_current_main_candidate_table", "preserve verified main-net seed1 smoke success"),
        (
            "signal_fix_targets_9.csv",
            combined[combined["final_screening_status"] == "LOCATION_OK_BUT_SMART_RESULT_FAIL"].copy(),
            "signal_fix_net_v1",
            "add_or_repair_pedestrian_only_phase",
            "location reliable but smart extension did not trigger",
        ),
        (
            "location_repair_targets_9.csv",
            combined[combined["final_screening_status"].isin(["RESULT_OK_BUT_LOCATION_BAD", "RESULT_OK_BUT_LOCATION_REVIEW"])].copy(),
            "location_repair_or_generated_signal_net_v1",
            "repair_real_location_or_try_generated_crossing_signal",
            "smart result exists but mapped location is unreliable",
        ),
    ]
    outputs: dict[str, pd.DataFrame] = {}
    for filename, df, group, action, base_reason in split_defs:
        rows = []
        for _, row in df.sort_values("original_rank").iterrows():
            cid = _norm(row.get("crosswalk_id"))
            m = pd.Series(map_by_id[cid]._asdict()) if cid in map_by_id else None
            rows.append(
                _build_split_row(
                    row,
                    m,
                    _norm(row.get("final_screening_status")),
                    group,
                    action,
                    _norm(row.get("reason")) or base_reason,
                    audit_by_id.get(cid),
                )
            )
        out = pd.DataFrame(rows)
        _write_csv(out_dir / filename, out, REQUIRED_SPLIT_COLS)
        outputs[filename] = out

    gen_rows = []
    for _, row in gen_diag.sort_values("original_rank").iterrows():
        gen_rows.append(
            {
                "crosswalk_id": _norm(row.get("crosswalk_id")),
                "original_rank": _int_or_blank(row.get("original_rank")),
                "current_status": "GENERATED_GEOMETRY_OK",
                "real_lon": _float_or_blank(row.get("real_lon")),
                "real_lat": _float_or_blank(row.get("real_lat")),
                "current_sumo_lon": _float_or_blank(row.get("old_sumo_lon")),
                "current_sumo_lat": _float_or_blank(row.get("old_sumo_lat")),
                "mapping_distance_m": _float_or_blank(row.get("old_mapping_distance_m"), 3),
                "location_verified": _norm(row.get("location_check_status")) == "OK",
                "crossing_exists": _bool(row.get("generated_crossing_exists")),
                "tls_connected": _bool(row.get("tls_id_found")),
                "ped_link_index_available": _bool(row.get("ped_link_index_found")),
                "pedestrian_only_phase_available": _bool(row.get("pedestrian_only_phase_available")),
                "smart_smoke_success": False,
                "baseline_smoke_success": False,
                "target_recovery_group": "generated_signal_net_v1",
                "recommended_action": "try_existing_tls_or_new_signal_installation",
                "reason": _norm(row.get("reason")) or "generated geometry lacks TLS/ped_link_index",
            }
        )
    gen_out = pd.DataFrame(gen_rows)
    _write_csv(out_dir / "generated_signal_targets_6.csv", gen_out, REQUIRED_SPLIT_COLS)
    outputs["generated_signal_targets_6.csv"] = gen_out

    master = pd.concat(outputs.values(), ignore_index=True)
    _write_csv(out_dir / "top50_max_installation_master.csv", master, REQUIRED_SPLIT_COLS)
    outputs["top50_max_installation_master.csv"] = master
    return outputs


def _build_tllogic_patch(
    source_net: Path,
    candidates: pd.DataFrame,
    out_path: Path,
    duration: str = "6",
) -> pd.DataFrame:
    root = ET.parse(source_net).getroot()
    tl_by_id = {_norm(t.attrib.get("id")): t for t in root.findall("tlLogic") if _norm(t.attrib.get("id"))}
    patch_root = ET.Element("tlLogics")
    rows: list[dict[str, Any]] = []
    for tls_id, group in candidates.groupby(candidates["tls_id_used"].astype(str), sort=False):
        tls_id = _norm(tls_id)
        ped_indices: set[int] = set()
        ids: list[str] = []
        for _, row in group.iterrows():
            ids.append(_norm(row.get("crosswalk_id")))
            ped_indices.update(_parse_int_list(row.get("ped_link_indices")))
            ped_idx = _int_or_blank(row.get("ped_link_index"))
            if isinstance(ped_idx, int):
                ped_indices.add(ped_idx)
        base_tl = tl_by_id.get(tls_id)
        if base_tl is None:
            rows.append({"crosswalk_id": "|".join(ids), "tls_id": tls_id, "patch_status": "REJECT", "reason": "missing_tlLogic"})
            continue
        phases = base_tl.findall("phase")
        if not phases:
            rows.append({"crosswalk_id": "|".join(ids), "tls_id": tls_id, "patch_status": "REJECT", "reason": "missing_phase"})
            continue
        max_len = max(len(_norm(p.attrib.get("state"))) for p in phases)
        bad = [idx for idx in ped_indices if idx < 0 or idx >= max_len]
        if bad or not ped_indices:
            rows.append(
                {
                    "crosswalk_id": "|".join(ids),
                    "tls_id": tls_id,
                    "patch_status": "REJECT",
                    "reason": "missing_or_out_of_range_ped_link_index",
                    "ped_link_indices": json.dumps(sorted(ped_indices)),
                }
            )
            continue
        tl_copy = ET.fromstring(ET.tostring(base_tl, encoding="utf-8"))
        copy_phases = tl_copy.findall("phase")
        target_phase = None
        target_idx = None
        for idx, phase in enumerate(copy_phases):
            state = _norm(phase.attrib.get("state"))
            if state and set(state) == {"r"}:
                target_phase = phase
                target_idx = idx
                break
        if target_phase is None:
            state = "r" * max_len
            target_phase = ET.Element("phase", {"duration": duration, "state": state})
            tl_copy.append(target_phase)
            target_idx = len(copy_phases)
            action = "append_pedestrian_only_phase"
        else:
            action = "reuse_all_red_phase_as_pedestrian_only_phase"
        chars = list(_norm(target_phase.attrib.get("state")))
        for idx in ped_indices:
            chars[idx] = "G"
        target_phase.set("state", "".join(chars))
        target_phase.set("duration", duration if not _norm(target_phase.attrib.get("duration")) else _norm(target_phase.attrib.get("duration")))
        patch_root.append(tl_copy)
        rows.append(
            {
                "crosswalk_id": "|".join(ids),
                "tls_id": tls_id,
                "ped_link_indices": json.dumps(sorted(ped_indices)),
                "patch_status": "PATCHED",
                "patch_action": action,
                "patched_phase_index": target_idx,
                "reason": "ped_link_index_only_green_vehicle_links_red",
            }
        )
    ET.indent(patch_root, space="  ")
    out_path.write_text(ET.tostring(patch_root, encoding="unicode", xml_declaration=True), encoding="utf-8")
    return pd.DataFrame(rows)


def _run_netconvert(cmd: list[str], log_path: Path) -> tuple[bool, str]:
    try:
        ret = subprocess.run(cmd, capture_output=True, text=True, timeout=1200)
        log_path.write_text((ret.stdout or "") + "\n" + (ret.stderr or ""), encoding="utf-8")
        return ret.returncode == 0, (ret.stderr or ret.stdout or "").strip()[:1000]
    except Exception as exc:
        log_path.write_text(str(exc), encoding="utf-8")
        return False, str(exc)


def _netconvert_with_tllogic(source_net: Path, tllogic: Path, output_net: Path, log_name: str) -> tuple[bool, str, list[str]]:
    cmd = [
        "netconvert",
        "--sumo-net-file",
        str(source_net),
        "--tllogic-files",
        str(tllogic),
        "--output-file",
        str(output_net),
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
    ok, msg = _run_netconvert(cmd, output_net.parent / log_name)
    return ok and output_net.exists(), msg, cmd


def _candidate_table_from_audit(audit: pd.DataFrame, source_name: str, net_file: Path) -> pd.DataFrame:
    if audit.empty:
        return pd.DataFrame()
    out = audit[
        audit["crossing_exists"].astype(bool)
        & audit["tls_connected"].astype(bool)
        & audit["ped_link_index_available"].astype(bool)
        & audit["pedestrian_only_phase_available"].astype(bool)
    ].copy()
    if out.empty:
        return out
    out["source_crosswalk_id"] = out["crosswalk_id"]
    out["canonical_crosswalk_id"] = out["crosswalk_id"]
    out["ped_depart_offset_sec"] = 0.0
    out["ped_repeat_count"] = 5
    out["ped_repeat_spacing_sec"] = 1.5
    out["source_file"] = source_name
    out["batch_network_file"] = str(net_file)
    out["final_verdict"] = "CANDIDATE_TABLE_READY_PENDING_SEED1_SMOKE"
    out["step_test_ok"] = True
    out["controlled_links_count"] = ""
    out["crossing_inventory_used"] = False
    cols = [
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
    return out[cols].copy()


def _build_signal_fix(out_dir: Path, top50_map: pd.DataFrame) -> dict[str, Any]:
    combined = _read_csv(TOP50_COMBINED)
    targets = combined[combined["final_screening_status"] == "LOCATION_OK_BUT_SMART_RESULT_FAIL"].copy()
    targets = targets.merge(
        top50_map[
            [
                "crosswalk_id",
                "nearest_junction_id",
                "tls_id_used",
                "crossing_edge_id",
                "ped_link_index",
                "real_lon",
                "real_lat",
                "sumo_lon",
                "sumo_lat",
            ]
        ],
        on="crosswalk_id",
        how="left",
    )
    targets["ped_link_indices"] = targets["ped_link_index"].apply(lambda v: json.dumps([int(v)]) if _int_or_blank(v) != "" else "[]")
    pre_audit = _audit_candidates(BASE_NET, targets)
    patch_plan = _build_tllogic_patch(BASE_NET, targets, out_dir / "signal_fix_net_v1.tll.xml")
    net_file = out_dir / "signal_fix_net_v1.net.xml"
    ok, msg, cmd = _netconvert_with_tllogic(BASE_NET, out_dir / "signal_fix_net_v1.tll.xml", net_file, "signal_fix_netconvert.log")
    (out_dir / "command_to_build_signal_fix_net_v1.sh").write_text("#!/usr/bin/env bash\nset -euo pipefail\n" + " ".join(cmd) + "\n", encoding="utf-8")
    post_audit = _audit_candidates(net_file if ok else BASE_NET, targets) if (ok or BASE_NET.exists()) else pd.DataFrame()
    cand = _candidate_table_from_audit(post_audit, "signal_fix_candidate_table.csv", net_file)
    _write_csv(out_dir / "signal_fix_patch_plan.csv", patch_plan)
    _write_csv(out_dir / "signal_fix_phase_audit.csv", post_audit)
    _write_csv(out_dir / "signal_fix_candidate_table.csv", cand)
    load_ok, load_err = _run_sumo_load_test(net_file) if ok else (False, msg)
    summary = [
        "# signal_fix_net_v1 summary",
        "",
        f"- input_targets: {len(targets)}",
        f"- unique_tls: {targets['tls_id_used'].nunique()}",
        f"- patch_rows: {len(patch_plan)}",
        f"- netconvert_ok: {ok}",
        f"- load_ok: {load_ok}",
        f"- candidate_table_rows: {len(cand)}",
        f"- pre_pedestrian_only_available: {int(pre_audit['pedestrian_only_phase_available'].sum()) if not pre_audit.empty else 0}",
        f"- post_pedestrian_only_available: {int(post_audit['pedestrian_only_phase_available'].sum()) if not post_audit.empty else 0}",
        f"- net_file: `{net_file}`",
        f"- load_error: {load_err or ''}",
    ]
    (out_dir / "signal_fix_net_v1_summary.md").write_text("\n".join(summary) + "\n", encoding="utf-8")
    return {"net": net_file, "candidate_table": out_dir / "signal_fix_candidate_table.csv", "audit": post_audit, "net_ok": ok, "load_ok": load_ok}


def _write_generated_node_and_crossing_patch(targets: pd.DataFrame, out_dir: Path) -> tuple[Path, Path]:
    base_patch = _read_csv(GEN_PATCH_CANDIDATES)
    use = base_patch[base_patch["crosswalk_id"].astype(str).isin(set(targets["crosswalk_id"].astype(str)))].copy()
    node_root = ET.Element("nodes")
    conn_root = ET.Element("connections")
    seen_nodes: set[str] = set()
    seen_crossings: set[tuple[str, str]] = set()
    for _, row in use.iterrows():
        node_id = _norm(row.get("selected_node_id"))
        if not node_id:
            continue
        if node_id not in seen_nodes:
            ET.SubElement(
                node_root,
                "node",
                {
                    "id": node_id,
                    "x": str(row.get("selected_node_x")),
                    "y": str(row.get("selected_node_y")),
                    "type": "traffic_light",
                },
            )
            seen_nodes.add(node_id)
        edges = " ".join(_parse_edges(row.get("selected_crossing_edges")))
        key = (node_id, edges)
        if edges and key not in seen_crossings:
            ET.SubElement(
                conn_root,
                "crossing",
                {"node": node_id, "edges": edges, "id": f"SIG_{_norm(row.get('crosswalk_id'))}", "width": "4.0"},
            )
            seen_crossings.add(key)
    ET.indent(node_root, space="  ")
    ET.indent(conn_root, space="  ")
    nodes_path = out_dir / "generated_signal_nodes_tls.nod.xml"
    con_path = out_dir / "generated_signal_crossings.con.xml"
    nodes_path.write_text(ET.tostring(node_root, encoding="unicode", xml_declaration=True), encoding="utf-8")
    con_path.write_text(ET.tostring(conn_root, encoding="unicode", xml_declaration=True), encoding="utf-8")
    return nodes_path, con_path


def _build_generated_signal(out_dir: Path, top50_map: pd.DataFrame) -> dict[str, Any]:
    gen_diag = _read_csv(GEN_DIAG)
    combined = _read_csv(TOP50_COMBINED)
    loc_repair_ids = set(
        combined[combined["final_screening_status"].isin(["RESULT_OK_BUT_LOCATION_BAD", "RESULT_OK_BUT_LOCATION_REVIEW"])]["crosswalk_id"].astype(str)
    )
    patch_candidates = _read_csv(GEN_PATCH_CANDIDATES)
    target_ids = set(gen_diag["crosswalk_id"].astype(str)) | (loc_repair_ids & set(patch_candidates["crosswalk_id"].astype(str)))
    targets = patch_candidates[patch_candidates["crosswalk_id"].astype(str).isin(target_ids)].copy()
    targets = targets.merge(
        combined[["crosswalk_id", "final_screening_status", "smart_completed", "baseline_completed", "smart_extension_count", "baseline_extension_count"]],
        on="crosswalk_id",
        how="left",
    )
    loc = _read_csv(GEN_PATCH_LOCATION)
    targets = targets.merge(
        loc[["crosswalk_id", "generated_crossing_edge_id", "generated_crossing_exists", "generated_crossing_center_lon", "generated_crossing_center_lat", "generated_to_real_distance_m", "location_check_status"]],
        on="crosswalk_id",
        how="left",
    )
    nodes_path, con_path = _write_generated_node_and_crossing_patch(targets, out_dir)
    pre_net = out_dir / "generated_signal_pre_phase.net.xml"
    cmd = [
        "netconvert",
        "--sumo-net-file",
        str(BASE_NET),
        "--node-files",
        str(nodes_path),
        "--connection-files",
        str(con_path),
        "--output-file",
        str(pre_net),
        "--no-turnarounds",
        "true",
        "--walkingareas",
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
    ok_pre, msg_pre = _run_netconvert(cmd, out_dir / "generated_signal_pre_netconvert.log")
    (out_dir / "command_to_build_generated_signal_pre_phase_net.sh").write_text("#!/usr/bin/env bash\nset -euo pipefail\n" + " ".join(cmd) + "\n", encoding="utf-8")

    if ok_pre and pre_net.exists():
        root = ET.parse(pre_net).getroot()
        net = read_net(pre_net)
        audit_rows: list[dict[str, Any]] = []
        for _, row in targets.iterrows():
            cid = _norm(row.get("crosswalk_id"))
            crossing = _norm(row.get("generated_crossing_edge_id"))
            if not crossing:
                node_id = _norm(row.get("selected_node_id"))
                crossing = f":{node_id}_c0"
            tls_id = _norm(row.get("selected_node_id"))
            ped_indices = pedestrian_link_indices(pre_net, tls_id, crossing, xml_root=root) if crossing else []
            route_from, route_to, route_reason = _route_for_crossing(net, crossing) if ped_indices else ("", "", "missing_ped_link_index")
            phase = _phase_summary(root, tls_id, ped_indices)
            nearest_tls_id, nearest_tls_dist = _nearest_tls(net, row.get("real_lon"), row.get("real_lat"))
            audit_rows.append(
                {
                    "crosswalk_id": cid,
                    "original_rank": _int_or_blank(row.get("original_rank")),
                    "real_lon": _float_or_blank(row.get("real_lon")),
                    "real_lat": _float_or_blank(row.get("real_lat")),
                    "old_sumo_lon": _float_or_blank(row.get("old_sumo_lon")),
                    "old_sumo_lat": _float_or_blank(row.get("old_sumo_lat")),
                    "generated_crossing_center_lon": _float_or_blank(row.get("generated_crossing_center_lon")),
                    "generated_crossing_center_lat": _float_or_blank(row.get("generated_crossing_center_lat")),
                    "mapping_distance_m": _float_or_blank(row.get("old_mapping_distance_m"), 3),
                    "generated_to_real_distance_m": _float_or_blank(row.get("generated_to_real_distance_m"), 3),
                    "selected_node_id": _norm(row.get("selected_node_id")),
                    "selected_crossing_edges": _norm(row.get("selected_crossing_edges")),
                    "crossing_edge_id": crossing,
                    "crossing_id": crossing,
                    "tls_id": tls_id,
                    "tls_id_used": tls_id,
                    "nearest_junction_id": tls_id,
                    "nearest_tls_id": nearest_tls_id,
                    "nearest_tls_distance_m": nearest_tls_dist,
                    "crossing_exists": _bool(row.get("generated_crossing_exists")),
                    "tls_connected": tls_id in {_norm(t.attrib.get("id")) for t in root.findall("tlLogic")},
                    "ped_link_indices": json.dumps(ped_indices, ensure_ascii=False),
                    "ped_link_index": ped_indices[0] if ped_indices else "",
                    "ped_link_index_available": bool(ped_indices),
                    "route_from_edge": route_from,
                    "route_to_edge": route_to,
                    "route_reason": route_reason,
                    "integration_class": "NEW_SIGNAL_INSTALLATION",
                    "recommended_action": "run_separate_baseline_for_generated_signal_net",
                    "reason": "generated node converted to traffic_light; do not mix with main candidates",
                    **phase,
                }
            )
        pre_audit = pd.DataFrame(audit_rows)
        patch_ready = pre_audit[pre_audit["ped_link_index_available"].astype(bool)].copy()
        patch_plan = _build_tllogic_patch(pre_net, patch_ready, out_dir / "generated_signal_net_v1.tll.xml")
        net_file = out_dir / "generated_signal_net_v1.net.xml"
        ok, msg, phase_cmd = _netconvert_with_tllogic(pre_net, out_dir / "generated_signal_net_v1.tll.xml", net_file, "generated_signal_netconvert.log")
        final_audit = _audit_candidates(net_file if ok else pre_net, patch_ready) if not patch_ready.empty else pre_audit.iloc[0:0].copy()
        if not final_audit.empty:
            final_audit = final_audit.merge(pre_audit.drop(columns=[c for c in pre_audit.columns if c in final_audit.columns and c != "crosswalk_id"]), on="crosswalk_id", how="left")
    else:
        pre_audit = pd.DataFrame()
        patch_plan = pd.DataFrame()
        final_audit = pd.DataFrame()
        net_file = out_dir / "generated_signal_net_v1.net.xml"
        phase_cmd = []
        ok = False
        msg = msg_pre

    _write_csv(out_dir / "generated_signal_integration_audit.csv", pre_audit)
    _write_csv(out_dir / "generated_signal_phase_audit.csv", final_audit)
    _write_csv(out_dir / "generated_signal_patch_plan.csv", patch_plan)
    if not net_file.exists() and pre_net.exists():
        shutil.copyfile(pre_net, net_file)
    cand = _candidate_table_from_audit(final_audit, "generated_signal_candidate_table.csv", net_file) if not final_audit.empty else pd.DataFrame()
    _write_csv(out_dir / "generated_signal_candidate_table.csv", cand)
    load_ok, load_err = _run_sumo_load_test(net_file) if net_file.exists() else (False, msg)
    if phase_cmd:
        (out_dir / "command_to_build_generated_signal_net_v1.sh").write_text("#!/usr/bin/env bash\nset -euo pipefail\n" + " ".join(phase_cmd) + "\n", encoding="utf-8")
    summary = [
        "# generated_signal_net_v1 summary",
        "",
        f"- input_targets: {len(targets)}",
        f"- generated_ok6_targets: {len(gen_diag)}",
        f"- location_repair_targets_with_generated_patch: {len(set(targets['crosswalk_id'].astype(str)) - set(gen_diag['crosswalk_id'].astype(str)))}",
        f"- pre_netconvert_ok: {ok_pre}",
        f"- final_netconvert_ok: {ok}",
        f"- load_ok: {load_ok}",
        f"- candidate_table_rows: {len(cand)}",
        f"- integration_class: NEW_SIGNAL_INSTALLATION",
        f"- baseline_required: true",
        f"- net_file: `{net_file}`",
        f"- load_error: {load_err or ''}",
    ]
    (out_dir / "generated_signal_net_v1_summary.md").write_text("\n".join(summary) + "\n", encoding="utf-8")
    return {"net": net_file, "candidate_table": out_dir / "generated_signal_candidate_table.csv", "audit": final_audit, "net_ok": ok, "load_ok": load_ok}


def _write_smoke_command(out_dir: Path, name: str, csv_path: Path, net_path: Path) -> Path:
    script = out_dir / f"command_to_run_{name}_seed1_smoke.sh"
    smart_dir = out_dir / f"{name}_seed1_smart_smoke"
    baseline_dir = out_dir / f"{name}_seed1_baseline_smoke"
    content = f"""#!/usr/bin/env bash
set -euo pipefail
CSV="{csv_path.resolve()}"
NET="{net_path.resolve()}"
ROWS=$(python3 - <<'PY'
import pandas as pd
from pathlib import Path
csv = Path(r"{csv_path.resolve()}")
print(len(pd.read_csv(csv)) if csv.exists() else 0)
PY
)
if [[ "${{ROWS}}" -eq 0 ]]; then
  echo "No seed1 smoke candidates in $CSV"
  exit 0
fi
python3 -m smart_crosswalk_sumo.run_phase6_recovery_smoke \\
  --candidate-csv "$CSV" \\
  --net-file "$NET" \\
  --scenario smart \\
  --seed 1 \\
  --sim-duration 600 \\
  --phase-aligned-ped-depart \\
  --output-dir "{smart_dir.resolve()}"
python3 -m smart_crosswalk_sumo.run_phase6_recovery_smoke \\
  --candidate-csv "$CSV" \\
  --net-file "$NET" \\
  --scenario baseline \\
  --seed 1 \\
  --sim-duration 600 \\
  --phase-aligned-ped-depart \\
  --output-dir "{baseline_dir.resolve()}"
"""
    script.write_text(content, encoding="utf-8")
    script.chmod(0o755)
    return script


def _verdict_from_smoke(out_dir: Path, name: str, candidate_csv: Path, run_smoke: bool) -> pd.DataFrame:
    smart_dir = out_dir / f"{name}_seed1_smart_smoke"
    baseline_dir = out_dir / f"{name}_seed1_baseline_smoke"
    if run_smoke and candidate_csv.exists() and len(pd.read_csv(candidate_csv)) > 0:
        script = out_dir / f"command_to_run_{name}_seed1_smoke.sh"
        ret = subprocess.run([str(script)], cwd=BASE_DIR, capture_output=True, text=True, timeout=1800)
        (out_dir / f"{name}_seed1_smoke_run.log").write_text((ret.stdout or "") + "\n" + (ret.stderr or ""), encoding="utf-8")
    cand = pd.read_csv(candidate_csv) if candidate_csv.exists() else pd.DataFrame(columns=["crosswalk_id"])
    smart = smart_dir / "phase6_smoke_summary.csv"
    base = baseline_dir / "phase6_smoke_summary.csv"
    smart_df = pd.read_csv(smart) if smart.exists() else pd.DataFrame()
    base_df = pd.read_csv(base) if base.exists() else pd.DataFrame()
    def as_int(value: Any, default: int = 0) -> int:
        try:
            if value is None or pd.isna(value):
                return default
            return int(float(value))
        except Exception:
            return default

    rows: list[dict[str, Any]] = []
    for cid in cand["crosswalk_id"].astype(str).tolist() if not cand.empty else []:
        sr = smart_df[smart_df["crosswalk_id"].astype(str) == cid].iloc[0].to_dict() if not smart_df.empty and cid in set(smart_df["crosswalk_id"].astype(str)) else {}
        br = base_df[base_df["crosswalk_id"].astype(str) == cid].iloc[0].to_dict() if not base_df.empty and cid in set(base_df["crosswalk_id"].astype(str)) else {}
        smart_ped_count = as_int(sr.get("ped_crossing_person_count"), 0)
        smart_extension_count = as_int(sr.get("extension_count"), 0)
        baseline_ped_count = as_int(br.get("ped_crossing_person_count"), 0)
        baseline_extension_count = as_int(br.get("extension_count"), -1)
        smart_ok = bool(sr) and _bool(sr.get("completed")) and smart_ped_count > 0 and smart_extension_count > 0
        base_ok = bool(br) and _bool(br.get("completed")) and baseline_ped_count > 0 and baseline_extension_count == 0
        rows.append(
            {
                "crosswalk_id": cid,
                "smart_completed": _bool(sr.get("completed")) if sr else False,
                "smart_ped_crossing_person_count": smart_ped_count if sr else 0,
                "smart_extension_count": smart_extension_count if sr else 0,
                "smart_smoke_success": smart_ok,
                "baseline_completed": _bool(br.get("completed")) if br else False,
                "baseline_ped_crossing_person_count": baseline_ped_count if br else 0,
                "baseline_extension_count": baseline_extension_count if br else -1,
                "baseline_smoke_success": base_ok,
                "ready_for_30seed": bool(smart_ok and base_ok),
                "reason": "seed1 smart/baseline passed" if smart_ok and base_ok else "seed1 smoke not run or criteria failed",
            }
        )
    return pd.DataFrame(rows)


def _render_map(out_html: Path, title: str, table: pd.DataFrame, audit: pd.DataFrame) -> None:
    merged = table.copy()
    if not audit.empty and "crosswalk_id" in audit.columns:
        merged = merged.merge(audit, on="crosswalk_id", how="left", suffixes=("", "_audit"))
    records: list[dict[str, Any]] = []
    for _, row in merged.iterrows():
        real_lon = row.get("real_lon")
        real_lat = row.get("real_lat")
        old_lon = row.get("current_sumo_lon", row.get("old_sumo_lon"))
        old_lat = row.get("current_sumo_lat", row.get("old_sumo_lat"))
        fixed_lon = row.get("fixed_crossing_lon", row.get("generated_crossing_center_lon"))
        fixed_lat = row.get("fixed_crossing_lat", row.get("generated_crossing_center_lat"))
        records.append(
            {
                "crosswalk_id": _norm(row.get("crosswalk_id")),
                "original_rank": _int_or_blank(row.get("original_rank")),
                "real_lon": _float_or_blank(real_lon),
                "real_lat": _float_or_blank(real_lat),
                "old_lon": _float_or_blank(old_lon),
                "old_lat": _float_or_blank(old_lat),
                "fixed_lon": _float_or_blank(fixed_lon),
                "fixed_lat": _float_or_blank(fixed_lat),
                "mapping_distance_m": _float_or_blank(row.get("mapping_distance_m"), 3),
                "tls_id": _norm(row.get("tls_id", row.get("tls_id_used"))),
                "ped_link_index": _norm(row.get("ped_link_index")),
                "pedestrian_only_phase_available": _bool(row.get("pedestrian_only_phase_available")),
                "final_recovery_status": _norm(row.get("final_recovery_status", row.get("current_status"))),
                "candidate_status": _norm(row.get("current_status")),
                "recommended_action": _norm(row.get("recommended_action")),
            }
        )
    center = next(((r["real_lat"], r["real_lon"]) for r in records if r["real_lat"] != "" and r["real_lon"] != ""), (37.56, 126.98))
    payload = json.dumps(records, ensure_ascii=False)
    html_doc = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>{html.escape(title)}</title>
  <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
  <style>
    html, body, #map {{ height: 100%; margin: 0; }}
    body {{ font-family: sans-serif; }}
    .panel {{ position:absolute; z-index:999; top:12px; left:12px; background:#111827; color:#f9fafb; padding:10px 12px; border-radius:8px; max-width:360px; }}
    .panel h1 {{ margin:0 0 6px; font-size:16px; }}
    .panel p {{ margin:0; font-size:12px; color:#d1d5db; }}
    table {{ border-collapse: collapse; }}
    th, td {{ border: 1px solid #ddd; padding: 3px 5px; font-size: 12px; }}
  </style>
</head>
<body>
<div id="map"></div>
<div class="panel"><h1>{html.escape(title)}</h1><p>green=real, gray=old SUMO, blue=fixed/generated crossing. Lines connect points.</p></div>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script>
const data = {payload};
const map = L.map('map').setView([{center[0]}, {center[1]}], 15);
L.tileLayer('https://{{s}}.tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png', {{maxZoom: 19}}).addTo(map);
function popup(r) {{
  const rows = ['crosswalk_id','original_rank','mapping_distance_m','tls_id','ped_link_index','pedestrian_only_phase_available','final_recovery_status','recommended_action']
    .map(k => `<tr><th>${{k}}</th><td>${{r[k] ?? ''}}</td></tr>`).join('');
  return `<table>${{rows}}</table>`;
}}
const bounds = [];
for (const r of data) {{
  if (r.real_lat !== '' && r.real_lon !== '') {{
    L.circleMarker([r.real_lat, r.real_lon], {{radius:7,color:'#16a34a',fillOpacity:.9}}).addTo(map).bindPopup(popup(r));
    bounds.push([r.real_lat, r.real_lon]);
  }}
  if (r.old_lat !== '' && r.old_lon !== '') {{
    L.circleMarker([r.old_lat, r.old_lon], {{radius:6,color:'#6b7280',fillOpacity:.7}}).addTo(map);
    bounds.push([r.old_lat, r.old_lon]);
  }}
  if (r.fixed_lat !== '' && r.fixed_lon !== '') {{
    L.circleMarker([r.fixed_lat, r.fixed_lon], {{radius:6,color:'#2563eb',fillOpacity:.9}}).addTo(map);
    bounds.push([r.fixed_lat, r.fixed_lon]);
  }}
  if (r.real_lat !== '' && r.fixed_lat !== '') L.polyline([[r.real_lat,r.real_lon],[r.fixed_lat,r.fixed_lon]], {{color:'#f97316',weight:2}}).addTo(map);
  if (r.old_lat !== '' && r.fixed_lat !== '') L.polyline([[r.old_lat,r.old_lon],[r.fixed_lat,r.fixed_lon]], {{color:'#64748b',weight:1,dashArray:'4 4'}}).addTo(map);
}}
if (bounds.length) map.fitBounds(bounds, {{padding:[40,40]}});
</script>
</body>
</html>
"""
    out_html.write_text(html_doc, encoding="utf-8")


def _final_status(out_dir: Path, signal_verdict: pd.DataFrame, gen_verdict: pd.DataFrame) -> None:
    combined = _read_csv(TOP50_COMBINED)
    status_rows: list[dict[str, Any]] = []
    signal_ready = set(signal_verdict[signal_verdict.get("ready_for_30seed", False).astype(bool)]["crosswalk_id"].astype(str)) if not signal_verdict.empty else set()
    gen_ready = set(gen_verdict[gen_verdict.get("ready_for_30seed", False).astype(bool)]["crosswalk_id"].astype(str)) if not gen_verdict.empty else set()
    for _, row in combined.sort_values("original_rank").iterrows():
        cid = _norm(row.get("crosswalk_id"))
        label = "NEEDS_MANUAL_REVIEW"
        if _norm(row.get("final_screening_status")) == "FINAL_GOOD_MAPPING_AND_GOOD_RESULT":
            label = "MAIN_READY_CURRENT_NET"
        elif cid in signal_ready:
            label = "RECOVERED_SIGNAL_FIX_READY"
        elif cid in gen_ready:
            label = "RECOVERED_GENERATED_SIGNAL_READY"
        elif _norm(row.get("final_screening_status")) in {"RESULT_OK_BUT_LOCATION_BAD", "RESULT_OK_BUT_LOCATION_REVIEW"}:
            label = "NEEDS_MANUAL_REVIEW"
        elif _norm(row.get("final_screening_status")) == "NOT_FINAL_CANDIDATE":
            label = "REJECT"
        status_rows.append(
            {
                "crosswalk_id": cid,
                "original_rank": _int_or_blank(row.get("original_rank")),
                "previous_status": _norm(row.get("final_screening_status")),
                "final_recovery_status": label,
                "smart_smoke_success": cid in signal_ready or cid in gen_ready or _norm(row.get("final_screening_status")) == "FINAL_GOOD_MAPPING_AND_GOOD_RESULT",
                "baseline_smoke_success": cid in signal_ready or cid in gen_ready or _norm(row.get("final_screening_status")) == "FINAL_GOOD_MAPPING_AND_GOOD_RESULT",
                "ready_for_30seed": label in {"MAIN_READY_CURRENT_NET", "RECOVERED_SIGNAL_FIX_READY", "RECOVERED_GENERATED_SIGNAL_READY"},
                "reason": "main preserved" if label == "MAIN_READY_CURRENT_NET" else "seed1 smoke passed" if label.startswith("RECOVERED") else _norm(row.get("reason")),
            }
        )
    status_df = pd.DataFrame(status_rows)
    _write_csv(out_dir / "top50_recovery_status_after_signal_and_crossing_patch.csv", status_df)
    ready_total = int(status_df["ready_for_30seed"].sum())
    lines = [
        "# top50 recovery status summary",
        "",
        f"- main_ready_current_net: {int((status_df['final_recovery_status'] == 'MAIN_READY_CURRENT_NET').sum())}",
        f"- recovered_signal_fix_ready: {int((status_df['final_recovery_status'] == 'RECOVERED_SIGNAL_FIX_READY').sum())}",
        f"- recovered_generated_signal_ready: {int((status_df['final_recovery_status'] == 'RECOVERED_GENERATED_SIGNAL_READY').sum())}",
        f"- final_30seed_candidate_total: {ready_total}",
        "- separate_baseline_required_net_versions: 2 if both signal/generated candidate tables non-empty; each patched net needs its own baseline",
        "",
        "## status counts",
    ]
    for status, count in status_df["final_recovery_status"].value_counts().items():
        lines.append(f"- {status}: {int(count)}")
    (out_dir / "top50_recovery_status_summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    ready = status_df[status_df["ready_for_30seed"].astype(bool)].copy()
    _write_csv(out_dir / "recovered_ready_for_30seed_candidates.csv", ready)


def main() -> None:
    parser = argparse.ArgumentParser(description="Recover maximum Top50 smart-crosswalk installable candidates without touching main net.")
    parser.add_argument("--output-dir", default="", help="Optional output directory. Default: timestamped result dir.")
    parser.add_argument("--run-smoke", action="store_true", help="Run seed1 smart/baseline smoke for generated candidate tables.")
    args = parser.parse_args()

    apply_sumo_environment()
    out_dir = Path(args.output_dir).resolve() if args.output_dir else RESULT_DIR / f"phase_next_top50_max_installation_recovery_{_now_tag()}"
    out_dir.mkdir(parents=True, exist_ok=True)

    if not BASE_NET.exists():
        raise FileNotFoundError(BASE_NET)
    top50_map = _read_csv(TOP50_MAP)
    base_rows = top50_map.rename(columns={"sumo_lon": "current_sumo_lon", "sumo_lat": "current_sumo_lat"}).copy()
    base_rows["ped_link_indices"] = base_rows["ped_link_index"].apply(lambda v: json.dumps([int(v)]) if _int_or_blank(v) != "" else "[]")
    base_audit = _audit_candidates(BASE_NET, base_rows)
    _make_splits(out_dir, base_audit)

    signal = _build_signal_fix(out_dir, top50_map)
    generated = _build_generated_signal(out_dir, top50_map)

    sig_cmd = _write_smoke_command(out_dir, "signal_fix", signal["candidate_table"], signal["net"])
    gen_cmd = _write_smoke_command(out_dir, "generated_signal", generated["candidate_table"], generated["net"])
    sig_verdict = _verdict_from_smoke(out_dir, "signal_fix", signal["candidate_table"], args.run_smoke)
    gen_verdict = _verdict_from_smoke(out_dir, "generated_signal", generated["candidate_table"], args.run_smoke)
    _write_csv(out_dir / "signal_fix_seed1_smoke_verdict.csv", sig_verdict)
    _write_csv(out_dir / "generated_signal_seed1_smoke_verdict.csv", gen_verdict)

    _render_map(out_dir / "signal_fix_net_v1_map_overlay.html", "signal_fix_net_v1 map overlay", pd.read_csv(out_dir / "signal_fix_targets_9.csv"), signal["audit"])
    _render_map(
        out_dir / "generated_signal_net_v1_map_overlay.html",
        "generated_signal_net_v1 map overlay",
        pd.read_csv(out_dir / "generated_signal_targets_6.csv"),
        generated["audit"],
    )
    _final_status(out_dir, sig_verdict, gen_verdict)

    metadata = {
        "output_dir": str(out_dir),
        "base_net": str(BASE_NET),
        "run_smoke": bool(args.run_smoke),
        "signal_fix_candidate_rows": int(len(pd.read_csv(signal["candidate_table"])) if signal["candidate_table"].exists() else 0),
        "generated_signal_candidate_rows": int(len(pd.read_csv(generated["candidate_table"])) if generated["candidate_table"].exists() else 0),
        "signal_fix_seed1_ready": int(sig_verdict["ready_for_30seed"].sum()) if not sig_verdict.empty else 0,
        "generated_signal_seed1_ready": int(gen_verdict["ready_for_30seed"].sum()) if not gen_verdict.empty else 0,
        "commands": [str(sig_cmd), str(gen_cmd)],
    }
    (out_dir / "run_metadata.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"output_dir: {out_dir}")
    print(f"signal_fix_candidate_rows: {metadata['signal_fix_candidate_rows']}")
    print(f"generated_signal_candidate_rows: {metadata['generated_signal_candidate_rows']}")
    print(f"signal_fix_seed1_ready: {metadata['signal_fix_seed1_ready']}")
    print(f"generated_signal_seed1_ready: {metadata['generated_signal_seed1_ready']}")
    print(f"signal_fix_smoke_command: {sig_cmd}")
    print(f"generated_signal_smoke_command: {gen_cmd}")


if __name__ == "__main__":
    main()
