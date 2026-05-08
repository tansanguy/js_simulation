# 현재 시뮬레이션 분포 가정 정리

## 범위

이 문서는 현재 `smart_crosswalk_sumo` 코드베이스에서 실제로 사용되는 분포 가정과 확률적 규칙을 정리한 것이다.
기준 파일은 다음과 같다.

- `smart_crosswalk_sumo/generate_demand.py`
- `smart_crosswalk_sumo/run_simulations.py`
- `smart_crosswalk_sumo/config/model_assumptions.yaml`
- `smart_crosswalk_sumo/config/model_parameters.yaml`
- `smart_crosswalk_sumo/config/sensitivity_scenarios.yaml`

분포 가정은 크게 1) 입력 수요 생성, 2) 시뮬레이션 내부 차량 도착/이벤트, 3) 센서 및 보행자 속성, 4) 민감도 오버레이로 나뉜다.

## 1. 차량 수요 가정

### 1.1 관측 교통량이 있는 경우

- `traffic_counts_15min.csv`가 있으면 대상 횡단보도 주변 edge들의 15분 교통량을 시간당 값으로 환산한다.
- 여러 edge가 매칭되면 `volume_per_hour`의 대표값으로 평균이 아니라 `median`을 사용한다.
- 즉, 관측치 기반 차량 유입률은
  `veh_per_hour = median(volume * 3600 / interval_sec)` 이다.
- 이는 분포를 새로 추정하는 방식이 아니라, 관측치 집합에서 강건 대표값 하나를 뽑는 규칙이다.

### 1.2 관측 교통량이 없는 경우

- fallback은 `estimated_aadt`에 난수를 더해 시간당 교통량을 만든다.
- 사용 규칙:
  `aadt_noisy = max(1000, estimated_aadt + U{-3000, ..., 3000})`
- 여기서 `U{-3000, ..., 3000}`는 정수 균등분포다.
- 이후
  `hourly_veh = aadt_noisy / 24 / lane_count`
- 마지막으로 프로파일 배수와 고정 계수 `1.8`을 곱한다.
- 결과적으로 fallback 차량 수요는 엄밀한 확률모형보다는
  `AADT + 균등 잡음` 기반 단순화다.

### 1.3 demand profile

- `average`
  - `veh_multiplier = 1.0`
  - 보행 수요 범위 `ped_lambda ∈ [100, 600]`
- `seoul_commute_peak`
  - `veh_multiplier = 2.0`
  - 보행 수요 범위 `ped_lambda ∈ [650, 1100]`

## 2. 보행자 수요 가정

### 2.1 보행자 도착률

- 보행자 도착분포 가정은 설정 YAML에 `exponential`로 명시돼 있다.
- 구현도 이에 맞춰 작동한다.
- 먼저 profile별 범위에서
  `ped_lambda ~ DiscreteUniform[min, max]`
- 이후 평균 간격을
  `mean_gap = 3600 / ped_lambda`
  로 계산한다.
- 각 보행자의 출발시각은
  `t_1 ~ Exponential(mean_gap)`
  `t_{k+1} - t_k ~ Exponential(mean_gap)`
  로 생성된다.
- 즉, 보행자 도착과정은 코드상 포아송 과정과 동치인 지수 inter-arrival 구조다.

### 2.2 고령자 비율

- 각 보행자는
  `P(elderly) = elderly_ratio`
  로 `adult` 또는 `elderly` 타입이 배정된다.
- 구현은 `rng.random() < elderly_ratio`이므로 베르누이 시행이다.
- `elderly_ratio`의 기본값은 후보 테이블의 `노인비율` 컬럼이다.
- 민감도 분석에서는 `elderly_ratio_multiplier` 또는 `elderly_ratio_override`가 여기에 곱해지거나 덮어쓴다.

### 2.3 보행 속도

- 보행자 개체별 실제 분포를 직접 샘플링하지는 않는다.
- 대신 SUMO 보행 타입 속성으로 다음을 준다.
- 일반 보행자:
  - `maxSpeed = 1.2 m/s`
  - `speedDev = 0.1`
- 고령 보행자:
  - `maxSpeed = 0.9 m/s`
  - `speedDev = 0.15`
  - `startupDelay = 1.5 s`
- 즉, 코드가 직접 정규분포를 샘플링하지는 않지만, SUMO 내부에서 `speedDev`에 따라 속도 변동이 들어간다.
- 현재 모델이 명시적으로 관리하는 것은
  - 일반 보행자 기준 속도
  - 고령 보행자 기준 속도
  - 고령 보행자 출발 지연
  이다.

