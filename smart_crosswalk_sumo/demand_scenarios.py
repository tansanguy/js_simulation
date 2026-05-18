from __future__ import annotations

import csv
from functools import lru_cache
from pathlib import Path
from typing import Any

import numpy as np


DEFAULT_DEMAND_SCENARIO_NAME = "main_realistic_stress"
DEFAULT_VEHICLE_FLOW_SCALE = 1.15
DEFAULT_PEDESTRIAN_SCALE = 1.20
PEDESTRIAN_BASE_DURATION_SEC = 600.0
PROJECT_ROOT = Path(__file__).resolve().parents[1]
PEDESTRIAN_ASSUMPTION_PATH_CANDIDATES = [
    PROJECT_ROOT / "result" / "active" / "pedestrian_assumption" / "crosswalk_pedestrian_assumptions_top50_daytime_high_1p2.csv",
    PROJECT_ROOT / "result" / "active" / "pedestrian_assumption" / "crosswalk_pedestrian_assumptions_daytime_high_1p2.csv",
]

MAIN_REALISTIC_STRESS_PEDESTRIAN_COUNT_600S_BY_ADMIN_DONG: dict[str, int] = {
    "광희동": 451,
    "명동": 173,
    "회현동": 78,
    "소공동": 42,
    "청구동": 24,
    "신당동": 17,
    "필동": 42,
    "장충동": 31,
    "을지로동": 48,
    "다산동": 37,
    "약수동": 34,
    "신당제5동": 17,
    "동화동": 30,
    "황학동": 30,
    "중림동": 22,
}

MAIN_REALISTIC_STRESS_ELDERLY_RATIO_BY_ADMIN_DONG: dict[str, float] = {
    "회현동": 0.2947,
    "청구동": 0.2478,
    "명동": 0.2358,
    "소공동": 0.0977,
}


def resolve_demand_scenario_name(scenario_name: str | None) -> str:
    resolved = str(scenario_name or DEFAULT_DEMAND_SCENARIO_NAME).strip()
    if not resolved:
        resolved = DEFAULT_DEMAND_SCENARIO_NAME
    if resolved != DEFAULT_DEMAND_SCENARIO_NAME:
        raise ValueError(
            f"Only {DEFAULT_DEMAND_SCENARIO_NAME!r} is supported right now; got {resolved!r}."
        )
    return resolved


@lru_cache(maxsize=1)
def load_admin_dong_pedestrian_assumptions() -> dict[str, tuple[int, str]]:
    for path in PEDESTRIAN_ASSUMPTION_PATH_CANDIDATES:
        if not path.exists():
            continue
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            required = {"admin_dong", "final_pedestrian_600s"}
            if not reader.fieldnames or not required.issubset(set(reader.fieldnames)):
                continue
            values: dict[str, list[int]] = {}
            for row in reader:
                admin_dong = str(row.get("admin_dong", "")).strip()
                count_text = str(row.get("final_pedestrian_600s", "")).strip()
                if not admin_dong or not count_text:
                    continue
                try:
                    count = int(round(float(count_text)))
                except Exception:
                    continue
                if count <= 0:
                    continue
                values.setdefault(admin_dong, []).append(count)
            if values:
                return {
                    admin_dong: (int(round(float(np.median(counts)))), f"admin_dong_assumption_csv:{path.name}")
                    for admin_dong, counts in values.items()
                }
    return {
        admin_dong: (count, "admin_dong_assumption_builtin_daytime_high_1p2")
        for admin_dong, count in MAIN_REALISTIC_STRESS_PEDESTRIAN_COUNT_600S_BY_ADMIN_DONG.items()
    }


def resolve_pedestrian_count_600s(
    admin_dong: str,
    *,
    risk_score: float | None = None,
    elderly_ratio: float | None = None,
) -> tuple[int, str]:
    dong = str(admin_dong or "").strip()
    assumptions = load_admin_dong_pedestrian_assumptions()
    payload = assumptions.get(dong)
    if payload is None:
        raise ValueError(f"unsupported admin_dong for pedestrian demand policy: {dong!r}")
    return payload


def resolve_elderly_ratio_for_admin_dong(
    admin_dong: str,
    *,
    candidate_ratio: float | None = None,
) -> tuple[float, str]:
    dong = str(admin_dong or "").strip()
    if dong in MAIN_REALISTIC_STRESS_ELDERLY_RATIO_BY_ADMIN_DONG:
        return float(MAIN_REALISTIC_STRESS_ELDERLY_RATIO_BY_ADMIN_DONG[dong]), "admin_dong_fixed"
    if candidate_ratio is not None:
        return float(min(1.0, max(0.0, float(candidate_ratio)))), "candidate_row_fallback"
    raise ValueError(f"unsupported admin_dong for elderly ratio policy: {dong!r}")


