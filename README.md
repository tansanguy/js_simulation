# Simulation Process Explanation
## 스마트 횡단보도 SUMO 시뮬레이션 — 연구 프로세스 설명

---

## 1. 연구 질문

스마트 횡단보도는 보행자가 존재할 때 보행자 신호 시간을 연장하여 보행 안전성을 높일 수 있다. 그러나 신호 연장은 차량 대기시간과 정체를 증가시킬 수 있다. 따라서 본 시뮬레이션은 후보 횡단보도별로 안전 편익과 교통 비용의 trade-off를 비교하여, 어떤 위치에 스마트 횡단보도를 우선 설치하는 것이 합리적인지 평가한다.

---

## 2. 전체 프로세스 요약

```
1. Input data preparation       입력 데이터 준비
         ↓
2. Network construction         SUMO 네트워크 생성
         ↓
3. Crosswalk-to-SUMO matching   횡단보도 ↔ SUMO 네트워크 매핑
         ↓
4. Baseline simulation          기준선 시뮬레이션 실행
         ↓
5. Smart crosswalk simulation   스마트 횡단보도 시나리오 실행
         ↓
6. Safety benefit estimation    안전 편익 추정
         ↓
7. Traffic cost estimation      교통 비용 추정
         ↓
8. Candidate ranking            후보 순위 산정
         ↓
9. Human-readable result export 사람이 읽을 결과물 생성
```

---

## 3. Input data preparation

### 사용 데이터

| 데이터 | 파일 | 설명 |
|---|---|---|
| T1 — 사고 데이터 | `data/T1_accident_crosswalk.csv` | 횡단보도별 사고 이력 |
| T2 — 횡단보도 속성 | `data/T2_crosswalk_features.csv` | 차선 수, 최대 속도, 횡단 길이, 고령자 비율 등 |
| 행정 경계 | `data/junggu_admin_boundary.geojson` | 서울 중구 행정 경계 (네트워크 범위 결정) |
| SUMO 레지스트리 | `registry/junggu_crosswalk_sumo_registry.csv` | 횡단보도별 SUMO 매핑 정보 (TLS ID, edge, ped link 등) |

### 후보 횡단보도 선정

- 실제 중구 행정 데이터 기반이므로 임의로 후보를 버리지 않는다.
- 다만 네트워크 매칭 가능성과 시뮬레이션 가능성에 따라 등급화한다.
- 레지스트리(`registry_status`)에서 `A` 또는 `recovered`인 후보가 시뮬레이션 대상이 된다.

### TAAS 데이터 활용

- 고령자 사고 비율, 상해 정도 비율 등의 calibration에 사용된다.
- 이 비율은 안전 편익 추정 시 가중치로 반영된다.

---

## 4. Network construction

### 방식

SUMO 네트워크는 OpenStreetMap(OSM) 데이터에서 자동 생성된다. 코드에서는 두 가지 모드를 지원한다:

| `network_mode` | 설명 |
|---|---|
| `expanded` | 중구 행정 경계에 buffer(기본 1000m)를 더한 넓은 네트워크 생성 |
| `single_crosswalk` | 특정 횡단보도 주변 소규모 네트워크만 생성 |

### 왜 `expanded` 모드를 쓰는가

- 좁은 네트워크는 주변 도로 흐름을 반영하지 못해 교통 비용을 과소평가한다.
- `buffer_m=1000` 설정은 실제 교통망 영향 범위를 포함하기 위한 설정이다.

---

## 5. Crosswalk-to-SUMO matching

### 목적

실제 횡단보도 좌표를 SUMO 네트워크의 edge, lane, TLS(Traffic Light System)와 연결한다.

### 매칭 기준

- 최대 허용 거리: 기본 50m (`max_match_distance_m`)
- TLS 필수 여부: `require_tls=True`이면 TLS가 없는 후보는 신호 연장 대상에서 제외
- synthetic TLS 주입: 실제 TLS가 없으나 신호화 가능한 위치는 가상 TLS를 삽입할 수 있음

### 매칭 실패 처리

매칭 실패 후보는 버리지 않고, 다음 중 하나로 분류된다:

