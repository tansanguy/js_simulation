from __future__ import annotations

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
