# Handoff for Taehoon

목적은 하나다. 새 도로망 생성 없이, 준비된 net.xml과 current pipeline으로 smoke/final을 재현 실행하는 것.

## Project Root

프로젝트 루트는 태훈님 환경에 맞는 실제 clone 경로를 쓴다.

```bash
cd "$PROJECT_ROOT"
```

`$PROJECT_ROOT` 예시:

- `<taehoon-mac-root>/js_simulation`
- `<azure-root>/js_simulation`

## What To Put In Place

- `result/active/nets/*.net.xml`
- `result/active/pedestrian_assumption/*.csv`
- `result/active/real_30seed_runs_sampled10/manifests/run_manifest.csv`

## Env

```bash
export SUMO_HOME="$SUMO_HOME"
export PATH="$SUMO_HOME/bin:$PATH"
export PYTHONPATH="$PROJECT_ROOT:${PYTHONPATH:-}"
```

`SUMO_HOME`가 비어 있으면 commands wrapper 또는 Python helper가 후보 경로를 찾는다.

## Run

```bash
bash commands/verify.sh
bash commands/run_smoke.sh --dry-run --limit 1
bash commands/run_smoke.sh --limit 1
bash commands/run_final.sh --dry-run --limit 1
bash commands/run_final.sh --limit 1
bash commands/aggregate_final.sh
bash commands/check_final_result.sh
```

## Check

```bash
ps aux | grep -E "simple_final_pipeline|run_sampled10_group|run_phase6_recovery_smoke|sumo" | grep -v grep
find outputs/smoke/runs -name simulation_result.csv | wc -l
grep -R "Traceback\|TraCIException\|ERROR\|FAILED\|failed" outputs/smoke/logs | tail -80
```

## Do Not

- 새 도로망 생성
- `outputs/` 커밋
- `net.xml` 커밋
- smoke를 최종 결과로 설명
