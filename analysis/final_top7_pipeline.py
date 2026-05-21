"""
final_top7_pipeline.py

이미 완주한 30seed baseline/smart paired 시뮬레이션 결과를 원천으로 삼아
top7 후보를 선정하고 보고서용 산출물을 생성한다.

사용법:
    cd /Users/junlee/Desktop/2026-1/js
    python3 analysis/final_top7_pipeline.py
    python3 analysis/final_top7_pipeline.py --jobs 8   # 병렬 처리

옵션:
    -j, --jobs N   Step 1 CSV 로딩 및 Step 6 매핑 audit을 N개 스레드로 병렬 처리 (기본값: 1)

출력: js/final/top7/
  A. final_all_seed_rows.csv
  B. final_paired_seed_delta.csv
  C. final_crosswalk_baseline_smart_summary.csv
  D. final_top7_candidates.csv
  E. final_top7_report_table.csv
  F. final_top7_mapping_audit.csv
  G. final_top7_map.geojson
"""

from __future__ import annotations

import argparse
import json
import math
import os
import warnings
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import numpy as np
import pandas as pd

# ── CLI 인자 파싱 ──────────────────────────────────────────────────────────────
_parser = argparse.ArgumentParser(
    description="top7 후보 선정 파이프라인",
    formatter_class=argparse.ArgumentDefaultsHelpFormatter,
)
_parser.add_argument(
    "-j", "--jobs",
    type=int,
    default=1,
    metavar="N",
    help="병렬 스레드 수 (Step 1 CSV 로딩, Step 6 매핑 audit). 0 이면 CPU 코어 수 사용.",
)
_args = _parser.parse_args()
JOBS: int = _args.jobs if _args.jobs > 0 else (os.cpu_count() or 1)
print(f"[설정] --jobs={JOBS}")

try:
    from scipy import stats as scipy_stats
    HAS_SCIPY = True
except ImportError:
    HAS_SCIPY = False
    warnings.warn("scipy 없음 – Wilcoxon p-value 컬럼 생략")

# ── 경로 설정 ─────────────────────────────────────────────────────────────────
SCRIPT_DIR = Path(__file__).resolve().parent
JS_ROOT = SCRIPT_DIR.parent  # js/

RUNS_ROOT = JS_ROOT / "final" / "runs"
OUTPUT_DIR = JS_ROOT / "final" / "top7"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

CANDIDATE_META_CSV = JS_ROOT / "final" / "manifests" / "candidate_metadata.csv"
STEPWISE_CSV = JS_ROOT / "crosswalk_stepwise_result_50m.csv"
REGISTRY_CSV = JS_ROOT / "smart_crosswalk_sumo" / "registry" / "junggu_crosswalk_sumo_registry.csv"

# ── 지표 정의 ─────────────────────────────────────────────────────────────────
KEY_METRICS = [
    "pedestrian_crossing_count",
    "pedestrian_clearance_failure_count",
    "unfinished_crossing_count",
    "ped_wait_time_mean",
    "ped_wait_time_max",
    "veh_delay_mean",
    "veh_avg_delay_sec",
    "network_avg_delay_sec",
    "network_arrived_vehicles",
    "local_500m_avg_delay_sec",
    "local_500m_vehicle_count",
    "local_500m_mean_speed",
    "total_vehicle_arrivals",
    "extension_count",
    "total_extension_sec",
    "pedestrian_green_extension_count",
    "pet_event_count",
    "low_pet_event_count",
    "low_pet_per_100_crossings",
    "pet_min",
    "pet_p10",
    "pet_mean",
    "very_risky_crossing_count",
    "risky_crossing_count",
    "safety_risk_score",
    "accident_risk_estimate",
    "accident_expected_value",
    "elderly_incomplete_crossings",
]

# ─────────────────────────────────────────────────────────────────────────────
# Step 1: 원천 데이터 수집
# ─────────────────────────────────────────────────────────────────────────────
print("=" * 60)
print("Step 1: 원천 데이터 수집")
print("=" * 60)


