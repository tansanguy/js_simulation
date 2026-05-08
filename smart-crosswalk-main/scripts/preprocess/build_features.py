"""스마트 횡단보도 최적 입지 선정 - 전처리 파이프라인
raw data → T1_accident_crosswalk.csv + T2_crosswalk_features.csv

DISTRICT 파라미터로 지역 지정. None = 서울 전체.
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from utils import *
import pandas as pd
import geopandas as gpd
import numpy as np
import re
from shapely import wkt
from sklearn.neighbors import BallTree
from scipy.stats import spearmanr

# ── 파라미터 ──────────────────────────────────────────────────────────────────
DISTRICT = None  # None = 서울 전체, '중구' 등으로 지역 지정 가능

# ── Step A: 횡단보도 로드 + 지역 필터 + 좌표 추출 ─────────────────────────────
print("[Step A] 횡단보도 로드...")
cw_raw = pd.read_csv(CROSSWALK, encoding='cp949')
if DISTRICT:
    cw_raw = cw_raw[cw_raw['시군구명'] == DISTRICT].copy()
print(f"  {DISTRICT or '서울 전체'}: {len(cw_raw)}개 (NODE {(cw_raw['노드링크 유형']=='NODE').sum()}, LINK {(cw_raw['노드링크 유형']=='LINK').sum()})")

def parse_point(wkt_str):
    if pd.isna(wkt_str): return None, None
    m = re.match(r'POINT\(([\d.]+)\s+([\d.]+)\)', str(wkt_str))
    return (float(m.group(1)), float(m.group(2))) if m else (None, None)

cw_nodes = cw_raw[cw_raw['노드링크 유형'] == 'NODE'].copy()
cw_nodes[['lon', 'lat']] = cw_nodes['노드 WKT'].apply(lambda x: pd.Series(parse_point(x)))
cw_nodes['crosswalk_id'] = cw_nodes['노드 ID']

cw_links = cw_raw[cw_raw['노드링크 유형'] == 'LINK'].copy()
cw_links['_geom'] = cw_links['링크 WKT'].apply(wkt.loads)
cw_links['lon'] = cw_links['_geom'].apply(lambda g: g.centroid.x)
cw_links['lat'] = cw_links['_geom'].apply(lambda g: g.centroid.y)
cw_links['crosswalk_id'] = cw_links['링크 ID']

cols = ['crosswalk_id', 'lon', 'lat', '읍면동명', '시군구명', '노드링크 유형']
cw = pd.concat([cw_nodes[cols], cw_links[cols]], ignore_index=True)
cw = cw.rename(columns={'읍면동명': 'dong_name', '시군구명': 'gu_name', '노드링크 유형': 'cw_type'})
print(f"  좌표 누락: {cw['lon'].isna().sum()}개")

# ── Step B: 사고 로드 + 65세↑ 횡단중 필터 ────────────────────────────────────
# accidents에 나이 컬럼 없음 → taas_raw의 '피해운전자 연령대'를 구분번호로 join
print("[Step B] 사고 필터링...")
acc_all = pd.read_csv(ACCIDENTS)
taas = pd.read_excel(TAAS_RAW, usecols=['구분번호', '피해운전자 연령대'])
acc_all = acc_all.merge(taas, on='구분번호', how='left')

acc_elderly = acc_all[
    (acc_all['피해운전자 연령대'] == '65세 이상') &
    (acc_all['acdnt_dc'].str.contains('횡단', na=False))
].copy()
print(f"  65세↑ + 횡단중: {len(acc_elderly)}건 / 전체 {len(acc_all)}건 (나이 매칭: {acc_all['피해운전자 연령대'].notna().sum()}건)")

# ── Step C: T1 생성 (사고 ↔ 횡단보도 매칭, 100m 이내) ─────────────────────────
print("[Step C] T1 생성...")
gdf_acc = gpd.GeoDataFrame(
    acc_elderly,
    geometry=gpd.points_from_xy(acc_elderly['lon'], acc_elderly['lat']),
    crs='EPSG:4326',
).to_crs('EPSG:5179')
gdf_cw = gpd.GeoDataFrame(
    cw,
    geometry=gpd.points_from_xy(cw['lon'], cw['lat']),
    crs='EPSG:4326',
).to_crs('EPSG:5179')

matched = gpd.sjoin_nearest(
    gdf_acc, gdf_cw[['crosswalk_id', 'geometry']],
    how='left', max_distance=100, distance_col='match_dist_m',
)
T1 = (
    matched[matched['crosswalk_id'].notna()]
    [['구분번호', 'crosswalk_id', 'match_dist_m']]
    .rename(columns={'구분번호': 'accident_id'})
    .copy()
)
T1['crosswalk_id'] = T1['crosswalk_id'].astype(int)
print(f"  매칭 성공: {len(T1)} / {len(acc_elderly)} ({len(T1)/len(acc_elderly):.1%}), 평균 {T1['match_dist_m'].mean():.1f}m")

# ── Step D: MOCT_LINK 매칭 (lanes, road_rank, max_spd, is_oneway) ──────────────
print("[Step D] MOCT_LINK + NODE 로드...")
moct_link = gpd.read_file(MOCT_LINK_SHP, encoding='cp949').to_crs('EPSG:4326')
moct_node = gpd.read_file(MOCT_NODE_SHP, encoding='cp949').to_crs('EPSG:4326')

lat_min, lat_max = cw['lat'].min() - 0.05, cw['lat'].max() + 0.05
lon_min, lon_max = cw['lon'].min() - 0.05, cw['lon'].max() + 0.05
moct_link_local = moct_link.cx[lon_min:lon_max, lat_min:lat_max].to_crs('EPSG:5179')
moct_node_local = moct_node.cx[lon_min:lon_max, lat_min:lat_max].to_crs('EPSG:5179')
print(f"  MOCT_LINK: {len(moct_link_local)}개 | MOCT_NODE: {len(moct_node_local)}개")

# 1차: 30m 이내 매칭 (임용빈 외 2024: 인접시설물 혼입 방지 기준)
road_30 = (
    gpd.sjoin_nearest(
        gdf_cw[['crosswalk_id', 'geometry']],
        moct_link_local[['LANES', 'ROAD_RANK', 'MAX_SPD', 'ROAD_USE', 'geometry']],
        how='left', max_distance=30, distance_col='_dist',
    )
    .sort_values('_dist')
    .drop_duplicates(subset='crosswalk_id', keep='first')
)
# 2차: 30m 실패분만 35m fallback (임용빈 외 2024: 차량사고 군집화 35.7m 이내)
failed_30 = road_30[road_30['LANES'].isna()]['crosswalk_id']
print(f"  30m 매칭: {road_30['LANES'].notna().sum()} / {len(road_30)}, fallback 대상: {len(failed_30)}개")
if len(failed_30) > 0:
    road_35 = (
        gpd.sjoin_nearest(
            gdf_cw[gdf_cw['crosswalk_id'].isin(failed_30)][['crosswalk_id', 'geometry']],
            moct_link_local[['LANES', 'ROAD_RANK', 'MAX_SPD', 'ROAD_USE', 'geometry']],
            how='left', max_distance=35, distance_col='_dist',
        )
        .sort_values('_dist')
        .drop_duplicates(subset='crosswalk_id', keep='first')
    )
    road_joined = pd.concat([road_30[road_30['LANES'].notna()], road_35], ignore_index=True)
else:
    road_joined = road_30

cw = cw.merge(
    road_joined[['crosswalk_id', 'LANES', 'ROAD_RANK', 'MAX_SPD', 'ROAD_USE']].rename(columns={
        'LANES': 'lanes', 'ROAD_RANK': 'road_rank', 'MAX_SPD': 'max_spd', 'ROAD_USE': '_road_use',
    }),
    on='crosswalk_id', how='left',
)
cw['is_oneway'] = cw['_road_use'].map({'0': 0, '1': 1})
cw.drop(columns='_road_use', inplace=True)
print(f"  최종 도로 매칭: {cw['lanes'].notna().sum()} / {len(cw)}, is_oneway=1: {int(cw['is_oneway'].sum())}")

# ── Step E: 법정동→행정동 매핑 + 고령인구 비율 ────────────────────────────────
print("[Step E] 고령인구 비율...")
dm = pd.read_csv(DONG_MAPPING_CSV, encoding='cp949')
dm.columns = ['sido', 'gu', 'admin_dong', 'dong_name',
              'admin_code', 'admin_code2', 'legal_code', 'base_date', 'seq']
seoul_map = (
    dm[dm['sido'].str.contains('서울', na=False)]
    [['gu', 'dong_name', 'admin_dong']]
    .drop_duplicates(subset=['gu', 'dong_name'])
)
cw = cw.merge(seoul_map.rename(columns={'gu': 'gu_name'}), on=['gu_name', 'dong_name'], how='left')
unmatched = cw[cw['admin_dong'].isna()][['gu_name', 'dong_name']].drop_duplicates()
if len(unmatched):
    print(f"  [WARNING] (gu+dong) 매핑 실패 {len(unmatched)}건, dong_name fallback 시도...")
# 2차: (gu+dong) 매칭 실패분 → dong_name만으로 재매칭
# (crosswalk_seoul 시군구 태깅 오류 추정, dong_mapping 2025.07 기준 신뢰)
dong_only_map = seoul_map.drop_duplicates(subset='dong_name')[['dong_name', 'admin_dong']]
unmatched_mask = cw['admin_dong'].isna()
if unmatched_mask.sum() > 0:
    cw.loc[unmatched_mask, 'admin_dong'] = cw.loc[unmatched_mask, 'dong_name'].map(
        dong_only_map.set_index('dong_name')['admin_dong']
    )
    print(f"  dong_name fallback: {unmatched_mask.sum()}개 처리 → 잔여 미매핑: {cw['admin_dong'].isna().sum()}개")

eld = pd.read_csv(ELDERLY_POP_DONG, encoding='utf-8-sig', skiprows=4)
eld.columns = ['시도', '구', '동', '전체_소계', '전체_남', '전체_여',
               '노인_소계', '노인_남', '노인_여',
               '노인_내국_소계', '노인_내국_남', '노인_내국_여',
               '노인_외국_소계', '노인_외국_남', '노인_외국_여']
for col in ['전체_소계', '노인_소계']:
    eld[col] = pd.to_numeric(eld[col], errors='coerce')
eld = eld[eld['동'] != '소계'].copy()
eld['elderly_ratio'] = eld['노인_소계'] / eld['전체_소계']

cw = cw.merge(eld[['동', 'elderly_ratio']].rename(columns={'동': 'admin_dong'}), on='admin_dong', how='left')
cw['elderly_ratio'] = cw['elderly_ratio'].fillna(eld['elderly_ratio'].mean())
print(f"  행정동 매칭: {cw['admin_dong'].notna().sum()} / {len(cw)}")

# ── Step F: accident_count (종속변수) ─────────────────────────────────────────
cw = cw.merge(
    T1.groupby('crosswalk_id').size().reset_index(name='accident_count'),
    on='crosswalk_id', how='left',
)
cw['accident_count'] = cw['accident_count'].fillna(0).astype(int)
print(f"[Step F] 사고건수: 0건 {(cw['accident_count']==0).sum()}개, 1건↑ {(cw['accident_count']>0).sum()}개")

# ── Step G: crosswalk_length (NODE 타입은 차선폭 중앙값으로 추정) ─────────────
print("[Step G] crosswalk_length...")
link_len = (
    cw_raw[cw_raw['노드링크 유형'] == 'LINK'][['링크 ID', '링크 길이']]
    .rename(columns={'링크 ID': 'crosswalk_id', '링크 길이': 'crosswalk_length'})
)
cw = cw.merge(link_len, on='crosswalk_id', how='left')

link_mask = cw['crosswalk_length'].notna() & cw['lanes'].notna() & (cw['lanes'] > 0)
median_width = (cw.loc[link_mask, 'crosswalk_length'] / cw.loc[link_mask, 'lanes']).median()
print(f"  LINK 실측 차선폭 중앙값: {median_width:.2f}m ({link_mask.sum()}개 기준)")
node_mask = cw['crosswalk_length'].isna() & cw['lanes'].notna()
cw.loc[node_mask, 'crosswalk_length'] = cw.loc[node_mask, 'lanes'] * median_width
print(f"  crosswalk_length 결측: {cw['crosswalk_length'].isna().sum()}개")

# ── Step H: has_signal (보행자 신호등 반경 30m 이내) ─────────────────────────
print("[Step H] has_signal...")
ped = pd.read_csv(PED_SIGNAL, encoding='cp949')
ped_gdf = gpd.GeoDataFrame(
    ped,
    geometry=gpd.points_from_xy(ped['X좌표'], ped['Y좌표']),
    crs='EPSG:5186',
).to_crs('EPSG:4326')
ped_rad = np.radians(np.column_stack([ped_gdf.geometry.y, ped_gdf.geometry.x]))
cw_rad  = np.radians(cw[['lat', 'lon']].values)
cw['has_signal'] = (
    BallTree(ped_rad, metric='haversine').query_radius(cw_rad, r=30 / 6_371_000, count_only=True) > 0
).astype(int)
print(f"  has_signal=1: {cw['has_signal'].sum()} / {len(cw)}")

# ── Step I: accident_count_Nm + night_accident_ratio ─────────────────────────
print("[Step I] accident_count_Nm + night_accident_ratio...")
acc_valid = acc_all.dropna(subset=['lon', 'lat'])
acc_rad   = np.radians(acc_valid[['lat', 'lon']].values)
is_night  = (acc_valid['tmzon'] == '야간').values
tree_acc  = BallTree(acc_rad, metric='haversine')

for r_m in [50, 100, 200]:
    cw[f'accident_count_{r_m}m'] = tree_acc.query_radius(cw_rad, r=r_m / 6_371_000, count_only=True)
idx_100m = tree_acc.query_radius(cw_rad, r=100 / 6_371_000)
cw['night_accident_ratio'] = [float(is_night[i].mean()) if len(i) > 0 else 0.0 for i in idx_100m]
print(f"  accident_count_100m > 0: {(cw['accident_count_100m'] > 0).sum()} / {len(cw)}")

# ── Step J: link_count (교차로 연결 링크 수) ──────────────────────────────────
# NODE 타입만 MOCT_NODE에 매칭 (교차로 지점). LINK 타입은 도로 구간 위에 있으므로 0.
print("[Step J] link_count...")
gdf_cw_node = gdf_cw[gdf_cw['cw_type'] == 'NODE'][['crosswalk_id', 'geometry']]
node_joined = (
    gpd.sjoin_nearest(
        gdf_cw_node,
        moct_node_local[['NODE_ID', 'geometry']],
        how='left', max_distance=50, distance_col='_dist',
    )
    .sort_values('_dist')
    .drop_duplicates(subset='crosswalk_id', keep='first')
)
link_cnt_map = (
    moct_link_local['F_NODE'].value_counts()
    .add(moct_link_local['T_NODE'].value_counts(), fill_value=0)
    .astype(int)
)
cw['link_count'] = (
    node_joined.set_index('crosswalk_id')['NODE_ID']
    .map(link_cnt_map)
    .reindex(cw['crosswalk_id'])
    .values
)
cw.loc[cw['cw_type'] == 'LINK', 'link_count'] = 0
node_nan = cw[cw['cw_type'] == 'NODE']['link_count'].isna().sum()
print(f"  NODE 타입 매칭: {cw[cw['cw_type']=='NODE']['link_count'].notna().sum()} / {(cw['cw_type']=='NODE').sum()}, NaN: {node_nan}")
print(f"  LINK 타입 → link_count=0: {(cw['cw_type']=='LINK').sum()}개")
print(f"  분포: {cw['link_count'].value_counts().sort_index().to_dict()}")

# ── Step K: time_gap_basic (고령자 vs 설계기준 횡단시간 차이, 초) ──────────────
# (7 + L/0.8) - (7 + L/1.0) = L/0.8 - L/1.0
cw['time_gap_basic'] = cw['crosswalk_length'] / 0.8 - cw['crosswalk_length'] / 1.0
print(f"[Step K] time_gap_basic NaN: {cw['time_gap_basic'].isna().sum()}")

# ── Step L: 검증 ──────────────────────────────────────────────────────────────
check_cols = [
    'lanes', 'road_rank', 'max_spd', 'elderly_ratio',
    'crosswalk_length', 'has_signal',
    'accident_count_50m', 'accident_count_100m', 'accident_count_200m',
    'night_accident_ratio', 'link_count', 'is_oneway', 'time_gap_basic',
]
print("\n[Step L] 결측 비율:")
print(cw[check_cols].isnull().mean().round(3).sort_values(ascending=False).to_string())

print("\n[Step L] Spearman r vs accident_count:")
for col in check_cols:
    mask = cw[col].notna()
    if mask.sum() < 10:
        continue
    r, p = spearmanr(cw.loc[mask, col], cw.loc[mask, 'accident_count'])
    print(f"  {col}: r={r:.3f}, p={p:.3f}")

# ── Step M: 저장 ──────────────────────────────────────────────────────────────
T2 = cw[[
    'crosswalk_id', 'lon', 'lat', 'dong_name', 'admin_dong',
    'lanes', 'road_rank', 'max_spd', 'elderly_ratio', 'accident_count',
    'crosswalk_length', 'has_signal',
    'accident_count_50m', 'accident_count_100m', 'accident_count_200m',
    'night_accident_ratio', 'link_count', 'is_oneway', 'time_gap_basic',
]].copy()

T1.to_csv(T1_PATH, index=False, encoding='utf-8-sig')
T2.to_csv(T2_PATH, index=False, encoding='utf-8-sig')
print(f"\n[Step M] T1: {T1_PATH} ({len(T1)}행 × {len(T1.columns)}열)")
print(f"[Step M] T2: {T2_PATH} ({len(T2)}행 × {len(T2.columns)}열)")
print(f"  컬럼: {T2.columns.tolist()}")
