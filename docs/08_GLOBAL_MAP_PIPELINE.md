# 전역 맵 시뮬레이션 파이프라인

`smart_crosswalk_sumo/run_global_map_pipeline.py`

---

## 개요

enhanced smoke 로직을 기반으로, **네트워크 전체 edge에 대한 공간적 측정**을 추가한 10분짜리 파이프라인이다.

| 구분 | enhanced smoke | **global map pipeline** |
|------|---------------|------------------------|
| 기본 시뮬레이션 시간 | 120s | **600s (10분)** |
| 횡단보도 수 | CSV 전체 | **최대 7개 (--max-crosswalks)** |
| 측정 범위 | 횡단보도 주변 500m 로컬 | 로컬 + **전체 네트워크 edge** |
| 전역 edge 출력 | 없음 | **global_edge_metrics.csv** 등 |
| 용도 | 단기 검증 | 맵 전체 영향 분석 |

---

## 빠른 시작

```bash
# 기본 실행 (600s, seed 1개)
python -m smart_crosswalk_sumo.run_global_map_pipeline \
    --candidate-csv result/active/real_30seed_runs_sampled10/manifests/current_main_12_candidates.csv \
    --net-file result/active/real_30seed_runs_sampled10/nets/current_main_12.net.xml \
    --output-dir outputs/global_map/run1 \
    --seed -1

# 3개 seed, 횡단보도 7개
python -m smart_crosswalk_sumo.run_global_map_pipeline \
    --candidate-csv ... \
    --net-file ... \
    --output-dir outputs/global_map/run1 \
    --seeds 1 2 3 \
    --max-crosswalks 7

# 단일 횡단보도 검증 (파이프라인 정상 확인용)
python -m smart_crosswalk_sumo.run_global_map_pipeline \
    --candidate-csv ... \
    --net-file ... \
    --output-dir outputs/global_map/test \
    --seed 1 \
    --crosswalk-id NODE_10335
```

---

## 인수 목록

### 필수

| 인수 | 설명 |
|------|------|
| `--candidate-csv PATH` | 횡단보도 후보 CSV |
| `--net-file PATH` | SUMO .net.xml |
| `--output-dir PATH` | 결과 저장 루트 |

### 시드

| 인수 | 기본값 | 설명 |
|------|--------|------|
| `--seed INT` | `-1` | `-1`이면 랜덤 시드 자동 선택 |
| `--seeds INT [INT ...]` | — | 명시 시드 목록. `--seeds 1 2 3` |

### 시뮬레이션

| 인수 | 기본값 | 설명 |
|------|--------|------|
| `--sim-duration INT` | `600` | 시뮬레이션 시간(초). 10분 = 600 |
| `--step-length FLOAT` | `0.1` | SUMO step 단위(초) |
| `--warmup-sec INT` | `0` | warmup 구간(초). 차량 pre-loading에만 사용 |

### 횡단보도

| 인수 | 기본값 | 설명 |
|------|--------|------|
| `--max-crosswalks INT` | `7` | 후보 CSV에서 선택할 최대 횡단보도 수. `risk_score` 상위 N개 선택. 컬럼 없으면 순서 기준 |
| `--crosswalk-id STR` | — | 단일 횡단보도 검증 모드 |

### 전역 맵

| 인수 | 기본값 | 설명 |
|------|--------|------|
| `--global-map-interval FLOAT` | `10.0` | 전체 edge 샘플링 간격(초). 낮을수록 상세하나 느림 |
| `--traffic-watch-interval FLOAT` | `1.0` | 로컬 500m 트래픽 집계 간격(초) |

### 시나리오

| 인수 | 기본값 | 설명 |
|------|--------|------|
| `--scenario-mode` | `both` | `both` / `baseline_only` / `smart_only` |
| `--ped-count INT` | `20` | 횡단보도당 보행자 수 |
| `--elderly-ratio FLOAT` | `0.2` | 노인 비율 (0~1) |
| `--ped-repeat-spacing-sec FLOAT` | `2.0` | 보행자 반복 출발 간격(초) |

