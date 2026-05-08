from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd


STATUS_ORDER = {
    "implemented": 0,
    "partially_implemented": 1,
    "not_implemented": 2,
    "unclear": 3,
}


def make_rows() -> tuple[list[dict[str, str]], list[dict[str, str]], list[dict[str, str]]]:
    visualization_rows = [
        {
            "연구 항목": "3. 시각화 방법",
            "세부 기능": "시뮬레이션 애니메이션 직접 확인",
            "현재 구현 여부": "partially_implemented",
            "관련 파일/함수": "smart_crosswalk_sumo/visualization_exports.py:35-53; smart_crosswalk_sumo/main.py:169-176,252-253",
            "현재 동작 방식": "`--export_gui` 실행 시 replay용 sumocfg를 복사하고 `sumo-gui`로 특정 시점 스크린샷을 캡처할 수 있다.",
            "부족한 점": "파이프라인이 GUI 세션을 상시 띄우거나 baseline/smart를 나란히 재생하지는 않는다.",
            "보완 필요 코드": "GUI replay launcher, baseline/smart 동시 재생 스크립트, 실행 안내 문서",
        },
        {
            "연구 항목": "3. 시각화 방법",
            "세부 기능": "baseline/smart SUMO-GUI용 sumocfg 생성",
            "현재 구현 여부": "implemented",
            "관련 파일/함수": "smart_crosswalk_sumo/generate_demand.py:498-509; smart_crosswalk_sumo/visualization_exports.py:26-32,131-176",
            "현재 동작 방식": "각 후보/seed마다 `baseline_seed*.sumocfg`, `smart_seed*.sumocfg`를 만들고 replay 사본도 생성한다.",
            "부족한 점": "GUI 전용 view 설정이 후보별로 최적화되지는 않는다.",
            "보완 필요 코드": "후보별 GUI 설정 자동 생성, baseline/smart 비교용 launch manifest",
        },
        {
            "연구 항목": "3. 시각화 방법",
            "세부 기능": "후보 횡단보도 주변 확대 보기",
            "현재 구현 여부": "partially_implemented",
            "관련 파일/함수": "smart_crosswalk_sumo/visualization_exports.py:18-23; scripts/validate_sumo_network.py:231-253,282-318,346-348",
            "현재 동작 방식": "GUI settings XML과 후보 crossing을 강조한 `network_validation_map.png`가 생성된다.",
            "부족한 점": "viewport가 정적 값이고 후보 위치로 자동 zoom/pan 되지 않는다.",
            "보완 필요 코드": "metadata 기반 candidate-centered viewport 계산, GUI bookmark 파일",
        },
        {
            "연구 항목": "3. 시각화 방법",
            "세부 기능": "차량·보행자·신호·대기열·스마트 연장 여부 시각 확인",
            "현재 구현 여부": "partially_implemented",
            "관련 파일/함수": "smart_crosswalk_sumo/run_simulations.py:1013-1050,1155-1189,1214-1244,1335-1413; smart_crosswalk_sumo/collect_metrics.py:299-336",
            "현재 동작 방식": "SUMO-GUI 자체로 차량/보행자/신호를 볼 수 있고, queue XML·FCD XML·extension event CSV가 별도 저장된다.",
            "부족한 점": "queue와 smart extension 상태를 GUI 위에 직접 overlay하지 않는다.",
            "보완 필요 코드": "TraCI GUI overlay, signal-state timeline, extension event annotation renderer",
        },
        {
            "연구 항목": "3. 시각화 방법",
            "세부 기능": "정량 결과 지도/그래프/PNG/CSV 시각화",
            "현재 구현 여부": "implemented",
            "관련 파일/함수": "smart_crosswalk_sumo/generate_reports.py:344-389,520-643; smart_crosswalk_sumo/visualization_exports.py:55-81,84-128; scripts/render_network_overlay.py:236-260",
            "현재 동작 방식": "trade-off PNG, candidate bar/scatter PNG, conflict overlay GeoJSON, queue heatmap HTML, CSV 요약을 생성한다.",
            "부족한 점": "대시보드 형태 통합 뷰는 없다.",
            "보완 필요 코드": "결과 dashboard, 지도-그래프 연동 시각화",
        },
        {
            "연구 항목": "3. 시각화 방법",
            "세부 기능": "baseline vs smart 비교 시각화",
            "현재 구현 여부": "implemented",
            "관련 파일/함수": "smart_crosswalk_sumo/generate_reports.py:103-136,212-260,344-389,534-597",
            "현재 동작 방식": "baseline/smart delta summary CSV와 trade-off scatter, candidate별 safety/traffic PNG를 만든다.",
            "부족한 점": "시계열 애니메이션 비교와 후보별 상세 리플레이 링크는 없다.",
            "보완 필요 코드": "candidate detail comparison page, baseline/smart synchronized replay",
        },
        {
            "연구 항목": "3. 시각화 방법",
            "세부 기능": "vehicle-only vs full simulation 비교 시각화",
            "현재 구현 여부": "not_implemented",
            "관련 파일/함수": "scripts/validate_pedestrian_full_simulation.py:160-176,397-408",
            "현재 동작 방식": "비교 CSV는 생성하지만 PNG/HTML 등 별도 시각화 코드는 없다.",
            "부족한 점": "full vs vehicle-only 차이를 직관적으로 보여주는 그래프가 없다.",
            "보완 필요 코드": "comparison plotting script, delta histogram/scatter, candidate ranking chart",
        },
    ]

    analysis_rows = [
        {
            "연구 항목": "4. 분석 규모",
            "세부 기능": "횡단보도 1개소 단위 분석 구조",
            "현재 구현 여부": "implemented",
            "관련 파일/함수": "smart_crosswalk_sumo/build_networks.py:401-541; smart_crosswalk_sumo/network_utils.py:406-435; smart_crosswalk_sumo/collect_metrics.py:88-109",
            "현재 동작 방식": "각 `cw_<id>` 디렉토리마다 독립 네트워크·metadata·route·sumocfg를 만든다.",
            "부족한 점": "후보 간 상호작용을 한 도시 단일 네트워크에서 동시에 평가하지 않는다.",
            "보완 필요 코드": "multi-candidate shared-network experiment mode",
        },
        {
            "연구 항목": "4. 분석 규모",
            "세부 기능": "여러 후보 반복 실행",
            "현재 구현 여부": "implemented",
            "관련 파일/함수": "smart_crosswalk_sumo/main.py:76-185; smart_crosswalk_sumo/generate_demand.py:364-558; smart_crosswalk_sumo/collect_metrics.py:43-351",
            "현재 동작 방식": "후보 목록과 seed 목록을 순회하며 baseline/smart를 반복 실행한다.",
            "부족한 점": "병렬 분산 실행이나 대규모 배치 스케줄링은 없다.",
            "보완 필요 코드": "parallel runner, job manifest, resume/retry control",
        },
        {
            "연구 항목": "4. 분석 규모",
            "세부 기능": "교통 비용의 공간 범위(local vs network)",
            "현재 구현 여부": "partially_implemented",
            "관련 파일/함수": "smart_crosswalk_sumo/run_simulations.py:125-179,591-645,648-680; smart_crosswalk_sumo/collect_metrics.py:228-241",
            "현재 동작 방식": "접근부 queue/delay, 주변 반경(`traffic_measure_radius_m`) 지표, network throughput/travel time를 함께 기록한다. 다만 비용형 `vehicle_delay_cost`는 접근부 total vehicle delay 기반이다.",
            "부족한 점": "전 도로망 총지체 비용을 별도 금액으로 산출하지 않는다.",
            "보완 필요 코드": "network-wide delay cost aggregation, local/network cost 분리 보고",
        },
        {
            "연구 항목": "4. 분석 규모",
            "세부 기능": "안전 편익의 공간 단위",
            "현재 구현 여부": "partially_implemented",
            "관련 파일/함수": "smart_crosswalk_sumo/preprocess.py:60-76; smart_crosswalk_sumo/run_simulations.py:524-546,648-668; smart_crosswalk_sumo/generate_reports.py:616-620",
            "현재 동작 방식": "후보 횡단보도 단위 위험 proxy(`PET_B_surrogate`, `elderly_incomplete_cross`, `risk_score`)를 계산한다.",
            "부족한 점": "도로망 전체 사고율이나 corridor-level safety aggregation은 없다.",
            "보완 필요 코드": "network-level safety aggregation, candidate-to-network spillover safety model",
        },
        {
            "연구 항목": "4. 분석 규모",
            "세부 기능": "시뮬레이션 시간 단위 설정",
            "현재 구현 여부": "implemented",
            "관련 파일/함수": "smart_crosswalk_sumo/main.py:194-199; scripts/run_report_simulation.py:30-31,103-111; scripts/validate_pedestrian_full_simulation.py:423-428",
            "현재 동작 방식": "`sim_duration`과 `warmup`을 CLI로 지정한다. 기본은 1800초 + warmup 300초이며 smoke/validation은 600초도 사용한다.",
            "부족한 점": "보고서용 표준 시간 단위를 코드에서 고정하지 않는다.",
            "보완 필요 코드": "scenario presets for 1h / peak-hour / daily package",
        },
        {
            "연구 항목": "4. 분석 규모",
            "세부 기능": "피크/비피크/야간 시나리오",
            "현재 구현 여부": "partially_implemented",
            "관련 파일/함수": "smart_crosswalk_sumo/generate_demand.py:34-45; scripts/run_report_simulation.py:77-78",
            "현재 동작 방식": "`average`, `seoul_commute_peak` 두 demand profile만 있다.",
            "부족한 점": "비피크/야간/주말/심야 보행 편차 시나리오는 없다.",
            "보완 필요 코드": "offpeak/night/weekend demand profiles and runner presets",
        },
        {
            "연구 항목": "4. 분석 규모",
            "세부 기능": "하루 단위 환산 로직",
            "현재 구현 여부": "not_implemented",
            "관련 파일/함수": "smart_crosswalk_sumo/main.py:194-199; smart_crosswalk_sumo/generate_reports.py:520-643",
            "현재 동작 방식": "시간당 또는 시뮬레이션 구간 평균 지표만 저장한다.",
            "부족한 점": "1시간 결과를 일일 편익/비용으로 환산하는 로직이 없다.",
            "보완 필요 코드": "hour-to-day scaling, time-of-day weighting, annualization module",
        },
        {
            "연구 항목": "4. 분석 규모",
            "세부 기능": "후보지 ranking / 설치 우선순위 계산",
            "현재 구현 여부": "partially_implemented",
            "관련 파일/함수": "smart_crosswalk_sumo/preprocess.py:60-77; smart_crosswalk_sumo/generate_reports.py:537-597,630-643",
            "현재 동작 방식": "전처리 단계에서 `risk_score`로 후보를 추리고, 사후에는 `tradeoff_summary.csv`와 개선률 PNG를 만든다.",
            "부족한 점": "최종 설치 우선순위 점수나 의사결정 규칙은 없다.",
            "보완 필요 코드": "multi-criteria ranking score, budget-constrained prioritization",
        },
        {
            "연구 항목": "4. 분석 규모",
            "세부 기능": "local impact와 network impact 구분",
            "현재 구현 여부": "implemented",
            "관련 파일/함수": "smart_crosswalk_sumo/run_simulations.py:591-645; smart_crosswalk_sumo/generate_reports.py:225-258",
            "현재 동작 방식": "접근부 delay/queue, 주변 반경 queue/speed, adjacent TLS queue, network throughput/travel time를 분리 저장한다.",
            "부족한 점": "보고서 출력이 항상 local/network를 명시적으로 분할 표기하지는 않는다.",
            "보완 필요 코드": "local_vs_network summary table and plots",
        },
    ]

    distribution_rows = [
        {
            "연구 항목": "5. 분포 및 파라미터 정당성",
            "세부 기능": "보행자 도착 분포",
            "현재 구현 여부": "implemented",
            "관련 파일/함수": "smart_crosswalk_sumo/generate_demand.py:154-160,225-288; smart_crosswalk_sumo/config/model_assumptions.yaml:9-15",
            "현재 동작 방식": "`ped_mean_gap_sec`를 계산하고 `rng.exponential(mean_gap)`로 도착 간격을 생성한다. 문서상도 exponential 가정이다.",
            "부족한 점": "현장 계수 기반 시간대별 비정상(non-homogeneous) arrival은 없다.",
            "보완 필요 코드": "time-varying pedestrian arrival profile, observed pedestrian count calibration",
        },
        {
            "연구 항목": "5. 분포 및 파라미터 정당성",
            "세부 기능": "일반/노인 보행자 속도 분포 구분",
            "현재 구현 여부": "partially_implemented",
            "관련 파일/함수": "smart_crosswalk_sumo/generate_demand.py:233-264,272-283; smart_crosswalk_sumo/config/model_assumptions.yaml:33-55",
            "현재 동작 방식": "성인/노인 `vType`를 분리하고 `maxSpeed`, `speedDev`, `startupDelay`를 다르게 둔다.",
            "부족한 점": "평균·표준편차·하한/상한을 갖는 절단정규분포는 아니고, `speedDev`만 준 SUMO 기본 분산에 의존한다.",
            "보완 필요 코드": "truncated-normal speed sampler, calibrated bounds, age-specific distributions",
        },
        {
            "연구 항목": "5. 분포 및 파라미터 정당성",
            "세부 기능": "차량 도착/교통량 분포",
            "현재 구현 여부": "partially_implemented",
            "관련 파일/함수": "smart_crosswalk_sumo/generate_demand.py:127-132,184-222; smart_crosswalk_sumo/run_simulations.py:1122-1128; smart_crosswalk_sumo/config/model_assumptions.yaml:1-7",
            "현재 동작 방식": "route 생성은 `randomTrips.py --period`의 평균 주기 기반이고, 내부 queue model은 `poisson` 또는 `bernoulli` arrival model을 지원한다.",
            "부족한 점": "실제 SUMO vehicle flow와 내부 queue arrival model이 완전히 일치하지 않는다.",
            "보완 필요 코드": "single-source traffic arrival model, observed OD/flow calibration, time-varying flows",
        },
        {
            "연구 항목": "5. 분포 및 파라미터 정당성",
            "세부 기능": "차량 속도 분포",
            "현재 구현 여부": "partially_implemented",
            "관련 파일/함수": "smart_crosswalk_sumo/preprocess.py:45-46,60-65; smart_crosswalk_sumo/run_simulations.py:1159-1167,1214-1244",
            "현재 동작 방식": "도로 특성으로 `max_speed_kph`를 보유하고, 실행 중 실제 속도는 SUMO 네트워크·car-following·event speed multiplier에 맡긴다.",
            "부족한 점": "명시적 `speedFactor` 분포나 차량군별 속도 분포 정의는 없다.",
            "보완 필요 코드": "vehicle type speedFactor distribution, class-specific speed calibration",
        },
        {
            "연구 항목": "5. 분포 및 파라미터 정당성",
            "세부 기능": "사고확률/안전위험 계산",
            "현재 구현 여부": "partially_implemented",
            "관련 파일/함수": "smart_crosswalk_sumo/preprocess.py:60-65; smart_crosswalk_sumo/run_simulations.py:524-546,648-668; smart_crosswalk_sumo/config/model_assumptions.yaml:145-159",
            "현재 동작 방식": "전처리 `risk_score`는 `accident_count`, `elderly_ratio`, `lane_count`, `max_speed_kph`를 사용하고, 실행 후에는 `PET_B_surrogate_severe + elderly_incomplete * weight`를 `safety_risk_score`와 `accident_expected_value`로 변환한다.",
            "부족한 점": "실제 사고 발생확률 모형은 아니고 `accident_count_50m/100m/200m`, `night_accident_ratio` 같은 상세 변수는 코드에 직접 보이지 않는다.",
            "보완 필요 코드": "formal safety surrogate model, spatial accident feature integration, night-time risk terms",
        },
        {
            "연구 항목": "5. 분포 및 파라미터 정당성",
            "세부 기능": "실제 사고 발생 시뮬레이션 여부",
            "현재 구현 여부": "not_implemented",
            "관련 파일/함수": "smart_crosswalk_sumo/run_simulations.py:283-301,320-436,648-668; smart_crosswalk_sumo/generate_reports.py:616-620",
            "현재 동작 방식": "`accident`는 도로용량 저하 이벤트로만 사용되고, 안전 편익은 proxy로 평가한다.",
            "부족한 점": "보행자-차량 충돌 확률이나 사고 건수 자체를 생성하지 않는다.",
            "보완 필요 코드": "crash probability module or surrogate-to-crash conversion model",
        },
        {
            "연구 항목": "5. 분포 및 파라미터 정당성",
            "세부 기능": "가속차량/위험 접근 차량 개념",
            "현재 구현 여부": "not_implemented",
            "관련 파일/함수": "smart_crosswalk_sumo/run_simulations.py:990-1003,1105-1120",
            "현재 동작 방식": "차량 exit 시각과 보행자 진입 시각으로 PET proxy를 만든다.",
            "부족한 점": "가속, 감속, TTC, harsh approach 같은 위험 접근 차량 상태를 별도 분류하지 않는다.",
            "보완 필요 코드": "approach speed/acceleration tracker, TTC/DRAC surrogate extraction",
        },
        {
            "연구 항목": "5. 분포 및 파라미터 정당성",
            "세부 기능": "민감도 분석: vehicle volume / disruption",
            "현재 구현 여부": "implemented",
            "관련 파일/함수": "smart_crosswalk_sumo/vehicle_sensitivity.py:19-37,64-139,189-233; smart_crosswalk_sumo/plot_vehicle_sensitivity.py:23-80",
            "현재 동작 방식": "traffic level, disruption scenario, signal policy를 바꿔 raw/summary CSV와 PNG를 생성한다.",
            "부족한 점": "vehicle speed나 pedestrian parameter sweep는 포함되지 않는다.",
            "보완 필요 코드": "broader sensitivity grid and experiment config",
        },
        {
            "연구 항목": "5. 분포 및 파라미터 정당성",
            "세부 기능": "민감도 분석: pedestrian arrival / elderly ratio / walking speed / vehicle speed / green extension / accident coefficient",
            "현재 구현 여부": "partially_implemented",
            "관련 파일/함수": "smart_crosswalk_sumo/vehicle_sensitivity.py:74-85,275-293; smart_crosswalk_sumo/main.py:215-225",
            "현재 동작 방식": "`extension_increment`, `max_extensions`, disruption rate는 입력값 변경이 가능하다.",
            "부족한 점": "나열된 파라미터를 체계적으로 sweep하는 자동화가 없다. green extension도 단일 입력 변경 수준이다.",
            "보완 필요 코드": "parameter sweep runner for pedestrian/vehicle/safety coefficients",
        },
    ]
    return visualization_rows, analysis_rows, distribution_rows


