# 서울 중구 스마트 횡단보도 SUMO 모델

이 파이프라인은 미래 교통량을 정확히 예측하는 모델이 아니라, 동일 조건에서 후보 횡단보도별 `baseline`과 `smart`를 비교하는 정책 시나리오 실험 모델이다.

핵심 목적은 다음 4가지다.

- 후보 횡단보도별 baseline/smart 전후 비교
- 보행 녹색 5초 단일 연장이 보행 안전과 차량 지체에 미치는 상대적 효과 평가
- 차량 지체를 교통비용으로 변환한 trade-off 확인
- 후보 선정 이후의 정량 평가와 비용-효과 분석 보조

이 파이프라인은 전체 횡단보도 자동 선정, 실제 사고 확정 예측, 모든 중구 횡단보도 최적화를 목표로 하지 않는다.

## 현재 상태

- 테스트 실행 단계
- SUMO 시각화 일부 테스트 완료
- 실제 최종 결과는 아직 없음
- 서버 또는 실행 환경 연결 시도 중

## 현재 기본 모델

- 신호는 기본적으로 고정 신호다.
- `smart` 시나리오는 보행 감지 시 보행 녹색을 `5초`만 `1회` 연장한다.
- 연장 조건은 보행 녹색 phase, 잔여시간 임계값 이하, crossing/detector 보행자 존재, 같은 cycle 미연장, false negative 미발생이다.
- 이벤트 기반 용량 감소는 `accident`, `bus_stop` 두 종류만 반영한다.
- baseline과 smart는 같은 seed에서 같은 이벤트 스케줄을 공유한다.
- 네트워크 기본 모드는 `expanded`이며, 중구 + 인접 도로를 포함하도록 `buffer_m=1000`을 사용한다.
- 제어 대상은 전체 횡단보도가 아니라 사전에 선정된 후보 횡단보도다.

## 실행 흐름

```text
preprocess.py       -> 후보 횡단보도 입력 정리
build_networks.py   -> local / expanded 네트워크 생성
generate_demand.py  -> 차량·보행 수요 생성
run_simulations.py  -> baseline / smart 개별 실행 + event penalty 반영
collect_metrics.py  -> seed별 원시 지표 수집
generate_reports.py -> 최종 요약 CSV + tradeoff 그림 생성
main.py             -> 전체 실행
```

## 기본 실행

```bash
python3 smart_crosswalk_sumo/main.py \
  --top_n 20 \
  --seeds 42 43 44 \
  --sim_duration 1800 \
  --warmup 300 \
  --network_mode expanded \
  --buffer_m 1000
```

또는 루트에서:

```bash
python3 run_pipeline.py --top_n 20 --seeds 42 43 44
```

## 주요 옵션

- `--network_mode expanded|local`
- `--buffer_m 1000`
- `--smart_extension_sec 5`
- `--max_extensions 1`
- `--incident_scenario normal_urban`
- `--model_assumptions smart_crosswalk_sumo/config/model_assumptions.yaml`

## 최종 보고용 핵심 출력

- `outputs/simulation_summary.csv`
- `outputs/baseline_vs_smart_summary.csv`
- `outputs/model_assumptions_used.csv`
- `figures/tradeoff_summary.png`

## 선택 디버그 출력

- `outputs/debug_extension_events.csv`에 해당하는 기존 `extension_events_seed.csv`
- `outputs/debug_incident_events.csv`에 해당하는 기존 `incident_events_seed.csv`
- SUMO snapshot / FCD / lane-edge debug 산출물

최종 보고에서는 핵심 출력 4개만 사용하고, 디버그 출력은 개발용 또는 부록용으로 분리한다.
