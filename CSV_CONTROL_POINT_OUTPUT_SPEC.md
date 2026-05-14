# CSV 기반 제어점 분석 산출물 명세

## 목적

이 문서는 `csv_based_control_points_<timestamp>/` 폴더의 모든 산출물 형식과 의미를 정의합니다.

---

## 1. csv_control_points.csv

**기본 분석 대상 횡단보도 제어점 분석 결과 (674행)**

### 파일 정보
- 인코딩: UTF-8 with BOM (EF BB BF)
- 구분자: comma (,)
- 헤더: 포함
- 행 수: 674 (data) + 1 (header) = 675행

### 컬럼 정의

| # | 컬럼명 | 타입 | 설명 | 예시 |
|----|--------|------|------|------|
| 1 | `crosswalk_id` | int | T2와 매칭되는 횡단보도 ID | 119055 |
| 2 | `csv_lon` | float | T2에서 가져온 경도 (WGS84) | 126.96886126772557 |
| 3 | `csv_lat` | float | T2에서 가져온 위도 (WGS84) | 37.563403744042105 |
| 4 | `sumo_x` | float | 변환된 SUMO projected X 좌표 | 195280.123 |
| 5 | `sumo_y` | float | 변환된 SUMO projected Y 좌표 | 449520.456 |
| 6 | `nearest_junction_id` | str | 가장 가까운 junction ID | junction123 |
| 7 | `distance_to_junction_m` | float | 가장 가까운 junction까지 거리 (m) | 45.2 |
| 8 | `nearest_tls_id` | str | 가장 가까운 TLS ID (없으면 null) | tls456 |
| 9 | `distance_to_tls_m` | float | 가장 가까운 TLS까지 거리 (m) (없으면 null) | 52.8 |
| 10 | `nearest_edges` | json | top 3 candidate edges (JSON 리스트) | `[{"edge_id":"e1","type":"incoming","distance":20.5},...]` |
| 11 | `existing_sumo_crossing_edge` | str | 실제 SUMO crossing edge ID (없으면 null) | edge123:crossing |
| 12 | `existing_crossing_distance_m` | float | crossing edge까지 거리 (m) (없으면 null) | 15.3 |
| 13 | `control_grade` | str | 제어 등급 | A_REAL_SUMO_CROSSING |
| 14 | `control_mode` | str | 제어 모드 | existing / synthetic / location_only / invalid |
| 15 | `tls_in_tllogic` | bool | TLS가 tlLogic에 정의되어 있는가 | true |
| 16 | `tls_in_connection` | bool | TLS가 connection tl에 참조되는가 | true |
| 17 | `pedestrian_phase_candidate` | json | 보행자 phase ID 리스트 | `[0,1,3]` |
| 18 | `failure_reason` | str | 실패 원인 (D등급일 때만 채움) | no_tls_nearby / coordinate_conversion_failed / ... |
| 19 | `notes` | str | 추가 주의사항 | "TLS 거리 100m 이상" |

### 컬럼 설명 상세

#### `sumo_x`, `sumo_y`

- null인 경우: WGS84 → SUMO projected 좌표 변환 실패
- 이 경우 자동으로 `control_grade = D_INVALID_NETWORK_MAPPING`

#### `nearest_edges`

JSON 리스트 형식. 예:
```json
[
  {"edge_id": "e1", "type": "incoming", "distance": 20.5},
  {"edge_id": "e2", "type": "outgoing", "distance": 35.2},
  {"edge_id": "e3:crossing", "type": "crossing", "distance": 5.0}
]
```

- edge_type: `incoming` | `outgoing` | `crossing`
- top 3만 포함 (거리 순 정렬)

#### `pedestrian_phase_candidate`

JSON 배열. TLS의 phase 중에서 보행자 신호 포함 가능한 phase의 인덱스.

예:
```json
[0, 2, 4]
```

또는 null (해당 TLS가 없거나 보행자 phase가 없음)

#### `control_grade`

- **A_REAL_SUMO_CROSSING**: 기존 SUMO crossing 있음 + TLS 제어 가능
- **B_CSV_SYNTHETIC_CONTROLLED**: 새 crossing 생성 가능 + TLS 제어 가능
- **C_CSV_LOCATION_ONLY**: 위치는 유효하나 신호 제어 불가능
- **D_INVALID_NETWORK_MAPPING**: 네트워크 연결 실패

#### `failure_reason`

