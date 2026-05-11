"""차량-보행자 충돌 위험 이벤트를 스냅샷 데이터프레임으로부터 탐지한다.

이 모듈은 SUMO/TraCI 없이 독립 실행 가능하다.
실제 시뮬레이션과의 연동은 이후 Phase에서 수행한다.

입력 스키마:
    pedestrian_frames 컬럼 (필수):
        time_s              float  시뮬레이션 시각 (초)
        pedestrian_id       str    보행자 ID
        crosswalk_id        str    횡단보도 ID
        x                   float  SUMO 좌표 x (m)
        y                   float  SUMO 좌표 y (m)

    pedestrian_frames 컬럼 (선택):
        age                 int    연령 (없으면 is_senior 컬럼으로 대체)
        is_senior           bool   고령자 여부 (age가 없을 때 사용)
        signal_state        str    신호 상태 ("green", "red", "yellow", ...)
        ped_remaining_crossing_time_s  float  보행 신호 잔여시간 (초)

    vehicle_frames 컬럼 (필수):
        time_s              float  시뮬레이션 시각 (초)
        vehicle_id          str    차량 ID
        x                   float  SUMO 좌표 x (m)
        y                   float  SUMO 좌표 y (m)
        speed_mps           float  속도 (m/s)

    vehicle_frames 컬럼 (선택):
        edge_id             str    도로 edge ID
        lane_id             str    차선 ID
        heading_deg         float  진행 방향 (도)

출력 risk_events.csv 스키마:
    time_s, crosswalk_id, pedestrian_id, vehicle_id,
    edge_id, lane_id, vehicle_speed_mps, min_distance_m, ttc_s,
    signal_state, is_senior, ped_remaining_crossing_time_s, risk_score
"""
from __future__ import annotations

import argparse
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pandas as pd

try:
    from .injury_severity_model import RiskScoreParams, compute_risk_score
except ImportError:
    from injury_severity_model import RiskScoreParams, compute_risk_score  # type: ignore

_INF = float("inf")


@dataclass
class RiskEventDetectorParams:
    """risk event 탐지 파라미터."""

    max_detection_distance_m: float = 15.0
    ttc_threshold_s: float = 5.0
    min_speed_mps: float = 0.5
    same_crosswalk_distance_m: float = 20.0
    senior_age_threshold: int = 65
    dedupe_time_window_s: float = 3.0
    min_risk_score_to_keep: float = 0.05


def _euclidean(x1: float, y1: float, x2: float, y2: float) -> float:
    return math.hypot(x1 - x2, y1 - y2)


def _estimate_ttc(distance_m: float, speed_mps: float, min_speed: float) -> float:
    if speed_mps < min_speed:
        return _INF
    return distance_m / speed_mps


def _is_senior_from_row(row: Any, threshold: int) -> bool:
    if "is_senior" in row.index:
        val = row["is_senior"]
        if isinstance(val, bool):
            return val
        if str(val).lower() in ("true", "1", "yes"):
            return True
        if str(val).lower() in ("false", "0", "no"):
            return False
    if "age" in row.index:
        try:
            return int(row["age"]) >= threshold
        except (ValueError, TypeError):
            pass
    return False


def _safe_str(val: Any, default: str = "") -> str:
    if val is None or (isinstance(val, float) and math.isnan(val)):
        return default
    return str(val)


def _safe_float(val: Any, default: float | None = None) -> float | None:
    if val is None or (isinstance(val, float) and math.isnan(val)):
        return default
    try:
        return float(val)
    except (TypeError, ValueError):
        return default


