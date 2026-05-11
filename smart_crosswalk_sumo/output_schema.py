from __future__ import annotations

from pathlib import Path

import pandas as pd


CANDIDATE_COLUMN_MAP = {
    "횡단보도ID": "crosswalk_id",
    "lon": "longitude",
    "lat": "latitude",
    "읍면동명": "dong_name",
    "LANES": "lane_count",
    "ROAD_RANK": "road_rank",
    "MAX_SPD": "max_speed_kph",
    "사고건수": "accident_count",
    "추정AADT": "estimated_aadt",
    "행정동": "admin_dong",
    "노인비율": "elderly_ratio",
    "nan_flag": "has_imputed_values",
    "lanes": "lane_count",
    "road_rank": "road_rank",
    "max_spd": "max_speed_kph",
    "has_signal": "has_signal",
    "crosswalk_length": "crosswalk_length",
    "time_gap_basic": "time_gap_basic",
}

SIMULATION_COLUMN_MAP = {
    "횡단보도ID": "crosswalk_id",
    "행정동": "admin_dong",
    "읍면동명": "dong_name",
    "시나리오": "scenario",
    "사고건수_원본": "raw_accident_count",
    "노인비율": "elderly_ratio",
    "LANES": "lane_count",
    "MAX_SPD": "max_speed_kph",
}


def english_output_columns(frame: pd.DataFrame) -> pd.DataFrame:
    return frame.rename(columns={**CANDIDATE_COLUMN_MAP, **SIMULATION_COLUMN_MAP})


def write_csv_utf8_sig(df: pd.DataFrame, path: str | Path, index: bool = False) -> None:
    """pandas DataFrame을 utf-8-sig 인코딩으로 저장한다 (Excel/Windows 한글 깨짐 방지)."""
    resolved = Path(path)
    resolved.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(resolved, index=index, encoding="utf-8-sig")
