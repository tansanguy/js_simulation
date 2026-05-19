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
sampled10 본실험은 SSM을 기본 off로 둔다. 필요할 때만 `--enable-ssm`를 명시한다.

cut-off / graduation 검토용 sequential-light 경로는 full 30seed 경로와 분리한다.
sequential-light에서는 `sim_duration=540`, `--output-profile light`, paired baseline/smart delta, KEEP-only candidate CSV를 기준으로 다음 seed를 정한다.
기존 후보별 `single_candidates/*` 반복 스크립트는 full/source-of-truth 경로로만 취급하고 sequential-light 입력으로 섞지 않는다.

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
- `simple_final_pipeline --limit N`은 `run_manifest.csv` 순서를 그대로 따르므로 baseline-placeholder 행만 먼저 잡힐 수 있다.
- paired comparison 검증에는 `--limit N`만 쓰지 말고, baseline 1개와 smart 1개가 함께 생성되는 run-id를 명시해서 돌린다.

## 5. 그룹별 30seed 본실험

현재 그룹별 실제 실행은 아래 4개 스크립트로 나뉜다.

```bash
bash result/active/real_30seed_runs_sampled10/commands/command_to_run_30seed_current_main_12.sh
bash result/active/real_30seed_runs_sampled10/commands/command_to_run_30seed_signal_fix_9.sh
bash result/active/real_30seed_runs_sampled10/commands/command_to_run_30seed_generated_signal_7.sh
bash result/active/real_30seed_runs_sampled10/commands/command_to_run_30seed_p1_p4_recovery_6.sh
```

전체 그룹을 순서대로 돌리려면 아래 스크립트를 쓰면 된다.

현재 `command_to_run_30seed_all_groups.sh` / `command_to_run_smoke30_seed1_all_groups.sh`는 `p1_p4_recovery_6`를 건너뛴다. `command_to_run_seed1_all_groups.sh`는 4개 그룹을 모두 돈다.

```bash
bash result/active/real_30seed_runs_sampled10/commands/command_to_run_30seed_all_groups.sh
```

파이프라인만 빠르게 확인하는 전용 명령은 없다. 가장 가까운 건 `bash commands/verify.sh`(환경/입력 점검), `bash result/active/real_30seed_runs_sampled10/commands/command_to_check_30seed_results.sh`(30seed 결과 상태 점검)이다.

## 6. 그룹별 입력 파일

각 그룹은 아래 입력을 쓴다.

- `current_main_12` → `result/active/nets/current_main_12.net.xml`
- `signal_fix_9` → `result/active/nets/signal_fix_9.net.xml`
- `generated_signal_7` → `result/active/nets/generated_signal_7.net.xml`
- `p1_p4_recovery_6` → `result/active/nets/p1_p4_recovery_6.net.xml`

같은 그룹의 후보 CSV는 `result/active/real_30seed_runs_sampled10/manifests/*.csv`에서 가져온다.

## 7. Sequential-light cut-off / graduation

정책 판정은 `smart_crosswalk_sumo.paired_significance_analysis`로 수행한다.
이 분석은 독립표본 비교가 아니라 같은 seed, 같은 route, 같은 demand의 `smart - baseline` paired delta를 사용한다.

정적 확인:

```bash
python3 -c "import ast, pathlib; paths=['smart_crosswalk_sumo/paired_significance_analysis.py','smart_crosswalk_sumo/run_phase6_recovery_smoke.py','smart_crosswalk_sumo/run_sampled10_group.py','smart_crosswalk_sumo/generate_reports.py']; [ast.parse(pathlib.Path(p).read_text()) for p in paths]; print('syntax_ok')"
python3 -m smart_crosswalk_sumo.paired_significance_analysis --help
python3 -m pytest tests/test_paired_significance_analysis.py tests/test_generate_reports.py -q
```

local smoke 1개 실행:

