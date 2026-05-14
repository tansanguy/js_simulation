# CSV 기반 횡단보도 제어점 설계 (신 방식)

## 목표

기존: CSV 횡단보도 → SUMO crossing에 억지로 매칭  
**신 방식**: CSV 좌표를 source of truth로 삼아, SUMO 제어 가능성을 별도로 검증

## 핵심 원칙

> **CSV 좌표는 믿되, SUMO 제어 가능성은 별도로 검증한다**

- CSV/T2의 좌표와 속성은 현실 근거 (변경 금지)
- SUMO 네트워크의 제어 가능성은 별도 검증 프로세스
- 기존 registry 및 net.xml은 유지 (새 산출물로만 작업)

---

## 전체 워크플로우

```
Input: T2_crosswalk_features.csv (모든 횡단보도 좌표)
       + sumo_2d/net.xml (SUMO 네트워크)
           ↓
[1] 좌표 변환 (WGS84 → SUMO projected)
           ↓
[2] Nearest junction 탐색
           ↓
[3] Nearest TLS 탐색
           ↓
[4] Candidate edges 탐색 (CSV 좌표 반경 내)
           ↓
[5] Existing SUMO crossing 확인
           ↓
[6] TLS 제어 가능성 검증
           ↓
[7] Control grade 판정 (A/B/C/D)
           ↓
Output: csv_control_points.csv
        csv_control_points_map.html
        case_119055_control_point.md
        csv_control_point_summary.md
```

---

## 단계별 상세 설계

### [1] 좌표 변환 (WGS84 → SUMO projected)

**목적**: T2의 WGS84 좌표를 SUMO 네트워크 좌표계로 변환

**입력**:
- `crosswalk_id`: 횡단보도 ID
- `lon`, `lat`: WGS84 좌표

**처리**:
```python
def wgs84_to_sumo_xy(lon, lat, net_elem):
    """
    WGS84 → SUMO projected coordinates
    
    SUMO의 netOffset과 projParameter 사용
    (대부분의 SUMO net.xml에는 정의됨)
    
    반환: (sumo_x, sumo_y) 또는 None (변환 실패 시)
    """
```

**실패 처리**:
- 변환 불가능 → `D_INVALID_NETWORK_MAPPING` (reason: coordinate_conversion_failed)

**산출물에 담을 컬럼**:
```
sumo_x, sumo_y
```

---

### [2] Nearest junction 탐색

**목적**: 각 CSV 좌표 주변에서 가장 가까운 junction 찾기

**처리**:
```python
def find_nearest_junction(sumo_x, sumo_y, net):
    """
    모든 junction을 순회하여 거리 계산
    거리 < max_search_radius (기본 200m)인 것만 반환
    
    반환: (junction_id, distance_m) 또는 (None, None)
    """
```

**거리 계산**:
- Euclidean distance (SUMO projected 좌표계에서)

**실패 처리**:
- nearest junction이 200m 이상 떨어짐 → 주의 플래그
- 하지만 failure는 아님 (뒤에서 TLS 검증으로 보완)

**산출물에 담을 컬럼**:
```
nearest_junction_id, distance_to_junction_m
```

---

### [3] Nearest TLS 탐색

**목적**: CSV 좌표 주변에서 가장 가까운 신호(TLS)를 찾기

**처리**:
```python
def find_nearest_tls(sumo_x, sumo_y, net):
    """
    모든 TLLogic을 순회하여 거리 계산
    거리 < max_search_radius (기본 300m)인 것만 반환
    
    반환: (tls_id, distance_m) 또는 (None, None)
    """
```

**주의**:
- TLS ID는 junction ID와 다를 수 있음
- 같은 junction에 여러 TLS가 있을 수 있음 (복잡한 교차로)

**실패 처리**:
- nearest TLS가 없음 → `D_INVALID_NETWORK_MAPPING` 또는 `C_CSV_LOCATION_ONLY`로 분류

