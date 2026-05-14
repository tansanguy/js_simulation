# CSV 기반 제어점 분석 구현 로드맵

## 개요

이 문서는 `csv_based_control_point_analysis.py`를 완성하기 위한 단계별 구현 계획입니다.

**상태**: 설계 완료, 코드 skeleton 작성 완료, 본격 구현 전 대기

---

## 준비 단계

### ✓ 완료 사항

1. **설계 문서** (`DESIGN_CSV_BASED_CONTROL_POINTS.md`)
   - 7가지 분석 단계 정의
   - Control grade (A/B/C/D) 분류 기준 확정
   - 119055 특별 분석 계획 수립

2. **코드 skeleton** (`smart_crosswalk_sumo/csv_based_control_point_analysis.py`)
   - 클래스 구조 설계
   - 메서드 시그니처 정의
   - 예상 데이터 구조 정의

3. **산출물 명세** (`CSV_CONTROL_POINT_OUTPUT_SPEC.md`)
   - csv_control_points.csv 컬럼 정의 (19개)
   - Summary report markdown 구조
   - Interactive map 기능 정의
   - Case 119055 보고서 구조

### ⏳ 대기 중

- 이 로드맵의 각 단계 승인

---

## Phase 1: 네트워크 분석 기반 구현

**목표**: net.xml을 파싱하고, junction/TLS/edge 쿼리 함수 완성

**담당**: 기존 `network_utils.py` 함수 재활용 및 확장

### Task 1.1: CoordinateTransformer 구현

**파일**: `smart_crosswalk_sumo/csv_based_control_point_analysis.py` - `CoordinateTransformer` 클래스

**작업**:
```python
class CoordinateTransformer:
    def __init__(self, net_elem: ET.Element):
        """
        net.xml의 <net projParameter> 속성에서 좌표계 정보 추출
        pyproj.Transformer 초기화
        
        주의: net.xml이 projParameter를 가지지 않으면 기본값 EPSG:5179 사용
        """
        pass
    
    def wgs84_to_sumo(self, lon: float, lat: float) -> Tuple[Optional[float], Optional[float]]:
        """
        WGS84 좌표 → SUMO projected 좌표 변환
        
        - 성공: (x, y) 반환
        - 실패: (None, None) 반환
        """
        pass
```

**예상 소요 시간**: 30분

**검증**:
- Test case: T2의 119055 좌표 변환
  - 입력: (126.96886126772557, 37.563403744042105)
  - 출력: (sumo_x, sumo_y) 확인

---

### Task 1.2: SUMONetworkAnalyzer - 인덱싱 구현

**파일**: `smart_crosswalk_sumo/csv_based_control_point_analysis.py` - `SUMONetworkAnalyzer._build_indices()`

**작업**:
```python
def _build_indices(self) -> None:
    """
    net.xml 파싱 결과로부터:
    
    1. junction dict 구축
       - key: junction_id
       - value: {'x': float, 'y': float, 'edges': [...]}
    
    2. tls_logics dict 구축
       - key: tls_id
       - value: {'x': float, 'y': float, 'phases': [...], 'state': [...]}
    
    3. edges dict 구축
       - key: edge_id
       - value: {'from': str, 'to': str, 'shape': LineString, 'type': str}
    
    4. tlconnections dict 구축
       - key: tls_id
       - value: [edge_ids that reference this tls]
    """
    pass
```

**참고**: 기존 `network_utils.read_net()` 함수 사용 및 확장

**예상 소요 시간**: 1시간

**검증**:
- 인덱싱 후 주요 수치 확인
  - junction 개수
  - tls 개수
  - edge 개수

---

### Task 1.3: find_nearest_junction & find_nearest_tls

**파일**: `smart_crosswalk_sumo/csv_based_control_point_analysis.py` - `SUMONetworkAnalyzer` 메서드

**작업**:
```python
def find_nearest_junction(self, sumo_x: float, sumo_y: float) -> Tuple[Optional[str], Optional[float]]:
    """
    모든 junction을 순회, Euclidean distance 계산
    MAX_JUNCTION_SEARCH_RADIUS_M (200m) 내에서 가장 가까운 것 반환
    """
    pass

def find_nearest_tls(self, sumo_x: float, sumo_y: float) -> Tuple[Optional[str], Optional[float]]:
    """
    모든 TLS를 순회, Euclidean distance 계산
    MAX_TLS_SEARCH_RADIUS_M (300m) 내에서 가장 가까운 것 반환
    """
    pass
```

**최적화 고려**:
- 31,080개 횡단보도 × junction/TLS 개수 반복 계산
- KDTree 또는 spatial index 사용 권고 (선택)

**예상 소요 시간**: 1.5시간

