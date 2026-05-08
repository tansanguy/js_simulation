from __future__ import annotations

import argparse
import json
import math
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from pandas.errors import EmptyDataError

try:
    from .model_config import load_model_parameters
except ImportError:
    from model_config import load_model_parameters


SIMULATION_SUMMARY_COLUMNS = [
    "crosswalk_id",
    "scenario",
    "seed_count",
    "safety_risk_score",
    "accident_expected_value",
    "elderly_incomplete_crossings",
    "avg_vehicle_delay_sec",
    "avg_queue_length",
    "max_queue_length",
    "surrounding_road_delay_sec",
    "vehicle_delay_cost",
    "extension_count",
    "total_extension_sec",
]

BASELINE_SMART_COLUMNS = [
    "crosswalk_id",
    "safety_risk_delta",
    "accident_expected_delta",
    "elderly_incomplete_crossings_delta",
    "avg_vehicle_delay_delta_sec",
    "avg_queue_length_delta",
    "max_queue_length_delta",
    "surrounding_road_delay_delta_sec",
    "vehicle_delay_cost_delta",
    "extension_count_smart",
    "total_extension_sec_smart",
]


def df_to_markdown(df: pd.DataFrame) -> str:
    try:
        return df.to_markdown(index=False)
    except ImportError:
        return "```text\n" + df.to_csv(index=False) + "```"


def mean_or_nan(series: pd.Series) -> float:
    arr = series.to_numpy(dtype=float)
    if arr.size == 0 or np.all(np.isnan(arr)):
        return float("nan")
    return float(np.nanmean(arr))


def pct_reduction(baseline: pd.Series, smart: pd.Series) -> pd.Series:
    denom = baseline.replace(0, np.nan)
    return (baseline - smart) / denom * 100


def metric_or_nan(frame: pd.DataFrame, column: str, index: pd.Index) -> pd.Series:
    if column in frame.columns:
        return frame[column]
    return pd.Series(np.nan, index=index, dtype=float)


def ensure_columns(df: pd.DataFrame, columns: list[str]) -> pd.DataFrame:
    frame = df.copy()
    for column in columns:
        if column not in frame.columns:
            frame[column] = np.nan
    return frame[columns]


def build_simulation_summary(avg_df: pd.DataFrame, seed_df: pd.DataFrame) -> pd.DataFrame:
    if avg_df.empty:
        return pd.DataFrame(columns=SIMULATION_SUMMARY_COLUMNS)

    seed_counts = (
        seed_df.groupby(["crosswalk_id", "scenario"], as_index=False)["seed"]
        .nunique()
        .rename(columns={"seed": "seed_count"})
        if not seed_df.empty and {"crosswalk_id", "scenario", "seed"}.issubset(seed_df.columns)
        else pd.DataFrame(columns=["crosswalk_id", "scenario", "seed_count"])
    )

    summary = avg_df.merge(seed_counts, on=["crosswalk_id", "scenario"], how="left")
    summary["crosswalk_id"] = summary["crosswalk_id"].astype(str)
    return ensure_columns(summary, SIMULATION_SUMMARY_COLUMNS).sort_values(
        ["crosswalk_id", "scenario"]
    )


