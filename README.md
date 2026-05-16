# Smart Crosswalk SUMO
서울 중구 기반 스마트 횡단보도 SUMO 연구 코드베이스다.

이 저장소의 현재 상태는 `docs/index.md`와 `docs/current_status.md`부터 읽는 게 맞다. 과거 계획, 진단, 드래프트, 레거시 결과 읽기 문서는 따로 분리돼 있다.

## What This Repo Does

- Top50 후보를 정리하고, 현재 30seed/smoke 대상 34개 후보를 관리한다.
- 보행자 수요를 `daytime_high_1p2` 시나리오로 만든다.
- 차량 수요를 `main_realistic_stress` 시나리오로 만든다.
- baseline/smart 비교용 SUMO 입력과 검증 CSV를 만든다.
- 30seed 결과, 네트워크 검증, 수요 검증, 차량 coverage 검증, human-readable 패키지를 정리한다.

## Current Experiment Design

### Candidate Selection

- Top50은 `risk_score` 내림차순, 동점 `crosswalk_id` 오름차순이다.
- `ROAD_RANK`는 후보 선정 기준이 아니다.
- 현재 34개 후보가 30seed/smoke 대상으로 관리된다.
- 나머지 미구현 Top50은 도로망 부재보다 `crossing` / `walkingarea` / `TLS` / `ped link` 정합성 문제로 봐야 한다.

### Pedestrian Demand

- 보행자 시나리오는 `daytime_high_1p2`다.
- S-DoT 관측값을 우선 쓰고, 결측 동은 생활인구 기반으로 추정한다.
- `final_pedestrian_600s = round(base_pedestrian_600s * 1.2)`다.
- 보행자에 `1.15`를 곱하지 않는다.
- 동화동은 `living_population_imputed`로 포함한다.
- 최종 보행자 demand end-to-end 검증은 아직 `partial`이다.
- `ped_crossing_person_count`는 수요 구현 검증이 아니라 runtime 관찰 지표다.
- 수요 구현 검증은 `demand_pedestrian.rou.xml`의 `person` 수 기준으로 본다.

### Vehicle Demand

- 차량 시나리오는 `main_realistic_stress`다.
- 총량은 `20,877 vph`, `600초 기준 3,480대`다.
- 차량 타입은 `passenger`만 쓴다. `passenger_ratio=1.0`.
- `ROAD_RANK`는 차량 분배 기준이 아니다.
- 12개 road-group allocation은 실제 edge-level routing bucket이 아니라 policy metadata다.
- 실제 route는 full-network fallback / `randomTrips.py` 기반이다.
- 전역 교통 흐름은 edge/grid coverage로 검증한다.
- “모든 edge에 균일 분포”라고 쓰면 안 된다.

### Baseline vs Smart

- baseline/smart는 같은 seed에서 같은 vehicle route/trip/net을 공유해야 한다.
- 현재 검증은 `sumocfg`를 실제 파싱해 route/trip/net 동일 여부를 `true` / `false` / `not_checked`로 남긴다.
- smart에서 달라지는 것은 신호 정책이어야 한다.

## Current Status

| Axis | Status | Current meaning |
|---|---|---|
| Top50 candidate validation | pass | `risk_score` desc, tie `crosswalk_id` asc; `ROAD_RANK` unused |
| Candidate mapping / tier validation | pass | 34 smart targets are managed in the 30seed lane |
| Pedestrian demand implementation validation | partial | `daytime_high_1p2` exists, but final end-to-end demand run is not fully proved |
| Pedestrian runtime observation validation | partial | `ped_crossing_person_count` is observed at runtime, not used as demand proof |
| Vehicle flow policy validation | pass | `main_realistic_stress`, `20,877 vph`, `3,480 / 600s`, passenger only |
| Vehicle global coverage validation | pass | edge/grid coverage pass, not uniform-all-edges claim |
| Baseline/smart same route/trip/net validation | pass | current checked artifact parses `sumocfg` and compares inputs |
| Network provenance and representativeness validation | partial | Jung-gu boundary + about 1 km buffer experimental network, not 1:1 copy |
| Result/csv organization validation | pass | active manifests, CSVs, validation folders, and readme artifacts are present |

## Read This First

1. [`docs/index.md`](docs/index.md)
2. [`docs/current_status.md`](docs/current_status.md)
3. [`docs/report_claims.md`](docs/report_claims.md)
4. [`docs/final_scenario_implementation_check.md`](docs/final_scenario_implementation_check.md)
5. [`docs/network_provenance_and_representativeness_check.md`](docs/network_provenance_and_representativeness_check.md)
6. [`docs/vehicle_global_flow_and_stress_interpretation_check.md`](docs/vehicle_global_flow_and_stress_interpretation_check.md)

## Main Commands

### Read-only Checks