def _load_result(result_path: Path):
    """단일 final_result.csv 로드. (frame, issues) 튜플 반환."""
    parts = result_path.parts
    folder_group = parts[-3]
    folder_candidate = parts[-2]

    issues = []
    try:
        df = pd.read_csv(result_path)
    except Exception as e:
        issues.append({"file": str(result_path), "issue": str(e)})
        return None, issues

    seed_rows = df[df["scenario"].isin(["baseline", "smart"])].copy()

    if "group" not in seed_rows.columns:
        seed_rows["group"] = folder_group
    if "crosswalk_id" not in seed_rows.columns:
        seed_rows["crosswalk_id"] = folder_candidate

    seed_rows["crosswalk_id"] = seed_rows["crosswalk_id"].astype(str)
    seed_rows["group"] = seed_rows["group"].fillna(folder_group).astype(str)

    if "completed" in seed_rows.columns:
        incomplete = seed_rows[seed_rows["completed"] != True]
        if not incomplete.empty:
            for _, row in incomplete.iterrows():
                issues.append({
                    "crosswalk_id": row.get("crosswalk_id", folder_candidate),
                    "scenario": row.get("scenario"),
                    "seed": row.get("seed"),
                    "issue": "completed=False",
                })

    return seed_rows, issues


all_paths = [
    p for p in sorted(RUNS_ROOT.glob("*/*/final_result.csv"))
    if p.parts[-2] != "baseline"
]

frames = []
validation_issues = []

if JOBS == 1:
    for result_path in all_paths:
        frame, issues = _load_result(result_path)
        if frame is not None:
            frames.append(frame)
        validation_issues.extend(issues)
else:
    with ThreadPoolExecutor(max_workers=JOBS) as pool:
        futures = {pool.submit(_load_result, p): p for p in all_paths}
        for future in as_completed(futures):
            frame, issues = future.result()
            if frame is not None:
                frames.append(frame)
            validation_issues.extend(issues)

print(f"  로드한 후보 폴더 수: {len(frames)}")

if not frames:
    raise RuntimeError("final_result.csv를 하나도 찾지 못했습니다. RUNS_ROOT 확인 필요.")

raw_df = pd.concat(frames, ignore_index=True)
print(f"  총 seed 행 수: {len(raw_df)}")
print(f"  crosswalk_id 수: {raw_df['crosswalk_id'].nunique()}")

# seed 수 검증 보고
seed_counts = (
    raw_df.groupby(["crosswalk_id", "scenario"])["seed"]
    .count()
    .unstack(fill_value=0)
    .rename(columns={"baseline": "n_baseline", "smart": "n_smart"})
)
print("\n  후보별 seed 수:")
print(seed_counts.to_string())

if validation_issues:
    print(f"\n  ⚠ 검증 이슈 {len(validation_issues)}건:")
    for issue in validation_issues[:10]:
        print(f"    {issue}")

# ─────────────────────────────────────────────────────────────────────────────
# Step 2: A. final_all_seed_rows.csv
# ─────────────────────────────────────────────────────────────────────────────
print("\n" + "=" * 60)
print("Step 2: A. final_all_seed_rows.csv 저장")
print("=" * 60)

# 핵심 앞쪽 컬럼 정의
front_cols = ["group", "crosswalk_id", "scenario", "seed"]
existing_key_metrics = [c for c in KEY_METRICS if c in raw_df.columns]
other_cols = [c for c in raw_df.columns if c not in front_cols + existing_key_metrics]
col_order = front_cols + existing_key_metrics + other_cols

raw_df = raw_df[[c for c in col_order if c in raw_df.columns]]
raw_df.to_csv(OUTPUT_DIR / "final_all_seed_rows.csv", index=False, encoding="utf-8-sig")
print(f"  저장 완료: {OUTPUT_DIR / 'final_all_seed_rows.csv'}  ({len(raw_df)} 행)")

# ─────────────────────────────────────────────────────────────────────────────
# Step 3: B. final_paired_seed_delta.csv
# ─────────────────────────────────────────────────────────────────────────────
print("\n" + "=" * 60)
print("Step 3: B. final_paired_seed_delta.csv")
print("=" * 60)

numeric_metrics = [c for c in existing_key_metrics if pd.api.types.is_numeric_dtype(raw_df[c])]

base_df = raw_df[raw_df["scenario"] == "baseline"].copy()
smart_df = raw_df[raw_df["scenario"] == "smart"].copy()