def build_baseline_vs_smart_summary(summary_df: pd.DataFrame) -> pd.DataFrame:
    if summary_df.empty:
        return pd.DataFrame(columns=BASELINE_SMART_COLUMNS)

    baseline = summary_df[summary_df["scenario"] == "baseline"].set_index("crosswalk_id")
    smart = summary_df[summary_df["scenario"] == "smart"].set_index("crosswalk_id")
    common = baseline.index.intersection(smart.index)
    if common.empty:
        return pd.DataFrame(columns=BASELINE_SMART_COLUMNS)

    result = pd.DataFrame(
        {
            "crosswalk_id": common,
            "safety_risk_delta": smart.loc[common, "safety_risk_score"].to_numpy()
            - baseline.loc[common, "safety_risk_score"].to_numpy(),
            "accident_expected_delta": smart.loc[common, "accident_expected_value"].to_numpy()
            - baseline.loc[common, "accident_expected_value"].to_numpy(),
            "elderly_incomplete_crossings_delta": smart.loc[common, "elderly_incomplete_crossings"].to_numpy()
            - baseline.loc[common, "elderly_incomplete_crossings"].to_numpy(),
            "avg_vehicle_delay_delta_sec": smart.loc[common, "avg_vehicle_delay_sec"].to_numpy()
            - baseline.loc[common, "avg_vehicle_delay_sec"].to_numpy(),
            "avg_queue_length_delta": smart.loc[common, "avg_queue_length"].to_numpy()
            - baseline.loc[common, "avg_queue_length"].to_numpy(),
            "max_queue_length_delta": smart.loc[common, "max_queue_length"].to_numpy()
            - baseline.loc[common, "max_queue_length"].to_numpy(),
            "surrounding_road_delay_delta_sec": smart.loc[common, "surrounding_road_delay_sec"].to_numpy()
            - baseline.loc[common, "surrounding_road_delay_sec"].to_numpy(),
            "vehicle_delay_cost_delta": smart.loc[common, "vehicle_delay_cost"].to_numpy()
            - baseline.loc[common, "vehicle_delay_cost"].to_numpy(),
            "extension_count_smart": smart.loc[common, "extension_count"].to_numpy(),
            "total_extension_sec_smart": smart.loc[common, "total_extension_sec"].to_numpy(),
        }
    )
    return ensure_columns(result, BASELINE_SMART_COLUMNS).sort_values("crosswalk_id")


def write_methodology_report(
    output_dir: Path,
    model_params: dict[str, dict[str, Any]],
    simulation_summary: pd.DataFrame,
    delta_summary: pd.DataFrame,
) -> None:
    if simulation_summary.empty:
        status_text = "현재 상태는 `pending/not_run`이다. 테스트 실행 단계이며 최종 결과표는 아직 채워지지 않았다."
    else:
        status_text = (
            f"현재 결과는 테스트 실행 단계 산출물이며, `crosswalk_id` 기준 요약 행 수는 {len(simulation_summary)}개다."
        )

    assumptions_preview = pd.DataFrame(
        [
            {
                "parameter": name,
                "value": json.dumps(meta.get("value"), ensure_ascii=False)
                if isinstance(meta.get("value"), (dict, list))
                else meta.get("value"),
                "source_type": meta.get("source_type"),
                "used_in": meta.get("used_in"),
            }
            for name, meta in model_params.items()
        ]
    )
    body = f"""# 시뮬레이션 메모

## 목적

이 시뮬레이션은 미래 교통량을 정확히 예측하는 모델이 아니라, 동일 조건에서 후보 횡단보도별 baseline/smart를 비교하는 정책 시나리오 실험이다.

- 동일 조건 비교
- 상대적 효과 평가
- 후보 횡단보도별 전/후 비교
- 5초 보행녹색 연장의 안전-지체 trade-off 산출

시뮬레이션은 후보를 자동 선정하지 않는다. 후보 횡단보도는 사전에 선정되며, 시뮬레이션은 선정된 후보에 대한 정량 비교와 비용-효과 보정 용도로 사용한다.

## 현재 상태

{status_text}

- SUMO 시각화는 일부 테스트 수준으로만 유지한다.
- 실제 최종 결과는 아직 확정본이 아니다.
- 서버 또는 실행 환경 연결은 계속 진행 중일 수 있다.

## 핵심 모델 규칙

- baseline: 고정 신호 유지
- smart: 보행 녹색 종료 직전 감지 시 5초 1회 연장
- 연장 조건: 보행 녹색 phase, 잔여시간 임계 이내, crossing/detector 보행자 존재, 같은 cycle 미연장, false negative 미발생
- 이벤트 기반 용량 감소: `accident`, `bus_stop`
- 네트워크 기본값: `expanded` + `buffer_m=1000`

## 사용 assumptions

{df_to_markdown(assumptions_preview)}

## 최종 보고용 파일

- `outputs/simulation_summary.csv`
- `outputs/baseline_vs_smart_summary.csv`
- `outputs/model_assumptions_used.csv`
- `figures/tradeoff_summary.png`

## 비교 결과 미리보기

{df_to_markdown(delta_summary.head(20)) if not delta_summary.empty else '비교 가능한 baseline/smart 결과가 아직 없다.'}
"""
    (output_dir / "report_4_methodology.md").write_text(body, encoding="utf-8")


