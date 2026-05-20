# Smart Crosswalk SUMO
서울 중구 스마트 횡단보도 신호 연장 정책을 SUMO로 검증하는 저장소다.

이 저장소의 현재 기준 문서는 [`docs/index.md`](docs/index.md)이며, 예전 문서는 `oldresult/archived_20260518_docs_legacy/docs/` 아래로 옮겼다.

## 한눈에 보기

- `commands/*.sh`가 현재 최상위 실행 진입점이다.
- 실제 파이프라인은 `smart_crosswalk_sumo/reporting/simple_final_pipeline.py`를 중심으로 `smoke`와 `final` 흐름을 관리한다.
- 그룹별 30seed 본실험은 `result/active/real_30seed_runs_sampled10/commands/*.sh`를 사용한다.
- 전역 맵 분석은 별도 흐름으로 `smart_crosswalk_sumo/run_global_map_pipeline.py`를 사용한다.

## 현재 실행 흐름

```text
commands/*.sh
→ smart_crosswalk_sumo/reporting/simple_final_pipeline.py
→ result/active/real_30seed_runs_sampled10/manifests/run_manifest.csv
→ outputs/smoke/* 또는 outputs/final/*
→ outputs/final/aggregate/final_summary.csv
```

`smoke`는 실행 확인용, `final`은 본실험용이다. `simple_final_pipeline --limit N`은 `run_manifest.csv` 순서를 그대로 따르므로, baseline-first 행만 먼저 잡힐 수 있다. paired comparison 확인이 필요하면 baseline/smart pair가 함께 생성되는 run-id를 직접 지정한다.

sequential-light cut-off / graduation 검토는 full 30seed 실행과 분리한다. 이 경로는 `sim_duration=540`, `--output-profile light`, KEEP-only candidate CSV를 기준으로 운영한다.

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

## 그룹별 30seed 실행

그룹별 본실험은 아래 스크립트를 사용한다.

```bash
bash result/active/real_30seed_runs_sampled10/commands/command_to_run_30seed_current_main_12.sh
bash result/active/real_30seed_runs_sampled10/commands/command_to_run_30seed_signal_fix_9.sh
bash result/active/real_30seed_runs_sampled10/commands/command_to_run_30seed_generated_signal_7.sh
bash result/active/real_30seed_runs_sampled10/commands/command_to_run_30seed_p1_p4_recovery_6.sh
```

전체 그룹을 순서대로 실행할 때는 아래 스크립트를 쓴다.

```bash
bash result/active/real_30seed_runs_sampled10/commands/command_to_run_30seed_all_groups.sh
```

빠른 점검용으로는 `bash commands/verify.sh`와 `bash result/active/real_30seed_runs_sampled10/commands/command_to_check_30seed_results.sh`를 쓴다.

## 환경 설정

터미널을 새로 열 때마다 `SUMO_HOME`을 다시 설정해야 한다.

```bash
export PROJECT_ROOT="/path/to/js"
cd "$PROJECT_ROOT"
export SUMO_HOME="/path/to/SUMO_HOME"
export PATH="$SUMO_HOME/bin:$PATH"
export PYTHONPATH="$PROJECT_ROOT:${PYTHONPATH:-}"
```

이 저장소는 Python 3.11.x와 `SUMO 1.26.0` 기준이다.

## 핵심 경로

- `commands/`
- `docs/`
- `result/active/real_30seed_runs_sampled10/manifests/`
- `result/active/real_30seed_runs_sampled10/commands/`
- `result/active/nets/`
- `outputs/smoke/`
- `outputs/final/`
- `outputs/final/aggregate/final_summary.csv`

## 현재 기준

- `smoke`는 실행 확인용이다.
- `final`은 본실험이다.
- `baseline`과 `smart`는 같은 vehicle route를 써야 한다.
- `pedestrian_wait_delta`는 secondary metric이다.
- `extension_count`는 효과 metric이 아니라 policy exposure / trigger sanity check다.
- `outputs/`와 `result/active/nets/*.net.xml`은 기본적으로 커밋 대상이 아니다.
- 세부 정책 기준은 [`docs/04_PIPELINE_POLICY.md`](docs/04_PIPELINE_POLICY.md)를 본다.