# seed 컬럼을 숫자로 통일
base_df["seed"] = pd.to_numeric(base_df["seed"], errors="coerce")
smart_df["seed"] = pd.to_numeric(smart_df["seed"], errors="coerce")

# inner join on (crosswalk_id, seed)
paired = base_df[["crosswalk_id", "group", "seed"] + numeric_metrics].merge(
    smart_df[["crosswalk_id", "seed"] + numeric_metrics],
    on=["crosswalk_id", "seed"],
    suffixes=("_baseline", "_smart"),
    how="inner",
)

for col in numeric_metrics:
    b_col = f"{col}_baseline"
    s_col = f"{col}_smart"
    if b_col in paired.columns and s_col in paired.columns:
        paired[f"delta_{col}"] = paired[s_col] - paired[b_col]

print(f"  paired 행 수: {len(paired)}")
print(f"  crosswalk_id당 paired seeds: {paired.groupby('crosswalk_id')['seed'].count().describe()}")

paired.to_csv(OUTPUT_DIR / "final_paired_seed_delta.csv", index=False, encoding="utf-8-sig")
print(f"  저장 완료: {OUTPUT_DIR / 'final_paired_seed_delta.csv'}")

# ─────────────────────────────────────────────────────────────────────────────
# Step 4: C. final_crosswalk_baseline_smart_summary.csv
# ─────────────────────────────────────────────────────────────────────────────
print("\n" + "=" * 60)
print("Step 4: C. final_crosswalk_baseline_smart_summary.csv")
print("=" * 60)

summary_rows = []

for cw_id, grp in paired.groupby("crosswalk_id"):
    row: dict = {
        "crosswalk_id": cw_id,
        "group": grp["group"].iloc[0],
        "n_seeds": len(grp),
    }

    for col in numeric_metrics:
        b_col = f"{col}_baseline"
        s_col = f"{col}_smart"
        d_col = f"delta_{col}"

        if b_col in grp.columns:
            row[f"baseline_{col}_mean"] = grp[b_col].mean()
        if s_col in grp.columns:
            row[f"smart_{col}_mean"] = grp[s_col].mean()
        if d_col in grp.columns:
            vals = grp[d_col].dropna()
            n = len(vals)
            mean_v = vals.mean()
            std_v = vals.std(ddof=1) if n > 1 else np.nan
            se_v = std_v / math.sqrt(n) if n > 1 else np.nan
            row[f"delta_{col}_mean"] = mean_v
            row[f"delta_{col}_std"] = std_v
            row[f"delta_{col}_se"] = se_v
            row[f"delta_{col}_ci95_lo"] = mean_v - 1.96 * se_v if not np.isnan(se_v) else np.nan
            row[f"delta_{col}_ci95_hi"] = mean_v + 1.96 * se_v if not np.isnan(se_v) else np.nan

            # Wilcoxon signed-rank test
            if HAS_SCIPY and n >= 5:
                b_vals = grp[b_col].dropna()
                s_vals = grp[s_col].dropna()
                paired_mask = b_vals.index.intersection(s_vals.index)
                if len(paired_mask) >= 5:
                    try:
                        _, p = scipy_stats.wilcoxon(
                            b_vals.loc[paired_mask].values,
                            s_vals.loc[paired_mask].values,
                        )
                        row[f"p_delta_{col}_wilcoxon"] = round(p, 4)
                    except Exception:
                        row[f"p_delta_{col}_wilcoxon"] = np.nan

    summary_rows.append(row)

summary_df = pd.DataFrame(summary_rows)
summary_df.to_csv(
    OUTPUT_DIR / "final_crosswalk_baseline_smart_summary.csv",
    index=False, encoding="utf-8-sig",
)
print(f"  저장 완료: {len(summary_df)} 후보  ({OUTPUT_DIR / 'final_crosswalk_baseline_smart_summary.csv'})")

# ─────────────────────────────────────────────────────────────────────────────
# Step 5: D. final_top7_candidates.csv
# ─────────────────────────────────────────────────────────────────────────────
print("\n" + "=" * 60)
print("Step 5: D. top7 선정")
print("=" * 60)

s = summary_df.copy()

# ── 필수 필터 ──────────────────────────────────────────────────────────────
MIN_SEEDS = 15
filter_log = {}

