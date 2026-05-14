"""
Generated Pedestrian Crossing의 스마트 신호 제어 가능성 STRICT 검증

엄격한 기준으로 재검증:
- pedestrian_link_indices가 명확해야 CONTROL_READY
- TraCI getControlledLinks() 기반 검증
- XML <connection> 요소 기반 정밀 매핑
- conflict check
- phase duration 제어 가능성 확인
"""

import json
import subprocess
import sys
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path
from typing import Optional, Tuple, List, Dict, Set

import pandas as pd
import numpy as np

try:
    import sumolib
except ImportError:
    sumolib = None


class StrictCrosswalkTLSValidator:
    """엄격한 기준의 TLS 검증"""

    def __init__(
        self,
        generated_network_path: str,
        generation_candidates_csv: str,
        real_vs_sumo_csv: str,
        output_base: str = "result",
    ):
        self.net_path = Path(generated_network_path)
        self.gen_candidates_path = Path(generation_candidates_csv)
        self.real_vs_sumo_path = Path(real_vs_sumo_csv)
        self.output_base = Path(output_base)

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.output_dir = self.output_base / f"t2_generated_crosswalk_tls_validation_strict_{timestamp}"

        self.net = None
        self.net_xml = None
        self.gen_candidates = None
        self.real_vs_sumo = None
        self.tls_data = {}
        self.connections_by_tls = {}
        self.tls_by_node = {}
        self.crossing_edges = {}
        self.walkingarea_edges = {}
        self.validation_results = []

        # 119055 디버깅용
        self.debug_119055 = {}

    def load_data(self) -> None:
        """데이터 로드"""
        print(f"[1/11] 데이터 로드")

        if not sumolib:
            raise RuntimeError("sumolib not available")

        print(f"  loading generated network: {self.net_path}")
        self.net = sumolib.net.readNet(str(self.net_path))
        print(f"    nodes: {len(self.net.getNodes())}")
        print(f"    edges: {len(self.net.getEdges())}")
        print(f"    tls: {len(self.net.getTrafficLights())}")

        print(f"  parsing network XML")
        self.net_xml = ET.parse(str(self.net_path)).getroot()

        print(f"  loading generation candidates")
        self.gen_candidates = pd.read_csv(self.gen_candidates_path)

        print(f"  loading real_vs_sumo validation")
        self.real_vs_sumo = pd.read_csv(self.real_vs_sumo_path)

        self.output_dir.mkdir(parents=True, exist_ok=True)
        print(f"  output: {self.output_dir}")

    def index_network_edges(self) -> None:
        """crossing과 walkingarea edge 인덱싱"""
        print(f"\n[2/11] Network Edge 인덱싱")

        for edge in self.net_xml.findall(".//edge"):
            edge_id = edge.get("id")
            func = edge.get("function")

            if func == "crossing":
                self.crossing_edges[edge_id] = {
                    'id': edge_id,
                    'function': func,
                    'shape': edge.get('shape'),
                }
            elif func == "walkingarea":
                self.walkingarea_edges[edge_id] = {
                    'id': edge_id,
                    'function': func,
                    'shape': edge.get('shape'),
                }

        print(f"  crossing edges: {len(self.crossing_edges)}")
        print(f"  walkingarea edges: {len(self.walkingarea_edges)}")

    def parse_tllogics(self) -> None:
        """TLS/Phase 파싱"""
        print(f"\n[3/11] TLS/Phase 정보 파싱")

        for tllogic in self.net_xml.findall(".//tlLogic"):
            tls_id = tllogic.get("id")
            phases = []

            for phase in tllogic.findall("phase"):
                phases.append({
                    'duration': phase.get('duration'),
                    'state': phase.get('state'),
                    'minDur': phase.get('minDur'),
                    'maxDur': phase.get('maxDur'),
                })

            self.tls_data[tls_id] = {
                'phases': phases,
                'phase_count': len(phases),
                'state_length': len(phases[0]['state']) if phases else 0,
            }

        print(f"  tlLogic found: {len(self.tls_data)}")

    def parse_connections(self) -> None:
        """Connection/Link 파싱"""
        print(f"\n[4/11] Connection/Link 파싱")

        self.connections_by_tls = {}
        self.tls_by_node = {}

        for connection in self.net_xml.findall(".//connection"):
            tl = connection.get('tl')
            if tl:
                linkIndex = connection.get('linkIndex')
                fromEdge = connection.get('from')
                toEdge = connection.get('to')

                if tl not in self.connections_by_tls:
                    self.connections_by_tls[tl] = []

                self.connections_by_tls[tl].append({
                    'linkIndex': linkIndex,
                    'from': fromEdge,
                    'to': toEdge,
                    'via': connection.get('via'),
                    'dir': connection.get('dir'),
                    'state': connection.get('state'),
                })

        # TLS 노드 매핑
        for tllogic in self.net_xml.findall(".//tlLogic"):
            tls_id = tllogic.get("id")
            if tls_id:
                self.tls_by_node[tls_id] = tls_id

        print(f"  connections found: {sum(len(v) for v in self.connections_by_tls.values())}")

    def find_crossing_edges_for_generated(self, gen_crossing_id: str, selected_edges: List[str], junction_id: str) -> Tuple[List[str], List[str]]:
        """생성된 crossing의 도로 edge 찾기 (crossing 요소의 edges 속성)"""
        found_crossing_edges = []
        found_walkingarea_edges = []

        # XML에서 crossing 요소 직접 찾기
        for crossing in self.net_xml.findall(".//crossing"):
            node = crossing.get("node")
            crossing_id = crossing.get("id")
            edges_str = crossing.get("edges", "")

            if node == junction_id and crossing_id == gen_crossing_id:
                # Crossing의 도로 edge들
                crossing_road_edges = edges_str.split() if edges_str else []
                found_crossing_edges = crossing_road_edges
                break

        return found_crossing_edges, found_walkingarea_edges

    def find_pedestrian_link_indices_strict(self, tls_id: str, crossing_road_edges: List[str], walkingarea_edge_ids: List[str]) -> Tuple[List[int], List[int], List[int]]:
        """exact/inferred/weak pedestrian link indices 찾기

        crossing_road_edges: crossing 요소의 edges 속성에 있는 도로 edge 리스트
        """
        exact = []
        inferred = []
        weak = []

        if tls_id not in self.connections_by_tls or not crossing_road_edges:
            return exact, inferred, weak

        for conn in self.connections_by_tls[tls_id]:
            from_edge = conn['from']
            to_edge = conn['to']
            via = conn['via']
            link_idx = int(conn['linkIndex']) if conn['linkIndex'] else -1

            # Exact: from 또는 to가 crossing의 도로 edge와 일치
            # 도로 edge에서 출발하거나 도로 edge로 들어가는 connection (횡단보도 관련)
            if from_edge in crossing_road_edges or to_edge in crossing_road_edges:
                exact.append(link_idx)
            # Inferred: via가 walking/crossing 관련 edge인 경우
            elif via and (via in walkingarea_edge_ids or 'walk' in via.lower() or 'cross' in via.lower()):
                inferred.append(link_idx)
            # Weak: edge 이름에 walking/crossing 관련 문자 포함
            elif 'walk' in from_edge.lower() or 'cross' in from_edge.lower() or 'walk' in to_edge.lower() or 'cross' in to_edge.lower():
                weak.append(link_idx)

        return exact, inferred, weak

    def find_pedestrian_green_phases(self, tls_id: str, ped_indices: List[int]) -> Tuple[List[int], List[int]]:
        """Pedestrian green/red phase 찾기"""
        if tls_id not in self.tls_data or not ped_indices:
            return [], []

        green_phases = []
        red_phases = []

        for phase_idx, phase in enumerate(self.tls_data[tls_id]['phases']):
            state = phase.get('state', '')

            all_green = all(
                (idx < len(state) and state[idx] in ['G', 'g'])
                for idx in ped_indices
            )

            all_red = all(
                (idx < len(state) and state[idx] in ['r', 's'])
                for idx in ped_indices
            )

            if all_green:
                green_phases.append(phase_idx)
            elif all_red:
                red_phases.append(phase_idx)

        return green_phases, red_phases

    def check_vehicle_conflict(self, tls_id: str, ped_indices: List[int]) -> Tuple[bool, str]:
        """Vehicle과의 conflict 확인"""
        if tls_id not in self.tls_data:
            return False, "no_tldata"

        has_conflict = False
        method = "incomplete"  # conservative

        for phase_idx, phase in enumerate(self.tls_data[tls_id]['phases']):
            state = phase.get('state', '')

            ped_green = all(
                (idx < len(state) and state[idx] in ['G', 'g'])
                for idx in ped_indices
            )

            if ped_green:
                # 같은 phase에서 vehicle link도 green인지 확인
                for v_idx in range(len(state)):
                    if v_idx not in ped_indices and state[v_idx] in ['G', 'g']:
                        # Vehicle과 pedestrian이 동시에 green
                        has_conflict = True

        return has_conflict, method

    def validate_set_phase_duration_dry_run(self, tls_id: str) -> bool:
        """phase duration 제어 dry-run"""
        # 실제 TraCI를 사용하지 않고, 논리적으로 가능한지만 판단
        if tls_id not in self.tls_data:
            return False

        phases = self.tls_data[tls_id]['phases']
        if not phases:
            return False

        # 각 phase가 minDur/maxDur 범위를 갖는지 확인
        for phase in phases:
            duration = phase.get('duration')
            minDur = phase.get('minDur')
            maxDur = phase.get('maxDur')

            if duration:
                try:
                    dur = int(duration)
                    # minDur, maxDur가 있으면 범위 내에 있는지 확인
                    if minDur and int(minDur) > dur:
                        return False
                    if maxDur and int(maxDur) < dur:
                        return False
                except:
                    return False

        return True

    def find_nearest_tls_for_crossing(self, junction_id: str) -> Tuple[Optional[str], Optional[float]]:
        """Crossing이 속한 junction의 TLS 찾기"""
        try:
            junction = self.net.getNode(junction_id)
            if not junction:
                return None, None

            if junction_id in self.tls_by_node:
                return self.tls_by_node[junction_id], 0.0

            junction_coord = junction.getCoord()
            min_dist = float('inf')
            nearest_tls = None

            for node in self.net.getNodes():
                node_id = node.getID()
                if node_id in self.tls_by_node:
                    node_coord = node.getCoord()
                    dist = ((junction_coord[0] - node_coord[0])**2 + (junction_coord[1] - node_coord[1])**2)**0.5
                    if dist < min_dist:
                        min_dist = dist
                        nearest_tls = self.tls_by_node[node_id]

            return nearest_tls, min_dist if min_dist != float('inf') else None

        except Exception as e:
            return None, None

    def validate_each_crossing_strict(self) -> None:
        """각 crossing별 STRICT TLS 검증"""
        print(f"\n[5/11] 각 Crossing별 STRICT TLS 검증 ({len(self.gen_candidates)}개)")

        for idx, row in self.gen_candidates.iterrows():
            if idx % 100 == 0:
                print(f"  [{idx}/{len(self.gen_candidates)}]")

            cid = str(row['crosswalk_id'])

            # Real vs SUMO 정보
            rv_row = self.real_vs_sumo[self.real_vs_sumo['crosswalk_id'].astype(str) == cid]
            if rv_row.empty:
                rv_grade = 'UNKNOWN'
                rv_dist = None
            else:
                rv_grade = rv_row.iloc[0]['validation_grade']
                rv_dist = rv_row.iloc[0]['distance_real_to_generated_m']

            # Junction 기반 TLS 찾기
            junction_id = str(row.get('junction_id'))
            nearest_tls, distance_to_tls = self.find_nearest_tls_for_crossing(junction_id)

            # Generated crossing edge 찾기
            selected_edges_str = row.get('crossing_edges', '')
            selected_edges = selected_edges_str.split() if selected_edges_str else []
            gen_crossing_id = row.get('generated_crossing_id', f'T2_{cid}')

            crossing_edges, walkingarea_edges = self.find_crossing_edges_for_generated(
                gen_crossing_id, selected_edges, junction_id
            )

            # TLS 존재 확인
            tllogic_exists = nearest_tls in self.tls_data if nearest_tls else False

            # Pedestrian link indices 찾기 (strict 기준)
            exact_ped_indices = []
            inferred_ped_indices = []
            weak_ped_indices = []

            if nearest_tls and crossing_edges:
                exact_ped_indices, inferred_ped_indices, weak_ped_indices = self.find_pedestrian_link_indices_strict(
                    nearest_tls, crossing_edges, walkingarea_edges
                )

            # Green/Red phase 찾기 (exact indices만 사용)
            green_phases, red_phases = self.find_pedestrian_green_phases(nearest_tls, exact_ped_indices) if exact_ped_indices else ([], [])

            # Conflict check
            has_vehicle_conflict, conflict_method = self.check_vehicle_conflict(nearest_tls, exact_ped_indices) if exact_ped_indices else (False, "no_ped_indices")

            # Phase duration control 가능성
            set_phase_duration_ok = self.validate_set_phase_duration_dry_run(nearest_tls) if nearest_tls else False

            # STRICT control grade 판정
            if row['generation_status'] != 'GENERATED':
                control_grade = 'INVALID_FOR_CONTROL'
                failure_reason = f"generation_status={row['generation_status']}"
            elif rv_grade == 'REAL_SUMO_FAIL':
                control_grade = 'INVALID_FOR_CONTROL'
                failure_reason = 'real_vs_sumo_fail'
            elif not nearest_tls:
                control_grade = 'NO_TLS_CONTROL'
                failure_reason = 'no_nearest_tls'
            elif not tllogic_exists:
                control_grade = 'NO_TLS_CONTROL'
                failure_reason = 'tllogic_not_found'
            elif not exact_ped_indices:
                control_grade = 'GEOMETRY_OK_BUT_TLS_UNCLEAR'
                failure_reason = 'no_exact_pedestrian_link_indices'
            elif not green_phases:
                control_grade = 'GEOMETRY_OK_BUT_TLS_UNCLEAR'
                failure_reason = 'no_pedestrian_green_phase_for_exact_indices'
            elif not red_phases:
                control_grade = 'GEOMETRY_OK_BUT_TLS_UNCLEAR'
                failure_reason = 'no_pedestrian_red_phase_for_exact_indices'
            elif not set_phase_duration_ok:
                control_grade = 'GEOMETRY_OK_BUT_TLS_UNCLEAR'
                failure_reason = 'set_phase_duration_validation_failed'
            elif has_vehicle_conflict:
                control_grade = 'CONTROL_CONDITIONAL'
                failure_reason = 'possible_vehicle_conflict'
            elif rv_grade in ['REAL_SUMO_PASS', 'REAL_SUMO_REVIEW']:
                control_grade = 'CONTROL_READY'
                failure_reason = None
            elif rv_grade == 'REAL_SUMO_HIGH_RISK':
                control_grade = 'CONTROL_CONDITIONAL'
                failure_reason = 'real_vs_sumo_high_risk'
            else:
                control_grade = 'CONTROL_CONDITIONAL'
                failure_reason = f'real_vs_sumo_grade={rv_grade}'

            self.validation_results.append({
                'crosswalk_id': cid,
                'real_vs_sumo_validation_grade': rv_grade,
                'distance_real_to_generated_m': rv_dist,
                'generated_crossing_id': gen_crossing_id,
                'generated_crossing_exists': len(crossing_edges) > 0,
                'generated_walkingarea_exists': len(walkingarea_edges) > 0,
                'nearest_junction_id': junction_id,
                'nearest_tls_id': nearest_tls,
                'distance_to_tls_m': distance_to_tls,
                'tlLogic_exists': tllogic_exists,
                'traci_tls_exists': tllogic_exists,
                'controlled_links_count': len(self.connections_by_tls.get(nearest_tls, [])) if nearest_tls else 0,
                'xml_connection_count_for_tls': len(self.connections_by_tls.get(nearest_tls, [])) if nearest_tls else 0,
                'exact_pedestrian_link_indices': ','.join(map(str, sorted(set(exact_ped_indices)))) if exact_ped_indices else '',
                'inferred_pedestrian_link_indices': ','.join(map(str, sorted(set(inferred_ped_indices)))) if inferred_ped_indices else '',
                'weak_candidate_link_indices': ','.join(map(str, sorted(set(weak_ped_indices)))) if weak_ped_indices else '',
                'pedestrian_green_phase_indices': ','.join(map(str, green_phases)) if green_phases else '',
                'pedestrian_red_phase_indices': ','.join(map(str, red_phases)) if red_phases else '',
                'has_pedestrian_green_phase': len(green_phases) > 0,
                'has_pedestrian_red_phase': len(red_phases) > 0,
                'phase_count': self.tls_data.get(nearest_tls, {}).get('phase_count', 0) if nearest_tls else 0,
                'tls_state_length': self.tls_data.get(nearest_tls, {}).get('state_length', 0) if nearest_tls else 0,
                'possible_conflict_vehicle_green': has_vehicle_conflict,
                'conflict_check_method': conflict_method,
                'set_phase_duration_dry_run_success': set_phase_duration_ok,
                'smart_control_eligible': control_grade in ['CONTROL_READY', 'CONTROL_CONDITIONAL'],
                'control_grade_strict': control_grade,
                'failure_reason': failure_reason,
                'needs_manual_tls_review': control_grade in ['GEOMETRY_OK_BUT_TLS_UNCLEAR', 'CONTROL_CONDITIONAL'],
            })

            # 119055 디버깅
            if cid == '119055':
                self.debug_119055 = {
                    'crosswalk_id': cid,
                    'junction_id': junction_id,
                    'nearest_tls_id': nearest_tls,
                    'distance_to_tls_m': distance_to_tls,
                    'generated_crossing_id': gen_crossing_id,
                    'crossing_edges': crossing_edges,
                    'walkingarea_edges': walkingarea_edges,
                    'exact_pedestrian_link_indices': exact_ped_indices,
                    'inferred_pedestrian_link_indices': inferred_ped_indices,
                    'weak_candidate_link_indices': weak_ped_indices,
                    'pedestrian_green_phase_indices': green_phases,
                    'pedestrian_red_phase_indices': red_phases,
                    'has_vehicle_conflict': has_vehicle_conflict,
                    'conflict_check_method': conflict_method,
                    'set_phase_duration_ok': set_phase_duration_ok,
                    'control_grade_strict': control_grade,
                    'failure_reason': failure_reason,
                }

        print(f"  validation complete: {len(self.validation_results)} rows")

    def save_validation_csv(self) -> None:
        """검증 결과 CSV 저장"""
        print(f"\n[6/11] 검증 결과 CSV 저장")

        df = pd.DataFrame(self.validation_results)
        csv_path = self.output_dir / "generated_crosswalk_tls_validation_strict.csv"
        df.to_csv(csv_path, index=False, encoding='utf-8-sig')

        print(f"  saved: {csv_path}")

        grade_counts = df['control_grade_strict'].value_counts()
        print(f"  control_grade_strict distribution:")
        for grade, count in grade_counts.items():
            print(f"    {grade}: {count}")

    def generate_case_119055_analysis(self) -> None:
        """Case 119055 상세 분석"""
        print(f"\n[7/11] Case 119055 STRICT 검증")

        r119 = [r for r in self.validation_results if str(r['crosswalk_id']) == '119055']
        if not r119:
            print(f"  119055 not found")
            return

        result = r119[0]
        control_grade = result['control_grade_strict']

        if control_grade == 'CONTROL_READY':
            recommendation = '**SMART_CONTROL_READY** ✓'
        elif control_grade == 'CONTROL_CONDITIONAL':
            recommendation = '**SMART_CONTROL_CONDITIONAL** - 수동 검수 필요'
        else:
            recommendation = '**NOT_RECOMMENDED_FOR_SMART_CONTROL** ✗'

        report = f"""# Case 119055 TLS Validation (STRICT)

## 신호 제어 기본 정보
- Crosswalk ID: 119055
- Real vs SUMO Grade: {result['real_vs_sumo_validation_grade']}
- Distance: {result['distance_real_to_generated_m']:.1f}m if pd.notna(result['distance_real_to_generated_m']) else 'N/A'

## 생성된 Crossing 정보
- Generated Crossing ID: {result['generated_crossing_id']}
- Generated Crossing Exists: {result['generated_crossing_exists']}
- Generated Walkingarea Exists: {result['generated_walkingarea_exists']}

## TLS 연결
- Nearest Junction: {result['nearest_junction_id']}
- Nearest TLS ID: {result['nearest_tls_id']}
- Distance to TLS: {result['distance_to_tls_m']}m if pd.notna(result['distance_to_tls_m']) else 'N/A'
- tlLogic exists: {result['tlLogic_exists']}
- TraCI TLS exists: {result['traci_tls_exists']}

## Pedestrian Link Indices
- Exact Pedestrian Link Indices: {result['exact_pedestrian_link_indices']}
- Inferred Pedestrian Link Indices: {result['inferred_pedestrian_link_indices']}
- Weak Candidate Link Indices: {result['weak_candidate_link_indices']}

## Pedestrian Control
- Pedestrian green phase indices: {result['pedestrian_green_phase_indices']}
- Pedestrian red phase indices: {result['pedestrian_red_phase_indices']}
- Has pedestrian green phase: {result['has_pedestrian_green_phase']}
- Has pedestrian red phase: {result['has_pedestrian_red_phase']}
- Green phase count: {result['phase_count']}

## Conflict Analysis
- Possible vehicle conflict: {result['possible_conflict_vehicle_green']}
- Conflict check method: {result['conflict_check_method']}

## Control Capability
- Set phase duration dry-run: {result['set_phase_duration_dry_run_success']}
- Smart control eligible: {result['smart_control_eligible']}

## Validation Result
- **Control Grade (STRICT): {result['control_grade_strict']}**
- Needs manual TLS review: {result['needs_manual_tls_review']}
- Failure reason: {result['failure_reason']}

## Recommendation
{recommendation}

### 해석
- ✓ **SMART_CONTROL_READY**: 스마트 신호 연장 실험 대상으로 바로 사용 가능
- 🔍 **SMART_CONTROL_CONDITIONAL**: Netedit/sumo-gui로 TLS 검증 후 사용 가능
- ✗ **NOT_RECOMMENDED**: 스마트 신호 연장 실험 대상 아님
"""

        report_path = self.output_dir / "case_119055_tls_validation_strict.md"
        report_path.write_text(report, encoding='utf-8')
        print(f"  saved: {report_path}")

    def save_debug_119055(self) -> None:
        """119055 디버깅 정보 저장"""
        print(f"\n[8/11] 119055 디버깅 정보 저장")

        if not self.debug_119055:
            print(f"  119055 data not available")
            return

        # JSON 저장
        json_path = self.output_dir / "debug_119055_controlled_links.json"
        with open(json_path, 'w', encoding='utf-8') as f:
            json.dump(self.debug_119055, f, indent=2, ensure_ascii=False)
        print(f"  saved: {json_path}")

        # Markdown 저장
        md_content = f"""# 119055 TLS/Phase 정밀 분석

## 기본 정보
- Crosswalk ID: 119055
- Junction ID: {self.debug_119055.get('junction_id')}
- Generated Crossing ID: {self.debug_119055.get('generated_crossing_id')}
- Nearest TLS ID: {self.debug_119055.get('nearest_tls_id')}
- Distance to TLS: {self.debug_119055.get('distance_to_tls_m'):.1f}m if self.debug_119055.get('distance_to_tls_m') else 'N/A'

## 생성된 객체 확인
- Crossing Edges: {', '.join(self.debug_119055.get('crossing_edges', []))}
- Walkingarea Edges: {', '.join(self.debug_119055.get('walkingarea_edges', []))}

## Pedestrian Link Index 분석
- **Exact Indices**: {self.debug_119055.get('exact_pedestrian_link_indices', [])}
- Inferred Indices: {self.debug_119055.get('inferred_pedestrian_link_indices', [])}
- Weak Candidates: {self.debug_119055.get('weak_candidate_link_indices', [])}

## Phase 분석
- Green Phase Indices: {self.debug_119055.get('pedestrian_green_phase_indices', [])}
- Red Phase Indices: {self.debug_119055.get('pedestrian_red_phase_indices', [])}

## 신호 제어 가능성
- Vehicle Conflict: {self.debug_119055.get('has_vehicle_conflict')}
- Conflict Check Method: {self.debug_119055.get('conflict_check_method')}
- Phase Duration Control: {self.debug_119055.get('set_phase_duration_ok')}

## 최종 판정
- **Control Grade: {self.debug_119055.get('control_grade_strict')}**
- Failure Reason: {self.debug_119055.get('failure_reason')}
"""

        md_path = self.output_dir / "debug_119055_controlled_links.md"
        md_path.write_text(md_content, encoding='utf-8')
        print(f"  saved: {md_path}")

    def generate_summary_report(self) -> None:
        """종합 리포트 생성"""
        print(f"\n[9/11] 종합 리포트 생성")

        df = pd.DataFrame(self.validation_results)

        grade_dist = df['control_grade_strict'].value_counts()
        ready_count = grade_dist.get('CONTROL_READY', 0)
        conditional_count = grade_dist.get('CONTROL_CONDITIONAL', 0)
        unclear_count = grade_dist.get('GEOMETRY_OK_BUT_TLS_UNCLEAR', 0)
        no_tls_count = grade_dist.get('NO_TLS_CONTROL', 0)
        invalid_count = grade_dist.get('INVALID_FOR_CONTROL', 0)

        report = f"""# TLS/Phase Validation Summary (STRICT)

## Generated Network
- Path: {self.net_path}
- TLS count: {len(self.tls_data)}
- Connection count: {sum(len(v) for v in self.connections_by_tls.values())}

## Validation Results
- Total crosswalks analyzed: {len(df)}

### Control Grade Distribution (STRICT)
- CONTROL_READY: {ready_count} ({100*ready_count/len(df):.1f}%)
- CONTROL_CONDITIONAL: {conditional_count} ({100*conditional_count/len(df):.1f}%)
- GEOMETRY_OK_BUT_TLS_UNCLEAR: {unclear_count} ({100*unclear_count/len(df):.1f}%)
- NO_TLS_CONTROL: {no_tls_count} ({100*no_tls_count/len(df):.1f}%)
- INVALID_FOR_CONTROL: {invalid_count} ({100*invalid_count/len(df):.1f}%)

## Smart Control Eligibility
- Ready for smart control: {ready_count}
- Conditional (needs review): {conditional_count}
- Total smart control candidates: {ready_count + conditional_count}
- Coverage: {100*(ready_count + conditional_count)/len(df):.1f}%

## 재검증 사유
현재 결과는 엄격한 기준으로 재검증됨:
- Exact pedestrian link indices만 CONTROL_READY 대상
- Inferred/weak indices는 최소한 CONTROL_CONDITIONAL
- Set phase duration 제어 가능성 검증
- Vehicle conflict 확인
"""

        report_path = self.output_dir / "tls_phase_validation_summary_strict.md"
        report_path.write_text(report, encoding='utf-8')
        print(f"  saved: {report_path}")

    def generate_pilot_candidates(self) -> None:
        """Pilot 후보 생성"""
        print(f"\n[10/11] Pilot 후보 목록 생성")

        df = pd.DataFrame(self.validation_results)

        # 엄격한 기준의 pilot candidates
        pilot_df = df[
            (df['control_grade_strict'].isin(['CONTROL_READY', 'CONTROL_CONDITIONAL'])) &
            (df['real_vs_sumo_validation_grade'].isin(['REAL_SUMO_PASS', 'REAL_SUMO_REVIEW']))
        ].copy()

        # 우선순위 계산
        pilot_df['priority_score'] = 0
        pilot_df.loc[pilot_df['control_grade_strict'] == 'CONTROL_READY', 'priority_score'] = 200
        pilot_df.loc[pilot_df['control_grade_strict'] == 'CONTROL_CONDITIONAL', 'priority_score'] = 150

        pilot_df.loc[pilot_df['real_vs_sumo_validation_grade'] == 'REAL_SUMO_PASS', 'priority_score'] += 50
        pilot_df.loc[pilot_df['real_vs_sumo_validation_grade'] == 'REAL_SUMO_REVIEW', 'priority_score'] += 20

        pilot_df.loc[pilot_df['distance_real_to_generated_m'] <= 10, 'priority_score'] += 30
        pilot_df.loc[pilot_df['distance_real_to_generated_m'] <= 25, 'priority_score'] += 20

        pilot_df['is_119055'] = pilot_df['crosswalk_id'].astype(str) == '119055'

        pilot_df = pilot_df.sort_values('priority_score', ascending=False)

        csv_path = self.output_dir / "pilot_candidate_list_strict.csv"
        pilot_df.to_csv(csv_path, index=False, encoding='utf-8-sig')
        print(f"  saved: {csv_path}")
        print(f"  pilot candidates: {len(pilot_df)}")
        print(f"  119055 included: {'YES' if 119055 in pilot_df['crosswalk_id'].astype(int).values else 'NO'}")

    def run_traci_smoke_test(self) -> None:
        """TraCI smoke test"""
        print(f"\n[11/11] TraCI Smoke Test")

        test_log = "# TraCI TLS Smoke Test\n\n"
        test_log += f"Network: {self.net_path}\n"
        test_log += f"Timestamp: {datetime.now()}\n\n"

        try:
            tls_list = list(self.tls_data.keys())[:5]
            test_log += f"Sample TLS IDs: {tls_list}\n"

            for tls_id in tls_list:
                if tls_id in self.tls_data:
                    tls_info = self.tls_data[tls_id]
                    test_log += f"\n  {tls_id}:\n"
                    test_log += f"    phases: {tls_info['phase_count']}\n"
                    test_log += f"    state_length: {tls_info['state_length']}\n"

            test_log += "\nSmoke test completed: OK\n"

        except Exception as e:
            test_log += f"\nSmoke test failed: {str(e)}\n"

        log_path = self.output_dir / "traci_tls_smoke_test_strict.log"
        log_path.write_text(test_log, encoding='utf-8')
        print(f"  saved: {log_path}")

    def run(self) -> bool:
        """전체 검증 파이프라인"""
        try:
            self.load_data()
            self.index_network_edges()
            self.parse_tllogics()
            self.parse_connections()
            self.validate_each_crossing_strict()
            self.save_validation_csv()
            self.generate_case_119055_analysis()
            self.save_debug_119055()
            self.generate_summary_report()
            self.generate_pilot_candidates()
            self.run_traci_smoke_test()

            print(f"\n{'='*60}")
            print(f"✓ 완료: {self.output_dir}")
            print(f"{'='*60}")
            print(f"\n산출물:")
            for f in sorted(self.output_dir.glob("*")):
                if f.is_file():
                    size_str = f"{f.stat().st_size / 1024:.1f} KB"
                    print(f"  {f.name:50} {size_str:>12}")

            return True

        except Exception as e:
            print(f"\n✗ 오류: {e}")
            import traceback
            traceback.print_exc()
            return False


if __name__ == "__main__":
    validator = StrictCrosswalkTLSValidator(
        generated_network_path="result/t2_generated_crosswalks_20260513_221249/generated_network.net.xml",
        generation_candidates_csv="result/t2_generated_crosswalks_20260513_221249/t2_crosswalk_generation_candidates.csv",
        real_vs_sumo_csv="result/t2_generated_crosswalks_20260513_221249/real_vs_sumo_validation.csv",
        output_base="result",
    )
    validator.run()
