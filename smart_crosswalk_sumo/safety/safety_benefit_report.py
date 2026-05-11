"""TAAS 기반 severity cutoff 적용 및 안전 편익 요약 리포트.

이 모듈은 SUMO/TraCI 없이 독립 실행 가능하다.
입력: risk_events CSV (baseline + smart_selected) + TAAS calibration targets JSON
출력:
  - safety_benefit_summary.csv
  - safety_severity_by_scenario.csv
  - safety_calibration_metadata.json
  - safety_benefit_report.md
"""
from __future__ import annotations

import argparse
import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

try:
    from .injury_severity_model import classify_severity
except ImportError:
    from injury_severity_model import classify_severity  # type: ignore

_SEVERITY_ORDER = ["fatal", "serious", "minor", "injury_report"]

_DEFAULT_TAAS_DIST = {
    "fatal": 0.0115,
    "serious": 0.5385,
    "minor": 0.3977,
    "injury_report": 0.0524,
}

_REQUIRED_RISK_COLS = [
    "time_s", "crosswalk_id", "pedestrian_id", "vehicle_id",
    "edge_id", "lane_id", "vehicle_speed_mps", "min_distance_m", "ttc_s",
    "signal_state", "is_senior", "ped_remaining_crossing_time_s", "risk_score",
]

_REPORT_STATEMENT = (
    "본 연구는 스마트 횡단보도 적용 전 기본 상태의 현실 모사력을 확보하기 위해 "
    "TAAS 기반 서울특별시 중구 보행자 사고 데이터를 활용하였다. "
    "동일한 위험도-상해정도 매핑 기준을 baseline/smart 시나리오에 고정 적용하여 "
    "정책 적용 전후의 상대적 안전 편익을 비교하였다."
)


# ---------------------------------------------------------------------------
# Cutoff calibration
# ---------------------------------------------------------------------------

def calibrate_severity_cutoffs_from_scores(
    scores: np.ndarray,
    target_dist: dict[str, float],
) -> tuple[dict[str, float], str]:
    """risk_score 분포에서 TAAS 비율 기반 분위수 cutoff를 산출한다.

    Returns
    -------
    cutoffs : dict  — fatal_min, serious_min, minor_min, injury_report_min
    mode    : str   — 'calibrated_from_input_scores' | 'fallback_uniform_taas'
    """
    dist = {k: float(target_dist.get(k, 0.0)) for k in _SEVERITY_ORDER}
    total = sum(dist.values())
    if total > 0:
        dist = {k: v / total for k, v in dist.items()}

    if len(scores) >= 5:
        s = np.sort(scores)[::-1]
        n = len(s)
        cutoffs: dict[str, float] = {}
        cum = 0.0
        for label in _SEVERITY_ORDER:
            rate = dist[label]
            idx = max(0, int(round((cum + rate) * n)) - 1)
            cutoffs[f"{label}_min"] = float(s[min(idx, n - 1)])
            cum += rate
        return cutoffs, "calibrated_from_input_scores"
    else:
        # 입력 score가 없거나 너무 작으면 TAAS 비율을 0-1 균등 분포에 적용한 기본값 사용
        cutoffs = {}
        cum = 0.0
        for label in _SEVERITY_ORDER:
            rate = dist[label]
            cutoffs[f"{label}_min"] = round(1.0 - cum - rate, 6)
            cum += rate
        return cutoffs, "fallback_uniform_taas"


# ---------------------------------------------------------------------------
# Loading helpers
# ---------------------------------------------------------------------------

