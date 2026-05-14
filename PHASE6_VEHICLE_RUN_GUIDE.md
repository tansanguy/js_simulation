# Phase 6 Vehicle Flow 실행 가이드

이 문서는 `docs/` 설계 문서가 아니라, 팀원이 터미널에 그대로 복붙해서 실행하고 결과 CSV를 읽는 용도의 루트 실행 가이드다.

## 1. 이 문서는 무엇인가

스마트 횡단보도 Phase 6 차량 흐름 실험을 어떻게 실행하는지, 결과가 어디에 저장되는지, CSV를 어떻게 읽는지 정리한 안내서다.

- 차량 흐름 실험 실행 방법
- 실행 결과 저장 위치
- 결과 CSV 읽는 법
- baseline과 smart 비교 방법
- 30 seed 반복으로 확장하는 방법

## 2. 지금까지 성공한 것

- 보행자-only batch03 mini 실험 성공
- NODE_7240에서 보행자 감지 기반 5초 1회 연장 확인
- 차량 runner smoke 성공
- NODE_7240 vehicle smoke에서 baseline/smart 모두 `completed=True`
- 차량 3대 모두 `route valid`, `departed`, `arrived`
- smart에서 `extension_count=1`, `extension_sec=5.0` 재현
- 현재 `run_phase6_vehicle_flow.py`에는 hourly demand와 impact scope 기능이 들어가 있음
- 기본 impact scope는 `100m`, `300m`, `500m`, `1000m`, `global`
- hourly demand는 `vehicle_count = hourly_volume * sim_duration / 3600`
- 30 seed plan/analyze 스크립트도 생성되어 있음
- `py_compile` 통과함

중요:
- 현재 smoke는 `3대 / 600초 = 18 veh/h` 수준이라 실제 교통량 본실험이 아니다.
- `300/600/900 veh/h`는 감도분석용 초안값이다.
- 실제 중구 교통량은 나중에 TOPIS/T-Data 매칭값으로 교체할 예정이다.

## 3. baseline과 smart가 다른 점

### baseline

- 기존 신호
- 보행자가 있어도 초록 시간 연장 없음

### smart

- 보행자가 감지되면 보행자 초록을 5초 연장
- 같은 phase에서 1회만 연장
- `extension_sec`는 `5.0`으로 고정

## 4. 실험 조건 초안

초안 조건은 아래와 같다.

- 후보: batch03 후보 3개
- 시뮬레이션 시간: 600초, 즉 10분
- warmup: 0
- seed: 우선 42 한 번
- 교통량: 300 veh/h부터 시작
- 환산:
  - 300 veh/h -> 50대
  - 600 veh/h -> 100대
  - 900 veh/h -> 150대
- 관찰 범위: `100m`, `300m`, `500m`, `1000m`, `global`
- 버스 정차/사고/돌발상황: 기준 실험에서는 제외
- 버스 정차/사고는 나중에 stress scenario로 분리

## 5. 실행 전 확인

아래 명령어는 시뮬레이션이 아니라 코드 문법만 확인한다.

```bash
cd /Users/junlee/Desktop/2026-1/js

python3 -m py_compile smart_crosswalk_sumo/run_phase6_vehicle_flow.py
python3 -m py_compile smart_crosswalk_sumo/plan_phase6_vehicle_experiment.py
python3 -m py_compile smart_crosswalk_sumo/analyze_phase6_vehicle_experiment.py
```

## 6. PYTHONPATH 오류와 해결

반복 실험에서 `commands.sh`를 그냥 실행하면 아래 오류가 날 수 있다.

```text
ModuleNotFoundError: No module named 'smart_crosswalk_sumo'
```

원인:
- `commands.sh` 안에서 프로젝트 루트를 `PYTHONPATH`에 넣지 않았기 때문이다.

해결:
- `commands.sh` 상단에 아래 두 줄이 들어가야 한다.

```bash
cd /Users/junlee/Desktop/2026-1/js
export PYTHONPATH="/Users/junlee/Desktop/2026-1/js:${PYTHONPATH:-}"
```

- 또는 실행할 때 직접 이렇게 감싸도 된다.

```bash
PYTHONPATH="/Users/junlee/Desktop/2026-1/js:${PYTHONPATH:-}" bash result/.../phase6_vehicle_experiment_commands.sh
```

## 7. 가장 작은 전체 버전 실행

먼저 baseline 1회, smart 1회만 실행한다.
이 단계는 전체 구조가 돌아가는지 보는 smoke 단계다.

### baseline

```bash
cd /Users/junlee/Desktop/2026-1/js

PYTHONPATH="/Users/junlee/Desktop/2026-1/js:${PYTHONPATH:-}" \
python3 smart_crosswalk_sumo/run_phase6_vehicle_flow.py \
  --candidate-csv result/phase6_transition_after_recovery_20260514_220549/phase6_mini_experiment_candidates_batch03_only.csv \
  --net-file result/phase_next_recovery_command_plan_20260514_200908/batch_03_run/recovery_tls_batch_network_v1.net.xml \
  --scenario baseline \
  --seed 42 \
  --sim-duration 600 \
  --warmup 0 \
  --extension-sec 5.0 \
  --output-dir result/phase6_transition_after_recovery_20260514_220549/vehicle_flow_hourly/batch03_300_smoke_full/baseline \
  --vehicle-demand-mode hourly \
  --vehicle-hourly-volume 300 \
  --vehicle-arrival-process deterministic \
  --vehicle-lane-scale 1.0 \
  --impact-radii-m 100,300,500,1000 \
  --include-global-scope
```