| 분류 | 의미 |
|---|---|
| `C4_missing_crossing` | SUMO 네트워크에 횡단보도 edge가 없음 |
| `C6_ped_route_failed` | 보행자 경로 생성 실패 |
| `C11_manual_netedit_required` | netedit 수동 수정이 필요한 복잡한 교차로 |
| `topology_repair_required` | 네트워크 재구성 필요 |

이 분류는 `human_readable/final_decision_table.csv`의 `notes` 컬럼에도 반영된다.

---

## 6. Baseline simulation

### 목적

스마트 횡단보도를 적용하지 않은 기본 상태를 시뮬레이션하여, 이후 smart 시나리오와 비교하는 기준선을 만든다.

### 설정

- `simulation_mode=integrated_selected`: 선택된 횡단보도를 포함한 전체 네트워크 시뮬레이션
- 기본 신호 체계 유지, 신호 연장 없음
- 동일한 교통 수요와 보행자 수요로 smart 시나리오와 동일 조건 비교

### 주의사항

baseline이 현실 교통 흐름과 너무 다르면 최종 편익 추정도 설득력이 떨어진다. 실제 교통량 데이터가 있으면 calibration을 통해 baseline을 조정할 수 있다.

---

## 7. Smart crosswalk simulation

### 목적

보행자가 있을 때 보행자 신호를 연장하는 개입 시나리오를 실행하여, baseline 대비 변화량을 측정한다.

### 동작 방식

```
보행 신호 green 상태
    → 잔여 시간이 트리거 윈도우 이내
        → 보행자 감지
            → 신호 연장 실행 (기본 +5초, max 1회)
```

### 측정 지표

- `extension_count`: 실제 신호 연장이 발생한 횟수
- `total_extension_sec`: 총 연장 시간(초)
- baseline 대비 교통 지표 변화
- baseline 대비 안전 proxy 지표 변화

### `extension_count = 0`이 되는 원인

1. 보행 신호 phase가 트리거 윈도우보다 짧아 개입 불가
2. 해당 시간대에 그 교차로에 보행자 수요가 없음
3. 레지스트리 매핑 문제 (ped_link_indices 불일치 등)

`debug/` 폴더의 `smart_extension_*` 진단 카운터로 원인 확인이 가능하다.

---

## 8. Safety benefit estimation

### 핵심 원칙

> SUMO는 실제 사고를 직접 발생시키지 않는다. 안전 편익은 proxy metric과 외부 분포 가정을 통해 추정된다.

### 추정 방식

1. **Risk event 탐지**: SUMO에서 차량-보행자 근접 거리·속도를 기준으로 위험 상황 이벤트 탐지
2. **TAAS calibration**: 고령자 사고 비율, 상해 정도 비율 등으로 위험 이벤트에 가중치 부여
3. **Proxy 지표**: `safety_risk_score`, `accident_expected_value` 등으로 표현

### 이 지표의 의미

- 절대적 사고 건수 예측이 아님
- **후보 간 상대 비교 및 정책 판단용 위험 감소 지표**
- "A가 B보다 안전 편익이 더 크다"는 비교 판단에 사용

---

## 9. Traffic cost estimation

### 측정 지표

| 지표 | 설명 |
|---|---|
| `avg_vehicle_delay_sec` | 차량 평균 지연시간(초) |
| `avg_queue_length` | 평균 대기열 길이 |
| `max_queue_length` | 최대 대기열 길이 |
| `network_avg_travel_time_sec` | 네트워크 평균 통행시간(초) |
| `vehicle_delay_cost` | 차량 지연 비용(원 환산) |
| `network_spillback_rate` | 정체 spillback 비율 |

### 한계

- 이상적인 도로 흐름만 가정하며, 버스 정차·불법주정차·사고·병목 등 실제 이벤트는 반영하지 않음
- 따라서 교통 비용이 실제보다 낮게 추정될 수 있음
- 향후 이벤트 기반 disturbance(`enable_random_disruptions`, `bus_stop_rate_per_hour` 등)를 추가하면 더 현실적인 추정이 가능함

