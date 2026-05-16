from __future__ import annotations

from typing import Any

import numpy as np


DEFAULT_DEMAND_SCENARIO_NAME = "main_realistic_stress"
DEFAULT_VEHICLE_FLOW_SCALE = 1.15
DEFAULT_PEDESTRIAN_SCALE = 1.20
PEDESTRIAN_BASE_DURATION_SEC = 600.0

MAIN_REALISTIC_STRESS_PEDESTRIAN_COUNT_600S_BY_ADMIN_DONG: dict[str, int] = {
    "광희동": 451,
    "명동": 173,
    "회현동": 78,
    "소공동": 42,
    "신당동": 17,
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


def resolve_pedestrian_count_600s(
    admin_dong: str,
    *,
    risk_score: float | None = None,
    elderly_ratio: float | None = None,
) -> tuple[int, str]:
    dong = str(admin_dong or "").strip()
    if dong in MAIN_REALISTIC_STRESS_PEDESTRIAN_COUNT_600S_BY_ADMIN_DONG:
        return MAIN_REALISTIC_STRESS_PEDESTRIAN_COUNT_600S_BY_ADMIN_DONG[dong], "admin_dong_fixed"

    score = float(risk_score if risk_score is not None else elderly_ratio if elderly_ratio is not None else 0.0)
    if score >= 5.0:
        return 451, "risk_score_tier_high"
    if score >= 4.0:
        return 173, "risk_score_tier_mid_high"
    if score >= 3.0:
        return 78, "risk_score_tier_mid"
    if score >= 2.0:
        return 42, "risk_score_tier_low"
    return 17, "risk_score_tier_min"


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


def build_fixed_type_assignments(count: int, elderly_ratio: float) -> list[str]:
    if count <= 0:
        return []
    elderly_ratio = min(1.0, max(0.0, float(elderly_ratio)))
    elderly_count = int(round(float(count) * elderly_ratio))
    elderly_count = max(0, min(count, elderly_count))
    if elderly_count == 0:
        return ["adult"] * count
    if elderly_count == count:
        return ["elderly"] * count

    elderly_indices = np.unique(np.linspace(0, count - 1, elderly_count, dtype=int))
    if elderly_indices.size < elderly_count:
        chosen = set(int(v) for v in elderly_indices.tolist())
        fillers = [idx for idx in range(count) if idx not in chosen]
        needed = elderly_count - elderly_indices.size
        elderly_indices = np.sort(np.concatenate([elderly_indices, np.asarray(fillers[:needed], dtype=int)]))
    chosen = set(int(v) for v in elderly_indices.tolist())
    return ["elderly" if idx in chosen else "adult" for idx in range(count)]


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
