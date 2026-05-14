# CSV 기반 제어점 분석 MVP 구현 로드맵

## 개요

**목표**: 기본 분석 대상 674개 횡단보도에 대해 CSV 기반 제어점 분석 완성

**산출물**: 5개 파일
1. `csv_control_points.csv` (674행)
2. `csv_control_point_summary.md`
3. `csv_control_points_map.html`
4. `case_119055_control_point.md`
5. `case_119055_neighborhood_map.html`

**소요 시간**: ~8~10시간 (연속 작업 시)

---

## 구현 단계

### Phase 1: 좌표 변환 + 네트워크 기반 함수 (2~3시간)

**목표**: net.xml 파싱, 기본 쿼리 함수 완성

#### Task 1.1: sumolib 기반 좌표 변환

```python
# smart_crosswalk_sumo/csv_based_control_point_analysis.py
class CoordinateTransformer:
    def __init__(self, net):
        """sumolib net object 사용"""
        self.net = net
    
    def wgs84_to_sumo(self, lon: float, lat: float) -> Tuple[Optional[float], Optional[float]]:
        """
        우선: net.convertLonLat2XY(lon, lat)
        fallback: pyproj.Transformer
        """
        try:
            x, y = self.net.convertLonLat2XY(lon, lat)
            return x, y
        except:
            # pyproj fallback
            pass
```

**소요 시간**: 45분

---

#### Task 1.2: SUMONetworkAnalyzer - 기본 쿼리 함수

```python
class SUMONetworkAnalyzer:
    def __init__(self, net):
        """sumolib net object 저장"""
        self.net = net
    
    def find_nearest_junction(self, sumo_x, sumo_y) -> Tuple[Optional[str], Optional[float]]:
        """모든 junction 순회, Euclidean distance 계산"""
        pass
    
    def find_nearest_tls(self, sumo_x, sumo_y) -> Tuple[Optional[str], Optional[float]]:
        """모든 TLS 순회, distance 계산"""
        pass
    
    def find_candidate_edges(self, sumo_x, sumo_y, junction_id) -> List[Dict]:
        """junction 근처 edges, shapely로 거리 계산"""
        pass
    
    def find_existing_sumo_crossing(self, candidate_edges) -> Tuple[Optional[str], Optional[float]]:
        """candidate_edges 중 :crossing 찾기"""
        pass
    
    def assess_tls_controllability(self, tls_id, junction_id) -> Dict:
        """tlLogic, connection, pedestrian phase 확인"""
        pass
```

**소요 시간**: 2시간

---

### Phase 2: Control grade 판정 + 전체 분석 실행 (2시간)

#### Task 2.1: ControlGradeAssessor (수정된 A/B/B_WEAK/C/D)

```python
class ControlGradeAssessor:
    @staticmethod
    def assess(csv_info) -> Tuple[str, str, Optional[str]]:
        """
        A_REAL_SUMO_CROSSING:
            - existing_crossing < 30m AND
            - nearest_tls exists AND
            - (tls_in_tllogic OR tls_in_connection) AND
            - (has_pedestrian_phase OR pedestrian_link_exists)
        
        B_CSV_SYNTHETIC_CONTROLLED:
            - no existing_crossing OR distance >= 30m AND
            - nearest_tls < 75m AND
            - (tls_in_tllogic OR tls_in_connection) AND
            - has_pedestrian_phase
        
        B_WEAK_SYNTHETIC_CONTROLLED:
            - nearest_tls 75~100m OR
            - phase candidate unclear but TLS exists
        
        C_CSV_LOCATION_ONLY:
            - junction exists but TLS controllability fails
        
        D_INVALID_NETWORK_MAPPING:
            - coord conversion failed OR
            - no junction found
        """
        pass
```

**소요 시간**: 45분

---

#### Task 2.2: analyze_all_crosswalks 실행

```python
def analyze_all_crosswalks(self) -> pd.DataFrame:
    """
    670개 횡단보도 순회 분석
    1% 진행률 출력
    """
    results = []
    for idx, row in enumerate(self.csv_data):
        if idx % 70 == 0:  # 1% 진행
            print(f"[{idx}/670]")
        result = self._analyze_single_crosswalk(row)
        results.append(result)
    
    self.results_df = pd.DataFrame(results)
    return self.results_df
```

**소요 시간**: 30분 (실행 + 검증)

---

### Phase 3: 산출물 생성 (3~4시간)

#### Task 3.1: csv_control_points.csv 저장

```python
def export_csv(self):
    """UTF-8 with BOM 저장"""
    write_csv_utf8_sig(self.results_df, str(self.result_dir / "csv_control_points.csv"))
```

**소요 시간**: 30분

---

#### Task 3.2: csv_control_point_summary.md 생성

```python
def generate_summary_report(self) -> str:
    """
    포함:
    - 분석 정보 (날짜, CSV 버전, net.xml 버전)
    - 등급별 집계 (A/B/B_WEAK/C/D)
    - 지역별 분포
    - 문제 분석
    - 시뮬레이션 대상 제안
    - 다음 단계
    """
    pass
```

**소요 시간**: 1시간

---

#### Task 3.3: csv_control_points_map.html 생성

```python
def generate_interactive_map(self):
    """
    Folium 기반 지도:
    - 색상: A(green), B(blue), B_WEAK(lightblue), C(yellow), D(red)
    - MarkerCluster 사용
    - 팝업: crosswalk_id, grade, distance_to_tls
    - 범례
    """
    pass
```

**소요 시간**: 1시간

---

#### Task 3.4: case_119055_control_point.md 생성