def compute_pair_diagnostics(
    pedestrian_frames: pd.DataFrame,
    vehicle_frames: pd.DataFrame,
) -> dict[str, Any]:
    """보행자-차량 쌍의 거리 분포 진단 통계를 반환한다. SUMO 불필요."""
    diag: dict[str, Any] = {
        "n_ped_frames": int(len(pedestrian_frames)),
        "n_veh_frames": int(len(vehicle_frames)),
        "n_time_steps_matched": 0,
        "n_pairs_total": 0,
        "n_pairs_within_50m": 0,
        "n_pairs_within_30m": 0,
        "n_pairs_within_15m": 0,
        "min_distance_m": None,
        "mean_distance_m": None,
    }
    if pedestrian_frames.empty or vehicle_frames.empty:
        return diag
    for req in ("time_s", "x", "y"):
        if req not in pedestrian_frames.columns or req not in vehicle_frames.columns:
            return diag
    ped = pedestrian_frames.copy()
    veh = vehicle_frames.copy()
    ped["time_s"] = ped["time_s"].astype(float).round(3)
    veh["time_s"] = veh["time_s"].astype(float).round(3)
    merged = pd.merge(ped, veh, on="time_s", suffixes=("_ped", "_veh"))
    if merged.empty:
        return diag
    diag["n_time_steps_matched"] = int(merged["time_s"].nunique())
    diag["n_pairs_total"] = int(len(merged))
    dists = (
        (merged["x_ped"] - merged["x_veh"]) ** 2
        + (merged["y_ped"] - merged["y_veh"]) ** 2
    ).pow(0.5)
    diag["min_distance_m"] = round(float(dists.min()), 3)
    diag["mean_distance_m"] = round(float(dists.mean()), 3)
    diag["n_pairs_within_50m"] = int((dists <= 50.0).sum())
    diag["n_pairs_within_30m"] = int((dists <= 30.0).sum())
    diag["n_pairs_within_15m"] = int((dists <= 15.0).sum())
    return diag


def detect_risk_events_from_frames(
    pedestrian_frames: pd.DataFrame,
    vehicle_frames: pd.DataFrame,
    params: RiskEventDetectorParams | None = None,
    score_params: RiskScoreParams | None = None,
) -> pd.DataFrame:
    """보행자·차량 스냅샷 DataFrame으로부터 위험 이벤트를 탐지한다.

    Parameters
    ----------
    pedestrian_frames:
        시뮬레이션 각 step의 보행자 위치/상태 스냅샷.
    vehicle_frames:
        시뮬레이션 각 step의 차량 위치/상태 스냅샷.
    params:
        탐지 파라미터. None이면 기본값 사용.
    score_params:
        risk_score 계산 파라미터. None이면 기본값 사용.

    Returns
    -------
    pd.DataFrame
        탐지된 risk event 행들. 빈 DataFrame일 수 있음.
    """
    if params is None:
        params = RiskEventDetectorParams()
    if score_params is None:
        score_params = RiskScoreParams()

    if pedestrian_frames.empty or vehicle_frames.empty:
        return _empty_risk_events_df()

    # 필수 컬럼 확인
    for col in ("time_s", "pedestrian_id", "x", "y"):
        if col not in pedestrian_frames.columns:
            raise ValueError(f"pedestrian_frames에 필수 컬럼 없음: '{col}'")
    for col in ("time_s", "vehicle_id", "x", "y", "speed_mps"):
        if col not in vehicle_frames.columns:
            raise ValueError(f"vehicle_frames에 필수 컬럼 없음: '{col}'")

    # time_s를 공통 키로 merge (inner join → 동일 timestep 쌍만)
    ped = pedestrian_frames.copy()
    veh = vehicle_frames.copy()
    ped["time_s"] = ped["time_s"].astype(float).round(3)
    veh["time_s"] = veh["time_s"].astype(float).round(3)

    merged = pd.merge(ped, veh, on="time_s", suffixes=("_ped", "_veh"))
    if merged.empty:
        return _empty_risk_events_df()

    raw_events: list[dict[str, Any]] = []

    for _, row in merged.iterrows():
        px, py = float(row["x_ped"]), float(row["y_ped"])
        vx, vy = float(row["x_veh"]), float(row["y_veh"])
        dist = _euclidean(px, py, vx, vy)

        if dist > params.max_detection_distance_m:
            continue

        speed = float(row["speed_mps"])
        ttc = _estimate_ttc(dist, speed, params.min_speed_mps)

        if ttc > params.ttc_threshold_s and dist > 3.0:
            continue

        is_senior = _is_senior_from_row(row, params.senior_age_threshold)
        signal_state = _safe_str(row.get("signal_state"))
        remaining = _safe_float(row.get("ped_remaining_crossing_time_s"))
        crosswalk_id = _safe_str(row.get("crosswalk_id", "unknown"))
        edge_id = _safe_str(row.get("edge_id", ""))
        lane_id = _safe_str(row.get("lane_id", ""))

        event_row: dict[str, Any] = {
            "vehicle_speed_mps": speed,
            "min_distance_m": dist,
            "ttc_s": ttc if ttc != _INF else None,
            "is_senior": is_senior,
            "signal_state": signal_state,
            "ped_remaining_crossing_time_s": remaining,
        }
        risk_score = compute_risk_score(event_row, score_params)

        if risk_score < params.min_risk_score_to_keep:
            continue

        ttc_out = None if ttc == _INF else round(ttc, 4)

        raw_events.append({
            "time_s": float(row["time_s"]),
            "crosswalk_id": crosswalk_id,
            "pedestrian_id": _safe_str(row["pedestrian_id"]),
            "vehicle_id": _safe_str(row["vehicle_id"]),
            "edge_id": edge_id,
            "lane_id": lane_id,
            "vehicle_speed_mps": round(speed, 4),
            "min_distance_m": round(dist, 4),
            "ttc_s": ttc_out,
            "signal_state": signal_state,
            "is_senior": bool(is_senior),
            "ped_remaining_crossing_time_s": remaining,
            "risk_score": round(risk_score, 6),
        })

    if not raw_events:
        return _empty_risk_events_df()

    df = pd.DataFrame(raw_events)
    df = _deduplicate_events(df, params)
    return df.sort_values(["time_s", "crosswalk_id", "pedestrian_id", "vehicle_id"]).reset_index(drop=True)


