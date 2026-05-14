# Simulation Process Explanation
## 스마트 횡단보도 SUMO 시뮬레이션 — 연구 프로세스 설명

Repo guardrails: [`AGENTS.md`](./AGENTS.md)

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

---

# Final Results Reading Guide
## 스마트 횡단보도 시뮬레이션 결과 읽는 법

### 1. 이 문서의 목적

이 문서는 SUMO 스마트 횡단보도 시뮬레이션 결과를 사람이 읽는 방법을 안내한다.

**중요한 원칙:**
- 모든 CSV를 읽는 것이 목표가 아니다.
- 최종적으로는 `human_readable/` 폴더의 핵심 파일만 보면 된다.
- 디버그 CSV와 로그 파일은 결과 신뢰성 확인용이며 발표/보고서에 직접 쓰지 않는다.

### 2. 결과 폴더 구조

```
result/<run_name>/
  human_readable/           ← 발표/보고서에 쓸 파일이 여기 있다
    summary_report.md           최종 요약 보고서 (가장 먼저 읽는다)
    final_decision_table.csv    후보별 최종 판단표 (발표에 바로 사용)
    safety_benefit_summary.csv  안전 편익 요약
    traffic_cost_summary.csv    교통 비용 요약
    per_crosswalk_summary.csv   후보별 핵심 결과 비교

  machine_readable/         ← 재현성/검증용, 발표자는 읽지 않아도 됨
    metrics_full.csv            전체 시뮬레이션 지표
    comparison_full.csv         baseline vs smart 전체 비교

  raw/                      ← 원본 시뮬레이션 산출물
  debug/                    ← TLS 검증, 네트워크 매칭 등 디버그 파일
  logs/                     ← 런타임 로그, SUMO stdout/stderr
  figures/                  ← 시각화 그래프
```

### 3. 사람이 읽어야 하는 핵심 파일

#### `human_readable/summary_report.md`

**가장 먼저 읽는 파일.** 한 번의 시뮬레이션 결과를 사람이 읽는 형태로 요약한 보고서다.

포함 내용:
- 시뮬레이션 실행 정보 (모드, 기간, 시드, 후보 수)
- 추천/조건부/비추천/무효 후보 수
- 안전 편익 핵심 해석
- 교통 비용 핵심 해석
- 주요 경고 및 한계

#### `human_readable/final_decision_table.csv`

**발표와 보고서에서 가장 직접적으로 사용하는 파일.**

| 컬럼 | 설명 |
|---|---|
| `crosswalk_id` | 횡단보도 식별자 |
| `location_name` | 위치명 |
| `simulation_status` | 시뮬레이션 유효성 (`valid` / `invalid` / `not_simulated`) |
| `recommendation` | 최종 추천 (`Recommended` / `Conditional` / `Not Recommended` / `Invalid`) |
| `rank` | 추천 순위 |
| `overall_score` | 종합 점수 |
| `safety_benefit_score` | 안전 편익 점수 |
| `traffic_cost_score` | 교통 비용 점수 (낮을수록 비용이 작음) |
| `net_benefit_score` | 순편익 점수 (안전 편익 - 교통 비용) |
| `avg_vehicle_delay_change` | baseline 대비 차량 평균 지연시간 변화(초) |
| `notes` | 주요 주의사항 |

#### `human_readable/safety_benefit_summary.csv`

보행자 안전 측면의 개선 효과 요약.

| 컬럼 | 설명 |
|---|---|
| `crosswalk_id` | 횡단보도 식별자 |
| `simulation_status` | 시뮬레이션 유효성 |
| `pedestrian_benefit_proxy` | 보행자 이익 proxy 지표 (위험 이벤트 감소 등) |
| `elderly_accident_ratio` | 고령자 사고 비율 (TAAS 기반 calibration 값) |
| `severe_injury_ratio` | 중상 비율 (TAAS 기반 calibration 값) |
| `safety_benefit_score` | 안전 편익 종합 점수 |
| `safety_interpretation` | 사람이 읽을 수 있는 해석 문장 |

#### `human_readable/traffic_cost_summary.csv`

차량/교통 흐름 측면의 비용 요약.

