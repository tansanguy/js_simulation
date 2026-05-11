# TAAS 기반 Baseline 현실성 보정 및 스마트 횡단보도 시뮬레이션 반영 프롬프트

## 목적

현재 스마트 횡단보도 SUMO 시뮬레이션은 단순히 실행 가능 여부만 보는 것이 아니라, **기본 상태(baseline)가 실제 서울 중구 보행자 사고 데이터와 어느 정도 유사한지**를 검증해야 한다.

따라서 `taas_raw.xlsx`에서 얻을 수 있는 실제 사고 분포, 특히 다음 두 지표를 baseline 시뮬레이션에 반영한다.

1. **고령자 사고 비율**
2. **상해 정도 비율**  
   - 사망
   - 중상
   - 경상
   - 부상신고

중요한 점은 TAAS 비율을 스마트 횡단보도 적용 결과에도 매번 강제로 맞추는 것이 아니다.  
TAAS는 **baseline 현실성 보정 기준**으로만 사용하고, 스마트 횡단보도 적용 시나리오에는 baseline에서 보정된 동일 기준을 고정 적용한다.

---

## 데이터 근거

분석 대상 파일:

```text
taas_raw.xlsx
```

확인된 주요 컬럼:

```text
구분번호
발생년월
주야
시군구
사고내용
법규위반
노면상태
기상상태
도로형태
가해운전자 차종
피해운전자 성별
피해운전자 연령대
피해운전자 상해정도
```

TAAS 기준 상해 정도 정의:

- **사망**: 교통사고 발생 후 30일 이내 사망
- **중상**: 3주 이상의 치료를 요하는 부상
- **경상**: 5일 이상 3주 미만의 치료를 요하는 부상
- **부상신고**: 5일 미만의 치료를 요하는 부상

출처:

```text
https://taas.koroad.or.kr/sta/acs/exs/wordArngPopup.do
https://taas.koroad.or.kr/gis/mcm/mcl/initMap.do?menuId=GIS_GMP_STS_RSN
```

---

## `taas_raw.xlsx` 분석 결과

### 1. 서울특별시 중구 전체 기준

| 항목 | 값 |
|---|---:|
| 총 사고 수 | 2,005건 |
| 정상 상해정도 분류 사고 수 | 2,000건 |
| 고령자, 65세 이상 피해자 비율 | 20.07% |
| 사망 | 2.50% |
| 중상 | 55.40% |
| 경상 | 36.45% |
| 부상신고 | 5.65% |

### 2. 서울특별시 중구 횡단보도 관련 사고 기준

`도로형태`에 `횡단보도`가 포함된 사고를 기준으로 한다.

| 항목 | 값 |
|---|---:|
| 중구 횡단보도 관련 사고 수 | 611건 |
| 고령자, 65세 이상 피해자 비율 | 17.51% |
| 사망 | 1.15% |
| 중상 | 53.85% |
| 경상 | 39.77% |
| 부상신고 | 5.24% |

### 3. 최근 5개년, 2020~2024년 중구 전체 기준

| 항목 | 값 |
|---|---:|
| 사고 수 | 368건 |
| 고령자, 65세 이상 피해자 비율 | 28.53% |
| 사망 | 1.63% |
| 중상 | 51.77% |
| 경상 | 41.42% |
| 부상신고 | 5.18% |

### 4. 최근 5개년, 2020~2024년 중구 횡단보도 관련 사고 기준

| 항목 | 값 |
|---|---:|
| 사고 수 | 89건 |
| 고령자, 65세 이상 피해자 비율 | 24.72% |
| 사망 | 0.00% |
| 중상 | 47.19% |
| 경상 | 48.31% |
| 부상신고 | 4.49% |

단, 최근 5개년 중구 횡단보도 관련 사고는 89건으로 표본이 작다.  
따라서 상해정도 보정 기준으로는 `중구 횡단보도 관련 전체기간 611건`을 기본값으로 쓰고, 최근 5개년 값은 민감도 검토 또는 보조 설명으로 사용한다.