## 3. 차량 도착과 대기행렬 가정

### 3.1 내부 queue proxy의 차량 도착분포

- `run_simulations.py` 내부 queue proxy는 실제 차량 route와 별도로 단순화된 도착모형을 쓴다.
- 기본 설정은 `vehicle_arrival_model = poisson`이다.
- step 길이를 `Δt`라 하면
  `arrival_lambda = vehicle_arrival_rate_per_hour / 3600 * Δt`
- 이때 기본 도착수는
  `arrivals_this_step ~ Poisson(arrival_lambda)`
- 만약 옵션을 바꾸면
  `bernoulli` 모드도 가능하며, 이 경우
  `arrivals_this_step ~ Bernoulli(min(arrival_lambda, 1.0))`
- 현재 기본값은 YAML과 코드 모두 `poisson`이다.

### 3.2 차량 출발/방출

- 출발은 확률분포로 샘플링하지 않는다.
- 포화교통류율과 녹색 여부에 따라 결정론적으로 처리한다.
- 기본값:
  - `saturation_flow_rate_per_hour = 1900 pc/h/ln`
- step당 처리용량은
  `capacity_per_step = saturation_flow_rate_per_hour / 3600 * Δt * lane_count`
- 여기에 외란이 있으면 `capacity_multiplier`를 곱한다.
- 누적 credit를 쌓아 `floor()`로 출발 대수를 계산하므로, 방출부는 결정론적 service process다.

## 4. 외란과 사고 이벤트 가정

### 4.1 기본 시나리오 이벤트

- `best_case`: 이벤트 없음
- `normal_urban`: 고정 시간 `bus_stop`, `accident`
- `congested_urban`: 고정 시간 `bus_stop`, `accident`
- `incident_case`: 고정 시간 `accident`, `bus_stop`

즉, 기본 외란 시나리오는 확률분포가 아니라 고정 시각과 고정 지속시간 템플릿이다.

### 4.2 랜덤 외란 이벤트

- `enable_random_disruptions`가 켜지면 추가 이벤트를 생성한다.
- 현재 랜덤 생성 대상은 코드상 `bus_stop`, `accident` 두 종류다.
- 시뮬레이션 시간 `sim_hours = sim_duration / 3600`
- 이벤트 개수:
  `count ~ Poisson(rate_per_hour * sim_hours)`
- 각 이벤트 시작시각:
  `start ~ Uniform(0, sim_duration - duration)`
- 지속시간은 현재 고정값이다.
  - `bus_stop = 35 s`
  - `accident = 240 s`
- 즉, 랜덤 외란은
  `Poisson count + Uniform start time + fixed duration`
  구조다.

### 4.3 외란 영향 계수

- 사고:
  - `capacity_multiplier_accident = 0.45`
  - `speed_multiplier_accident = 0.45`
- 버스 정차:
  - `capacity_multiplier_bus_stop = 0.75`
  - `speed_multiplier_bus_stop = 0.8`
- 이 값들은 확률분포가 아니라 고정 계수 가정이다.

## 5. 신호 연장과 센서 가정

### 5.1 보행신호 연장 정책

- 기본 cycle:
  - `cycle_time = 120 s`
  - `yellow_time = 4 s`
  - `all_red_time = 3 s`
- smart 시나리오 기본 연장 규칙:
  - `extension_increment = 5 s`
  - `max_extensions = 1`
  - `trigger_remaining = 10 s`

### 5.2 센서 false negative

- 보행자 감지 실패율:
  - `sensor_fn_rate = 0.05`
- 구현은
  `detected_peds and random.random() > sensor_fn_rate`
  일 때만 연장한다.
- 따라서 감지 성공은 베르누이 시행이며
  `P(success) = 0.95`
  `P(false negative) = 0.05`
  로 해석할 수 있다.

## 6. 안전 surrogate와 위험 가정

### 6.1 PET surrogate 분류

- PET 관련 surrogate는 실측 사고확률이 아니라 위험 대리변수다.
- 분류 임계값:
  - `severe`: PET < 1.34
  - `moderate`: 1.34 ≤ PET < 2.88
  - `safe`: PET ≥ 2.88
- 이는 확률분포가 아니라 임계값 기반 분류 규칙이다.

### 6.2 안전위험 점수

- 현재 safety surrogate는
  `safety_risk_score = (PET_B_surrogate_severe + elderly_incomplete_cross * safety_weight) * accident_risk_coefficient * vehicle_speed_risk_multiplier`
- 기본값:
  - `safety_weight = 2.0`
  - `accident_expected_cost = 1,000,000 KRW / risk_unit`
