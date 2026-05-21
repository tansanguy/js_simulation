# Runtime Optimization Plan — Smart Crosswalk SUMO Simulation

> **Status**: Plan Mode (진단 + 설계 only, 코드 수정 없음)
> **Date**: 2026-05-16
> **Scope**: `run_simulations.py` 중심, smoke/minimal 600s 미니런 최적화

---

## 1. Executive Summary

`one_candidate_base_smart_test` 실행 중 Ctrl+C 시점에 `traci.lane.getLastStepMeanSpeed(lane_id)`에서 멈춰 있었다.
이는 매 0.1s step (600s → 6,000 steps) 마다 **모든 monitored lane** 에 대해 3개 TraCI get 호출 (`getLastStepHaltingNumber`, `getLastStepOccupancy`, `getLastStepMeanSpeed`) 을 수행하는 구조가 핵심 병목임을 강하게 시사한다.

**핵심 수치 추정:**
- `traffic_measure_radius_m=500.0` → monitored lane 수: ~50–200개 (네트워크 규모 의존)
- 매 step 당 TraCI lane 호출: `N_lanes × 3` = 150–600회
- 6,000 steps × 600 호출 = **360만 TraCI 왕복** (lane만)
- 여기에 vehicle/person 개별 조회까지 더하면 **총 500만~1,000만 TraCI 호출**

**목표:** smoke 600s baseline+smart 1쌍을 현재 수 분에서 **1분 이내**로 단축, full metric 정합성 유지.

---

## 2. Current Runtime Bottleneck Hypotheses

| # | 가설 | 근거 | 예상 기여도 |
|---|------|------|------------|
| H1 | 매 step lane 순회 (3 TraCI calls/lane) | Ctrl+C 시 `getLastStepMeanSpeed`에서 hang | **60–70%** |
| H2 | 매 step 전체 vehicle 순회 (4 TraCI calls/veh) | `getRoadID`, `getSpeed`, `getLaneID`, `getAccumulatedWaitingTime` | **15–20%** |
| H3 | 매 step person 순회 + edge person ID 조회 | `getIDList`, `getTypeID`, `getLastStepPersonIDs` | **5–10%** |
| H4 | lane/edge XML 레코드 메모리 누적 | 6,000 steps × N_lanes 레코드 in-memory | **3–5%** |
| H5 | `build_surrounding_scope` net.xml 재파싱 | 시뮬레이션 시작 시 1회이므로 낮음 | **<2%** |

---

## 3. Code Hotspot Map

### Table A: 병목 후보 표

| file | function | suspected_bottleneck | evidence | expected_impact | risk |
|------|----------|---------------------|----------|-----------------|------|
| `run_simulations.py:1556-1565` | `run_simulation` inner loop | 매 step `monitored_lanes` 전체 순회: `getLastStepHaltingNumber`, `getLastStepOccupancy`, `getLastStepMeanSpeed` | Ctrl+C hang at `getLastStepMeanSpeed` | **Critical (60-70%)** | sampling interval 도입 시 spillback 감지 지연 가능 |
| `run_simulations.py:1473-1490` | `run_simulation` inner loop | 매 step 전체 vehicle 순회: `getRoadID`, `getAccumulatedWaitingTime`, `getLaneID`, `getSpeed` | 차량 수 증가 시 선형 증가 | **High (15-20%)** | sampling 시 area_vehicle_time_sec 정밀도 감소 |
| `run_simulations.py:1451-1459` | `run_simulation` inner loop | 매 step `person.getIDList()` + 개별 `getTypeID` | person 수 비례 | **Medium (5-10%)** | elderly 분류 누락 위험 낮음 |
| `run_simulations.py:2364-2375` | `run_simulation_integrated` inner loop | 동일 lane 순회 (integrated 모드) | 동일 패턴 | **Critical** | 위와 동일 |
| `run_simulations.py:2317-2335` | `run_simulation_integrated` inner loop | 전체 vehicle 순회 + debug_state 업데이트 | debug_state.update 오버헤드 추가 | **High** | debug_state 생략 시 장애 진단 어려움 |
| `run_simulations.py:1605-1609` | `run_simulation` inner loop | approach_lanes 별도 `getLastStepHaltingNumber` 재호출 | monitored_lanes와 중복 조회 가능 | **Low-Med** | 캐시로 제거 가능 |
| `run_simulations.py:1750-1778` | `run_simulation` post-loop | lane_data/edge_data XML 직렬화 (6000×N 레코드) | 메모리 + I/O | **Low** | sampling 시 자동 감소 |
| `collect_metrics.py:75` | `collect_all` | `sim_duration` default=1800 (main.py default=1800) | 600 미지정 시 3배 longer | **Config risk** | 명시적 가드 필요 |

---

## 4. Proposed Metric Profile Design