```bash
python3 -m smart_crosswalk_sumo.reporting.simple_final_pipeline --outputs-root outputs/policy_smoke run --mode smoke --limit 1
```

paired smoke 확인이 필요하면 baseline-only limit 대신 smart run-id를 직접 지정한다.
`current_main_12_smart_NODE_10335_seed01` 같은 smart row를 돌리면 같은 run 안에서 baseline/smart pair가 같이 생성된다.

결과 파일과 핵심 칼럼 확인:

```bash
find outputs/policy_smoke/smoke/runs -name simulation_result.csv | sort
python3 -c "import pandas as pd; p='outputs/policy_smoke/smoke/runs/current_main_12/baseline/seed01/simulation_result.csv'; df=pd.read_csv(p,nrows=1); print([c for c in df.columns if c in ['pedestrian_clearance_failure_count','unfinished_crossing_count','extension_count','local_500m_avg_delay_sec','vehicle_delay_cost','vehicle_route_sha256','pedestrian_route_sha256']])"
```

paired analysis와 상태별 candidate CSV 생성:

```bash
mkdir -p /private/tmp/phase6_policy_check
python3 -m smart_crosswalk_sumo.paired_significance_analysis \
  --run-glob "outputs/policy_smoke/smoke/runs/*/baseline/seed01" \
  --run-glob "outputs/policy_smoke/smoke/runs/*/smart/*/seed01" \
  --candidate-csv result/active/real_30seed_runs_sampled10/manifests/current_main_12_candidates.csv \
  --output /private/tmp/phase6_policy_check/sequential_summary.csv \
  --keep-output /private/tmp/phase6_policy_check/keep_candidates.csv \
  --pass-output /private/tmp/phase6_policy_check/pass_candidates.csv \
  --cut-output /private/tmp/phase6_policy_check/cut_candidates.csv \
  --recheck-output /private/tmp/phase6_policy_check/recheck_candidates.csv
```

다음 seed 실행에는 `keep_candidates.csv`만 사용한다.
`pass_candidates.csv`는 qualified pool 보존용이고, `cut_candidates.csv`와 `recheck_candidates.csv`는 다음 실행 입력이 아니다.

sequential-light seed 실행을 직접 만들 때는 아래 옵션을 유지한다.

```bash
python3 -m smart_crosswalk_sumo.run_sampled10_group \
  --candidate-csv /private/tmp/phase6_policy_check/keep_candidates.csv \
  --net-file result/active/nets/current_main_12.net.xml \
  --seed 2 \
  --output-dir /private/tmp/phase6_policy_check/current_main_12_seed02 \
  --sim-duration 540 \
  --warmup 0 \
  --traci_step_length 0.1 \
  --traffic_measure_radius_m 500.0 \
  --extension_increment 5.0 \
  --max_extensions 1 \
  --metric-sample-interval 10 \
  --vehicle-sample-interval 10 \
  --progress-interval 60 \
  --phase-aligned-ped-depart \
  --ped-repeat-count 5 \
  --ped-repeat-spacing-sec 2 \
  --include-vehicles \
  --output-profile light \
  --manifest-row-role baseline_placeholder \
  --manifest-crosswalk-id BASELINE_CURRENT_MAIN_12
```

## 8. 그룹별로 새 정책 돌리기

새 정책을 그룹별로 돌릴 때는 그룹 하나씩 따로 처리한다.
핵심은 `keep_candidates.csv`를 다음 seed 입력으로만 쓰고, 그룹별 candidate CSV와 net file을 섞지 않는 것이다.

흐름은 아래처럼 잡는다.

1. 그룹 선택
2. 그 그룹의 baseline/smart pair 결과를 만든다
3. `paired_significance_analysis`를 그 그룹 후보 CSV에만 돌린다
4. `KEEP`만 뽑은 `keep_candidates.csv`를 다음 seed 입력으로 쓴다
5. `PASS`가 나오면 그 그룹은 추가 seed를 멈춘다
6. `CUT`이면 그 그룹 후보는 pool에서 뺀다
7. `RECHECK`면 해당 그룹만 다시 확인한다

