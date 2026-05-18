# 문제 해결

막히면 먼저 `outputs/*/logs`를 본다. 그다음 `audit`를 본다.

## verify 실패

```bash
bash commands/verify.sh
```

체크 포인트:

- `SUMO_HOME` 설정
- `result/active/nets/*.net.xml`
- `result/active/pedestrian_assumption/*.csv`
- `result/active/real_30seed_runs_sampled10/manifests/run_manifest.csv`

## `SUMO_HOME` 문제

증상:

- `verify.sh` 실패
- `randomTrips.py` 경로 오류
- TraCI 초기화 실패

대응:

`SUMO_HOME`은 `SUMO 1.26.0` 설치 경로를 가리켜야 한다.

```bash
export SUMO_HOME="/path/to/your/SUMO_HOME"
export PATH="$SUMO_HOME/bin:$PATH"
```

이 설정은 터미널을 새로 열 때마다 다시 해줘야 한다.

## `pedestrian_crossing_count = 0`

자주 보는 원인:

- smoke duration이 너무 짧음
- phase-aligned depart가 시뮬레이션 밖으로 밀림
- crossing edge / detector 매핑이 안 맞음

확인:

- `pedestrian_depart_plan.csv`
- `demand_pedestrian.rou.xml`
- `simulation_result.csv`

## `vehicle_route_count`가 너무 작음

증상:

- `vehicle_route_count = 1` 또는 아주 작은 값
- `unique_vehicle_route_count`도 같이 작음
- `duplicate_factor`가 비정상적으로 큼

확인:

- `global_vehicle_routes/demand_vehicle.rou.xml`
- `unique_vehicle_route_count`
- `duplicate_factor`

## PET unavailable

확인:

- `pet_available`
- `pet_unavailable_reason`
- `low_pet_per_100_crossings`

`PET unavailable`은 보통 횡단 수가 없거나 PET 커버리지가 부족할 때 나온다.

## paired analysis가 RECHECK를 냄

`RECHECK`는 자동 탈락이 아니다. 데이터 또는 구현 확인이 필요하다는 뜻이다.

자주 보는 원인:

- baseline-only 또는 smart-only 입력만 들어감
- baseline/smart pair 중 하나가 없음
- `vehicle_route_sha256` 또는 `pedestrian_route_sha256` 불일치
- route hash가 비어 있어 공유 조건을 검증할 수 없음
- baseline에서 `extension_count > 0`
- smart에서 seed당 extension이 1회를 초과
- `failed_cases.csv`에 에러가 있음

확인:

```bash
python3 -m smart_crosswalk_sumo.paired_significance_analysis \
  --run-glob "outputs/policy_smoke/smoke/runs/*/baseline/seed01" \
  --run-glob "outputs/policy_smoke/smoke/runs/*/smart/*/seed01" \
  --candidate-csv result/active/real_30seed_runs_sampled10/manifests/current_main_12_candidates.csv \
  --output /private/tmp/phase6_policy_check/sequential_summary.csv \
  --recheck-output /private/tmp/phase6_policy_check/recheck_candidates.csv
```

`sequential_summary.csv`의 `quality_reasons`, `stop_reason`, `primary_metric_source`를 먼저 본다.

## n=5에서 extension 0회 CUT

n=5까지 smart extension이 한 번도 발생하지 않으면 해당 후보는 policy exposure가 없으므로 hard cut될 수 있다.
이때 `extension_count`는 효과 metric이 아니라 trigger sanity check다.

확인:

```bash
python3 -c "import pandas as pd; df=pd.read_csv('/private/tmp/phase6_policy_check/sequential_summary.csv'); print(df[['candidate_id','checkpoint','smart_extension_count_total','extension_trigger_rate','pass_cut_keep_recheck','stop_reason']].tail(30).to_string(index=False))"
```

## wait metric이 나빠졌는데 KEEP/PASS로 보임

정상일 수 있다. `pedestrian_wait_delta`는 secondary metric이고, primary는 clearance failure / unfinished crossing / low-PET risk proxy다.
보행자 대기시간이 악화돼도 primary safety proxy와 traffic gate가 정책 기준을 만족하면 KEEP 또는 PASS가 가능하다.

## light 실행인데 debug 파일이 너무 큼

sequential/light 반복 실행에는 `--output-profile light`를 사용한다.
이 profile에서는 `debug_trace`, `extension_trigger_debug`, `signal_phase_audit` 같은 대용량 출력이 꺼져야 한다.
SSM은 삭제하지 않고 final safety 검증 profile에서 켤 수 있게 유지한다.

## 다음 seed 후보가 너무 많거나 적음

다음 실행에는 `keep_candidates.csv`만 넘긴다.
`pass_candidates.csv`는 qualified pool 보존용이고, `cut_candidates.csv`, `recheck_candidates.csv`는 실행 입력이 아니다.

확인:

```bash
wc -l /private/tmp/phase6_policy_check/keep_candidates.csv
head -5 /private/tmp/phase6_policy_check/keep_candidates.csv
```

## aggregate_final crash

먼저 이것을 본다.

```bash
cat outputs/final/audit/preflight_checks.csv
```

흔한 원인:

- 입력 파일 누락
- `SUMO_HOME` 없음
- run 결과 폴더에 CSV가 너무 많이 섞임

## run skip됨

같은 `output_dir`에 성공 run이 있으면 다시 안 돈다.

대응:

- 다른 `--outputs-root`를 쓴다.
- 필요하면 대상 run만 새 경로로 분리한다.

## 로그 검색

```bash
find outputs/smoke/logs -type f | sort | tail -20
grep -R "Traceback\|TraCIException\|ERROR\|FAILED\|failed" outputs/smoke/logs | tail -80
grep -R "Traceback\|TraCIException\|ERROR\|FAILED\|failed" outputs/final/logs | tail -80
```

## 보내야 할 것

막히면 아래 3개만 있으면 된다.

1. `commands/verify.sh` 출력
2. 해당 run 로그 파일
3. `outputs/*/audit/preflight_checks.csv`
