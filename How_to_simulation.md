## How to Simulation

이 문서는 다른 사람이 Phase 6 차량 실험을 바로 돌릴 수 있게 만든 실행용 안내서다.

목적:
- baseline/smart 차량 실험 실행
- 반복 실험 결과 위치 확인
- 결과 정리 파이프라인 실행
- 사람이 읽을 README/CSV 결과 확인

중요:
- `smart`는 보행자 전용 phase만 연장한다.
- mixed phase는 skip한다.
- `setPhaseDuration()`은 phase 전체를 연장한다.
- 그래서 결과 해석은 `extension_events.csv`와 `extension_skip_events.csv`를 같이 봐야 한다.

## 1. 먼저 볼 파일

- 실행 가이드: `How_to_simulation.md`
- 사람용 결과 README: `result/phase6_transition_after_recovery_20260514_220549/README_PHASE6_RESULTS.md`
- 사람용 결과 폴더: `result/phase6_vehicle_experiment_300_30seed_human/`

## 2. 실행 전 확인

아래는 시뮬레이션이 아니라 문법 확인이다.

```bash
cd /Users/junlee/Desktop/2026-1/js
python3 -m py_compile smart_crosswalk_sumo/run_phase6_vehicle_flow.py
python3 -m py_compile smart_crosswalk_sumo/organize_phase6_human_results.py
```

## 3. baseline 1회 실행

```bash
cd /Users/junlee/Desktop/2026-1/js

PYTHONPATH="/Users/junlee/Desktop/2026-1/js:${PYTHONPATH:-}" \
python3 smart_crosswalk_sumo/run_phase6_vehicle_flow.py \
  --candidate-csv result/phase6_transition_after_recovery_20260514_220549/phase6_mini_experiment_candidates_batch03_only.csv \
  --net-file result/phase_next_recovery_command_plan_20260514_200908/batch_03_run/recovery_tls_batch_network_v1.net.xml \
  --scenario baseline \
  --seed 1 \
  --sim-duration 600 \
  --warmup 0 \
  --extension-sec 5.0 \
  --output-dir result/phase6_transition_after_recovery_20260514_220549/phase6_vehicle_experiment_300_30seed_guard_check/NODE_7240/baseline \
  --vehicle-demand-mode hourly \
  --vehicle-hourly-volume 300 \
  --vehicle-arrival-process deterministic \
  --impact-radii-m 100,300,500,1000 \
  --include-global-scope \
  --limit-crosswalk-ids NODE_7240
```

기대:
- `completed=True`
- `extension_count=0`
- `extension_skip_events.csv`는 비어 있어도 된다

## 4. smart 1회 실행

```bash
cd /Users/junlee/Desktop/2026-1/js

PYTHONPATH="/Users/junlee/Desktop/2026-1/js:${PYTHONPATH:-}" \
python3 smart_crosswalk_sumo/run_phase6_vehicle_flow.py \
  --candidate-csv result/phase6_transition_after_recovery_20260514_220549/phase6_mini_experiment_candidates_batch03_only.csv \
  --net-file result/phase_next_recovery_command_plan_20260514_200908/batch_03_run/recovery_tls_batch_network_v1.net.xml \
  --scenario smart \
  --seed 1 \
  --sim-duration 600 \
  --warmup 0 \
  --extension-sec 5.0 \
  --output-dir result/phase6_transition_after_recovery_20260514_220549/phase6_vehicle_experiment_300_30seed_guard_check/NODE_7240/smart \
  --vehicle-demand-mode hourly \
  --vehicle-hourly-volume 300 \
  --vehicle-arrival-process deterministic \
  --impact-radii-m 100,300,500,1000 \
  --include-global-scope \
  --limit-crosswalk-ids NODE_7240
```

기대:
- `completed=True`
- `extension_events.csv`에는 pedestrian-only event만 들어감
- mixed phase면 `extension_skip_events.csv`에 기록됨

## 5. 사람용 결과 정리

실행 후 raw CSV를 읽어서 사람용 폴더를 만든다.

```bash
cd /Users/junlee/Desktop/2026-1/js

PYTHONPATH="/Users/junlee/Desktop/2026-1/js:${PYTHONPATH:-}" \
python3 smart_crosswalk_sumo/organize_phase6_human_results.py \
  --experiment-root result/phase6_transition_after_recovery_20260514_220549/phase6_vehicle_experiment_300_30seed \
  --transition-root result/phase6_transition_after_recovery_20260514_220549 \
  --output-dir result/phase6_vehicle_experiment_300_30seed_human \
  --volume 300
```

이 명령은:
- `result/phase6_vehicle_experiment_300_30seed_human/README.md`
- `result/phase6_vehicle_experiment_300_30seed_human/csv/*.csv`
- `result/phase6_vehicle_experiment_300_30seed_human/csv/*.md`
- `result/phase6_vehicle_experiment_300_30seed_human/logs/run_300_30seed_error_extract.txt`
- `result/phase6_transition_after_recovery_20260514_220549/README_PHASE6_RESULTS.md`

를 만든다.

## 6. 결과 폴더 구조

