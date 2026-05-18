# 결과 읽기

결과는 `simulation_result.csv`와 `final_summary.csv` 두 층으로 보면 된다.

## 폴더 구조

```text
outputs/
  smoke/
    manifests/
    runs/
    logs/
    debug/
    audit/
  final/
    manifests/
    runs/
    logs/
    debug/
    audit/
    aggregate/final_summary.csv
```

## smoke vs final

- `smoke`는 120초, seed 1개, 파이프라인 확인용이다.
- `final`은 600초, seed 1~30, 본실험용이다.
- `smoke` 결과는 정책 효과를 말하는 증거가 아니다.
- `final_summary.csv`가 최종 기준 파일이다.

## `simulation_result.csv`

run 단위 결과다. 자주 보는 컬럼은 아래다.

- `scenario`: `baseline` 또는 `smart`
- `seed`: run seed
- `pedestrian_crossing_count`: 관측된 보행자 횡단 수
- `extension_count`: smart 신호 연장 횟수
- `vehicle_route_count`: 차량 route 수
- `unique_vehicle_route_count`: 중복 제거 route 수
- `duplicate_factor`: `vehicle_route_count / unique_vehicle_route_count`
- `unique_vehicle_route_ratio`: `unique_vehicle_route_count / vehicle_route_count`
- `network_vehicle_edge_coverage_ratio`: 네트워크 분포 폭
- `local_500m_vehicle_count`: 후보 주변 500m 차량량
- `pet_available`: PET 가능 여부
- `pet_unavailable_reason`: PET가 불가하면 사유

## `final_summary.csv`

후보 단위 요약이다. `baseline`과 `smart`의 seed 평균을 모은다.

자주 보는 컬럼은 아래다.

- `crosswalk_id`
- `result_status`
- `result_reason`
- `usable_seed_count`
- `baseline_pedestrian_crossing_count_mean`
- `smart_pedestrian_crossing_count_mean`
- `baseline_extension_count_mean`
- `smart_extension_count_mean`
- `baseline_network_avg_delay_sec_mean`
- `smart_network_avg_delay_sec_mean`
- `network_avg_delay_sec_delta_mean`
- `baseline_local_500m_avg_delay_sec_mean`
- `smart_local_500m_avg_delay_sec_mean`
- `local_500m_avg_delay_sec_delta_mean`
- `baseline_network_vehicle_edge_coverage_ratio_mean`
- `smart_network_vehicle_edge_coverage_ratio_mean`
- `baseline_low_pet_per_100_crossings_mean`
- `smart_low_pet_per_100_crossings_mean`
- `low_pet_per_100_crossings_delta_mean`
- `baseline_accident_risk_estimate_mean`
- `smart_accident_risk_estimate_mean`
- `accident_risk_estimate_delta_mean`

## 해석

- `pass`: 최종 보고에 사용 가능
- `warning`: 쓸 수는 있지만 주석이 필요함
- `fail`: 제외
- `exclude`: 구조상 최종 보고 대상이 아님

## 요약 확인

```bash
python3 - <<'PY'
import pandas as pd
from pathlib import Path

path = Path("outputs/final/aggregate/final_summary.csv")
df = pd.read_csv(path)
cols = [
    "crosswalk_id",
    "result_status",
    "usable_seed_count",
    "smart_network_avg_delay_sec_mean",
    "smart_low_pet_per_100_crossings_mean",
    "smart_accident_risk_estimate_mean",
]
print(df[cols].head(10).to_string(index=False))
PY
```

## 전체 칼럼 전수조사

기준은 현재 생성 코드다.

- `simulation_result.csv`: [`smart_crosswalk_sumo/run_sampled10_group.py`](../smart_crosswalk_sumo/run_sampled10_group.py)
- `final_summary.csv`: [`smart_crosswalk_sumo/reporting/simple_final_pipeline.py`](../smart_crosswalk_sumo/reporting/simple_final_pipeline.py)

### `simulation_result.csv`

run 1개, seed 1개 기준 결과다. 일부 칼럼은 `baseline` / `smart` 공통이고, 일부는 보행자 PET나 차량 route 상태에 따라 비어 있을 수 있다.

실행/식별 칼럼:

