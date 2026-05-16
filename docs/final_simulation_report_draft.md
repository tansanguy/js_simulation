# 스마트 횡단보도 SUMO 시뮬레이션 최종 보고서 초안

Temporary draft. Do not treat this file as current status or final completion proof.

## 0. 이 실험의 개요
본 실험의 목적은 스마트 횡단보도 도입 시 예상되는 보행자 안전 편익과 차량 교통 비용 간의 상충 관계(Trade-off)를 관측하는 데 있습니다. 본 실험 구조에서 **유일한 독립변수는 '스마트 횡단보도 적용 여부'**입니다. 실험은 스마트 횡단보도 기능이 완전히 꺼진 기본 상태(`baseline`)와 해당 기능이 켜진 상태(`smart_crosswalk`) 두 시나리오를 비교하여 진행됩니다.

이 보고서는 정책적 판단의 절대적 근거가 아니라, 통제된 시뮬레이션 환경에서 관측된 지표를 기록한 결과표입니다. 따라서 시뮬레이션에서 도출된 결과는 차량-보행자 간 근접 위험 상황(Proxy)과 그에 따른 교통 지연 비용의 증감 추이(상대 비교)만을 설명하며, 실제 사상자 수 감소와 같이 단정적인 결론을 내리는 데 사용되어서는 안 됩니다.

---

## 1. 이 시뮬레이션에 대한 설명

### 1.1 SUMO / TraCI 역할
- **SUMO (Simulation of Urban MObility):** 개별 차량과 보행자의 이동, 교차로 신호 체계 등을 미시적으로 모사하는 교통 시뮬레이터입니다.
- **TraCI (Traffic Control Interface):** Python 스크립트가 실행 중인 SUMO에 실시간으로 접속하여 상태를 읽고 제어할 수 있게 해주는 인터페이스입니다. 본 실험에서는 TraCI를 통해 보행자 접근을 감지하고 스마트 횡단보도 신호 연장 로직을 개입시키며, 근접 위험 이벤트(Risk Event)를 실시간으로 수집합니다.

### 1.2 도로망 형성 과정
- 시뮬레이션 도로망은 실제 중구 지역의 구조를 반영하기 위해 OpenStreetMap (OSM) 데이터를 Overpass API로 다운로드하여 구성되었습니다.
- 다운로드한 데이터는 `netconvert` 도구를 통해 SUMO가 이해할 수 있는 형태(`SUMO network`)로 변환되었습니다.
- 이후 대상 횡단보도들을 매핑 및 패치하여 최종적인 통합 도로망(`integrated_selected network`)을 생성했습니다.
- `network manifest` 파일은 이 통합 과정에 어떤 횡단보도와 패치 파일들이 포함되었는지 명세하는 역할을 합니다. (참고: Overpass API 504 Timeout, `SUMO_HOME`, `PROJ_LIB`, `proj.db` 환경 변수 설정 등은 파이프라인 구동 시 운영상 관리해야 할 환경적 한계 요소입니다.)

### 1.3 차량 수요
- 차량의 통행 경로는 `routes_seed*.rou.xml` 형태로 정의됩니다.
- 난수 시드(`seed`)는 차량 투입 타이밍과 경로 선택의 무작위성을 통제하는 값으로, 시나리오 간 동일한 배경 교통량을 재현하기 위해 고정하여 사용합니다.
- 차량 수요는 시뮬레이션 내에서 횡단보도를 통과하는 교통 부하를 의미합니다. (단, 구체적인 AADT/O-D 기반 생성 방식의 상세 로직은 코드 확인이 추가로 필요합니다.)

### 1.4 보행자 수요
- 보행자 경로는 `peds_seed*.rou.xml` 로 정의됩니다.
- 라우팅 파일 내 보행자 수요 존재와 실제 시뮬레이션 상의 보행자 프레임(`pedestrian_frame_count`) 수집은 별개입니다. 실제 프레임이 수집되어야만 보행자가 성공적으로 시뮬레이션 공간 내에 존재하고 추적되었다고 볼 수 있습니다.

