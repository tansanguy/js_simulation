# 🚦 스마트 횡단보도 시뮬레이션 결과 변수 가이드

이 문서는 스마트 횡단보도 DES(Discrete Event Simulation) 실험 결과 파일(`simulation_result.csv`)에 포함된 주요 변수들의 의미와 산출 방식을 설명합니다.

---

## 1. 기본 정보 (Metadata)

*   **`run_name`**: 해당 시뮬레이션 실행의 고유 명칭입니다. (예: `smoke_20260520_1030`)
*   **`crosswalk_id`**: 분석 대상인 횡단보도의 고유 ID입니다.
*   **`scenario`**: 실험 시나리오를 구분합니다.
    *   `baseline`: 일반 신호 제어 (스마트 기능 OFF)
    *   `smart`: 스마트 횡단보도 신호 제어 (스마트 기능 ON)
*   **`scenario_name` / `demand_profile`**: 시뮬레이션에 적용된 교통량 및 보행자 수요 시나리오의 이름입니다. (예: `peak_morning`, `off_peak`)
*   **`seed`**: 시뮬레이션 무작위성을 결정하는 시드 값입니다. 동일 조건에서 결과가 같은지 재현하는 데 사용됩니다.

---

## 2. 시뮬레이션 상태 (Simulation Status)

*   **`elapsed_sec`**: 시뮬레이션 1회를 실행하는 데 걸린 실제 시간(초)입니다.
*   **`completed`**: 시뮬레이션이 중단 없이 정상적으로 종료되었는지 여부(`True`/`False`)를 나타냅니다.
*   **`sim_duration`**: 시뮬레이션 내부 세계에서 흐른 총 시간(초)입니다.

---

## 3. 보행자 안전 및 편의 (Pedestrian Safety & Comfort)

*   **`ped_crossing_presence_steps`**: 횡단보도 위에 보행자가 한 명이라도 존재했던 시간 단계(Step)의 총합입니다. 보행자 노출도를 측정합니다.
*   **`pet_event_count`**: 보행자와 차량 간의 상충 발생 시 관측된 **PET(Post-Encroachment Time)** 이벤트의 총 횟수입니다.
    *   *PET란?* 보행자가 떠난 자리에 차량이 도착하거나, 차량이 지나간 자리에 보행자가 진입하는 데 걸린 시간차를 의미합니다.
*   **`very_risky_crossing_count`**: PET가 1.34초 미만인 매우 위험한 횡단 횟수입니다. (Severe Risk)
*   **`risky_crossing_count`**: PET가 1.34초 이상 2.88초 미만인 위험한 횡단 횟수입니다. (Moderate Risk)
*   **`safe_crossing_count`**: PET가 2.88초 이상인 안전한 횡단 횟수입니다. (Safe)
*   **`low_pet_event_count`**: `very_risky`와 `risky` 횟수를 합친 값으로, 잠재적 사고 위험이 높은 사례의 총합입니다.
*   **`low_pet_per_100_crossings`**: 보행자 100회 횡단당 발생하는 low PET 사례의 비율입니다.
*   **`low_pet_per_100_conflict_candidates`**: 차량-보행자 간의 잠재적 상충 기회(Conflict Candidates) 100회당 발생하는 low PET 사례의 비율입니다.
*   **`pet_min` / `pet_p10` / `pet_mean`**: 관측된 PET 값들의 최소값, 하위 10% 값, 평균값입니다.
*   **`pet_available` / `pet_unavailable_reason`**: PET 계산이 가능했는지 여부와, 불가능했다면 그 사유(예: 보행자 없음, 차량 상충 없음 등)를 나타냅니다.
*   **`accident_risk_estimate`**: PET 데이터와 보행자 유형(고령자 등)을 종합하여 산출한 **사고 리스크 추정치**입니다.
*   **`safety_risk_score`**: 안전 리스크를 점수화한 지표입니다. (위험 PET 사례 + 고령자 미완료 횡단 가중치)
*   **`accident_expected_value`**: `safety_risk_score`에 사고 비용을 곱하여 산출한 사회적 비용 추정값입니다.

---

## 4. 차량 흐름 및 지연 (Traffic Flow & Delay)

*   **`veh_delay_mean` (`veh_avg_delay_sec`)**: 횡단보도 주변 상충 구간을 통과하는 차량들의 평균 지연 시간(초)입니다.
*   **`veh_delay_max`**: 특정 차량이 겪은 최대 지연 시간(초)입니다.
*   **`total_vehicle_arrivals`**: 시뮬레이션 종료 시점까지 목적지에 도달한 총 차량 대수입니다.
*   **`surrounding_lane_count`**: 분석 대상 주변(반경 500m 이내)에 포함된 차로의 수입니다.

### 📍 주변 500m 영향권 지표 (Local 500m Radius)
횡단보도 설치로 인해 주변 도로망에 미치는 영향을 평가하는 지표들입니다.
*   **`local_500m_vehicle_count`**: 500m 반경 내를 통과한 고유 차량 대수입니다.
*   **`local_500m_mean_speed`**: 반경 내 차량들의 평균 속도(m/s)입니다.
*   **`local_500m_avg_delay_sec`**: 반경 내 차량들이 겪은 평균 누적 지연 시간(초)입니다.
*   **`local_500m_mean_time_loss`**: 반경 내 차량들의 평균 시간 손실(Time Loss)입니다.
*   **`local_500m_queue_proxy`**: 반경 내에서 정지해 있는 차량들의 평균 대수(대기행렬 근사치)입니다.
*   **`local_500m_stop_count`**: 시뮬레이션 전 기간 동안 반경 내에서 정지 상태가 관측된 누적 횟수입니다.

---

## 5. 스마트 제어 성능 (Smart Control Performance)

*   **`extension_count` (`pedestrian_green_extension_count`)**: 보행자 안전을 위해 보행 녹색 신호를 연장한 총 횟수입니다.
*   **`total_extension_sec`**: 스마트 기능을 통해 추가로 제공된 보행 녹색 시간의 총합(초)입니다.

---

## 6. 해석 가이드

1.  **Safety (안전성)**: `smart` 시나리오에서 `low_pet_event_count`나 `safety_risk_score`가 `baseline` 대비 낮아졌다면 스마트 횡단보도가 효과적으로 작동하고 있는 것으로 해석합니다.
2.  **Efficiency (효율성)**: `smart` 시나리오에서 `veh_delay_mean`이나 `local_500m_avg_delay_sec`가 급격히 상승하지 않는지 확인하여 교통 흐름 저해 정도를 평가합니다.
3.  **Exposure (노출도)**: `ped_crossing_presence_steps`가 너무 낮으면 해당 시드에서의 실험 데이터 신뢰도가 낮을 수 있으므로 다른 시드 결과와 함께 검토해야 합니다.