### 보행자 수요 스케일링

| 인수 | 기본값 | 설명 |
|------|--------|------|
| `--ped-demand-scale-mode` | `none` | `none` \| `source_min_to_target` |
| `--min-ped-count-target INT` | `30` | 최저 수요 목표 |
| `--max-ped-count-cap INT` | `200` | scaled_ped_count 상한 |
| `--ped-demand-source STR` | — | pedestrian.csv 경로 (레이블 전용) |

### 캐시

| 인수 | 기본값 | 설명 |
|------|--------|------|
| `--vehicle-route-cache-dir PATH` | — | 차량 경로 캐시 디렉토리. 여러 run 간 공유 가능 |

---

## 출력 구조

```
{output-dir}/
├── baseline/
│   └── seed{N}/
│       ├── csv/
│       │   ├── simulation_result.csv        # 횡단보도별 결과 (로컬 지표)
│       │   ├── global_edge_metrics.csv      # ★ edge별 평균 speed/flow/halting
│       │   └── global_network_timeseries.csv # ★ 시간축 전체 네트워크 집계
│       └── log/
│           └── run_metadata.json
├── smart/
│   └── seed{N}/
│       └── csv/  (동일 구조)
├── global_edge_comparison/
│   └── seed{N}_global_edge_comparison.csv  # ★ baseline vs smart edge-level delta
├── global_edge_comparison_agg.csv          # ★ 전 seed 평균 delta
├── global_simulation_result.csv            # 전체 횡단보도/시드 결과 통합
└── _vehicle_routes/
    └── seed{N}/                            # 차량 경로 캐시
```

---

## 출력 파일 상세

### `simulation_result.csv`

enhanced smoke의 `simulation_result.csv`와 동일 구조. 주요 컬럼:

| 컬럼 | 설명 |
|------|------|
| `crosswalk_id` | 횡단보도 ID |
| `scenario` | `baseline` / `smart` |
| `seed` | 시드 |
| `pedestrian_crossing_count` | 실제 횡단 보행자 수 |
| `ped_wait_time_mean` | 평균 보행자 대기시간(초) |
| `extension_count` | 신호 연장 발동 횟수 |
| `avg_queue_length` | 평균 대기열 길이 |
| `network_mean_travel_time` | 전체 네트워크 평균 통행시간 |
| `network_mean_time_loss` | 전체 네트워크 평균 time loss |
| `safety_risk_score` | 안전 위험 점수 (PET 기반) |
| `global_map_interval_sec` | 전역 샘플링 간격 (메타) |

---

### `global_edge_metrics.csv` ★

전체 passenger edge 별 측정값. **이 파이프라인의 핵심 출력.**

| 컬럼 | 설명 |
|------|------|
| `edge_id` | SUMO edge ID |
| `scenario` | `baseline` / `smart` |
| `seed` | 시드 |
| `sample_count` | 샘플 횟수 (`sim_duration / interval`) |
| `mean_speed` | 평균 속도 (m/s) |
| `min_speed` | 최저 속도 (m/s) |
| `max_speed` | 최고 속도 (m/s) |
| `mean_flow` | 평균 재차 차량 수 |
| `max_flow` | 최대 재차 차량 수 |
| `mean_halting` | 평균 정지 차량 수 |
| `max_halting` | 최대 정지 차량 수 |
| `mean_occupancy` | 평균 점유율 (%) |

> **샘플 수** = `floor((sim_duration - warmup_sec) / global_map_interval)`. 600s / 10s = 60회.

---

### `global_network_timeseries.csv` ★

시간축 전체 네트워크 집계. 시간대별 혼잡도 확인용.

| 컬럼 | 설명 |
|------|------|
| `t` | SUMO 절대 시간(초) |
| `policy_t` | 정책 시간 (`t - warmup_sec`) |
| `scenario` | `baseline` / `smart` |
| `total_active_vehicles` | 해당 시점 전체 재차 차량 수 |
| `total_halting_vehicles` | 해당 시점 전체 정지 차량 수 |
| `network_mean_speed` | 해당 시점 전체 edge 평균 속도 (m/s) |
| `sampled_edge_count` | 샘플링된 edge 수 |

