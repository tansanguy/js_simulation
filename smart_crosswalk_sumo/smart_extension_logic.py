"""신호 확장 결정 로직 — SUMO/TraCI 의존성 없는 순수 Python.

run_simulations.py 런타임과 no-SUMO 진단 스크립트가 동일 함수를 공유한다.
"""
from __future__ import annotations


def is_ped_green_state(state: str, ped_link_indices: list[int]) -> bool:
    if not state or not ped_link_indices:
        return False
    return any(idx < len(state) and state[idx] in {"g", "G"} for idx in ped_link_indices)


def is_yellow_state(state: str) -> bool:
    return any(ch in {"y", "Y"} for ch in state)


def is_all_red_state(state: str) -> bool:
    return bool(state) and all(ch in {"r", "R", "s", "S", "o", "O"} for ch in state)


def is_vehicle_green_state(state: str, ped_link_indices: list[int]) -> bool:
    if not state:
        return False
    if is_ped_green_state(state, ped_link_indices):
        return False
    if is_yellow_state(state) or is_all_red_state(state):
        return False
    return any(ch in {"g", "G"} for ch in state)


def evaluate_smart_extension_decision(
    tls_id: str | None,
    ped_link_indices: list[int],
    tls_state: str,
    remaining_s: float,
    pedestrians_detected: bool,
    extension_count_in_cycle: int,
    extended_in_cycle: bool,
    signal_params: dict,
    sensor_pass: bool = True,
) -> dict[str, object]:
    """보행 신호 확장 여부를 결정한다.

    모든 입력은 TraCI 호출 결과로부터 호출자가 미리 해소해서 넘긴다.
    sensor_pass는 호출자가 random.random() > sensor_fn_rate 를 계산한 결과다.

    Returns:
        {"should_extend": bool, "reason": str, "extension_sec": float}
    """
    if not tls_id:
        return {"should_extend": False, "reason": "missing_tls_id", "extension_sec": 0.0}
    if not ped_link_indices:
        return {"should_extend": False, "reason": "missing_ped_link_indices", "extension_sec": 0.0}
    if not is_ped_green_state(tls_state, ped_link_indices):
        return {"should_extend": False, "reason": "ped_signal_not_green", "extension_sec": 0.0}
    if remaining_s > signal_params["trigger_remaining"]:
        return {"should_extend": False, "reason": "remaining_time_sufficient", "extension_sec": 0.0}
    if extension_count_in_cycle >= signal_params["max_extensions"]:
        return {"should_extend": False, "reason": "max_extension_reached", "extension_sec": 0.0}
    if extended_in_cycle:
        return {"should_extend": False, "reason": "already_extended_in_cycle", "extension_sec": 0.0}
    if not pedestrians_detected:
        return {"should_extend": False, "reason": "no_pedestrians_detected", "extension_sec": 0.0}
    if not sensor_pass:
        return {"should_extend": False, "reason": "sensor_false_negative", "extension_sec": 0.0}
    return {
        "should_extend": True,
        "reason": "all_conditions_met",
        "extension_sec": float(signal_params["extension_increment"]),
    }
