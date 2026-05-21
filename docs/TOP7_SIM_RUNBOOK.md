# Top7 스마트 횡단보도 시뮬레이션 실행 가이드

**작성일**: 2026-05-21  
**대상**: 태훈님 맥미니  
**목적**: 서울 중구 top7 후보 횡단보도에 대해 baseline / smart 각 30 seed paired 시뮬레이션 실행

---

## 0. 개요

### 실험 구조

```
generated_signal_7.net.xml (중구 전체 도로망)
  │
  ├─ baseline 시뮬레이션 × 30 seed
  │    └─ top7 후보 모두 일반 신호 운영 (smart extension 없음)
  │
  └─ smart 시뮬레이션 × 30 seed × 7 후보
       └─ 후보별로 해당 crossing에만 smart extension 적용
          (보행자 감지 시 신호 최대 3회, 회당 5초 연장)
```

### 측정 지표

| 범위 | 지표 |
|------|------|
| **중구 전체 (network-wide)** | `network_avg_delay_sec` (평균 대기시간), `network_mean_travel_time`, `network_arrived_vehicles`, `network_teleport_count` |
| **횡단보도 반경 500m (local)** | `local_500m_avg_delay_sec`, `local_500m_vehicle_count`, `local_500m_mean_speed` |
| **횡단보도 개별** | `extension_count`, `total_extension_sec`, `ped_wait_time_mean`, `pedestrian_crossing_count`, `pet_event_count` |

> **전역 교통흐름 측정 방식**: SUMO가 자동 생성하는 `statistics.xml`과 `tripinfo.xml`을 파싱.
> `global_traffic_flow.csv`에는 전체 SUMO 통계와 도착 완료 차량의 `duration` / `timeLoss` / `waitingTime` 집계가 함께 저장된다.
> → 중구 전체 도로망에서 smart crosswalk 설치의 교통 영향을 정량화.

---

## 1. 최종 top7 후보

| 순위 | crosswalk_id | 동명 | crossing_edge_id | ped_link_index | 매핑 방식 |
|------|-------------|------|-------------------|---------------|---------|
| 1 | **LINK_194891** | 회현동2가 | `:11203052965_c0` | 11 | READY |
| 2 | **NODE_10262** | 남대문로3가 | `:11203041635_c0` | 4 | READY |
| 3 | **NODE_125895** | 신당동 | `:1936511299_c1` | 12 | OVERRIDE (인접 TLS) |
| 4 | **NODE_5831** | 남대문로5가 | `:5161705573_c0` | 2 | OVERRIDE (dedup) |
| 5 | **LINK_239754** | 남대문로5가 | `:5593950698_c0` | 1 | READY |
| 6 | **NODE_5846** | 남대문로5가 | `:7195225083_c0` | 0 | OVERRIDE (보행자경로우회) |
| 7 | **NODE_8369** | 신당동 | `:1936511299_c0` | 11 | OVERRIDE (dedup) |

> **주의 — 같은 교차로 군집**:  
> - NODE_5831 / LINK_239754 / NODE_5846 → 남대문로5가 동일 교차로 군집 (군집2, 36m 이내)  
> - NODE_125895 / NODE_8369 → 신당동 동일 교차로 군집 (군집4, 119m 이내)  
> - 각각 다른 `crossing_edge_id`에 배정되어 있으므로 동시 실행 가능

### 현재 구현된 산출물

최종 top7 관련 산출물은 아래 경로에 생성된다.

- `final/top7/`: top7 선정 결과, 매핑 감사표, 지도 산출물
- `final/top7_sim/`: smoke / 30seed 실행 스크립트, 실행 결과, 후보 CSV

대표 파일:

- `final/top7/final_top7_candidates.csv`
- `final/top7/final_top7_report_table.csv`
- `final/top7/final_top7_mapping_audit.csv`
- `final/top7/final_top7_map.geojson`
- `final/top7_sim/manifests/top7_baseline_candidates.csv`
- `final/top7_sim/manifests/single_candidates/<crosswalk_id>.csv`

### 구글드라이브 수령 경로

팀 구글드라이브에서 파일을 받을 때는 아래 폴더를 기준 경로로 사용한다.

- `Google Drive/Shared drives/Smart Crosswalk/top7/generated_signal_7/`