---

## 추천 Calibration Target

최종 기준은 다음처럼 둔다.

```json
{
  "region": "서울특별시 중구",
  "target_scope": "crosswalk_related_all_years",
  "n_accidents": 611,
  "senior_rate_reference": {
    "junggu_crosswalk_all_years": 0.1751,
    "junggu_all_2020_2024": 0.2853,
    "junggu_crosswalk_2020_2024": 0.2472
  },
  "severity_distribution": {
    "fatal": 0.0115,
    "serious": 0.5385,
    "minor": 0.3977,
    "injury_report": 0.0524
  }
}
```

---

## 핵심 설계 원칙

### 잘못된 방식

TAAS 비율을 사용해서 사고 상해정도를 단순 랜덤 배정하지 말 것.

예를 들어 다음 방식은 피한다.

```text
사망 1.15% 확률
중상 53.85% 확률
경상 39.77% 확률
부상신고 5.24% 확률
```

이렇게만 구현하면 시뮬레이션이 현실을 모사한 것이 아니라, 결과 라벨을 사후에 랜덤으로 붙인 것에 가깝다.

### 올바른 방식

1. SUMO 시뮬레이션에서 차량-보행자 위험 이벤트를 탐지한다.
2. 각 위험 이벤트에 대해 위험 점수를 계산한다.
3. baseline에서 위험 점수 분포가 TAAS 상해정도 분포와 유사하도록 cutoff를 보정한다.
4. 보정된 cutoff를 스마트 횡단보도 적용 시나리오에 그대로 적용한다.
5. baseline 대비 스마트 횡단보도 적용 후 위험 이벤트 수, 고령자 위험 이벤트 비율, 중상 이상 비율이 어떻게 바뀌는지 비교한다.

---

## 구현 요청

아래 내용을 코드베이스에 반영해줘.

### 1. TAAS calibration target 추출 스크립트 추가

파일 예시:

```text
smart_crosswalk_sumo/calibration/extract_taas_calibration_targets.py
```

기능:

- 입력: `data/taas_raw.xlsx`
- 출력: `data/taas_calibration_targets.json`
- 필터링:
  - `시군구 == "서울특별시 중구"`
  - 횡단보도 관련 기준은 `도로형태` 컬럼에 `"횡단보도"`가 포함된 행
  - 최근 5개년 기준은 `발생년월`에서 연도를 추출하여 2020~2024년 필터링
- 산출값:
  - 전체 사고 수
  - 정상 상해정도 분류 사고 수
  - 고령자 비율
  - 사망/중상/경상/부상신고 비율
  - 기본 calibration target

고령자 판정:

- `피해운전자 연령대`에서 숫자를 추출한다.
- 65세 이상이면 senior로 처리한다.
- `65세이상`, `70세`, `80세이상` 등 표현이 섞여 있을 수 있으므로 문자열 처리에 주의한다.

상해정도 표준화:

- `피해운전자 상해정도`를 다음 내부 라벨로 매핑한다.

```text
사망 -> fatal
중상 -> serious
경상 -> minor
부상신고 -> injury_report
```

---

### 2. 위험 이벤트 탐지 모듈 추가

파일 예시:

```text
smart_crosswalk_sumo/safety/risk_event_detector.py
```

목표:

SUMO에서 실제 사고가 직접 발생하지 않아도, 차량과 보행자의 근접 상황을 기반으로 `conflict event`를 정의한다.

위험 이벤트 후보 조건 예시:

- 보행자와 차량의 최소 거리 `min_distance_m`가 특정 임계값 이하
- TTC(Time-To-Collision)가 특정 임계값 이하
- 차량 속도가 일정 수준 이상
- 보행자가 횡단 중인데 차량이 접근 중
- 보행 신호 잔여시간이 부족한 상태에서 횡단 중
- 차량 신호/보행 신호 충돌 가능성이 있는 상태