**산출물에 담을 컬럼**:
```
nearest_tls_id, distance_to_tls_m
```

---

### [4] Candidate edges 탐색

**목적**: CSV 좌표 반경 내에서 교차로 관련 edge들 찾기

**처리**:
```python
def find_candidate_edges(sumo_x, sumo_y, junction_id, net, radius_m=50):
    """
    1) junction에 접속된 모든 edge 수집
    2) CSV 좌표로부터 거리 < radius_m인 것만 필터링
    3) 우선순위: incoming, outgoing, crossing 순
    
    반환: list of (edge_id, edge_type, distance_m)
    """
```

**edge_type 분류**:
- `incoming`: junction으로 유입
- `outgoing`: junction에서 유출
- `crossing`: 횡단보도 용 edge (예: `:crossing_*`)

**산출물에 담을 컬럼**:
```
nearest_edges  # JSON 리스트, top 3 edge_id만
```

---

### [5] Existing SUMO crossing 확인

**목적**: CSV 좌표 근처에 실제 SUMO crossing edge가 있는지 확인

**처리**:
```python
def find_existing_sumo_crossing(sumo_x, sumo_y, candidate_edges, net, radius_m=50):
    """
    candidate_edges 중에서 crossing type인 것 찾기
    
    반환: (crossing_edge_id, distance_m) 또는 (None, None)
    """
```

**판정 기준**:
- SUMO edge name에 `:crossing` 또는 `crossing:` 포함
- CSV 좌표로부터 거리 < 50m이면 "실제 SUMO crossing 있음"

**산출물에 담을 컬럼**:
```
existing_sumo_crossing_edge, existing_crossing_distance_m
```

---

### [6] TLS 제어 가능성 검증

**목적**: CSV 좌표 주변의 TLS가 보행자 신호 제어에 사용 가능한가?

**처리**:
```python
def assess_tls_controllability(tls_id, junction_id, candidate_edges, net):
    """
    1) TLS가 tlLogic에 존재하는가?
    2) TLS가 connection tl에 참조되는가?
    3) 보행자 phase가 명확한가? (walk phase의 지속시간 >= 최소값)
    
    반환: {
        'tls_in_tllogic': bool,
        'tls_in_connection': bool,
        'has_pedestrian_phase': bool,
        'pedestrian_phase_candidates': list of phase_ids,
        'can_control': bool  # 종합 판정
    }
    """
```

**주의**:
- ghost TLS (tlLogic 정의 없이 junction만 있는 경우) 제외
- pedestrian phase는 state 문자열에 'w' 또는 'W' 포함인 것으로 식별

**산출물에 담을 컬럼**:
```
tls_in_tllogic, tls_in_connection, pedestrian_phase_candidate, can_control
```

---

### [7] Control grade 판정

**목적**: CSV 좌표의 횡단보도를 SUMO에서 제어할 수 있는 정도를 분류

#### Control grade 정의 (수정판)

| Grade | 조건 | 용도 |
|-------|------|------|
| **A_REAL_SUMO_CROSSING** | • CSV 좌표 기준 30m 이내 SUMO crossing/crossing edge 존재 AND <br> • nearest_tls 있음 AND <br> • tlLogic 또는 connection 기준 제어 가능 AND <br> • pedestrian link 또는 pedestrian phase 후보 존재 | SUMO 네트워크에서 기존에 이미 신호로 제어되는 횡단보도. 시뮬레이션 즉시 가능. |
| **B_CSV_SYNTHETIC_CONTROLLED** | • 기존 SUMO crossing 없거나 거리 ≥ 30m AND <br> • CSV 좌표 기준 75m 이내 제어 가능 TLS 존재 AND <br> • phase extension 대상으로 사용 가능 AND <br> • 보행자 phase 후보 명확 | CSV 좌표 기반 새 제어점 생성 가능. TLS 신호 제어 기반. 시뮬레이션 권장 대상. |
| **B_WEAK_SYNTHETIC_CONTROLLED** | • TLS가 75~100m 이내 존재 OR <br> • phase 후보가 불명확하지만 TLS는 존재 | 실험 후보로는 보수적으로 제외하거나 별도 검토 필요. |
| **C_CSV_LOCATION_ONLY** | • CSV 좌표는 유효 AND <br> • 도로망/junction 근처 있음 AND <br> • TLS 없거나 phase 연결 불명확 | 현실 위치는 유효하나 신호 제어 불가능. 신호 연장 시뮬레이션 대상 아님. |
| **D_INVALID_NETWORK_MAPPING** | • 좌표 변환 실패 OR <br> • nearest edge/junction 탐색 실패 OR <br> • 네트워크 영역 밖 | 현재 SUMO 네트워크로 사용 불가능. |

