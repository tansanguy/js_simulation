from __future__ import annotations

import argparse
import math
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

import pandas as pd
import sumolib

try:
    import traci  # type: ignore
except Exception:  # pragma: no cover
    traci = None

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


def _as_float(value: Any) -> float | None:
    try:
        if value is None:
            return None
        num = float(value)
        if math.isnan(num):
            return None
        return num
    except Exception:
        return None


def _edge_cluster(edge_id: str) -> str:
    eid = _as_str(edge_id)
    if not eid:
        return ""
    if eid.startswith(":"):
        return eid
    eid = eid.lstrip("-")
    return eid.split("#", 1)[0]


def _internal_junction_id(edge_id: str) -> str:
    eid = _as_str(edge_id)
    if not eid.startswith(":"):
        return ""
    core = eid[1:]
    if "_" not in core:
        return ""
    return core.split("_", 1)[0]


def _collect_ghost_tls(netconvert_log: Path) -> set[str]:
    if not netconvert_log.exists():
        return set()
    marker = "does not control any links; it will not be build"
    out: set[str] = set()
    for line in netconvert_log.read_text(encoding="utf-8", errors="ignore").splitlines():
        if marker not in line:
            continue
        parts = line.split("'")
        if len(parts) >= 2:
            tl = _as_str(parts[1])
            if tl:
                out.add(tl)
    return out


def _joined_tls_clusters(tl_logic_ids: set[str]) -> list[set[str]]:
    clusters: list[set[str]] = []
    for tl in tl_logic_ids:
        if not tl.startswith("joinedS_"):
            continue
        ids = [tok for tok in tl.split("_") if tok and tok != "joinedS"]
        clusters.append(set(ids))
    return clusters


def _shares_junction_cluster(j1: str, j2: str, clusters: list[set[str]]) -> bool:
    if not j1 or not j2:
        return False
    if j1 == j2:
        return True
    for cluster in clusters:
        if j1 in cluster and j2 in cluster:
            return True
    return False


def _find_sumocfg(net_dir: Path, seed: int) -> Path | None:
    for name in [f"baseline_seed{seed}.sumocfg", f"smart_selected_seed{seed}.sumocfg", "baseline_seed42.sumocfg"]:
        p = net_dir / name
        if p.exists():
            return p
    return None


def _snapshot_traci_tls_ids(net_dir: Path, seed: int) -> tuple[set[str], str]:
    if traci is None:
        return set(), "traci_unavailable"
    sumocfg = _find_sumocfg(net_dir, seed)
    temp_cfg_path: Path | None = None
    if sumocfg is None:
        net_file = net_dir / "network.net.xml"
        if not net_file.exists():
            return set(), "sumocfg_not_found"
        temp_cfg_path = net_dir / f"_tls_probe_seed{seed}.sumocfg"
        cfg_text = f"""<configuration>
    <input>
        <net-file value="{net_file.name}"/>
    </input>
    <time>
        <begin value="0"/>
        <end value="1"/>
        <step-length value="0.1"/>
    </time>
</configuration>
"""
        temp_cfg_path.write_text(cfg_text, encoding="utf-8")
        sumocfg = temp_cfg_path
    label = f"recovery_probe_{seed}"
    try:
        traci.start(
            [
                "sumo",
                "-c",
                str(sumocfg),
                "--seed",
                str(seed),
                "--step-length",
                "0.1",
                "--no-warnings",
                "--no-step-log",
            ],
            label=label,
        )
        traci.switch(label)
        try:
            traci.simulationStep()
        except Exception:
            pass
        return set(traci.trafficlight.getIDList()), "ok"
    except Exception as exc:
        return set(), f"traci_probe_failed:{exc}"
    finally:
        try:
            traci.switch(label)
            traci.close(False)
        except Exception:
            pass
        if temp_cfg_path is not None:
            try:
                temp_cfg_path.unlink(missing_ok=True)
            except Exception:
                pass