D 등급일 때만 채움. 주요 값:
- `coordinate_conversion_failed`: WGS84 → SUMO 좌표 변환 실패
- `no_junction_found`: 반경 200m 내 junction 없음
- `no_tls_nearby`: 반경 300m 내 TLS 없음 (C 등급일 때)
- `tls_too_far`: TLS가 100m 이상 떨어짐 (C 등급일 때)
- `tls_not_in_tllogic`: TLS가 net.xml의 tlLogic에 정의 안 됨 (C 등급일 때)
- `no_pedestrian_phase`: TLS에 보행자 phase 없음 (C 등급일 때)

---

## 2. csv_control_point_summary.md

**전체 분석 결과 요약 보고서**

### 파일 정보
- 형식: Markdown
- 인코딩: UTF-8
- 문서 구조: 계층형

### 섹션 구성

#### 1. 분석 정보

```markdown
# CSV 기반 횡단보도 제어점 분석 결과

## 분석 정보

- **실행 날짜**: 2026-05-13
- **실행 시간**: 14:30:22 KST
- **CSV 데이터**: T2_crosswalk_features.csv (31,080개 횡단보도)
- **SUMO 네트워크**: sumo_2d/net.xml
- **네트워크 버전**: [파일 hash 또는 버전 정보]
```

#### 2. 등급별 집계

```markdown
## 등급별 집계

| 등급 | 개수 | 비율 | 설명 |
|------|------|------|------|
| A_REAL_SUMO_CROSSING | 1,230 | 3.9% | 기존 SUMO crossing + TLS 제어 가능 |
| B_CSV_SYNTHETIC_CONTROLLED | 15,450 | 49.7% | 새 crossing 생성 + TLS 제어 가능 |
| C_CSV_LOCATION_ONLY | 12,800 | 41.2% | 위치 유효 but TLS 제어 불가 |
| D_INVALID_NETWORK_MAPPING | 1,600 | 5.2% | 네트워크 연결 실패 |
| **합계** | **31,080** | **100%** | |
```

#### 3. 지역별 분포 (동 단위)

각 동(admin_dong)별로 등급 분포 표:

```markdown
## 지역별 분포 (행정동 기준)

| 행정동 | A | B | C | D | 합계 |
|--------|---|---|---|---|------|
| 소공동 | 12 | 48 | 35 | 5 | 100 |
| 순화동 | 20 | 62 | 45 | 3 | 130 |
| 예지동 | 15 | 45 | 38 | 2 | 100 |
| ... | ... | ... | ... | ... | ... |
| **합계** | **1,230** | **15,450** | **12,800** | **1,600** | **31,080** |
```

#### 4. 문제 분석

##### 4.1 D 등급 분석 (네트워크 연결 실패)

```markdown
## 문제 분석

### D 등급 분석 (1,600개, 5.2%)

#### 실패 원인별 분포

| 실패 원인 | 개수 | 비율 | 대응 방안 |
|---------|------|------|----------|
| coordinate_conversion_failed | 450 | 28.1% | net.xml 좌표계 설정 확인 필요 |
| no_junction_found (거리 > 200m) | 800 | 50.0% | 네트워크 범위 확장 필요 |
| unknown_failure | 350 | 21.9% | 수작업 검사 필요 |
| **합계** | **1,600** | **100%** | |

**해석**: 절반가량(50%)은 네트워크 범위 이슈이며, 좌표 변환 문제는 소수(28%)다.

**권고**: 네트워크를 현재보다 200m 확장하면 약 50%를 B 또는 C 등급으로 개선할 수 있을 것으로 예상된다.
```

##### 4.2 C 등급 분석 (위치 유효, TLS 제어 불가)

```markdown
### C 등급 분석 (12,800개, 41.2%)

#### TLS 불가 원인별 분포

| 원인 | 개수 | 비율 |
|------|------|------|
| no_tls_nearby (거리 > 300m) | 6,500 | 50.8% |
| tls_too_far (거리 100~300m) | 3,200 | 25.0% |
| tls_not_in_tllogic | 1,600 | 12.5% |
| no_pedestrian_phase | 900 | 7.0% |
| **합계** | **12,800** | **100%** |

**해석**: TLS가 없거나 너무 멀어서(> 100m) 제어 불가능한 경우가 75.8%.

**권고**: C 등급 위치에 새로운 신호 설치를 검토하는 것이 방정식보다 현실적일 수 있다.
```

#### 5. 시뮬레이션 대상 후보