def _load_risk_events(paths: list[str | Path]) -> tuple[pd.DataFrame, list[str]]:
    """여러 CSV를 읽어 합친다. 빈 파일은 스키마 유지."""
    warnings: list[str] = []
    frames: list[pd.DataFrame] = []
    for p in paths:
        pp = Path(p)
        if not pp.exists():
            warnings.append(f"file_not_found:{pp}")
            continue
        try:
            df = pd.read_csv(pp)
            missing = [c for c in _REQUIRED_RISK_COLS if c not in df.columns]
            if missing:
                warnings.append(f"missing_columns:{pp}:{missing}")
            frames.append(df)
        except Exception as exc:
            warnings.append(f"read_error:{pp}:{exc}")
    if not frames:
        empty = pd.DataFrame(columns=_REQUIRED_RISK_COLS)
        return empty, warnings
    combined = pd.concat(frames, ignore_index=True)
    return combined, warnings


def _load_taas_targets(path: str | Path) -> tuple[dict[str, float], dict[str, Any]]:
    """TAAS JSON에서 severity_distribution과 메타를 읽는다."""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        target = data.get("recommended_calibration_target") or data.get("calibration_target") or {}
        dist = target.get("severity_distribution") or _DEFAULT_TAAS_DIST
        return {k: float(v) for k, v in dist.items()}, target
    except Exception:
        return dict(_DEFAULT_TAAS_DIST), {}


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------

def _scenario_stats(
    df: pd.DataFrame,
    scenario: str,
    cutoffs: dict[str, float],
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "scenario": scenario,
        "total_risk_events": 0,
        "senior_risk_events": 0,
        "mean_risk_score": None,
        "p95_risk_score": None,
        "warning": "",
    }
    for s in _SEVERITY_ORDER:
        row[f"{s}_count"] = 0
        row[f"{s}_rate"] = None

    if df.empty or "risk_score" not in df.columns:
        row["warning"] = "no_risk_events_for_scenario"
        return row

    scores = df["risk_score"].dropna().to_numpy(dtype=float)
    if len(scores) == 0:
        row["warning"] = "no_risk_events_for_scenario"
        return row

    row["total_risk_events"] = int(len(scores))
    row["mean_risk_score"] = round(float(np.mean(scores)), 6)
    row["p95_risk_score"] = round(float(np.percentile(scores, 95)), 6)

    if "is_senior" in df.columns:
        row["senior_risk_events"] = int(df["is_senior"].fillna(False).astype(bool).sum())

    severity_labels = df["risk_score"].apply(lambda s: classify_severity(float(s), cutoffs))
    n = len(scores)
    for s in _SEVERITY_ORDER:
        cnt = int((severity_labels == s).sum())
        row[f"{s}_count"] = cnt
        row[f"{s}_rate"] = round(cnt / n, 6) if n > 0 else None

    return row


# ---------------------------------------------------------------------------
# Report generation
# ---------------------------------------------------------------------------