이 폴더에서 내려받은 뒤, 로컬에서는 다음 위치로 맞춘다.

- `generated_signal_7.net.xml` → `result/active/nets/generated_signal_7.net.xml`
- `top7_baseline_candidates.csv` → `final/top7_sim/manifests/top7_baseline_candidates.csv`
- `single_candidates/*.csv` → `final/top7_sim/manifests/single_candidates/`
- `final_top7_candidates.csv` → `final/top7/final_top7_candidates.csv`
- `final_top7_report_table.csv` → `final/top7/final_top7_report_table.csv`
- `final_top7_mapping_audit.csv` → `final/top7/final_top7_mapping_audit.csv`
- `final_top7_map.geojson` → `final/top7/final_top7_map.geojson`

드라이브에만 있고 로컬에 아직 없다면, 아래처럼 폴더를 열어서 수동 복사해도 된다.

```bash
open "$PROJECT_ROOT/result/active/nets"
open "$PROJECT_ROOT/final/top7_sim/manifests"
open "$PROJECT_ROOT/final/top7"
```

---

## 2. 시뮬레이션 파라미터

```
net-file        result/active/nets/generated_signal_7.net.xml
sim-duration    1800 s   (30분, 전역 교통 수렴 위해 기존 600s에서 연장)
warmup          300 s    (네트워크 warm-up, 통계 수집에서 제외)
seed            1 ~ 30
traci_step      0.1 s
extension_inc   5.0 s    (신호 1회 연장량)
max_extensions  3        (보행 신호 1사이클당 최대 연장 횟수)
radius_m        500.0    (local 교통 측정 반경)
ped-repeat      5 회     (보행자 1명 반복 횟수)
ped-spacing     1.5 s    (반복 간격)
SSM             ON       (PET 관측 활성화) ← 디폴트 OFF, 반드시 --enable-ssm 명시
include-vehicles ON
output-profile  full (30seed 본 실행) / light (smoke 검증)
```

> **SSM 주의**: `run_sampled10_group`의 SSM 디폴트는 **OFF**이다.  
> `--enable-ssm` 플래그를 명시해야 PET 이벤트가 기록된다. `--disable-ssm`은 명시적으로 끌 때 사용.

---

## 3. 디렉토리 구조

```
js/                                         ← 프로젝트 루트 (여기서 모든 명령 실행)
├── result/active/nets/
│   └── generated_signal_7.net.xml          ← SUMO 네트워크 파일 (수정 금지)
│
├── analysis/
│   └── make_top7_sim_candidates.py         ← candidate CSV 생성 + preflight 검증
│
└── final/top7_sim/
    ├── manifests/
    │   ├── top7_baseline_candidates.csv    ← baseline 실행용 (top7 × 1 파일)
    │   ├── top7_mapping_resolution.csv     ← 매핑 이력 (왜 이 crossing인지 기록)
    │   └── single_candidates/
    │       ├── LINK_194891.csv             ← 후보별 smart 실행용 CSV
    │       ├── NODE_10262.csv
    │       └── ... (7개)
    │
    ├── commands/
    │   ├── smoke_top7_seed1.sh             ← [1단계] seed1 smoke 검증
    │   ├── smoke_multi_smart_seed1.sh      ← [선택] 동시 multi-smart 검증
    │   ├── run_top7_30seed.sh              ← [2단계] 30seed 본 실행
    │   └── verify_top7_runs.sh             ← [확인] 완료 여부 점검
    │
    ├── runs/                               ← 실행 결과 저장
    │   ├── baseline/seed01/ ~ seed30/
    │   └── smart/{crosswalk_id}/seed01/ ~ seed30/
    │
    └── results/                            ← 집계 결과
```

---

## 4. 실행 전 환경 확인

```bash
# 프로젝트 루트로 이동
cd /path/to/js          # 실제 경로로 변경

# SUMO 확인
sumo --version          # 1.26.0 이상

# Python 패키지 확인
python3 -c "import sumolib, traci, pandas, scipy; print('OK')"

# SUMO_HOME 설정 (없으면 추가)
export SUMO_HOME=/path/to/sumo    # 실제 경로로 변경
export PATH="$SUMO_HOME/bin:$PATH"

# 가상환경 사용 시
source .venv/bin/activate
```

---

## 5. 실행 순서