```markdown
## 시뮬레이션 대상 후보

### 시뮬레이션 가능한 등급 (A + B)

- **개수**: 1,230 + 15,450 = **16,680개**
- **비율**: 53.6%
- **상태**: 즉시 시뮬레이션 진행 가능

### 향후 개선 가능한 등급 (C)

- **개수**: 12,800개
- **비율**: 41.2%
- **조건**: TLS 추가 또는 신호 설치 후 재분석 필요

### 네트워크 이슈 (D)

- **개수**: 1,600개
- **비율**: 5.2%
- **조건**: 네트워크 범위 확장 또는 부분 재구성 필요

---
```

#### 6. 다음 단계

```markdown
## 다음 단계

1. **A + B 등급 (16,680개)으로 시뮬레이션 진행**
   - 네트워크 준비 작업 필요 (crossing edge 생성)
   - 기본 신호 체계 구성

2. **C 등급에 대한 상세 검토**
   - TLS가 100m 이상 떨어진 사례 (25%) 검토
   - 신호 설치 가능 지역 판단

3. **D 등급 네트워크 개선**
   - 좌표 변환 설정 재확인
   - 네트워크 범위 확장 검토

---
```

#### 7. 주요 주의사항

```markdown
## 주요 주의사항

1. **CSV 좌표는 현실 기준 (고정)**
   - T2의 좌표와 속성 변경 금지
   - 모든 개선은 SUMO 네트워크 수정으로만 진행

2. **제어 가능성은 예측일 뿐**
   - 실제 신호 제어 작동은 추가 검증 필요
   - 보행자 경로 생성 성공을 별도로 확인

3. **등급 업그레이드의 한계**
   - C 등급을 B로 업그레이드하려면 TLS 수정/추가 필요
   - D 등급을 개선하려면 네트워크 재구성 필요

---
```

### 예시 최종 구조

```markdown
# CSV 기반 횡단보도 제어점 분석 결과

## 분석 정보
...

## 등급별 집계
...

## 지역별 분포
...

## 문제 분석

### D 등급 분석
...

### C 등급 분석
...

## 시뮬레이션 대상 후보
...

## 다음 단계
...

## 주요 주의사항
...
```

---

## 3. csv_control_points_map.html

**대화형 지도 (Folium/Leaflet 기반)**

### 파일 정보
- 형식: HTML5 + Leaflet JavaScript
- 인코딩: UTF-8
- 파일 크기: ~1~2MB (마커 670개)

### 기능

#### 3.1 기본 지도

- OpenStreetMap 타일 사용
- 중구 영역에 centered
- 초기 zoom level: 12~13

#### 3.2 마커 표시

**색상 코드**:
- 🟢 **초록**: A_REAL_SUMO_CROSSING
- 🔵 **파랑**: B_CSV_SYNTHETIC_CONTROLLED
- 🟦 **연파랑**: B_WEAK_SYNTHETIC_CONTROLLED
- 🟡 **노랑**: C_CSV_LOCATION_ONLY
- 🔴 **빨강**: D_INVALID_NETWORK_MAPPING

**마커 클러스터링**: zoom level에 따라 자동 그룹화

#### 3.3 팝업 (마커 클릭)

```html
<div>
  <h4>Crosswalk ID: 119055</h4>
  <p><b>등급</b>: B_CSV_SYNTHETIC_CONTROLLED</p>
  <p><b>위치</b>: 순화동, 소공동</p>
  <p><b>Nearest TLS</b>: tls456 (52.8m)</p>
  <p><b>Existing Crossing</b>: 없음</p>
  <p><b>Pedestrian Phase</b>: Available</p>
  <p><i>실패 원인: (없음)</i></p>
</div>
```

#### 3.4 범례 (Legend)

```
□ A_REAL_SUMO_CROSSING (기존 crossing + TLS 제어 가능)
□ B_CSV_SYNTHETIC_CONTROLLED (새 crossing 생성 + TLS 제어 가능)
□ C_CSV_LOCATION_ONLY (위치 유효, TLS 제어 불가)
□ D_INVALID_NETWORK_MAPPING (네트워크 연결 실패)
```

#### 3.5 필터링 컨트롤 (선택)

사용자가 특정 등급만 표시할 수 있는 체크박스:
```
☑ A_REAL_SUMO_CROSSING
☑ B_CSV_SYNTHETIC_CONTROLLED
☑ B_WEAK_SYNTHETIC_CONTROLLED
☑ C_CSV_LOCATION_ONLY
☑ D_INVALID_NETWORK_MAPPING
```