출력 필드 예시:

```text
run_name
seed
scenario
time_s
crosswalk_id
pedestrian_id
vehicle_id
pedestrian_age_group
is_senior
vehicle_speed_mps
min_distance_m
ttc_s
signal_state
ped_remaining_crossing_time_s
risk_score
```

---

### 3. 위험 점수 모델 추가

파일 예시:

```text
smart_crosswalk_sumo/safety/injury_severity_model.py
```

기능:

각 위험 이벤트에 대해 `risk_score`를 계산한다.

기본 구조 예시:

```text
risk_score =
    w_speed * speed_component
  + w_ttc * ttc_component
  + w_distance * distance_component
  + w_signal * signal_component
  + w_senior * senior_component
  + w_remaining_time * remaining_time_component
```

권장사항:

- 점수는 0~1 범위로 정규화한다.
- 차량 속도가 높을수록 위험 증가
- TTC가 낮을수록 위험 증가
- 최소 거리가 짧을수록 위험 증가
- 횡단 잔여시간이 부족할수록 위험 증가
- 고령자일 경우 위험 점수에 가중치를 부여
- 단, 고령자 가중치가 결과를 과도하게 지배하지 않도록 상한을 둔다.

---

### 4. Baseline calibration 모듈 추가

파일 예시:

```text
smart_crosswalk_sumo/calibration/calibrate_baseline_to_taas.py
```

기능:

baseline 시뮬레이션 결과의 위험 이벤트를 이용해서 TAAS 기준에 맞는 상해정도 cutoff를 산출한다.

절차:

1. baseline 시뮬레이션에서 나온 risk event CSV를 읽는다.
2. `risk_score` 기준으로 내림차순 정렬한다.
3. TAAS 상해정도 비율에 따라 cutoff를 정한다.

기본 기준:

```text
fatal:        상위 1.15%
serious:      그 다음 53.85%
minor:        그 다음 39.77%
injury_report: 나머지 5.24%
```

4. 산출된 cutoff를 저장한다.

출력 예시:

```text
result/<run_name>/calibration/taas_severity_cutoffs.json
```

예시 구조:

```json
{
  "source": "taas_raw.xlsx",
  "scope": "서울특별시 중구 횡단보도 관련 사고 전체기간",
  "severity_distribution": {
    "fatal": 0.0115,
    "serious": 0.5385,
    "minor": 0.3977,
    "injury_report": 0.0524
  },
  "risk_score_cutoffs": {
    "fatal_min": 0.91,
    "serious_min": 0.47,
    "minor_min": 0.18,
    "injury_report_min": 0.0
  }
}
```

주의:

- 이 cutoff는 baseline에서만 보정한다.
- 스마트 횡단보도 시나리오에서는 새로 보정하지 않고, baseline에서 만든 cutoff를 그대로 적용한다.

---

### 5. Smart scenario 평가 모듈 추가

파일 예시:

```text
smart_crosswalk_sumo/safety/apply_taas_calibrated_severity.py
```

기능:

- baseline에서 저장한 `taas_severity_cutoffs.json`을 읽는다.
- baseline과 smart scenario의 risk event에 동일 cutoff를 적용한다.
- 각 이벤트를 `fatal`, `serious`, `minor`, `injury_report`로 분류한다.
- 시나리오별 비교표를 생성한다.

출력 예시:

```text
result/<run_name>/outputs/safety_benefit_summary.csv
result/<run_name>/outputs/safety_benefit_by_crosswalk.csv
result/<run_name>/outputs/safety_benefit_by_age_group.csv
```

요약 지표:

```text
total_risk_events
senior_risk_event_count
senior_risk_event_rate
fatal_count
serious_count
minor_count
injury_report_count
fatal_or_serious_count
fatal_or_serious_rate
risk_event_rate_per_1000_pedestrians
fatal_or_serious_rate_per_1000_pedestrians
```