def generate_safety_benefit_report(
    baseline_paths: list[str | Path],
    smart_paths: list[str | Path],
    taas_targets_path: str | Path,
    output_dir: str | Path,
) -> dict[str, Path]:
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    taas_dist, taas_meta = _load_taas_targets(taas_targets_path)
    baseline_df, bl_warnings = _load_risk_events(baseline_paths)
    smart_df, sm_warnings = _load_risk_events(smart_paths)
    all_warnings = bl_warnings + sm_warnings

    # Cutoff calibration — use combined scores if available
    combined_scores_for_calibration = (
        baseline_df["risk_score"].dropna().to_numpy(dtype=float)
        if not baseline_df.empty and "risk_score" in baseline_df.columns
        else np.array([])
    )
    cutoffs, calibration_mode = calibrate_severity_cutoffs_from_scores(
        combined_scores_for_calibration, taas_dist
    )

    # Per-scenario statistics
    bl_stats = _scenario_stats(baseline_df, "baseline", cutoffs)
    sm_stats = _scenario_stats(smart_df, "smart_selected", cutoffs)

    severity_rows = [bl_stats, sm_stats]
    severity_df = pd.DataFrame(severity_rows)
    severity_path = out / "safety_severity_by_scenario.csv"
    severity_df.to_csv(severity_path, index=False, encoding="utf-8-sig")

    # Delta computation
    def _safe_delta(a: Any, b: Any) -> float | None:
        if a is None or b is None:
            return None
        try:
            return float(b) - float(a)
        except (TypeError, ValueError):
            return None

    def _safe_pct(baseline_val: Any, delta: Any) -> float | None:
        if baseline_val is None or delta is None:
            return None
        try:
            bv = float(baseline_val)
            return round(delta / bv * 100.0, 4) if bv != 0 else None
        except (TypeError, ValueError):
            return None

    n_bl = bl_stats["total_risk_events"]
    n_sm = sm_stats["total_risk_events"]
    delta_total = _safe_delta(n_bl, n_sm) if (n_bl > 0 or n_sm > 0) else None
    reduction_pct = _safe_pct(n_bl, delta_total) if n_bl and n_bl > 0 else None

    summary_row: dict[str, Any] = {
        "baseline_total_risk_events": n_bl,
        "smart_total_risk_events": n_sm,
        "risk_event_delta": delta_total,
        "risk_event_reduction_pct": reduction_pct,
        "baseline_senior_risk_events": bl_stats["senior_risk_events"],
        "smart_senior_risk_events": sm_stats["senior_risk_events"],
        "senior_risk_event_delta": _safe_delta(bl_stats["senior_risk_events"], sm_stats["senior_risk_events"]),
        "baseline_mean_risk_score": bl_stats["mean_risk_score"],
        "smart_mean_risk_score": sm_stats["mean_risk_score"],
    }
    for s in _SEVERITY_ORDER:
        bl_c = bl_stats[f"{s}_count"]
        sm_c = sm_stats[f"{s}_count"]
        summary_row[f"baseline_{s}_count"] = bl_c
        summary_row[f"smart_{s}_count"] = sm_c
        summary_row[f"{s}_delta"] = _safe_delta(bl_c, sm_c)

    if bl_stats["warning"]:
        summary_row["baseline_warning"] = bl_stats["warning"]
    if sm_stats["warning"]:
        summary_row["smart_warning"] = sm_stats["warning"]

    summary_df = pd.DataFrame([summary_row])
    summary_path = out / "safety_benefit_summary.csv"
    summary_df.to_csv(summary_path, index=False, encoding="utf-8-sig")

    # Calibration metadata
    calib_meta: dict[str, Any] = {
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "taas_source": str(taas_targets_path),
        "taas_scope": taas_meta.get("scope", "junggu_crosswalk_all_years"),
        "taas_n_accidents": taas_meta.get("n_accidents"),
        "taas_severity_distribution": taas_dist,
        "calibration_mode": calibration_mode,
        "n_scores_used_for_calibration": int(len(combined_scores_for_calibration)),
        "severity_cutoffs": cutoffs,
        "warnings": all_warnings,
        "design_note": (
            "Cutoffs are calibrated from baseline risk_score distribution against TAAS target ratios. "
            "The same cutoffs are applied to both baseline and smart scenarios without recalibration. "
            "This allows relative comparison, not absolute accident count estimation."
        ),
    }
    calib_path = out / "safety_calibration_metadata.json"
    calib_path.write_text(json.dumps(calib_meta, ensure_ascii=False, indent=2), encoding="utf-8")

    # Markdown report
    report_lines = [
        "# TAAS 기반 안전 편익 분석 리포트",
        "",
        f"> {_REPORT_STATEMENT}",
        "",
        f"생성일시: {calib_meta['generated_at']}",
        "",
        "## 1. TAAS Calibration 기준",
        "",
        f"- scope: `{calib_meta['taas_scope']}`",
        f"- n_accidents: {calib_meta['taas_n_accidents']}",
        f"- calibration_mode: `{calibration_mode}`",
        f"- n_scores_used: {calib_meta['n_scores_used_for_calibration']}",
        "",
        "| Severity | TAAS 비율 | Cutoff (min) |",
        "|---|---:|---:|",
    ]
    for s in _SEVERITY_ORDER:
        dist_v = taas_dist.get(s, 0.0)
        cut_v = cutoffs.get(f"{s}_min", "N/A")
        cut_str = f"{cut_v:.4f}" if isinstance(cut_v, float) else str(cut_v)
        report_lines.append(f"| {s} | {dist_v*100:.2f}% | {cut_str} |")

    report_lines += [
        "",
        "## 2. 시나리오별 위험 이벤트 요약",
        "",
        "| 지표 | baseline | smart_selected | delta |",
        "|---|---:|---:|---:|",
        f"| total_risk_events | {n_bl} | {n_sm} | {delta_total if delta_total is not None else 'N/A'} |",
        f"| senior_risk_events | {bl_stats['senior_risk_events']} | {sm_stats['senior_risk_events']} | {_safe_delta(bl_stats['senior_risk_events'], sm_stats['senior_risk_events'])} |",
        f"| mean_risk_score | {bl_stats['mean_risk_score']} | {sm_stats['mean_risk_score']} | N/A |",
        f"| risk_event_reduction_pct | — | — | {reduction_pct}% |",
        "",
        "| Severity | baseline | smart_selected | delta |",
        "|---|---:|---:|---:|",
    ]
    for s in _SEVERITY_ORDER:
        bl_c = bl_stats[f"{s}_count"]
        sm_c = sm_stats[f"{s}_count"]
        d = _safe_delta(bl_c, sm_c)
        report_lines.append(f"| {s} | {bl_c} | {sm_c} | {d} |")

    report_lines += [
        "",
        "## 3. 경고 및 한계",
        "",
    ]
    for w in all_warnings:
        report_lines.append(f"- ⚠️ {w}")
    if bl_stats["warning"]:
        report_lines.append(f"- baseline 시나리오: `{bl_stats['warning']}`")
    if sm_stats["warning"]:
        report_lines.append(f"- smart 시나리오: `{sm_stats['warning']}`")
    report_lines += [
        "",
        "### 과학적 타당성 한계",
        "",
        "- SUMO는 실제 사고를 재현하지 않으므로 위험 이벤트는 conflict 기반 추정값이다.",
        "- Severity cutoff는 입력 risk_score 분포로부터 TAAS 비율을 fitting한 것이며,",
        "  절대 사고 건수 일치를 목표로 하지 않는다.",
        "- 60초 이하 단기 smoke에서 도출된 cutoff는 신뢰도가 낮다.",
        "- 충분한 보행 이벤트가 수집된 장기 시뮬레이션 결과에만 최종 해석을 적용할 것.",
        "",
        "---",
        "",
        f"출력 디렉터리: `{out}`",
    ]

    md_path = out / "safety_benefit_report.md"
    md_path.write_text("\n".join(report_lines), encoding="utf-8")

    return {
        "safety_benefit_summary": summary_path,
        "safety_severity_by_scenario": severity_path,
        "safety_calibration_metadata": calib_path,
        "safety_benefit_report": md_path,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="TAAS 기반 안전 편익 리포트 생성 (SUMO 불필요)"
    )
    parser.add_argument(
        "--baseline_risk_events",
        nargs="+",
        required=True,
        help="baseline risk_events CSV 경로 (복수 지정 가능, 합산됨)",
    )
    parser.add_argument(
        "--smart_risk_events",
        nargs="+",
        required=True,
        help="smart_selected risk_events CSV 경로 (복수 지정 가능, 합산됨)",
    )
    parser.add_argument(
        "--taas_targets",
        required=True,
        help="data/taas_calibration_targets.json 경로",
    )
    parser.add_argument(
        "--output_dir",
        required=True,
        help="출력 디렉터리",
    )
    args = parser.parse_args()

    paths = generate_safety_benefit_report(
        baseline_paths=args.baseline_risk_events,
        smart_paths=args.smart_risk_events,
        taas_targets_path=args.taas_targets,
        output_dir=args.output_dir,
    )

    for key, p in paths.items():
        print(f"{key}: {p}")


if __name__ == "__main__":
    main()
