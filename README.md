# Smart Crosswalk SUMO
서울 중구 스마트 횡단보도 신호 연장 정책을 SUMO로 검증하는 코드베이스다.

문서 입구는 [`docs/index.md`](docs/index.md)다. 예전 문서는 `oldresult/archived_20260518_docs_legacy/docs/`로 옮겼다.

## 현재 실행 흐름

```text
commands/*.sh
→ smart_crosswalk_sumo/reporting/simple_final_pipeline.py
→ result/active/real_30seed_runs_sampled10/manifests/run_manifest.csv
→ outputs/smoke/* 또는 outputs/final/*
→ outputs/final/aggregate/final_summary.csv
```

## 바로 쓰는 명령

```bash
bash commands/verify.sh
bash commands/run_smoke.sh --dry-run
bash commands/run_smoke.sh --limit 1
bash commands/run_smoke.sh
bash commands/run_final.sh --dry-run
bash commands/run_final.sh
bash commands/aggregate_final.sh
bash commands/check_final_result.sh
```

## 환경 설정

터미널을 새로 열 때마다 `SUMO_HOME`을 다시 잡아야 한다.

```bash
export PROJECT_ROOT="/path/to/js_simulation"
cd "$PROJECT_ROOT"
export SUMO_HOME="/path/to/your/SUMO_HOME"
export PATH="$SUMO_HOME/bin:$PATH"
export PYTHONPATH="$PROJECT_ROOT:${PYTHONPATH:-}"
```

## 핵심 경로

- `commands/`
- `result/active/real_30seed_runs_sampled10/manifests/`
- `result/active/nets/`
- `outputs/smoke/`
- `outputs/final/`
- `outputs/final/aggregate/final_summary.csv`

## 지켜야 할 것

- `smoke`는 실행 확인용이다.
- `final`은 본실험이다.
- `baseline`과 `smart`는 같은 vehicle route를 써야 한다.
- `outputs/`와 `result/active/nets/*.net.xml`은 커밋 대상이 아니다.
- 새 도로망 생성 설명은 현재 문서에서 쓰지 않는다.