#### 판정 로직

```python
def assess_control_grade(csv_info):
    """
    csv_info = {
        'crosswalk_id': ...,
        'sumo_x': ..., 'sumo_y': ...,  # 좌표 변환 결과
        'nearest_junction_id': ...,
        'nearest_tls_id': ...,
        'nearest_tls_distance_m': ...,
        'existing_sumo_crossing_edge': ...,
        'existing_crossing_distance_m': ...,
        'tls_in_tllogic': bool,
        'tls_in_connection': bool,
        'has_pedestrian_phase': bool,
        'pedestrian_link_exists': bool,
    }
    
    # D: 좌표 변환 실패 또는 junction 없음
    if not csv_info['sumo_x'] or not csv_info['nearest_junction_id']:
        return 'D_INVALID_NETWORK_MAPPING'
    
    # A: 기존 crossing + TLS 제어 가능
    if (csv_info['existing_sumo_crossing_edge'] and 
        csv_info['existing_crossing_distance_m'] < 30 and
        csv_info['nearest_tls_id'] and
        (csv_info['tls_in_tllogic'] or csv_info['tls_in_connection']) and
        (csv_info['has_pedestrian_phase'] or csv_info['pedestrian_link_exists'])):
        return 'A_REAL_SUMO_CROSSING'
    
    # B: 새 crossing 생성 가능, TLS 제어 가능 (≤ 75m)
    if (not csv_info['existing_sumo_crossing_edge'] or csv_info['existing_crossing_distance_m'] >= 30 and
        csv_info['nearest_tls_id'] and
        csv_info['nearest_tls_distance_m'] < 75 and
        (csv_info['tls_in_tllogic'] or csv_info['tls_in_connection']) and
        csv_info['has_pedestrian_phase']):
        return 'B_CSV_SYNTHETIC_CONTROLLED'
    
    # B_WEAK: TLS가 있지만 거리가 멀거나 phase 불명확
    if (csv_info['nearest_tls_id'] and
        75 <= csv_info['nearest_tls_distance_m'] < 100 and
        (csv_info['tls_in_tllogic'] or csv_info['tls_in_connection'])):
        return 'B_WEAK_SYNTHETIC_CONTROLLED'
    
    # C: 위치는 유효하나 TLS 제어 불가능
    if csv_info['nearest_junction_id']:
        return 'C_CSV_LOCATION_ONLY'
    
    # D: 기본값
    return 'D_INVALID_NETWORK_MAPPING'
```

---

## 119055 특별 분석

**대상**: crosswalk_id = 119055
**위치**: 순화동, 소공동 (126.968861, 37.563404)
**특이점**: 기존 분석에서 문제가 있었던 케이스

### 분석 항목 (필수)