---

## 4. case_119055_control_point.md

**Case 119055 상세 분석 보고서**

### 파일 정보
- 형식: Markdown
- 인코딩: UTF-8
- 길이: 2~3 페이지 (1,500~2,000 단어)

### 섹션 구성

#### 1. 기본 정보

```markdown
# Case 119055: 순화동, 소공동 횡단보도 제어점 분석

## 기본 정보

| 항목 | 값 |
|------|-----|
| **Crosswalk ID** | 119055 |
| **위치** | 서울시 중구 순화동, 소공동 |
| **CSV 좌표 (WGS84)** | (126.96886, 37.56340) |
| **SUMO 좌표** | (195,280.12, 449,520.46) |
| **도로 등급** | 104 (일반도로) |
| **최대 속도** | 30 km/h |
| **고령자 비율** | 9.77% |
| **사고 건수** | 0건 |

---
```

#### 2. 좌표 변환

```markdown
## 좌표 변환 분석

### WGS84 → SUMO Projected

| 항목 | 값 | 상태 |
|------|-----|------|
| CSV 입력 (lon, lat) | (126.96886, 37.56340) | ✓ |
| SUMO 변환 (x, y) | (195,280.12, 449,520.46) | ✓ 성공 |
| 좌표계 | EPSG:5179 (한반도 통일좌표) | ✓ |

**해석**: 좌표 변환 성공. SUMO 네트워크의 모든 후속 분석 가능.

---
```

#### 3. 주변 구조 분석

```markdown
## 주변 네트워크 구조 분석

### Nearest Junction

| 항목 | 값 | 분석 |
|------|-----|------|
| Junction ID | junc_789 | |
| 거리 | 35.2m | ✓ 200m 내 (OK) |
| 접속 edge 수 | 8개 | 일반적인 크기 |
| Edge 방향 | incoming: 4, outgoing: 4 | 대칭적 구조 |

**해석**: 정상적인 교차로. Junction 기반 분석 진행 가능.

### Nearest TLS

| 항목 | 값 | 분석 |
|------|-----|------|
| TLS ID | tls_789 | (junction과 동일) |
| 거리 | 35.2m | ✓ 100m 내 (OK) |
| tlLogic 정의 | ✓ Yes | |
| Phase 수 | 8개 | |
| 보행자 Phase | ✓ Yes (phase 2, 6) | |

**해석**: TLS가 있고, 보행자 신호 제어 가능.

### Candidate Edges

```
반경 50m 내:
  1. edge_789_to_790 (outgoing, 10.2m)
  2. edge_788_to_789 (incoming, 15.8m)
  3. edge_789:crossing (crossing, 3.5m)  ← 기존 crossing
```

**해석**: 기존 crossing edge가 3.5m 거리에 있음.

---
```

#### 4. 제어 가능성 판정

```markdown
## 제어 가능성 판정

### TLS Controllability Check

| 항목 | 값 | 판정 |
|------|-----|------|
| TLS in tlLogic | Yes | ✓ |
| TLS in Connection | Yes | ✓ |
| Pedestrian Phase Available | Yes | ✓ |
| Phase Duration (min) | 12sec (walk) | ✓ |

**종합**: TLS 제어 가능 (✓ can_control)

### Existing Crossing vs. CSV Location

```
CSV 좌표    ●---35.2m---● Junction
                    |
                   3.5m
                    |
            ●─────────●  Existing crossing edge
```

**해석**: 기존 SUMO crossing이 CSV 좌표에서 3.5m 거리.
→ 거리 < 30m이므로 **A_REAL_SUMO_CROSSING으로 분류**.

---
```

#### 5. 종합 판정

```markdown
## 종합 판정 결과

```
┌──────────────────────────────────────────┐
│        Control Grade 판정 결과             │
├──────────────────────────────────────────┤
│                                          │
│   **A_REAL_SUMO_CROSSING**               │
│   (기존 SUMO crossing + TLS 제어 가능)     │
│                                          │
├──────────────────────────────────────────┤
│ 근거:                                     │
│ 1. ✓ CSV 좌표 변환 성공                   │
│ 2. ✓ Nearest junction 있음 (35.2m)      │
│ 3. ✓ Nearest TLS 있음 (35.2m)           │
│ 4. ✓ TLS in tlLogic: Yes                │
│ 5. ✓ Pedestrian phase: Yes              │
│ 6. ✓ Existing crossing < 30m (3.5m)    │
│                                          │
│ 결론: 즉시 시뮬레이션 대상 가능           │
│                                          │
└──────────────────────────────────────────┘
```

### 추천사항

- ✓ **시뮬레이션 포함 권고**
- ✓ **추가 네트워크 개입 불필요**
- ✓ **기존 crossing edge 및 TLS 재사용**

---
```