---

### `global_edge_comparison.csv` ★

baseline vs smart의 **edge 단위 delta**. 스마트 횡단보도가 교통 흐름에 미친 영향을 공간적으로 확인.

| 컬럼 | 설명 |
|------|------|
| `edge_id` | SUMO edge ID |
| `mean_speed_baseline` | baseline 평균 속도 |
| `mean_speed_smart` | smart 평균 속도 |
| `delta_mean_speed` | `smart - baseline` 속도 차이 |
| `pct_delta_mean_speed` | 속도 변화율 (%) |
| `delta_mean_flow` | 유량 변화 |
| `delta_mean_halting` | 정지 차량 수 변화 |
| `delta_mean_occupancy` | 점유율 변화 |

> `delta > 0` = smart 시나리오에서 해당 지표 증가.
> `delta_mean_speed > 0` = smart에서 더 빠름 (긍정적).
> `delta_mean_halting < 0` = smart에서 정지 차량 감소 (긍정적).

---

### `global_edge_comparison_agg.csv`

전 seed에 걸친 `global_edge_comparison`의 edge별 평균. 안정적인 비교가 필요할 때 사용.

---

## enhanced smoke와 비교

| 항목 | enhanced smoke | global map pipeline |
|------|---------------|---------------------|
| 목적 | 횡단보도별 빠른 검증 | 네트워크 전체 영향 분석 |
| 시뮬레이션 시간 | 120s | 600s |
| 횡단보도 수 | 제한 없음 | 기본 7개 |
| 로컬 500m 측정 | ✅ | ✅ |
| 전역 edge 측정 | ❌ | ✅ |
| 시간축 네트워크 집계 | ❌ | ✅ |
| edge-level delta 비교 | ❌ | ✅ |
| diagnostic PET 모드 | targeted / randomized | ❌ (미지원) |
| trace CSV (TLS/lane) | ✅ | ❌ (성능 우선) |

---

## 성능 참고

- edge 수 × 샘플 횟수 = 데이터 포인트. 네트워크 500 edge, 600s / 10s 간격 = **30,000 포인트/시나리오**.
- `--global-map-interval`을 높이면(예: 30s) step loop 부하 감소.
- `--traffic-watch-interval 1.0`(기본)은 enhanced smoke `0.1`보다 넉넉하다. 정밀도 필요 시 `0.1`로 낮춤.
- 차량 경로는 `--vehicle-route-cache-dir`로 여러 run 간 재사용 가능.

---

## 결과 해석 예시

### 교통 영향 없는 이상적 케이스
```
delta_mean_speed   ≈ 0   (스마트 신호가 전체 흐름에 영향 없음)
delta_mean_halting < 0   (횡단보도 인근 정지 소폭 감소)
extension_count   >= 1   (smart 시나리오에서 신호 연장 발동됨)
```

### 신호 연장이 주변 도로에 영향을 준 케이스
```
delta_mean_speed < 0  (신호 연장으로 인근 차량 속도 감소)
delta_mean_halting > 0 (대기 차량 증가)
→ 해당 edge_id 확인 후 TLS 파라미터 검토
```

---

## 관련 파일

| 파일 | 역할 |
|------|------|
| `smart_crosswalk_sumo/run_enhanced_smoke_group.py` | 이 파이프라인의 기반. 로컬 측정 로직 공유 |
| `smart_crosswalk_sumo/run_phase6_recovery_smoke.py` | traci 유틸리티, PET 계산 함수 |
| `smart_crosswalk_sumo/vehicle_demand_policy.py` | 차량 수요 정책 상수 |
| `crosswalk_stepwise_result_50m.csv` | lanes, elderly_ratio join 소스 |
| `docs/04_PIPELINE_POLICY.md` | 결과 판정 기준 (PASS/CUT/KEEP) |