### 1.5 노인 보행자 비율
- 원본 데이터(`T2_crosswalk_features.csv`)에 명시된 노인 비율을 바탕으로, 시뮬레이션 내 보행자 중 일부를 고령자 속성(`is_senior`)으로 분류합니다.
- 이를 통해 수집되는 `senior_risk_event_count` 지표는 고령 보행자가 연루된 근접 위험 상황 횟수를 의미합니다. (정확한 속성 부여 세부 로직은 추가 확인이 필요합니다.)

### 1.6 도로 이벤트와 분포
- 현실적인 도로 상황을 모사하기 위해 `bus_stop`(버스 정차), `accident`(사고/고장), `illegal_parking`(불법 주정차)과 같은 이벤트들이 확률적으로 주입됩니다.
- 이러한 이벤트는 도로 용량을 줄여 꼬리물기 등 **현실적 정체를 만들기 위한 외생 조건(통제 변수)**입니다.
- 이들은 정책 변수(독립변수)가 아니며, `baseline`과 `smart_crosswalk` 양쪽 시나리오에 완벽히 동일하게 적용되어 비교의 공정성을 유지합니다.

---

## 2. 횡단보도 전처리 및 SUMO 매핑 방법

### 2.1 T2_crosswalk_features.csv의 역할
- 중구 지역 내 횡단보도들의 위치와 속성을 담은 후보군 원본 데이터입니다.
- 횡단보도ID, 위도(lat), 경도(lon) 외에도 차로 수(LANES), 도로등급(ROAD_RANK), 제한속도(MAX_SPD), 사고건수, 추정AADT, 행정동, 노인비율 등 시뮬레이션 환경 구성과 분석 기준이 되는 주요 컬럼들을 포함하고 있습니다.

### 2.2 lon/lat → SUMO x/y 좌표 변환
- T2 데이터의 위치 정보는 위경도 좌표계(lon/lat)이나, SUMO 내부 연산 및 TraCI 제어는 모두 자체 평면 직교 좌표계(x/y)를 사용합니다.
- 따라서 원본 lon/lat 값을 그대로 엣지(Edge) 탐색에 사용할 경우 거리 계산 및 매핑 오류가 발생하므로, 반드시 내부 변환 로직을 거친 x/y 좌표를 기준으로 탐색해야 합니다.

### 2.3 edge / node / crossing / TLS 매핑
- 변환된 좌표를 중심으로 반경 내의 차량용 도로(Edge)와 교차점(Junction) 후보를 탐색합니다.
- 해당 위치에 존재하는 횡단보도 구역(Crossing Edge)과 신호등(TLS, Traffic Light System) 유무를 확인합니다.
- 차량의 접근 차로(Approach lane)와 상충 엣지(Conflict edge)를 정리하여 매핑을 구성합니다.

### 2.4 registry 구축
- Registry는 상기 매핑 과정을 통해 횡단보도들이 SUMO 네트워크 상에 어떤 형태로 인식되었는지를 관리하는 통합 상태 장부입니다.
- 상태값 설명:
  - `A`: 원본 네트워크에 횡단보도 및 신호등 구조가 완벽히 존재하는 상태
  - `recovered`: 불완전한 구조를 로직으로 보완하여 횡단보도로 인식시킨 상태
  - `patched_unsignalized`: 물리적 횡단보도가 없어 파이썬 패치 스크립트로 강제 생성한 상태
  - `represented_by_patched_crossing`: 인접한 패치 횡단보도 하나로 통합 대표되는 상태
  - `manual_review`: 자동 매핑 실패로 수동 확인이 필요한 상태
  - `netconvert_discarded_unclassified`: 패치를 시도했으나 `netconvert` 구조적 제약으로 최종 폐기된 상태
- 유의점: Registry 상 유효한 상태라 하더라도, `network manifest`를 통해 최종 통합 도로망에 포함되어야만 시뮬레이션 실행이 가능합니다.

### 2.5 crossing patch / recovery
- OSM 원본 도로망은 차량 중심이므로, 보행자를 위한 횡단 구역이 누락된 경우가 많습니다. 이를 보완하기 위해 `<crossing node="..." edges="..." priority="true"/>` 와 같은 구문을 담은 `.con.xml` 파일을 별도 생성하여 구조를 강제 주입(Patch)합니다.
- 단, 연결 가능한 보행로(`pedestrian edge`)나 교차로 내 대기 공간(`walkingarea`)이 부재하는 등 논리적 모순이 발생하면 `netconvert` 단계에서 해당 패치를 폐기(Discard)할 수 있음을 survival report를 통해 확인해야 합니다. 중복 처리 방지도 고려해야 합니다.

