# 태훈님 인수인계

이 문서는 태훈님이 바로 실행할 수 있게 쓴 작업용 인수인계다.
여기서는 팀 구글드라이브에서 받은 `XML`과 `net.xml`을 프로젝트 로컬 경로에 넣는 절차와,
그 다음에 어떤 명령을 어떤 순서로 돌리면 되는지까지 적는다.

중요:

- `result/active/real_30seed_runs_sampled10/commands/*.sh`는 legacy full 30seed wrapper다.
- 새 sequential-light / cut-off / graduation 판단은 `docs/04_PIPELINE_POLICY.md` 기준으로 본다.
- paired comparison은 baseline-only smoke가 아니라 baseline/smart pair가 같이 있는 입력으로만 판단한다.

이 절차는 Python 3.11.x와 SUMO 1.26.0 기준이다.

다른 준비나 실행 설명이 더 필요하면 아래 문서를 보면 된다.

- 환경 준비: [`docs/01_SETUP.md`](01_SETUP.md)
- 실행 순서: [`docs/02_RUN_GUIDE.md`](02_RUN_GUIDE.md)
- 결과 해석: [`docs/03_RESULT_GUIDE.md`](03_RESULT_GUIDE.md)
- 정책 기준: [`docs/04_PIPELINE_POLICY.md`](04_PIPELINE_POLICY.md)
- 오류 확인: [`docs/05_TROUBLESHOOTING.md`](05_TROUBLESHOOTING.md)
- 커밋 기준: [`docs/06_COMMIT_GUIDE.md`](06_COMMIT_GUIDE.md)

## 0. 먼저 확인할 것

```bash
python3.11 --version
echo "$SUMO_HOME"
```

- Python은 `3.11.x`여야 한다.
- SUMO는 `1.26.0` 설치 경로를 `SUMO_HOME`에 넣어야 한다.
- `SUMO_HOME`을 바꾼 뒤에는 `PATH`도 같이 다시 잡아야 한다.

## 1. XML 넣기

팀 구글드라이브에 올라온 `XML`은 그 경로를 직접 쓰지 말고, 프로젝트 로컬 경로로 옮겨서 써야 한다.
드라이브 경로를 직접 물고 있으면 팀원 환경마다 꼬이기 쉽다.

아래에서 `$PROJECT_ROOT`는 각자 clone 한 프로젝트 루트다. 예를 들면 `~/js_simulation`이다.

팀 구글드라이브에서 내려받을 기준 폴더는 아래처럼 잡아두면 된다.

- `Google Drive/Shared drives/Smart Crosswalk/top7/generated_signal_7/`

이 폴더 안에는 최소한 다음 파일이 있어야 한다.

- `generated_signal_7.net.xml`
- `final_top7_candidates.csv`
- `final_top7_report_table.csv`
- `final_top7_mapping_audit.csv`
- `top7_baseline_candidates.csv`
- `single_candidates/*.csv`

받아야 할 파일은 `result/active/nets/*.net.xml`이다. 이 파일들은 프로젝트 아래의 같은 경로로 넣는다.

```bash
mkdir -p "$PROJECT_ROOT/result/active/nets"
cp "<TEAM_GOOGLE_DRIVE_XML_DIR>/"*.xml "$PROJECT_ROOT/result/active/nets/"
```

만약 팀 구글드라이브에서 압축 파일로 받았다면, 먼저 풀고 나서 위 경로로 옮긴다.

```bash
tar -xzf active_nets_xml.tgz
mkdir -p "$PROJECT_ROOT/result/active/nets"
cp result/active/nets/*.xml "$PROJECT_ROOT/result/active/nets/"
```

## 2. 들어가야 하는 파일

최소한 아래 파일은 있어야 한다.

- `result/active/nets/current_main_12.net.xml`
- `result/active/nets/signal_fix_9.net.xml`
- `result/active/nets/generated_signal_7.net.xml`
- `result/active/nets/p1_p4_recovery_6.net.xml`

top7 실행만 볼 때는 최소한 아래 조합이면 된다.