mask_seeds = s["n_seeds"] >= MIN_SEEDS
filter_log["n_seeds"] = (~mask_seeds).sum()

mask_ext = True
if "smart_extension_count_mean" in s.columns:
    mask_ext = s["smart_extension_count_mean"] > 0
    filter_log["extension=0"] = (~mask_ext).sum()

mask_ped = True
if "baseline_pedestrian_crossing_count_mean" in s.columns:
    mask_ped = s["baseline_pedestrian_crossing_count_mean"] > 0
    filter_log["no_ped"] = (~mask_ped).sum()

mask_veh = True
if "baseline_local_500m_vehicle_count_mean" in s.columns:
    mask_veh = s["baseline_local_500m_vehicle_count_mean"] > 0
    filter_log["no_veh"] = (~mask_veh).sum()

# extension_count 대신 total_extension_sec 사용 (extension_count 없는 경우 대비)
if "smart_extension_count_mean" not in s.columns and "smart_total_extension_sec_mean" in s.columns:
    mask_ext = s["smart_total_extension_sec_mean"] > 0
    filter_log["extension_sec=0"] = (~mask_ext).sum()

valid_mask = mask_seeds & mask_ext & mask_ped & mask_veh
candidates = s[valid_mask].copy()

print(f"  필터 전: {len(s)}  필터 후: {len(candidates)}")
for k, v in filter_log.items():
    if v > 0:
        print(f"    제외 사유 '{k}': {v}건")

if len(candidates) < 7:
    print(f"  ⚠ 필터 통과 후보가 {len(candidates)}개뿐. 필터 완화(n_seeds >= 10)하여 재시도.")
    mask_seeds2 = s["n_seeds"] >= 10
    valid_mask2 = mask_seeds2 & mask_ext & mask_ped & mask_veh
    candidates = s[valid_mask2].copy()
    print(f"  완화 후 후보 수: {len(candidates)}")

# ── 점수 계산 (min-max 0~100 정규화 후 가중 합산) ────────────────────────────
def minmax_score(series: pd.Series, higher_is_better: bool = True) -> pd.Series:
    mn, mx = series.min(), series.max()
    if mx == mn:
        return pd.Series(50.0, index=series.index)
    normalized = (series - mn) / (mx - mn) * 100
    return normalized if higher_is_better else 100 - normalized


# score_ped_benefit: ped_wait_time 감소가 클수록 좋음 (delta 음수가 좋음 → -delta 클수록 좋음)
if "delta_ped_wait_time_mean_mean" in candidates.columns:
    candidates["score_ped_benefit"] = minmax_score(-candidates["delta_ped_wait_time_mean_mean"])
elif "delta_veh_avg_delay_sec_mean" in candidates.columns:
    candidates["score_ped_benefit"] = minmax_score(-candidates["delta_veh_avg_delay_sec_mean"])
else:
    candidates["score_ped_benefit"] = 50.0

# score_traffic_cost: veh_delay 증가가 작을수록 좋음 (delta 작을수록 좋음)
veh_delta_col = next(
    (c for c in ["delta_veh_delay_mean_mean", "delta_veh_avg_delay_sec_mean",
                 "delta_network_avg_delay_sec_mean", "delta_local_500m_avg_delay_sec_mean"]
     if c in candidates.columns),
    None,
)
if veh_delta_col:
    candidates["score_traffic_cost"] = minmax_score(-candidates[veh_delta_col])
else:
    candidates["score_traffic_cost"] = 50.0

# score_extension_active: 총 연장 시간이 클수록 활성화 많음
ext_col = next(
    (c for c in ["smart_total_extension_sec_mean", "smart_extension_count_mean",
                 "smart_pedestrian_green_extension_count_mean"]
     if c in candidates.columns),
    None,
)
if ext_col:
    candidates["score_extension_active"] = minmax_score(candidates[ext_col])
else:
    candidates["score_extension_active"] = 50.0

# score_safety: 안전 지표 개선 (delta 음수가 좋음)
safety_delta_col = next(
    (c for c in ["delta_safety_risk_score_mean", "delta_accident_risk_estimate_mean",
                 "delta_low_pet_event_count_mean", "delta_very_risky_crossing_count_mean"]
     if c in candidates.columns),
    None,
)
if safety_delta_col:
    candidates["score_safety"] = minmax_score(-candidates[safety_delta_col])