### Table C: Metric Profile 표

| metric | smoke | minimal | full | collection_frequency | reason |
|--------|-------|---------|------|---------------------|--------|
| `pedestrian_count` | ✅ | ✅ | ✅ | every step | 핵심 완주 지표 |
| `elderly_pedestrian_count` | ✅ | ✅ | ✅ | every step | 핵심 완주 지표 |
| `extension_count` | ✅ | ✅ | ✅ | event-driven | smart 효과 핵심 |
| `extension_events_log` | ✅ | ✅ | ✅ | event-driven | smart 효과 핵심 |
| `pedestrian_crossing_times` | ❌ | ✅ | ✅ | every step | 보행자 성능 |
| `pedestrian_wait_times` | ❌ | ✅ | ✅ | every step | 보행자 성능 |
| `veh_waits` (per-vehicle) | ❌ | ✅ | ✅ | every step | 차량 지연 |
| `queue_lengths` (approach) | ❌ | ✅ | ✅ | every step | 대기열 |
| `PET_A_proxy` / `PET_B_surrogate` | ❌ | ✅ | ✅ | event-driven | 안전 지표 |
| `lane_halting` (per-lane) | ❌ | ❌ | ✅ | every N steps | 무거운 lane 순회 |
| `lane_occupancy` (per-lane) | ❌ | ❌ | ✅ | every N steps | 무거운 lane 순회 |
| `lane_speeds` (per-lane) | ❌ | ❌ | ✅ | every N steps | 무거운 lane 순회 |
| `surrounding_queue_totals` | ❌ | ❌ | ✅ | every N steps | lane 순회 의존 |
| `direction_queue_totals` | ❌ | ❌ | ✅ | every N steps | lane 순회 의존 |
| `adjacent_tls_queue_totals` | ❌ | ❌ | ✅ | every N steps | lane 순회 의존 |
| `spillback_steps` | ❌ | ❌ | ✅ | every N steps | boundary 감지 |
| `area_vehicle_waits` | ❌ | ❌ | ✅ | every N steps | 주변 차량 |
| `surrounding_speed_samples` | ❌ | ❌ | ✅ | every N steps | 주변 속도 |
| `lane_data_records` XML | ❌ | ❌ | ✅ | every N steps | 디버그 출력 |
| `edge_data_records` XML | ❌ | ❌ | ✅ | every N steps | 디버그 출력 |
| `fcd_vehicle_records` | ❌ | ❌ | `--export_fcd` | every step | FCD 출력 |
| `fcd_person_records` | ❌ | ❌ | `--export_fcd` | every step | FCD 출력 |
| `network_arrival_count` | ✅ | ✅ | ✅ | arrived event | 기본 throughput |
| `vehicle_model_state` (queue model) | ✅ | ✅ | ✅ | every step | 내부 큐 모델 (TraCI 호출 없음) |
| `incident_events` | ✅ | ✅ | ✅ | event-driven | 공정성 |
| `route/trip/net existence` | ✅ | ❌ | ❌ | pre-run | smoke 검증 |

### 설계 원칙

```
--metric-profile smoke    → lane 순회 완전 생략, vehicle 순회 최소화
--metric-profile minimal  → lane 순회 생략, 핵심 per-vehicle/ped 지표만
--metric-profile full     → 기존 동작 100% 유지 (default)
```

---

## 5. Proposed Sampling Interval Design

```
--metric-sample-interval 10   (초 단위, default=0 → 매 step)
```

**적용 범위 (full profile에서만 의미):**
- `lane_halting`, `lane_occupancy`, `lane_speeds` 수집
- `surrounding_queue_totals`, `direction_queue_totals`, `adjacent_tls_queue_totals`
- `lane_data_records`, `edge_data_records` XML 레코드 생성
- `spillback_steps` 감지

**적용하지 않는 지표 (매 step 유지):**
- `vehicle_model_state` (내부 큐 모델, TraCI 호출 없음)
- `extension` 로직 (event-driven)
- `PET_A/B` (event-driven)
- `pedestrian_crossing_times`, `pedestrian_wait_times` (보행자 진입/퇴출 이벤트)

**구현 의사코드:**
```python
# run_simulations.py inner loop, line ~1552
lane_sample_due = (metric_sample_interval <= 0) or (rel_t % metric_sample_interval < traci_step_length)
if collect and lane_sample_due:
    for lane_id in monitored_lanes:
        ...
```

**예상 효과:**
- `--metric-sample-interval 10` + step_length=0.1 → lane 조회 **100배 감소**
- 6,000 steps → 60 lane-sample steps
- 360만 lane TraCI 호출 → 3.6만

---

## 6. Network / Route Reuse Plan

### 현재 상태

