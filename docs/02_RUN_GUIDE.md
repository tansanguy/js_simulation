# 실행 안내

이 문서는 현재 저장소에서 실제로 쓰는 실행 표면을 정리한 문서다.
지금 기준으로는 `commands/*.sh`가 최상위 실행 진입점이고, 내부에서는 `smart_crosswalk_sumo/reporting/simple_final_pipeline.py`가 `smoke`와 `final` 흐름을 관리한다.

## 1. 실행 구조

```text
commands/*.sh
→ smart_crosswalk_sumo/reporting/simple_final_pipeline.py
→ result/active/real_30seed_runs_sampled10/manifests/run_manifest.csv
→ outputs/smoke/* 또는 outputs/final/*
→ outputs/final/aggregate/final_summary.csv
```

그룹별 30seed 본실험은 위 `smoke` / `final` 흐름과 별도로,
`result/active/real_30seed_runs_sampled10/commands/*.sh` 아래의 그룹 전용 스크립트로 실행한다.
실제 계산은 모두 `smart_crosswalk_sumo.run_sampled10_group`가 담당한다.

## 2. 먼저 확인할 것

아래 순서로 보면 안전하다.

1. 환경 확인
2. `smoke` dry-run
3. `smoke` 1개 실행
4. `smoke` 전체 실행
5. `final` dry-run
6. `final` 실행
7. `final` aggregate
8. `final` check

## 3. 기본 명령

```bash
bash commands/verify.sh
bash commands/run_smoke.sh --dry-run
bash commands/run_smoke.sh --limit 1
bash commands/run_smoke.sh --run-id current_main_12_smart_NODE_10335_seed01
bash commands/run_smoke.sh
bash commands/run_final.sh --dry-run
bash commands/run_final.sh
bash commands/aggregate_final.sh
bash commands/check_final_result.sh
```

## 4. `smoke` 와 `final` 차이

- `smoke`는 실행이 정상인지 확인하는 용도다.
- `final`은 본실험이다.
- `--dry-run`은 실제 실행 없이 계획만 확인한다.
- `--limit 1`은 맨 앞의 1개만 돌려 빠르게 검증한다.
- `--run-id`는 특정 run 하나만 지정한다.

## 5. 그룹별 30seed 본실험

현재 그룹별 실제 실행은 아래 4개 스크립트로 나뉜다.

```bash
bash result/active/real_30seed_runs_sampled10/commands/command_to_run_30seed_current_main_12.sh
bash result/active/real_30seed_runs_sampled10/commands/command_to_run_30seed_signal_fix_9.sh
bash result/active/real_30seed_runs_sampled10/commands/command_to_run_30seed_generated_signal_7.sh
bash result/active/real_30seed_runs_sampled10/commands/command_to_run_30seed_p1_p4_recovery_6.sh
```

전체 그룹을 순서대로 돌리려면 아래 스크립트를 쓰면 된다.

```bash
bash result/active/real_30seed_runs_sampled10/commands/command_to_run_30seed_all_groups.sh
```

## 6. 그룹별 입력 파일

각 그룹은 아래 입력을 쓴다.

- `current_main_12` → `result/active/nets/current_main_12.net.xml`
- `signal_fix_9` → `result/active/nets/signal_fix_9.net.xml`
- `generated_signal_7` → `result/active/nets/generated_signal_7.net.xml`
- `p1_p4_recovery_6` → `result/active/nets/p1_p4_recovery_6.net.xml`

같은 그룹의 후보 CSV는 `result/active/real_30seed_runs_sampled10/manifests/*.csv`에서 가져온다.

## 7. 실행 중 확인

```bash
ps aux | grep -E "simple_final_pipeline|run_sampled10_group|run_phase6_recovery_smoke|sumo" | grep -v grep
find outputs/smoke/runs -name simulation_result.csv | wc -l
find outputs/smoke/logs -type f | sort | tail -20
grep -R "Traceback\|TraCIException\|ERROR\|FAILED\|failed" outputs/smoke/logs | tail -80
```

## 8. 중단 / 재실행

- 성공한 run은 같은 `output_dir`이면 다시 돌지 않는다.
- 다른 실험과 분리하려면 다른 `--outputs-root`를 쓰는 편이 낫다.
- `smoke`는 검증용, `final`은 본실험이다.