else:
    candidates["score_safety"] = 50.0

# 가중 합산
candidates["selection_score"] = (
    0.35 * candidates["score_ped_benefit"]
    + 0.25 * candidates["score_traffic_cost"]
    + 0.20 * candidates["score_extension_active"]
    + 0.20 * candidates["score_safety"]
)

candidates = candidates.sort_values("selection_score", ascending=False).reset_index(drop=True)
top7 = candidates.head(7).copy()
top7["rank"] = range(1, len(top7) + 1)

# selection_reason 생성
def make_reason(row: pd.Series) -> str:
    parts = []
    if row.get("score_ped_benefit", 0) >= 70:
        parts.append("보행자 대기시간 감소")
    if row.get("score_extension_active", 0) >= 70:
        parts.append("신호 연장 활성")
    if row.get("score_safety", 0) >= 70:
        parts.append("안전 지표 개선")
    if row.get("score_traffic_cost", 0) >= 70:
        parts.append("차량 지체 증가 적음")
    if not parts:
        parts.append("종합 점수 상위")
    return " + ".join(parts)

top7["selection_reason"] = top7.apply(make_reason, axis=1)

top7.to_csv(OUTPUT_DIR / "final_top7_candidates.csv", index=False, encoding="utf-8-sig")
print(f"\n  ✓ Top7 선정 결과:")
print(top7[["rank", "crosswalk_id", "group", "n_seeds", "selection_score", "selection_reason"]].to_string(index=False))

# ─────────────────────────────────────────────────────────────────────────────
# Step 6: F. final_top7_mapping_audit.csv
# ─────────────────────────────────────────────────────────────────────────────
print("\n" + "=" * 60)
print("Step 6: F. final_top7_mapping_audit.csv")
print("=" * 60)

# 소스 1: candidate_metadata
meta_df = pd.read_csv(CANDIDATE_META_CSV) if CANDIDATE_META_CSV.exists() else pd.DataFrame()
meta_df["crosswalk_id"] = meta_df["crosswalk_id"].astype(str) if not meta_df.empty else meta_df

# 소스 2: stepwise (좌표)
try:
    step_df = pd.read_csv(STEPWISE_CSV, encoding="cp949")
except UnicodeDecodeError:
    step_df = pd.read_csv(STEPWISE_CSV, encoding="utf-8")
step_df["crosswalk_id"] = step_df["crosswalk_id"].astype(str)

# 소스 3: 각 그룹 baseline seed01의 simulation_result.csv (tls_id_used, crossing_edge_id)
tls_rows = []
for sim_path in sorted(RUNS_ROOT.glob("*/baseline/seed01/simulation_result.csv")):
    try:
        df_sim = pd.read_csv(sim_path)
        needed = [c for c in ["crosswalk_id", "tls_id_used", "crossing_edge_id"] if c in df_sim.columns]
        if "crosswalk_id" not in needed:
            continue
        sub = df_sim[needed].drop_duplicates("crosswalk_id")
        sub["crosswalk_id"] = sub["crosswalk_id"].astype(str)
        tls_rows.append(sub)
    except Exception:
        pass
tls_df = pd.concat(tls_rows, ignore_index=True).drop_duplicates("crosswalk_id") if tls_rows else pd.DataFrame()

# 소스 4: 레지스트리 (NODE_ prefix 제거 후 정수 키)
registry = pd.read_csv(REGISTRY_CSV) if REGISTRY_CSV.exists() else pd.DataFrame()
if not registry.empty:
    registry["crosswalk_id_int"] = pd.to_numeric(registry["crosswalk_id"], errors="coerce")
    reg_cols = ["crosswalk_id_int", "match_distance_m", "mapping_confidence",
                "tls_id", "crossing_edge", "crossing_lon", "crossing_lat",
                "runnable_for_signal_extension"]
    registry = registry[[c for c in reg_cols if c in registry.columns]]

# 모든 34 후보에 대해 매핑 audit 생성
all_cw_ids = summary_df["crosswalk_id"].unique().tolist()
_top7_ids = set(top7["crosswalk_id"].values)