### 2.6 최종 설치 후보 pool
- 전체 670개 후보 중 662개가 최종 설치 후보 Pool로 정리되었습니다.
- 이 중 상당수(412개)는 기존 망에 횡단보도가 존재하지 않아 `patched_unsignalized` 형태로 신규 구성된 crossing 후보이며, 34개는 `represented_by_patched_crossing`으로 대표 crossing이 커버하도록 중복 관리되었습니다.
- (참고: 스마트 기능이 적용되는 '신호 연장형' 후보군과 기능 없이 인프라만 구성되는 '비신호/프록시' 포함 전체 후보군 Pool은 논리적으로 구분해서 설명해야 합니다.)

---

## 3. 결과 파일과 해석 방법

### 3.1 최종 3 CSV 구조
- 패키지 내 CSV 파일들은 점수를 매기기 위한 것이 아니라 시뮬레이션 내에서 수집된 **관측 결과표**입니다.
  - `baseline.csv`: 기능 미적용 시나리오 관측값
  - `smart_crosswalk.csv`: 기능 적용 시나리오 관측값
  - `comparison.csv`: 두 시나리오 간 관측값의 차이(Delta)를 기록한 표

### 3.2 교통 비용 지표
- `avg_vehicle_delay_sec`: 차량 1대당 평균 지연 시간 (증가 시 비용 증가)
- `avg_queue_length` / `max_queue_length`: 교차로 정체 대기열의 평균 및 최대 길이
- `vehicle_delay_cost`: 평균 지연 시간을 기반으로 산출된 차량 통행 지연 비용 추정치
- `network_avg_travel_time_sec`: 네트워크 내 차량들의 평균 통행 시간
- `network_avg_speed_mps`: 네트워크 전역의 평균 주행 속도
- `network_arrived_vehicles`: 목적지에 정상 도착한 차량 수 (교통 처리량)
- `network_teleported_vehicles`: 정체 누적으로 시뮬레이터가 위치를 강제 이동시킨 차량 수
- `network_spillback_rate`: 차량 꼬리물기가 교차로를 넘어 후방 도로까지 마비시킨 비율

### 3.3 스마트 신호 개입 지표
- `extension_count`: 스마트 로직이 보행자를 감지하여 신호를 연장한 횟수
- `total_extension_sec`: 연장된 총 시간
- 이 수치들이 0인 경우, 해당 실행 내내 보행자 감지로 인한 스마트 개입이 한 번도 동작하지 않았음을 시사합니다. (좋고 나쁨이 아닌 개입 강도의 관측치입니다.)

### 3.4 안전 proxy 지표
- `risk_event_count`: 차량과 보행자가 근접하여 충돌 위험 상황(Proxy)으로 기록된 총 횟수
- `senior_risk_event_count`: 해당 위험 이벤트 중 고령자가 연루된 횟수
- `risk_score` / `safety_risk_score`: 위험도 점수
- `accident_expected_value`: 상해 기대값
- `pedestrian_frame_count` / `vehicle_frame_count`: 추적된 프레임 수
- `pair_diag_n_within_15m`, `pair_diag_min_distance_m`: 근접 상황 지표
- **주의:** 수집된 이벤트는 실제 물리적 충돌(사고) 건수가 아닙니다. 또한 `risk_event_count`가 0일 경우 비교 대상이 없어 안전 편익 지표 변화에 대한 결론은 보류되어야 합니다.

### 3.5 baseline vs smart_crosswalk 비교
- `comparison.csv`에 기록된 Delta(차이값)를 토대로 교통 비용 지표가 얼마나 변동했는지, 스마트 신호 개입이 얼마나 일어났는지, 안전 Proxy 변화가 있는지를 관측합니다.
- Delta 수치는 자동 정책 점수가 확정 짓는 값이 아니며, 단순 관측 차이값입니다.

---

## 4. 실행 결과 요약 (119055 후보 대상, seed=42)