---

## 10. Candidate ranking

### 판단 구조

추천 후보 선정 기준:

1. **시뮬레이션 유효성**: `simulation_status = valid`
2. **안전 편익 대비 교통 비용 비율**: 안전 편익이 크고 교통 비용이 허용 가능한 후보
3. **네트워크 검증 통과**: TLS 매핑, pedestrian route 생성 모두 성공

단순히 사고위험이 높은 곳만 고르는 것이 아니다. 실제 신호 제어가 가능하고 SUMO 네트워크 검증을 통과한 후보여야 한다.

### 분류

| 분류 | 기준 |
|---|---|
| `Recommended` | 안전 편익 크고, 교통 비용 낮거나 허용 가능 |
| `Conditional` | 안전 편익 있으나, 교통 비용 높거나 추가 검증 필요 |
| `Not Recommended` | 안전 편익 작거나, 교통 비용 과도함 |
| `Invalid` | 네트워크 매핑 실패, ghost TLS, 보행자 경로 실패 등 |

---

## 11. Limitations

이 시뮬레이션의 알려진 한계:

1. **SUMO는 실제 사고를 직접 예측하지 않는다.** 안전 편익은 proxy 기반 추정이며 절대적 사고 감소량이 아니다.

2. **네트워크 매칭 실패가 있을 수 있다.** ghost TLS, 보행자 경로 생성 실패 등으로 일부 후보는 `Invalid`로 분류된다. 이는 해당 후보가 나쁜 후보라는 의미가 아니라, 현재 네트워크 조건에서 유효한 비교가 불가능하다는 의미다.

3. **교통량/보행량 데이터가 부족하면 calibration 한계가 있다.** 실제 교통량 데이터 없이는 시뮬레이션 결과와 현실의 차이를 보정하기 어렵다.

4. **모든 실제 도로 이벤트를 반영하지 못한다.** 버스 정차, 불법주정차, 사고, 병목 등은 별도 파라미터(`bus_stop_rate_per_hour` 등)로 설정해야 하며 기본값은 0이다.

5. **시뮬레이션 시간이 짧으면 안전 편익을 측정하기 어렵다.** `sim_duration=600`(10분)은 smoke test 수준이며, 보행자 수요가 충분하지 않으면 `extension_count=0`이 될 수 있다.

---

## 12. Output interpretation

### 핵심 메시지

최종 결과는 **절대적 예언이 아니라 후보 간 상대 비교 도구**다.

중요한 것은:
- "어디가 가장 좋아 보이는가"보다
- **"왜 그 후보가 안전 편익 대비 교통 비용이 합리적인가"를 설명하는 것**

발표/보고서에서 강조해야 할 것:
- 이 방법론은 여러 후보를 동일한 조건 하에서 비교하는 데 강점이 있다
- 매칭/검증을 통과한 후보만 공정하게 비교된다
- Invalid 후보는 실패가 아니라 "현재 네트워크 조건에서 검증 불가"다
- delta 값(변화량)은 관측 차이이지 인과관계가 아니다

---

## 부록: 주요 코드 파일

| 파일 | 역할 |
|---|---|
| `smart_crosswalk_sumo/main.py` | 메인 진입점, 인수 파싱 |
| `smart_crosswalk_sumo/run_simulations.py` | 시뮬레이션 실행 오케스트레이터 |
| `smart_crosswalk_sumo/integrated_mode.py` | integrated_selected 모드 실행 |
| `smart_crosswalk_sumo/collect_metrics.py` | 시뮬레이션 지표 수집 |
| `smart_crosswalk_sumo/smart_extension_logic.py` | 스마트 신호 연장 로직 |
| `smart_crosswalk_sumo/safety/risk_event_detector.py` | 위험 이벤트 탐지 |
| `smart_crosswalk_sumo/calibration/` | TAAS calibration 모듈 |
| `smart_crosswalk_sumo/reporting/final_result_package.py` | 3-CSV 관측 테이블 생성 |
| `smart_crosswalk_sumo/reporting/export_human_readable_results.py` | human_readable 폴더 생성 |
