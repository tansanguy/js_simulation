#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    from smart_crosswalk_sumo.output_schema import english_output_columns, write_csv_utf8_sig
except Exception:  # pragma: no cover
    from output_schema import english_output_columns, write_csv_utf8_sig


SCENARIO_NAME = "daytime_high_1p2"
BASE_PEDESTRIAN_600S_BY_ADMIN_DONG = {
    "소공동": (35, "sdot_observed"),
    "회현동": (65, "sdot_observed"),
    "명동": (144, "sdot_observed"),
    "필동": (35, "living_population_imputed"),
    "장충동": (26, "living_population_imputed"),
    "광희동": (376, "sdot_observed"),
    "을지로동": (40, "living_population_imputed"),
    "신당동": (14, "sdot_observed"),
    "다산동": (31, "living_population_imputed"),
    "약수동": (28, "living_population_imputed"),
    "청구동": (20, "living_population_imputed"),
    "신당제5동": (14, "living_population_imputed"),
    "동화동": (25, "living_population_imputed"),
    "황학동": (25, "living_population_imputed"),
    "중림동": (18, "living_population_imputed"),
}


def build_assumptions(input_csv: Path, output_csv: Path, missing_csv: Path) -> tuple[pd.DataFrame, pd.DataFrame]:
    frame = pd.read_csv(input_csv)
    if "행정동" not in frame.columns and "admin_dong" not in frame.columns:
        raise ValueError(f"missing required column: 행정동 or admin_dong in {input_csv}")

    normalized = english_output_columns(frame).copy()
    normalized["scenario_name"] = SCENARIO_NAME

    base_values: list[int | None] = []
    ped_sources: list[str | None] = []
    missing_admin_dongs: list[str] = []

    for raw_admin_dong in normalized["admin_dong"].fillna("").astype(str):
        admin_dong = raw_admin_dong.strip()
        payload = BASE_PEDESTRIAN_600S_BY_ADMIN_DONG.get(admin_dong)
        if payload is None:
            base_values.append(None)
            ped_sources.append(None)
            if admin_dong and admin_dong not in missing_admin_dongs:
                missing_admin_dongs.append(admin_dong)
            continue
        base_count, source = payload
        base_values.append(int(base_count))
        ped_sources.append(source)

    normalized["base_pedestrian_600s"] = base_values
    normalized["final_pedestrian_600s"] = normalized["base_pedestrian_600s"].apply(
        lambda value: int(round(float(value) * 1.2)) if pd.notna(value) else pd.NA
    )
    normalized["ped_source"] = ped_sources

    output_cols = [
        "crosswalk_id",
        "longitude",
        "latitude",
        "dong_name",
        "lane_count",
        "road_rank",
        "max_speed_kph",
        "accident_count",
        "estimated_aadt",
        "admin_dong",
        "elderly_ratio",
        "has_imputed_values",
        "crossing_length_m",
        "ped_green_base",
        "ped_green_elderly",
        "risk_score",
        "base_pedestrian_600s",
        "final_pedestrian_600s",
        "ped_source",
        "scenario_name",
    ]
    available_cols = [col for col in output_cols if col in normalized.columns]
    result_df = normalized[available_cols].copy()
    result_df = result_df.sort_values(["admin_dong", "risk_score", "crosswalk_id"], ascending=[True, False, True])

    missing_df = pd.DataFrame(
        [{"admin_dong": admin_dong, "reason": "no_pedestrian_assumption"} for admin_dong in missing_admin_dongs],
        columns=["admin_dong", "reason"],
    )

    write_csv_utf8_sig(result_df, output_csv)
    write_csv_utf8_sig(missing_df, missing_csv)
    return result_df, missing_df


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--input_csv",
        default="smart_crosswalk_sumo/outputs/candidates.csv",
    )
    parser.add_argument(
        "--output_csv",
        default="result/active/pedestrian_assumption/crosswalk_pedestrian_assumptions_daytime_high_1p2.csv",
    )
    parser.add_argument(
        "--missing_csv",
        default="result/active/pedestrian_assumption/missing_pedestrian_assumption.csv",
    )
    args = parser.parse_args()
    build_assumptions(Path(args.input_csv), Path(args.output_csv), Path(args.missing_csv))


if __name__ == "__main__":
    main()
