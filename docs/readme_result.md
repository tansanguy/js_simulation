# 스마트 횡단보도 시뮬레이션 결과 읽는 법

이 문서는 `integrated_short_test` 실행 결과로 생성된 데이터와 리포트를 해석하는 방법을 안내합니다.

## 1. 주요 결과 파일 안내
시뮬레이션이 완료되면 `outputs` 폴더에 다음과 같은 파일들이 생성됩니다.

*   **`baseline_vs_smart_summary.csv` (가장 중요)**: 스마트 횡단보도 설치 전(Baseline)과 후(Smart)를 직접 비교한 데이터입니다.
*   **`per_crosswalk_simulation_results.csv`**: 각 횡단보도별 상세 시뮬레이션 지표(지체 시간, 사고 위험도 등) 평균값입니다.
*   **`network_simulation_summary.csv`**: 해당 지역 전체(네트워크)의 차량 흐름 변화를 요약한 데이터입니다.

## 2. 핵심 지표 해석 방법

### 🛡️ 안전성 지표 (Safety)
*   **`safety_risk_score`**: 사고 발생 위험도를 수치화한 것입니다. 낮을수록 안전합니다.
*   **`safety_risk_delta`**: (스마트 - 기본) 수치입니다. **마이너스(-) 값이 클수록** 스마트 횡단보도 설치 후 안전성이 크게 개선되었음을 의미합니다.

### 🚗 교통 흐름 지표 (Traffic Flow)
*   **`avg_vehicle_delay_sec`**: 차량 한 대당 평균 지체 시간(초)입니다.
*   **`avg_vehicle_delay_delta_sec`**: 스마트 횡단보도 설치로 인해 추가 발생한 지체 시간입니다. 이 값이 너무 크면(예: +5초 이상) 교통 흐름에 방해가 된다고 판단할 수 있습니다.

### 🚶 보행자 지표 (Pedestrian)
*   **`extension_count_smart`**: 보행 신호가 실제로 연장된 횟수입니다.
*   **`total_extension_sec_smart`**: 총 연장된 시간(초)입니다.

## 3. 결과 해석 예시
만약 리포트에서 다음과 같은 결과가 나왔다면:
- `safety_risk_delta`: -1.5
- `avg_vehicle_delay_delta_sec`: +0.8

**해석**: "차량 지체는 약 0.8초 정도 미미하게 증가했으나, 보행자 사고 위험도는 1.5점 감소하여 안전성이 크게 향상되었습니다. 따라서 스마트 횡단보도 설치의 타당성이 높습니다."

---
## 4. 분석 방법론 및 후보 등급 체계 (Methodology & Implementation Tiers)

본 시뮬레이션은 실세계 T2 횡단보도 데이터를 SUMO 네트워크에 정밀하게 이식하기 위해 다음과 같은 **후보 구현 상태(Implementation Audit) 체계**를 따릅니다.

### 📊 후보 분류 등급 (Implementation Tiers)
*   **Grade A (Ready for Simulation)**: T2 좌표와 SUMO crossing 간 거리가 50m 이내이며, 보행자 경로가 해당 횡단보도를 정상적으로 통과하여 즉시 시뮬레이션이 가능한 상태입니다.
*   **Grade B (Route Repair Success)**: 50m 이내에 매칭되었으나, SUMO 네트워크 생성 시 보행자 경로(walkingarea 연결 등)에 단절이 있는 경우입니다. 이 경우 **자동 경로 복구(Route Repair)** 알고리즘을 통해 주변 연결 정보를 재탐색하여 시뮬레이션 가능 상태로 보정합니다.
*   **Grade C (Crossing Reconstruction Required)**: 중구 내부 데이터임에도 불구하고 50m 이내에 제어 가능한 crossing 또는 신호기(TLS)를 찾지 못한 경우입니다. 억지로 먼 곳에 매칭할 경우 엉뚱한 교차로를 제어하게 되므로, 이 후보들은 제외하되 향후 네트워크 편집(netedit)을 통한 보강이 필요한 대상으로 분류합니다.
*   **Grade D (Out of Scope)**: 행정구역(중구) 외부이거나 T2 후보군에 포함되지 않은 데이터입니다.

### ⚙️ 핵심 기술 원칙
*   **엄격한 50m 매칭 제한**: 위치 정합성과 신호 제어 논리 무결성을 위해 50m 초과 매칭은 절대 허용하지 않습니다.
*   **네트워크 범위**: 교통류 단절 및 경계 효과(Boundary Effect) 방지를 위해 **중구 행정경계 + 1000m Buffer** 네트워크를 사용합니다.
*   **데이터 보존**: 모든 분석 대상 후보는 삭제되지 않으며, `crosswalk_sumo_implementation_audit.csv`에 그 상태와 사유가 투명하게 기록되어 향후 인프라 개선 및 네트워크 보정의 근거 데이터로 활용됩니다.

---
본 시뮬레이션은 SUMO(Simulation of Urban MObility) 엔진을 사용하여 수행되었습니다.
