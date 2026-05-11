"""Synthetic pedestrian traffic-light injector for unsignalized crosswalk junctions.

SUMO 없이 순수 Python XML 조작으로 통합 net.xml에 보행 신호 TLS를 추가한다.
Tier 2 후보 (tl= connection 없는 crossing edge) 전용.

사용법:
    python3 -m smart_crosswalk_sumo.synthetic_tls_injector verify \\
        --net_xml result/.../network.net.xml \\
        --registry result/registry_patch_v4_residual/junggu_crosswalk_sumo_registry_final.csv
"""
from __future__ import annotations

import argparse
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any


# SIGNAL_PARAMS와 동일한 기본값 (run_simulations.py SIGNAL_PARAMS 참조)
_DEFAULT_SIGNAL_PARAMS: dict[str, Any] = {
    "cycle_time": 120.0,
    "ped_green_duration": 27.0,
    "yellow_duration": 3.0,
    "all_red_duration": 3.0,
}


def _node_from_crossing_edge(crossing_edge: str) -> str | None:
    """`:NODE_cN` 형태의 crossing edge ID에서 junction node ID를 추출한다."""
    m = re.match(r":(\w+)_c\d+$", str(crossing_edge))
    return m.group(1) if m else None


def _synthetic_tls_id(node_id: str) -> str:
    return str(node_id)


def _compute_offset(node_id: str, cycle_time: float) -> int:
    """node_id 해시 기반 결정론적 offset (신호 동기화 방지)."""
    return int(node_id) % int(cycle_time) if str(node_id).isdigit() else 0


def evaluate_patch_tls_coverage(
    net_xml_path: str | Path,
    crossing_edges: list[str],
) -> dict[str, str]:
    """각 crossing edge의 TLS 커버리지를 분류한다.

    Args:
        net_xml_path: 통합 net.xml 경로.
        crossing_edges: 평가할 crossing edge ID 목록.

    Returns:
        {crossing_edge: "tier1" | "tier2_no_tl_conn" | "tier2_no_tllogic"} 딕셔너리.
    """
    root = ET.parse(str(net_xml_path)).getroot()
    tl_ids_with_logic = {tl.get("id") for tl in root.findall("tlLogic")}
    conn_tl_map: dict[str, str] = {}
    for c in root.findall("connection"):
        tl = c.get("tl")
        to = c.get("to", "")
        if tl and to:
            conn_tl_map[to] = tl

    result: dict[str, str] = {}
    for ce in crossing_edges:
        tl = conn_tl_map.get(str(ce))
        if not tl:
            result[ce] = "tier2_no_tl_conn"
        elif tl not in tl_ids_with_logic:
            result[ce] = "tier2_no_tllogic"
        else:
            result[ce] = "tier1"
    return result


def inject_synthetic_tls(
    net_xml_path: str | Path,
    tier2_crossing_edges: list[str],
    output_path: str | Path,
    signal_params: dict[str, Any] | None = None,
) -> dict[str, dict[str, Any]]:
    """Tier 2 crossing edges에 synthetic 보행자 신호 TLS를 주입한다.

    각 crossing edge의 junction에:
    - junction type을 traffic_light로 변경 (priority인 경우)
    - <tlLogic> 추가 (2-phase: 보행자 녹색 + 적색)
    - 해당 crossing connection에 tl= / linkIndex= 속성 추가

    Args:
        net_xml_path: 원본 통합 net.xml 경로.
        tier2_crossing_edges: synthetic TLS 주입이 필요한 crossing edge ID 목록.
        output_path: 주입된 net.xml 저장 경로.
        signal_params: 신호 타이밍 파라미터 (None이면 _DEFAULT_SIGNAL_PARAMS 사용).

    Returns:
        {crossing_edge: {"tls_id": str, "ped_link_indices": list[int]}} 딕셔너리.
    """
    if not tier2_crossing_edges:
        return {}

    params = {**_DEFAULT_SIGNAL_PARAMS, **(signal_params or {})}
    cycle_time = float(params["cycle_time"])
    ped_green = float(params["ped_green_duration"])
    yellow = float(params["yellow_duration"])
    all_red = float(params["all_red_duration"])
    ped_red = cycle_time - ped_green - yellow - all_red

    ET.register_namespace("", "")
    tree = ET.parse(str(net_xml_path))
    root = tree.getroot()

    # 중복 처리 방지: junction당 한 번만 처리
    processed_nodes: set[str] = set()
    result: dict[str, dict[str, Any]] = {}

    for ce in tier2_crossing_edges:
        node_id = _node_from_crossing_edge(ce)
        if not node_id:
            continue
        tls_id = _synthetic_tls_id(node_id)

        # crossing connection (from=:NODE_wX to=:NODE_cN) 찾기
        # ped가 walkingarea에서 crossing으로 진입하는 방향
        ped_conn = None
        for c in root.findall("connection"):
            to = c.get("to", "")
            frm = c.get("from", "")
            if to == ce and frm.startswith(f":{node_id}_w"):
                ped_conn = c
                break

        if ped_conn is None:
            # 방향 반대로도 시도
            for c in root.findall("connection"):
                frm = c.get("from", "")
                if frm == ce:
                    ped_conn = c
                    break

        if ped_conn is None:
            continue

        # linkIndex 결정: 이 TLS에 이미 할당된 최대 linkIndex + 1
        existing_max = -1
        for c in root.findall("connection"):
            if c.get("tl") == tls_id:
                try:
                    existing_max = max(existing_max, int(c.get("linkIndex", -1)))
                except ValueError:
                    pass
        link_index = existing_max + 1

        # connection에 tl= / linkIndex= 추가
        ped_conn.set("tl", tls_id)
        ped_conn.set("linkIndex", str(link_index))

        if node_id not in processed_nodes:
            processed_nodes.add(node_id)

            # junction type 변경 (priority → traffic_light)
            for j in root.findall("junction"):
                if j.get("id") == node_id:
                    if j.get("type") in ("priority", "right_before_left", "allway_stop"):
                        j.set("type", "traffic_light")
                    j.set("tl", tls_id)
                    break

            # tlLogic이 아직 없는 경우에만 추가
            existing_tl = any(tl.get("id") == tls_id for tl in root.findall("tlLogic"))
            if not existing_tl:
                offset = _compute_offset(node_id, cycle_time)
                # state 길이는 link_index + 1 (이 TLS가 제어하는 링크 수)
                state_len = link_index + 1
                g_state = "G" * state_len
                y_state = "y" * state_len
                r_state = "r" * state_len

                tl_elem = ET.SubElement(root, "tlLogic")
                tl_elem.set("id", tls_id)
                tl_elem.set("type", "static")
                tl_elem.set("programID", "0")
                tl_elem.set("offset", str(offset))

                p1 = ET.SubElement(tl_elem, "phase")
                p1.set("duration", str(int(ped_green)))
                p1.set("state", g_state)

                p2 = ET.SubElement(tl_elem, "phase")
                p2.set("duration", str(int(yellow)))
                p2.set("state", y_state)

                p3 = ET.SubElement(tl_elem, "phase")
                p3.set("duration", str(int(ped_red)))
                p3.set("state", r_state)

                p4 = ET.SubElement(tl_elem, "phase")
                p4.set("duration", str(int(all_red)))
                p4.set("state", r_state)

        result[ce] = {
            "tls_id": tls_id,
            "ped_link_indices": [link_index],
        }

    tree.write(str(output_path), encoding="utf-8", xml_declaration=True)
    return result