**검증**:
- 119055 test: 
  - nearest_junction 거리 확인
  - nearest_tls 거리 확인

---

### Task 1.4: find_candidate_edges

**파일**: `smart_crosswalk_sumo/csv_based_control_point_analysis.py` - `SUMONetworkAnalyzer.find_candidate_edges()`

**작업**:
```python
def find_candidate_edges(self, sumo_x: float, sumo_y: float, junction_id: str) -> List[Dict[str, Any]]:
    """
    1. junction에 접속된 모든 edge 수집 (incoming + outgoing)
    2. 각 edge의 shape (line segment)로부터 최단 거리 계산
    3. distance < CANDIDATE_EDGES_RADIUS_M (50m)인 것만 반환
    4. 거리 순 정렬
    5. 우선순위: incoming > outgoing > crossing
    
    반환: [{
        'edge_id': str,
        'edge_type': 'incoming' | 'outgoing' | 'crossing',
        'distance_m': float
    }, ...]
    """
    pass
```

**주의**:
- shapely.distance(Point, LineString) 사용
- edge type 식별: edge_id에 `:crossing` 포함 여부

**예상 소요 시간**: 1시간

---

### Task 1.5: find_existing_sumo_crossing

**파일**: `smart_crosswalk_sumo/csv_based_control_point_analysis.py` - `SUMONetworkAnalyzer.find_existing_sumo_crossing()`

**작업**:
```python
def find_existing_sumo_crossing(self, sumo_x: float, sumo_y: float,
                                 candidate_edges: List[Dict[str, Any]]) -> Tuple[Optional[str], Optional[float]]:
    """
    candidate_edges 중에서 `:crossing` 또는 `crossing:` 포함하는 edge 찾기
    있으면 (edge_id, distance) 반환, 없으면 (None, None)
    """
    pass
```

**예상 소요 시간**: 15분

---

### Task 1.6: assess_tls_controllability

**파일**: `smart_crosswalk_sumo/csv_based_control_point_analysis.py` - `SUMONetworkAnalyzer.assess_tls_controllability()`

**작업**:
```python
def assess_tls_controllability(self, tls_id: Optional[str], junction_id: Optional[str],
                                candidate_edges: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    TLS 제어 가능성 검증:
    
    1. tls_in_tllogic: TLS가 net.xml의 tlLogic에 정의되어 있는가?
    2. tls_in_connection: TLS가 connection tl에 참조되는가?
    3. has_pedestrian_phase: 보행자 phase가 있는가?
       - TLS의 phase state에서 'w' 또는 'W' 포함 확인
    
    반환: {
        'tls_in_tllogic': bool,
        'tls_in_connection': bool,
        'has_pedestrian_phase': bool,
        'pedestrian_phase_candidates': [phase_ids],
        'can_control': bool  # 종합 판정
    }
    """
    pass
```

**주의**:
- TLS phase state 파싱: "rRrGgGwWwww" 형식
- 'w' (walk) 또는 'W' (walk extended) 포함하는 position 추출

**예상 소요 시간**: 1시간

---

## Phase 1 예상 소요 시간

| Task | 예상 시간 |
|------|----------|
| 1.1 CoordinateTransformer | 30분 |
| 1.2 _build_indices | 1시간 |
| 1.3 find_nearest_* | 1.5시간 |
| 1.4 find_candidate_edges | 1시간 |
| 1.5 find_existing_sumo_crossing | 15분 |
| 1.6 assess_tls_controllability | 1시간 |
| **Phase 1 합계** | **5.25시간** |
| 테스트/디버깅 여유 | +2시간 |
| **Phase 1 Total** | **~7시간** |

---

## Phase 2: 분류 로직 및 분석 실행

**목표**: ControlGradeAssessor 완성 및 전체 분석 실행

### Task 2.1: ControlGradeAssessor 구현

**파일**: `smart_crosswalk_sumo/csv_based_control_point_analysis.py` - `ControlGradeAssessor.assess()`

**작업**: 설계 문서의 판정 로직 구현

```python
@staticmethod
def assess(csv_info: Dict[str, Any]) -> Tuple[str, str, Optional[str]]:
    """
    Control grade 4단계 판정:
    
    A_REAL_SUMO_CROSSING:
      - sumo_x/sumo_y는 유효
      - existing_crossing < 30m
      - tls_in_tllogic = True
      - has_pedestrian_phase = True
    
    B_CSV_SYNTHETIC_CONTROLLED:
      - sumo_x/sumo_y는 유효
      - nearest_tls < 100m
      - tls_in_tllogic = True
      - has_pedestrian_phase = True
    
    C_CSV_LOCATION_ONLY:
      - nearest_junction은 있음 (> 200m은 아님)
      - 위의 A, B 조건 미충족
    
    D_INVALID_NETWORK_MAPPING:
      - sumo_x/sumo_y 변환 실패 OR
      - nearest_junction 없음 (> 200m)
    """
    pass
```