| 컬럼 | 설명 |
|---|---|
| `crosswalk_id` | 횡단보도 식별자 |
| `simulation_status` | 시뮬레이션 유효성 |
| `avg_vehicle_delay_baseline` | baseline 차량 평균 지연(초) |
| `avg_vehicle_delay_smart` | smart 시나리오 차량 평균 지연(초) |
| `avg_vehicle_delay_change` | 변화량 (양수 = 지연 증가 = 교통 비용 발생) |
| `avg_travel_time_change` | 통행시간 변화(초) |
| `throughput_change` | 처리량 변화 |
| `traffic_cost_score` | 교통 비용 종합 점수 |
| `traffic_interpretation` | 사람이 읽을 수 있는 해석 문장 |

#### `human_readable/per_crosswalk_summary.csv`

각 스마트 횡단보도 후보별 핵심 결과를 한 줄로 요약한 비교표.

| 컬럼 | 설명 |
|---|---|
| `crosswalk_id` | 횡단보도 식별자 |
| `location_name` | 위치명 |
| `simulation_status` | 시뮬레이션 유효성 |
| `rank` | 추천 순위 |
| `recommendation` | 최종 추천 |
| `safety_benefit_score` | 안전 편익 점수 |
| `traffic_cost_score` | 교통 비용 점수 |
| `net_benefit_score` | 순편익 점수 |
| `key_reason` | 한 줄 핵심 이유 |

### 4. 안전 편익 읽는 법

#### 안전 편익이란?

스마트 횡단보도 도입으로 보행자 사고위험 또는 사고 심각도가 얼마나 줄어드는지를 의미한다.

#### 중요한 한계 — 반드시 알고 읽을 것

> **SUMO는 실제 사고를 직접 발생시키지 않는다.**

이 프로젝트에서 안전 편익은 다음 방식으로 추정한다:
1. SUMO 시뮬레이션에서 차량-보행자 근접 이벤트(`risk_event_count`)를 탐지
2. TAAS 사고 데이터(`taas_raw.xlsx`)에서 고령자 사고 비율, 상해 정도 비율 등을 추출
3. 두 데이터를 결합하여 위험 감소 지표를 추정

따라서 안전 편익은:
- **"실제 사고 건수 예측값"이 아니다**
- **"후보 간 상대 비교와 정책 판단을 위한 위험 감소 지표"**로 읽어야 한다

#### `risk_event_count = 0`인 경우

`risk_event_count = 0`이면 **안전 편익 정량 결론은 보류**다.

SUMO는 횡단보도에서 차량을 올바르게 감속/정지시키기 때문에 근접 이벤트가 발생하지 않을 수 있다. 이는 시스템 오류가 아니라 SUMO의 정상 동작이다. 이 경우 `simulation_status = valid`이지만 안전 편익은 측정되지 않은 상태다.

#### `extension_count = 0`인 경우

스마트 신호 연장이 한 번도 발생하지 않았음을 의미한다. 안전 편익과 교통 비용 모두 측정 불가이며, `debug/` 폴더의 진단 카운터로 원인을 확인해야 한다.

### 5. 교통 비용 읽는 법

#### 교통 비용이란?

보행자 신호 연장으로 인해 차량 흐름이 얼마나 나빠지는지를 의미한다.

주요 지표:
- **`avg_vehicle_delay_change`**: 차량 평균 지연시간 증가(초). 양수이면 지연이 늘어난 것.
- **`avg_travel_time_change`**: 평균 통행시간 변화.
- **`throughput_change`**: 단위시간당 통과 차량 수 변화.

#### 해석 기준

| 값 | 해석 |
|---|---|
| 지연 증가 < 2초 | 교통 비용 낮음 — 허용 가능 |
| 지연 증가 2~5초 | 교통 비용 중간 — 추가 검토 필요 |
| 지연 증가 > 5초 | 교통 비용 높음 — 신중하게 판단 |

> **주의**: 이 시뮬레이션은 이상적인 도로 흐름을 가정한다. 실제 도로에서는 버스 정차, 불법주정차, 사고, 병목 등도 정체 원인이므로 교통 비용이 실제보다 낮게 추정될 수 있다.

### 6. 최종 판단 읽는 법

