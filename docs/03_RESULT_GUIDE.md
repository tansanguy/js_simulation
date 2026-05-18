# Result Guide

결과는 `simulation_result.csv`와 `final_summary.csv` 두 층으로 본다.

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

- `smoke`는 120초, seed 1개, 파이프라인 검증용이다.
- `final`은 600초, seed 1~30, 본실험용이다.
- `smoke` 결과는 정책 효과 증거가 아니다.
- `final_summary.csv`가 최종 source-of-truth다.

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
- `warning`: 쓸 수는 있으나 주석 필요
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