---

### 6. Baseline 현실성 검증 리포트 추가

파일 예시:

```text
smart_crosswalk_sumo/reports/taas_baseline_validation_report.py
```

출력 예시:

```text
result/<run_name>/outputs/taas_baseline_validation_report.md
```

리포트에 포함할 내용:

1. 사용한 TAAS 기준 설명
2. `taas_raw.xlsx`에서 추출한 기준값
3. baseline 시뮬레이션 결과값
4. TAAS 기준과 baseline 결과의 차이
5. 보정 전/후 비교
6. 스마트 횡단보도 적용 후 비교
7. 한계:
   - SUMO는 사고 자체를 직접 재현하는 도구가 아니므로 conflict event 기반 위험도 평가를 사용함
   - TAAS의 실제 사고 분포는 관측된 사고 데이터이고, SUMO 결과는 위험 이벤트 기반 추정값임
   - 따라서 절대 사고 건수 일치보다 상대적 개선 효과 비교에 초점을 둠

---

## 최종 산출물 구조

아래 파일들이 생성되도록 구현한다.

```text
data/taas_calibration_targets.json

result/<run_name>/calibration/taas_severity_cutoffs.json

result/<run_name>/outputs/risk_events_baseline.csv
result/<run_name>/outputs/risk_events_smart.csv

result/<run_name>/outputs/safety_benefit_summary.csv
result/<run_name>/outputs/safety_benefit_by_crosswalk.csv
result/<run_name>/outputs/safety_benefit_by_age_group.csv

result/<run_name>/outputs/taas_baseline_validation_report.md
```

---

## 보고서용 설명 문장

다음 문장을 보고서에 활용할 수 있게 리포트에도 포함한다.

> 본 연구는 스마트 횡단보도 적용 전 기본 상태의 현실 모사력을 확보하기 위해 TAAS 기반 서울특별시 중구 보행자 사고 데이터를 활용하였다. 특히 피해자 연령대와 상해정도 분포를 기준으로 baseline 시뮬레이션의 위험 이벤트 분포를 보정하였다. 이후 동일한 위험도-상해정도 매핑 기준을 스마트 횡단보도 적용 시나리오에 고정 적용하여 정책 적용 전후의 상대적 안전 편익을 비교하였다.

---

## 구현 시 주의사항

1. TAAS 비율을 smart scenario에 다시 맞추지 말 것.
2. baseline에서 산출된 severity cutoff를 모든 시나리오에 고정 적용할 것.
3. 사고 건수 자체를 맞추려 하지 말고, 위험 이벤트의 분포와 상대적 개선 효과를 비교할 것.
4. 고령자 사고 비율은 단순 랜덤 배정이 아니라, 보행속도, 반응시간, 횡단 잔여시간 부족, 위험점수 가중치에 반영할 것.
5. 최종 리포트에는 “현실 사고 데이터 기반 baseline calibration”과 “정책 적용 전후 상대 비교”를 명확히 구분할 것.
6. 실행 명령어와 검증 명령어를 README 또는 리포트에 함께 남길 것.

---

## 기대 결과

최종적으로 다음과 같은 설명이 가능해야 한다.

```text
본 시뮬레이션은 서울 중구 TAAS 보행자 사고 데이터의 고령자 비율 및 상해정도 분포를 기준으로 baseline 위험도 모델을 보정하였다. 이후 동일한 위험도-상해정도 분류 기준을 스마트 횡단보도 적용 시나리오에 적용하여, 현실 사고 구조를 반영한 조건에서 정책 적용 전후의 안전 편익을 비교하였다.
```

이렇게 구현하면 단순 실행 결과가 아니라, 현실 데이터를 기준으로 검증된 baseline 위에서 스마트 횡단보도 효과를 평가한 것으로 설명할 수 있다.

파일 경로: /Users/junlee/Desktop/2026-1/js/data/raw/taas_raw.xlsx 