def df_to_markdown(df: pd.DataFrame) -> str:
    columns = list(df.columns)
    header = "| " + " | ".join(columns) + " |"
    separator = "| " + " | ".join(["---"] * len(columns)) + " |"
    rows = [header, separator]
    for _, row in df.iterrows():
        values = []
        for col in columns:
            text = str(row[col]).replace("\n", "<br>")
            values.append(text)
        rows.append("| " + " | ".join(values) + " |")
    return "\n".join(rows)


def write_markdown(
    out_path: Path,
    visualization_df: pd.DataFrame,
    analysis_df: pd.DataFrame,
    distribution_df: pd.DataFrame,
) -> None:
    lines = [
        "# Research Items 3, 4, 5 Implementation Audit",
        "",
        "현재 코드를 기준으로 연구 설계 3, 4, 5번 항목의 구현 여부를 감사했다.",
        "",
        "## 3. 시각화 방법",
        "",
        df_to_markdown(visualization_df),
        "",
        "## 4. 분석 규모",
        "",
        df_to_markdown(analysis_df),
        "",
        "## 5. 분포 및 파라미터 정당성",
        "",
        df_to_markdown(distribution_df),
        "",
    ]
    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_missing_features(out_path: Path) -> None:
    lines = [
        "# Missing Features For Research Design",
        "",
        "## Visualization",
        "- baseline/smart 동시 GUI replay launcher와 후보별 zoom preset이 필요하다.",
        "- smart extension, queue, signal state를 GUI 또는 후처리 영상에 overlay하는 코드가 필요하다.",
        "- vehicle-only vs full simulation 비교용 PNG/HTML 시각화가 필요하다.",
        "",
        "## Analysis Scope",
        "- 일일 환산, 피크/비피크/야간 preset, 연간화 로직이 필요하다.",
        "- 최종 설치 우선순위를 계산하는 multi-criteria ranking score가 필요하다.",
        "- 전 도로망 비용과 후보 주변 비용을 분리한 비용 보고 체계가 필요하다.",
        "",
        "## Distribution / Safety",
        "- 보행자/차량 분포를 시간대별로 보정하는 observed-data 기반 수요 모듈이 필요하다.",
        "- 보행속도에 대해 절단정규 등 명시적 분포와 calibration 근거가 필요하다.",
        "- safety proxy를 실제 사고 기대값 또는 정식 surrogate risk model로 연결하는 모듈이 필요하다.",
        "- 가속차량, 위험 접근 차량, TTC/DRAC 같은 위험 대리변수 추출이 필요하다.",
        "- pedestrian arrival, elderly ratio, walking speed, vehicle speed, green extension, accident coefficient 민감도 sweep가 필요하다.",
        "",
    ]
    out_path.write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output_dir",
        default=str(Path("result") / "research_design_audit_2026-05-08"),
    )
    args = parser.parse_args()

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    visualization_rows, analysis_rows, distribution_rows = make_rows()
    visualization_df = pd.DataFrame(visualization_rows).sort_values(
        by=["현재 구현 여부", "세부 기능"],
        key=lambda s: s.map(STATUS_ORDER),
    )
    analysis_df = pd.DataFrame(analysis_rows).sort_values(
        by=["현재 구현 여부", "세부 기능"],
        key=lambda s: s.map(STATUS_ORDER),
    )
    distribution_df = pd.DataFrame(distribution_rows).sort_values(
        by=["현재 구현 여부", "세부 기능"],
        key=lambda s: s.map(STATUS_ORDER),
    )

    visualization_df.to_csv(output_dir / "visualization_capability_audit.csv", index=False)
    analysis_df.to_csv(output_dir / "analysis_scope_audit.csv", index=False)
    distribution_df.to_csv(output_dir / "distribution_assumption_audit.csv", index=False)

    write_markdown(
        output_dir / "research_items_3_4_5_implementation_audit.md",
        visualization_df,
        analysis_df,
        distribution_df,
    )
    write_missing_features(output_dir / "missing_features_for_research_design.md")

    print(output_dir)


if __name__ == "__main__":
    main()
