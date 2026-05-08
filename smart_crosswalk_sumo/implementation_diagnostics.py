from __future__ import annotations

import argparse
import math
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

import pandas as pd

try:
    from .output_schema import write_csv_utf8_sig
except ImportError:
    from output_schema import write_csv_utf8_sig


def _as_str(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and math.isnan(value):
        return ""
    return str(value).strip()


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    text = _as_str(value).lower()
    return text in {"1", "true", "yes", "y"}


def _haversine_m(lon1: float, lat1: float, lon2: float, lat2: float) -> float:
    r = 6_371_000.0
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2.0) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlmb / 2.0) ** 2
    return 2.0 * r * math.asin(math.sqrt(max(0.0, min(1.0, a))))


def _read_tls_topology(net_file: Path) -> tuple[set[str], set[str], dict[str, str]]:
    root = ET.parse(net_file).getroot()
    tl_logic_ids = {
        _as_str(node.attrib.get("id"))
        for node in root.findall("tlLogic")
        if _as_str(node.attrib.get("id"))
    }
    connection_tl_ids = {
        _as_str(node.attrib.get("tl"))
        for node in root.findall("connection")
        if _as_str(node.attrib.get("tl"))
    }
    junction_types = {
        _as_str(node.attrib.get("id")): _as_str(node.attrib.get("type"))
        for node in root.findall("junction")
        if _as_str(node.attrib.get("id"))
    }
    return tl_logic_ids, connection_tl_ids, junction_types


def _collect_netconvert_ghost_tls(netconvert_log: Path) -> set[str]:
    if not netconvert_log.exists():
        return set()
    ghosts: set[str] = set()
    marker = "does not control any links; it will not be build"
    for line in netconvert_log.read_text(encoding="utf-8", errors="ignore").splitlines():
        if marker not in line:
            continue
        # Example: Warning: The traffic light '436875645' does not control any links; it will not be build.
        parts = line.split("'")
        if len(parts) >= 2:
            tl_id = _as_str(parts[1])
            if tl_id:
                ghosts.add(tl_id)
    return ghosts


def _classify_c_subtype(row: dict[str, Any], ghost_tls_ids: set[str]) -> str:
    failure_reason = _as_str(row.get("failure_reason"))
    invalid_reason = _as_str(row.get("invalid_reason"))
    tls_id = _as_str(row.get("tls_id"))
    junction_type = _as_str(row.get("junction_type"))
    tl_logic_exists = _as_bool(row.get("tlLogic_exists_in_net_xml"))
    path_exists = _as_bool(row.get("path_exists"))
    crossing_edge_exists = _as_bool(row.get("crossing_edge_exists"))
    from_edge_exists = _as_bool(row.get("from_edge_exists"))
    to_edge_exists = _as_bool(row.get("to_edge_exists"))

    if "distance_exceeded" in failure_reason:
        return "C1_distance_match_failed"
    if tls_id and tls_id in ghost_tls_ids:
        return "C3_ghost_tls"
    if junction_type == "traffic_light" and not tl_logic_exists:
        return "C3_ghost_tls"
    if "missing_tls" in failure_reason or (not tls_id and "missing_tls" in invalid_reason):
        return "C2_no_tllogic"
    if not crossing_edge_exists:
        return "C4_missing_crossing"
    if not from_edge_exists or not to_edge_exists:
        return "C5_walkingarea_edge_missing"
    if not path_exists or "no_pedestrian_path" in invalid_reason or "path_missing_crossing_edge" in invalid_reason:
        return "C6_ped_route_failed"
    if "error" in _as_str(row.get("validation_status")).lower():
        return "C12_data_or_topology_review"
    return "C11_manual_netedit_required"


def _grade_from_status(status: str, usable: bool) -> str:
    if usable and status == "ready_for_simulation":
        return "A"
    if status == "route_repair_success":
        return "B"
    return "C"


def _auto_recovery_flag(c_subtype: str, match_distance_m: float | None) -> bool:
    if c_subtype == "C1_distance_match_failed":
        if match_distance_m is None:
            return False
        return float(match_distance_m) <= 100.0
    return c_subtype in {
        "C2_no_tllogic",
        "C3_ghost_tls",
        "C5_walkingarea_edge_missing",
        "C6_ped_route_failed",
    }


