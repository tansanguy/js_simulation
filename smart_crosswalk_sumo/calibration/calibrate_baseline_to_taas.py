"""baseline 시뮬레이션 risk_events로부터 TAAS 기준 severity cutoff를 산출한다.

절차:
1. baseline risk_events_baseline.csv 읽기 (risk_score 컬럼 필수)
2. risk_score 내림차순 정렬
3. TAAS severity 비율로 분위수 기반 cutoff 산출
4. result/<run_name>/calibration/taas_severity_cutoffs.json 저장

주의: 이 cutoff는 baseline에서만 산출한다.
      smart 시나리오에는 새로 보정하지 않고 동일 cutoff를 고정 적용한다.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd

DEFAULT_SEVERITY_DIST = {
    "fatal": 0.0115,
    "serious": 0.5385,
    "minor": 0.3977,
    "injury_report": 0.0524,
}

SEVERITY_ORDER = ["fatal", "serious", "minor", "injury_report"]


def compute_cutoffs(
    scores: np.ndarray,
    severity_dist: dict[str, float],
) -> dict[str, float]:
    s = np.sort(scores)[::-1]  # 내림차순
    n = len(s)
    if n == 0:
        return {f"{label}_min": 0.0 for label in SEVERITY_ORDER}

    cutoffs: dict[str, float] = {}
    cumulative = 0.0
    for label in SEVERITY_ORDER:
        rate = float(severity_dist.get(label, 0.0))
        lower_idx = max(0, int(round((cumulative + rate) * n)) - 1)
        cutoffs[f"{label}_min"] = float(s[lower_idx]) if lower_idx < n else 0.0
        cumulative += rate

    return cutoffs


def calibrate(
    risk_events_path: Path,
    taas_targets_path: Path,
    output_path: Path,
    scope: str = "junggu_crosswalk_all_years",
) -> dict:
    if not risk_events_path.exists():
        raise FileNotFoundError(f"risk_events 파일이 없습니다: {risk_events_path}")
    if not taas_targets_path.exists():
        raise FileNotFoundError(f"taas_calibration_targets가 없습니다: {taas_targets_path}")

    df = pd.read_csv(risk_events_path)
    if "risk_score" not in df.columns:
        raise ValueError("risk_events CSV에 'risk_score' 컬럼이 없습니다. injury_severity_model.py를 먼저 실행하세요.")

    scores = df["risk_score"].dropna().to_numpy(dtype=float)
    if len(scores) < 10:
        raise ValueError(
            f"risk_events 샘플이 너무 적습니다 ({len(scores)}개). "
            "cutoff 산출을 위해 최소 10개 이상의 risk_score가 필요합니다."
        )

    with taas_targets_path.open(encoding="utf-8") as f:
        taas = json.load(f)

    severity_dist = (
        taas.get("calibration_target", {}).get("severity_distribution")
        or taas.get("scopes", {}).get(scope, {}).get("severity_distribution")
        or DEFAULT_SEVERITY_DIST
    )

    cutoffs = compute_cutoffs(scores, severity_dist)

    result = {
        "source": str(taas_targets_path),
        "scope": scope,
        "n_risk_events": int(len(scores)),
        "severity_distribution": severity_dist,
        "risk_score_cutoffs": cutoffs,
        "score_stats": {
            "mean": round(float(np.mean(scores)), 4) if len(scores) > 0 else None,
            "std": round(float(np.std(scores)), 4) if len(scores) > 0 else None,
            "min": round(float(np.min(scores)), 4) if len(scores) > 0 else None,
            "max": round(float(np.max(scores)), 4) if len(scores) > 0 else None,
        },
    }

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="baseline risk_events → TAAS severity cutoffs")
    parser.add_argument("--risk_events", required=True, help="risk_events_baseline.csv 경로")
    parser.add_argument("--taas_targets", required=True, help="taas_calibration_targets.json 경로")
    parser.add_argument("--output", required=True, help="taas_severity_cutoffs.json 출력 경로")
    parser.add_argument("--scope", default="junggu_crosswalk_all_years")
    args = parser.parse_args()

    result = calibrate(
        Path(args.risk_events),
        Path(args.taas_targets),
        Path(args.output),
        scope=args.scope,
    )
    print(f"출력: {args.output}")
    print(f"이벤트 수: {result['n_risk_events']}")
    print(f"cutoffs: {result['risk_score_cutoffs']}")


if __name__ == "__main__":
    main()