- `run_name`: run 이름
- `crosswalk_id`: 횡단보도 ID
- `scenario`: `baseline` / `smart`
- `seed`: 시드
- `output_dir`: run 출력 폴더
- `run_start_time`: 시작 시각
- `run_end_time`: 종료 시각
- `elapsed_sec`: 총 실행 시간
- `completed`: 시뮬레이션 완료 여부
- `sim_duration`: 시뮬레이션 길이
- `warmup`: 워밍업 시간
- `step_length`: TraCI step 길이
- `scenario_name`: 실험 시나리오 이름
- `demand_profile`: 수요 프로필 이름

교차로/경로 메타:

- `tls_id_used`: 사용한 신호기 ID
- `ped_link_index`: 보행 링크 인덱스
- `crossing_edge_id`: 횡단 대상 edge ID
- `route_from_edge`: 차량 출발 edge
- `route_to_edge`: 차량 도착 edge
- `step_count`: 실제 진행 step 수
- `route_reason`: route 선택 사유
- `generated_vehicle_route_file`: 생성된 vehicle route XML 경로
- `surrounding_lane_count`: 주변 lane 수
- `vehicle_bbox_coverage`: 후보 주변 bbox가 네트워크 bbox를 얼마나 덮는지 나타내는 비율
- `batch_network_file`: 사용한 네트워크 파일 경로

보행자/지연 칼럼:

- `ped_crossing_presence_steps`: 보행자 횡단이 관측된 step 수
- `ped_crossing_person_count`: 관측된 보행자 수
- `pedestrian_crossing_count`: `ped_crossing_person_count`와 같은 의미의 원시 칼럼
- `expected_ped_repeat_count`: 기대한 보행자 반복 수
- `ped_repeat_count_match`: 관측값이 기대값과 일치하는지
- `ped_wait_time_mean`: 보행자 평균 대기 시간
- `ped_wait_time_max`: 보행자 최대 대기 시간
- `average_pedestrian_wait_time`: `ped_wait_time_mean` 별칭
- `pedestrian_waiting_time_mean`: `ped_wait_time_mean` 별칭
- `pedestrian_waiting_time`: `ped_wait_time_mean` 별칭
- `pedestrian_waiting_time_max`: `ped_wait_time_max` 별칭
- `max_pedestrian_wait_time`: `ped_wait_time_max` 별칭
- `veh_delay_mean`: 보행 횡단 주변 차량 평균 지연
- `veh_delay_max`: 보행 횡단 주변 차량 최대 지연
- `veh_avg_delay_sec`: `veh_delay_mean` 별칭
- `avg_vehicle_delay_sec`: 네트워크 평균 지연
- `surrounding_road_delay_sec`: 500m 주변 도로 평균 지연
- `vehicle_delay_cost`: 네트워크 평균 timeLoss
- `safety_risk_score`: 안전 리스크 점수
- `accident_expected_value`: `safety_risk_score` 별칭
- `elderly_incomplete_crossings`: 고령자 미완료 횡단 수

차량/네트워크 칼럼:

- `extension_count`: 신호 연장 횟수
- `pedestrian_green_extension_count`: `extension_count` 별칭
- `total_extension_sec`: 총 연장 시간
- `generated_vehicle_count`: 생성 차량 수
- `vehicle_route_count`: route 총 개수
- `unique_vehicle_route_count`: 중복 제거 route 개수
- `duplicate_factor`: `vehicle_route_count / unique_vehicle_route_count`
- `unique_vehicle_route_ratio`: `unique_vehicle_route_count / vehicle_route_count`
- `used_vehicle_edges`: 사용된 차량 edge 수
- `network_arrived_vehicles`: 도착 차량 수
- `network_departed_vehicles`: 출발 차량 수
- `total_vehicle_arrivals`: `network_departed_vehicles`와 같은 계열의 집계값
- `network_mean_travel_time`: 네트워크 평균 travel time
- `network_mean_time_loss`: 네트워크 평균 timeLoss
- `network_avg_delay_sec`: 네트워크 평균 대기 지연
- `network_vehicle_edge_coverage_ratio`: 네트워크에서 차량 edge가 얼마나 커버됐는지
- `network_teleport_count`: teleport 수
- `network_collision_count`: collision 수
- `local_500m_vehicle_count`: 500m 주변 차량 수
- `local_500m_mean_speed`: 500m 주변 평균 속도
- `local_500m_mean_time_loss`: 500m 주변 평균 timeLoss
- `local_500m_avg_delay_sec`: 500m 주변 평균 지연
- `local_500m_queue_proxy`: 500m 주변 queue proxy
- `local_500m_stop_count`: 500m 주변 정지 횟수
- `avg_queue_length`: 평균 queue 길이
- `max_queue_length`: 최대 queue 길이

