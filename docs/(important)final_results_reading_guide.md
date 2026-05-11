# Final Results Reading Guide
## 스마트 횡단보도 시뮬레이션 결과 읽는 법

---

## 1. 이 문서의 목적

이 문서는 SUMO 스마트 횡단보도 시뮬레이션 결과를 사람이 읽는 방법을 안내한다.

**중요한 원칙:**
- 모든 CSV를 읽는 것이 목표가 아니다.
- 최종적으로는 `human_readable/` 폴더의 핵심 파일만 보면 된다.
- 디버그 CSV와 로그 파일은 결과 신뢰성 확인용이며 발표/보고서에 직접 쓰지 않는다.

---

## 2. 결과 폴더 구조

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

---

## 3. 사람이 읽어야 하는 핵심 파일

### `human_readable/summary_report.md`

**가장 먼저 읽는 파일.** 한 번의 시뮬레이션 결과를 사람이 읽는 형태로 요약한 보고서다.

포함 내용:
- 시뮬레이션 실행 정보 (모드, 기간, 시드, 후보 수)
- 추천/조건부/비추천/무효 후보 수
- 안전 편익 핵심 해석
- 교통 비용 핵심 해석
- 주요 경고 및 한계

### `human_readable/final_decision_table.csv`

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

### `human_readable/safety_benefit_summary.csv`

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

### `human_readable/traffic_cost_summary.csv`

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

### `human_readable/per_crosswalk_summary.csv`

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

---

## 4. 안전 편익 읽는 법

### 안전 편익이란?

스마트 횡단보도 도입으로 보행자 사고위험 또는 사고 심각도가 얼마나 줄어드는지를 의미한다.

### 중요한 한계 — 반드시 알고 읽을 것

> **SUMO는 실제 사고를 직접 발생시키지 않는다.**

이 프로젝트에서 안전 편익은 다음 방식으로 추정한다:
1. SUMO 시뮬레이션에서 차량-보행자 근접 이벤트(`risk_event_count`)를 탐지
2. TAAS 사고 데이터(`taas_raw.xlsx`)에서 고령자 사고 비율, 상해 정도 비율 등을 추출
3. 두 데이터를 결합하여 위험 감소 지표를 추정

따라서 안전 편익은:
- **"실제 사고 건수 예측값"이 아니다**
- **"후보 간 상대 비교와 정책 판단을 위한 위험 감소 지표"**로 읽어야 한다

### `risk_event_count = 0`인 경우

`risk_event_count = 0`이면 **안전 편익 정량 결론은 보류**다.

SUMO는 횡단보도에서 차량을 올바르게 감속/정지시키기 때문에 근접 이벤트가 발생하지 않을 수 있다. 이는 시스템 오류가 아니라 SUMO의 정상 동작이다. 이 경우 `simulation_status = valid`이지만 안전 편익은 측정되지 않은 상태다.

### `extension_count = 0`인 경우

스마트 신호 연장이 한 번도 발생하지 않았음을 의미한다. 안전 편익과 교통 비용 모두 측정 불가이며, `debug/` 폴더의 진단 카운터로 원인을 확인해야 한다.

---

## 5. 교통 비용 읽는 법

### 교통 비용이란?

보행자 신호 연장으로 인해 차량 흐름이 얼마나 나빠지는지를 의미한다.

주요 지표:
- **`avg_vehicle_delay_change`**: 차량 평균 지연시간 증가(초). 양수이면 지연이 늘어난 것.
- **`avg_travel_time_change`**: 평균 통행시간 변화.
- **`throughput_change`**: 단위시간당 통과 차량 수 변화.

### 해석 기준

| 값 | 해석 |
|---|---|
| 지연 증가 < 2초 | 교통 비용 낮음 — 허용 가능 |
| 지연 증가 2~5초 | 교통 비용 중간 — 추가 검토 필요 |
| 지연 증가 > 5초 | 교통 비용 높음 — 신중하게 판단 |

> **주의**: 이 시뮬레이션은 이상적인 도로 흐름을 가정한다. 실제 도로에서는 버스 정차, 불법주정차, 사고, 병목 등도 정체 원인이므로 교통 비용이 실제보다 낮게 추정될 수 있다.

---

## 6. 최종 판단 읽는 법

`recommendation` 컬럼의 4가지 값:

| 값 | 의미 |
|---|---|
| `Recommended` | 안전 편익이 크고 교통 비용이 작거나 허용 가능한 후보 |
| `Conditional` | 안전 편익은 있으나 교통 비용이 크거나 추가 검증이 필요한 후보 |
| `Not Recommended` | 안전 편익이 작거나 교통 비용이 과도한 후보 |
| `Invalid` | SUMO 네트워크 매칭 실패, ghost TLS, pedestrian route 문제 등으로 유효한 비교가 불가능한 후보 |

> `Invalid`는 **실패**가 아니다. 현재 네트워크 조건에서 시뮬레이션이 유효하지 않다는 의미이며, 네트워크를 재구성하면 향후 포함될 수 있다.

---

## 7. 결과를 발표/보고서에 쓰는 방법

### 발표 핵심 자료 (이것만 보여준다)

1. `summary_report.md`의 결론 부분
2. `final_decision_table.csv`의 상위 후보 5~10개
3. `figures/safety_vs_traffic_tradeoff.png` (scatter plot)
4. `figures/candidate_ranking.png` (bar chart)

### 보조 자료

- `safety_benefit_summary.csv`: 안전 편익 근거 설명 시
- `traffic_cost_summary.csv`: 교통 비용 근거 설명 시

### 발표에서 하지 말아야 할 것

- 모든 CSV를 슬라이드에 넣는 것
- `debug/`, `logs/`, `raw/` 폴더 내용을 발표 자료로 쓰는 것
- `risk_event_count = 0`인 경우를 "사고 없음"으로 해석하는 것
- delta 값을 인과관계로 해석하는 것 ("스마트 횡단보도가 지연을 X초 줄였다" → 불가)

---

## 8. 파일별 분류 요약

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