```bash
python3 -m smart_crosswalk_sumo.reporting.aggregate_30seed_results status --pipeline-root result/active/real_30seed_runs
python3 -m smart_crosswalk_sumo.reporting.aggregate_30seed_results pedestrian-smoke-validation --run-manifest result/active/real_30seed_runs/run_manifest.csv --candidate-metadata result/active/real_30seed_runs/manifests/candidate_metadata.csv --validation-root result/active/real_30seed_runs/validation
python3 scripts/validate_demand_policy.py --run_dir result/vehicle_policy_smoke_1seed
python3 scripts/validate_sumo_network.py --nets_dir result/active/nets --output_dir result/active/validation/net_phase_audit
python3 scripts/build_vehicle_flow_audit.py --baseline_sumocfg result/vehicle_policy_smoke_1seed/sumo_nets/cw_NODE_5837/baseline_seed42.sumocfg --smart_sumocfg result/vehicle_policy_smoke_1seed/sumo_nets/cw_NODE_5837/smart_seed42.sumocfg --output_dir result/active/vehicle_flow_audit
```

### Simulation Runners

```bash
python3 -m smart_crosswalk_sumo.run_phase6_recovery_smoke --candidate-csv result/active/real_30seed_runs/manifests/current_main_12_candidates.csv --net-file result/active/nets/current_main_12.net.xml --scenario baseline --seed 42 --output-dir result/one_candidate_base_smart_test/ped_baseline_seed42
python3 -m smart_crosswalk_sumo.run_phase6_recovery_smoke --candidate-csv result/active/real_30seed_runs/manifests/current_main_12_candidates.csv --net-file result/active/nets/current_main_12.net.xml --scenario smart --seed 42 --output-dir result/one_candidate_base_smart_test/ped_smart_seed42
python3 -m smart_crosswalk_sumo.run_phase6_vehicle_flow --candidate-csv result/phase6_transition_after_recovery_20260514_220549/phase6_mini_experiment_candidates_batch03_only.csv --net-file result/phase_next_recovery_command_plan_20260514_200908/batch_03_run/recovery_tls_batch_network_v1.net.xml --scenario baseline --seed 42 --output-dir result/phase6_mini_experiment_batch03_5sec_sorted_baseline_20260514_220549
python3 -m smart_crosswalk_sumo.run_phase6_vehicle_flow --candidate-csv result/phase6_transition_after_recovery_20260514_220549/phase6_mini_experiment_candidates_batch03_only.csv --net-file result/phase_next_recovery_command_plan_20260514_200908/batch_03_run/recovery_tls_batch_network_v1.net.xml --scenario smart --seed 42 --output-dir result/phase6_mini_experiment_batch03_5sec_sorted_smart_20260514_220549
```

### Full Pipeline Entry Point

- `python3 -m smart_crosswalk_sumo.main`
- This umbrella runner supports preprocessing, demand generation, simulation, reporting, and validation flags.
- Runtime options added for reuse/sampling: `--reuse_nets_dir`, `--reuse_demand_dir`, `--force_demand`, `--metric-sample-interval`.
- `--metric-sample-interval 0` keeps the current full-step behavior; nonzero values only affect lane-heavy sampling.

## Main Outputs

| Path | Meaning |
|---|---|
| `result/active/real_30seed_runs/` | current 30seed manifests, CSVs, and validation outputs |
| `result/active/pedestrian_assumption/` | pedestrian assumption CSVs and Top50 mapped candidates |
| `result/active/nets/` | active batch networks for the 30seed pipeline |
| `result/vehicle_policy_smoke_1seed/` | checked vehicle demand / baseline-smart validation artifact |
| `result/active/vehicle_flow_audit/` | vehicle route and coverage audit outputs |
| `result/phase6_transition_after_recovery_20260514_220549/` | batch03 mini compare artifact and source CSV |
| `result/phase6_mini_experiment_batch03_5sec_sorted_baseline_20260514_220549/` | batch03 baseline smoke outputs |
| `result/phase6_mini_experiment_batch03_5sec_sorted_smart_20260514_220549/` | batch03 smart smoke outputs |

## Limits

- SUMO network is not a 1:1 clone of Jung-gu roads.
- Pedestrian final end-to-end demand remains partial.
- Runtime person counts do not prove demand implementation by themselves.
- Vehicle road-group numbers are policy metadata, not exact route buckets.
- Global vehicle coverage does not mean every edge gets equal traffic.
- Smoke results are execution-valid only. They are not policy-effect proof by themselves.

## Safe Next Steps

1. Use `docs/current_status.md` as the live status source.
2. Use `docs/report_claims.md` as the wording guardrail for reports.
3. Keep pedestrian and vehicle lanes separate.
4. Keep baseline/smart input equality as a strict check.
5. Keep legacy draft and diagnosis docs out of current-status claims.
