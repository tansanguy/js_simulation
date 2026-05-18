# 정책 기준

이 문서는 왜 이렇게 돌리는지 적는 기준 문서다. 실행 명령보다 정책 정의가 우선한다.

## 스마트 횡단보도 정책

- 이 정책은 보행자 평균 대기시간 감소 장치가 아니다.
- `baseline`은 기존 신호 상태이며 `extension_count = 0`이어야 한다.
- `smart`는 보행자 녹색 종료 시점에 아직 횡단을 끝내지 못한 보행자가 있으면 보행자 신호를 연장한다.
- 연장은 `extension_increment = 5s`, `max_extensions = 1`이다.
- extension은 `smart`에서만 발생할 수 있다.
- `extension_count`는 효과 metric이 아니라 policy exposure 및 trigger sanity check다.
- SSM은 삭제하지 않는다. 반복/light 실행에서는 off로 두고, final safety 검증에서는 on으로 사용할 수 있게 유지한다.

## Primary Endpoint

- primary endpoint는 `pedestrian_clearance_failure_delta`, `unfinished_crossing_delta`, low-PET risk proxy 중심이다.
- `pedestrian_wait_delta`는 secondary metric이다.
- 새 결과에는 `pedestrian_clearance_failure_count`, `unfinished_crossing_count`가 있어야 한다.
- 기존 결과에 clearance/unfinished 컬럼이 없으면 low-PET 계열 proxy로 fallback할 수 있다.
- clearance 컬럼이 있어도 값이 모두 비어 있으면 다음 안전 proxy로 fallback해야 한다.

## Paired Comparison

- `baseline`과 `smart`는 같은 seed, 같은 route, 같은 demand 조건에서 비교해야 한다.
- 비교 단위는 seed별 `smart - baseline` paired delta다.
- route/demand hash가 불일치하거나 hash 검증이 비어 있으면 RECHECK 대상이다.
- missing pair, failed case, duplicate scenario row, baseline extension 발생, smart max extension 위반은 자동 결론이 아니라 RECHECK 대상이다.

## Cut-off

- cut-off는 통계적 결론이 아니라 계산 시간을 줄이기 위한 sequential screening이다.
- n=5에서는 active 후보 중 하위 10%를 CUT한다.
- n=10에서는 남은 active 후보 중 하위 20%를 CUT한다.
- CUT은 후보 pool에서 제외한다는 뜻이다.
- data quality fail, missing pair, route/demand mismatch, baseline extension 발생, smart extension 정책 위반은 비율과 무관하게 RECHECK다.
- n=5까지 smart extension이 0회인 후보, safety 개선이 없는 후보, traffic cost가 과한 후보는 hard cut 대상이 될 수 있다.

## Graduation

- PASS는 후보 제거가 아니다.
- PASS는 qualified 후보 pool에 유지하되 이후 seed 추가 실행을 중단한다는 뜻이다.
- n=5와 n=10은 cut-off round이며, 원칙적으로 graduation round가 아니다.
- graduation round는 n=12, 15, 18, 20, 21~30이다.
- n=21부터 n=30까지는 매 seed마다 graduation 가능 여부를 확인한다.
- PASS 조건에는 primary safety improvement, direction consistency, bootstrap CI, paired test boundary, traffic gate, extension policy sanity가 함께 들어가야 한다.

## 상태 정의

- `PASS`: 졸업. qualified 후보 pool에 유지하고 추가 seed 실행을 중단한다.
- `CUT`: 후보 pool에서 제외한다.
- `KEEP`: 다음 seed 또는 다음 checkpoint로 계속 실행한다.
- `RECHECK`: 데이터/구현 확인 필요. 자동 탈락이 아니다.
- 다음 seed 실행 candidate CSV에는 `KEEP` 후보만 넘긴다.
- `PASS`, `CUT`은 다음 실행 대상에서 제외한다.
- `RECHECK`는 별도 검토 대상으로 분리한다.

## 실행 Profile

- sequential/light 실험은 `sim_duration = 540s`를 사용한다.
- 기존 full/source-of-truth 설정과 혼동하지 않는다.
- 반복 실행에서는 `debug_trace`, `extension_trigger_debug`, `signal_phase_audit` 같은 대용량 debug 출력을 끈다.
- group 실행에서는 seed당 baseline group run 1회와 smart group run 구조를 유지한다.
- 기존 single-candidate loop는 sequential/light 경로에 섞지 않는다.

## 최종 설치 후보 해석

- 최종 설치 7개는 시뮬레이션에서 후보를 7개만 남긴다는 뜻이 아니다.
- 시뮬레이션은 qualified 후보 pool을 만들고, 최종 7개는 그 pool에서 후속 의사결정으로 선택한다.