### Step 1 — candidate CSV 재생성 및 preflight 검증

**반드시 먼저 실행.** runner와 동일한 기준(pedestrian shortest path)으로 모든 후보의
`route_from_edge` / `route_to_edge`가 유효한지 확인한다.

```bash
# CSV 재생성 (이미 생성됐어도 한 번 실행 권장)
python3 analysis/make_top7_sim_candidates.py

# 검증만 (CSV 수정 없이 통과 여부만 확인)
python3 analysis/make_top7_sim_candidates.py --verify-only
```

**기대 출력 (--verify-only)**:
```
=== --verify-only: runner 동등 검증 ===
  ✓ LINK_194891   crossing=:11203052965_c0  ok  781985788#1→:11203052965_w1→...
  ✓ NODE_10262    crossing=:11203041635_c0  ok  ...
  ✓ NODE_125895   crossing=:1936511299_c1   ok  ...
  ✓ NODE_5831     crossing=:5161705573_c0   ok  ...
  ✓ LINK_239754   crossing=:5593950698_c0   ok  ...
  ✓ NODE_5846     crossing=:7195225083_c0   ok  ...
  ✓ NODE_8369     crossing=:1936511299_c0   ok  ...
  ✓ baseline CSV: top7 7행
```

7개 모두 `✓`여야 다음으로 진행.

---

### Step 2 — smoke 검증 (seed 1 단건 실행)

30seed 본 실행 전에 실제 SUMO를 돌려서 정상 작동하는지 확인.

```bash
# 전체 top7 smoke (약 7 × 2 × 10분 = 140분 예상)
bash final/top7_sim/commands/smoke_top7_seed1.sh

# 특정 후보만 빠르게 (권장: 처음엔 2~3개만 먼저 확인)
bash final/top7_sim/commands/smoke_top7_seed1.sh --ids LINK_194891 NODE_10262

# smart 후보 병렬 실행
bash final/top7_sim/commands/smoke_top7_seed1.sh --ids LINK_194891 NODE_10262 --jobs 2

# 숫자만 입력 가능
bash final/top7_sim/commands/smoke_top7_seed1.sh --ids 194891 10262

# 재실행 (기존 결과 무시)
bash final/top7_sim/commands/smoke_top7_seed1.sh --ids LINK_194891 --force

# 특정 횡단보도만 지정해 실행
bash final/top7_sim/commands/smoke_top7_seed1.sh --ids NODE_5846 NODE_8369

# 숫자 suffix만 넣어도 됨
bash final/top7_sim/commands/smoke_top7_seed1.sh --ids 5846 8369
```

**smoke 결과 확인 포인트**:
```
  ✓ LINK_194891   ext=1(5.0s)  pet=N  net_delay=X.XXs(Δ+/-Y.YYs)
```
- `ext=0` → smart extension이 한 번도 발동 안 됨 (보행자 수요 부족 or TLS 매핑 오류)
- `net_delay=NaN` → 차량 수요 없음 (`--include-vehicles` 확인)

**`--ids` 동작 방식**

- `--ids` 뒤에 적은 횡단보도만 smoke 대상으로 실행한다.
- 입력은 `NODE_5846`, `LINK_194891`처럼 정확한 ID 또는 `5846`, `194891` 같은 숫자 suffix 둘 다 가능하다.
- `--ids`를 생략하면 top7 전체를 순회한다.
- 선택한 ID가 top7 목록 밖이어도, 해당 `single_candidates/<id>.csv`가 있으면 실행은 허용한다. 이 경우 경고만 출력한다.

---

### Step 3 — 30seed 본 실행

smoke 전체 통과 후 실행. **맥미니에서 `--jobs=4` 병렬 권장.**

> **SSM**: `run_top7_30seed.sh`가 내부적으로 `--enable-ssm`을 포함하는지 확인할 것.  
> 포함되지 않았다면 직접 `run_sampled10_group`을 호출할 때 아래 예시처럼 명시해야 한다.

```bash
# 병렬 4개 실행 (CPU 코어 수에 맞게 조정)
bash final/top7_sim/commands/run_top7_30seed.sh --jobs=4

# 완료된 seed는 스킵하고 나머지만 실행
bash final/top7_sim/commands/run_top7_30seed.sh --jobs=4 --skip-if-done

# 10회만 실행
bash final/top7_sim/commands/run_top7_30seed.sh --jobs=4 --seeds 1-10 --skip-if-done
```