def verify_injection(
    net_xml_path: str | Path,
    crossing_edge: str,
    tls_id: str,
) -> bool:
    """SUMO 없이 synthetic TLS 주입 결과를 XML 구조로 검증한다.

    Returns:
        True if (1) tlLogic[tls_id] exists, (2) crossing connection has tl=tls_id.
    """
    root = ET.parse(str(net_xml_path)).getroot()
    has_logic = any(tl.get("id") == tls_id for tl in root.findall("tlLogic"))
    has_conn = any(
        c.get("tl") == tls_id and (c.get("to") == crossing_edge or c.get("from") == crossing_edge)
        for c in root.findall("connection")
    )
    return has_logic and has_conn


# ─── CLI ──────────────────────────────────────────────────────────────────────

def _cmd_verify(args: argparse.Namespace) -> None:
    try:
        import pandas as pd
    except ImportError:
        print("pandas 필요: pip install pandas", file=sys.stderr)
        sys.exit(1)

    net_xml = Path(args.net_xml)
    registry_path = Path(args.registry)

    if not net_xml.exists():
        print(f"net_xml 없음: {net_xml}", file=sys.stderr)
        sys.exit(1)
    if not registry_path.exists():
        print(f"registry 없음: {registry_path}", file=sys.stderr)
        sys.exit(1)

    df = pd.read_csv(str(registry_path))
    target = df[df["control_mode"].isin(["proxy_unsignalized", "unsignalized"])]
    crossing_edges = list(target["crossing_edge"].dropna().unique())

    print(f"\n{'─'*70}")
    print("synthetic_tls_injector — Tier 분류 진단")
    print(f"net_xml: {net_xml}")
    print(f"대상 후보 수: {len(target)}  |  고유 crossing edge 수: {len(crossing_edges)}")
    print(f"{'─'*70}")

    tier_map = evaluate_patch_tls_coverage(str(net_xml), crossing_edges)

    from collections import Counter
    tier_counts = Counter(tier_map.values())
    for tier, cnt in sorted(tier_counts.items()):
        print(f"  {tier}: {cnt}개 crossing edge")

    tier2_ces = [ce for ce, t in tier_map.items() if t.startswith("tier2")]
    tier2_cws = target[target["crossing_edge"].isin(tier2_ces)]
    print(f"\nTier 2 후보 (synthetic TLS 필요): {len(tier2_cws)}개 crosswalk")
    if not tier2_ces:
        print("  → Tier 2 없음: 모두 Tier 1 (registry 재판정만으로 해결 가능)")
    else:
        for ce in tier2_ces:
            node = _node_from_crossing_edge(ce)
            cw_ids = tier2_cws[tier2_cws["crossing_edge"] == ce]["crosswalk_id"].tolist()
            print(f"  crossing_edge={ce}  junction={node}  crosswalk_ids={cw_ids}")

    print(f"\nTier 1 후보: {len(target) - len(tier2_cws)}개 crosswalk")
    print("  → registry 재판정으로 runnable_for_signal_extension=True 승격 가능")
    print(f"{'─'*70}\n")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="synthetic TLS 주입 진단")
    sub = parser.add_subparsers(dest="command")

    p_verify = sub.add_parser("verify", help="Tier 분류 진단 (SUMO 불필요)")
    p_verify.add_argument("--net_xml", required=True)
    p_verify.add_argument("--registry", required=True)

    parsed = parser.parse_args(argv)
    if parsed.command == "verify":
        _cmd_verify(parsed)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
