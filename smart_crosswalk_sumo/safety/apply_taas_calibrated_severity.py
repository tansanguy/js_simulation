"""baseline cutoff를 baseline/smart 양쪽에 적용해 severity를 분류하고 안전 편익을 비교한다.

설계 원칙:
- baseline에서 산출한 cutoff를 smart 시나리오에 재보정 없이 고정 적용
- 상대적 개선 효과(비율 변화)를 주요 지표로 사용
- 절대 사고 건수 일치가 목표가 아니라, 분포 구조 하에서 정책 효과 비교가 목표
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import pandas as pd

try:
    from .injury_severity_model import classify_severity as _classify_severity
except ImportError:
    from injury_severity_model import classify_severity as _classify_severity  # type: ignore

SEVERITY_ORDER = ["fatal", "serious", "minor", "injury_report"]


def _summarize(df: pd.DataFrame, scenario: str, total_pedestrians: int | None = None) -> dict[str, Any]:
    n = len(df)
    senior = int(df["is_senior"].sum()) if "is_senior" in df.columns else 0
    counts = {s: int((df["severity"] == s).sum()) for s in SEVERITY_ORDER}
    fatal_serious = counts["fatal"] + counts["serious"]

    result: dict[str, Any] = {
        "scenario": scenario,
        "total_risk_events": n,
        "senior_risk_event_count": senior,
        "senior_risk_event_rate": round(senior / n, 4) if n > 0 else 0.0,
    }
    for s in SEVERITY_ORDER:
        result[f"{s}_count"] = counts[s]
    result["fatal_or_serious_count"] = fatal_serious
    result["fatal_or_serious_rate"] = round(fatal_serious / n, 4) if n > 0 else 0.0

    if total_pedestrians and total_pedestrians > 0:
        result["risk_event_rate_per_1000_pedestrians"] = round(n / total_pedestrians * 1000, 4)
        result["fatal_or_serious_rate_per_1000_pedestrians"] = round(fatal_serious / total_pedestrians * 1000, 4)
    else:
        result["risk_event_rate_per_1000_pedestrians"] = None
        result["fatal_or_serious_rate_per_1000_pedestrians"] = None

    return result


def apply_and_compare(
    baseline_events_path: Path,
    smart_events_path: Path,
    cutoffs_path: Path,
    output_dir: Path,
    total_ped_baseline: int | None = None,
    total_ped_smart: int | None = None,
) -> dict[str, Path]:
    if not cutoffs_path.exists():
        raise FileNotFoundError(f"cutoffs 파일이 없습니다: {cutoffs_path}")

    with cutoffs_path.open(encoding="utf-8") as f:
        cutoffs_data = json.load(f)
    cutoffs = cutoffs_data["risk_score_cutoffs"]

    output_dir.mkdir(parents=True, exist_ok=True)

    summary_rows: list[dict[str, Any]] = []
    cw_rows: list[dict[str, Any]] = []
    age_rows: list[dict[str, Any]] = []

    for path, scenario, total_ped in [
        (baseline_events_path, "baseline", total_ped_baseline),
        (smart_events_path, "smart", total_ped_smart),
    ]:
        if not path.exists():
            continue
        df = pd.read_csv(path)
        if "risk_score" not in df.columns:
            raise ValueError(f"'risk_score' 컬럼 없음: {path}")
        df["severity"] = df["risk_score"].apply(lambda s: _classify_severity(float(s), cutoffs))

        summary_rows.append(_summarize(df, scenario, total_ped))

        # crosswalk별 집계
        if "crosswalk_id" in df.columns:
            for cw_id, grp in df.groupby("crosswalk_id", dropna=False):
                row = _summarize(grp, scenario, None)
                row["crosswalk_id"] = cw_id
                cw_rows.append(row)

        # 연령대별 집계
        age_col = "pedestrian_age_group" if "pedestrian_age_group" in df.columns else None
        if age_col:
            for age_grp, grp in df.groupby(age_col, dropna=False):
                row = _summarize(grp, scenario, None)
                row["age_group"] = age_grp
                age_rows.append(row)

    summary_df = pd.DataFrame(summary_rows)
    cw_df = pd.DataFrame(cw_rows)
    age_df = pd.DataFrame(age_rows)

    summary_path = output_dir / "safety_benefit_summary.csv"
    cw_path = output_dir / "safety_benefit_by_crosswalk.csv"
    age_path = output_dir / "safety_benefit_by_age_group.csv"

    summary_df.to_csv(summary_path, index=False, encoding="utf-8-sig")
    cw_df.to_csv(cw_path, index=False, encoding="utf-8-sig")
    age_df.to_csv(age_path, index=False, encoding="utf-8-sig")

    return {
        "safety_benefit_summary": summary_path,
        "safety_benefit_by_crosswalk": cw_path,
        "safety_benefit_by_age_group": age_path,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="TAAS calibrated severity 적용 및 안전 편익 비교")
    parser.add_argument("--baseline_events", required=True)
    parser.add_argument("--smart_events", required=True)
    parser.add_argument("--cutoffs", required=True, help="taas_severity_cutoffs.json 경로")
    parser.add_argument("--output_dir", required=True)
    parser.add_argument("--total_ped_baseline", type=int, default=None)
    parser.add_argument("--total_ped_smart", type=int, default=None)
    args = parser.parse_args()

    paths = apply_and_compare(
        Path(args.baseline_events),
        Path(args.smart_events),
        Path(args.cutoffs),
        Path(args.output_dir),
        args.total_ped_baseline,
        args.total_ped_smart,
    )
    for key, p in paths.items():
        print(f"{key}: {p}")


if __name__ == "__main__":
    main()