| artifact | 재사용 가능? | 조건 |
|----------|------------|------|
| `network.net.xml` | ✅ | `--skip_networks` 이미 존재 |
| `routes_seed*.rou.xml` | ✅ | `--skip_demand` 이미 존재 |
| `peds_seed*.rou.xml` | ✅ | `--skip_demand` 이미 존재 |
| `*.trips.xml` | ✅ | route 파일과 동시 생성 |
| `*_seed*.sumocfg` | ✅ | demand 단계에서 생성 |
| `metadata.json` | ✅ | network 단계에서 생성 |

### 제안: 시간 분리 기록

```json
{
  "network_build_sec": 12.3,
  "demand_generation_sec": 4.5,
  "simulation_baseline_sec": 45.2,
  "simulation_smart_sec": 47.8,
  "post_validation_sec": 2.1,
  "total_sec": 111.9
}
```

현재 `run_phase6_recovery_smoke.py`는 `elapsed_seconds`를 기록하지만 단계별 분리가 없다.
`run_simulations.py`의 `run_simulation()`은 시간 측정 없음 → `time.perf_counter()` 래핑 필요.

### 재사용 위험 방어

- `network.net.xml` hash를 `metadata.json`에 기록
- demand 파라미터 변경 시 route 재생성 강제
- `demand_params.csv`의 `sim_duration_sec` 컬럼으로 확인

---

## 7. sim_duration and Vehicle Count Guardrail

### 현재 문제

| 위치 | default | 비고 |
|------|---------|------|
| `main.py:397` | `sim_duration=1800` | 30분 |
| `collect_metrics.py:75` | `sim_duration=1800` | 30분 |
| `run_simulations.py:1024` | `sim_duration=1800` | 30분 |
| `run_simulations.py:3194` | `sim_duration=1800` | CLI default |
| `main_realistic_stress` 정책 | 600초 기준 3,480대 | |

**위험:** `--sim_duration 600` 없이 실행하면 1800초가 적용되어:
- 차량 수가 3,480 × 3 ≈ 10,440대로 증가
- 실행 시간 3배 + 차량 증가에 따른 비선형 증가

### 제안

1. `main_realistic_stress` 시나리오일 때 `sim_duration != 600`이면 **WARNING 출력**
2. `demand_params.csv`에 다음 컬럼 확인: `sim_duration_sec`, `total_vehicle_flow_vph`, `expected_vehicle_count_for_duration`, `total_vehicle_count_600s`, `generated_vehicle_count`
3. `generated_vehicle_count`와 `expected_vehicle_count_for_duration` 차이가 10% 초과 시 warning

---

## 8. Post-run Validation Separation

### 현재 구조

```
main.py → run_pipeline()
  ├─ preprocess_inputs()
  ├─ build_all_networks()
  ├─ generate_for_candidates()     ← demand generation
  ├─ collect_all()                  ← simulation (무거움)
  ├─ calibrate()
  ├─ generate_all_reports()
  └─ _maybe_validate_demand()      ← post-run validation (✅ 이미 분리됨)
```

- `demand_validation.py`의 `validate_demand_run()`은 시뮬레이션 중에 호출되지 않음 ✅
- 시뮬레이션 루프 내부에 demand/coverage validation 코드 없음 ✅
- **결론:** 현재 분리가 이미 올바르게 되어 있으며 추가 조치 불필요.

---

## 9. Benchmark Plan

### 벤치마크 설계

| 항목 | 값 |
|------|-----|
| candidate | 1개 (고정) |
| seeds | `42` (1개) |
| sim_duration | 600초 |
| scenarios | baseline + smart |
| 비교 축 | 최적화 전 vs 후 |
| 고정 요소 | 같은 network, route, trip, ped, sumocfg |

### 결과 비교 대상 CSV

| CSV file | 비교 컬럼 |
|----------|----------|
| `simulation_results_seed.csv` | `pedestrian_count`, `elderly_pedestrian_count`, `veh_avg_delay_sec`, `queue_avg`, `queue_max`, `PET_B_surrogate_count`, `pedestrian_green_extension_count`, `extension_count`, `crossing_time`, `pedestrian_waiting_time_mean` |
| `extension_events_seed.csv` | row count, `extension_sec` sum |
| `incident_events_seed.csv` | row count (동일해야 함) |

### 허용 범위

- **smoke profile**: `pedestrian_count`, `extension_count`는 full과 동일. lane/surrounding 지표는 NaN 허용.
- **minimal profile**: 위 + `veh_avg_delay_sec`, `queue_avg` 동일.
- **full + sampling**: 모든 값이 full (매 step)과 ±5% 이내.

---

## 10. Risks and Backward Compatibility

