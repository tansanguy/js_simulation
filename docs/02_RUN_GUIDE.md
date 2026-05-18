# 실행 안내

현재 실행 표면은 `commands/*.sh`다. 내부에서는 `smart_crosswalk_sumo/reporting/simple_final_pipeline.py`가 처리한다.

## 실행 순서

1. 환경 확인
2. `smoke` dry-run
3. `smoke` 1개 실행
4. `smoke` 전체 실행
5. `final` dry-run
6. `final` 실행
7. `final` aggregate
8. `final` check

## 기본 명령

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

## 옵션

- `--source-pipeline-root`: 기본값은 `result/active/real_30seed_runs_sampled10`
- `--outputs-root`: 기본값은 `outputs`
- `--limit`: 앞에서 N개만 실행
- `--run-id`: 특정 run만 실행

예시:

```bash
bash commands/run_final.sh --dry-run --outputs-root outputs
```

## 실행 중 확인

```bash
ps aux | grep -E "simple_final_pipeline|run_sampled10_group|run_phase6_recovery_smoke|sumo" | grep -v grep
find outputs/smoke/runs -name simulation_result.csv | wc -l
find outputs/smoke/logs -type f | sort | tail -20
grep -R "Traceback\|TraCIException\|ERROR\|FAILED\|failed" outputs/smoke/logs | tail -80
```

## 중단/재실행

- 성공한 run은 같은 `output_dir`이면 다시 안 돈다.
- 다른 실험과 분리하려면 다른 `--outputs-root`를 쓰는 편이 낫다.
- `smoke`는 검증용, `final`은 본실험이다.
