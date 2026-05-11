"""TAAS 사고 데이터에서 baseline calibration target을 추출한다.

출처: https://taas.koroad.or.kr
입력: data/raw/taas_raw.xlsx
출력: data/taas_calibration_targets.json
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

import pandas as pd

PROJECT_DIR = Path(__file__).resolve().parents[2]

DEFAULT_INPUT = PROJECT_DIR / "data" / "raw" / "taas_raw.xlsx"
DEFAULT_OUTPUT = PROJECT_DIR / "data" / "taas_calibration_targets.json"

SEVERITY_MAP = {
    "사망": "fatal",
    "중상": "serious",
    "경상": "minor",
    "부상신고": "injury_report",
}


def _extract_age_number(text: str) -> int | None:
    s = str(text).strip()
    m = re.search(r"(\d+)", s)
    if not m:
        return None
    return int(m.group(1))


def _is_senior(age_text: str) -> bool:
    # "65세이상", "70대", "80세이상", "75" 등 처리
    s = str(age_text).strip()
    if "이상" in s:
        n = _extract_age_number(s)
        return n is not None and n >= 65
    n = _extract_age_number(s)
    if n is None:
        return False
    # 연령대 표현 ("60대" → 60s, "70대" → 70s)
    if "대" in s:
        return n >= 70
    return n >= 65


def _compute_stats(df: pd.DataFrame) -> dict[str, Any]:
    total = len(df)
    known_severity = df["severity_label"].isin(SEVERITY_MAP.values())
    classified = int(known_severity.sum())
    df_cls = df[known_severity]

    senior_count = int(df["is_senior"].sum()) if "is_senior" in df.columns else 0
    senior_rate = round(senior_count / total, 4) if total > 0 else 0.0

    severity_counts: dict[str, int] = {v: 0 for v in SEVERITY_MAP.values()}
    for label, cnt in df_cls["severity_label"].value_counts().items():
        if label in severity_counts:
            severity_counts[label] = int(cnt)

    severity_dist: dict[str, float] = {}
    for label, cnt in severity_counts.items():
        severity_dist[label] = round(cnt / classified, 4) if classified > 0 else 0.0

    return {
        "total_accidents": total,
        "classified_accidents": classified,
        "senior_count": senior_count,
        "senior_rate": senior_rate,
        "severity_counts": severity_counts,
        "severity_distribution": severity_dist,
    }


def extract(input_path: Path, output_path: Path) -> dict[str, Any]:
    df_raw = pd.read_excel(input_path, engine="openpyxl")

    # 컬럼명 공백 정리
    df_raw.columns = [str(c).strip() for c in df_raw.columns]

    # 서울 중구 필터
    if "시군구" not in df_raw.columns:
        raise ValueError("'시군구' 컬럼이 없습니다.")
    df = df_raw[df_raw["시군구"].astype(str).str.strip() == "서울특별시 중구"].copy()

    # 상해정도 표준화
    sev_col = "피해운전자 상해정도"
    if sev_col not in df.columns:
        raise ValueError(f"'{sev_col}' 컬럼이 없습니다.")
    df["severity_label"] = df[sev_col].astype(str).str.strip().map(SEVERITY_MAP).fillna("unknown")

    # 고령자 판정
    age_col = "피해운전자 연령대"
    if age_col in df.columns:
        df["is_senior"] = df[age_col].astype(str).apply(_is_senior)
    else:
        df["is_senior"] = False

    # 연도 추출 (발생년월: "202301" 또는 "2023-01" 형태 가정)
    date_col = "발생년월"
    if date_col in df.columns:
        df["year"] = df[date_col].astype(str).str[:4].apply(
            lambda x: int(x) if x.isdigit() else None
        )
    else:
        df["year"] = None

    # 횡단보도 기준 필터
    road_col = "도로형태"
    if road_col in df.columns:
        df_cw = df[df[road_col].astype(str).str.contains("횡단보도", na=False)].copy()
    else:
        df_cw = df.copy()

    # 최근 5개년 (2020–2024)
    df_recent = df[df["year"].isin(range(2020, 2025))].copy() if "year" in df.columns else df.copy()
    df_cw_recent = df_cw[df_cw["year"].isin(range(2020, 2025))].copy() if "year" in df_cw.columns else df_cw.copy()

    # 각 subset 통계를 한 번씩만 계산
    stats_all = _compute_stats(df)
    stats_cw = _compute_stats(df_cw)
    stats_recent = _compute_stats(df_recent)
    stats_cw_recent = _compute_stats(df_cw_recent)

    cw_n = stats_cw["total_accidents"]
    cw_recent_n = stats_cw_recent["total_accidents"]
    note = (
        f"junggu_crosswalk_all_years({cw_n}건) 기준 권장."
        f" 최근 5개년 횡단보도({cw_recent_n}건)는 표본 소규모로 보조 참고용."
    )

    calibration_target: dict[str, Any] = {
        "scope": "junggu_crosswalk_all_years",
        "note": note,
        "senior_rate_references": {
            "junggu_crosswalk_all_years": stats_cw["senior_rate"],
            "junggu_all_2020_2024": stats_recent["senior_rate"],
            "junggu_crosswalk_2020_2024": stats_cw_recent["senior_rate"],
        },
        "severity_distribution": stats_cw["severity_distribution"],
        "n_accidents": cw_n,
    }

    result: dict[str, Any] = {
        "region": "서울특별시 중구",
        "source_file": str(input_path),
        "scopes": {
            "junggu_all_years": stats_all,
            "junggu_crosswalk_all_years": stats_cw,
            "junggu_all_2020_2024": stats_recent,
            "junggu_crosswalk_2020_2024": stats_cw_recent,
        },
        "calibration_target": calibration_target,
        "recommended_calibration_target": calibration_target,
    }

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="TAAS 사고 데이터 → calibration target JSON 추출")
    parser.add_argument("--input", default=str(DEFAULT_INPUT))
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    args = parser.parse_args()

    result = extract(Path(args.input), Path(args.output))
    target = result["calibration_target"]["severity_distribution"]
    print(f"출력: {args.output}")
    print(f"junggu_crosswalk_all_years 총 사고: {result['scopes']['junggu_crosswalk_all_years']['total_accidents']}")
    print(f"severity_distribution: {target}")


if __name__ == "__main__":
    main()