**`--enable-ssm` 직접 호출 예시** (SSM을 명시적으로 켜야 할 때):

```bash
# LINK_194891, seed 1, SSM ON — run_sampled10_group 직접 호출
python3 -m smart_crosswalk_sumo.run_sampled10_group \
  --candidate-csv   final/top7_sim/manifests/single_candidates/LINK_194891.csv \
  --net-file        result/active/nets/generated_signal_7.net.xml \
  --seed            1 \
  --output-dir      final/top7_sim/runs/smart/LINK_194891/seed01 \
  --sim-duration    1800 \
  --warmup          300 \
  --traci_step_length 0.1 \
  --traffic_measure_radius_m 500.0 \
  --extension_increment 5.0 \
  --max_extensions  3 \
  --include-vehicles \
  --output-profile  full \
  --enable-ssm
```

- `--enable-ssm` 없이 실행하면 `pet_event_count` 등 SSM 지표가 모두 0 또는 NaN이 된다.

**예상 소요 시간** (단일 run 기준 ~35분 × 240 run ÷ 4 병렬 = 약 35시간):
- 1 run = baseline (1800s) + smart (1800s) = 3600s sim ≒ 30~40분 wall clock
- 30 seed × 1 baseline + 30 seed × 7 smart = 240 runs
- `--jobs=4` 기준: 약 30~35시간

중간에 끊어도 `--skip-if-done`으로 이어서 실행 가능. `--seeds`는 `1-10`, `1,3,5`, `1-5,8,10` 형식을 지원한다.

---

### Step 4 — 완료 확인

```bash
# 완료 수 빠른 확인
bash final/top7_sim/commands/verify_top7_runs.sh --summary

# 상세 확인 (각 seed 성공/실패)
bash final/top7_sim/commands/verify_top7_runs.sh
```

**기대 출력**:
```
=== baseline (30 seed) ===
  → ✓30 / ✗0 / ?0  (완료율: 100%)

=== smart (7후보 × 30seed) ===
  LINK_194891: ✓30 / ✗0 / ?0  (완료율: 100%)
  NODE_10262:  ✓30 / ✗0 / ?0  (완료율: 100%)
  ...

=== 전체 요약 ===
  기대 총 run 수: 240
  ✓ 성공: 240
  완료율: 100%
```

---

### Step 5 — 결과 집계

본 실행 완료 후 분석 파이프라인 실행.

```bash
# 결과 집계 + top7 최종 분석
python3 analysis/final_top7_pipeline.py --runs-root final/top7_sim/runs
```

**출력 파일**:
```
final/top7/
├── final_all_seed_rows.csv          ← 전체 seed 원시 결과 (30×2×7 = 420행)
├── final_paired_seed_delta.csv      ← seed별 baseline-smart delta
├── final_crosswalk_baseline_smart_summary.csv  ← 후보별 통계 (mean/CI/p-value)
├── final_top7_report_table.csv      ← 보고서용 압축 표
└── final_top7_map.geojson           ← 지도 시각화용
```

---

## 6. 실행 결과 파일 구조

각 run의 결과는 아래 경로에 저장됨:

```
final/top7_sim/runs/
├── baseline/
│   └── seed01/ ~ seed30/
│       ├── simulation_result.csv        ← 핵심: 후보별 지표 1행씩
│       ├── benchmark_timing.json        ← 성공/실패 여부
│       ├── phase6_smoke_baseline_tripinfo.xml  ← 전역 교통흐름 원본
│       ├── phase6_smoke_smart_tripinfo.xml
│       ├── global_traffic_flow.csv      ← statistics/tripinfo 통합 전역 교통흐름
│       ├── global_traffic_comparison.csv← statistics.xml baseline/smart/delta
│       └── global_tripinfo_flow.csv     ← tripinfo.xml baseline/smart/delta
│
└── smart/
    ├── LINK_194891/
    │   └── seed01/ ~ seed30/
    │       └── simulation_result.csv
    ├── NODE_10262/
    └── ... (7개)
```

**`simulation_result.csv` 주요 컬럼**:

| 컬럼 | 설명 |
|------|------|
| `crosswalk_id` | 후보 ID |
| `scenario` | `baseline` 또는 `smart` |
| `seed` | 1~30 |
| `extension_count` | smart 발동 횟수 (baseline은 항상 0) |
| `total_extension_sec` | 총 연장 시간(초) |
| `pedestrian_crossing_count` | 실제 횡단 보행자 수 |
| `ped_wait_time_mean` | 보행자 평균 대기시간(초) |
| `network_avg_delay_sec` | **중구 전체** 차량 평균 대기시간 |
| `network_mean_travel_time` | 전체 차량 평균 통행시간 |
| `network_arrived_vehicles` | 도착 완료 차량 수 |
| `local_500m_avg_delay_sec` | 횡단보도 반경 500m 차량 평균 지체 |
| `pet_event_count` | PET 이벤트 수 |
| `low_pet_event_count` | 위험 PET 이벤트 (PET < 1.5s) |
| `vehicle_route_sha256` | 차량 경로 해시 (paired 검증용) |
| `pedestrian_route_sha256` | 보행자 경로 해시 |

---

## 7. 실패 시 대처

### "no valid pedestrian route pair" 에러

```
ValueError: no valid pedestrian route pair for XXX
```

**원인**: candidate CSV의 `route_from_edge` / `route_to_edge`가 해당 crossing을 통과하지 않음.

**해결**:
```bash
# 1. candidate CSV 재생성 (runner 동등 검증 포함)
python3 analysis/make_top7_sim_candidates.py

# 2. preflight 재확인
python3 analysis/make_top7_sim_candidates.py --verify-only

# 3. 해당 후보만 재실행
bash final/top7_sim/commands/smoke_top7_seed1.sh --ids {crosswalk_id} --force
```

### "benchmark_timing.json missing" 에러

출력 디렉토리가 비어있거나 도중에 중단된 경우.

```bash
# 해당 seed 삭제 후 재실행
rm -rf final/top7_sim/runs/baseline/seed{XX}
bash final/top7_sim/commands/run_top7_30seed.sh --jobs=4 --skip-if-done
```

### ext=0 (extension이 한 번도 발동 안 됨)

```bash
# 해당 후보의 pedestrian_route_diagnostics.csv 확인
cat final/top7_sim/runs/baseline/seed01/pedestrian_route_diagnostics.csv

# TLS 매핑 확인
python3 -c "
import pandas as pd
df = pd.read_csv('final/top7_sim/manifests/single_candidates/{crosswalk_id}.csv')
print(df[['tls_id_used','crossing_edge_id','ped_link_index']].to_string())
"
```

### 실행이 중간에 끊긴 경우

```bash
# 완료된 것 건너뛰고 남은 것만 재개
bash final/top7_sim/commands/run_top7_30seed.sh --jobs=4 --skip-if-done
```

---

## 8. 선택: 동시 multi-smart 검증

여러 후보가 같은 시뮬레이션에서 동시에 smart 작동하는지 확인.  
(결과 논문에서 "동시 설치 효과" 분석용)

```bash
# 2개 후보 동시 설치 검증
bash final/top7_sim/commands/smoke_multi_smart_seed1.sh --ids LINK_194891 NODE_10262 --force

# 전체 top7 동시 설치
bash final/top7_sim/commands/smoke_multi_smart_seed1.sh --force

# 결과 확인
python3 -c "
import pandas as pd
df = pd.read_csv('final/top7_sim/results/multi_smart_smoke_comparison.csv')
print(df[['selected_ids','n_smart_crosswalks',
          'delta_network_avg_delay_sec','total_extension_count','comparison_ok']].to_string())
"
```

---

## 9. 주요 파일 체크리스트

실행 전 아래 파일이 존재하는지 확인:

```bash
ls -lh result/active/nets/generated_signal_7.net.xml
ls -lh final/top7_sim/manifests/top7_baseline_candidates.csv
ls final/top7_sim/manifests/single_candidates/*.csv | wc -l   # 34개여야 함
cat final/top7_sim/manifests/top7_baseline_candidates.csv | python3 -c "
import sys, csv; rows=list(csv.DictReader(sys.stdin))
print(f'baseline CSV: {len(rows)}행')
for r in rows: print(f'  {r[\"crosswalk_id\"]:15s}  {r[\"crossing_edge_id\"]}')
"
```