def scale_pedestrian_count_for_duration(base_count_600s: int, sim_duration: int) -> int:
    if sim_duration <= 0 or base_count_600s <= 0:
        return 0
    scaled = int(round(float(base_count_600s) * float(sim_duration) / PEDESTRIAN_BASE_DURATION_SEC))
    return max(0, scaled)


def build_fixed_departure_times(count: int, sim_duration: int) -> list[float]:
    if count <= 0 or sim_duration <= 0:
        return []
    step = float(sim_duration) / float(count)
    # Midpoint placement keeps arrivals deterministic and avoids the t=0 pile-up.
    return [min(float(sim_duration) - 1e-6, (idx + 0.5) * step) for idx in range(count)]


def resolve_pedestrian_type_counts(
    count: int,
    elderly_ratio: float,
    slow_elderly_share_within_elderly: float = 0.20,
) -> dict[str, int]:
    if count <= 0:
        return {
            "adult": 0,
            "elderly": 0,
            "slow_elderly": 0,
            "elderly_total": 0,
        }
    elderly_ratio = min(1.0, max(0.0, float(elderly_ratio)))
    elderly_count = int(round(float(count) * elderly_ratio))
    elderly_count = max(0, min(count, elderly_count))
    slow_share = min(1.0, max(0.0, float(slow_elderly_share_within_elderly)))
    slow_elderly_count = int(round(float(elderly_count) * slow_share))
    slow_elderly_count = max(0, min(elderly_count, slow_elderly_count))
    regular_elderly_count = max(0, elderly_count - slow_elderly_count)
    adult_count = max(0, count - elderly_count)
    return {
        "adult": int(adult_count),
        "elderly": int(regular_elderly_count),
        "slow_elderly": int(slow_elderly_count),
        "elderly_total": int(elderly_count),
    }


def build_fixed_type_assignments(
    count: int,
    elderly_ratio: float,
    slow_elderly_share_within_elderly: float = 0.20,
) -> list[str]:
    if count <= 0:
        return []
    counts = resolve_pedestrian_type_counts(
        count,
        elderly_ratio,
        slow_elderly_share_within_elderly=slow_elderly_share_within_elderly,
    )
    elderly_total = counts["elderly_total"]
    if elderly_total == 0:
        return ["adult"] * count
    if elderly_total == count and counts["slow_elderly"] == 0:
        return ["elderly"] * count
    if counts["slow_elderly"] == count:
        return ["slow_elderly"] * count
    elderly_indices = np.unique(np.linspace(0, count - 1, elderly_total, dtype=int))
    if elderly_indices.size < elderly_total:
        chosen = set(int(v) for v in elderly_indices.tolist())
        fillers = [idx for idx in range(count) if idx not in chosen]
        needed = elderly_total - elderly_indices.size
        elderly_indices = np.sort(np.concatenate([elderly_indices, np.asarray(fillers[:needed], dtype=int)]))
    slow_count = counts["slow_elderly"]
    slow_indices = np.unique(np.linspace(0, elderly_indices.size - 1, slow_count, dtype=int)) if slow_count > 0 else np.asarray([], dtype=int)
    slow_slots = {
        int(elderly_indices[int(pos)])
        for pos in slow_indices.tolist()
        if 0 <= int(pos) < elderly_indices.size
    }
    if len(slow_slots) < slow_count:
        fillers = [int(idx) for idx in elderly_indices.tolist() if int(idx) not in slow_slots]
        for idx in fillers[: slow_count - len(slow_slots)]:
            slow_slots.add(int(idx))
    elderly_slots = set(int(v) for v in elderly_indices.tolist())
    assignments: list[str] = []
    for idx in range(count):
        if idx in slow_slots:
            assignments.append("slow_elderly")
        elif idx in elderly_slots:
            assignments.append("elderly")
        else:
            assignments.append("adult")
    return assignments


def build_demand_payload(
    admin_dong: str,
    elderly_ratio: float,
    sim_duration: int,
    *,
    risk_score: float | None = None,
) -> dict[str, Any]:
    base_count_600s, source = resolve_pedestrian_count_600s(
        admin_dong,
        risk_score=risk_score,
        elderly_ratio=elderly_ratio,
    )
    generated_count = scale_pedestrian_count_for_duration(base_count_600s, sim_duration)
    return {
        "scenario_name": DEFAULT_DEMAND_SCENARIO_NAME,
        "vehicle_flow_scale": DEFAULT_VEHICLE_FLOW_SCALE,
        "pedestrian_scale": DEFAULT_PEDESTRIAN_SCALE,
        "pedestrian_count_600s": int(base_count_600s),
        "generated_pedestrian_count": int(generated_count),
        "pedestrian_scale_source": source,
    }