1. **CSV/T2 좌표**: 정확한 WGS84 lon/lat
2. **SUMO 변환 좌표**: sumolib 또는 pyproj로 변환된 x, y
3. **Nearest junction**: junction_id, distance_to_junction_m
4. **Nearest TLS**: tls_id, distance_to_tls_m, tlLogic 여부, connection 여부
5. **Nearest edges**: top 3 (edge_id, type, distance)
6. **Existing crossing 후보**: edge_id (있으면), distance_m
7. **Control grade 판정**: A / B / B_WEAK / C / D
8. **B_CSV_SYNTHETIC_CONTROLLED 가능 여부**: YES / NO / CONDITIONAL
9. **최종 권고**: USE_AS_PILOT / CONDITIONAL_PILOT / DO_NOT_USE

### 출력 파일

**별도 파일 1**: `csv_control_points_<timestamp>/case_119055_control_point.md`
- 기본 정보
- 좌표 변환 결과
- 네트워크 분석 결과
- 제어 가능성 판정
- 최종 권고

**별도 파일 2**: `csv_control_points_<timestamp>/case_119055_neighborhood_map.html`
- 119055 중심 250m 반역 내 모든 후보 표시
- 119055를 굵게 강조
- 색상: control grade별 구분

---

## 산출물 스키마

### 1. `csv_control_points.csv`

**모든 기본 분석 대상 횡단보도 (674개)에 대한 CSV 기반 제어점 분석**

필수 컬럼:

```
crosswalk_id                          // T2와 매칭되는 ID
csv_lon, csv_lat                       // T2에서 가져온 WGS84 좌표
sumo_x, sumo_y                         // 변환된 SUMO projected 좌표
nearest_junction_id                    // 가장 가까운 junction
distance_to_junction_m                 // 거리 (m)
nearest_tls_id                         // 가장 가까운 TLS
distance_to_tls_m                      // 거리 (m)
nearest_edges                          // JSON: top 3 edge (edge_id, type, distance)
existing_sumo_crossing_edge            // 실제 SUMO crossing edge 있으면 edge_id, 없으면 NULL
existing_crossing_distance_m           // 있으면 거리, 없으면 NULL
control_grade                          // A/B/C/D
control_mode                           // "existing" / "synthetic" / "location_only" / "invalid"
tls_in_tllogic                         // 네트워크의 tlLogic에 정의됨?
tls_in_connection                      // 네트워크의 connection tl에 참조됨?
pedestrian_phase_candidate             // 보행자 phase ID 리스트 (JSON)
failure_reason                         // D 등급일 때 실패 원인 텍스트
notes                                  // 추가 주의사항
```

**크기**: 674행 × 18컬럼 (header 포함 675행)

---

### 2. `csv_control_point_summary.md`

**전체 분석 결과 요약 보고서**

포함 항목:
- 분석 실행 날짜, 시간
- CSV 데이터 버전 (T2 이름/행 수)
- SUMO 네트워크 버전 (net.xml 파일명)
- **등급별 집계**:
  - A_REAL_SUMO_CROSSING: N개 (%)
  - B_CSV_SYNTHETIC_CONTROLLED: N개 (%)
  - C_CSV_LOCATION_ONLY: N개 (%)
  - D_INVALID_NETWORK_MAPPING: N개 (%)
- **지역별 분포** (동 단위):
  - 각 동 별 등급 분포 표
- **문제 분석**:
  - D 등급의 주요 failure reason 분포
  - C 등급에서 TLS가 없는 이유 분석
- **다음 단계 제안**:
  - A+B 등급만으로 시뮬레이션 진행 가능
  - C 등급에서 우선 검토 대상 (TLS 추가 여지)
  - D 등급은 네트워크 재검사 필요

---

### 3. `csv_control_points_map.html`

**대화형 지도 (Folium/Leaflet 기반)**

기능:
- 모든 CSV 좌표를 지도에 표시
- 색상으로 등급 구분:
  - 초록: A_REAL_SUMO_CROSSING
  - 파랑: B_CSV_SYNTHETIC_CONTROLLED
  - 노랑: C_CSV_LOCATION_ONLY
  - 빨강: D_INVALID_NETWORK_MAPPING
- 마커 클릭 시 팝업:
  - crosswalk_id
  - 등급
  - 가장 가까운 TLS
  - failure reason (있으면)