def comparison_table(avg_df: pd.DataFrame) -> pd.DataFrame:
    baseline = avg_df[avg_df["시나리오"] == "baseline"].set_index("횡단보도ID")
    smart = avg_df[avg_df["시나리오"] == "smart"].set_index("횡단보도ID")
    common = baseline.index.intersection(smart.index)
    baseline = baseline.loc[common]
    smart = smart.loc[common]
    baseline_sur_delay = metric_or_nan(baseline, "surrounding_veh_avg_delay_sec", common)
    smart_sur_delay = metric_or_nan(smart, "surrounding_veh_avg_delay_sec", common)
    baseline_sur_queue = metric_or_nan(baseline, "surrounding_queue_total_max", common)
    smart_sur_queue = metric_or_nan(smart, "surrounding_queue_total_max", common)
    baseline_adj_queue = metric_or_nan(baseline, "adjacent_tls_queue_total_max", common)
    smart_adj_queue = metric_or_nan(smart, "adjacent_tls_queue_total_max", common)

    table = pd.DataFrame(
        {
            "횡단보도ID": common,
            "행정동": baseline["행정동"].values,
            "읍면동명": baseline["읍면동명"].values,
            "노인비율": baseline.get("노인비율", pd.Series(index=common, dtype=float)).values,
            "baseline_PET_B_고위험": baseline.get("PET_B_surrogate_severe", pd.Series(index=common, dtype=float)).values,
            "smart_PET_B_고위험": smart.get("PET_B_surrogate_severe", pd.Series(index=common, dtype=float)).values,
            "PET_B_고위험_감소율_pct": pct_reduction(
                baseline.get("PET_B_surrogate_severe", pd.Series(index=common, dtype=float)),
                smart.get("PET_B_surrogate_severe", pd.Series(index=common, dtype=float)),
            ).values,
            "baseline_고령자미완료": baseline.get("elderly_incomplete_cross", pd.Series(index=common, dtype=float)).values,
            "smart_고령자미완료": smart.get("elderly_incomplete_cross", pd.Series(index=common, dtype=float)).values,
            "고령자미완료_감소율_pct": pct_reduction(
                baseline.get("elderly_incomplete_cross", pd.Series(index=common, dtype=float)),
                smart.get("elderly_incomplete_cross", pd.Series(index=common, dtype=float)),
            ).values,
            "baseline_차량평균지체_sec": baseline.get("veh_avg_delay_sec", pd.Series(index=common, dtype=float)).values,
            "smart_차량평균지체_sec": smart.get("veh_avg_delay_sec", pd.Series(index=common, dtype=float)).values,
            "차량평균지체_증가_sec": (
                smart.get("veh_avg_delay_sec", pd.Series(index=common, dtype=float))
                - baseline.get("veh_avg_delay_sec", pd.Series(index=common, dtype=float))
            ).values,
            "baseline_주변차량평균지체_sec": baseline_sur_delay.values,
            "smart_주변차량평균지체_sec": smart_sur_delay.values,
            "주변차량평균지체_증가_sec": (smart_sur_delay - baseline_sur_delay).values,
            "baseline_주변전체최대대기행렬": baseline_sur_queue.values,
            "smart_주변전체최대대기행렬": smart_sur_queue.values,
            "주변전체최대대기행렬_증가": (smart_sur_queue - baseline_sur_queue).values,
            "baseline_인접교차로최대대기행렬": baseline_adj_queue.values,
            "smart_인접교차로최대대기행렬": smart_adj_queue.values,
            "인접교차로최대대기행렬_증가": (smart_adj_queue - baseline_adj_queue).values,
        }
    )
    return table.sort_values("PET_B_고위험_감소율_pct", ascending=False, na_position="last")


def report_1(avg_df: pd.DataFrame, output_dir: Path) -> pd.DataFrame:
    report = avg_df.copy()
    report.to_csv(output_dir / "report_1_simulation_results.csv", index=False)
    return report


