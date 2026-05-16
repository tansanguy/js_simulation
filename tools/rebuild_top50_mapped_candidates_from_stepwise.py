#!/usr/bin/env python3
from __future__ import annotations

from datetime import datetime
from pathlib import Path
import shutil
import sys

import pandas as pd


BASE_DIR = Path(__file__).resolve().parents[1]
SOURCE_CSV = BASE_DIR / "smart_crosswalk_sumo" / "data" / "crosswalk_stepwise_result_50m.csv"
OUTPUT_CSV = BASE_DIR / "result" / "active" / "pedestrian_assumption" / "top50_mapped_candidates_daytime_high_1p2.csv"
BACKUP_GLOB_STEM = "top50_mapped_candidates_daytime_high_1p2.backup_"


def _backup_existing(path: Path) -> Path | None:
    if not path.exists():
        return None
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = path.with_name(f"{path.stem}.backup_{stamp}{path.suffix}")
    shutil.copy2(path, backup_path)
    return backup_path


def _require_columns(df: pd.DataFrame, columns: list[str]) -> None:
    missing = [col for col in columns if col not in df.columns]
    if missing:
        raise ValueError(f"missing required columns: {missing}")


def main() -> int:
    if SOURCE_CSV != BASE_DIR / "smart_crosswalk_sumo" / "data" / "crosswalk_stepwise_result_50m.csv":
        raise AssertionError("source_csv mismatch")
    if not SOURCE_CSV.exists():
        raise FileNotFoundError(f"source CSV not found: {SOURCE_CSV}")

    df = pd.read_csv(SOURCE_CSV)
    _require_columns(df, ["crosswalk_id", "risk_score", "lon", "lat"])

    work = df.copy()
    work["risk_score"] = pd.to_numeric(work["risk_score"], errors="raise")
    work["crosswalk_id"] = work["crosswalk_id"].astype(str)
    work = work.sort_values(
        by=["risk_score", "crosswalk_id"],
        ascending=[False, True],
        kind="mergesort",
    ).head(50).reset_index(drop=True)

    if len(work) != 50:
        raise AssertionError(f"expected 50 rows, got {len(work)}")

    work.insert(0, "top50_rank", range(1, 51))
    work["longitude"] = work["lon"]
    work["latitude"] = work["lat"]

    if work["longitude"].isna().any() or work["latitude"].isna().any():
        raise AssertionError("longitude/latitude has missing values")

    if work["top50_rank"].tolist() != list(range(1, 51)):
        raise AssertionError("top50_rank must be 1..50")

    sorted_check = work["risk_score"].tolist()
    if any(sorted_check[i] < sorted_check[i + 1] for i in range(len(sorted_check) - 1)):
        raise AssertionError("risk_score is not descending")

    OUTPUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    backup_path = _backup_existing(OUTPUT_CSV)
    work.to_csv(OUTPUT_CSV, index=False)

    print(f"source_csv: {SOURCE_CSV}")
    if backup_path is not None:
        print(f"backup_csv: {backup_path}")
    print(f"output_csv: {OUTPUT_CSV}")
    print(f"rows: {len(work)}")
    print("crosswalk_id,top50_rank,risk_score,dong_name,longitude,latitude")
    for _, row in work[["crosswalk_id", "top50_rank", "risk_score", "dong_name", "longitude", "latitude"]].iterrows():
        print(
            f"{row['crosswalk_id']},{row['top50_rank']},{row['risk_score']},"
            f"{row.get('dong_name', '')},{row['longitude']},{row['latitude']}"
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
