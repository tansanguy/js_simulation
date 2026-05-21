# 전체 변수 설명 (final_summary.csv)

## 식별 정보

| 변수명 | 설명 |
|--------|------|
| `group` | 실험 그룹 (current_main_12 / generated_signal_7 / p1_p4_recovery_6 / signal_fix_9) |
| `crosswalk_id` | 횡단보도 식별자 (NODE_xxxxx 또는 LINK_xxxxx) |
| `run_label` | 실행 레이블 (baseline_avg / smart_avg) |
| `seed` | 난수 시드 (평균 행에서는 NaN) |
| `scenario` | 시나리오 종류 (baseline_avg / smart_avg) |

---

## 시뮬레이션 설정

| 변수명 | 설명 |
|--------|------|
| `elapsed_sec` | 실제 시뮬레이션 실행 소요 시간 (초) |
| `sim_duration` | 시뮬레이션 시간 길이 (초, 통상 600s) |
| `warmup` | 워밍업 구간 길이 (초) |
| `step_length` | 시뮬레이션 스텝 단위 (초, 통상 0.1s) |
| `step_count` | 총 시뮬레이션 스텝 수 |

---

## 보행자 관련

| 변수명 | 설명 |
|--------|------|
| `ped_link_index` | 횡단보도에 해당하는 보행자 링크 인덱스 |
| `ped_crossing_presence_steps` | 보행자가 횡단보도에 존재한 스텝 수 |
| `ped_crossing_person_count` | 횡단보도를 이용한 총 보행자 수 |
| `pedestrian_crossing_count` | 횡단 완료 횟수 |
| `expected_ped_repeat_count` | 보행자 반복 등장 기대 횟수 |
| `pedestrian_clearance_failure_count` | 녹색 신호 내 횡단 완료 실패 횟수 |
| `unfinished_crossing_count` | 미완료 횡단 횟수 (신호 종료 전 미통과) |
| `ped_wait_time_mean` | 보행자 평균 대기 시간 (초) |
| `ped_wait_time_max` | 보행자 최대 대기 시간 (초) |
| `average_pedestrian_wait_time` | 보행자 평균 대기 시간 (집계 방식 다름) |
| `pedestrian_waiting_time_mean` | 보행자 대기 시간 평균 (별도 집계) |
| `pedestrian_waiting_time` | 보행자 대기 시간 합산 |
| `pedestrian_waiting_time_max` | 보행자 대기 시간 최댓값 |
| `max_pedestrian_wait_time` | 보행자 최대 대기 시간 (별도 집계) |
| `elderly_incomplete_crossings` | 노인 보행자 미완료 횡단 횟수 |
| `pedestrian_green_extension_count` | 보행자 녹색 신호 연장 횟수 |
| `total_extension_sec` | 녹색 신호 연장 총 시간 (초) |

---

## 차량 지체

| 변수명 | 설명 |
|--------|------|
| `veh_delay_mean` | 횡단보도 인근 차량 평균 지체 (초) |
| `veh_delay_max` | 횡단보도 인근 차량 최대 지체 (초) |
| `veh_avg_delay_sec` | 차량 평균 지체 (초, 별도 집계) |
| `avg_vehicle_delay_sec` | 차량 평균 지체 (초, 별도 집계) |
| `surrounding_road_delay_sec` | 주변 도로 차량 평균 지체 (초) |
| `vehicle_delay_cost` | 차량 지체 비용 (지체 × 차량 수) |
| `avg_queue_length` | 평균 대기열 길이 (차량 수) |
| `max_queue_length` | 최대 대기열 길이 (차량 수) |
| `extension_count` | 신호 연장 발생 횟수 |

---

## 차량 발생 및 경로

| 변수명 | 설명 |
|--------|------|
| `generated_vehicle_count` | 시뮬레이션 내 생성된 차량 수 |
| `vehicle_route_count` | 차량 경로 수 (중복 포함) |
| `unique_vehicle_route_count` | 고유 차량 경로 수 |
| `duplicate_factor` | 경로 중복 계수 (route_count / unique_count) |
| `unique_vehicle_route_ratio` | 고유 경로 비율 (unique / total) |
| `used_vehicle_edges` | 실제 사용된 도로 엣지 수 |

---

## 네트워크 전체 (SUMO tripinfo)

| 변수명 | 설명 |
|--------|------|
| `network_arrived_vehicles` | 네트워크 전체 도착 완료 차량 수 |
| `network_departed_vehicles` | 네트워크 전체 출발 차량 수 |
| `network_mean_travel_time` | 전체 네트워크 평균 이동 시간 (초) |
| `network_mean_time_loss` | 전체 네트워크 평균 시간 손실 (초) |
| `network_avg_delay_sec` | 전체 네트워크 평균 지체 (초) |
| `network_vehicle_edge_coverage_ratio` | 네트워크 엣지 대비 차량 이용 엣지 비율 |
| `network_teleport_count` | 텔레포트(막힘 탈출) 발생 횟수 |
| `network_collision_count` | 네트워크 충돌 발생 횟수 |
| `total_vehicle_arrivals` | 전체 도착 차량 수 (별도 집계) |

---

## 로컬 500m 권역

| 변수명 | 설명 |
|--------|------|
| `local_500m_vehicle_count` | 횡단보도 500m 권역 내 차량 수 |
| `local_500m_mean_speed` | 권역 내 차량 평균 속도 (m/s) |
| `local_500m_mean_time_loss` | 권역 내 평균 시간 손실 (초) |
| `local_500m_avg_delay_sec` | 권역 내 차량 평균 지체 (초) |
| `local_500m_queue_proxy` | 권역 내 대기열 프록시 (정지 횟수 기반) |
| `local_500m_stop_count` | 권역 내 차량 정지 횟수 |
| `surrounding_lane_count` | 횡단보도 주변 차로 수 |
| `vehicle_bbox_coverage` | 차량 공간 점유율 (bounding box 기준) |

---

## 안전 (PET 기반)

| 변수명 | 설명 |
|--------|------|
| `pet_event_count` | PET 이벤트 총 발생 횟수 (보차 상충) |
| `very_risky_crossing_count` | 매우 위험한 횡단 횟수 (PET < 임계값) |
| `risky_crossing_count` | 위험한 횡단 횟수 |
| `safe_crossing_count` | 안전한 횡단 횟수 |
| `low_pet_event_count` | 낮은 PET 이벤트 수 (고위험 상충) |
| `low_pet_per_100_crossings` | 횡단 100회당 저PET 이벤트 수 |
| `low_pet_per_100_conflict_candidates` | 상충 후보 100회당 저PET 이벤트 수 |
| `pet_min` | PET 최솟값 (초) |
| `pet_p10` | PET 10번째 백분위수 (초) |
| `pet_mean` | PET 평균값 (초, 높을수록 안전) |
| `pet_coverage_ratio` | PET 측정 가능 횡단 비율 |

---

## 안전 종합 지표

| 변수명 | 설명 |
|--------|------|
| `safety_risk_score` | 안전 위험 점수 (높을수록 위험) |
| `accident_risk_estimate` | 사고 위험 추정값 |
| `accident_expected_value` | 사고 기댓값 (확률 × 심각도) |