`recommendation` 컬럼의 4가지 값:

| 값 | 의미 |
|---|---|
| `Recommended` | 안전 편익이 크고 교통 비용이 작거나 허용 가능한 후보 |
| `Conditional` | 안전 편익은 있으나 교통 비용이 크거나 추가 검증이 필요한 후보 |
| `Not Recommended` | 안전 편익이 작거나 교통 비용이 과도한 후보 |
| `Invalid` | SUMO 네트워크 매칭 실패, ghost TLS, pedestrian route 문제 등으로 유효한 비교가 불가능한 후보 |

> `Invalid`는 **실패**가 아니다. 현재 네트워크 조건에서 시뮬레이션이 유효하지 않다는 의미이며, 네트워크를 재구성하면 향후 포함될 수 있다.

### 7. 결과를 발표/보고서에 쓰는 방법

#### 발표 핵심 자료 (이것만 보여준다)

1. `summary_report.md`의 결론 부분
2. `final_decision_table.csv`의 상위 후보 5~10개
3. `figures/safety_vs_traffic_tradeoff.png` (scatter plot)
4. `figures/candidate_ranking.png` (bar chart)

#### 보조 자료

- `safety_benefit_summary.csv`: 안전 편익 근거 설명 시
- `traffic_cost_summary.csv`: 교통 비용 근거 설명 시

#### 발표에서 하지 말아야 할 것

- 모든 CSV를 슬라이드에 넣는 것
- `debug/`, `logs/`, `raw/` 폴더 내용을 발표 자료로 쓰는 것
- `risk_event_count = 0`인 경우를 "사고 없음"으로 해석하는 것
- delta 값을 인과관계로 해석하는 것 ("스마트 횡단보도가 지연을 X초 줄였다" → 불가)

### 8. 파일별 분류 요약

| 폴더/파일 | 발표용 | 검증용 | 설명 |
|---|:---:|:---:|---|
| `human_readable/summary_report.md` | ✅ | | 최종 요약 보고서 |
| `human_readable/final_decision_table.csv` | ✅ | | 후보별 최종 판단표 |
| `human_readable/safety_benefit_summary.csv` | ✅ | | 안전 편익 요약 |
| `human_readable/traffic_cost_summary.csv` | ✅ | | 교통 비용 요약 |
| `human_readable/per_crosswalk_summary.csv` | ✅ | | 후보 비교표 |
| `machine_readable/*.csv` | | ✅ | 전체 지표, 재현성 용 |
| `debug/*.csv` | | ✅ | TLS/네트워크 검증용 |
| `logs/*.log` | | ✅ | 런타임 로그 |
| `raw/` | | ✅ | 원본 시뮬레이션 출력물 |
| `figures/*.png` | ✅ | | 시각화 |

---

# 최소 외란 4종의 선행연구 기반 분포 설계

## 핵심 결론

원문을 다시 확인해 보니, 초안에서 잡았던 방향은 맞았지만 분포 선택은 조금 더 정교하게 다듬는 편이 좋습니다. 가장 연구답게 정리하면, `bus_stop`은 **zero mass를 가진 inverse Gaussian 계열**, `accident`는 **severity를 나눈 Weibull 계열**, `illegal_stop`은 **Poisson 도착 + Exponential 지속시간**, `pedestrian_burst`는 **기본 Poisson, 군집 시 Negative Binomial**이 가장 직접적으로 논문 내용과 맞습니다. 즉 이번 수정의 핵심은 이벤트를 많이 늘리는 것이 아니라, 현재 모델의 고정 지속시간 가정을 선행연구에서 확인한 **분포 family**로 교체하고, `illegal_stop`과 `pedestrian_burst`를 같은 수준의 확률 이벤트로 추가하는 데 있습니다.

중요한 점은, 네 편의 논문이 모두 "파라미터 숫자"를 그대로 가져오라고 말해 주는 것은 아니라는 점입니다. 대신 이 논문들은 **어떤 분포 family가 맞는지**, **어떤 이질성 축을 반드시 나눠야 하는지**, **언제 단순 모형이 깨지는지**를 알려 줍니다. 그래서 지금 단계에서 가장 설득력 있는 방식은 **분포 family는 논문에서 가져오고, 파라미터는 현지 자료로 다시 적합**하는 것입니다.

