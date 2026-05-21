# 스코어 선정 변수 설명 (final_score.csv)

## 개요

각 횡단보도 1행. baseline_avg → smart_avg 개선율 기준으로 두 가지 스코어를 산출.

- **개선율** = `(baseline - smart) / |baseline| × 100` (낮을수록 좋은 변수 기준)
- `pet_mean`은 높을수록 좋으므로 `(smart - baseline) / |baseline| × 100`
- 양수 = 스마트 신호가 개선, 음수 = 스마트 신호가 악화

---

## 식별 열

| 변수명 | 설명 |
|--------|------|
| `group` | 실험 그룹 |
| `crosswalk_id` | 횡단보도 식별자 |

---

## 차량 지체 변수 (Score 1 구성)

> 낮을수록 좋음 → 개선율 양수 = 지체 감소

| 변수명 | 설명 |
|--------|------|
| `veh_delay_mean_baseline` | baseline 차량 평균 지체 (초) |
| `veh_delay_mean_smart` | smart 차량 평균 지체 (초) |
| `veh_delay_mean_impr` | 차량 평균 지체 개선율 (%) |
| `local_500m_avg_delay_sec_baseline` | baseline 500m 권역 차량 평균 지체 (초) |
| `local_500m_avg_delay_sec_smart` | smart 500m 권역 차량 평균 지체 (초) |
| `local_500m_avg_delay_sec_impr` | 500m 권역 차량 평균 지체 개선율 (%) |
| `surrounding_road_delay_sec_baseline` | baseline 주변 도로 차량 평균 지체 (초) |
| `surrounding_road_delay_sec_smart` | smart 주변 도로 차량 평균 지체 (초) |
| `surrounding_road_delay_sec_impr` | 주변 도로 지체 개선율 (%) |
| `local_500m_queue_proxy_baseline` | baseline 500m 권역 대기열 프록시 |
| `local_500m_queue_proxy_smart` | smart 500m 권역 대기열 프록시 |
| `local_500m_queue_proxy_impr` | 대기열 프록시 개선율 (%) |

---

## 안전 변수 (Score 2 구성)

> 안전 위험 지표는 낮을수록 좋음, pet_mean은 높을수록 좋음

| 변수명 | 설명 |
|--------|------|
| `safety_risk_score_baseline` | baseline 안전 위험 점수 |
| `safety_risk_score_smart` | smart 안전 위험 점수 |
| `safety_risk_score_impr` | 안전 위험 점수 개선율 (%) — 양수 = 위험 감소 |
| `very_risky_crossing_count_baseline` | baseline 매우 위험한 횡단 횟수 |
| `very_risky_crossing_count_smart` | smart 매우 위험한 횡단 횟수 |
| `very_risky_crossing_count_impr` | 매우 위험한 횡단 개선율 (%) |
| `accident_risk_estimate_baseline` | baseline 사고 위험 추정값 |
| `accident_risk_estimate_smart` | smart 사고 위험 추정값 |
| `accident_risk_estimate_impr` | 사고 위험 추정 개선율 (%) |
| `pet_mean_baseline` | baseline PET 평균 (초) |
| `pet_mean_smart` | smart PET 평균 (초) |
| `pet_mean_impr` | PET 평균 개선율 (%) — 양수 = 보차 간격 증가 (안전 향상) |

---

## 스코어

| 변수명 | 설명 |
|--------|------|
| `score1_delay_impr` | **차량 지체 스코어** — 지체 4개 변수 개선율 단순 평균 (%). 높을수록 스마트 신호가 차량 지체를 줄임 |
| `score2_tradeoff` | **Tradeoff 스코어** — 안전 개선율 60% + 지체 개선율 40% 가중 평균 (%). 높을수록 안전이 향상되면서 지체도 크게 늘지 않음 |
| `rank_score1` | Score 1 순위 (1위 = 지체 가장 많이 개선) |
| `rank_score2` | Score 2 순위 (1위 = tradeoff 가장 우수) |

---

## 스코어 산출 공식

```
Score 1 = mean(veh_delay_mean_impr,
               local_500m_avg_delay_sec_impr,
               surrounding_road_delay_sec_impr,
               local_500m_queue_proxy_impr)

Score 2 = 0.6 × mean(safety_risk_score_impr,
                      very_risky_crossing_count_impr,
                      accident_risk_estimate_impr,
                      pet_mean_impr)
        + 0.4 × Score 1
```