| 지표명 | Baseline 관측값 | Smart_crosswalk 관측값 | Delta (차이) | 해석 |
|---|---|---|---|---|
| **avg_vehicle_delay_sec** | 10.51 | 10.51 | 0.0 | 변화 없음 |
| **avg_queue_length** | 14.43 | 14.43 | 0.0 | 변화 없음 |
| **max_queue_length** | 29.0 | 29.0 | 0.0 | 변화 없음 |
| **vehicle_delay_cost** | 131345.78 | 131345.78 | 0.0 | 변화 없음 |
| **network_avg_travel_time_sec**| 176.10 | 176.10 | 0.0 | 변화 없음 |
| **network_avg_speed_mps** | 14.19 | 14.19 | 0.0 | 변화 없음 |
| **network_arrived_vehicles**| 51.0 | 51.0 | 0.0 | 통과 차량 변화 없음 |
| **extension_count** | 0.0 | 0.0 | 0.0 | **스마트 신호 개입 발생 안 함** |
| **total_extension_sec** | 0.0 | 0.0 | 0.0 | **스마트 신호 개입 발생 안 함** |
| **risk_event_count** | 0 | 0 | 0 | **위험 상황 미관측 (결론 보류)** |
| **senior_risk_event_count** | 0 | 0 | 0 | **위험 상황 미관측 (결론 보류)** |
| **pedestrian_frame_count** | 1079 | 1079 | 0 | 프레임 정상 수집 확인 |
| **vehicle_frame_count** | 5304 | 5304 | 0 | 프레임 정상 수집 확인 |
| **pair_diag_n_within_15m** | 49 | 49 | 0 | 15m 내 근접 관측 |
| **pair_diag_min_distance_m**| 3.28 | 3.28 | 0.0 | 차량-보행자 최소 근접 거리 3.28m |

---

## 5. 현재 결과로 주장 가능한 것 / 주장하면 안 되는 것

| 구분 | 내용 |
|---|---|
| **주장 가능한 것** | - baseline과 smart_crosswalk 시나리오가 동일 조건에서 비교 관측되었다.<br>- 교통 비용 지표가 시나리오별로 관측되었다.<br>- 보행자/차량 frame 수집 여부를 확인했다.<br>- risk event CSV가 정상적으로 생성되었다. |
| **조건부 주장 (보류 중)**| - `risk_event_count > 0`일 때만 안전 proxy 변화를 논의 가능.<br>- `extension_count > 0`일 때만 스마트 신호 개입 효과를 논의 가능. |
| **주장하면 안 되는 것** | - 실제 사고가 줄었다.<br>- 사망자/중상자가 줄었다.<br>- 스마트횡단보도의 효과가 최종 입증됐다.<br>- TAAS 상해 정도 지표와 1:1로 일치한다. |

---

## 6. 한계와 다음 작업
- **단일 환경 한계:** 현재 실행 결과는 단일 횡단보도(`crosswalk_id: 119055`)와 단일 시드(`seed=42`)에 기반한 한정적 관측입니다.
- **해석의 보류:** 결과표에서 `risk_event_count` = 0이므로 이번 시뮬레이션에서는 근접 위험 상황이 발생하지 않아 안전 편익을 관측할 수 없었고 해석을 보류합니다.
- **해석의 보류:** `extension_count` = 0이므로 스마트 개입 조건이 달성되지 않았거나 기능이 실질적으로 작동할 환경이 아니었을 가능성이 있습니다.
- **향후 과제:** 파이프라인 작동이 확인되었으므로, 향후 다수의 횡단보도 후보군을 대상으로 여러 난수 시드(Seed)와 더 긴 시뮬레이션 수행 시간(`sim_duration`)을 부여하여 충분한 통계적 관측값을 확보하는 추가 검증이 필요합니다.

---

## 7. 결론
이 실험은 스마트 횡단보도의 적용 여부에 따른 교통 비용과 안전 Proxy 변화를 관측하고 비교할 수 있는 시뮬레이션 구조를 만들었습니다. 이번 실행 결과는 분석 체계 연습 및 데이터 수집 파이프라인의 유효성을 검증하는 데 목적이 있습니다. 최종적인 정책 효과는 향후 확대된 실험을 통해 Risk Event와 스마트 개입이 통계적으로 충분히 관측된 이후에 주장해야 합니다.