## 논문 심층 요약

### 버스 정차 논문

APC/AVL 자료를 이용해 3개 주요 노선의 버스 dwell time을 분석한 연구입니다. 자료는 2021년 8월부터 2022년 8월까지 1년치였고, 43개 정류장 중 15개는 평균과 중앙값 dwell이 거의 0에 가까웠고, 반대로 7개 정류장은 평균 dwell이 20초를 넘었습니다. 분포 적합에서는 inverse Gaussian, log-normal, power log-normal, Fisk, Johnson's SU가 반복적으로 좋은 후보였고, 본문에 예시로 제시된 두 정류장에서는 inverse Gaussian이 가장 좋은 적합을 보였습니다. 이 논문에서 직접 읽히는 메시지는 "버스 정차시간은 고정치가 아니라, **정차 안 하는 경우의 질량점과 정차하는 경우의 우측 꼬리 분포가 공존하는 혼합 구조**"라는 점입니다.

### 사고/접촉사고 논문

traffic incident duration 문헌 전체를 구조적으로 정리한 review입니다. 이 리뷰는 incident duration이 detection/reporting, dispatch, travel, clearance의 여러 phase 정의에 따라 다르게 측정될 수 있고, 시간대·요일 같은 시간 요인, 차량 수·특수차량 개입·사상자 수 같은 incident 특성, 위치·도로 상태·교통량·기상 등 다양한 요인에 의해 매우 이질적이라고 정리합니다. 분포 측면에서는 log-normal, log-logistic, Weibull, generalized F가 반복적으로 등장하며, hazard-based duration model, 특히 parametric AFT 모형이 많이 쓰였다고 요약합니다. 이 리뷰는 **incident type별로, 그리고 duration phase별로 적절한 분포가 달라질 수 있다**고 강조합니다.

### 불법 주정차 논문

parking violation records와 비디오 현장자료를 결합해 double parking의 교통영향을 추정한 연구입니다. 이 논문이 지금 과제와 가장 잘 맞는 이유는, 불법 주정차를 단순 설명변수가 아니라 **확률적 사건**으로 직접 다룬다는 점입니다. 거시 모형으로는 M/M/∞ queueing model을 사용했는데, 이 구조는 곧 **도착은 Poisson process**, **지속시간은 Exponential service time**이라는 뜻입니다.

### 보행자 군집 도착 논문

신호교차로 횡단보도 3곳에서 수집한 자료를 바탕으로 pedestrian equivalent factor를 계산한 연구인데, 우리 과제에서는 arrival model이 특히 중요합니다. 논문은 pedestrian arrival를 일정 시간 간격의 count process로 보고, **보행량이 작고 분산이 평균과 비슷할 때는 Poisson**, **보행량이 커지고 분산이 평균보다 커질 때는 Negative Binomial**이 적절하다고 명시합니다.

## 서울 중구용 분포 채택 원칙

이 네 편을 현재 시뮬레이터 관점으로 다시 번역하면, **bus_stop과 accident는 "지속시간 family"를 문헌에서 가져오고**, **illegal_stop과 pedestrian_burst는 "사건 발생과 지속시간 구조 자체"를 문헌에서 가져오는** 형태라고 보는 것이 정확합니다. 다시 말해, bus와 accident 쪽 논문은 duration이 어떤 꼴이어야 하는지를 강하게 말해 주지만, 유한 시뮬레이션 구간 안에서 count를 어떻게 생성할지는 직접 정의하지 않습니다. 그래서 시뮬레이터에서는 `Poisson` 또는 시간대별 `NHPP`를 **event generator overlay**로 두고, 정작 문헌에서 가져와야 할 핵심은 `bus_stop`의 inverse-Gaussian형 오른쪽 꼬리, `accident`의 Weibull형 duration과 severity/type stratification입니다. 반대로 `illegal_stop`은 Gao & Ozbay가 M/M/∞를 전면에 세웠기 때문에 `Poisson arrival + Exponential duration`을 거의 그대로 차용할 수 있고, `pedestrian_burst`는 Zhang et al.이 Poisson과 Negative Binomial의 전환 규칙을 직접 제시하기 때문에 기본 상태와 군집 상태를 이중체계로 나누는 것이 가장 자연스럽습니다.

