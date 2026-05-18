# Smart Crosswalk SUMO
서울 중구 스마트 횡단보도 신호 연장 정책을 SUMO로 검증하는 코드베이스다.

현재 입구 문서는 [`docs/index.md`](docs/index.md)다. 레거시 문서는 `oldresult/archived_20260518_docs_legacy/docs/`로 넘겼다.

## 현재 파이프라인

```text
commands/*.sh
→ smart_crosswalk_sumo/reporting/simple_final_pipeline.py
→ result/active/real_30seed_runs_sampled10/manifests/run_manifest.csv
→ outputs/smoke/* 또는 outputs/final/*
→ outputs/final/aggregate/final_summary.csv
```

## 바로 쓸 명령

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

## 핵심 경로

- `commands/`
- `result/active/real_30seed_runs_sampled10/manifests/`
- `result/active/nets/`
- `outputs/smoke/`
- `outputs/final/`
- `outputs/final/aggregate/final_summary.csv`

## 지켜야 할 것

- smoke는 검증용이다.
- final은 본실험이다.
- baseline/smart는 같은 vehicle route를 써야 한다.
- outputs/와 net.xml은 커밋 대상이 아니다.
- 새 도로망 생성 설명은 현재 문서에서 쓰지 않는다.
