# Executive Summary

현재 `real_30seed_runs` 파이프라인은 **보행자 smoke 검증 중심**으로는 일부 가정과 맞지만, 사용자가 요구한 **전역 차량 흐름, baseline/smart의 동일 차량 수요 공유, 차량 coverage 검증, 동별 분포 검증**까지 포함한 전체 실험 설계와는 아직 맞지 않는다.

핵심 판단은 아래와 같다.

- `run_manifest.csv`에는 `validation_manifest`가 붙어 있지만, 실제 `command`는 여전히 `smart_crosswalk_sumo.run_phase6_recovery_smoke`만 실행한다.
- 현재 실행 결과 폴더에는 `demand_pedestrian.rou.xml`, `phase6_smoke_summary.csv`, `route_generation_audit.csv` 같은 보행자 smoke 산출물은 있으나, 차량 검증에 필요한 `demand_vehicle.rou.xml`, `demand_all.rou.xml`, `.trips.xml`, `tripinfo*.xml`, `vehicle_global_coverage_validation.csv`, `vehicle_edge_coverage_summary.csv`는 보이지 않는다.
- 차량 총량/전역 coverage는 이미 별도 구현이 있다. 다만 그 구현은 `run_phase6_vehicle_flow.py`, `vehicle_demand_policy.py`, `build_vehicle_flow_audit.py` 쪽에 있고, 현재 smoke command에는 연결돼 있지 않다.
- 보행자 반복수/spacing/phase-aligned depart는 smoke summary와 person route 산출물로 부분 검증 가능하지만, `demand_params.csv` 기반의 정식 validation chain은 현재 smoke 경로에 없다.
- 동별 분포는 upstream pedestrian assumption / top50 계열 CSV에는 `dong_name`, `admin_dong`가 있지만, 현재 30-seed candidate CSV와 run-level manifest는 그 컬럼을 유지하지 않아서 현재 run 결과만으로는 직접 검증이 어렵다.

실무적으로는 다음이 가장 안전하다.

- smoke pipeline은 지금처럼 pedestrian smoke sanity check 용도로 유지한다.
- vehicle 전역 흐름/coverage 검증은 별도 vehicle-flow pipeline 또는 별도 validation command manifest로 분리한다.
- baseline/smart 비교는 같은 `net_group` 내부에서만 비교하고, 비교 범위는 baseline과 smart가 동일해야 한다.

# Current Pipeline Flow

현재 파이프라인은 `aggregate_30seed_results.py prepare`가 중심이다.