- `result/active/nets/generated_signal_7.net.xml`
- `final/top7_sim/manifests/top7_baseline_candidates.csv`
- `final/top7_sim/manifests/single_candidates/*.csv`

## 3. 실행 우선순위

태훈님은 아래 순서로 돌리면 된다. 숫자는 현재 `all_groups` wrapper가 실제로 포함하는 그룹 기준이다.
이 순서는 legacy full 30seed 실행 순서이며, 새 정책의 sequential-light 판단 순서가 아니다.

| 우선순위 | 그룹 | 총 시뮬레이션 수 | 실행 명령 |
|---|---|---:|---|
| 1 | `current_main_12` | 390 | `bash result/active/real_30seed_runs_sampled10/commands/command_to_run_30seed_current_main_12.sh` |
| 2 | `signal_fix_9` | 300 | `bash result/active/real_30seed_runs_sampled10/commands/command_to_run_30seed_signal_fix_9.sh` |
| 3 | `generated_signal_7` | 240 | `bash result/active/real_30seed_runs_sampled10/commands/command_to_run_30seed_generated_signal_7.sh` |

`p1_p4_recovery_6`는 현재 wrapper에서 건너뛴다. 이 그룹은 별도 명령으로만 돌린다.

전체를 순서대로 이어서 돌리려면 아래 하나를 쓰면 된다.

```bash
bash result/active/real_30seed_runs_sampled10/commands/command_to_run_30seed_all_groups.sh
```

## 4. 각 그룹이 실제로 도는 명령

아래 명령은 그룹별 30seed 본실험을 직접 도는 스크립트다.
스크립트 내부에서 `smart_crosswalk_sumo.run_sampled10_group`를 호출한다.

```bash
bash result/active/real_30seed_runs_sampled10/commands/command_to_run_30seed_current_main_12.sh
bash result/active/real_30seed_runs_sampled10/commands/command_to_run_30seed_signal_fix_9.sh
bash result/active/real_30seed_runs_sampled10/commands/command_to_run_30seed_generated_signal_7.sh
bash result/active/real_30seed_runs_sampled10/commands/command_to_run_30seed_p1_p4_recovery_6.sh
```

## 5. 각 그룹 입력

그룹별 입력은 아래처럼 짝이 맞는다.

- `current_main_12` → `result/active/nets/current_main_12.net.xml`
- `signal_fix_9` → `result/active/nets/signal_fix_9.net.xml`
- `generated_signal_7` → `result/active/nets/generated_signal_7.net.xml`
- `p1_p4_recovery_6` → `result/active/nets/p1_p4_recovery_6.net.xml`

후보 CSV는 각 그룹별 `result/active/real_30seed_runs_sampled10/manifests/*.csv`를 사용한다.

새 정책 기준으로는 `paired_significance_analysis.py`의 결과에서 `KEEP`만 다음 seed candidate CSV로 넘긴다.
`PASS`는 qualified pool 유지와 seed 중단, `CUT`은 pool 제외, `RECHECK`는 별도 검토다.

## 6. 확인

```bash
find result/active/nets -maxdepth 1 -type f -name "*.net.xml" | sort
```

위 파일들이 보이면 드라이브에서 받은 `XML`은 제대로 들어간 것이다.

## 7. 기억할 점

- 이 인수인계는 Python 3.11.x와 SUMO 1.26.0 기준이다.
- 다른 Python 또는 SUMO 버전으로는 이 흐름을 기본 지원으로 보지 않는다.
- `result/active/nets/*.net.xml`은 Git에 올리지 않는다.
- `XML`은 드라이브 경로를 직접 쓰지 않고 로컬 프로젝트 경로로 옮겨 둔다.
- 실행이나 환경 설명이 더 필요하면 `docs/01_SETUP.md`와 `docs/02_RUN_GUIDE.md`를 본다.
- smoke 검증에서 `simple_final_pipeline --limit N`은 baseline-first일 수 있으므로 paired comparison 검증용으로 쓰지 않는다.
- paired smoke가 필요하면 baseline 1개와 smart 1개가 함께 생성되는 `run-id`를 지정한다.