def _build_crossing_maps(root: ET.Element) -> tuple[dict[str, list[str]], dict[str, set[str]]]:
    crossing_edges = {
        _as_str(edge.attrib.get("id"))
        for edge in root.findall("edge")
        if _as_str(edge.attrib.get("function")) == "crossing"
    }
    crossing_to_vehicle_edges: dict[str, list[str]] = {}
    for edge in root.findall("edge"):
        eid = _as_str(edge.attrib.get("id"))
        if eid not in crossing_edges:
            continue
        raw = _as_str(edge.attrib.get("crossingEdges"))
        crossing_to_vehicle_edges[eid] = [tok for tok in raw.split() if tok]

    crossing_to_tls: dict[str, set[str]] = {eid: set() for eid in crossing_edges}
    for conn in root.findall("connection"):
        tl = _as_str(conn.attrib.get("tl"))
        if not tl:
            continue
        f = _as_str(conn.attrib.get("from"))
        t = _as_str(conn.attrib.get("to"))
        if f in crossing_to_tls:
            crossing_to_tls[f].add(tl)
        if t in crossing_to_tls:
            crossing_to_tls[t].add(tl)
    return crossing_to_vehicle_edges, crossing_to_tls


def _candidate_crossings_within(
    net: sumolib.net.Net,
    lon: float,
    lat: float,
    radius_m: float,
) -> list[tuple[str, float]]:
    x, y = net.convertLonLat2XY(lon, lat)
    nearby = net.getNeighboringEdges(x, y, radius_m, includeJunctions=False)
    by_id: dict[str, float] = {}
    for edge, dist in nearby:
        if edge.getFunction() != "crossing":
            continue
        eid = edge.getID()
        prev = by_id.get(eid)
        if prev is None or dist < prev:
            by_id[eid] = float(dist)
    return sorted(by_id.items(), key=lambda item: item[1])


def _shortest_path_uses_crossing(
    net: sumolib.net.Net,
    from_edge_id: str,
    to_edge_id: str,
    crossing_edge_id: str,
) -> tuple[bool, bool, str]:
    from_edge_id = _as_str(from_edge_id)
    to_edge_id = _as_str(to_edge_id)
    crossing_edge_id = _as_str(crossing_edge_id)
    if not from_edge_id or not to_edge_id or not crossing_edge_id:
        return False, False, "missing_from_to_or_crossing"
    try:
        from_edge = net.getEdge(from_edge_id)
        to_edge = net.getEdge(to_edge_id)
    except Exception:
        return False, False, "missing_from_or_to_edge_in_net"
    try:
        path = net.getShortestPath(from_edge, to_edge, vClass="pedestrian")
    except Exception as exc:
        return False, False, f"ped_shortest_path_error:{exc}"
    if not path or not path[0]:
        return False, False, "no_pedestrian_path"
    edge_ids = [edge.getID() for edge in path[0]]
    uses_crossing = crossing_edge_id in edge_ids
    uses_walkingarea = any(eid.startswith(":") and "_w" in eid for eid in edge_ids)
    if not uses_crossing:
        return False, uses_walkingarea, "path_missing_target_crossing"
    return True, uses_walkingarea, "ok"