def _build_mapping_row(cw_id: str) -> dict:
    r: dict = {"crosswalk_id": cw_id}

    if not meta_df.empty:
        meta_row = meta_df[meta_df["crosswalk_id"] == cw_id]
        if not meta_row.empty:
            r["dong_name"] = meta_row.iloc[0].get("dong_name", "")
            r["net_group"] = meta_row.iloc[0].get("net_group", "")
            r["net_file"] = meta_row.iloc[0].get("net_file", "")
            r["risk_rank"] = meta_row.iloc[0].get("risk_rank", np.nan)
            r["risk_score"] = meta_row.iloc[0].get("risk_score", np.nan)

    step_row = step_df[step_df["crosswalk_id"] == cw_id]
    if not step_row.empty:
        r["lat"] = step_row.iloc[0].get("lat", np.nan)
        r["lon"] = step_row.iloc[0].get("lon", np.nan)
        if "dong_name" not in r:
            r["dong_name"] = step_row.iloc[0].get("dong_name", "")

    if not tls_df.empty:
        tls_row = tls_df[tls_df["crosswalk_id"] == cw_id]
        if not tls_row.empty:
            r["tls_id_used"] = tls_row.iloc[0].get("tls_id_used", "")
            r["crossing_edge_id"] = tls_row.iloc[0].get("crossing_edge_id", "")

    if not registry.empty:
        numeric_id = None
        if cw_id.startswith("NODE_"):
            try:
                numeric_id = int(cw_id.replace("NODE_", ""))
            except ValueError:
                pass
        if numeric_id is not None:
            reg_row = registry[registry["crosswalk_id_int"] == numeric_id]
            if not reg_row.empty:
                r["match_distance_m"] = reg_row.iloc[0].get("match_distance_m", np.nan)
                r["mapping_confidence"] = reg_row.iloc[0].get("mapping_confidence", np.nan)
                r["runnable_for_signal_extension"] = reg_row.iloc[0].get("runnable_for_signal_extension", "")
                if "crossing_lon" in reg_row.columns and pd.isna(r.get("lon")):
                    r["lon"] = reg_row.iloc[0].get("crossing_lon", np.nan)
                    r["lat"] = reg_row.iloc[0].get("crossing_lat", np.nan)

    r["tls_ok"] = bool(r.get("tls_id_used") and str(r.get("tls_id_used", "")).strip() not in ("", "nan"))

    sum_row = summary_df[summary_df["crosswalk_id"] == cw_id]
    if not sum_row.empty and "baseline_pedestrian_crossing_count_mean" in sum_row.columns:
        ped_mean = sum_row.iloc[0]["baseline_pedestrian_crossing_count_mean"]
        r["ped_route_ok"] = bool(not np.isnan(ped_mean) and ped_mean > 0)
    else:
        r["ped_route_ok"] = False

    dist = r.get("match_distance_m", np.nan)
    if cw_id.startswith("LINK_"):
        r["mapping_status"] = "link_type"
        r["mapping_reason"] = "LINK 타입 후보 – 레지스트리 NODE 키 매핑 불가, tls_id_used로 대체"
    elif np.isnan(dist) if isinstance(dist, float) else False:
        r["mapping_status"] = "unknown"
        r["mapping_reason"] = "레지스트리 매핑 정보 없음"
    elif dist < 50:
        r["mapping_status"] = "exact"
        r["mapping_reason"] = f"현실 좌표와 SUMO crossing 거리 {dist:.1f}m (< 50m)"
    elif dist < 100:
        r["mapping_status"] = "approx"
        r["mapping_reason"] = f"근사 매핑 {dist:.1f}m (50~100m)"
    else:
        r["mapping_status"] = "distant"
        r["mapping_reason"] = f"매핑 거리 큼 {dist:.1f}m (> 100m)"

    r["is_top7"] = cw_id in _top7_ids
    return r


if JOBS == 1:
    mapping_rows = [_build_mapping_row(cw_id) for cw_id in all_cw_ids]
else:
    mapping_rows_dict: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=JOBS) as pool:
        futures = {pool.submit(_build_mapping_row, cw_id): cw_id for cw_id in all_cw_ids}
        for future in as_completed(futures):
            row = future.result()
            mapping_rows_dict[row["crosswalk_id"]] = row
    mapping_rows = [mapping_rows_dict[cw_id] for cw_id in all_cw_ids]

