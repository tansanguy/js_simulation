from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import pandas as pd

try:
    from .output_schema import english_output_columns
except ImportError:
    from output_schema import english_output_columns


def preprocess_inputs(
    t1_path: str | Path,
    t2_path: str | Path,
    output_dir: str | Path,
    top_n: int = 20,
    target_crosswalk_ids: list[str] | tuple[str, ...] | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    t1 = pd.read_csv(t1_path)
    t2 = english_output_columns(pd.read_csv(t2_path))

    required = {"crosswalk_id", "longitude", "latitude"}
    missing_required = sorted(c for c in required if c not in t2.columns)
    if missing_required:
        raise ValueError(f"T2 필수 컬럼 누락: {missing_required}")

    if "lane_count" not in t2.columns:
        t2["lane_count"] = np.nan
    if "max_speed_kph" not in t2.columns:
        t2["max_speed_kph"] = np.nan
    if "accident_count" not in t2.columns:
        t2["accident_count"] = 0.0
    if "elderly_ratio" not in t2.columns:
        t2["elderly_ratio"] = 0.0
    if "estimated_aadt" not in t2.columns:
        t2["estimated_aadt"] = np.nan

    t2["has_imputed_values"] = t2["lane_count"].isna() | t2["max_speed_kph"].isna()
    t2["lane_count"] = pd.to_numeric(t2["lane_count"], errors="coerce").fillna(2.0).astype(float)
    t2["max_speed_kph"] = pd.to_numeric(t2["max_speed_kph"], errors="coerce").fillna(50.0).astype(float)
    t2["accident_count"] = pd.to_numeric(t2["accident_count"], errors="coerce").fillna(0).astype(float)
    t2["estimated_aadt"] = pd.to_numeric(t2["estimated_aadt"], errors="coerce")
    aadt_median = t2["estimated_aadt"].median()
    if pd.isna(aadt_median):
        aadt_median = 0.0
    t2["estimated_aadt"] = t2["estimated_aadt"].fillna(aadt_median).astype(float)
    t2["elderly_ratio"] = pd.to_numeric(t2["elderly_ratio"], errors="coerce")
    t2["elderly_ratio"] = t2["elderly_ratio"].fillna(t2["elderly_ratio"].median()).astype(float)

    t2["crossing_length_m"] = t2["lane_count"] * 3.5
    t2["ped_green_base"] = (t2["crossing_length_m"] / 1.0 + 7).clip(lower=10.0)
    t2["ped_green_elderly"] = (t2["crossing_length_m"] / 0.8 + 7).clip(lower=10.0)

    t2["risk_score"] = (
        t2["accident_count"] * 0.5
        + t2["elderly_ratio"] * 10
        + t2["lane_count"] * 0.3
        + (t2["max_speed_kph"] / 50) * 0.2
    )

    if target_crosswalk_ids:
        wanted = {str(cw_id) for cw_id in target_crosswalk_ids}
        candidates = t2[t2["crosswalk_id"].astype(str).isin(wanted)].copy()
        found = set(candidates["crosswalk_id"].astype(str))
        missing = sorted(wanted - found)
        if missing:
            raise ValueError(f"target_crosswalk_ids를 찾지 못했습니다: {missing}")
        candidates = candidates.sort_values("risk_score", ascending=False)
    else:
        candidates = t2.nlargest(top_n, "risk_score").copy()
    english_output_columns(candidates).to_csv(output_dir / "candidates.csv", index=False)
    english_output_columns(t2).to_csv(output_dir / "preprocessed_crosswalks.csv", index=False)

    return candidates, t1, t2


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--t1", default="data/processed/T1_accident_crosswalk.csv")
    parser.add_argument("--t2", default="data/processed/T2_crosswalk_features.csv")
    parser.add_argument("--top_n", type=int, default=20)
    parser.add_argument("--target_crosswalk_ids", nargs="+", default=None)
    parser.add_argument("--output_dir", default="outputs")
    args = parser.parse_args()
    preprocess_inputs(args.t1, args.t2, args.output_dir, args.top_n, args.target_crosswalk_ids)


if __name__ == "__main__":
    main()