- 즉, 실제 사고확률을 직접 추정하지 않고 surrogate score를 비용형 지표로 환산한다.

## 7. 민감도 분석에서 추가되는 분포/파라미터 오버레이

현재 `sensitivity_scenarios.yaml`에 정의된 주요 오버레이는 다음과 같다.

### 7.1 pedestrian_arrival_rate_multiplier

- `low = 0.5`
- `base = 1.0`
- `high = 1.5`
- `very_high = 2.0`

적용 방식:

- 먼저 `ped_lambda ~ DiscreteUniform[min, max]`
- 이후 `ped_lambda := round(ped_lambda * multiplier)`
- 즉, 도착분포의 형태는 그대로 `exponential inter-arrival`이고, rate만 바뀐다.

### 7.2 elderly_ratio

- `low = 0.8`
- `base = 1.0`
- `high = 1.2`

적용 방식:

- 기본 `elderly_ratio`에 multiplier를 곱하거나 override를 사용한다.
- 최종값은 `[0, 1]`로 clip된다.

### 7.3 walking_speed_profile

- `normal_fast`
  - `normal_ped_speed_mps = 1.35`
  - `elderly_ped_speed_mps = 1.05`
  - `elderly_startup_delay_sec = 1.2`
- `base`
  - `1.20`, `0.90`, `1.5`
- `elderly_slow`
  - `1.10`, `0.75`, `1.8`
- `conservative_slow`
  - `1.00`, `0.70`, `2.2`

### 7.4 vehicle_volume_multiplier

- `low = 0.7`
- `base = 1.0`
- `high = 1.3`
- `congested = 1.6`

적용 방식:

- 관측 기반 또는 fallback으로 계산된 `veh_per_hour`에 곱한다.

### 7.5 vehicle_speed_factor

- `slow`
  - `speed_factor = 0.90`
  - `saturation_flow_multiplier = 0.90`
  - `risk_exposure_multiplier = 0.90`
- `base`
  - `1.00`, `1.00`, `1.00`
- `fast`
  - `1.10`, `1.08`, `1.15`

적용 방식:

- 현재 구현에서는 주로
  - queue service의 `saturation_flow_multiplier`
  - 위험 surrogate의 `risk_exposure_multiplier`
  로 반영된다.

### 7.6 accident_risk_coefficient

- `low = 0.80`
- `base = 1.00`
- `high = 1.25`

적용 방식:

- `safety_risk_score`와 `accident_expected_value`에 배수로 반영된다.
- 실제 사고확률을 추정하는 값이 아니라, surrogate 민감도 계수다.

### 7.7 green_extension_policy

- `no_extension`
  - `extension_increment_sec = 0`
  - `max_extensions = 0`
  - `trigger_remaining_sec = 0`
- `base_extension`
  - `5`, `1`, `10`
- `aggressive_extension`
  - `7`, `2`, `14`

적용 방식:

- smart 시나리오의 연장 정책 파라미터를 직접 덮어쓴다.

## 8. 분포가 아닌 결정론 가정

다음은 확률분포라기보다 구조적/결정론적 단순화다.

- baseline은 연장 없음, smart는 조건부 연장
- `ped_green_base`는 전처리 단계에서 횡단거리 기반으로 계산
- 차량 지체 비용:
  `vehicle_delay_cost = total_vehicle_delay / 3600 * 15000`
- 외란 이벤트의 영향 계수는 고정
- queue discharge는 포화교통류율 기반 결정론
- PET 임계값은 고정
- 실제 사고확률 모델은 없음

## 9. 요약

현재 시뮬레이션의 핵심 분포 가정은 다음과 같이 요약된다.

- 차량 도착:
  - 관측치가 있으면 median 기반 대표값
  - 없으면 `AADT + discrete uniform noise`
  - 내부 queue proxy는 step별 `Poisson` 도착
- 보행자 도착:
  - `ped_lambda ~ DiscreteUniform(profile range)`
  - inter-arrival은 `Exponential(mean_gap)`
- 고령자 여부:
  - `Bernoulli(elderly_ratio)`
- 센서 실패:
  - `Bernoulli(sensor_fn_rate = 0.05)`
- 랜덤 외란:
  - event count는 `Poisson`
  - start time은 `Uniform`
  - duration은 fixed

보고서에서는 위 가정이 “실제 관측치를 완전 재현하는 예측모형”이 아니라, 후보 간 상대 비교를 위한 단순화된 시나리오 모델이라는 점을 명시하는 것이 적절하다.