def write_comparison_report(comp: pd.DataFrame, output_dir: Path) -> None:
    if comp.empty:
        body = "비교 가능한 baseline/smart 결과가 없습니다.\n"
    else:
        pet_reduction = mean_or_nan(comp["PET_B_고위험_감소율_pct"])
        delay_delta = mean_or_nan(comp["차량평균지체_증가_sec"])
        body = f"""# 일반 횡단보도 vs 스마트 횡단보도 시뮬레이션 비교 보고서

- 분석 횡단보도 수: {len(comp)}
- PET_B 고위험 감소율 평균: {pet_reduction:.2f}%
- 차량 평균 지체 증가(접근부): {delay_delta:.2f} sec

{df_to_markdown(comp.head(20))}
"""
    (output_dir / "report_2_comparison.md").write_text(body, encoding="utf-8")


def write_tradeoff_report(comp: pd.DataFrame, output_dir: Path) -> None:
    body = "# 노인 보행 안전 vs 차량 지체 트레이드오프 분석\n\n"
    if comp.empty:
        body += "비교 가능한 결과가 없습니다.\n"
    else:
        body += df_to_markdown(
            comp[
                [
                    "횡단보도ID",
                    "행정동",
                    "PET_B_고위험_감소율_pct",
                    "차량평균지체_증가_sec",
                    "주변차량평균지체_증가_sec",
                    "주변전체최대대기행렬_증가",
                ]
            ]
        )
    (output_dir / "report_3_tradeoff.md").write_text(body, encoding="utf-8")


def model_parameter_markdown(model_params: dict[str, dict[str, Any]]) -> str:
    rows = []
    for name, meta in model_params.items():
        rows.append(
            {
                "parameter": name,
                "value": json.dumps(meta.get("value"), ensure_ascii=False)
                if isinstance(meta.get("value"), (dict, list))
                else meta.get("value"),
                "unit": meta.get("unit"),
                "source_type": meta.get("source_type"),
                "source_note": meta.get("source_note"),
                "fallback": meta.get("fallback"),
            }
        )
    return df_to_markdown(pd.DataFrame(rows))