### smart

```bash
cd /Users/junlee/Desktop/2026-1/js

PYTHONPATH="/Users/junlee/Desktop/2026-1/js:${PYTHONPATH:-}" \
python3 smart_crosswalk_sumo/run_phase6_vehicle_flow.py \
  --candidate-csv result/phase6_transition_after_recovery_20260514_220549/phase6_mini_experiment_candidates_batch03_only.csv \
  --net-file result/phase_next_recovery_command_plan_20260514_200908/batch_03_run/recovery_tls_batch_network_v1.net.xml \
  --scenario smart \
  --seed 42 \
  --sim-duration 600 \
  --warmup 0 \
  --extension-sec 5.0 \
  --output-dir result/phase6_transition_after_recovery_20260514_220549/vehicle_flow_hourly/batch03_300_smoke_full/smart \
  --vehicle-demand-mode hourly \
  --vehicle-hourly-volume 300 \
  --vehicle-arrival-process deterministic \
  --vehicle-lane-scale 1.0 \
  --impact-radii-m 100,300,500,1000 \
  --include-global-scope
```

## 8. 실행 조건 초안

- 후보: batch03 후보 3개
- 시뮬레이션 시간: 600초, 즉 10분
- warmup: 0
- seed: 우선 42 한 번
- 교통량: 300 veh/h부터 시작
- 환산:
  - 300 veh/h -> 50대
  - 600 veh/h -> 100대
  - 900 veh/h -> 150대
- 관찰 범위: `100m`, `300m`, `500m`, `1000m`, `global`
- 버스 정차/사고/돌발상황: 기준 실험에서는 제외
- 버스 정차/사고는 나중에 stress scenario로 분리

## 9. 결과 폴더 구조

### grouped 방식

그룹 단위로 결과를 모은다.

예시:

```text
result/phase6_transition_after_recovery_20260514_220549/phase6_vehicle_experiment_draft/
  volume_300/
    seed_01/
      baseline/
      smart/
```

특징:
- 60 runs 수준으로 관리하기 쉽다
- batch 전체 비교를 빠르게 보기 좋다
- 횡단보도별 세부 분리는 하지 않는다

### by-crosswalk 방식

횡단보도별로 결과를 분리한다.

예시:

```text
result/phase6_transition_after_recovery_20260514_220549/phase6_vehicle_experiment_300_30seed_by_crosswalk/
  crosswalk_LINK_43439/
    volume_300/
      seed_01/
        baseline/
        smart/
      seed_02/
        baseline/
        smart/
  crosswalk_NODE_7240/
    volume_300/
      seed_01/
        baseline/
        smart/
  crosswalk_LINK_52248/
    volume_300/
      seed_01/
        baseline/
        smart/
```

특징:
- 횡단보도별 결과를 바로 보기 쉽다
- 실행 수가 늘어난다
- batch03 3개 후보, 30 seed, baseline/smart면 180 runs다

추천:
- 팀원 공유와 횡단보도별 결과 확인에는 by-crosswalk 방식을 추천한다
- 대신 실행 수가 크게 늘어난다는 점을 알아야 한다

## 10. by-crosswalk 계획 파일 만들기

반복 실험용 명령어 목록은 먼저 생성만 한다.

```bash
cd /Users/junlee/Desktop/2026-1/js

PYTHONPATH="/Users/junlee/Desktop/2026-1/js:${PYTHONPATH:-}" \
python3 smart_crosswalk_sumo/plan_phase6_vehicle_experiment.py \
  --candidate-csv result/phase6_transition_after_recovery_20260514_220549/phase6_mini_experiment_candidates_batch03_only.csv \
  --net-file result/phase_next_recovery_command_plan_20260514_200908/batch_03_run/recovery_tls_batch_network_v1.net.xml \
  --output-root result/phase6_transition_after_recovery_20260514_220549 \
  --experiment-name phase6_vehicle_experiment_300_30seed_by_crosswalk \
  --sim-duration 600 \
  --warmup 0 \
  --extension-sec 5.0 \
  --seeds 1-30 \
  --vehicle-hourly-volumes 300 \
  --vehicle-arrival-process deterministic \
  --impact-radii-m 100,300,500,1000 \
  --include-global-scope \
  --output-layout by-crosswalk
```

설명:
- 이 명령어는 반복 시뮬레이션을 직접 실행하지 않는다
- 실행할 명령어 목록만 만든다
- by-crosswalk는 횡단보도별 결과를 분리해서 저장한다

## 11. 계획 파일 확인

생성된 파일이 있는지, 몇 줄인지 먼저 본다.

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
