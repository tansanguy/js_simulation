"""Human-readable result exporter.

Reads raw simulation outputs from result/<run_name>/outputs/ and produces:

  human_readable/
    summary_report.md
    final_decision_table.csv
    safety_benefit_summary.csv
    traffic_cost_summary.csv
    per_crosswalk_summary.csv

  machine_readable/
    metrics_full.csv
    comparison_full.csv

  debug/        (copies of debug CSVs)
  logs/         (copies of log files)
  figures/      (created here if matplotlib is available)

Original files in outputs/ are never deleted.

CLI:
  python3 -m smart_crosswalk_sumo.reporting.export_human_readable_results \\
    --run_dir result/final_single_run_119055_s600
"""
from __future__ import annotations

import argparse
import json
import math
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _nan_to_none(v: Any) -> Any:
    if v is None:
        return None
    if isinstance(v, float) and math.isnan(v):
        return None
    return v


def _safe_float(v: Any) -> float | None:
    try:
        f = float(v)
        return None if math.isnan(f) else f
    except Exception:
        return None


def _load_csv(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame()
    try:
        return pd.read_csv(path, encoding="utf-8-sig")
    except Exception:
        try:
            return pd.read_csv(path, encoding="utf-8")
        except Exception:
            return pd.DataFrame()


def _load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        with path.open(encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _col(df: pd.DataFrame, col: str, row_idx: int = 0, default: Any = None) -> Any:
    if df.empty or col not in df.columns or len(df) <= row_idx:
        return default
    v = df.iloc[row_idx][col]
    return default if (isinstance(v, float) and math.isnan(v)) else v


def _row_by_scenario(df: pd.DataFrame, scenario: str) -> pd.Series | None:
    if df.empty or "scenario" not in df.columns:
        return None
    rows = df[df["scenario"] == scenario]
    return rows.iloc[0] if not rows.empty else None


def _find_smart_scenario(df: pd.DataFrame) -> str | None:
    if "scenario" not in df.columns:
        return None
    for name in ("smart_selected", "smart", "smart_crosswalk"):
        if (df["scenario"] == name).any():
            return name
    return None


# ---------------------------------------------------------------------------
# Simulation status detection
# ---------------------------------------------------------------------------

def _detect_simulation_status(run_dir: Path) -> str:
    """Return 'valid', 'partial', or 'invalid' based on available outputs."""
    out = run_dir / "outputs"
    seed_summary = _load_csv(out / "network_simulation_summary_seed.csv")
    if seed_summary.empty:
        return "invalid"
    smart = _find_smart_scenario(seed_summary)
    if smart is None:
        return "partial"
    bl = _row_by_scenario(seed_summary, "baseline")
    sm = _row_by_scenario(seed_summary, smart)
    if bl is None or sm is None:
        return "partial"
    return "valid"


# ---------------------------------------------------------------------------
# Score computation
# ---------------------------------------------------------------------------

def _compute_scores(
    bl: pd.Series | None,
    sm: pd.Series | None,
    meta: dict[str, Any],
) -> dict[str, Any]:
    """Compute safety/traffic/net scores from baseline and smart rows."""
    scores: dict[str, Any] = {
        "avg_vehicle_delay_baseline": None,
        "avg_vehicle_delay_smart": None,
        "avg_vehicle_delay_change": None,
        "avg_travel_time_baseline": None,
        "avg_travel_time_smart": None,
        "avg_travel_time_change": None,
        "throughput_change": None,
        "extension_count": None,
        "risk_event_count_bl": None,
        "risk_event_count_sm": None,
        "pedestrian_benefit_proxy": None,
        "safety_benefit_score": None,
        "traffic_cost_score": None,
        "net_benefit_score": None,
        "overall_score": None,
    }

    if bl is None or sm is None:
        return scores

    def _g(row: pd.Series, col: str) -> float | None:
        return _safe_float(row.get(col)) if col in row.index else None

    bl_delay = _g(bl, "avg_vehicle_delay_sec")
    sm_delay = _g(sm, "avg_vehicle_delay_sec")
    bl_tt = _g(bl, "network_avg_travel_time_sec")
    sm_tt = _g(sm, "network_avg_travel_time_sec")
    bl_arr = _g(bl, "network_arrived_vehicles")
    sm_arr = _g(sm, "network_arrived_vehicles")
    bl_risk = _g(bl, "risk_event_count") or 0.0
    sm_risk = _g(sm, "risk_event_count") or 0.0
    ext_count = _g(sm, "extension_count") or 0.0

    scores["avg_vehicle_delay_baseline"] = bl_delay
    scores["avg_vehicle_delay_smart"] = sm_delay
    scores["avg_vehicle_delay_change"] = (
        round(sm_delay - bl_delay, 4) if bl_delay is not None and sm_delay is not None else None
    )
    scores["avg_travel_time_baseline"] = bl_tt
    scores["avg_travel_time_smart"] = sm_tt
    scores["avg_travel_time_change"] = (
        round(sm_tt - bl_tt, 4) if bl_tt is not None and sm_tt is not None else None
    )
    scores["throughput_change"] = (
        round(sm_arr - bl_arr, 1) if bl_arr is not None and sm_arr is not None else None
    )
    scores["extension_count"] = ext_count
    scores["risk_event_count_bl"] = bl_risk
    scores["risk_event_count_sm"] = sm_risk

    # pedestrian_benefit_proxy: reduction in risk events (higher = more benefit)
    ped_proxy = (bl_risk - sm_risk) if (bl_risk > 0 or sm_risk > 0) else None
    scores["pedestrian_benefit_proxy"] = ped_proxy

    # elderly_accident_ratio from metadata or per_crosswalk CSV
    elderly_ratio = _safe_float(meta.get("elderly_ratio"))

    # safety_benefit_score: proxy-based composite
    if ped_proxy is not None and elderly_ratio is not None:
        scores["safety_benefit_score"] = round(
            max(0.0, ped_proxy) * (1 + elderly_ratio), 4
        )
    elif ext_count and ext_count > 0:
        scores["safety_benefit_score"] = round(float(ext_count) * 0.1, 4)
    else:
        scores["safety_benefit_score"] = 0.0

    # traffic_cost_score: delay increase normalized (lower = less cost)
    delay_change = scores["avg_vehicle_delay_change"]
    if delay_change is not None:
        scores["traffic_cost_score"] = round(max(0.0, delay_change), 4)
    else:
        scores["traffic_cost_score"] = None

    # net_benefit_score = safety_benefit - traffic_cost (normalized)
    saf = scores["safety_benefit_score"]
    tc = scores["traffic_cost_score"]
    if saf is not None and tc is not None:
        scores["net_benefit_score"] = round(float(saf) - float(tc) * 0.1, 4)
    else:
        scores["net_benefit_score"] = None

    # overall_score = net_benefit_score (can be refined later)
    scores["overall_score"] = scores["net_benefit_score"]

    return scores


def _recommend(
    sim_status: str,
    scores: dict[str, Any],
    ext_count: float | None,
) -> str:
    if sim_status == "invalid":
        return "Invalid"
    if sim_status == "partial":
        return "Invalid"
    net = scores.get("net_benefit_score")
    tc = scores.get("traffic_cost_score")
    saf = scores.get("safety_benefit_score")
    ext = ext_count or 0.0
    if net is None:
        return "Invalid"
    if ext == 0:
        return "Conditional"
    if net > 0 and (tc is None or tc < 5.0):
        return "Recommended"
    if net >= 0:
        return "Conditional"
    return "Not Recommended"


def _safety_interpretation(
    sim_status: str, saf_score: float | None, ext_count: float | None
) -> str:
    if sim_status == "invalid":
        return "Invalid simulation result."
    if ext_count == 0 or ext_count is None:
        return "No smart extension occurred; safety benefit not measurable in this run."
    if saf_score is None:
        return "Safety benefit could not be computed."
    if saf_score > 5:
        return "High expected safety benefit due to high elderly/severe injury risk weight."
    if saf_score > 1:
        return "Moderate safety benefit."
    return "Low safety benefit or insufficient evidence."


def _traffic_interpretation(
    sim_status: str, delay_change: float | None
) -> str:
    if sim_status == "invalid":
        return "Invalid simulation result."
    if delay_change is None:
        return "Traffic cost could not be computed."
    if delay_change < 2.0:
        return "Low traffic cost."
    if delay_change < 5.0:
        return "Moderate traffic cost."
    return "High traffic cost; additional validation required."


def _key_reason(recommendation: str, scores: dict[str, Any], ext_count: float | None) -> str:
    delay = scores.get("avg_vehicle_delay_change")
    saf = scores.get("safety_benefit_score")
    if recommendation == "Invalid":
        return "Simulation invalid due to TLS/network matching issue."
    if ext_count == 0 or ext_count is None:
        return "No signal extension triggered; safety impact not measurable."
    if recommendation == "Recommended":
        delay_str = f"{delay:.1f}s" if delay is not None else "N/A"
        return f"Large safety benefit with acceptable vehicle delay increase ({delay_str})."
    if recommendation == "Conditional":
        delay_str = f"{delay:.1f}s" if delay is not None else "N/A"
        return f"Safety benefit exists, but traffic delay increase is {delay_str}."
    return "Safety benefit is small or traffic cost is too high."


# ---------------------------------------------------------------------------
# Build human-readable CSVs
# ---------------------------------------------------------------------------

def _build_final_decision_table(
    crosswalk_ids: list[str],
    id_to_meta: dict[str, Any],
    id_to_scores: dict[str, Any],
    id_to_sim_status: dict[str, str],
) -> pd.DataFrame:
    rows = []
    ranked = sorted(
        crosswalk_ids,
        key=lambda cid: (
            -1 if id_to_scores.get(cid, {}).get("overall_score") is None
            else -(id_to_scores[cid]["overall_score"] or 0),
        ),
    )
    for rank, cid in enumerate(ranked, start=1):
        meta = id_to_meta.get(cid, {})
        scores = id_to_scores.get(cid, {})
        sim_status = id_to_sim_status.get(cid, "invalid")
        ext_count = scores.get("extension_count")
        rec = _recommend(sim_status, scores, ext_count)
        rows.append({
            "crosswalk_id": cid,
            "location_name": meta.get("dong_name") or meta.get("admin_dong") or "",
            "simulation_status": sim_status,
            "recommendation": rec,
            "rank": rank,
            "overall_score": scores.get("overall_score"),
            "safety_benefit_score": scores.get("safety_benefit_score"),
            "traffic_cost_score": scores.get("traffic_cost_score"),
            "net_benefit_score": scores.get("net_benefit_score"),
            "elderly_risk_weight": meta.get("elderly_ratio"),
            "severe_injury_weight": meta.get("severe_injury_ratio"),
            "avg_vehicle_delay_change": scores.get("avg_vehicle_delay_change"),
            "avg_travel_time_change": scores.get("avg_travel_time_change"),
            "pedestrian_benefit_proxy": scores.get("pedestrian_benefit_proxy"),
            "notes": _key_reason(rec, scores, ext_count),
        })
    return pd.DataFrame(rows)


def _build_safety_benefit_summary(
    crosswalk_ids: list[str],
    id_to_meta: dict[str, Any],
    id_to_scores: dict[str, Any],
    id_to_sim_status: dict[str, str],
) -> pd.DataFrame:
    rows = []
    for cid in crosswalk_ids:
        meta = id_to_meta.get(cid, {})
        scores = id_to_scores.get(cid, {})
        sim_status = id_to_sim_status.get(cid, "invalid")
        ext_count = scores.get("extension_count")
        saf = scores.get("safety_benefit_score")
        rows.append({
            "crosswalk_id": cid,
            "simulation_status": sim_status,
            "pedestrian_benefit_proxy": scores.get("pedestrian_benefit_proxy"),
            "elderly_accident_ratio": meta.get("elderly_ratio"),
            "severe_injury_ratio": meta.get("severe_injury_ratio"),
            "safety_benefit_score": saf,
            "safety_interpretation": _safety_interpretation(sim_status, saf, ext_count),
        })
    return pd.DataFrame(rows)


def _build_traffic_cost_summary(
    crosswalk_ids: list[str],
    id_to_scores: dict[str, Any],
    id_to_sim_status: dict[str, str],
) -> pd.DataFrame:
    rows = []
    for cid in crosswalk_ids:
        scores = id_to_scores.get(cid, {})
        sim_status = id_to_sim_status.get(cid, "invalid")
        delay_change = scores.get("avg_vehicle_delay_change")
        rows.append({
            "crosswalk_id": cid,
            "simulation_status": sim_status,
            "avg_vehicle_delay_baseline": scores.get("avg_vehicle_delay_baseline"),
            "avg_vehicle_delay_smart": scores.get("avg_vehicle_delay_smart"),
            "avg_vehicle_delay_change": delay_change,
            "avg_travel_time_baseline": scores.get("avg_travel_time_baseline"),
            "avg_travel_time_smart": scores.get("avg_travel_time_smart"),
            "avg_travel_time_change": scores.get("avg_travel_time_change"),
            "throughput_change": scores.get("throughput_change"),
            "traffic_cost_score": scores.get("traffic_cost_score"),
            "traffic_interpretation": _traffic_interpretation(sim_status, delay_change),
        })
    return pd.DataFrame(rows)


def _build_per_crosswalk_summary(
    crosswalk_ids: list[str],
    id_to_meta: dict[str, Any],
    id_to_scores: dict[str, Any],
    id_to_sim_status: dict[str, str],
) -> pd.DataFrame:
    rows = []
    ranked = sorted(
        crosswalk_ids,
        key=lambda cid: (
            -(id_to_scores.get(cid, {}).get("overall_score") or -9999),
        ),
    )
    for rank, cid in enumerate(ranked, start=1):
        meta = id_to_meta.get(cid, {})
        scores = id_to_scores.get(cid, {})
        sim_status = id_to_sim_status.get(cid, "invalid")
        ext_count = scores.get("extension_count")
        rec = _recommend(sim_status, scores, ext_count)
        rows.append({
            "crosswalk_id": cid,
            "location_name": meta.get("dong_name") or meta.get("admin_dong") or "",
            "simulation_status": sim_status,
            "rank": rank,
            "recommendation": rec,
            "safety_benefit_score": scores.get("safety_benefit_score"),
            "traffic_cost_score": scores.get("traffic_cost_score"),
            "net_benefit_score": scores.get("net_benefit_score"),
            "key_reason": _key_reason(rec, scores, ext_count),
        })
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# summary_report.md
# ---------------------------------------------------------------------------

def _build_summary_report(
    run_dir: Path,
    run_metadata: dict[str, Any],
    decision_df: pd.DataFrame,
    safety_df: pd.DataFrame,
    traffic_df: pd.DataFrame,
    sim_status_global: str,
) -> str:
    args = run_metadata.get("args", {})
    created_at = run_metadata.get("created_at", _now_iso())

    n_recommended = len(decision_df[decision_df["recommendation"] == "Recommended"]) if not decision_df.empty else 0
    n_conditional = len(decision_df[decision_df["recommendation"] == "Conditional"]) if not decision_df.empty else 0
    n_not_rec = len(decision_df[decision_df["recommendation"] == "Not Recommended"]) if not decision_df.empty else 0
    n_invalid = len(decision_df[decision_df["recommendation"] == "Invalid"]) if not decision_df.empty else 0

    top5 = decision_df.head(5) if not decision_df.empty else pd.DataFrame()
    top5_md = ""
    if not top5.empty:
        cols = ["crosswalk_id", "location_name", "recommendation",
                "safety_benefit_score", "traffic_cost_score", "net_benefit_score"]
        available = [c for c in cols if c in top5.columns]
        # Manual markdown table (avoids tabulate dependency)
        header = "| " + " | ".join(available) + " |"
        sep = "| " + " | ".join(["---"] * len(available)) + " |"
        rows_md = [header, sep]
        for _, row in top5[available].iterrows():
            rows_md.append("| " + " | ".join(str(row[c]) for c in available) + " |")
        top5_md = "\n".join(rows_md)

    # traffic warning
    any_delay_increase = False
    if not traffic_df.empty and "avg_vehicle_delay_change" in traffic_df.columns:
        changes = traffic_df["avg_vehicle_delay_change"].dropna()
        if len(changes) > 0 and changes.max() > 2.0:
            any_delay_increase = True

    # extension count warning
    no_extension = False
    if not decision_df.empty and "notes" in decision_df.columns:
        no_ext_notes = decision_df["notes"].str.contains("No signal extension", na=False)
        if no_ext_notes.any():
            no_extension = True

    main_interpretation = (
        "시뮬레이션 결과, 스마트 횡단보도 개입이 발생한 후보에 대해 안전 편익과 교통 비용의 trade-off가 측정되었다. "
        "안전 편익은 SUMO 위험 이벤트(risk event) 감소량과 고령자 사고 비율 가중치로 추정되었으며, "
        "교통 비용은 baseline 대비 차량 평균 지연시간 변화로 측정되었다. "
        "현재 결과는 후보 간 상대 비교를 위한 지표이며 절대적 사고 건수 예측이 아니다. "
        "최종 설치 우선순위 결정에는 현실 교통량 데이터 기반 calibration이 추가로 권장된다."
    )

    warnings = [
        "SUMO does not directly predict real accidents.",
        "Safety benefit is estimated through proxy metrics and external risk weights.",
        "Invalid TLS/network matching candidates should not be interpreted as policy failures.",
        "Additional calibration with real traffic and accident data is recommended.",
    ]
    if no_extension:
        warnings.append(
            "extension_count=0 for some candidates: smart signal extension was not triggered. "
            "Check debug/smart_extension_diagnostic for root cause."
        )
    if any_delay_increase:
        warnings.append(
            "Some candidates show vehicle delay increase > 2s. "
            "This is an ideal-condition estimate; real-world delay may be higher."
        )

    warnings_md = "\n".join(f"- {w}" for w in warnings)

    return f"""# Smart Crosswalk Simulation Summary

## Run Information

| Item | Value |
|---|---|
| Run name | `{run_dir.name}` |
| Simulation mode | `{args.get('simulation_mode', 'N/A')}` |
| Network mode | `{args.get('network_mode', 'N/A')}` |
| Buffer | `{args.get('buffer_m', 'N/A')} m` |
| Duration | `{args.get('sim_duration', 'N/A')} s` |
| Seeds | `{args.get('seeds', 'N/A')}` |
| Smart crosswalk IDs | `{args.get('smart_crosswalk_ids', 'N/A')}` |
| Simulation status | `{sim_status_global}` |
| Generated at | `{_now_iso()}` |
| Original run at | `{created_at}` |

---

## Key Result

| Category | Count |
|---|---|
| Recommended | {n_recommended} |
| Conditional | {n_conditional} |
| Not Recommended | {n_not_rec} |
| Invalid | {n_invalid} |

---

## Main Interpretation

{main_interpretation}

---

## Top Candidates

{top5_md if top5_md else "_No valid simulation data available._"}

_(See `final_decision_table.csv` for the full table.)_

---

## Safety Benefit Summary

- 안전 편익은 SUMO risk event 감소와 TAAS 기반 고령자/중상 가중치로 추정된다.
- `risk_event_count = 0`인 경우 안전 편익 정량 결론은 보류 상태다.
- `safety_benefit_score`는 후보 간 상대 비교용 지표이며 절대적 사고 건수 예측이 아니다.
- 상세 내용: `safety_benefit_summary.csv` 참고.

---

## Traffic Cost Summary

- 교통 비용은 baseline 대비 차량 평균 지연시간 변화(`avg_vehicle_delay_change`)로 측정된다.
- 지연 증가 < 2초: 교통 비용 낮음 (허용 가능)
- 지연 증가 2~5초: 교통 비용 중간 (추가 검토 필요)
- 지연 증가 > 5초: 교통 비용 높음 (신중 판단)
- 상세 내용: `traffic_cost_summary.csv` 참고.

---

## Warnings and Limitations

{warnings_md}
"""


# ---------------------------------------------------------------------------
# Figure generation
# ---------------------------------------------------------------------------

def _try_make_figures(
    decision_df: pd.DataFrame,
    figures_dir: Path,
) -> list[str]:
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        return ["matplotlib not available — figures skipped"]

    generated: list[str] = []
    figures_dir.mkdir(parents=True, exist_ok=True)

    # 1. safety_vs_traffic_tradeoff.png
    try:
        if not decision_df.empty and \
           "traffic_cost_score" in decision_df.columns and \
           "safety_benefit_score" in decision_df.columns:
            plot_df = decision_df.dropna(subset=["traffic_cost_score", "safety_benefit_score"])
            if not plot_df.empty:
                color_map = {
                    "Recommended": "#2ecc71",
                    "Conditional": "#f39c12",
                    "Not Recommended": "#e74c3c",
                    "Invalid": "#95a5a6",
                }
                fig, ax = plt.subplots(figsize=(8, 6))
                for rec, grp in plot_df.groupby("recommendation"):
                    ax.scatter(
                        grp["traffic_cost_score"],
                        grp["safety_benefit_score"],
                        label=rec,
                        color=color_map.get(rec, "#333333"),
                        s=80,
                        alpha=0.8,
                    )
                    for _, row in grp.iterrows():
                        ax.annotate(
                            str(row["crosswalk_id"]),
                            (row["traffic_cost_score"], row["safety_benefit_score"]),
                            fontsize=7,
                            textcoords="offset points",
                            xytext=(4, 4),
                        )
                ax.set_xlabel("Traffic Cost Score (lower = less cost)")
                ax.set_ylabel("Safety Benefit Score (higher = more benefit)")
                ax.set_title("Safety Benefit vs Traffic Cost Trade-off")
                ax.legend()
                fig.tight_layout()
                out_path = figures_dir / "safety_vs_traffic_tradeoff.png"
                fig.savefig(out_path, dpi=120, bbox_inches="tight")
                plt.close(fig)
                generated.append(str(out_path))
    except Exception as exc:
        generated.append(f"tradeoff_chart_error: {exc}")

    # 2. candidate_ranking.png
    try:
        if not decision_df.empty and "net_benefit_score" in decision_df.columns:
            plot_df = decision_df.dropna(subset=["net_benefit_score"]).head(20)
            if not plot_df.empty:
                color_map = {
                    "Recommended": "#2ecc71",
                    "Conditional": "#f39c12",
                    "Not Recommended": "#e74c3c",
                    "Invalid": "#95a5a6",
                }
                colors = [
                    color_map.get(r, "#333333")
                    for r in plot_df.get("recommendation", ["Invalid"] * len(plot_df))
                ]
                fig, ax = plt.subplots(figsize=(10, 5))
                ax.bar(
                    plot_df["crosswalk_id"].astype(str),
                    plot_df["net_benefit_score"],
                    color=colors,
                )
                ax.set_xlabel("Crosswalk ID")
                ax.set_ylabel("Net Benefit Score")
                ax.set_title("Candidate Ranking by Net Benefit Score (top 20)")
                ax.tick_params(axis="x", rotation=45)
                fig.tight_layout()
                out_path = figures_dir / "candidate_ranking.png"
                fig.savefig(out_path, dpi=120, bbox_inches="tight")
                plt.close(fig)
                generated.append(str(out_path))
    except Exception as exc:
        generated.append(f"ranking_chart_error: {exc}")

    return generated


# ---------------------------------------------------------------------------
# File classification and copy
# ---------------------------------------------------------------------------

_DEBUG_PATTERNS = [
    "pedestrian_connectivity_audit*",
    "invalid_pedestrian_*",
    "excluded_integrated_*",
    "network_*_debug*",
    "registry_*",
    "tls_*",
    "demand_source_audit*",
]

_LOG_PATTERNS = [
    "runtime_trace_*.log",
    "sumo_stdout_*.log",
    "sumo_stderr_*.log",
    "*.log",
]

_RAW_PATTERNS = [
    "edge_data_*.xml",
    "lane_data_*.xml",
    "risk_events_*.csv",
    "extension_events_*.csv",
    "incident_events_*.csv",
    "incident_impact_*.csv",
    "model_assumptions_used.csv",
    "calibration_report.csv",
    "calibration_summary.md",
    "report_*.md",
]


def _classify_and_copy(out_dir: Path, outputs_dir: Path) -> None:
    """Copy files from outputs_dir into classified subdirectories under out_dir."""
    if not outputs_dir.exists():
        return

    debug_dir = out_dir / "debug"
    logs_dir = out_dir / "logs"
    raw_dir = out_dir / "raw"
    machine_dir = out_dir / "machine_readable"

    for d in (debug_dir, logs_dir, raw_dir, machine_dir):
        d.mkdir(parents=True, exist_ok=True)

    classified: set[str] = set()

    def _copy_glob(pattern: str, dest: Path) -> None:
        for p in outputs_dir.glob(pattern):
            shutil.copy2(p, dest / p.name)
            classified.add(p.name)

    for pat in _DEBUG_PATTERNS:
        _copy_glob(pat, debug_dir)
    for pat in _LOG_PATTERNS:
        _copy_glob(pat, logs_dir)
    for pat in _RAW_PATTERNS:
        _copy_glob(pat, raw_dir)

    # machine_readable: everything not already classified and not a log/xml
    for f in outputs_dir.iterdir():
        if f.name in classified:
            continue
        if f.suffix in (".xml", ".log"):
            continue
        if f.suffix == ".csv":
            shutil.copy2(f, machine_dir / f.name)


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def export_human_readable_results(run_dir: Path) -> dict[str, Path]:
    """Create human-readable result files from raw simulation outputs.

    Args:
        run_dir: Path to result/<run_name>/ directory.

    Returns:
        Dict mapping output names to file paths.
    """
    run_dir = run_dir.resolve()
    out_dir = run_dir / "outputs"
    human_dir = run_dir / "human_readable"
    figures_dir = run_dir / "figures"
    human_dir.mkdir(parents=True, exist_ok=True)
    figures_dir.mkdir(parents=True, exist_ok=True)

    run_metadata = _load_json(run_dir / "run_metadata.json")
    seed_summary = _load_csv(out_dir / "network_simulation_summary_seed.csv")
    per_cw = _load_csv(out_dir / "per_crosswalk_simulation_results.csv")

    sim_status_global = _detect_simulation_status(run_dir)
    smart_scenario = _find_smart_scenario(seed_summary)

    bl_row = _row_by_scenario(seed_summary, "baseline")
    sm_row = _row_by_scenario(seed_summary, smart_scenario or "smart_selected")

    # Collect crosswalk IDs
    crosswalk_ids: list[str] = []
    if not per_cw.empty and "crosswalk_id" in per_cw.columns:
        crosswalk_ids = [str(x) for x in per_cw["crosswalk_id"].unique().tolist()]
    elif not seed_summary.empty:
        args = run_metadata.get("args", {})
        smart_ids = args.get("smart_crosswalk_ids") or []
        crosswalk_ids = [str(x) for x in smart_ids] if smart_ids else []

    # Build per-crosswalk metadata and scores
    id_to_meta: dict[str, Any] = {}
    id_to_scores: dict[str, Any] = {}
    id_to_sim_status: dict[str, str] = {}

    if not per_cw.empty and crosswalk_ids:
        for cid in crosswalk_ids:
            cid_rows = per_cw[per_cw["crosswalk_id"].astype(str) == cid]
            if cid_rows.empty:
                id_to_meta[cid] = {}
                id_to_scores[cid] = {}
                id_to_sim_status[cid] = "invalid"
                continue

            bl_cw = cid_rows[cid_rows["scenario"] == "baseline"].iloc[0] if "scenario" in cid_rows.columns and (cid_rows["scenario"] == "baseline").any() else None
            sm_cw = None
            for sc in ("smart_selected", "smart", "smart_crosswalk"):
                if "scenario" in cid_rows.columns and (cid_rows["scenario"] == sc).any():
                    sm_cw = cid_rows[cid_rows["scenario"] == sc].iloc[0]
                    break

            meta: dict[str, Any] = {}
            if bl_cw is not None:
                for col in ("admin_dong", "dong_name", "elderly_ratio", "lane_count", "max_speed_kph"):
                    v = bl_cw.get(col) if hasattr(bl_cw, "get") else (bl_cw[col] if col in bl_cw.index else None)
                    if v is not None and not (isinstance(v, float) and math.isnan(v)):
                        meta[col] = v

            id_to_meta[cid] = meta
            id_to_scores[cid] = _compute_scores(bl_cw, sm_cw, meta)
            id_to_sim_status[cid] = "valid" if (bl_cw is not None and sm_cw is not None) else "invalid"
    else:
        # No per-crosswalk CSV: use network-level summary
        for cid in crosswalk_ids:
            args = run_metadata.get("args", {})
            id_to_meta[cid] = {}
            id_to_scores[cid] = _compute_scores(bl_row, sm_row, {})
            id_to_sim_status[cid] = sim_status_global

    # If still no crosswalk IDs, create a dummy entry for network-level result
    if not crosswalk_ids:
        args = run_metadata.get("args", {})
        smart_ids = args.get("smart_crosswalk_ids") or ["unknown"]
        crosswalk_ids = [str(x) for x in smart_ids]
        for cid in crosswalk_ids:
            id_to_meta[cid] = {}
            id_to_scores[cid] = _compute_scores(bl_row, sm_row, {})
            id_to_sim_status[cid] = sim_status_global

    # Build DataFrames
    decision_df = _build_final_decision_table(
        crosswalk_ids, id_to_meta, id_to_scores, id_to_sim_status
    )
    safety_df = _build_safety_benefit_summary(
        crosswalk_ids, id_to_meta, id_to_scores, id_to_sim_status
    )
    traffic_df = _build_traffic_cost_summary(
        crosswalk_ids, id_to_scores, id_to_sim_status
    )
    per_cw_summary_df = _build_per_crosswalk_summary(
        crosswalk_ids, id_to_meta, id_to_scores, id_to_sim_status
    )

    # Save CSVs
    result: dict[str, Path] = {}

    decision_path = human_dir / "final_decision_table.csv"
    decision_df.to_csv(decision_path, index=False, encoding="utf-8-sig")
    result["final_decision_table"] = decision_path

    safety_path = human_dir / "safety_benefit_summary.csv"
    safety_df.to_csv(safety_path, index=False, encoding="utf-8-sig")
    result["safety_benefit_summary"] = safety_path

    traffic_path = human_dir / "traffic_cost_summary.csv"
    traffic_df.to_csv(traffic_path, index=False, encoding="utf-8-sig")
    result["traffic_cost_summary"] = traffic_path

    per_cw_path = human_dir / "per_crosswalk_summary.csv"
    per_cw_summary_df.to_csv(per_cw_path, index=False, encoding="utf-8-sig")
    result["per_crosswalk_summary"] = per_cw_path

    # summary_report.md
    report_md = _build_summary_report(
        run_dir, run_metadata, decision_df, safety_df, traffic_df, sim_status_global
    )
    report_path = human_dir / "summary_report.md"
    report_path.write_text(report_md, encoding="utf-8")
    result["summary_report"] = report_path

    # machine_readable: copy full CSVs
    machine_dir = run_dir / "machine_readable"
    machine_dir.mkdir(parents=True, exist_ok=True)
    for fname in ("network_simulation_summary_seed.csv", "network_simulation_summary.csv",
                  "per_crosswalk_simulation_results_seed.csv", "per_crosswalk_simulation_results.csv",
                  "baseline_vs_smart_summary.csv", "network_baseline_vs_smart_summary.csv"):
        src = out_dir / fname
        if src.exists():
            dst = machine_dir / fname
            shutil.copy2(src, dst)

    # Classify and copy remaining files
    _classify_and_copy(run_dir, out_dir)

    # Figures
    fig_paths = _try_make_figures(decision_df, figures_dir)
    for i, fp in enumerate(fig_paths):
        result[f"figure_{i}"] = Path(fp)

    return result


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Export human-readable results from a simulation run directory."
    )
    parser.add_argument(
        "--run_dir",
        required=True,
        help="Path to result/<run_name>/ directory",
    )
    args = parser.parse_args()

    run_dir = Path(args.run_dir)
    if not run_dir.exists():
        print(f"ERROR: run_dir not found: {run_dir}")
        return

    print(f"Exporting human-readable results from: {run_dir}")
    paths = export_human_readable_results(run_dir)
    print("\nGenerated files:")
    for key, p in paths.items():
        exists = "OK" if Path(p).exists() else "MISSING"
        print(f"  [{exists}] {key}: {p}")


if __name__ == "__main__":
    main()
