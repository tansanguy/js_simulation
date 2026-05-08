# utils.py

from pathlib import Path

# 프로젝트 루트 = 이 파일이 있는 폴더
PROJECT_ROOT = Path(__file__).resolve().parent

# 데이터 경로
RAW_DIR        = PROJECT_ROOT / "data" / "raw"
PROCESSED_DIR  = PROJECT_ROOT / "data" / "processed"

# raw 파일
TAAS_RAW       = RAW_DIR / "taas_raw.xlsx"
ACCIDENTS      = RAW_DIR / "accidents_with_coords.csv"
CROSSWALK      = RAW_DIR / "crosswalk_seoul.csv"
PED_SIGNAL     = RAW_DIR / "ped_signal.csv"
ELDERLY_POP_GU    = RAW_DIR / "elderly_pop_gu.csv"
ELDERLY_POP_DONG    = RAW_DIR / "elderly_pop_dong.csv"
NODE_LINK_DIR  = RAW_DIR / "node_link"
MOCT_LINK_SHP  = NODE_LINK_DIR / "MOCT_LINK.shp"
MOCT_NODE_SHP  = NODE_LINK_DIR / "MOCT_NODE.shp"
TRAFFIC_DIR    = RAW_DIR / "traffic_volume"
DONG_MAPPING_CSV = RAW_DIR / "dong_mapping.csv"  # 국가데이터처 법정동-행정동 연계정보 (cp949)

# processed 파일
T1_PATH        = PROCESSED_DIR / "T1_accident_crosswalk.csv"
T2_PATH        = PROCESSED_DIR / "T2_crosswalk_features.csv"