1. group spec를 로드한다.
- `current_main_12`, `signal_fix_9`, `generated_signal_7`, `p1_p4_recovery_6`의 4개 그룹으로 나뉜다.
- 각 그룹은 서로 다른 `net_file`을 가진다.
- 그룹별 후보 수는 12, 9, 7, 6개이고, smart 총 후보 수는 34개다.
- 근거: [`smart_crosswalk_sumo/reporting/aggregate_30seed_results.py`](/Users/junlee/Desktop/2026-1/js/smart_crosswalk_sumo/reporting/aggregate_30seed_results.py#L654)
- 근거: [`result/active/real_30seed_runs/manifests/run_plan_by_group.csv`](/Users/junlee/Desktop/2026-1/js/result/active/real_30seed_runs/manifests/run_plan_by_group.csv)

2. candidate CSV를 생성하고 single-candidate CSV로 쪼갠다.
- baseline은 그룹 전체 candidate CSV를 사용한다.
- smart는 crosswalk별 single-candidate CSV를 사용한다.
- 현재 `current_main_12_candidates.csv`에는 `crosswalk_id`, `route_from_edge`, `route_to_edge`, `ped_repeat_count`, `ped_repeat_spacing_sec`는 있지만 `dong_name`, `admin_dong`는 없다.
- 근거: [`result/active/real_30seed_runs/manifests/current_main_12_candidates.csv`](/Users/junlee/Desktop/2026-1/js/result/active/real_30seed_runs/manifests/current_main_12_candidates.csv)

3. run_manifest를 생성한다.
- baseline 30개 seed, smart는 candidate 수 x 30 seed로 확장된다.
- 현재 snapshot 기준으로 전체 plan은 1140 runs이고, 이는 baseline 120 + smart 1020이다.
- 근거: [`result/active/real_30seed_runs/manifests/baseline_run_manifest.csv`](/Users/junlee/Desktop/2026-1/js/result/active/real_30seed_runs/manifests/baseline_run_manifest.csv)
- 근거: [`result/active/real_30seed_runs/manifests/smart_run_manifest.csv`](/Users/junlee/Desktop/2026-1/js/result/active/real_30seed_runs/manifests/smart_run_manifest.csv)
- 근거: [`result/active/real_30seed_runs/run_manifest.csv`](/Users/junlee/Desktop/2026-1/js/result/active/real_30seed_runs/run_manifest.csv)

4. baseline/smart command를 생성한다.
- 둘 다 `python3 -m smart_crosswalk_sumo.run_phase6_recovery_smoke`를 실행한다.
- baseline은 `--extension-sec 0`이고 smart는 `--extension-sec 5.0`이다.
- `--validate_demand`는 붙지 않는다.
- `--include-vehicles`도 붙지 않는다.
- 근거: [`smart_crosswalk_sumo/reporting/aggregate_30seed_results.py`](/Users/junlee/Desktop/2026-1/js/smart_crosswalk_sumo/reporting/aggregate_30seed_results.py#L442)
- 근거: [`smart_crosswalk_sumo/reporting/aggregate_30seed_results.py`](/Users/junlee/Desktop/2026-1/js/smart_crosswalk_sumo/reporting/aggregate_30seed_results.py#L674)
- 근거: [`smart_crosswalk_sumo/run_phase6_recovery_smoke.py`](/Users/junlee/Desktop/2026-1/js/smart_crosswalk_sumo/run_phase6_recovery_smoke.py#L1481)

5. smoke 실행 결과를 쓴다.
- smoke runner는 `phase6_smoke_summary.csv`, `phase6_smoke_*_results.csv`, debug trace, route order summary, candidate validation JSON을 쓴다.
- vehicle route/trip/coverage는 기본적으로 쓰지 않는다.
- 근거: [`smart_crosswalk_sumo/run_phase6_recovery_smoke.py`](/Users/junlee/Desktop/2026-1/js/smart_crosswalk_sumo/run_phase6_recovery_smoke.py#L842)
- 근거: [`smart_crosswalk_sumo/run_phase6_recovery_smoke.py`](/Users/junlee/Desktop/2026-1/js/smart_crosswalk_sumo/run_phase6_recovery_smoke.py#L1354)
- 근거: [`smart_crosswalk_sumo/run_phase6_recovery_smoke.py`](/Users/junlee/Desktop/2026-1/js/smart_crosswalk_sumo/run_phase6_recovery_smoke.py#L1561)

6. aggregate 결과를 만든다.
- `seed30_run_level_results.csv`, `seed30_baseline_smart_comparison.csv`, `seed30_metric_summary.csv`, `seed30_group_summary.csv`, `run_validation_summary.csv`를 쓴다.
- 이 단계는 결과를 요약할 뿐, validation을 실행하지는 않는다.
- 근거: [`smart_crosswalk_sumo/reporting/aggregate_30seed_results.py`](/Users/junlee/Desktop/2026-1/js/smart_crosswalk_sumo/reporting/aggregate_30seed_results.py#L1365)

7. validation catalog를 만든다.
- `run_validation_manifest.csv`는 존재하지만 catalog 성격이다.
- `status`, `row_count`, `source_artifact`, `source_path`를 기록한다.
- 실행 명령은 아니다.
- 근거: [`result/active/real_30seed_runs/manifests/run_validation_manifest.csv`](/Users/junlee/Desktop/2026-1/js/result/active/real_30seed_runs/manifests/run_validation_manifest.csv)
- 근거: [`smart_crosswalk_sumo/reporting/aggregate_30seed_results.py`](/Users/junlee/Desktop/2026-1/js/smart_crosswalk_sumo/reporting/aggregate_30seed_results.py#L751)

8. 실제 post-run validation 실행 여부
- 현재 snapshot에서는 실행되지 않는다.
- `main.py`에는 `--validate_demand` 훅이 있으나 smoke runner가 그 경로를 타지 않는다.
- 근거: [`smart_crosswalk_sumo/main.py`](/Users/junlee/Desktop/2026-1/js/smart_crosswalk_sumo/main.py#L95)
- 근거: [`smart_crosswalk_sumo/main.py`](/Users/junlee/Desktop/2026-1/js/smart_crosswalk_sumo/main.py#L493)

# My Assumptions

1. 차량 흐름은 전역 background traffic이어야 한다.
2. baseline과 smart는 같은 seed에서 같은 차량 수요/route를 공유해야 한다.
3. smart의 차이는 보행자 신호 연장 또는 설치 횡단보도 정책에 한정돼야 한다.
4. 차량 총량은 정책 CSV 또는 vehicle demand policy 기준이어야 한다.
5. 도로별/방향별/관측지점별 분배가 있다면 route 생성에 반영돼야 한다.
6. net_group가 달라도 각 그룹 내부에서 차량은 전체 맵에 퍼져야 한다.
7. 보행자 수요는 명시 입력값에 따라 생성돼야 한다.
8. 동별 분포는 가능하면 입력/가정과 함께 확인돼야 한다.
9. baseline/smart 비교는 동일 관찰 범위를 가져야 한다.
10. net과 crosswalk는 해당 맵 안에서 일치해야 한다.

# Assumption vs Current Pipeline Comparison Table

| 항목 | 내 가정 | 현재 코드/산출물 상태 | 판정 | 근거 파일 | 필요한 수정 |
|---|---|---|---|---|---|
| 차량 흐름 전역성 | 특정 횡단보도 주변만이 아니라 전역 background traffic이어야 함 | 현재 smoke run에는 차량 route/trip/coverage 산출물이 없음. 차량 전역 흐름은 별도 `run_phase6_vehicle_flow.py` / `build_vehicle_flow_audit.py` 쪽에 존재 | 불일치 | [`run_phase6_recovery_smoke.py`](/Users/junlee/Desktop/2026-1/js/smart_crosswalk_sumo/run_phase6_recovery_smoke.py#L1481), [`run_phase6_vehicle_flow.py`](/Users/junlee/Desktop/2026-1/js/smart_crosswalk_sumo/run_phase6_vehicle_flow.py#L876) | smoke와 vehicle-flow 분리 또는 vehicle-flow validation 연결 |
| baseline/smart 차량 공유 | 같은 seed에서 같은 차량 수요/route 공유 | 현재 smoke command는 차량을 만들지 않으며, 같은 차량 route를 공유할 수 없다 | 불일치 | [`run_manifest.csv`](/Users/junlee/Desktop/2026-1/js/result/active/real_30seed_runs/run_manifest.csv), [`run_phase6_recovery_smoke.py`](/Users/junlee/Desktop/2026-1/js/smart_crosswalk_sumo/run_phase6_recovery_smoke.py#L1060) | vehicle route 생성/검증을 별도 파이프라인으로 분리 |
| smart 차이 범위 | 보행자 신호 연장/설치 정책만 달라야 함 | smoke에서 baseline은 그룹 전체 candidate CSV, smart는 single-candidate CSV를 쓰므로 관찰 범위가 다르다. 차량 차이는 없지만 비교 범위는 동일하지 않다 | 부분일치 | [`baseline_run_manifest.csv`](/Users/junlee/Desktop/2026-1/js/result/active/real_30seed_runs/manifests/baseline_run_manifest.csv), [`smart_run_manifest.csv`](/Users/junlee/Desktop/2026-1/js/result/active/real_30seed_runs/manifests/smart_run_manifest.csv) | baseline/smart 비교 범위를 동일 candidate set / 동일 scope로 맞춤 |
| 차량 총량 정책 기반 | 정책 CSV / vehicle demand policy 기준 | 차량 총량은 `vehicle_demand_policy.py`와 `run_phase6_vehicle_flow.py`에 구현돼 있지만 현재 smoke command에는 연결 안 됨 | 부분일치 | [`vehicle_demand_policy.py`](/Users/junlee/Desktop/2026-1/js/smart_crosswalk_sumo/vehicle_demand_policy.py#L167), [`build_vehicle_flow_audit.py`](/Users/junlee/Desktop/2026-1/js/scripts/build_vehicle_flow_audit.py#L167) | current pipeline에 vehicle-flow 검증 진입점 연결 |
| 도로/방향 분배 반영 | road/direction/observation-point 분배가 route 생성에 반영돼야 함 | vehicle-flow 쪽 route generation/audit에는 반영 가능하지만, current smoke snapshot에는 없음 | 부분일치 | [`vehicle_demand_policy.py`](/Users/junlee/Desktop/2026-1/js/smart_crosswalk_sumo/vehicle_demand_policy.py#L535), [`run_phase6_vehicle_flow.py`](/Users/junlee/Desktop/2026-1/js/smart_crosswalk_sumo/run_phase6_vehicle_flow.py#L1470) | 별도 vehicle-flow 결과를 기준으로 검증 |
| net_group 내부 전역 분산 | 각 net_group 안에서 차량이 전체 맵에 퍼져야 함 | current smoke 결과에는 vehicle coverage 측정 불가. vehicle-flow audit는 이 목적에 맞지만 현재 run_manifest와는 분리됨 | 확인불가 | [`build_vehicle_flow_audit.py`](/Users/junlee/Desktop/2026-1/js/scripts/build_vehicle_flow_audit.py#L167), [`run_phase6_vehicle_flow.py`](/Users/junlee/Desktop/2026-1/js/smart_crosswalk_sumo/run_phase6_vehicle_flow.py#L1579) | 전역 coverage validation을 vehicle-flow 산출물로 연결 |
| 보행자 입력값 반영 | 보행자 수요는 명시 입력값대로 생성돼야 함 | smoke summary에 `expected_ped_repeat_count`, `ped_repeat_count_match`, `ped_crossing_person_count`가 기록된다. `phase6_smoke_summary.csv`와 person route 파일은 존재한다 | 부분일치 | [`phase6_smoke_summary.csv`](/Users/junlee/Desktop/2026-1/js/result/active/real_30seed_runs/runs/current_main_12/baseline/seed01/phase6_smoke_summary.csv), [`demand_pedestrian.rou.xml`](/Users/junlee/Desktop/2026-1/js/result/active/real_30seed_runs/runs/current_main_12/baseline/seed01/demand_pedestrian.rou.xml) | smoke 결과에 대한 정식 validation hook 또는 adapter 필요 |
| `--ped-repeat-count` / spacing / phase alignment | 이 옵션들이 person route에 반영돼야 함 | 현재 smoke summary와 `pedestrian_depart_plan.csv`는 이 옵션을 반영한 흔적을 남긴다 | 부분일치 | [`pedestrian_depart_plan.csv`](/Users/junlee/Desktop/2026-1/js/result/active/real_30seed_runs/runs/current_main_12/baseline/seed01/pedestrian_depart_plan.csv), [`run_phase6_recovery_smoke.py`](/Users/junlee/Desktop/2026-1/js/smart_crosswalk_sumo/run_phase6_recovery_smoke.py#L1501) | 별도 validation 출력 생성 |
| 보행자 수/summary/log/route 일치 | 입력값과 결과 summary/log/person route가 일치해야 함 | seed01 smoke summary와 route XML은 존재하지만 `demand_params.csv`는 없다. 정식 `validate_demand_run()` 경로는 current smoke command와 연결 안 됨 | 부분일치 | [`demand_validation.py`](/Users/junlee/Desktop/2026-1/js/smart_crosswalk_sumo/demand_validation.py#L839), [`run_phase6_recovery_smoke.py`](/Users/junlee/Desktop/2026-1/js/smart_crosswalk_sumo/run_phase6_recovery_smoke.py#L1561) | smoke outputs를 직접 받아들이는 adapter 또는 main pipeline hook |
| 동별 보행자 분포 | 가능하면 dong_name/admin_dong 기준으로 분포 확인 | current 30-seed candidate CSV는 `dong_name`, `admin_dong`를 유지하지 않는다. upstream pedestrian assumption files에는 존재한다 | 불일치 | [`current_main_12_candidates.csv`](/Users/junlee/Desktop/2026-1/js/result/active/real_30seed_runs/manifests/current_main_12_candidates.csv), [`csv_output_inventory.csv`](/Users/junlee/Desktop/2026-1/js/result/active/real_30seed_runs/csv/csv_output_inventory.csv) | dong 컬럼을 run manifest까지 보존하거나 upstream source를 별도 보관 |
| baseline 관찰 범위 | 전체 후보/전체 관찰 대상 횡단보도를 봐야 함 | baseline smoke는 그룹 전체 candidate CSV를 쓴다. run-level summary는 후보별 row를 가진다 | 부분일치 | [`baseline_run_manifest.csv`](/Users/junlee/Desktop/2026-1/js/result/active/real_30seed_runs/manifests/baseline_run_manifest.csv), [`seed30_run_level_results.csv`](/Users/junlee/Desktop/2026-1/js/result/active/real_30seed_runs/csv/seed30_run_level_results.csv) | baseline/smart가 같은 관찰 범위인지 명시적으로 고정 |
| smart 관찰 범위 | 설치 횡단보도에 정책 적용, 비교 지표는 동일 범위 | smart smoke는 crosswalk별 single-candidate run이다. 같은 net_group/seed/crosswalk_id로 비교는 가능하지만 전체 범위는 baseline과 다르다 | 부분일치 | [`smart_run_manifest.csv`](/Users/junlee/Desktop/2026-1/js/result/active/real_30seed_runs/manifests/smart_run_manifest.csv), [`seed30_baseline_smart_comparison.csv`](/Users/junlee/Desktop/2026-1/js/result/active/real_30seed_runs/csv/seed30_baseline_smart_comparison.csv) | 비교 범위를 candidate-level 또는 network-level 중 하나로 통일 |
| net/crosswalk 정합성 | 그 맵에 속한 횡단보도는 그 맵에서 돌아야 함 | group-specific `net_file`이 존재하고 candidate CSV에 `batch_network_file`, `route_from_edge`, `route_to_edge`가 있다. smoke summary와 candidate validation JSON도 생성된다 | 부분일치 | [`current_main_12_candidates.csv`](/Users/junlee/Desktop/2026-1/js/result/active/real_30seed_runs/manifests/current_main_12_candidates.csv), [`phase6_smoke_summary.csv`](/Users/junlee/Desktop/2026-1/js/result/active/real_30seed_runs/runs/current_main_12/baseline/seed01/phase6_smoke_summary.csv), [`phase6_smoke_baseline_candidate_validation.json`](/Users/junlee/Desktop/2026-1/js/result/active/real_30seed_runs/runs/current_main_12/baseline/seed01/phase6_smoke_baseline_candidate_validation.json) | net_group별 validation을 유지하고, 차량은 같은 net에서 전역 검증 |

# Validation Feasibility: Pedestrian / Vehicle / Whole-map Coverage

## A. 보행자 수요가 입력대로 생성됐는가

현재 가능한 수준:

- **당장 가능**: `phase6_smoke_summary.csv`, `demand_pedestrian.rou.xml`, `pedestrian_depart_plan.csv`, `phase6_smoke_*_candidate_validation.json`로 smoke-level 확인은 가능하다.
- **adapter 만들면 가능**: `validate_demand_run()` 형태의 정식 validation으로 만들 수 있다.
- **현재 구조로는 불완전**: `demand_params.csv`가 current smoke run에서 생성되지 않아서, `demand_validation.py`의 정식 contract를 그대로 쓸 수는 없다.

근거:

- [`demand_validation.py`](/Users/junlee/Desktop/2026-1/js/smart_crosswalk_sumo/demand_validation.py#L839)
- [`run_phase6_recovery_smoke.py`](/Users/junlee/Desktop/2026-1/js/smart_crosswalk_sumo/run_phase6_recovery_smoke.py#L1501)
- [`result/active/real_30seed_runs/runs/current_main_12/baseline/seed01/phase6_smoke_summary.csv`](/Users/junlee/Desktop/2026-1/js/result/active/real_30seed_runs/runs/current_main_12/baseline/seed01/phase6_smoke_summary.csv)
- [`result/active/real_30seed_runs/runs/current_main_12/baseline/seed01/demand_pedestrian.rou.xml`](/Users/junlee/Desktop/2026-1/js/result/active/real_30seed_runs/runs/current_main_12/baseline/seed01/demand_pedestrian.rou.xml)

## B. 차량 수요가 입력대로 생성됐는가

현재 가능한 수준:

- **vehicle-flow 실행/산출물이 있어야 가능**: 현재 smoke 결과에는 차량 XML이 없다.
- **별도 validation command가 있으면 가능**: `run_phase6_vehicle_flow.py` / `build_vehicle_flow_audit.py` 쪽은 차량 demand 정책과 route/coverage를 검증할 수 있다.
- **현재 smoke 구조로는 불가능**: `run_manifest.command`에 차량 생성 또는 validation이 붙지 않았다.

근거:

- [`vehicle_demand_policy.py`](/Users/junlee/Desktop/2026-1/js/smart_crosswalk_sumo/vehicle_demand_policy.py#L167)
- [`build_vehicle_flow_audit.py`](/Users/junlee/Desktop/2026-1/js/scripts/build_vehicle_flow_audit.py#L167)
- [`run_phase6_vehicle_flow.py`](/Users/junlee/Desktop/2026-1/js/smart_crosswalk_sumo/run_phase6_vehicle_flow.py#L876)
- [`run_phase6_recovery_smoke.py`](/Users/junlee/Desktop/2026-1/js/smart_crosswalk_sumo/run_phase6_recovery_smoke.py#L1481)

## C. 차량이 전체 맵에 흐르는가

현재 가능한 수준:

- **vehicle-flow 실행/산출물이 있어야 가능**: 전체 driveable edge coverage는 차량 route XML이 있어야 계산된다.
- **aggregate 단계에서 일괄 검증 가능**: 차량 flow output을 모은 뒤 coverage audit를 돌리는 방식은 이미 구현돼 있다.
- **현재 구조로는 불가능**: smoke output tree에는 `demand_vehicle.rou.xml`, `demand_all.rou.xml`, `.trips.xml`, `tripinfo*.xml`, `vehicle_edge_coverage_summary.csv`, `vehicle_global_coverage_validation.csv`가 없다.

근거:

- [`vehicle_demand_policy.py`](/Users/junlee/Desktop/2026-1/js/smart_crosswalk_sumo/vehicle_demand_policy.py#L535)
- [`build_vehicle_flow_audit.py`](/Users/junlee/Desktop/2026-1/js/scripts/build_vehicle_flow_audit.py#L167)
- [`run_phase6_vehicle_flow.py`](/Users/junlee/Desktop/2026-1/js/smart_crosswalk_sumo/run_phase6_vehicle_flow.py#L1470)
- [`result/active/real_30seed_runs/csv/seed30_run_level_results.csv`](/Users/junlee/Desktop/2026-1/js/result/active/real_30seed_runs/csv/seed30_run_level_results.csv)

# Baseline vs Smart Observation Scope Check

현재 구조의 핵심 차이는 이렇다.

- baseline smoke는 그룹 전체 candidate CSV를 대상으로 한 번에 돈다.
- smart smoke는 crosswalk별 single-candidate CSV를 대상으로 돈다.
- run-level summary는 baseline run의 개별 crosswalk row와 smart run의 개별 crosswalk row를 비교할 수 있게 만든다.
- 하지만 **전체 관찰 범위**는 baseline과 smart가 동일하지 않다.

따라서 공정성 관점에서 보면:

- pedestrian smoke sanity check는 후보별 비교로는 의미가 있다.
- 차량 전역 흐름 비교는 현재 smoke 레벨로는 공정한 baseline/smart 비교가 아니다.
- 네트워크 전체 관찰은 baseline/smart가 같은 vehicle background를 공유해야 의미가 있는데, 현재 smoke 구조에는 그 보장이 없다.

특히 사용자가 원하는 기본 원칙인 아래 조건은 현재 파이프라인에서 완전히 만족하지 않는다.

- 차량 흐름은 전역이다.
- 정책 개입은 설치 횡단보도에만 적용된다.
- 비교 지표는 동일 관찰 범위를 쓴다.

현재는 baseline/smart가 같은 `net_file`을 쓰는 것은 맞지만, smoke 레벨에서는 vehicle route 자체가 없고, candidate scope도 baseline과 smart가 동일한 방식으로 관측되지 않는다.

근거:

- [`baseline_run_manifest.csv`](/Users/junlee/Desktop/2026-1/js/result/active/real_30seed_runs/manifests/baseline_run_manifest.csv)
- [`smart_run_manifest.csv`](/Users/junlee/Desktop/2026-1/js/result/active/real_30seed_runs/manifests/smart_run_manifest.csv)
- [`seed30_baseline_smart_comparison.csv`](/Users/junlee/Desktop/2026-1/js/result/active/real_30seed_runs/csv/seed30_baseline_smart_comparison.csv)

# Recommended Pipeline Revision

가장 안전한 구조는 **smoke pipeline과 vehicle-flow pipeline을 분리**하는 것이다.

추천 우선순위:

1. `run_phase6_recovery_smoke`는 pedestrian smoke sanity check 용도로 유지한다.
2. 차량 전역 흐름과 coverage는 `run_phase6_vehicle_flow.py` / `build_vehicle_flow_audit.py` 기반의 별도 validation command로 돌린다.
3. `aggregate_30seed_results.py`는 결과 요약과 catalog 생성만 맡긴다.
4. validation manifest는 별도 command manifest로 생성하되, run command에 자동 결합하지 않는다.

이유:

- 현재 smoke run은 차량 산출물이 없다.
- baseline/smart의 차량 비교는 same route / same seed / same network contract가 필요하다.
- candidate-level pedestrian smoke와 network-level vehicle coverage는 검증 contract가 다르다.
- 따라서 한 command에 모두 붙이면 실패 모드가 불명확해진다.

선택지 기준으로 보면:

- `run command 뒤에 validation 자동 실행`: 현재는 비추천.
- `별도 validation command manifest 생성`: 가장 적절하다.
- `aggregate 단계에서 일괄 validation`: 이미 있는 결과에는 적합하지만, primary execution path로는 보조적이다.
- `smoke pipeline과 vehicle-flow pipeline 분리 검증`: 연구 설계 기준으로는 반드시 필요하다.

# Minimal Next Actions

1. 현재 `real_30seed_runs` snapshot이 seed01 위주라는 점을 전제로, pedestrian smoke 결과와 vehicle-flow 결과를 분리해서 본다.
2. 보행자 검증은 `phase6_smoke_summary.csv` + `demand_pedestrian.rou.xml` + `pedestrian_depart_plan.csv`로 우선 판정한다.
3. 차량 검증은 `run_phase6_vehicle_flow.py` 계열 산출물에서만 판정한다.
4. `dong_name/admin_dong` 기반 분포를 보려면 current 30-seed candidate manifest에 그 컬럼을 유지하거나 upstream source를 별도 보존해야 한다.
5. baseline/smart 비교는 같은 `net_group` 내부에서만 수행하고, 비교 범위는 candidate-level 또는 network-level 중 하나로 고정한다.

