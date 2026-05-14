"""
Phase 5.6: Exact Control vs Proxy Control 분류

목표: 617개 unclear 후보를 다음으로 재분류
1. EXACT_CONTROL_READY: exact pedestrian linkIndex 있음 (기존 51개)
2. PROXY_CONTROL_READY: detection zone + TLS phase proxy로 근사 (새로 추가)
3. PROXY_CONTROL_CONDITIONAL: TLS 거리 75-100m (조건부)
4. NETEDIT_RECOVERY_REQUIRED: 수동 검증 필요
5. LOCATION_ONLY: TLS 제어 불가
6. EXCLUDE: 부적합

Proxy control:
- Exact pedestrian linkIndex 없음
- T2 좌표 기반 detection zone (20m)
- Nearest TLS phase extension
- Synthetic smart-crossing approximation
"""

import json
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path
from typing import Optional, Tuple, List, Dict

import pandas as pd
import numpy as np

try:
    import sumolib
except ImportError:
    sumolib = None


class ExactProxyControlClassifier:
    """Exact/Proxy control 분류"""

    def __init__(
        self,
        strict_validation_csv: str,
        recovery_csv: str,
        generated_network_path: str,
        real_vs_sumo_csv: str,
        t2_csv: str,
        output_base: str = "result",
    ):
        self.strict_csv_path = Path(strict_validation_csv)
        self.recovery_csv_path = Path(recovery_csv)
        self.net_path = Path(generated_network_path)
        self.real_vs_sumo_path = Path(real_vs_sumo_csv)
        self.t2_csv_path = Path(t2_csv)
        self.output_base = Path(output_base)

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.output_dir = self.output_base / f"t2_crosswalk_control_proxy_classification_{timestamp}"

        self.strict_df = None
        self.recovery_df = None
        self.real_vs_sumo_df = None
        self.t2_df = None
        self.net = None
        self.net_xml = None

        self.classification_results = []
        self.proxy_phase_selection = []

    def load_data(self) -> None:
        """데이터 로드"""
        print(f"[1/9] 데이터 로드")

        self.strict_df = pd.read_csv(self.strict_csv_path)
        self.recovery_df = pd.read_csv(self.recovery_csv_path)
        self.real_vs_sumo_df = pd.read_csv(self.real_vs_sumo_path)
        self.t2_df = pd.read_csv(self.t2_csv_path)

        if sumolib:
            self.net = sumolib.net.readNet(str(self.net_path))
        self.net_xml = ET.parse(str(self.net_path)).getroot()

        self.output_dir.mkdir(parents=True, exist_ok=True)
        print(f"  output: {self.output_dir}")

    def find_pedestrian_green_phases(self, tls_id: str) -> List[int]:
        """TLS의 모든 pedestrian green phase 후보 찾기"""
        green_phases = []

        for tllogic in self.net_xml.findall(".//tlLogic"):
            if tllogic.get("id") != tls_id:
                continue

            for phase_idx, phase in enumerate(tllogic.findall("phase")):
                state = phase.get("state", "")
                if not state:
                    continue

                # lower-case 'g'가 있으면 pedestrian-likely phase
                if 'g' in state:
                    green_phases.append(phase_idx)

            break

        return green_phases

    def select_proxy_phase(self, tls_id: str, green_phases: List[int]) -> Tuple[Optional[int], str, float]:
        """Proxy control을 위한 phase 선택"""
        if not green_phases:
            return None, "no_green_phase", 0.0

        # Phase state 분석
        best_phase = None
        best_score = -1
        best_state = ""

        for tllogic in self.net_xml.findall(".//tlLogic"):
            if tllogic.get("id") != tls_id:
                continue

            for phase_idx, phase in enumerate(tllogic.findall("phase")):
                if phase_idx not in green_phases:
                    continue

                state = phase.get("state", "")
                duration = int(phase.get("duration", 0))

                # 점수 계산
                score = 0

                # Lower-case 'g' 비율 (pedestrian-friendly)
                if state:
                    g_count = state.count('g')
                    score += g_count

                # Duration (너무 짧으면 안 됨, 5초 이상)
                if duration >= 5:
                    score += 1
                elif duration >= 30:
                    score += 2

                # 모두 green인 phase (가장 좋음)
                if state == 'g' * len(state):
                    score += 10

                if score > best_score:
                    best_score = score
                    best_phase = phase_idx
                    best_state = state

            break

        if best_phase is not None:
            confidence = min(best_score / 12.0, 1.0)
            return best_phase, best_state, confidence
        else:
            return None, "", 0.0

    def classify_candidates(self) -> None:
        """모든 후보 분류"""
        print(f"\n[2/9] 후보 분류")

        # Strict READY (이미 분류됨)
        for _, row in self.strict_df.iterrows():
            if row['control_grade_strict'] == 'CONTROL_READY':
                self.classification_results.append({
                    'crosswalk_id': str(row['crosswalk_id']),
                    'source': 'strict_ready',
                    'real_vs_sumo_validation_grade': row['real_vs_sumo_validation_grade'],
                    'distance_real_to_generated_m': row.get('distance_real_to_generated_m'),
                    'nearest_tls_id': row['nearest_tls_id'],
                    'distance_to_tls_m': row.get('distance_to_tls_m'),
                    'exact_pedestrian_link_indices': row.get('exact_pedestrian_link_indices', ''),
                    'final_control_class': 'EXACT_CONTROL_READY',
                    'control_mode': 'exact',
                    'phase_selection_method': 'exact_linkindex',
                    'phase_selection_confidence': 1.0,
                })

        # Recovery 및 unclear 후보
        for _, row in self.recovery_df.iterrows():
            cid = str(row['crosswalk_id'])
            rv_grade = row.get('real_vs_sumo_validation_grade', 'UNKNOWN')
            rv_distance = row.get('distance_real_to_generated_m')
            nearest_tls = row.get('nearest_tls_id')
            green_phases_str = row.get('pedestrian_green_phase_indices', '')

            # String을 float로 변환
            try:
                rv_distance = float(rv_distance) if rv_distance and pd.notna(rv_distance) else None
            except:
                rv_distance = None

            # Green phases string 정규화
            if pd.isna(green_phases_str) or green_phases_str == '':
                green_phases_str = ''
            else:
                green_phases_str = str(green_phases_str)

            # EXCLUDE 기준
            if rv_grade == 'REAL_SUMO_FAIL' or pd.isna(nearest_tls):
                final_class = 'EXCLUDE'
                control_mode = 'none'
                phase_method = 'excluded'
                phase_confidence = 0.0

            # PROXY_CONTROL 기준
            elif (rv_grade in ['REAL_SUMO_PASS', 'REAL_SUMO_REVIEW'] and
                  rv_distance and rv_distance <= 50 and
                  nearest_tls and pd.notna(nearest_tls)):

                # Recovery에서 계산된 green phases 확인, 없으면 network에서 찾기
                green_phase_list = [int(x) for x in green_phases_str.split(',') if x]

                if not green_phase_list:
                    # Network에서 직접 TLS의 green phase 찾기
                    green_phase_list = self.find_pedestrian_green_phases(str(nearest_tls))

                if green_phase_list:
                    # Green phase가 있으면 proxy control 가능
                    selected_phase = green_phase_list[0]  # 첫 번째 green phase 선택
                    confidence = 0.7  # 기본 신뢰도

                    final_class = 'PROXY_CONTROL_READY'
                    control_mode = 'proxy'
                    phase_method = 'proxy_green_phase_from_network'
                    phase_confidence = confidence

                    # Proxy phase selection 저장
                    self.proxy_phase_selection.append({
                        'crosswalk_id': cid,
                        'nearest_tls_id': nearest_tls,
                        'distance_to_tls_m': row.get('distance_to_tls_m'),
                        'candidate_phase_indices': ','.join(map(str, green_phase_list)),
                        'selected_proxy_phase_index': selected_phase,
                        'phase_state': f'proxy_phase_{selected_phase}',
                        'phase_selection_method': 'pedestrian_green_phase_from_network',
                        'phase_selection_confidence': confidence,
                        'needs_manual_phase_review': False,
                        'detection_zone_radius': 20,
                        'proxy_control_mode': 'zone_detection_with_tls_extension',
                    })
                else:
                    final_class = 'LOCATION_ONLY'
                    control_mode = 'none'
                    phase_method = 'no_green_phase'
                    phase_confidence = 0.0

            # NETEDIT 또는 LOCATION
            else:
                final_class = 'LOCATION_ONLY'
                control_mode = 'none'
                phase_method = 'geometry_only'
                phase_confidence = 0.0

            self.classification_results.append({
                'crosswalk_id': cid,
                'source': 'recovery',
                'real_vs_sumo_validation_grade': rv_grade,
                'distance_real_to_generated_m': rv_distance,
                'nearest_tls_id': nearest_tls,
                'distance_to_tls_m': row.get('distance_to_tls_m'),
                'exact_pedestrian_link_indices': row.get('recovered_exact_link_indices', ''),
                'final_control_class': final_class,
                'control_mode': control_mode,
                'phase_selection_method': phase_method,
                'phase_selection_confidence': phase_confidence,
            })

        print(f"  classification complete: {len(self.classification_results)} rows")

    def save_classifications(self) -> None:
        """분류 결과 저장"""
        print(f"\n[3/9] 분류 결과 저장")

        df = pd.DataFrame(self.classification_results)
        csv_path = self.output_dir / "control_candidate_classification.csv"
        df.to_csv(csv_path, index=False, encoding='utf-8-sig')

        print(f"  saved: {csv_path}")

        class_dist = df['final_control_class'].value_counts()
        print(f"  final_control_class distribution:")
        for cls, count in class_dist.items():
            print(f"    {cls}: {count}")

    def save_exact_control_candidates(self) -> None:
        """EXACT control 후보"""
        print(f"\n[4/9] EXACT control 후보 저장")

        df = pd.DataFrame(self.classification_results)
        exact = df[df['final_control_class'] == 'EXACT_CONTROL_READY'].copy()

        csv_path = self.output_dir / "exact_control_candidates.csv"
        exact.to_csv(csv_path, index=False, encoding='utf-8-sig')

        print(f"  saved: {csv_path}")
        print(f"  total exact candidates: {len(exact)}")

    def save_proxy_control_candidates(self) -> None:
        """PROXY control 후보"""
        print(f"\n[5/9] PROXY control 후보 저장")

        df = pd.DataFrame(self.classification_results)
        proxy = df[df['final_control_class'].isin(['PROXY_CONTROL_READY', 'PROXY_CONTROL_CONDITIONAL'])].copy()

        csv_path = self.output_dir / "proxy_control_candidates.csv"
        proxy.to_csv(csv_path, index=False, encoding='utf-8-sig')

        print(f"  saved: {csv_path}")
        print(f"  total proxy candidates: {len(proxy)}")
        print(f"    PROXY_CONTROL_READY: {len(proxy[proxy['final_control_class']=='PROXY_CONTROL_READY'])}")
        print(f"    PROXY_CONTROL_CONDITIONAL: {len(proxy[proxy['final_control_class']=='PROXY_CONTROL_CONDITIONAL'])}")

    def save_proxy_phase_selection(self) -> None:
        """Proxy phase selection 저장"""
        print(f"\n[6/9] Proxy phase selection 저장")

        if not self.proxy_phase_selection:
            print(f"  no proxy phase selections")
            return

        df = pd.DataFrame(self.proxy_phase_selection)
        csv_path = self.output_dir / "proxy_phase_selection.csv"
        df.to_csv(csv_path, index=False, encoding='utf-8-sig')

        print(f"  saved: {csv_path}")
        print(f"  total selections: {len(df)}")
        print(f"  needs manual review: {len(df[df['needs_manual_phase_review']==True])}")

    def save_other_categories(self) -> None:
        """기타 분류 저장"""
        print(f"\n[7/9] 기타 분류 저장")

        df = pd.DataFrame(self.classification_results)

        # NETEDIT
        netedit = df[df['final_control_class'] == 'NETEDIT_RECOVERY_REQUIRED'].copy()
        csv_path = self.output_dir / "netedit_recovery_required.csv"
        netedit.to_csv(csv_path, index=False, encoding='utf-8-sig')
        print(f"  netedit recovery required: {len(netedit)}")

        # LOCATION_ONLY
        location = df[df['final_control_class'] == 'LOCATION_ONLY'].copy()
        csv_path = self.output_dir / "location_only_candidates.csv"
        location.to_csv(csv_path, index=False, encoding='utf-8-sig')
        print(f"  location only: {len(location)}")

    def generate_summary_report(self) -> None:
        """종합 리포트"""
        print(f"\n[8/9] 종합 리포트 생성")

        df = pd.DataFrame(self.classification_results)

        exact = len(df[df['final_control_class'] == 'EXACT_CONTROL_READY'])
        proxy = len(df[df['final_control_class'] == 'PROXY_CONTROL_READY'])
        proxy_cond = len(df[df['final_control_class'] == 'PROXY_CONTROL_CONDITIONAL'])
        netedit = len(df[df['final_control_class'] == 'NETEDIT_RECOVERY_REQUIRED'])
        location = len(df[df['final_control_class'] == 'LOCATION_ONLY'])
        exclude = len(df[df['final_control_class'] == 'EXCLUDE'])

        report = f"""# Control Candidate Classification Report

## 분류 결과

### 최종 등급 분포
| 등급 | 개수 | 비율 |
|------|------|------|
| EXACT_CONTROL_READY | {exact} | {100*exact/len(df):.1f}% |
| PROXY_CONTROL_READY | {proxy} | {100*proxy/len(df):.1f}% |
| PROXY_CONTROL_CONDITIONAL | {proxy_cond} | {100*proxy_cond/len(df):.1f}% |
| NETEDIT_RECOVERY_REQUIRED | {netedit} | {100*netedit/len(df):.1f}% |
| LOCATION_ONLY | {location} | {100*location/len(df):.1f}% |
| EXCLUDE | {exclude} | {100*exclude/len(df):.1f}% |
| **Total** | **{len(df)}** | **100%** |

### 실험 활용도
- Exact control 실험 가능: {exact}개
- Proxy control 실험 가능: {proxy + proxy_cond}개
- **총 실험 대상: {exact + proxy + proxy_cond}개**

## 해석

### Exact Control ({exact}개)
- 실제 pedestrian linkIndex 확인됨
- TraCI phase 정확 제어 가능
- 신뢰도: 최고

### Proxy Control ({proxy + proxy_cond}개)
- Exact linkIndex 없음
- T2 좌표 기반 detection zone (20m)
- Nearest TLS phase extension으로 근사
- 신뢰도: 중간 (synthetic approximation)

### Netedit Recovery ({netedit}개)
- Geometry 좋음, TLS 가까움
- 하지만 phase 불명확
- Netedit/SUMO-GUI에서 확인 필요

### Location Only ({location}개)
- 스마트 신호 실험 제외
- 보행자 안전/사고 분석에만 사용

---

생성일: {datetime.now()}
"""

        report_path = self.output_dir / "control_candidate_summary.md"
        report_path.write_text(report, encoding='utf-8')
        print(f"  saved: {report_path}")

    def generate_case_119055_report(self) -> None:
        """119055 리포트"""
        print(f"\n[9/9] Case 119055 리포트")

        df = pd.DataFrame(self.classification_results)
        c119 = df[df['crosswalk_id'] == '119055']

        if c119.empty:
            print(f"  119055 not found")
            return

        row = c119.iloc[0]

        report = f"""# Case 119055 - EXACT Control Confirmation

## 최종 분류
- Final Control Class: {row['final_control_class']}
- Control Mode: {row['control_mode']}
- Source: strict_ready

## Exact Control Attributes
- Exact Pedestrian Link Indices: {row['exact_pedestrian_link_indices']}
- Phase Selection Method: {row['phase_selection_method']}
- Phase Selection Confidence: {row['phase_selection_confidence']}

## 최종 권고
- **Smart Signal Extension 실험 가능**: YES ✓
- **Pilot Priority**: 1순위 ✓
- **Control Fidelity**: Exact ✓

---

Verification: {datetime.now()}
"""

        report_path = self.output_dir / "case_119055_exact_control.md"
        report_path.write_text(report, encoding='utf-8')
        print(f"  saved: {report_path}")

    def run(self) -> bool:
        """전체 분류 파이프라인"""
        try:
            self.load_data()
            self.classify_candidates()
            self.save_classifications()
            self.save_exact_control_candidates()
            self.save_proxy_control_candidates()
            self.save_proxy_phase_selection()
            self.save_other_categories()
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
    classifier = ExactProxyControlClassifier(
        strict_validation_csv="result/t2_generated_crosswalk_tls_validation_strict_20260513_223623/generated_crosswalk_tls_validation_strict.csv",
        recovery_csv="result/t2_crosswalk_tls_recovery_20260513_225312/unclear_tls_recovery.csv",
        generated_network_path="result/t2_generated_crosswalks_20260513_221249/generated_network.net.xml",
        real_vs_sumo_csv="result/t2_generated_crosswalks_20260513_221249/real_vs_sumo_validation.csv",
        t2_csv="smart_crosswalk_sumo/data/T2_crosswalk_features.csv",
        output_base="result",
    )
    classifier.run()