따라서 서울 중구에 적용할 때는 **분포 family는 이제 확정하고**, 파라미터만 현지 데이터로 다시 맞추면 됩니다. `bus_stop`은 정류장·방향·시간대별 `p_stop`, `μ_bus`, `λ_bus`를 적합하고, `accident`는 severity 또는 incident-type별 Weibull shape/scale을 적합하며, `illegal_stop`은 블록·시간대별 Poisson rate와 Exponential mean을 적합하면 됩니다. `pedestrian_burst`는 5초 또는 10초 bin count로 평균과 분산을 먼저 비교한 뒤, 평시에는 Poisson, 군집 구간에서는 Negative Binomial dispersion parameter를 따로 적합하는 구조가 가장 깔끔합니다.

## 최종 정리 표

| 이벤트 | 논문에서 확인한 핵심 | 우리 시뮬레이션 최종 채택 분포 | 채택 이유 |
|---|---|---|---|
| `bus_stop` | 버스 dwell은 stop-by-stop·time-of-day별 편차가 크고, skip probability가 높으며, positive dwell의 적합 family로 inverse Gaussian, log-normal, Fisk, Johnson's SU 등이 반복적으로 등장했다. | **권장 1안:** `S_stop ~ Bernoulli(1 - p_skip(stop,TOD))`, `D_bus \| S_stop=1 ~ InvGauss(μ_bus, λ_bus)`. | 초안의 `LogNormal`도 후보군 안에는 있지만, 원문을 기준으로 하면 1순위는 inverse Gaussian이다. 또 이 논문은 "정차 안 함"이라는 질량점을 분리해야 한다는 점을 강하게 시사한다. |
| `accident` | incident duration literature는 단일 보편분포를 제시하지 않고, log-normal·log-logistic·Weibull·generalized F가 데이터셋별로 반복된다. | `N_acc ~ NHPP(λ_acc(t))` 또는 데이터가 없으면 `Poisson(λ_acc T)`. `severity ~ Categorical(minor, moderate, severe)`. `D_acc \| severity=s ~ Weibull(k_s, θ_s)`. | **Weibull + severity strata**가 가장 덜 임의적이다. 다만 이 논문 자체도 도시부보다 freeway 자료가 많다고 인정하므로, family만 채택하고 파라미터는 중구 자료로 다시 적합하는 것이 맞다. |
| `illegal_stop` | M/M/∞ queueing model을 사용했기 때문에 사건 도착은 Poisson, 지속시간은 Exponential service time 구조다. | `N_stop ~ Poisson(λ_stop T)`. `D_stop ~ Exponential(β_stop)`. 실무적으로는 `D_stop`을 600초에서 우측 절단해도 무방. | 네 개 이벤트 중 이 row만큼은 논문에서 분포 구조가 가장 직접적으로 나온다. 따라서 **Poisson count + Exponential duration**을 그대로 채택하는 것이 가장 깔끔하다. |
| `pedestrian_burst` | 보행자 도착은 저유량에서 Poisson, 고유량·고분산 상태에서는 Negative Binomial로 설명된다고 논문이 직접 제시한다. | **평시:** `N_{Δt} ~ Poisson(λ_base Δt)`. **군집 구간:** `N_{Δt} ~ NegBin(μ_burst, r)`. | 기존 코드의 "λ multiplier overlay"는 방향은 맞지만, 원문 기준으로는 군집 상태를 **overdispersion이 있는 count model**로 바꾸는 편이 더 정확하다. |

결론을 한 문장으로 압축하면, 이번 4편을 기반으로 한 최소 세트의 최종 분포는 **`bus_stop = Bernoulli stop/no-stop + Inverse Gaussian duration`, `accident = NHPP/Poisson count + Weibull duration + severity strata`, `illegal_stop = Poisson count + Exponential duration`, `pedestrian_burst = baseline Poisson + burst Negative Binomial`**입니다. 이 구성이 선행연구를 가장 정직하게 반영하면서도, 지금 시뮬레이터 구조에 바로 옮겨 넣기 쉬운 형태입니다.