그룹별 입력은 아래를 쓴다.

- `current_main_12` → `result/active/real_30seed_runs_sampled10/manifests/current_main_12_candidates.csv`
- `signal_fix_9` → `result/active/real_30seed_runs_sampled10/manifests/signal_fix_9_candidates.csv`
- `generated_signal_7` → `result/active/real_30seed_runs_sampled10/manifests/generated_signal_7_candidates.csv`
- `p1_p4_recovery_6` → `result/active/real_30seed_runs_sampled10/manifests/p1_p4_recovery_6_candidates.csv`

그룹별 sequential-light 실행 예시는 아래 형태다.

```bash
GROUP=current_main_12
CANDIDATE_CSV="result/active/real_30seed_runs_sampled10/manifests/${GROUP}_candidates.csv"
NET_FILE="result/active/nets/${GROUP}.net.xml"

python3 -m smart_crosswalk_sumo.paired_significance_analysis \
  --run-glob "outputs/${GROUP}/baseline/seed01" \
  --run-glob "outputs/${GROUP}/smart/*/seed01" \
  --candidate-csv "$CANDIDATE_CSV" \
  --output "/private/tmp/phase6_policy_check/${GROUP}/sequential_summary.csv" \
  --keep-output "/private/tmp/phase6_policy_check/${GROUP}/keep_candidates.csv" \
  --pass-output "/private/tmp/phase6_policy_check/${GROUP}/pass_candidates.csv" \
  --cut-output "/private/tmp/phase6_policy_check/${GROUP}/cut_candidates.csv" \
  --recheck-output "/private/tmp/phase6_policy_check/${GROUP}/recheck_candidates.csv"

python3 -m smart_crosswalk_sumo.run_sampled10_group \
  --candidate-csv "/private/tmp/phase6_policy_check/${GROUP}/keep_candidates.csv" \
  --net-file "$NET_FILE" \
  --seed 2 \
  --output-dir "/private/tmp/phase6_policy_check/${GROUP}/seed02" \
  --sim-duration 540 \
  --warmup 0 \
  --traci_step_length 0.1 \
  --traffic_measure_radius_m 500.0 \
  --extension_increment 5.0 \
  --max_extensions 1 \
  --metric-sample-interval 10 \
  --vehicle-sample-interval 10 \
  --progress-interval 60 \
  --phase-aligned-ped-depart \
  --ped-repeat-count 5 \
  --ped-repeat-spacing-sec 2 \
  --include-vehicles \
  --output-profile light
```

이 방식은 그룹별 sequential-light 운영용이다.
`result/active/real_30seed_runs_sampled10/commands/*.sh`의 legacy 30seed wrapper와는 다르다.

## 9. 실행 중 확인

```bash
ps aux | grep -E "simple_final_pipeline|run_sampled10_group|run_phase6_recovery_smoke|sumo" | grep -v grep
find outputs/smoke/runs -name simulation_result.csv | wc -l
find outputs/smoke/logs -type f | sort | tail -20
grep -R "Traceback\|TraCIException\|ERROR\|FAILED\|failed" outputs/smoke/logs | tail -80
```

## 10. 중단 / 재실행

- 성공한 run은 같은 `output_dir`이면 다시 돌지 않는다.
- 다른 실험과 분리하려면 다른 `--outputs-root`를 쓰는 편이 낫다.
- `smoke`는 검증용, `final`은 본실험이다.

## 11. Git 점검

배포 전에는 result/output/net/xml/.DS_Store/oldresult가 섞이지 않았는지 확인한다.

```bash
git status --short --branch
git diff -- smart_crosswalk_sumo docs commands README.md tests
git status --short -- result outputs oldresult
find . -name ".DS_Store" -print
git ls-files | rg "^(result/|outputs/|oldresult/)|\\.net\\.xml$|\\.DS_Store$"
```