def generate_c_recovery_reports(
    output_dir: str | Path,
    nets_dir: str | Path,
    seed: int = 42,
) -> dict[str, Path]:
    out_dir = Path(output_dir)
    net_dir = Path(nets_dir) / "integrated_selected"
    net_file = net_dir / "network.net.xml"
    netconvert_log = net_dir / "netconvert.log"

    auto_path = out_dir / "auto_recovery_candidates.csv"
    diag_path = out_dir / "implementation_tier_diagnostics.csv"
    manual_path = out_dir / "manual_netedit_candidates.csv"

    if not auto_path.exists():
        raise FileNotFoundError(f"자동 복구 입력 파일이 없습니다: {auto_path}")
    if not diag_path.exists():
        raise FileNotFoundError(f"진단 입력 파일이 없습니다: {diag_path}")
    if not net_file.exists():
        raise FileNotFoundError(f"통합 네트워크 파일이 없습니다: {net_file}")

    auto_df = pd.read_csv(auto_path)
    diag_df = pd.read_csv(diag_path)
    manual_df = pd.read_csv(manual_path) if manual_path.exists() else pd.DataFrame()

    net = sumolib.net.readNet(str(net_file), withInternal=True, withPedestrianConnections=True)
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
    ghost_tls_ids = _collect_ghost_tls(netconvert_log)
    joined_clusters = _joined_tls_clusters(tl_logic_ids)
    traci_tls_ids, traci_probe_status = _snapshot_traci_tls_ids(net_dir, seed)

    crossing_to_vehicle_edges, crossing_to_tls = _build_crossing_maps(root)

    diag_by_id = {
        _as_str(row.get("crosswalk_id")): row
        for row in diag_df.to_dict(orient="records")
    }

    attempt_rows: list[dict[str, Any]] = []
    auto_recovered_rows: list[dict[str, Any]] = []
    failed_auto_rows: list[dict[str, Any]] = []
    tls_remap_rows: list[dict[str, Any]] = []

    for row in auto_df.to_dict(orient="records"):
        cw_id = _as_str(row.get("smart_crosswalk_id"))
        diag_row = diag_by_id.get(cw_id, {})
        lon = _as_float(row.get("csv_lon"))
        lat = _as_float(row.get("csv_lat"))

        base_crossing = _as_str(diag_row.get("crossing_edge")) or _as_str(row.get("nearest_sumo_edge"))
        base_tls = _as_str(diag_row.get("tls_id")) or _as_str(row.get("nearest_tls_candidate"))
        base_junction = _internal_junction_id(base_crossing)
        base_veh_edges = crossing_to_vehicle_edges.get(base_crossing, [])
        base_edge_clusters = {_edge_cluster(eid) for eid in base_veh_edges if _edge_cluster(eid)}

        chosen: dict[str, Any] | None = None
        scan_notes: list[str] = []

        if lon is not None and lat is not None:
            for radius in (50.0, 75.0, 100.0):
                candidates = _candidate_crossings_within(net, float(lon), float(lat), radius)
                scan_notes.append(f"r{int(radius)}:{len(candidates)}")
                for crossing_id, dist_m in candidates:
                    cand_veh_edges = crossing_to_vehicle_edges.get(crossing_id, [])
                    cand_clusters = {_edge_cluster(eid) for eid in cand_veh_edges if _edge_cluster(eid)}
                    same_edge_cluster = bool(base_edge_clusters and cand_clusters and (base_edge_clusters & cand_clusters))

                    cand_junction = _internal_junction_id(crossing_id)
                    same_junction_cluster = _shares_junction_cluster(base_junction, cand_junction, joined_clusters)

                    # Road-name matching is optional in this network; edge cluster / junction cluster are primary guards.
                    context_ok = bool(same_edge_cluster or same_junction_cluster)
                    if not context_ok:
                        continue

                    uses_crossing, uses_walkingarea, route_reason = _shortest_path_uses_crossing(
                        net,
                        _as_str(diag_row.get("from_edge")),
                        _as_str(diag_row.get("to_edge")),
                        crossing_id,
                    )
                    if not uses_crossing:
                        continue

                    cand_tls_ids = sorted(crossing_to_tls.get(crossing_id, set()))
                    if cand_tls_ids:
                        valid_tls_ids = [
                            tl
                            for tl in cand_tls_ids
                            if tl in tl_logic_ids
                            and tl in connection_tl_ids
                            and tl not in ghost_tls_ids
                            and tl in traci_tls_ids
                        ]
                        control_policy = "controlled"
                        tl_valid = bool(valid_tls_ids)
                    else:
                        valid_tls_ids = []
                        control_policy = "uncontrolled"
                        tl_valid = bool(uses_walkingarea)

                    if not tl_valid:
                        continue

                    chosen = {
                        "crossing_id": crossing_id,
                        "distance_m": float(dist_m),
                        "radius_m": float(radius),
                        "control_policy": control_policy,
                        "resolved_tllogic_id": valid_tls_ids[0] if valid_tls_ids else "",
                        "tls_candidates": "|".join(cand_tls_ids),
                        "route_reason": route_reason,
                        "uses_walkingarea": uses_walkingarea,
                        "same_edge_cluster": same_edge_cluster,
                        "same_junction_cluster": same_junction_cluster,
                    }
                    break
                if chosen:
                    break

        if chosen:
            status = "auto_recovered_distance_rematch"
            simulation_usable = True
            rejected_reason = ""
        else:
            status = "manual_netedit_required"
            simulation_usable = False
            rejected_reason = "no_safe_match_within_100m_or_route_tls_validation_failed"

        attempt_rows.append(
            {
                "smart_crosswalk_id": cw_id,
                "c_subtype": _as_str(diag_row.get("c_subtype")) or _as_str(row.get("notes")),
                "attempt_scope": "auto_recovery_pool",
                "original_tls_id": base_tls,
                "original_crossing_edge": base_crossing,
                "attempted_radii_m": "50|75|100",
                "scan_summary": "|".join(scan_notes),
                "matched_crossing_edge": chosen.get("crossing_id") if chosen else "",
                "recovery_distance_m": chosen.get("distance_m") if chosen else "",
                "matched_radius_m": chosen.get("radius_m") if chosen else "",
                "same_edge_cluster": chosen.get("same_edge_cluster", False),
                "same_junction_cluster": chosen.get("same_junction_cluster", False),
                "resolved_tllogic_id": chosen.get("resolved_tllogic_id") if chosen else "",
                "tls_candidates": chosen.get("tls_candidates") if chosen else "",
                "control_policy": chosen.get("control_policy") if chosen else "",
                "path_uses_crossing": bool(chosen),
                "path_uses_walkingarea": chosen.get("uses_walkingarea", False) if chosen else False,
                "recovery_status": status,
                "simulation_usable": simulation_usable,
                "rejected_reason": rejected_reason,
                "action_taken": "include_in_recovered_targets" if simulation_usable else "route_to_manual_netedit",
            }
        )

        if chosen:
            auto_recovered_rows.append(
                {
                    "smart_crosswalk_id": cw_id,
                    "recovery_status": status,
                    "matched_crossing_edge": chosen["crossing_id"],
                    "recovery_distance_m": chosen["distance_m"],
                    "matched_radius_m": chosen["radius_m"],
                    "resolved_tllogic_id": chosen["resolved_tllogic_id"],
                    "control_policy": chosen["control_policy"],
                    "simulation_usable": True,
                    "action_taken": "eligible_for_integrated_selected",
                }
            )
        else:
            failed_auto_rows.append(
                {
                    "smart_crosswalk_id": cw_id,
                    "original_tls_id": base_tls,
                    "original_crossing_edge": base_crossing,
                    "failure_reason": rejected_reason,
                    "action_taken": "manual_netedit_required",
                    "can_auto_recover": False,
                }
            )

        base_is_ghost = bool(base_tls and base_tls in ghost_tls_ids)
        base_in_net = bool(base_tls and base_tls in tl_logic_ids)
        base_in_traci = bool(base_tls and base_tls in traci_tls_ids)
        resolved_tl = chosen.get("resolved_tllogic_id") if chosen else ""
        tls_remap_rows.append(
            {
                "smart_crosswalk_id": cw_id,
                "original_tls_id": base_tls,
                "original_is_ghost_tls": base_is_ghost,
                "original_exists_in_tllogic": base_in_net,
                "original_exists_in_traci": base_in_traci,
                "resolved_tllogic_id": resolved_tl,
                "resolved_exists_in_tllogic": bool(resolved_tl and resolved_tl in tl_logic_ids),
                "resolved_exists_in_traci": bool(resolved_tl and resolved_tl in traci_tls_ids),
                "remap_status": "resolved" if resolved_tl else "manual_tls_reconstruction_required",
                "action_taken": "use_resolved_tllogic" if resolved_tl else "manual_tls_reconstruction_required",
            }
        )

    c4_df = diag_df[diag_df.get("c_subtype", "") == "C4_missing_crossing"].copy()
    c4_patch_rows: list[dict[str, Any]] = []
    c4_manual_rows: list[dict[str, Any]] = []

    for row in c4_df.to_dict(orient="records"):
        cw_id = _as_str(row.get("crosswalk_id"))
        lon = _as_float(row.get("lon"))
        lat = _as_float(row.get("lat"))

        vehicle_edges: list[tuple[str, float]] = []
        ped_edges: list[tuple[str, float]] = []
        junction_id = ""

        if lon is not None and lat is not None:
            x, y = net.convertLonLat2XY(float(lon), float(lat))
            near = net.getNeighboringEdges(x, y, 100.0, includeJunctions=False)
            for edge, dist in near:
                func = edge.getFunction()
                eid = edge.getID()
                if func in {"internal", "crossing", "walkingarea"}:
                    if func in {"walkingarea", "crossing"}:
                        ped_edges.append((eid, float(dist)))
                    continue
                if edge.allows("pedestrian"):
                    ped_edges.append((eid, float(dist)))
                if edge.allows("passenger") or edge.allows("private"):
                    vehicle_edges.append((eid, float(dist)))

            vehicle_edges = sorted(vehicle_edges, key=lambda item: item[1])
            ped_edges = sorted(ped_edges, key=lambda item: item[1])

            if vehicle_edges:
                n1 = net.getEdge(vehicle_edges[0][0]).getFromNode()
                n2 = net.getEdge(vehicle_edges[0][0]).getToNode()
                nodes = [n1, n2]
                if len(vehicle_edges) > 1:
                    nodes.extend([net.getEdge(vehicle_edges[1][0]).getFromNode(), net.getEdge(vehicle_edges[1][0]).getToNode()])
                best_node = None
                best_dist = None
                for node in nodes:
                    nx, ny = node.getCoord()
                    d = math.hypot(nx - x, ny - y)
                    if best_dist is None or d < best_dist:
                        best_node = node
                        best_dist = d
                if best_node is not None:
                    junction_id = best_node.getID()

        chosen_vehicle = [eid for eid, _ in vehicle_edges[:2]]
        chosen_ped = [eid for eid, _ in ped_edges[:3]]
        can_patch = bool(len(chosen_vehicle) >= 2 and junction_id and chosen_ped)

        patch_row = {
            "smart_crosswalk_id": cw_id,
            "csv_lon": lon,
            "csv_lat": lat,
            "nearest_vehicle_edge_1": chosen_vehicle[0] if len(chosen_vehicle) > 0 else "",
            "nearest_vehicle_edge_2": chosen_vehicle[1] if len(chosen_vehicle) > 1 else "",
            "nearest_ped_edge": chosen_ped[0] if chosen_ped else "",
            "candidate_junction_id": junction_id,
            "candidate_crossing_road_edges": " ".join(chosen_vehicle),
            "plain_con_xml_patch_spec": (
                f'<crossing node="{junction_id}" edges="{" ".join(chosen_vehicle)}"/>' if can_patch else ""
            ),
            "can_generate_plain_con_xml_patch": can_patch,
            "action_taken": "generate_plain_con_xml_patch_candidate" if can_patch else "manual_netedit_required",
            "rejected_reason": "" if can_patch else "insufficient_vehicle_or_ped_context_within_100m",
            "notes": _as_str(row.get("failure_reason")),
        }
        c4_patch_rows.append(patch_row)
        if not can_patch:
            c4_manual_rows.append(
                {
                    "smart_crosswalk_id": cw_id,
                    "failure_reason": "C4_missing_crossing_no_patch_context",
                    "suggested_action": "manual_netedit_required",
                    "requires_manual_netedit": True,
                    "notes": _as_str(row.get("failure_reason")),
                }
            )

    attempt_df = pd.DataFrame(attempt_rows)
    auto_recovered_df = pd.DataFrame(
        auto_recovered_rows,
        columns=[
            "smart_crosswalk_id",
            "recovery_status",
            "matched_crossing_edge",
            "recovery_distance_m",
            "matched_radius_m",
            "resolved_tllogic_id",
            "control_policy",
            "simulation_usable",
            "action_taken",
        ],
    )
    failed_auto_df = pd.DataFrame(
        failed_auto_rows,
        columns=[
            "smart_crosswalk_id",
            "original_tls_id",
            "original_crossing_edge",
            "failure_reason",
            "action_taken",
            "can_auto_recover",
        ],
    )
    tls_remap_df = pd.DataFrame(tls_remap_rows)
    c4_patch_df = pd.DataFrame(c4_patch_rows)

    recovered_target_df = auto_recovered_df.copy()

    manual_updates = []
    if not manual_df.empty:
        manual_updates.extend(manual_df.to_dict(orient="records"))
    manual_updates.extend(failed_auto_df.to_dict(orient="records"))
    manual_updates.extend(c4_manual_rows)
    manual_updated_df = pd.DataFrame(manual_updates)
    if not manual_updated_df.empty and "smart_crosswalk_id" in manual_updated_df.columns:
        manual_updated_df["smart_crosswalk_id"] = manual_updated_df["smart_crosswalk_id"].astype(str)
        manual_updated_df = manual_updated_df.drop_duplicates(subset=["smart_crosswalk_id"], keep="first")

    paths: dict[str, Path] = {
        "c_recovery_attempts": out_dir / "c_recovery_attempts.csv",
        "auto_recovered_candidates": out_dir / "auto_recovered_candidates.csv",
        "failed_auto_recovery_candidates": out_dir / "failed_auto_recovery_candidates.csv",
        "crossing_patch_candidates": out_dir / "crossing_patch_candidates.csv",
        "tls_remap_candidates": out_dir / "tls_remap_candidates.csv",
        "manual_netedit_candidates_updated": out_dir / "manual_netedit_candidates_updated.csv",
        "recovered_simulation_target_list": out_dir / "recovered_simulation_target_list.csv",
        "c_recovery_summary": out_dir / "c_recovery_summary.md",
    }

    write_csv_utf8_sig(attempt_df, paths["c_recovery_attempts"])
    write_csv_utf8_sig(auto_recovered_df, paths["auto_recovered_candidates"])
    write_csv_utf8_sig(failed_auto_df, paths["failed_auto_recovery_candidates"])
    write_csv_utf8_sig(c4_patch_df, paths["crossing_patch_candidates"])
    write_csv_utf8_sig(tls_remap_df, paths["tls_remap_candidates"])
    write_csv_utf8_sig(manual_updated_df, paths["manual_netedit_candidates_updated"])
    write_csv_utf8_sig(recovered_target_df, paths["recovered_simulation_target_list"])

    summary_lines = [
        "# C Recovery Summary",
        "",
        f"- traci_tls_probe_status: {traci_probe_status}",
        f"- auto_recovery_input_rows: {len(auto_df)}",
        f"- auto_recovered_rows: {len(auto_recovered_df)}",
        f"- failed_auto_recovery_rows: {len(failed_auto_df)}",
        f"- c4_missing_crossing_rows: {len(c4_df)}",
        f"- c4_patch_candidates: {int(c4_patch_df['can_generate_plain_con_xml_patch'].sum()) if not c4_patch_df.empty else 0}",
        f"- c4_manual_required: {int((~c4_patch_df['can_generate_plain_con_xml_patch']).sum()) if not c4_patch_df.empty else 0}",
        f"- recovered_simulation_targets: {len(recovered_target_df)}",
        "",
        "검증 원칙: 50/75/100m 단계 탐색 + edge/junction 컨텍스트 일치 + 보행 경로 crossing 통과 + TLS 유효성 확인 후에만 승격.",
    ]
    paths["c_recovery_summary"].write_text("\n".join(summary_lines), encoding="utf-8")
    return paths


def main() -> None:
    parser = argparse.ArgumentParser(description="C 등급 후보 복구 검증 리포트 생성")
    parser.add_argument("--output_dir", required=True)
    parser.add_argument("--nets_dir", required=True)
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()
    paths = generate_c_recovery_reports(args.output_dir, args.nets_dir, seed=args.seed)
    for k, v in paths.items():
        print(f"{k}: {v}")


if __name__ == "__main__":
    main()