def _to_float(value: Any) -> float | None:
    try:
        if value is None:
            return None
        num = float(value)
        if math.isnan(num):
            return None
        return num
    except Exception:
        return None


def generate_implementation_diagnostics(
    output_dir: str | Path,
    nets_dir: str | Path,
) -> dict[str, Path]:
    out_dir = Path(output_dir)
    net_root = Path(nets_dir) / "integrated_selected"
    audit_path = out_dir / "crosswalk_sumo_implementation_audit.csv"
    if not audit_path.exists():
        raise FileNotFoundError(f"진단 입력 파일이 없습니다: {audit_path}")

    net_file = net_root / "network.net.xml"
    if not net_file.exists():
        raise FileNotFoundError(f"통합 네트워크 파일이 없습니다: {net_file}")
    netconvert_log = net_root / "netconvert.log"

    audit_df = pd.read_csv(audit_path)
    tl_logic_ids, connection_tl_ids, junction_types = _read_tls_topology(net_file)
    ghost_tls_ids = _collect_netconvert_ghost_tls(netconvert_log)

    enriched_rows: list[dict[str, Any]] = []
    for row in audit_df.to_dict(orient="records"):
        tls_id = _as_str(row.get("tls_id"))
        status = _as_str(row.get("implementation_status"))
        usable = _as_bool(row.get("final_usable_for_simulation"))
        grade = _grade_from_status(status, usable)
        tl_logic_exists = bool(tls_id and tls_id in tl_logic_ids)
        tl_conn_exists = bool(tls_id and tls_id in connection_tl_ids)
        junction_type = junction_types.get(tls_id, "")
        merged = dict(row)
        merged["tls_id"] = tls_id
        merged["grade"] = grade
        merged["junction_type"] = junction_type
        merged["tlLogic_exists_in_net_xml"] = tl_logic_exists
        merged["tl_id_exists_in_connection_tl"] = tl_conn_exists
        merged["is_netconvert_ghost_tls"] = bool(tls_id and tls_id in ghost_tls_ids)
        if grade == "C":
            c_subtype = _classify_c_subtype(merged, ghost_tls_ids)
            merged["c_subtype"] = c_subtype
            merged["can_auto_recover"] = _auto_recovery_flag(
                c_subtype,
                _to_float(merged.get("initial_match_distance_m")),
            )
            merged["requires_manual_netedit"] = not bool(merged["can_auto_recover"])
        else:
            merged["c_subtype"] = ""
            merged["can_auto_recover"] = False
            merged["requires_manual_netedit"] = False
        enriched_rows.append(merged)

    diag_df = pd.DataFrame(enriched_rows)

    candidate_matching_cols = [
        "crosswalk_id",
        "lon",
        "lat",
        "dong_name",
        "admin_dong",
        "crossing_edge",
        "crossing_lon",
        "crossing_lat",
        "initial_match_distance_m",
        "repaired_match_distance_m",
        "implementation_status",
        "final_usable_for_simulation",
        "grade",
        "c_subtype",
        "failure_reason",
        "repair_suggestion",
        "can_auto_recover",
        "requires_manual_netedit",
    ]
    candidate_matching_df = diag_df[[c for c in candidate_matching_cols if c in diag_df.columns]].copy()

    tls_cols = [
        "crosswalk_id",
        "tls_id",
        "junction_type",
        "tlLogic_exists_in_net_xml",
        "tl_id_exists_in_connection_tl",
        "is_netconvert_ghost_tls",
        "implementation_status",
        "grade",
        "c_subtype",
        "failure_reason",
    ]
    tls_df = diag_df[[c for c in tls_cols if c in diag_df.columns]].copy()

    ped_cols = [
        "crosswalk_id",
        "crossing_edge",
        "from_edge",
        "to_edge",
        "crossing_edge_exists",
        "from_edge_exists",
        "to_edge_exists",
        "from_edge_allows_pedestrian",
        "to_edge_allows_pedestrian",
        "incoming_walkingarea_count",
        "outgoing_walkingarea_count",
        "path_exists",
        "path_uses_crossing",
        "path_uses_walkingarea",
        "path_cost",
        "path_edge_count",
        "validation_status",
        "invalid_reason",
        "grade",
        "c_subtype",
    ]
    ped_df = diag_df[[c for c in ped_cols if c in diag_df.columns]].copy()

    veh_rows: list[dict[str, Any]] = []
    for row in diag_df.to_dict(orient="records"):
        veh_rows.append(
            {
                "crosswalk_id": _as_str(row.get("crosswalk_id")),
                "crossing_edge": _as_str(row.get("crossing_edge")),
                "tls_id": _as_str(row.get("tls_id")),
                "tlLogic_exists_in_net_xml": _as_bool(row.get("tlLogic_exists_in_net_xml")),
                "tl_id_exists_in_connection_tl": _as_bool(row.get("tl_id_exists_in_connection_tl")),
                "implementation_status": _as_str(row.get("implementation_status")),
                "grade": _as_str(row.get("grade")),
                "c_subtype": _as_str(row.get("c_subtype")),
                "failure_reason": _as_str(row.get("failure_reason")),
            }
        )
    veh_df = pd.DataFrame(veh_rows)

    summary_rows: list[dict[str, Any]] = []
    for grade, group in diag_df.groupby("grade", dropna=False):
        summary_rows.append(
            {
                "grade": _as_str(grade),
                "count": int(len(group)),
            }
        )
    if "c_subtype" in diag_df.columns:
        for subtype, group in diag_df[diag_df["grade"] == "C"].groupby("c_subtype", dropna=False):
            summary_rows.append(
                {
                    "grade": "C",
                    "c_subtype": _as_str(subtype),
                    "count": int(len(group)),
                }
            )
    summary_df = pd.DataFrame(summary_rows)

    recon_rows: list[dict[str, Any]] = []
    for row in diag_df[diag_df["grade"] == "C"].to_dict(orient="records"):
        csv_lat = _to_float(row.get("lat"))
        csv_lon = _to_float(row.get("lon"))
        crossing_lat = _to_float(row.get("crossing_lat"))
        crossing_lon = _to_float(row.get("crossing_lon"))
        distance_m = _to_float(row.get("initial_match_distance_m"))
        if distance_m is None and None not in (csv_lat, csv_lon, crossing_lat, crossing_lon):
            distance_m = _haversine_m(float(csv_lon), float(csv_lat), float(crossing_lon), float(crossing_lat))
        recon_rows.append(
            {
                "smart_crosswalk_id": _as_str(row.get("crosswalk_id")),
                "csv_lat": csv_lat,
                "csv_lon": csv_lon,
                "nearest_sumo_node": _as_str(row.get("tls_id")),
                "nearest_sumo_edge": _as_str(row.get("crossing_edge")),
                "nearest_tls_candidate": _as_str(row.get("tls_id")),
                "nearest_valid_tllogic": _as_str(row.get("tls_id")) if _as_bool(row.get("tlLogic_exists_in_net_xml")) else "",
                "distance_m": distance_m,
                "implementation_status": _as_str(row.get("implementation_status")),
                "failure_reason": _as_str(row.get("failure_reason")),
                "suggested_action": _as_str(row.get("repair_suggestion")) or "netedit/plain XML reconstruction",
                "netedit_priority": "high" if not _as_bool(row.get("can_auto_recover")) else "medium",
                "expected_recovery_difficulty": "high" if not _as_bool(row.get("can_auto_recover")) else "medium",
                "can_auto_recover": _as_bool(row.get("can_auto_recover")),
                "requires_manual_netedit": _as_bool(row.get("requires_manual_netedit")),
                "notes": _as_str(row.get("c_subtype")),
            }
        )
    recon_df = pd.DataFrame(recon_rows)
    auto_recovery_df = recon_df[recon_df["can_auto_recover"] == True].copy()
    manual_netedit_df = recon_df[recon_df["requires_manual_netedit"] == True].copy()

    paths: dict[str, Path] = {}
    paths["candidate_matching_audit"] = out_dir / "candidate_matching_audit.csv"
    paths["tls_validation_audit"] = out_dir / "tls_validation_audit.csv"
    paths["pedestrian_connectivity_audit"] = out_dir / "pedestrian_connectivity_audit.csv"
    paths["vehicle_connectivity_audit"] = out_dir / "vehicle_connectivity_audit.csv"
    paths["implementation_tier_diagnostics"] = out_dir / "implementation_tier_diagnostics.csv"
    paths["implementation_tier_summary"] = out_dir / "implementation_tier_summary.csv"
    paths["reconstruction_required_candidates"] = out_dir / "reconstruction_required_candidates.csv"
    paths["auto_recovery_candidates"] = out_dir / "auto_recovery_candidates.csv"
    paths["manual_netedit_candidates"] = out_dir / "manual_netedit_candidates.csv"

    write_csv_utf8_sig(candidate_matching_df, paths["candidate_matching_audit"])
    write_csv_utf8_sig(tls_df, paths["tls_validation_audit"])
    write_csv_utf8_sig(ped_df, paths["pedestrian_connectivity_audit"])
    write_csv_utf8_sig(veh_df, paths["vehicle_connectivity_audit"])
    write_csv_utf8_sig(diag_df, paths["implementation_tier_diagnostics"])
    write_csv_utf8_sig(summary_df, paths["implementation_tier_summary"])
    write_csv_utf8_sig(recon_df, paths["reconstruction_required_candidates"])
    write_csv_utf8_sig(auto_recovery_df, paths["auto_recovery_candidates"])
    write_csv_utf8_sig(manual_netedit_df, paths["manual_netedit_candidates"])

    smoke_lines = [
        "# Integrated Smoke Test Report",
        "",
        f"- total_candidates: {len(diag_df)}",
        f"- grade_A: {int((diag_df['grade'] == 'A').sum())}",
        f"- grade_B: {int((diag_df['grade'] == 'B').sum())}",
        f"- grade_C: {int((diag_df['grade'] == 'C').sum())}",
        f"- auto_recovery_candidates: {len(auto_recovery_df)}",
        f"- manual_netedit_candidates: {len(manual_netedit_df)}",
        "",
    ]
    smoke_path = out_dir / "integrated_smoke_test_report.md"
    smoke_path.write_text("\n".join(smoke_lines), encoding="utf-8")
    paths["integrated_smoke_test_report"] = smoke_path

    final_lines = [
        "# Final Simulation Readiness Report",
        "",
        "본 분석은 670개 전체 후보를 진단한 뒤, 구현 가능한 후보만 정밀 시뮬레이션 대상으로 분리하는 구조를 따른다.",
        "",
        f"- implementation_diagnostics_rows: {len(diag_df)}",
        f"- ready_grade_A: {int((diag_df['grade'] == 'A').sum())}",
        f"- route_repair_grade_B: {int((diag_df['grade'] == 'B').sum())}",
        f"- reconstruction_grade_C: {int((diag_df['grade'] == 'C').sum())}",
        f"- auto_recovery_pool: {len(auto_recovery_df)}",
        f"- manual_reconstruction_pool: {len(manual_netedit_df)}",
        "",
        "결론: 670개 중 133개만 쓰는 프로젝트가 아니라, 670개 전체를 진단하고 구현 가능한 후보를 시뮬레이션하는 프로젝트다.",
    ]
    final_path = out_dir / "final_simulation_readiness_report.md"
    final_path.write_text("\n".join(final_lines), encoding="utf-8")
    paths["final_simulation_readiness_report"] = final_path
    return paths


def main() -> None:
    parser = argparse.ArgumentParser(description="SUMO 스마트 횡단보도 구현 진단 산출물 생성")
    parser.add_argument("--output_dir", required=True)
    parser.add_argument("--nets_dir", required=True)
    args = parser.parse_args()
    paths = generate_implementation_diagnostics(args.output_dir, args.nets_dir)
    for name, path in paths.items():
        print(f"{name}: {path}")


if __name__ == "__main__":
    main()