mapping_df = pd.DataFrame(mapping_rows)
mapping_df.to_csv(OUTPUT_DIR / "final_top7_mapping_audit.csv", index=False, encoding="utf-8-sig")
print(f"  저장 완료: {OUTPUT_DIR / 'final_top7_mapping_audit.csv'}  ({len(mapping_df)} 행)")

# ─────────────────────────────────────────────────────────────────────────────
# Step 7: E. final_top7_report_table.csv
# ─────────────────────────────────────────────────────────────────────────────
print("\n" + "=" * 60)
print("Step 7: E. final_top7_report_table.csv")
print("=" * 60)

report_cols_wanted = [
    "rank", "crosswalk_id",
    "baseline_ped_wait_time_mean_mean", "smart_ped_wait_time_mean_mean",
    "delta_ped_wait_time_mean_mean",
    "baseline_veh_delay_mean_mean", "smart_veh_delay_mean_mean",
    "delta_veh_delay_mean_mean",
    "baseline_veh_avg_delay_sec_mean", "smart_veh_avg_delay_sec_mean",
    "delta_veh_avg_delay_sec_mean",
    "smart_total_extension_sec_mean",
    "smart_extension_count_mean",
    "baseline_low_pet_event_count_mean", "smart_low_pet_event_count_mean",
    "delta_low_pet_event_count_mean",
    "baseline_network_avg_delay_sec_mean", "smart_network_avg_delay_sec_mean",
    "delta_network_avg_delay_sec_mean",
    "n_seeds", "selection_score", "selection_reason",
]

# dong_name 병합
report_df = top7.merge(
    mapping_df[["crosswalk_id", "dong_name", "lat", "lon"]].drop_duplicates("crosswalk_id"),
    on="crosswalk_id",
    how="left",
)

report_cols_actual = (
    ["rank", "crosswalk_id", "dong_name"]
    + [c for c in report_cols_wanted if c in report_df.columns and c not in ("rank", "crosswalk_id")]
)
report_df = report_df[[c for c in report_cols_actual if c in report_df.columns]]

# 소수점 포맷
float_cols = report_df.select_dtypes(include="float").columns
report_df[float_cols] = report_df[float_cols].round(3)

report_df.to_csv(OUTPUT_DIR / "final_top7_report_table.csv", index=False, encoding="utf-8-sig")
print(f"  저장 완료: {OUTPUT_DIR / 'final_top7_report_table.csv'}")
print(report_df[["rank", "crosswalk_id", "dong_name"] + [c for c in report_df.columns if "delta_ped" in c]].to_string(index=False))

# ─────────────────────────────────────────────────────────────────────────────
# Step 8: G. final_top7_map.geojson
# ─────────────────────────────────────────────────────────────────────────────
print("\n" + "=" * 60)
print("Step 8: G. final_top7_map.geojson")
print("=" * 60)


def make_circle_polygon(lon: float, lat: float, radius_m: float, n_pts: int = 36) -> list:
    """위경도 중심으로부터 radius_m 반경 원 근사 polygon 좌표 반환."""
    coords = []
    lat_deg = radius_m / 111320.0
    lon_deg = radius_m / (111320.0 * math.cos(math.radians(lat)))
    for i in range(n_pts + 1):
        angle = math.radians(i * 360 / n_pts)
        coords.append([
            round(lon + lon_deg * math.cos(angle), 6),
            round(lat + lat_deg * math.sin(angle), 6),
        ])
    return coords


features = []

top7_map = top7.merge(
    mapping_df[["crosswalk_id", "dong_name", "lat", "lon",
                "tls_id_used", "mapping_status", "tls_ok", "ped_route_ok"]].drop_duplicates("crosswalk_id"),
    on="crosswalk_id",
    how="left",
)