```python
def generate_case_119055_report(self) -> str:
    """
    필수 항목:
    - CSV/T2 좌표
    - SUMO 변환 좌표
    - nearest junction & distance
    - nearest TLS & distance & tlLogic & connection
    - nearest edges (top 3)
    - existing crossing candidate
    - control grade
    - B_CSV_SYNTHETIC_CONTROLLED 가능 여부
    - 최종 권고 (USE_AS_PILOT / CONDITIONAL_PILOT / DO_NOT_USE)
    """
    pass
```

**소요 시간**: 1시간

---

#### Task 3.5: case_119055_neighborhood_map.html 생성

```python
def generate_case_119055_neighborhood_map(self):
    """
    119055 중심 250m 범위:
    - 119055: 굵은 마커 또는 별 마커 (강조)
    - 주변 후보: 일반 마커 (색상: control grade)
    - 범례
    - 거리 표시 (선택)
    """
    pass
```

**소요 시간**: 1시간

---

### Phase 4: 통합 테스트 및 최종 검증 (1시간)

#### Task 4.1: 전체 pipeline 실행

```python
def run_mvp():
    analysis = CSVBasedControlPointAnalysis(...)
    analysis.load_data()
    analysis.analyze_all_crosswalks()
    analysis.export_results()
    print("Done!")
```

**소요 시간**: 30분

---

#### Task 4.2: 산출물 검증

- [ ] csv_control_points.csv: 670행 + 1 헤더
- [ ] control grade 분포 확인
- [ ] 119055 control grade 확인
- [ ] 119055 B_CSV_SYNTHETIC_CONTROLLED 가능 여부 확인
- [ ] map HTML 렌더링 확인
- [ ] case_119055_neighborhood_map.html 렌더링 확인

**소요 시간**: 30분

---

## Phase별 예상 소요 시간

| Phase | Task 개수 | 예상 시간 |
|-------|----------|----------|
| 1 | 2 | 2.75시간 |
| 2 | 2 | 1.25시간 |
| 3 | 5 | 4시간 |
| 4 | 2 | 1시간 |
| **합계** | **11** | **~9시간** |

**버퍼 포함 총 시간**: ~10~11시간

---

## 구현 주의사항

1. **좌표 변환 우선순위**
   - 1차: `net.convertLonLat2XY(lon, lat)` ← 필수
   - 2차: `pyproj` fallback만 (실제로는 거의 사용 안 될 것)

2. **Registry 무시하기**
   - 기존 registry의 crossing_edge, tls_id 등을 참고로만 사용
   - 실제 판정은 current net.xml에서만 계산
   - registry와 net.xml 불일치 문제 해결 목표

3. **170개 TLS 확인**
   - "current net.xml 기준 TLS 후보가 몇 개인지" 최종 보고
   - net.tlLogics() 길이 확인

4. **기존 파일 보존**
   - registry/junggu_crosswalk_sumo_registry.csv: 수정 금지
   - sumo_2d/net.xml: 수정 금지
   - 기존 simulation 로직: 수정 금지
   - 모든 산출물은 새 폴더에만 생성

---

## 실행 명령어 (예상)

```bash
cd /Users/junlee/Desktop/2026-1/js

python -m smart_crosswalk_sumo.csv_based_control_point_analysis \
    --csv T2_crosswalk_features.csv \
    --net sumo_2d/net.xml \
    --output result
```

**산출물 폴더**: `result/csv_based_control_points_<YYYYMMDD_HHMMSS>/`

---

## 최종 보고 형식

MVP 완료 후 다음을 보고:

1. **수정/추가 파일 목록**
   ```
   Modified:
   - DESIGN_CSV_BASED_CONTROL_POINTS.md
   - CSV_CONTROL_POINT_OUTPUT_SPEC.md
   
   Added:
   - smart_crosswalk_sumo/csv_based_control_point_analysis.py (full implementation)
   - result/csv_based_control_points_<timestamp>/ (5 files)
   ```

2. **산출물 경로**
   ```
   /Users/junlee/Desktop/2026-1/js/result/csv_based_control_points_<YYYYMMDD_HHMMSS>/
   ```

3. **실행 명령어**
   ```bash
   python -m smart_crosswalk_sumo.csv_based_control_point_analysis \
       --csv T2_crosswalk_features.csv \
       --net sumo_2d/net.xml \
       --output result
   ```

4. **결과 요약**
   ```
   csv_control_points.csv:
   - 행 수: 674 (+ 1 header) = 675
   - control grade 분포:
     * A_REAL_SUMO_CROSSING: N개 (%)
     * B_CSV_SYNTHETIC_CONTROLLED: N개 (%)
     * B_WEAK_SYNTHETIC_CONTROLLED: N개 (%)
     * C_CSV_LOCATION_ONLY: N개 (%)
     * D_INVALID_NETWORK_MAPPING: N개 (%)
   
   case_119055:
   - CSV 좌표: (126.96886, 37.56340)
   - SUMO 좌표: (x, y)
   - Nearest junction: [junction_id] ([distance]m)
   - Nearest TLS: [tls_id] ([distance]m)
   - Existing crossing: [있음/없음]
   - Control grade: [A/B/B_WEAK/C/D]
   - B_CSV_SYNTHETIC_CONTROLLED 가능: [YES/NO/CONDITIONAL]
   - 최종 권고: [USE_AS_PILOT / CONDITIONAL_PILOT / DO_NOT_USE]
   
   Current net.xml:
   - TLS 개수: N개
   
   검증:
   - 기존 registry: 수정 안 함 ✓
   - 기존 net.xml: 수정 안 함 ✓
   - 기존 simulation 로직: 수정 안 함 ✓
   - 기존 audit 폴더: 건드림 안 함 ✓
   ```

---