def _deduplicate_events(df: pd.DataFrame, params: RiskEventDetectorParams) -> pd.DataFrame:
    """같은 (ped, veh, crosswalk) 쌍의 dedupe_time_window_s 내 이벤트를 최고 risk_score 하나로 축약."""
    if df.empty:
        return df

    df = df.sort_values(["pedestrian_id", "vehicle_id", "crosswalk_id", "time_s"]).copy()
    window = float(params.dedupe_time_window_s)
    keep_mask = [True] * len(df)
    records = df.to_dict(orient="records")

    # 각 (ped, veh, cw) 그룹별 window 처리
    groups: dict[tuple[str, str, str], list[int]] = {}
    for i, r in enumerate(records):
        key = (str(r["pedestrian_id"]), str(r["vehicle_id"]), str(r["crosswalk_id"]))
        groups.setdefault(key, []).append(i)

    for indices in groups.values():
        if len(indices) == 1:
            continue
        times = [records[i]["time_s"] for i in indices]
        scores = [records[i]["risk_score"] for i in indices]

        # sliding window: 각 점에서 window 내 최고 score만 남김
        window_start = 0
        for j in range(len(indices)):
            # window 시작 갱신
            while times[j] - times[window_start] > window:
                window_start += 1
            window_indices = range(window_start, j + 1)
            best = max(window_indices, key=lambda k: scores[k])
            if j != best:
                keep_mask[indices[j]] = False

    return df.iloc[[i for i, k in enumerate(keep_mask) if k]].copy()


def _empty_risk_events_df() -> pd.DataFrame:
    return pd.DataFrame(columns=[
        "time_s", "crosswalk_id", "pedestrian_id", "vehicle_id",
        "edge_id", "lane_id", "vehicle_speed_mps", "min_distance_m", "ttc_s",
        "signal_state", "is_senior", "ped_remaining_crossing_time_s", "risk_score",
    ])


def write_risk_events_csv(df: pd.DataFrame, output_path: str | Path) -> Path:
    """risk events DataFrame을 utf-8-sig CSV로 저장한다."""
    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(out, index=False, encoding="utf-8-sig")
    return out


def _build_synthetic_frames() -> tuple[pd.DataFrame, pd.DataFrame]:
    """synthetic smoke test용 소형 데이터 생성. SUMO 불필요."""
    ped_rows = [
        # cw_001 위에 보행자, 고령자 포함
        {"time_s": 10.0, "pedestrian_id": "ped_01", "crosswalk_id": "cw_001",
         "x": 100.0, "y": 200.0, "age": 70,
         "signal_state": "green", "ped_remaining_crossing_time_s": 4.0},
        {"time_s": 10.0, "pedestrian_id": "ped_02", "crosswalk_id": "cw_001",
         "x": 100.5, "y": 200.5, "age": 35,
         "signal_state": "green", "ped_remaining_crossing_time_s": 4.0},
        {"time_s": 11.0, "pedestrian_id": "ped_01", "crosswalk_id": "cw_001",
         "x": 100.2, "y": 200.2, "age": 70,
         "signal_state": "red", "ped_remaining_crossing_time_s": 1.5},
        # cw_002 위에 보행자
        {"time_s": 15.0, "pedestrian_id": "ped_03", "crosswalk_id": "cw_002",
         "x": 300.0, "y": 400.0, "age": 25,
         "signal_state": "green", "ped_remaining_crossing_time_s": 8.0},
    ]
    veh_rows = [
        # cw_001 근처 차량 (고속, 근접)
        {"time_s": 10.0, "vehicle_id": "veh_A", "x": 103.0, "y": 200.0,
         "speed_mps": 9.0, "edge_id": "edge_main_1", "lane_id": "edge_main_1_0"},
        {"time_s": 10.0, "vehicle_id": "veh_B", "x": 108.0, "y": 200.0,
         "speed_mps": 4.0, "edge_id": "edge_main_1", "lane_id": "edge_main_1_1"},
        {"time_s": 11.0, "vehicle_id": "veh_A", "x": 101.5, "y": 200.0,
         "speed_mps": 8.0, "edge_id": "edge_main_1", "lane_id": "edge_main_1_0"},
        # cw_002 근처 차량 (멀어서 탐지 안 됨)
        {"time_s": 15.0, "vehicle_id": "veh_C", "x": 320.0, "y": 400.0,
         "speed_mps": 6.0, "edge_id": "edge_side_2", "lane_id": "edge_side_2_0"},
    ]
    return pd.DataFrame(ped_rows), pd.DataFrame(veh_rows)


