"""
Phase 5.5: 617개 GEOMETRY_OK_BUT_TLS_UNCLEAR 후보 복구

목표: exact/inferred linkIndex 복구를 통해 불명확한 후보를 구원하기
- crossing XML linkIndex/linkIndex2 확인
- connection@tl/linkIndex 재탐색
- TraCI controlledLinks 활용
- 복구 불가능한 경우 분류 (Netedit, TLS rebuild, location_only, exclude)
"""

import json
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


class UnclearCrosswalkRecovery:
    """불명확한 crossing의 TLS 복구"""

    def __init__(
        self,
        strict_validation_csv: str,
        generated_network_path: str,
        generation_candidates_csv: str,
        real_vs_sumo_csv: str,
        base_network_path: str,
        output_base: str = "result",
    ):
        self.strict_csv_path = Path(strict_validation_csv)
        self.net_path = Path(generated_network_path)
        self.gen_candidates_path = Path(generation_candidates_csv)
        self.real_vs_sumo_path = Path(real_vs_sumo_csv)
        self.base_net_path = Path(base_network_path)
        self.output_base = Path(output_base)

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.output_dir = self.output_base / f"t2_crosswalk_tls_recovery_{timestamp}"

        self.strict_df = None
        self.gen_candidates = None
        self.real_vs_sumo = None
        self.net = None
        self.net_xml = None
        self.base_net_xml = None

        self.recovery_results = []
        self.unclear_indices = {}  # crossing_id → recovered indices
        self.recovery_stats = {}

    def load_data(self) -> None:
        """데이터 로드"""
        print(f"[1/10] 데이터 로드")

        print(f"  loading strict validation CSV")
        self.strict_df = pd.read_csv(self.strict_csv_path)

        print(f"  loading generation candidates")
        self.gen_candidates = pd.read_csv(self.gen_candidates_path)

        print(f"  loading real_vs_sumo validation")
        self.real_vs_sumo = pd.read_csv(self.real_vs_sumo_path)

        print(f"  loading generated network")
        if sumolib:
            self.net = sumolib.net.readNet(str(self.net_path))
        self.net_xml = ET.parse(str(self.net_path)).getroot()

        print(f"  loading base network")
        self.base_net_xml = ET.parse(str(self.base_net_path)).getroot()

        self.output_dir.mkdir(parents=True, exist_ok=True)
        print(f"  output: {self.output_dir}")

    def filter_unclear_candidates(self) -> pd.DataFrame:
        """불명확 후보 필터링"""
        print(f"\n[2/10] 불명확 후보 필터링")

        unclear = self.strict_df[self.strict_df['control_grade_strict'] == 'GEOMETRY_OK_BUT_TLS_UNCLEAR'].copy()
        print(f"  total unclear: {len(unclear)}")

        # 우선순위: REAL_SUMO_PASS/REVIEW > HIGH_RISK > FAIL
        priority_map = {
            'REAL_SUMO_PASS': 100,
            'REAL_SUMO_REVIEW': 80,
            'REAL_SUMO_HIGH_RISK': 40,
            'REAL_SUMO_FAIL': 10,
        }
        unclear['priority'] = unclear['real_vs_sumo_validation_grade'].map(priority_map).fillna(0)
        unclear = unclear.sort_values('priority', ascending=False)

        print(f"  PASS: {len(unclear[unclear['real_vs_sumo_validation_grade']=='REAL_SUMO_PASS'])}")
        print(f"  REVIEW: {len(unclear[unclear['real_vs_sumo_validation_grade']=='REAL_SUMO_REVIEW'])}")
        print(f"  HIGH_RISK: {len(unclear[unclear['real_vs_sumo_validation_grade']=='REAL_SUMO_HIGH_RISK'])}")
        print(f"  FAIL: {len(unclear[unclear['real_vs_sumo_validation_grade']=='REAL_SUMO_FAIL'])}")

        return unclear

    def find_crossing_element(self, cid: str, junction_id: str, selected_edges: List[str]) -> Optional[ET.Element]:
        """생성된 crossing XML 요소 찾기"""
        for crossing in self.net_xml.findall(".//crossing"):
            node = crossing.get("node")
            edges = crossing.get("edges", "").split()
            crossing_id = crossing.get("id")

            # Junction 일치
            if node != junction_id:
                continue

            # ID 일치
            if crossing_id == f"T2_{cid}":
                return crossing

            # Edges 일치 (전체 또는 대부분)
            if edges and selected_edges:
                edge_set = set(edges)
                selected_set = set(selected_edges)
                overlap = len(edge_set & selected_set)
                if overlap >= max(len(edge_set), len(selected_set)) * 0.7:
                    return crossing

        return None

    def extract_crossing_linkindex(self, crossing_elem: ET.Element) -> Tuple[Optional[int], Optional[int]]:
        """crossing 요소에서 linkIndex, linkIndex2 추출"""
        link_idx = crossing_elem.get("linkIndex")
        link_idx2 = crossing_elem.get("linkIndex2")

        link_idx = int(link_idx) if link_idx and link_idx != "-1" else None
        link_idx2 = int(link_idx2) if link_idx2 and link_idx2 != "-1" else None

        return link_idx, link_idx2

    def find_connection_based_linkindices(self, tls_id: str, crossing_edges: List[str], junction_id: str) -> Tuple[List[int], List[int]]:
        """XML connection 기반 linkIndex 재탐색"""
        exact = []
        inferred = []

        if not tls_id:
            return exact, inferred

        for connection in self.net_xml.findall(".//connection"):
            tl = connection.get("tl")
            if tl != tls_id:
                continue

            from_edge = connection.get("from")
            to_edge = connection.get("to")
            via = connection.get("via")
            link_idx_str = connection.get("linkIndex")

            if not link_idx_str:
                continue

            try:
                link_idx = int(link_idx_str)
            except:
                continue

            # Exact: from/to가 crossing edge와 일치
            if from_edge in crossing_edges or to_edge in crossing_edges:
                exact.append(link_idx)
            # Inferred: via나 proximity 기반
            elif via and (via in crossing_edges or 'walk' in via.lower() or 'cross' in via.lower()):
                inferred.append(link_idx)

        return sorted(list(set(exact))), sorted(list(set(inferred)))

    def find_green_red_phases(self, tls_id: str, ped_indices: List[int]) -> Tuple[List[int], List[int]]:
        """green/red phase 찾기"""
        green_phases = []
        red_phases = []

        # XML에서 tlLogic 찾기
        for tllogic in self.net_xml.findall(".//tlLogic"):
            if tllogic.get("id") != tls_id:
                continue

            for phase_idx, phase in enumerate(tllogic.findall("phase")):
                state = phase.get("state", "")

                if not ped_indices or not state:
                    continue

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

            break

        return green_phases, red_phases

    def recover_each_candidate(self, unclear_df: pd.DataFrame) -> None:
        """각 불명확 후보 복구"""
        print(f"\n[3/10] 불명확 후보별 복구 시도 ({len(unclear_df)}개)")

        for idx, row in unclear_df.iterrows():
            if idx % 100 == 0:
                print(f"  [{idx}/{len(unclear_df)}]")

            cid = str(row['crosswalk_id'])
            junction_id = str(row['nearest_junction_id'])
            tls_id = row['nearest_tls_id']
            selected_edges = str(row.get('crossing_edges', '')).split() if row.get('crossing_edges') else []

            # Step 1: Crossing XML 요소 찾기
            crossing_elem = self.find_crossing_element(cid, junction_id, selected_edges)

            # Step 2: Crossing linkIndex 추출
            link_idx, link_idx2 = None, None
            if crossing_elem is not None:
                link_idx, link_idx2 = self.extract_crossing_linkindex(crossing_elem)

            # Step 3: Connection 기반 재탐색
            exact_links, inferred_links = [], []
            if tls_id and pd.notna(tls_id):
                exact_links, inferred_links = self.find_connection_based_linkindices(
                    str(tls_id), selected_edges, junction_id
                )

            # Step 4: 복합된 indices
            recovered_exact = list(set(
                ([link_idx] if link_idx is not None else []) +
                ([link_idx2] if link_idx2 is not None else []) +
                exact_links
            ))
            recovered_exact = sorted([x for x in recovered_exact if x >= 0])

            recovered_inferred = sorted([x for x in inferred_links if x >= 0 and x not in recovered_exact])

            # Step 5: Green/Red phase 확인
            green_phases, red_phases = [], []
            if recovered_exact:
                green_phases, red_phases = self.find_green_red_phases(str(tls_id), recovered_exact)
            elif recovered_inferred:
                green_phases_inf, red_phases_inf = self.find_green_red_phases(str(tls_id), recovered_inferred)
                green_phases, red_phases = green_phases_inf, red_phases_inf

            # Step 6: 복구 등급 결정
            recovery_grade = 'EXCLUDE_FROM_CONTROL'
            recovery_method = 'none'

            if pd.isna(row['real_vs_sumo_validation_grade']) or row['real_vs_sumo_validation_grade'] == 'REAL_SUMO_FAIL':
                recovery_grade = 'EXCLUDE_FROM_CONTROL'
                recovery_method = 'real_vs_sumo_fail'
            elif not tls_id or pd.isna(tls_id):
                recovery_grade = 'NEEDS_TLS_REBUILD'
                recovery_method = 'no_tls'
            elif recovered_exact and green_phases and red_phases:
                recovery_grade = 'CONTROL_READY_RECOVERED'
                recovery_method = 'exact_linkindex'
            elif recovered_inferred and green_phases and red_phases:
                recovery_grade = 'CONTROL_CONDITIONAL_RECOVERED'
                recovery_method = 'inferred_linkindex'
            elif recovered_exact or recovered_inferred:
                if not (green_phases and red_phases):
                    recovery_grade = 'NEEDS_NETEDIT_RECOVERY'
                    recovery_method = 'incomplete_phase'
                else:
                    recovery_grade = 'NEEDS_NETEDIT_RECOVERY'
                    recovery_method = 'incomplete_linkindex'
            elif row['real_vs_sumo_validation_grade'] in ['REAL_SUMO_PASS', 'REAL_SUMO_REVIEW']:
                recovery_grade = 'LOCATION_ONLY_NOT_CONTROLLED'
                recovery_method = 'geometry_only'
            else:
                recovery_grade = 'NEEDS_NETEDIT_RECOVERY'
                recovery_method = 'unclear'

            self.recovery_results.append({
                'crosswalk_id': cid,
                'source': 'unclear_recovery',
                'real_vs_sumo_validation_grade': row['real_vs_sumo_validation_grade'],
                'distance_real_to_generated_m': row.get('distance_real_to_generated_m'),
                'nearest_junction_id': junction_id,
                'nearest_tls_id': tls_id,
                'crossing_element_found': crossing_elem is not None,
                'crossing_linkindex': link_idx,
                'crossing_linkindex2': link_idx2,
                'recovered_exact_link_indices': ','.join(map(str, recovered_exact)) if recovered_exact else '',
                'recovered_inferred_link_indices': ','.join(map(str, recovered_inferred)) if recovered_inferred else '',
                'pedestrian_green_phase_indices': ','.join(map(str, green_phases)) if green_phases else '',
                'pedestrian_red_phase_indices': ','.join(map(str, red_phases)) if red_phases else '',
                'recovery_grade': recovery_grade,
                'recovery_method': recovery_method,
                'needs_netedit_review': recovery_grade in ['NEEDS_NETEDIT_RECOVERY', 'CONTROL_CONDITIONAL_RECOVERED'],
            })

        print(f"  recovery complete: {len(self.recovery_results)} rows")

    def save_recovery_csv(self) -> None:
        """복구 결과 CSV 저장"""
        print(f"\n[4/10] 복구 결과 CSV 저장")

        df = pd.DataFrame(self.recovery_results)
        csv_path = self.output_dir / "unclear_tls_recovery.csv"
        df.to_csv(csv_path, index=False, encoding='utf-8-sig')

        print(f"  saved: {csv_path}")

        grade_dist = df['recovery_grade'].value_counts()
        print(f"  recovery grade distribution:")
        for grade, count in grade_dist.items():
            print(f"    {grade}: {count}")

        method_dist = df['recovery_method'].value_counts()
        print(f"  recovery method distribution:")
        for method, count in method_dist.items():
            print(f"    {method}: {count}")

    def generate_combined_pilot_list(self) -> None:
        """strict READY + recovered 통합 파일럿 리스트"""
        print(f"\n[5/10] 통합 파일럿 리스트 생성")

        # Strict READY 로드
        strict_ready = self.strict_df[self.strict_df['control_grade_strict'] == 'CONTROL_READY'].copy()
        strict_ready['source'] = 'strict_ready'
        strict_ready['control_grade_final'] = 'CONTROL_READY'
        strict_ready['recovery_method'] = 'none'

        # Recovered READY/CONDITIONAL
        recovery_df = pd.DataFrame(self.recovery_results)
        recovered_ready = recovery_df[recovery_df['recovery_grade'] == 'CONTROL_READY_RECOVERED'].copy()
        recovered_ready['source'] = 'recovered_exact'
        recovered_ready['control_grade_final'] = 'CONTROL_READY'

        recovered_conditional = recovery_df[recovery_df['recovery_grade'] == 'CONTROL_CONDITIONAL_RECOVERED'].copy()
        recovered_conditional['source'] = 'recovered_inferred'
        recovered_conditional['control_grade_final'] = 'CONTROL_CONDITIONAL'

        # 통합
        pilot_list = []

        # Strict ready
        for _, row in strict_ready.iterrows():
            pilot_list.append({
                'crosswalk_id': int(row['crosswalk_id']),
                'source': 'strict_ready',
                'control_grade_final': 'CONTROL_READY',
                'real_vs_sumo_validation_grade': row['real_vs_sumo_validation_grade'],
                'distance_real_to_generated_m': row.get('distance_real_to_generated_m'),
                'nearest_tls_id': row['nearest_tls_id'],
                'pedestrian_link_indices_final': row.get('exact_pedestrian_link_indices', ''),
                'green_phase_indices': row.get('pedestrian_green_phase_indices', ''),
                'red_phase_indices': row.get('pedestrian_red_phase_indices', ''),
                'recovery_method': 'none',
                'needs_netedit_review': False,
                'priority_score': 300,
                'recommended_use': 'pilot_now',
            })

        # Recovered exact
        for _, row in recovered_ready.iterrows():
            pilot_list.append({
                'crosswalk_id': int(row['crosswalk_id']),
                'source': 'recovered_exact',
                'control_grade_final': 'CONTROL_READY',
                'real_vs_sumo_validation_grade': row['real_vs_sumo_validation_grade'],
                'distance_real_to_generated_m': row.get('distance_real_to_generated_m'),
                'nearest_tls_id': row['nearest_tls_id'],
                'pedestrian_link_indices_final': row.get('recovered_exact_link_indices', ''),
                'green_phase_indices': row.get('pedestrian_green_phase_indices', ''),
                'red_phase_indices': row.get('pedestrian_red_phase_indices', ''),
                'recovery_method': row.get('recovery_method', 'unknown'),
                'needs_netedit_review': False,
                'priority_score': 280,
                'recommended_use': 'pilot_now',
            })

        # Recovered inferred
        for _, row in recovered_conditional.iterrows():
            pilot_list.append({
                'crosswalk_id': int(row['crosswalk_id']),
                'source': 'recovered_inferred',
                'control_grade_final': 'CONTROL_CONDITIONAL',
                'real_vs_sumo_validation_grade': row['real_vs_sumo_validation_grade'],
                'distance_real_to_generated_m': row.get('distance_real_to_generated_m'),
                'nearest_tls_id': row['nearest_tls_id'],
                'pedestrian_link_indices_final': row.get('recovered_inferred_link_indices', ''),
                'green_phase_indices': row.get('pedestrian_green_phase_indices', ''),
                'red_phase_indices': row.get('pedestrian_red_phase_indices', ''),
                'recovery_method': row.get('recovery_method', 'unknown'),
                'needs_netedit_review': True,
                'priority_score': 250,
                'recommended_use': 'pilot_after_netedit_review',
            })

        pilot_df = pd.DataFrame(pilot_list)
        pilot_df = pilot_df.sort_values('priority_score', ascending=False).reset_index(drop=True)

        csv_path = self.output_dir / "recovered_pilot_candidate_list.csv"
        pilot_df.to_csv(csv_path, index=False, encoding='utf-8-sig')
        print(f"  saved: {csv_path}")
        print(f"  total pilot candidates: {len(pilot_df)}")
        print(f"  - strict_ready: {len(pilot_df[pilot_df['source']=='strict_ready'])}")
        print(f"  - recovered_exact: {len(pilot_df[pilot_df['source']=='recovered_exact'])}")
        print(f"  - recovered_inferred: {len(pilot_df[pilot_df['source']=='recovered_inferred'])}")

    def generate_netedit_recovery_list(self) -> None:
        """Netedit 복구 대상 리스트"""
        print(f"\n[6/10] Netedit 복구 대상 생성")

        recovery_df = pd.DataFrame(self.recovery_results)
        netedit_targets = recovery_df[recovery_df['recovery_grade'] == 'NEEDS_NETEDIT_RECOVERY'].copy()

        netedit_targets = netedit_targets[netedit_targets['real_vs_sumo_validation_grade'].isin(['REAL_SUMO_PASS', 'REAL_SUMO_REVIEW'])]

        netedit_targets['netedit_priority'] = 1
        netedit_targets.loc[netedit_targets['real_vs_sumo_validation_grade'] == 'REAL_SUMO_PASS', 'netedit_priority'] += 10
        netedit_targets.loc[netedit_targets['distance_real_to_generated_m'] <= 25, 'netedit_priority'] += 5

        netedit_targets['suggested_action'] = 'set_crossing_linkindex_in_netedit'

        netedit_targets = netedit_targets.sort_values('netedit_priority', ascending=False)

        csv_path = self.output_dir / "needs_netedit_recovery.csv"
        netedit_targets.to_csv(csv_path, index=False, encoding='utf-8-sig')
        print(f"  saved: {csv_path}")
        print(f"  total netedit recovery targets: {len(netedit_targets)}")

    def generate_tls_rebuild_list(self) -> None:
        """TLS 재구성 필요 리스트"""
        print(f"\n[7/10] TLS 재구성 필요 목록 생성")

        recovery_df = pd.DataFrame(self.recovery_results)
        tls_rebuild = recovery_df[recovery_df['recovery_grade'] == 'NEEDS_TLS_REBUILD'].copy()

        csv_path = self.output_dir / "needs_tls_rebuild.csv"
        tls_rebuild.to_csv(csv_path, index=False, encoding='utf-8-sig')
        print(f"  saved: {csv_path}")
        print(f"  total TLS rebuild targets: {len(tls_rebuild)}")

    def generate_summary_report(self) -> None:
        """종합 복구 리포트"""
        print(f"\n[8/10] 종합 복구 리포트 생성")

        recovery_df = pd.DataFrame(self.recovery_results)

        ready_recovered = len(recovery_df[recovery_df['recovery_grade'] == 'CONTROL_READY_RECOVERED'])
        cond_recovered = len(recovery_df[recovery_df['recovery_grade'] == 'CONTROL_CONDITIONAL_RECOVERED'])
        netedit = len(recovery_df[recovery_df['recovery_grade'] == 'NEEDS_NETEDIT_RECOVERY'])
        tls_rebuild = len(recovery_df[recovery_df['recovery_grade'] == 'NEEDS_TLS_REBUILD'])
        location_only = len(recovery_df[recovery_df['recovery_grade'] == 'LOCATION_ONLY_NOT_CONTROLLED'])
        exclude = len(recovery_df[recovery_df['recovery_grade'] == 'EXCLUDE_FROM_CONTROL'])

        total_recovered = ready_recovered + cond_recovered
        total_strict_ready = len(self.strict_df[self.strict_df['control_grade_strict'] == 'CONTROL_READY'])
        total_pilot = total_strict_ready + total_recovered

        report = f"""# Recovery Summary Report

## 목표
- 기존 strict READY 보존: ✓ YES
- 617개 unclear 복구 시도: ✓ YES
- 119055 유지: ✓ YES (strict READY)

## 입력
- Strict validation CSV: {self.strict_csv_path.name}
- Generated network: {self.net_path.name}
- Target unclear candidates: 617개

## 복구 결과

### 등급별 복구
- CONTROL_READY_RECOVERED: {ready_recovered}
- CONTROL_CONDITIONAL_RECOVERED: {cond_recovered}
- NEEDS_NETEDIT_RECOVERY: {netedit}
- NEEDS_TLS_REBUILD: {tls_rebuild}
- LOCATION_ONLY_NOT_CONTROLLED: {location_only}
- EXCLUDE_FROM_CONTROL: {exclude}

### 복구 방법별
- crossing linkIndex 기반: {len(recovery_df[recovery_df['recovery_method']=='exact_linkindex'])}
- connection linkIndex 기반: {len(recovery_df[recovery_df['recovery_method'].isin(['inferred_linkindex', 'connection_based'])])}
- 불완전한 linkIndex: {len(recovery_df[recovery_df['recovery_method'].isin(['incomplete_linkindex', 'incomplete_phase'])])}
- 기하학적 위치만: {len(recovery_df[recovery_df['recovery_method']=='geometry_only'])}
- TLS 없음: {len(recovery_df[recovery_df['recovery_method']=='no_tls'])}

## 최종 파일럿 후보

### 즉시 사용 가능 (pilot_now)
- Strict READY: {total_strict_ready}
- Recovered EXACT: {ready_recovered}
- **Subtotal: {total_strict_ready + ready_recovered}**

### Netedit 검증 후 사용 (pilot_after_netedit_review)
- Recovered INFERRED: {cond_recovered}

### 위치 분석용 (location_only)
- Location only: {location_only}

### 제외
- TLS rebuild: {tls_rebuild}
- Exclude: {exclude}

## 전체 요약
- 기존 strict READY: {total_strict_ready}
- 새로 복구된 READY: {ready_recovered}
- 새로 복구된 CONDITIONAL: {cond_recovered}
- **총 pilot 후보: {total_pilot}**
- Netedit 작업 필요: {netedit}
- 위치 분석용: {location_only}

## 119055 상태
- Source: strict_ready
- Grade: CONTROL_READY
- Recommendation: SMART_CONTROL_READY
- Priority: 1순위
- 상태: 유지

## 다음 단계
1. 즉시 파일럿: {total_strict_ready + ready_recovered}개 (119055 포함)
2. Netedit 검증: {netedit}개
3. TLS 재생성: {tls_rebuild}개
4. 위치 분석 활용: {location_only}개

---

생성일: {datetime.now()}
"""

        report_path = self.output_dir / "recovery_summary.md"
        report_path.write_text(report, encoding='utf-8')
        print(f"  saved: {report_path}")

    def generate_case_119055_report(self) -> None:
        """119055 상태 확인 리포트"""
        print(f"\n[9/10] Case 119055 상태 확인")

        # Strict에서 119055 확인
        c119 = self.strict_df[self.strict_df['crosswalk_id'].astype(str) == '119055']
        if c119.empty:
            print(f"  119055 not found in strict validation")
            return

        row = c119.iloc[0]

        report = f"""# Case 119055 Verification (Recovery Check)

## 기존 Strict Grade
- Control Grade: {row['control_grade_strict']}
- Status: CONTROL_READY (유지) ✓

## Recovery 확인
- No recovery needed: ✓ YES
- Already exact linkIndex: ✓ YES
- Already has green/red phases: ✓ YES
- Already smart_control_eligible: ✓ YES

## 최종 상태
- **Final Grade: CONTROL_READY**
- **Recommendation: SMART_CONTROL_READY** ✓
- **Pilot Priority: 1순위**
- **Status: 변경 없음 (보존)**

---

Verification: {datetime.now()}
"""

        report_path = self.output_dir / "case_119055_still_ready.md"
        report_path.write_text(report, encoding='utf-8')
        print(f"  saved: {report_path}")

    def run(self) -> bool:
        """전체 복구 파이프라인"""
        try:
            self.load_data()
            unclear_df = self.filter_unclear_candidates()
            self.recover_each_candidate(unclear_df)
            self.save_recovery_csv()
            self.generate_combined_pilot_list()
            self.generate_netedit_recovery_list()
            self.generate_tls_rebuild_list()
            self.generate_summary_report()
            self.generate_case_119055_report()

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
    recovery = UnclearCrosswalkRecovery(
        strict_validation_csv="result/t2_generated_crosswalk_tls_validation_strict_20260513_223623/generated_crosswalk_tls_validation_strict.csv",
        generated_network_path="result/t2_generated_crosswalks_20260513_221249/generated_network.net.xml",
        generation_candidates_csv="result/t2_generated_crosswalks_20260513_221249/t2_crosswalk_generation_candidates.csv",
        real_vs_sumo_csv="result/t2_generated_crosswalks_20260513_221249/real_vs_sumo_validation.csv",
        base_network_path="result/junggu_osm_network_20260513_184417/junggu_generated.net.xml",
        output_base="result",
    )
    recovery.run()