**기대값**:
```
baseline CSV: 7행
  LINK_194891      :11203052965_c0
  NODE_10262       :11203041635_c0
  NODE_125895      :1936511299_c1
  NODE_5831        :5161705573_c0
  LINK_239754      :5593950698_c0
  NODE_5846        :7195225083_c0
  NODE_8369        :1936511299_c0
```

---

## 10. 전체 커맨드 요약

```bash
cd /path/to/js                   # ← 실제 경로로 변경

# [필수] 환경 확인
sumo --version
python3 -c "import sumolib, traci, pandas, scipy; print('OK')"

# [Step 1] preflight
python3 analysis/make_top7_sim_candidates.py --verify-only

# [Step 2] smoke (먼저 2개만 빠르게 확인)
bash final/top7_sim/commands/smoke_top7_seed1.sh --ids LINK_194891 NODE_10262 --jobs 2 --force

# [Step 3] 10회만 본 실행
bash final/top7_sim/commands/run_top7_30seed.sh --jobs=4 --seeds 1-10 --skip-if-done

# [Step 4] 완료 확인
bash final/top7_sim/commands/verify_top7_runs.sh --summary

# [Step 5] 결과 집계
python3 analysis/final_top7_pipeline.py --runs-root final/top7_sim/runs
```

---

## 11. top7521 후보 세트 실행

> **top7521**은 최종 확정된 7개 후보 세트입니다. 인프라(net.xml, 파라미터, 스크립트 구조)는 기존 top7과 동일합니다.

### 후보 목록

| # | crosswalk_id |
|---|-------------|
| 1 | **LINK_239754** |
| 2 | **NODE_5831** |
| 3 | **NODE_5846** |
| 4 | **LINK_10218** |
| 5 | **LINK_120139** |
| 6 | **NODE_10335** |
| 7 | **NODE_10376** |

### 실행 명령

```bash
# 기본 (순차)
bash final/top7_sim/commands/run_top7521_30seed.sh

# 병렬 4개 (권장)
bash final/top7_sim/commands/run_top7521_30seed.sh --jobs=4

# 완료된 seed 스킵하며 이어 실행
bash final/top7_sim/commands/run_top7521_30seed.sh --jobs=4 --skip-if-done

# seed 범위 지정
bash final/top7_sim/commands/run_top7521_30seed.sh --jobs=4 --seeds 1-10

# 실제 실행 전 확인
bash final/top7_sim/commands/run_top7521_30seed.sh --dry-run --jobs=4
```

dry-run 기준 **총 240 runs** (baseline 30 + smart 7×30).

### 결과 저장 경로

기존 `runs/` 디렉토리와 동일한 위치 사용.

```
final/top7_sim/runs/
├── baseline/seed01/ ~ seed30/
└── smart/
    ├── LINK_239754/seed01/ ~ seed30/
    ├── NODE_5831/
    ├── NODE_5846/
    ├── LINK_10218/
    ├── LINK_120139/
    ├── NODE_10335/
    └── NODE_10376/
```

### 전체 흐름 요약

```bash
# [Step 1] dry-run으로 구성 확인
bash final/top7_sim/commands/run_top7521_30seed.sh --dry-run --jobs=4

# [Step 2] 30seed 본 실행
bash final/top7_sim/commands/run_top7521_30seed.sh --jobs=4 --skip-if-done

# [Step 3] 결과 집계
python3 analysis/final_top7_pipeline.py
```

---

## 12. 참고: 기존 30seed 결과와의 관계

이번 top7_sim은 기존 `final/runs/` 결과와 **독립적**:

| 항목 | 기존 결과 (`final/runs/`) | 이번 결과 (`final/top7_sim/runs/`) |
|------|--------------------------|----------------------------------|
| 네트워크 | 4개 그룹별 분리 net | generated_signal_7 통합 |
| 후보 수 | 34개 전체 | top7만 |
| sim_duration | 600s | **1800s** |
| warmup | 0s | **300s** |
| max_extensions | 1 | **3** |
| SSM | 꺼짐 | **켜짐** |
| 목적 | 후보 선별용 | **최종 효과 검증용** |

기존 결과는 top7 선별에 사용됐고, 이번 실험은 선별된 top7에 대한 **최종 paired simulation**임.

---

*문의: junlee (준석) 또는 issue tracker*