- 지역별 필터 옵션

---

### 4. `case_119055_control_point.md`

**case 119055 상세 분석 보고서**

포함 항목:

#### 기본 정보
- crosswalk_id: 119055
- 위치: 순화동, 소공동
- CSV 좌표: (126.968861, 37.563404)

#### 좌표 변환
- SUMO projected (x, y): [값]
- 변환 성공 여부

#### 주변 구조
- Nearest junction:
  - ID: [값]
  - 거리: [m]
  - 접속 edge 개수: [N]
- Nearest TLS:
  - ID: [값]
  - 거리: [m]
  - tlLogic 정의 유무
  - 보행자 phase 존재 여부

#### SUMO 네트워크 분석
- 기존 crossing edge: [있음/없음]
- Candidate edges (반경 50m):
  - [edge_id1] (incoming, 20m)
  - [edge_id2] (outgoing, 35m)
  - [edge_id3] (crossing, 5m) ← 있으면

#### 제어 가능성 판정

```
┌─────────────────────────────────────┐
│  Control Grade: B_CSV_SYNTHETIC    │
│  Controllable: YES                   │
└─────────────────────────────────────┘

근거:
1. 기존 SUMO crossing: 없음
2. Nearest TLS: ID [junction_id] (거리 [X]m)
3. TLS in tlLogic: YES
4. Pedestrian phase: YES (state: "rRrGgGwWwww")
5. 추가 개입: 불필요 (TLS 신호 제어 즉시 가능)
```

#### 추천
- **추천 분류**: B_CSV_SYNTHETIC_CONTROLLED
- **다음 단계**: 이 좌표에 새로운 crossing edge 생성 + 기존 TLS 활용
- **주의사항**: [있으면 기록]

---

## 파일 저장 위치

```
result/
  csv_based_control_points_<YYYYMMDD_HHMMSS>/
    csv_control_points.csv
    csv_control_point_summary.md
    csv_control_points_map.html
    case_119055_control_point.md
```

예: `csv_based_control_points_20260513_143022/`

---

## 구현 기술 스택

- **언어**: Python 3.8+
- **주요 라이브러리**:
  - `sumolib`: **SUMO net 읽기 및 좌표 변환 (우선)**
  - `pyproj`: 좌표 변환 fallback (WGS84 ↔ SUMO projected)
  - `shapely`: 거리 계산, 공간 연산
  - `folium`: 대화형 지도 생성
  - `pandas`: CSV 읽기/쓰기
  - `xml.etree.ElementTree`: net.xml 파싱
- **기존 모듈 재활용**:
  - `smart_crosswalk_sumo/network_utils.py`: junction, TLS, edge 쿼리 (참고)
  - `smart_crosswalk_sumo/preprocess.py`: 데이터 로드

**좌표 변환 우선순위**:
1. `sumolib.net.readNet(...).convertLonLat2XY(lon, lat)` ← **권장**
2. `pyproj.Transformer` ← fallback only

---

## MVP 구현 범위

### 이번 단계 (MVP)
1. **코드 작성**: `csv_based_control_point_analysis.py` skeleton → 실제 구현
2. **실행**: 기본 분석 대상 670개 횡단보도에 대해 분석
3. **산출물 5개**: csv_control_points.csv, summary MD, map HTML, case_119055 MD, case_119055 neighborhood map HTML
4. **검증**: 119055 결과 확인

### 향후 단계
1. MVP 검증 후 31,080개 전체 확장 (필요시)
2. A+B 등급 선정 및 시뮬레이션 진행

---

## 설계 승인 체크리스트

- [ ] 7가지 단계 프로세스 이해 완료
- [ ] Control grade (A/B/C/D) 분류 기준 동의
- [ ] CSV 기반 산출물 구조 검토 완료
- [ ] 119055 특별 분석 항목 확인
- [ ] 코드 작성 권한 요청

