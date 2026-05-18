# 정책 기준

이 문서는 왜 이렇게 돌리는지 적는 문서다. 실행 순서보다 정책이 먼저다.

## baseline

- 기존 신호 상태다.
- `extension_count = 0`이어야 한다.

## smart

- 보행자 녹색을 `+5s` 연장한다.
- 한 cycle당 최대 1회다.
- 차량 green compensation은 쓰지 않는다.

## 차량 수요

- 시나리오 이름은 `main_realistic_stress`다.
- 총량은 `20,877 vph`다.
- `600s` 기준 `3,480`대다.
- 차량 타입은 `passenger`만 쓴다.
- `baseline`과 `smart`는 같은 vehicle route를 공유해야 한다.
- `ROAD_RANK`는 실제 route bucket이 아니다.
- road-group 값은 정책 메타데이터다.

## 보행자 수요

- 시나리오는 `daytime_high_1p2`다.
- S-DoT 관측값을 우선 쓴다.
- 결측은 생활인구 기반 보정으로 메운다.
- `final_pedestrian_600s = round(base_pedestrian_600s * 1.2)`다.

동 단위 고령자 비율은 현재 정책 값으로 아래를 쓴다.

- 회현동: `0.2947`
- 청구동: `0.2478`
- 명동: `0.2358`
- 소공동: `0.0977`

보행 속도는 아래다.

- 일반 보행자: `1.00 m/s`
- 고령 보행자: `0.85 m/s`
- 느린 고령 보행자: `0.73 m/s`

## smoke vs final

- `smoke`: 120초, seed 1개, 검증용
- `final`: 600초, seed 30개, 최종 결과용

## 결과 기준

- `outputs/final/aggregate/final_summary.csv`가 최종 source-of-truth다.
- `smoke`는 실행 유효성 확인만 한다.
- coverage는 broad distribution 검증이다. 모든 edge에 균일 분포를 뜻하지는 않는다.
