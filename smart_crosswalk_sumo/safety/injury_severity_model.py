"""차량-보행자 위험 이벤트에 대한 risk_score 계산 모델.

score = normalize(
    w_speed * speed_component
  + w_ttc * ttc_component
  + w_distance * distance_component
  + w_signal * signal_component
  + w_senior * senior_component
  + w_remaining_time * remaining_time_component
)

설계 원칙 (taas_calibration_simulation_prompt.md):
- 점수 0~1 범위
- 고령자 가중치 상한 0.15 (결과 과지배 방지)
- 각 component는 sigmoid 기반 0~1 정규화
"""
from __future__ import annotations

import argparse
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pandas as pd


@dataclass
class RiskScoreParams:
    w_speed: float = 0.30
    w_ttc: float = 0.30
    w_distance: float = 0.20
    w_signal: float = 0.10
    w_senior: float = 0.10
    w_remaining_time: float = 0.10

    # component 정규화 기준값
    speed_ref_mps: float = 14.0       # 50 km/h 기준
    ttc_ref_s: float = 3.0            # TTC 임계값
    distance_ref_m: float = 5.0       # 거리 임계값
    remaining_time_ref_s: float = 5.0 # 잔여 횡단시간 임계값

    senior_bonus_cap: float = 0.15    # 고령자 가중치 결과 상한

    def __post_init__(self) -> None:
        # w_senior는 raw score에 포함되지 않는 별도 보너스이므로 합산 검증에서 제외
        raw_total = self.w_speed + self.w_ttc + self.w_distance + self.w_signal + self.w_remaining_time
        if abs(raw_total - 1.0) > 1e-6:
            raise ValueError(f"raw 가중치 합(w_senior 제외)이 1이 아닙니다: {raw_total:.4f}")


def _sigmoid_clamp(x: float, center: float, scale: float = 1.0) -> float:
    z = (x - center) / max(scale, 1e-9)
    return 1.0 / (1.0 + math.exp(-z))


def _speed_component(speed_mps: float, ref: float) -> float:
    if speed_mps <= 0:
        return 0.0
    return float(min(speed_mps / max(ref, 0.1), 1.0))


def _ttc_component(ttc_s: float | None, ref: float) -> float:
    if ttc_s is None or ttc_s < 0:
        return 0.5
    if ttc_s == 0:
        return 1.0
    return float(max(0.0, 1.0 - ttc_s / max(ref, 0.1)))


def _distance_component(dist_m: float | None, ref: float) -> float:
    if dist_m is None or dist_m < 0:
        return 0.5
    if dist_m == 0:
        return 1.0
    return float(max(0.0, 1.0 - dist_m / max(ref, 0.1)))


def _signal_component(signal_state: str) -> float:
    s = str(signal_state).lower()
    if "red" in s or "stop" in s:
        return 0.9
    if "yellow" in s or "amber" in s:
        return 0.6
    if "green" in s or "go" in s:
        return 0.1
    return 0.4


def _senior_component(is_senior: bool) -> float:
    return 1.0 if is_senior else 0.0


def _remaining_time_component(remaining_s: float | None, ref: float) -> float:
    if remaining_s is None or remaining_s < 0:
        return 0.5
    if remaining_s == 0:
        return 1.0
    return float(max(0.0, 1.0 - remaining_s / max(ref, 0.1)))


def compute_risk_score(event_row: dict[str, Any], params: RiskScoreParams | None = None) -> float:
    if params is None:
        params = RiskScoreParams()

    speed = float(event_row.get("vehicle_speed_mps", 0.0) or 0.0)
    ttc = event_row.get("ttc_s")
    if ttc is not None:
        ttc = float(ttc)
    dist = event_row.get("min_distance_m")
    if dist is not None:
        dist = float(dist)
    signal = str(event_row.get("signal_state", "") or "")
    is_senior = bool(event_row.get("is_senior", False))
    remaining = event_row.get("ped_remaining_crossing_time_s")
    if remaining is not None:
        remaining = float(remaining)

    c_speed = _speed_component(speed, params.speed_ref_mps)
    c_ttc = _ttc_component(ttc, params.ttc_ref_s)
    c_dist = _distance_component(dist, params.distance_ref_m)
    c_signal = _signal_component(signal)
    c_senior = _senior_component(is_senior)
    c_remaining = _remaining_time_component(remaining, params.remaining_time_ref_s)

    raw = (
        params.w_speed * c_speed
        + params.w_ttc * c_ttc
        + params.w_distance * c_dist
        + params.w_signal * c_signal
        + params.w_remaining_time * c_remaining
    )
    # 고령자 보너스 (상한 적용)
    senior_bonus = params.w_senior * c_senior
    capped_bonus = min(float(senior_bonus), params.senior_bonus_cap * (raw + senior_bonus + 1e-9))
    score = float(min(1.0, max(0.0, raw + capped_bonus)))
    return score


_SEVERITY_ORDER = ["fatal", "serious", "minor", "injury_report"]


def classify_severity(score: float, cutoffs: dict[str, float]) -> str:
    """risk_score를 severity 라벨로 분류한다.

    cutoffs 키: fatal_min, serious_min, minor_min (taas_severity_cutoffs.json 기준).
    score가 fatal_min 이상이면 fatal, serious_min 이상이면 serious, 이하는 injury_report.
    """
    s = float(score)
    if s >= float(cutoffs.get("fatal_min", 1.0)):
        return "fatal"
    if s >= float(cutoffs.get("serious_min", 1.0)):
        return "serious"
    if s >= float(cutoffs.get("minor_min", 1.0)):
        return "minor"
    return "injury_report"


def score_dataframe(df: pd.DataFrame, params: RiskScoreParams | None = None) -> pd.DataFrame:
    if params is None:
        params = RiskScoreParams()
    df = df.copy()
    df["risk_score"] = df.apply(lambda r: compute_risk_score(r.to_dict(), params), axis=1)
    return df


def main() -> None:
    parser = argparse.ArgumentParser(description="risk event CSV에 risk_score 컬럼 추가")
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    df = pd.read_csv(args.input)
    df = score_dataframe(df)
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(out, index=False, encoding="utf-8-sig")
    print(f"risk_score 추가 완료: {out} ({len(df)}행)")


if __name__ == "__main__":
    main()