### raw run 출력

```text
result/phase6_transition_after_recovery_20260514_220549/phase6_vehicle_experiment_300_30seed/
  volume_300/
    seed_01/
      baseline/
      smart/
```

각 run 폴더 안:
- `phase6_vehicle_flow_{scenario}_results.csv`
- `phase6_vehicle_flow_{scenario}_extension_events.csv`
- `phase6_vehicle_flow_{scenario}_extension_skip_events.csv`
- `phase6_vehicle_flow_{scenario}_impact_edges.csv`
- `run_metadata.json`

### human pack

```text
result/phase6_vehicle_experiment_300_30seed_human/
  README.md
  csv/
    00_run_completion_summary.csv
    00_run_completion_summary.md
    01_all_results_long.csv
    01_all_results_long.md
    02_baseline_smart_paired_delta.csv
    02_baseline_smart_paired_delta.md
    03_paired_summary_by_crosswalk.csv
    03_paired_summary_by_crosswalk.md
    04_extension_events_all.csv
    04_extension_events_all.md
    05_signal_phase_audit.csv
    05_signal_phase_audit.md
    06_impact_scope_edge_counts.csv
    06_impact_scope_edge_counts.md
    07_source_file_inventory.csv
    07_source_file_inventory.md
  logs/
    run_300_30seed_error_extract.txt
```

## 7. 읽는 순서

1. `00_run_completion_summary.csv`
2. `02_baseline_smart_paired_delta.csv`
3. `03_paired_summary_by_crosswalk.csv`
4. `04_extension_events_all.csv`
5. `05_signal_phase_audit.csv`

## 8. 자주 틀리는 점

- `setPhaseDuration()`은 phase 전체를 연장한다.
- mixed phase를 연장하면 차량 green도 같이 늘 수 있다.
- 차량 지체 감소만 보고 정책 성공이라고 말하면 안 된다.
- 보행자 대기시간 지표가 없으면 그 효과는 쓸 수 없다.
- global 지표는 참고용이다.
- grouped `300 veh/h`는 최종 정책 효과가 아니라 smoke/구조 검증용이다.

## 9. 한 줄 요약

먼저 `baseline`과 `smart`를 1회씩 돌려 구조를 확인하고, 그 다음 `organize_phase6_human_results.py`로 사람용 CSV/README/log를 만든다.

```bash
cd /Users/junlee/Desktop/2026-1/js

python3 - <<'PY'
from pathlib import Path
import pandas as pd

root = Path("result/phase6_transition_after_recovery_20260514_220549/phase6_vehicle_experiment_300_30seed_by_crosswalk")
manifest = root / "phase6_vehicle_experiment_manifest.csv"
commands = root / "phase6_vehicle_experiment_commands.sh"
print("manifest_exists:", manifest.exists())
print("commands_exists:", commands.exists())
if manifest.exists():
    df = pd.read_csv(manifest)
    print("rows:", len(df))
    print("scenario_counts:")
    print(df["scenario"].value_counts().to_string())
    print("crosswalk_counts:")
    print(df["crosswalk_id"].value_counts().to_string())
    print("seed_min:", df["seed"].min())
    print("seed_max:", df["seed"].max())
    print("output_layouts:")
    print(df["output_layout"].value_counts().to_string())
PY
```

## 12. 반복 실행

생성된 `commands.sh`를 실행한다.

```bash
cd /Users/junlee/Desktop/2026-1/js

bash result/phase6_transition_after_recovery_20260514_220549/phase6_vehicle_experiment_300_30seed_by_crosswalk/phase6_vehicle_experiment_commands.sh \
  2>&1 | tee result/phase6_transition_after_recovery_20260514_220549/phase6_vehicle_experiment_300_30seed_by_crosswalk/run_300_30seed_by_crosswalk.log
```

주의:
- `commands.sh`는 `PYTHONPATH`를 스스로 설정해야 한다
- 다른 위치에서 실행할 때도 import가 깨지지 않아야 한다

## 13. 실행 후 결과 폴더

### by-crosswalk 결과 폴더 예시

```text
result/phase6_transition_after_recovery_20260514_220549/phase6_vehicle_experiment_300_30seed_by_crosswalk/
  crosswalk_LINK_43439/
    volume_300/
      seed_01/
        baseline/
        smart/
```

### 각 run 폴더 안의 주요 파일

- `phase6_vehicle_flow_baseline_results.csv` 또는 `phase6_vehicle_flow_smart_results.csv`
- `phase6_vehicle_flow_baseline_extension_events.csv` 또는 `phase6_vehicle_flow_smart_extension_events.csv`
- `phase6_vehicle_flow_baseline_impact_edges.csv` 또는 `phase6_vehicle_flow_smart_impact_edges.csv`
- `phase6_vehicle_flow_baseline_impact_anchor_debug.csv` 또는 `phase6_vehicle_flow_smart_impact_anchor_debug.csv`
- `phase6_vehicle_flow_baseline_extension_condition_debug.csv` 또는 `phase6_vehicle_flow_smart_extension_condition_debug.csv`
- `run_metadata.json`