def parse_osm_poi_from_net_dirs(nets_dir: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    bus_stops: list[dict[str, Any]] = []
    subway_ents: list[dict[str, Any]] = []
    for osm in nets_dir.glob("cw_*/map.osm"):
        try:
            root = ET.parse(osm).getroot()
        except Exception:
            continue
        for node in root.findall("node"):
            tags = {t.attrib.get("k"): t.attrib.get("v") for t in node.findall("tag")}
            lat = float(node.attrib.get("lat", "0"))
            lon = float(node.attrib.get("lon", "0"))
            name = tags.get("name", "")
            if tags.get("highway") == "bus_stop":
                bus_stops.append({"lat": lat, "lon": lon, "name": name})
            if tags.get("railway") == "subway_entrance":
                subway_ents.append({"lat": lat, "lon": lon, "name": name})
    return bus_stops, subway_ents


def write_figures(
    delta_summary: pd.DataFrame,
    figures_dir: Path,
) -> None:
    figures_dir.mkdir(parents=True, exist_ok=True)
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        return

    plt.figure(figsize=(8, 6))
    if delta_summary.empty:
        plt.text(0.5, 0.5, "pending / not_run", ha="center", va="center", fontsize=16)
        plt.xlim(0, 1)
        plt.ylim(0, 1)
        plt.xticks([])
        plt.yticks([])
    else:
        x_vals = pd.to_numeric(delta_summary["avg_vehicle_delay_delta_sec"], errors="coerce")
        y_vals = pd.to_numeric(delta_summary["safety_risk_delta"], errors="coerce")
        plt.scatter(
            x_vals,
            y_vals,
            color="#1f5aa6",
            s=70,
            alpha=0.85,
        )
        for row in delta_summary.itertuples(index=False):
            plt.annotate(
                str(row.crosswalk_id),
                (row.avg_vehicle_delay_delta_sec, row.safety_risk_delta),
                xytext=(4, 4),
                textcoords="offset points",
                fontsize=8,
            )
        plt.axhline(0, color="gray", linewidth=1)
        plt.axvline(0, color="gray", linewidth=1)
        plt.xlabel("avg_vehicle_delay_delta_sec")
        plt.ylabel("safety_risk_delta")
    plt.title("5초 보행녹색 연장에 따른 안전-지체 Trade-off")
    plt.tight_layout()
    plt.savefig(figures_dir / "tradeoff_summary.png", dpi=160)
    plt.close()


def safe_div(a: float, b: float) -> float:
    return float(a / b) if b and not math.isclose(b, 0.0) else float("nan")


def build_seed_level_tradeoff(seed_df: pd.DataFrame) -> pd.DataFrame:
    if seed_df.empty:
        return pd.DataFrame()
    required = {"crosswalk_id", "seed", "scenario"}
    if not required.issubset(seed_df.columns):
        return pd.DataFrame()
    base = seed_df[seed_df["scenario"] == "baseline"].copy()
    smart = seed_df[seed_df["scenario"] == "smart"].copy()
    keys = ["crosswalk_id", "seed"]
    merged = base.merge(smart, on=keys, suffixes=("_baseline", "_smart"))
    if merged.empty:
        return pd.DataFrame()

    out = pd.DataFrame()
    out["candidate_id"] = merged["crosswalk_id"].astype(str)
    out["seed"] = merged["seed"]
    out["scenario"] = "baseline_vs_smart"

    out["pedestrian_waiting_time_mean_baseline"] = pd.to_numeric(
        merged.get("average_pedestrian_wait_time_baseline"), errors="coerce"
    )
    out["pedestrian_waiting_time_mean_smart"] = pd.to_numeric(
        merged.get("average_pedestrian_wait_time_smart"), errors="coerce"
    )
    out["pedestrian_waiting_time_mean"] = out["pedestrian_waiting_time_mean_smart"]
    out["pedestrian_waiting_time_p95"] = np.nan
    out["vehicle_mean_time_loss"] = pd.to_numeric(merged.get("veh_avg_delay_sec_smart"), errors="coerce")
    out["vehicle_mean_travel_time"] = pd.to_numeric(
        merged.get("surrounding_avg_travel_time_sec_smart"), errors="coerce"
    )
    out["vehicle_mean_speed"] = pd.to_numeric(merged.get("surrounding_mean_speed_mps_smart"), errors="coerce")
    out["throughput"] = pd.to_numeric(merged.get("surrounding_throughput_veh_per_hour_smart"), errors="coerce")

    b_wait = out["pedestrian_waiting_time_mean_baseline"]
    s_wait = out["pedestrian_waiting_time_mean_smart"]
    b_loss = pd.to_numeric(merged.get("veh_avg_delay_sec_baseline"), errors="coerce")
    s_loss = pd.to_numeric(merged.get("veh_avg_delay_sec_smart"), errors="coerce")
    b_spd = pd.to_numeric(merged.get("surrounding_mean_speed_mps_baseline"), errors="coerce")
    s_spd = pd.to_numeric(merged.get("surrounding_mean_speed_mps_smart"), errors="coerce")

    out["safety_metric_delta_abs"] = b_wait - s_wait
    out["safety_metric_delta_pct"] = (b_wait - s_wait) / b_wait.replace(0, np.nan) * 100.0
    out["traffic_metric_delta_abs"] = s_loss - b_loss
    out["traffic_metric_delta_pct"] = (s_loss - b_loss) / b_loss.replace(0, np.nan) * 100.0
    speed_drop_pct = (b_spd - s_spd) / b_spd.replace(0, np.nan) * 100.0
    out["safety_improvement_pct"] = out["safety_metric_delta_pct"]
    out["traffic_degradation_pct"] = (out["traffic_metric_delta_pct"] + speed_drop_pct) / 2.0
    out["tradeoff_ratio"] = out["safety_improvement_pct"] / out["traffic_degradation_pct"].replace(0, np.nan)

    out["pedestrian_completed_count"] = pd.to_numeric(merged.get("PET_A_proxy_count_smart"), errors="coerce")
    out["pedestrian_unserved_count"] = pd.to_numeric(merged.get("elderly_incomplete_cross_smart"), errors="coerce")
    out["pedestrian_red_wait_exposure"] = pd.to_numeric(merged.get("max_pedestrian_wait_time_smart"), errors="coerce")
    out["pedestrian_green_extension_count"] = pd.to_numeric(
        merged.get("pedestrian_green_extension_count_smart"), errors="coerce"
    )
    out["pedestrian_green_shortage_count"] = pd.to_numeric(
        merged.get("PET_B_surrogate_severe_smart"), errors="coerce"
    )
    out["pedestrian_safety_score"] = pd.to_numeric(merged.get("safety_risk_score_smart"), errors="coerce")

    out["vehicle_count"] = pd.to_numeric(merged.get("total_vehicle_arrivals_smart"), errors="coerce")
    out["mean_travel_time"] = out["vehicle_mean_travel_time"]
    out["mean_delay"] = pd.to_numeric(merged.get("veh_avg_delay_sec_smart"), errors="coerce")
    out["mean_time_loss"] = pd.to_numeric(merged.get("avg_vehicle_delay_sec_smart"), errors="coerce")
    out["mean_waiting_time"] = pd.to_numeric(merged.get("surrounding_veh_avg_delay_sec_smart"), errors="coerce")
    out["mean_speed"] = out["vehicle_mean_speed"]
    out["queue_length_mean"] = pd.to_numeric(merged.get("surrounding_queue_total_avg_smart"), errors="coerce")
    out["queue_length_p95"] = np.nan
    out["stop_count_mean"] = pd.to_numeric(merged.get("queue_avg_smart"), errors="coerce")
    out["arrived_count"] = pd.to_numeric(merged.get("network_arrived_vehicles_smart"), errors="coerce")
    out["teleported_count"] = np.nan
    out["vehicle_efficiency_score"] = out["throughput"] / out["mean_delay"].replace(0, np.nan)
    return out


def build_candidate_quality_report(candidates: pd.DataFrame, nets_dir: Path, seed_tradeoff: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    by_cand = seed_tradeoff.groupby("candidate_id").size().to_dict() if not seed_tradeoff.empty else {}
    for row in candidates.itertuples(index=False):
        cw = str(getattr(row, "crosswalk_id"))
        cw_dir = nets_dir / f"cw_{cw}"
        metadata_path = cw_dir / "metadata.json"
        net_path = cw_dir / "network.net.xml"
        route_exists = any(cw_dir.glob("routes_seed*.rou.xml"))
        ped_exists = any(cw_dir.glob("peds_seed*.rou.xml"))
        baseline_exists = any(cw_dir.glob("baseline_seed*.sumocfg"))
        smart_exists = any(cw_dir.glob("smart_seed*.sumocfg"))
        md = {}
        if metadata_path.exists():
            try:
                md = json.loads(metadata_path.read_text(encoding="utf-8"))
            except Exception:
                md = {}
        has_vehicle_edges = bool(md.get("vehicle_conflict_edges"))
        has_ped_route = bool(md.get("ped_route", {}).get("from_edge")) and bool(md.get("ped_route", {}).get("to_edge"))
        has_tls = bool(md.get("tls_id"))
        has_crossing = bool(md.get("crossing_edge"))
        ran_pairs = int(by_cand.get(cw, 0))
        if all([net_path.exists(), route_exists, ped_exists, baseline_exists, smart_exists, has_vehicle_edges, has_ped_route, has_tls, has_crossing, ran_pairs > 0]):
            grade = "A"
        elif net_path.exists() and (route_exists or ped_exists):
            grade = "B"
        else:
            grade = "C"
        rows.append(
            {
                "candidate_id": cw,
                "network_exists": int(net_path.exists()),
                "vehicle_edge_connected": int(has_vehicle_edges),
                "pedestrian_path_exists": int(has_ped_route),
                "signalized_junction_exists": int(has_tls),
                "crossing_exists": int(has_crossing),
                "vehicle_route_exists": int(route_exists),
                "pedestrian_route_exists": int(ped_exists),
                "baseline_cfg_exists": int(baseline_exists),
                "smart_cfg_exists": int(smart_exists),
                "seed_pairs_ran": ran_pairs,
                "quality_grade": grade,
                "warning": "" if grade == "A" else "structure weak or run missing",
            }
        )
    return pd.DataFrame(rows)


def write_required_outputs(
    output_dir: Path,
    figures_dir: Path,
    candidates_csv: Path | None,
    nets_dir: Path | None,
    seed_df: pd.DataFrame,
) -> None:
    results_dir = Path.cwd() / "results"
    docs_dir = Path.cwd() / "docs"
    figs_dir = results_dir / "figures"
    results_dir.mkdir(parents=True, exist_ok=True)
    docs_dir.mkdir(parents=True, exist_ok=True)
    figs_dir.mkdir(parents=True, exist_ok=True)

    seed_tradeoff = build_seed_level_tradeoff(seed_df)
    seed_tradeoff.to_csv(results_dir / "baseline_smart_seed_results.csv", index=False)

    summary = (
        seed_tradeoff.groupby("candidate_id", as_index=False)
        .agg(
            safety_improvement_pct=("safety_improvement_pct", "mean"),
            safety_improvement_std=("safety_improvement_pct", "std"),
            traffic_degradation_pct=("traffic_degradation_pct", "mean"),
            traffic_degradation_std=("traffic_degradation_pct", "std"),
            tradeoff_ratio=("tradeoff_ratio", "mean"),
            throughput=("throughput", "mean"),
            vehicle_mean_time_loss=("vehicle_mean_time_loss", "mean"),
            vehicle_mean_travel_time=("vehicle_mean_travel_time", "mean"),
            vehicle_mean_speed=("vehicle_mean_speed", "mean"),
            pedestrian_waiting_time_mean=("pedestrian_waiting_time_mean", "mean"),
            pedestrian_waiting_time_p95=("pedestrian_waiting_time_p95", "mean"),
        )
        if not seed_tradeoff.empty
        else pd.DataFrame()
    )
    summary.to_csv(results_dir / "baseline_smart_summary.csv", index=False)

    quality = pd.DataFrame()
    if candidates_csv and candidates_csv.exists() and nets_dir and nets_dir.exists():
        candidates = pd.read_csv(candidates_csv)
        quality = build_candidate_quality_report(candidates, nets_dir, seed_tradeoff)
    quality.to_csv(results_dir / "candidate_quality_report.csv", index=False)

    tradeoff_summary = summary.copy()
    tradeoff_summary.to_csv(results_dir / "tradeoff_summary.csv", index=False)

    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        if not summary.empty:
            s = summary.sort_values("safety_improvement_pct", ascending=False)
            plt.figure(figsize=(10, 5))
            plt.bar(s["candidate_id"].astype(str), s["safety_improvement_pct"])
            plt.xticks(rotation=60, ha="right")
            plt.tight_layout()
            plt.savefig(figs_dir / "safety_improvement_by_candidate.png", dpi=160)
            plt.close()

            t = summary.sort_values("traffic_degradation_pct", ascending=False)
            plt.figure(figsize=(10, 5))
            plt.bar(t["candidate_id"].astype(str), t["traffic_degradation_pct"], color="#c24")
            plt.xticks(rotation=60, ha="right")
            plt.tight_layout()
            plt.savefig(figs_dir / "traffic_degradation_by_candidate.png", dpi=160)
            plt.close()

            plt.figure(figsize=(7, 6))
            plt.scatter(summary["traffic_degradation_pct"], summary["safety_improvement_pct"], color="#1f5aa6")
            for r in summary.itertuples(index=False):
                plt.annotate(str(r.candidate_id), (r.traffic_degradation_pct, r.safety_improvement_pct), fontsize=8)
            plt.xlabel("traffic_degradation_pct")
            plt.ylabel("safety_improvement_pct")
            plt.tight_layout()
            plt.savefig(figs_dir / "safety_vs_traffic_tradeoff.png", dpi=160)
            plt.close()
        else:
            for name in [
                "safety_improvement_by_candidate.png",
                "traffic_degradation_by_candidate.png",
                "safety_vs_traffic_tradeoff.png",
            ]:
                plt.figure(figsize=(7, 4))
                plt.text(0.5, 0.5, "no comparable baseline/smart result", ha="center", va="center")
                plt.xlim(0, 1)
                plt.ylim(0, 1)
                plt.xticks([])
                plt.yticks([])
                plt.tight_layout()
                plt.savefig(figs_dir / name, dpi=160)
                plt.close()
    except Exception:
        pass

    sim_md = (
        "# simulation_methodology\n\n"
        "본 분석은 중구 OSM 기반 SUMO 변환망에서 baseline/smart 상대 비교 실험을 수행했다.\n"
        "안전성은 사고 발생이 아닌 보행자 대기/지연/녹색부족 proxy로 평가했다.\n"
    )
    (docs_dir / "simulation_methodology.md").write_text(sim_md, encoding="utf-8")

    net_md = "# network_quality_report\n\n"
    if not quality.empty:
        net_md += df_to_markdown(quality)
    else:
        net_md += "candidate quality data unavailable."
    (docs_dir / "network_quality_report.md").write_text(net_md, encoding="utf-8")

    if not seed_tradeoff.empty:
        wait_imp = float(seed_tradeoff["safety_improvement_pct"].mean())
        wait_std = float(seed_tradeoff["safety_improvement_pct"].std())
        loss_inc = float(seed_tradeoff["traffic_metric_delta_pct"].mean())
        spd_drop = float(((seed_tradeoff["traffic_degradation_pct"] * 2.0) - seed_tradeoff["traffic_metric_delta_pct"]).mean())
        interp = (
            f"스마트 횡단보도 적용 시, 전체 후보지 평균 보행자 대기시간은 baseline 대비 {wait_imp:.1f}% 감소하였다 "
            f"(표준편차 {wait_std:.1f}%p). 반면 차량 평균 timeLoss는 {loss_inc:.1f}% 증가하고 "
            f"평균속도는 {spd_drop:.1f}% 감소하였다. 따라서 본 시뮬레이션은 안전성 대리지표 개선과 "
            f"교통 효율 비용 간 trade-off를 보였다."
        )
    else:
        interp = "실행 결과가 없어 해석 문장을 생성하지 못했다."
    (docs_dir / "final_interpretation.md").write_text(interp + "\n", encoding="utf-8")


def generate_all_reports(
    output_dir: str | Path = "outputs",
    figures_dir: str | Path = "figures",
    candidates_csv: str | Path | None = None,
    nets_dir: str | Path | None = None,
    model_parameters_path: str | Path | None = None,
) -> dict[str, pd.DataFrame]:
    output_dir = Path(output_dir)
    figures_dir = Path(figures_dir)
    avg_path = output_dir / "simulation_results.csv"
    seed_path = output_dir / "simulation_results_seed.csv"
    if not avg_path.exists():
        raise FileNotFoundError(f"{avg_path}가 없습니다. 먼저 시뮬레이션을 실행하세요.")

    try:
        avg_df = pd.read_csv(avg_path)
    except EmptyDataError:
        avg_df = pd.DataFrame(columns=["crosswalk_id","admin_dong","dong_name","scenario"])
    try:
        seed_df = pd.read_csv(seed_path)
    except (EmptyDataError, FileNotFoundError):
        seed_df = pd.DataFrame(columns=["crosswalk_id", "scenario", "seed"])
    model_params = load_model_parameters(model_parameters_path)
    simulation_summary = build_simulation_summary(avg_df, seed_df)
    delta_summary = build_baseline_vs_smart_summary(simulation_summary)
    simulation_summary.to_csv(output_dir / "simulation_summary.csv", index=False)
    delta_summary.to_csv(output_dir / "baseline_vs_smart_summary.csv", index=False)
    write_methodology_report(output_dir, model_params, simulation_summary, delta_summary)
    write_figures(delta_summary, figures_dir)
    write_required_outputs(
        output_dir,
        figures_dir,
        Path(candidates_csv) if candidates_csv else None,
        Path(nets_dir) if nets_dir else None,
        seed_df,
    )
    return {
        "simulation_summary": simulation_summary,
        "baseline_vs_smart_summary": delta_summary,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output_dir", default="outputs")
    parser.add_argument("--figures_dir", default="figures")
    parser.add_argument("--candidates", default="outputs/candidates.csv")
    parser.add_argument("--nets_dir", default="sumo_nets")
    parser.add_argument("--model_parameters", default=None)
    args = parser.parse_args()
    generate_all_reports(
        args.output_dir,
        args.figures_dir,
        args.candidates,
        args.nets_dir,
        args.model_parameters,
    )


if __name__ == "__main__":
    main()