#### 6. 주의사항 (있으면)

```markdown
## 주의사항

없음 (모든 조건 충족)

---
```

#### 7. 다음 단계

```markdown
## 다음 단계

1. **코드 생성**
   - crossing edge ID: `edge_789:crossing` (이미 존재)
   - TLS ID: `tls_789` (이미 존재)
   - 보행자 phase: phase 2 또는 phase 6 사용

2. **시뮬레이션 설정**
   - crossing edge: `edge_789:crossing`
   - TLS phase: `phase_2` (walk time: 12sec)
   - pedestrian link: [자동 생성]

3. **검증**
   - 보행자 경로 생성 성공 확인
   - 신호 동작 시뮬레이션 테스트

---

**분석 완료**: 2026-05-13 14:32:15 KST
```

---

## 5. case_119055_neighborhood_map.html

**119055 주변 지도**

### 파일 정보
- 형식: HTML5 + Folium/Leaflet
- 인코딩: UTF-8
- 범위: 119055 중심 250m 이내 모든 후보
- 파일 크기: ~500KB

### 기능

#### 5.1 기본 지도

- OpenStreetMap 타일 사용
- 119055 좌표 중심으로 centered
- 초기 zoom level: 14~15

#### 5.2 마커 표시

**119055 자신**:
- 특별 강조 (굵은 테두리 또는 별 마커)
- 색상: control grade 기반
- 팝업: "Crosswalk ID: 119055 (주인공)"

**주변 후보들** (반경 250m):
- 색상: 각 control grade별
- 일반 마커 (119055보다 작음)
- 팝업: crosswalk_id, grade, distance_to_119055

#### 5.3 범례

```
★ 119055 (주인공)
□ A_REAL_SUMO_CROSSING
□ B_CSV_SYNTHETIC_CONTROLLED
□ B_WEAK_SYNTHETIC_CONTROLLED
□ C_CSV_LOCATION_ONLY
□ D_INVALID_NETWORK_MAPPING
```

#### 5.4 거리 표시 (선택)

- 119055에서 각 주변 후보까지 직선 거리 표시
- 원 형태로 250m 반경 표시

---

## 5. 콘텐츠 검증 체크리스트

### csv_control_points.csv
- [ ] 674행 + 헤더
- [ ] 19개 컬럼
- [ ] UTF-8 with BOM 인코딩
- [ ] null 값 적절히 처리
- [ ] JSON 컬럼 유효성 검사

### csv_control_point_summary.md
- [ ] Markdown 문법 정상
- [ ] 등급별 집계 행 합 = 674
- [ ] 백분율 계산 정확
- [ ] 지역별 표 완성도

### csv_control_points_map.html
- [ ] 지도 렌더링 정상
- [ ] 마커 674개 모두 표시
- [ ] 색상 코드 정확 (A/B/B_WEAK/C/D)
- [ ] 팝업 정보 표시 정상
- [ ] 필터링 작동 (있으면)

### case_119055_control_point.md
- [ ] 모든 필수 항목 포함
- [ ] 표 형식 일관성
- [ ] 수치 정확성
- [ ] 최종 권고 명시 (USE_AS_PILOT / CONDITIONAL_PILOT / DO_NOT_USE)

### case_119055_neighborhood_map.html
- [ ] 지도 렌더링 정상
- [ ] 119055 강조 표시 (특별 마커 또는 색상)
- [ ] 반경 250m 내 주변 후보 모두 표시
- [ ] 색상 코드 정확
- [ ] 팝업 정보 표시 정상

---

## 附: 파일 용량 예측

| 파일 | 예측 크기 |
|------|----------|
| csv_control_points.csv | 2~3 MB |
| csv_control_point_summary.md | 50~100 KB |
| csv_control_points_map.html | 1~2 MB |
| case_119055_control_point.md | 20~30 KB |
| case_119055_neighborhood_map.html | 500KB~1 MB |
| **합계** | ~4~7 MB |