**예상 소요 시간**: 30분

---

### Task 2.2: _analyze_single_crosswalk 완성

**파일**: `smart_crosswalk_sumo/csv_based_control_point_analysis.py` - `CSVBasedControlPointAnalysis._analyze_single_crosswalk()`

**작업**: 1~7 단계를 순차 호출하여 결과 dict 생성

**예상 소요 시간**: 30분

---

### Task 2.3: analyze_all_crosswalks 테스트 및 최적화

**파일**: `smart_crosswalk_sumo/csv_based_control_point_analysis.py` - `CSVBasedControlPointAnalysis.analyze_all_crosswalks()`

**작업**:
- 31,080개 횡단보도에 대해 반복 분석
- 진행률 출력 (1,000개마다)
- 메모리 사용량 모니터링

**최적화 고려**:
- pandas DataFrame 사전 할당 (용량 예약)
- 경과 시간 추정

**예상 소요 시간**: 
- 실행: 5~15분 (SUMO 네트워크 크기에 따라)
- 검증: 30분

---

### Task 2.4: _generate_notes 구현

**파일**: `smart_crosswalk_sumo/csv_based_control_point_analysis.py` - `CSVBasedControlPointAnalysis._generate_notes()`

**작업**: 각 케이스별 주의사항 텍스트 생성

```python
def _generate_notes(self, csv_info: Dict[str, Any]) -> str:
    """
    제어 등급별 주의사항 생성
    
    예:
    - A: "기존 crossing edge 재사용 가능"
    - B: "새로운 crossing edge 생성 필요"
    - C: "TLS 거리 100m 이상 — 신호 설치 검토 필요"
    - D: "[failure_reason] — 네트워크 검사 필요"
    """
    pass
```

**예상 소요 시간**: 30분

---

## Phase 3: 산출물 생성

**목표**: CSV, Markdown, HTML 산출물 모두 생성

### Task 3.1: generate_summary_report 구현

**파일**: `smart_crosswalk_sumo/csv_based_control_point_analysis.py` - `CSVBasedControlPointAnalysis.generate_summary_report()`

**작업**: 설계 문서의 `csv_control_point_summary.md` 구조 구현

**포함 항목**:
- 분석 정보 (날짜, CSV 버전, net.xml 버전)
- 등급별 집계 + 백분율
- 지역별 분포 (행정동 기준)
- D 등급 원인 분석
- C 등급 원인 분석
- 시뮬레이션 대상 제안
- 다음 단계
- 주의사항

**예상 소요 시간**: 1.5시간

---

### Task 3.2: export_results - CSV 출력

**파일**: `smart_crosswalk_sumo/csv_based_control_point_analysis.py` - `CSVBasedControlPointAnalysis.export_results()` 일부

**작업**:
```python
def export_results(self) -> None:
    ...
    # [1] CSV 산출
    csv_output = self.result_dir / "csv_control_points.csv"
    write_csv_utf8_sig(self.results_df, str(csv_output))
    ...
```

**검증**:
- 31,080행 × 19 컬럼
- UTF-8 with BOM
- null 값 정상 처리

**예상 소요 시간**: 30분 (실행 포함)

---

### Task 3.3: generate_interactive_map

**파일**: `smart_crosswalk_sumo/csv_based_control_point_analysis.py` (새 메서드)

**작업**:
```python
def generate_interactive_map(self) -> None:
    """
    Folium을 사용하여 interactive map 생성
    
    1. Folium Map 생성 (center: 중구, zoom: 12)
    2. 색상 매핑 (control_grade → color)
    3. MarkerCluster 추가
    4. 팝업 정보 (crosswalk_id, grade, tls_id, ...)
    5. 범례 (legend) 추가
    6. 필터링 컨트롤 (선택)
    7. HTML 저장
    """
    pass
```

**의존성**:
- `folium`
- `folium.plugins.MarkerCluster`

**예상 소요 시간**: 1.5시간

**최적화**:
- 마커 클러스터링으로 성능 개선
- 팝업 정보 간결화

---

### Task 3.4: analyze_case_119055 구현

**파일**: `smart_crosswalk_sumo/csv_based_control_point_analysis.py` - `CSVBasedControlPointAnalysis.analyze_case_119055()`

**작업**:
```python
def analyze_case_119055(self) -> Dict[str, Any]:
    """
    T2에서 119055 행 찾기
    _analyze_single_crosswalk() 결과 + 상세 분석
    
    반환: {
        'crosswalk_id': 119055,
        'csv_info': {...},
        'network_info': {...},
        'judgment': {...},
        'markdown': "# Case 119055 보고서 내용..."
    }
    """
    pass
```