PET/사고 칼럼:

- `pet_source`: PET 산출 소스
- `pet_event_count`: PET 이벤트 수
- `very_risky_crossing_count`: 매우 위험한 crossing 수
- `risky_crossing_count`: 위험 crossing 수
- `safe_crossing_count`: 안전 crossing 수
- `low_pet_event_count`: low PET 이벤트 수
- `low_pet_per_100_crossings`: 100 crossing당 low PET 수
- `low_pet_per_100_conflict_candidates`: 100 conflict 후보당 low PET 수
- `pet_min`: PET 최소값
- `pet_p10`: PET 10퍼센타일
- `pet_mean`: PET 평균
- `pet_available`: PET 계산 가능 여부
- `pet_coverage_ratio`: PET 커버리지 비율
- `pet_unavailable_reason`: PET 불가 사유
- `accident_risk_estimate`: 사고 리스크 추정치

### `final_summary.csv`

후보 1개 기준 public summary다. seed 평균과 판정값만 남긴다.

식별/판정 칼럼:

- `run_group`: 실험 그룹
- `net_group`: 네트워크 그룹
- `crosswalk_id`: 횡단보도 ID
- `admin_dong`: 행정동
- `dong_name`: 동 이름
- `mapping_distance_tier`: 매핑 거리 구간
- `registry_status`: 레지스트리 상태
- `implementation_status`: 구현 상태
- `control_mode`: 제어 모드
- `planned_seed_count`: 계획 seed 수
- `usable_seed_count`: 실제 사용 가능한 seed 수
- `pass_seed_count`: pass seed 수
- `warning_seed_count`: warning seed 수
- `fail_seed_count`: fail seed 수
- `exclude_seed_count`: exclude seed 수
- `result_status`: 최종 판정
- `result_reason`: 최종 판정 사유
- `vehicle_route_hash_unique_count`: 차량 route hash의 고유 개수
- `pedestrian_route_hash_unique_count`: 보행자 route hash의 고유 개수

baseline vs smart 평균 칼럼:

- `baseline_pedestrian_crossing_count_mean`: baseline 평균 보행 횡단 수
- `smart_pedestrian_crossing_count_mean`: smart 평균 보행 횡단 수
- `baseline_extension_count_mean`: baseline 평균 연장 횟수
- `smart_extension_count_mean`: smart 평균 연장 횟수
- `baseline_network_avg_delay_sec_mean`: baseline 평균 네트워크 지연
- `smart_network_avg_delay_sec_mean`: smart 평균 네트워크 지연
- `network_avg_delay_sec_delta_mean`: smart - baseline 차이
- `baseline_network_mean_time_loss_mean`: baseline 평균 timeLoss
- `smart_network_mean_time_loss_mean`: smart 평균 timeLoss
- `network_mean_time_loss_delta_mean`: smart - baseline 차이
- `baseline_local_500m_avg_delay_sec_mean`: baseline 500m 주변 평균 지연
- `smart_local_500m_avg_delay_sec_mean`: smart 500m 주변 평균 지연
- `local_500m_avg_delay_sec_delta_mean`: smart - baseline 차이
- `baseline_network_vehicle_edge_coverage_ratio_mean`: baseline 평균 edge coverage
- `smart_network_vehicle_edge_coverage_ratio_mean`: smart 평균 edge coverage
- `baseline_low_pet_per_100_crossings_mean`: baseline 평균 low PET 비율
- `smart_low_pet_per_100_crossings_mean`: smart 평균 low PET 비율
- `low_pet_per_100_crossings_delta_mean`: smart - baseline 차이
- `baseline_accident_risk_estimate_mean`: baseline 평균 사고 리스크
- `smart_accident_risk_estimate_mean`: smart 평균 사고 리스크
- `accident_risk_estimate_delta_mean`: smart - baseline 차이

PET 보조 칼럼:

- `smart_pet_source_mode`: usable seed에서 가장 흔한 PET 소스
- `smart_pet_available_all_usable_seeds`: usable seed 전부에서 PET 가능했는지
- `smart_pet_coverage_ratio_mean`: usable seed 평균 PET 커버리지
- `smart_pet_unavailable_reason_mode`: 가장 흔한 PET 불가 사유

보는 법:

- `pass`: 바로 써도 됨
- `warning`: 쓰되 주석 필요
- `fail`: 제외
- `exclude`: 구조상 최종 보고 대상 아님
