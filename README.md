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

cut-off / graduation 검토는 full 30seed 실행과 분리된 sequential-light 흐름이다.
이 흐름은 1회 5초 보행자 녹색 연장 정책을 paired comparison으로 평가하며, primary endpoint는 보행자 평균 대기시간이 아니라 clearance failure / unfinished crossing / low-PET risk proxy다.
`PASS`는 qualified 후보 pool 유지 및 추가 seed 중단, `CUT`은 후보 pool 제외, `KEEP`은 다음 seed 실행, `RECHECK`는 데이터/구현 확인 대상이다.
paired comparison은 baseline-only smoke가 아니라 baseline/smart pair가 함께 있는 입력으로만 판단한다.
`simple_final_pipeline --limit N`은 baseline-first일 수 있으므로 paired 검증용으로 쓰지 않는다.
sampled10 본실험은 `--disable-ssm`를 기본으로 쓰고, final safety 검증에서만 SSM을 다시 켠다.
그룹별 sequential-light 운영은 [`docs/02_RUN_GUIDE.md`](docs/02_RUN_GUIDE.md)와 [`docs/07_HANDOFF_FOR_TAEHOON.md`](docs/07_HANDOFF_FOR_TAEHOON.md)를 따른다.

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

그룹별 본실험은 아래 스크립트를 쓴다.

```bash
bash result/active/real_30seed_runs_sampled10/commands/command_to_run_30seed_current_main_12.sh
bash result/active/real_30seed_runs_sampled10/commands/command_to_run_30seed_signal_fix_9.sh
bash result/active/real_30seed_runs_sampled10/commands/command_to_run_30seed_generated_signal_7.sh
bash result/active/real_30seed_runs_sampled10/commands/command_to_run_30seed_p1_p4_recovery_6.sh
```

전체 그룹을 순서대로 돌릴 때는 아래를 쓴다.

현재 `command_to_run_30seed_all_groups.sh`는 `p1_p4_recovery_6`를 건너뛴다. 이 그룹은 개별 명령으로 따로 돌린다.

```bash
bash result/active/real_30seed_runs_sampled10/commands/command_to_run_30seed_all_groups.sh
```

빠른 파이프라인 전용 체크 명령은 없다. `commands/verify.sh`는 환경/입력 확인용이고, `result/active/real_30seed_runs_sampled10/commands/command_to_check_30seed_results.sh`는 30seed 결과 상태 점검용이다.

태훈님 우선순위와 실행 순서는 [`docs/07_HANDOFF_FOR_TAEHOON.md`](docs/07_HANDOFF_FOR_TAEHOON.md)를 본다.

## 환경 설정

터미널을 새로 열 때마다 `SUMO_HOME`을 다시 잡아야 한다.
이 가이드는 Python 3.11.x와 `SUMO 1.26.0` 기준이다.

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
- `result/active/real_30seed_runs_sampled10/commands/`

## 지켜야 할 것

- `smoke`는 실행 확인용이다.
- `final`은 본실험이다.
- sequential-light 실행은 `sim_duration=540`, `--output-profile light`, KEEP-only candidate CSV를 기준으로 한다.
- `debug_trace`, `extension_trigger_debug`, `signal_phase_audit` 같은 대용량 debug 출력은 반복 실행에서 끈다.
- 그룹별 30seed 실행은 `result/active/real_30seed_runs_sampled10/commands/*.sh`를 쓴다.
- `baseline`과 `smart`는 같은 vehicle route를 써야 한다.
- `pedestrian_wait_delta`는 secondary metric이다.
- `extension_count`는 효과 metric이 아니라 policy exposure / trigger sanity check다.
- `outputs/`와 `result/active/nets/*.net.xml`은 커밋 대상이 아니다.
- 새 도로망 생성 설명은 현재 문서에서 쓰지 않는다.