def _cmd_synthetic_smoke(args: argparse.Namespace) -> None:
    ped_frames, veh_frames = _build_synthetic_frames()
    params = RiskEventDetectorParams()
    score_params = RiskScoreParams()

    events = detect_risk_events_from_frames(ped_frames, veh_frames, params, score_params)

    out_path = write_risk_events_csv(events, args.output)
    print(f"탐지된 risk events: {len(events)}행")
    print(f"출력: {out_path}")

    if not events.empty:
        print(f"\n[risk_score 요약]")
        print(f"  min  : {events['risk_score'].min():.4f}")
        print(f"  max  : {events['risk_score'].max():.4f}")
        print(f"  mean : {events['risk_score'].mean():.4f}")
        print(f"\n[is_senior 분포]")
        print(events["is_senior"].value_counts().to_string())
        print(f"\n[sample rows]")
        print(events[["time_s", "crosswalk_id", "pedestrian_id", "vehicle_id",
                       "min_distance_m", "ttc_s", "risk_score", "is_senior"]].to_string(index=False))
    else:
        print("(탐지된 이벤트 없음 — 파라미터 또는 synthetic 데이터 확인 필요)")


REQUIRED_SCHEMA_COLUMNS = [
    "time_s", "crosswalk_id", "pedestrian_id", "vehicle_id",
    "edge_id", "lane_id", "vehicle_speed_mps", "min_distance_m", "ttc_s",
    "signal_state", "is_senior", "ped_remaining_crossing_time_s", "risk_score",
]


def validate_risk_events_csv(csv_path: str | Path) -> dict[str, Any]:
    """risk_events CSV 파일의 스키마를 검증한다. SUMO 불필요."""
    p = Path(csv_path)
    result: dict[str, Any] = {
        "path": str(p),
        "exists": p.exists(),
        "row_count": 0,
        "missing_columns": [],
        "present_columns": [],
        "ok": False,
    }
    if not p.exists():
        return result
    try:
        df = pd.read_csv(p)
        result["row_count"] = int(len(df))
        result["present_columns"] = list(df.columns)
        result["missing_columns"] = [c for c in REQUIRED_SCHEMA_COLUMNS if c not in df.columns]
        result["ok"] = len(result["missing_columns"]) == 0
    except Exception as e:
        result["read_error"] = str(e)
    return result


def _cmd_validate_schema(args: argparse.Namespace) -> None:
    result = validate_risk_events_csv(args.csv)
    print(f"path      : {result['path']}")
    print(f"exists    : {result['exists']}")
    print(f"rows      : {result['row_count']}")
    print(f"ok        : {result['ok']}")
    if result["missing_columns"]:
        print(f"MISSING   : {result['missing_columns']}")
    else:
        print("schema    : all required columns present")


def main() -> None:
    parser = argparse.ArgumentParser(description="risk_event_detector 서브커맨드")
    sub = parser.add_subparsers(dest="command", required=True)

    p_smoke = sub.add_parser("synthetic-smoke", help="SUMO 없이 합성 데이터로 탐지기 검증")
    p_smoke.add_argument(
        "--output",
        default="result/risk_event_detector_synthetic_smoke/risk_events.csv",
    )

    p_val = sub.add_parser("validate-schema", help="risk_events CSV 스키마 검증")
    p_val.add_argument("--csv", required=True, help="검증할 risk_events CSV 경로")

    args = parser.parse_args()
    if args.command == "synthetic-smoke":
        _cmd_synthetic_smoke(args)
    elif args.command == "validate-schema":
        _cmd_validate_schema(args)


if __name__ == "__main__":
    main()