**산출물**:
- `case_119055_control_point.md` 생성

**예상 소요 시간**: 1시간

---

## Phase 3 예상 소요 시간

| Task | 예상 시간 |
|------|----------|
| 3.1 generate_summary_report | 1.5시간 |
| 3.2 export CSV | 30분 |
| 3.3 generate_interactive_map | 1.5시간 |
| 3.4 analyze_case_119055 | 1시간 |
| **Phase 3 합계** | **4.5시간** |
| 테스트/조정 여유 | +1.5시간 |
| **Phase 3 Total** | **~6시간** |

---

## Phase 4: 통합 테스트 및 검증

**목표**: 전체 파이프라인 test 및 결과 검증

### Task 4.1: 통합 테스트

**작업**:
- `CSVBasedControlPointAnalysis.run()` 전체 실행
- 메모리 사용량 확인
- 실행 시간 측정
- 에러 메시지 확인

**예상 소요 시간**: 1시간

---

### Task 4.2: 산출물 검증

**작업**:
- CSV: 행 수, 컬럼 수, null 값
- Markdown: 문법, 표 정확성
- HTML: 렌더링, 마커 개수, 팝업
- Case 119055: 수치 정확성

**예상 소요 시간**: 1시간

---

### Task 4.3: 성능 프로파일링 (선택)

**작업**:
- 각 단계 소요 시간 측정
- 병목 지점 식별
- 최적화 가능성 검토

**예상 소요 시간**: 30분

---

## Phase 4 예상 소요 시간

| Task | 예상 시간 |
|------|----------|
| 4.1 통합 테스트 | 1시간 |
| 4.2 산출물 검증 | 1시간 |
| 4.3 성능 프로파일링 | 30분 |
| **Phase 4 합계** | **2.5시간** |

---

## 전체 예상 소요 시간

| Phase | 예상 시간 |
|-------|----------|
| Phase 1: 네트워크 분석 | 7시간 |
| Phase 2: 분류 로직 | TBD (Phase 1 완료 후 1~2시간) |
| Phase 3: 산출물 생성 | 6시간 |
| Phase 4: 테스트/검증 | 2.5시간 |
| **합계** | **~17시간** |

**현실적 예상**: 실제 작업 시간 + 디버깅 + 최적화 = **2~3일** (연속 작업 시)

---

## 의존성 및 사전 조건

### 필요한 Python 라이브러리

```
pyproj       # 좌표 변환
shapely      # 기하학 연산
pandas       # 데이터 처리
folium       # 지도 생성
lxml         # XML 파싱 (network_utils에서 사용)
```

### 설치

```bash
pip install pyproj shapely pandas folium lxml
```

### 기존 코드 의존성

- `smart_crosswalk_sumo/network_utils.py`: `read_net()`, `distance_to_edge_shape()` 등
- `smart_crosswalk_sumo/preprocess.py`: `load_crosswalk_features()`
- `smart_crosswalk_sumo/output_schema.py`: `write_csv_utf8_sig()`

### 데이터 파일 의존성

- `T2_crosswalk_features.csv`: 31,080개 횡단보도
- `sumo_2d/net.xml`: SUMO 네트워크

---

## 다음 단계

### ✓ 설계 검토 및 승인 요청

1. **DESIGN_CSV_BASED_CONTROL_POINTS.md** 검토
   - 7가지 분석 단계 타당성
   - Control grade 분류 기준 동의
   - 119055 특별 분석 항목 확인

2. **CSV_CONTROL_POINT_OUTPUT_SPEC.md** 검토
   - 산출물 포맷 확인
   - 컬럼 정의 검증
   - 요구 사항 충족 여부

3. **이 로드맵 (IMPLEMENTATION_ROADMAP_CSV_CONTROL_POINTS.md)** 검토
   - Phase별 작업 범위 타당성
   - 소요 시간 현실성
   - 추가 작업 여부

### 승인 후 진행

1. Phase 1 시작 (네트워크 분석 기반)
2. Phase 1 완료 후 Phase 2/3 진행
3. Phase 4에서 최종 검증
4. 결과 분석 및 다음 단계 결정

---

## 주의사항

1. **설계 단계 준수**
   - 기존 registry/net.xml 수정 금지
   - 새 산출물만 생성

2. **성능 고려**
   - 31,080개 × 여러 계산 = 높은 반복 횟수
   - 메모리 효율 중요 (KDTree 등 인덱싱 권고)

3. **119055 검증**
   - 코드 구현 중간중간 119055 test case로 확인
   - 최종 산출물에서 수치 정확성 검증

4. **버전 관리**
   - 매 phase 완료 후 git commit
   - 산출물 버전 (timestamp) 관리

---