| 위험 | 영향 | 완화 |
|------|------|------|
| smoke profile에서 surrounding 지표가 NaN | report 스크립트 오류 가능 | default를 `full`로 유지 |
| sampling interval로 spillback 감지 누락 | spillback_rate 과소추정 | full profile에서는 sampling=0 유지 |
| `--metric-profile` 미지정 시 동작 변경 | 기존 파이프라인 깨짐 | default=`full` |
| approach_lanes 중복 호출 제거 | queue_lengths 값 변화 | cache hit으로 동일 값 보장 |
| integrated mode 동시 수정 | 범위 확대 | per-candidate만 먼저, integrated 후속 |

---

## 11. Minimal Implementation Steps

### Table B: 개선안 우선순위 표

| priority | change | expected_speedup | correctness_risk | implementation_complexity | test_needed |
|----------|--------|-----------------|-------------------|--------------------------|-------------|
| **P1** | `--metric-profile smoke`: lane 순회 전체 skip | **5–10×** | Low (lane 지표만 NaN) | Medium | smoke 벤치마크 |
| **P2** | `--metric-sample-interval N`: lane 순회 N초마다 | **3–5×** (full) | Very Low | Low | full ±5% |
| **P3** | approach_lanes 중복 제거 (cache hit) | **1.05×** | None | Very Low | 값 동일성 |
| **P4** | `benchmark_timing.json` 단계별 시간 기록 | 0× (진단) | None | Very Low | 출력 확인 |
| **P5** | sim_duration guardrail warning | 0× (방어) | None | Very Low | 로그 확인 |
| **P6** | vehicle 순회 sampling (smoke skip) | **1.3–2×** 추가 | Medium | Medium | smoke 벤치마크 |
| **P7** | integrated mode 동일 최적화 | P1과 동일 | 동일 | Medium | 통합 벤치마크 |

---

## 12. Exact Commands for Future Benchmark

### 최적화 전 벤치마크
```bash
# baseline
time python3 -m smart_crosswalk_sumo.run_phase6_recovery_smoke \
  --candidate-csv result/active/real_30seed_runs/manifests/current_main_12_candidates.csv \
  --net-file result/active/nets/current_main_12.net.xml \
  --scenario baseline --seed 42 --sim-duration 600 \
  --output-dir result/one_candidate_base_smart_test/bench_before_baseline_seed42

# smart
time python3 -m smart_crosswalk_sumo.run_phase6_recovery_smoke \
  --candidate-csv result/active/real_30seed_runs/manifests/current_main_12_candidates.csv \
  --net-file result/active/nets/current_main_12.net.xml \
  --scenario smart --seed 42 --sim-duration 600 \
  --output-dir result/one_candidate_base_smart_test/bench_before_smart_seed42
```

### 최적화 후 벤치마크 (구현 완료 시)
```bash
# baseline — smoke profile
time python3 -m smart_crosswalk_sumo.run_phase6_recovery_smoke \
  --candidate-csv result/active/real_30seed_runs/manifests/current_main_12_candidates.csv \
  --net-file result/active/nets/current_main_12.net.xml \
  --scenario baseline --seed 42 --sim-duration 600 \
  --metric-profile smoke \
  --output-dir result/one_candidate_base_smart_test/bench_after_baseline_seed42

# smart — smoke profile
time python3 -m smart_crosswalk_sumo.run_phase6_recovery_smoke \
  --candidate-csv result/active/real_30seed_runs/manifests/current_main_12_candidates.csv \
  --net-file result/active/nets/current_main_12.net.xml \
  --scenario smart --seed 42 --sim-duration 600 \
  --metric-profile smoke \
  --output-dir result/one_candidate_base_smart_test/bench_after_smart_seed42
```

### 수정 후 반드시 비교해야 할 결과 CSV 목록

1. `simulation_results_seed.csv`
2. `extension_events_seed.csv`
3. `incident_events_seed.csv`
4. `incident_impact_seed.csv`
5. `demand_params.csv` (변경 없어야 함)
6. `benchmark_timing.json` (신규)

---

*이 문서는 Plan Mode 진단 결과이며, 코드 수정/SUMO 실행/result 삭제를 포함하지 않습니다.*

## Implementation Note

- `run_simulations.py`에 step-local lane/vehicle cache를 넣고, `--metric-sample-interval`, `--vehicle-sample-interval`, `--progress-interval`을 추가했다.
- `main.py`는 `--reuse_nets_dir`, `--reuse_demand_dir`, `--force_demand`, `--metric-sample-interval`, `--vehicle-sample-interval`, `--progress-interval`을 받아 benchmark timing을 `benchmark_timing.json`으로 남긴다.
- `build_networks.py`와 `generate_demand.py`는 provenance 및 hash를 기준으로 재사용 여부를 판정한다.
- full default 동작은 그대로 유지되며, sampling과 reuse는 옵션을 명시한 경우에만 동작한다.
- final safety/PET/report용 결과는 `--metric-sample-interval 0 --vehicle-sample-interval 0`일 때만 exact로 취급한다.