# delta 지표 컬럼 후보
ped_delta_col = next(
    (c for c in ["delta_ped_wait_time_mean_mean", "delta_veh_avg_delay_sec_mean"]
     if c in top7_map.columns),
    None,
)
ext_col_map = next(
    (c for c in ["smart_total_extension_sec_mean", "smart_extension_count_mean"]
     if c in top7_map.columns),
    None,
)
veh_delta_col_map = next(
    (c for c in ["delta_veh_delay_mean_mean", "delta_veh_avg_delay_sec_mean",
                 "delta_network_avg_delay_sec_mean"]
     if c in top7_map.columns),
    None,
)

for _, row in top7_map.iterrows():
    lat = row.get("lat")
    lon = row.get("lon")

    if pd.isna(lat) or pd.isna(lon):
        print(f"  ⚠ {row['crosswalk_id']}: 좌표 없음 → GeoJSON에서 제외")
        continue

    props = {
        "crosswalk_id": str(row["crosswalk_id"]),
        "rank": int(row["rank"]),
        "dong_name": str(row.get("dong_name", "")),
        "group": str(row.get("group", "")),
        "n_seeds": int(row.get("n_seeds", 0)),
        "selection_score": round(float(row.get("selection_score", 0)), 2),
        "selection_reason": str(row.get("selection_reason", "")),
        "tls_id_used": str(row.get("tls_id_used", "")),
        "mapping_status": str(row.get("mapping_status", "")),
        "tls_ok": bool(row.get("tls_ok", False)),
        "ped_route_ok": bool(row.get("ped_route_ok", False)),
    }

    # 주요 지표 추가
    for col in [ped_delta_col, ext_col_map, veh_delta_col_map,
                "delta_network_avg_delay_sec_mean",
                "baseline_local_500m_vehicle_count_mean"]:
        if col and col in row.index and not pd.isna(row[col]):
            props[col] = round(float(row[col]), 3)

    # Point feature
    features.append({
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [float(lon), float(lat)]},
        "properties": props,
    })

    # 500m buffer circle
    circle_coords = make_circle_polygon(float(lon), float(lat), 500.0)
    buffer_props = {
        "crosswalk_id": str(row["crosswalk_id"]),
        "rank": int(row["rank"]),
        "feature_type": "buffer_500m",
        "dong_name": str(row.get("dong_name", "")),
    }
    if ped_delta_col and ped_delta_col in row.index and not pd.isna(row.get(ped_delta_col)):
        buffer_props["delta_ped_wait"] = round(float(row[ped_delta_col]), 3)
    features.append({
        "type": "Feature",
        "geometry": {"type": "Polygon", "coordinates": [circle_coords]},
        "properties": buffer_props,
    })

geojson = {
    "type": "FeatureCollection",
    "name": "최종 top7 스마트 횡단보도 후보의 중구 내 영향 분포",
    "features": features,
}

geojson_path = OUTPUT_DIR / "final_top7_map.geojson"
with open(geojson_path, "w", encoding="utf-8") as f:
    json.dump(geojson, f, ensure_ascii=False, indent=2)

print(f"  저장 완료: {geojson_path}  ({len(features)} features = {len(features)//2} points + {len(features)//2} buffers)")

# ─────────────────────────────────────────────────────────────────────────────
# 최종 요약
# ─────────────────────────────────────────────────────────────────────────────
print("\n" + "=" * 60)
print("완료 요약")
print("=" * 60)
output_files = [
    ("A", "final_all_seed_rows.csv"),
    ("B", "final_paired_seed_delta.csv"),
    ("C", "final_crosswalk_baseline_smart_summary.csv"),
    ("D", "final_top7_candidates.csv"),
    ("E", "final_top7_report_table.csv"),
    ("F", "final_top7_mapping_audit.csv"),
    ("G", "final_top7_map.geojson"),
]
for label, fname in output_files:
    fpath = OUTPUT_DIR / fname
    size = fpath.stat().st_size if fpath.exists() else 0
    status = "✓" if fpath.exists() else "✗"
    print(f"  {status} [{label}] {fname}  ({size:,} bytes)")

if validation_issues:
    issues_path = OUTPUT_DIR / "validation_issues.json"
    with open(issues_path, "w", encoding="utf-8") as f:
        json.dump(validation_issues, f, ensure_ascii=False, indent=2)
    print(f"\n  ⚠ 검증 이슈 {len(validation_issues)}건 → {issues_path}")

print(f"\n출력 디렉토리: {OUTPUT_DIR}")