### CSV에서 봐야 할 핵심 컬럼

- `completed`
  - `True`면 정상 종료
- `simulation_error`
  - 비어 있으면 정상
- `extension_count`
  - smart 연장 횟수
- `extension_sec`
  - `5.0`이어야 함
- `vehicle_requested_count`
  - 투입하려고 한 차량 수
- `vehicle_departed_count`
  - 실제 출발한 차량 수
- `vehicle_arrived_count`
  - 실제 도착한 차량 수
- `impact_500m_time_loss_mean`
  - 500m 영향권 차량 손실시간
- `impact_global_time_loss_mean`
  - 전체 네트워크 기준 차량 손실시간

### extension_events.csv 확인법

- smart에서 실제 5초 연장이 몇 번 있었는지 본다
- baseline은 보통 비어 있어도 정상
- `candidate_index`와 `original_candidate_index`를 같이 본다

## 14. 결과 확인

아래 스크립트로 핵심 컬럼을 한 번에 확인할 수 있다.

```bash
cd /Users/junlee/Desktop/2026-1/js

python3 - <<'PY'
from pathlib import Path
import pandas as pd
import json

roots = [
    Path("result/phase6_transition_after_recovery_20260514_220549/phase6_vehicle_experiment_300_30seed_by_crosswalk/crosswalk_NODE_7240/volume_300/seed_01/baseline"),
    Path("result/phase6_transition_after_recovery_20260514_220549/phase6_vehicle_experiment_300_30seed_by_crosswalk/crosswalk_NODE_7240/volume_300/seed_01/smart"),
]

cols = [
    "crosswalk_id",
    "completed",
    "simulation_error",
    "extension_count",
    "extension_sec",
    "vehicle_requested_count",
    "vehicle_route_valid_count",
    "vehicle_departed_count",
    "vehicle_arrived_count",
    "vehicle_route_invalid_count",
    "veh_waiting_time_mean",
    "veh_time_loss_mean",
    "impact_100m_time_loss_mean",
    "impact_300m_time_loss_mean",
    "impact_500m_time_loss_mean",
    "impact_1000m_time_loss_mean",
    "impact_global_time_loss_mean",
]

for root in roots:
    scenario = root.name
    print("\n==", scenario, "==")

    meta_p = root / "run_metadata.json"
    if meta_p.exists():
        meta = json.loads(meta_p.read_text(encoding="utf-8"))
        print("simulation_error:", meta.get("simulation_error"))
        print("candidate_ids:", meta.get("candidate_ids"))
        print("vehicle_demand_mode:", meta.get("vehicle_demand_mode"))
        print("vehicle_hourly_volume:", meta.get("vehicle_hourly_volume"))

    csv_path = root / f"phase6_vehicle_flow_{scenario}_results.csv"
    if not csv_path.exists():
        print("MISSING:", csv_path)
        continue

    df = pd.read_csv(csv_path)
    use_cols = [c for c in cols if c in df.columns]
    print(df[use_cols].to_string(index=False))

    events_p = root / f"phase6_vehicle_flow_{scenario}_extension_events.csv"
    if events_p.exists():
        events = pd.read_csv(events_p)
        print("extension_events:", len(events))
        if len(events):
            print(events.to_string(index=False))
PY
```

## 15. 반복 결과 분석

반복 실험 결과를 baseline/smart paired comparison으로 읽는 명령어다.

```bash
cd /Users/junlee/Desktop/2026-1/js

PYTHONPATH="/Users/junlee/Desktop/2026-1/js:${PYTHONPATH:-}" \
python3 smart_crosswalk_sumo/analyze_phase6_vehicle_experiment.py \
  --manifest-csv result/phase6_transition_after_recovery_20260514_220549/phase6_vehicle_experiment_300_30seed_by_crosswalk/phase6_vehicle_experiment_manifest.csv \
  --output-dir result/phase6_transition_after_recovery_20260514_220549/phase6_vehicle_experiment_300_30seed_by_crosswalk/analysis
```

설명:
- seed별로 baseline/smart를 pair matching한다
- paired difference를 계산한다
- 평균, 표준편차, 95% CI를 본다
- 30회 반복이 자동으로 유의성을 보장하는 것은 아니다

## 16. 해석 주의사항

- `300/600/900 veh/h`는 실제 중구 교통량이 아니라 sensitivity 초안값이다
- 실제 본실험에서는 TOPIS/T-Data 매칭값으로 교체해야 한다
- 30회 반복은 평균 추정을 안정화하는 것이지, 자동으로 유의성을 보장하지는 않는다
- bus stop / accident / disruption은 기준 시나리오에 넣지 않는다
- 버스 정차나 사고는 나중에 별도 stress scenario로 분리한다
- `global` 지표는 참고용이며, 정책 효과 판단은 설치 지점과 영향권 지표 중심으로 본다

## 17. 다음 단계

1. `batch03_300_smoke_full` 성공 확인
2. `hourly 600 veh/h` 실행
3. `hourly 900 veh/h` 실행
4. `30 seed` command plan 생성
5. command plan 검토
6. 반복 실행
7. analyze script로 paired comparison 분석
