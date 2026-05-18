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
