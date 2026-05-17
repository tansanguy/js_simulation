from __future__ import annotations

import argparse
import csv
import json
import math
import os
import random
import shutil
import socket
import time
import xml.etree.ElementTree as ET
from collections import Counter, deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

try:
    from scipy.stats import invgauss, weibull_min, expon  # type: ignore
    _SCIPY_AVAILABLE = True
except ImportError:
    invgauss = None
    weibull_min = None
    expon = None
    _SCIPY_AVAILABLE = False

try:
    import traci  # type: ignore
except ImportError:  # pragma: no cover
    traci = None

try:
    from .model_config import get_parameter_value, load_model_parameters
    from .network_utils import (
        distance_to_edge_shape,
        edge_allows,
        edge_center,
        edge_function,
        lane_allows,
        load_metadata,
        normalized_sumo_home,
        pedestrian_link_indices,
        read_net,
        recent_values,
    )
    from .sensitivity import (
        apply_parameter_value_overrides,
        load_sensitivity_scenarios,
        resolve_sensitivity_case,
    )
except ImportError:
    from model_config import get_parameter_value, load_model_parameters
    from network_utils import (
        distance_to_edge_shape,
        edge_allows,
        edge_center,
        edge_function,
        lane_allows,
        load_metadata,
        normalized_sumo_home,
        pedestrian_link_indices,
        read_net,
        recent_values,
    )
    from sensitivity import apply_parameter_value_overrides, load_sensitivity_scenarios, resolve_sensitivity_case

try:
    from .smart_extension_logic import (
        evaluate_smart_extension_decision,
        is_all_red_state,
        is_ped_green_state,
        is_vehicle_green_state,
        is_yellow_state,
    )
except ImportError:
    from smart_extension_logic import (
        evaluate_smart_extension_decision,
        is_all_red_state,
        is_ped_green_state,
        is_vehicle_green_state,
        is_yellow_state,
    )


SIGNAL_PARAMS = {
    "cycle_time": 120.0,
    "yellow_time": 4.0,
    "all_red_time": 3.0,
    "ped_entry_time": 7.0,
    "extension_increment": 5.0,
    "max_extensions": 1,
    "trigger_remaining": 10.0,
    "clearance_time": 2.0,
    "sensor_fn_rate": 0.05,
}

VEHICLE_MODEL_PARAMS = {
    "arrival_model": "poisson",
    "arrival_rate_per_hour": None,
    "saturation_flow_rate_per_hour": 1900.0,
    "num_lanes": 1,
}

DEFAULT_RANDOM_DISRUPTION_RATES = {
    "bus_stop": 0.0,
    "illegal_parking": 0.0,
    "minor_incident": 0.0,
    "accident": 0.0,
}


COMPASS_DIRECTIONS = ("N", "E", "S", "W")


@dataclass(frozen=True)
class IncidentEvent:
    incident_id: str
    event_type: str
    start_time: float
    end_time: float
    affected_edge_ids: tuple[str, ...]
    affected_lane_ids: tuple[str, ...]
    severity: float
    capacity_multiplier: float
    speed_multiplier: float
    blocked_lanes_count: int
    allow_rerouting: bool


@dataclass
class LaneState:
    max_speed: float
    allowed: tuple[str, ...] | None = None
    disallowed: tuple[str, ...] | None = None


@dataclass
class StepMetricCache:
    lane_metrics: dict[str, dict[str, float] | None]
    vehicle_state: dict[str, dict[str, float | str] | None]

    def reset(self) -> None:
        self.lane_metrics.clear()
        self.vehicle_state.clear()


def _lane_metric_snapshot(lane_id: str, cache: dict[str, dict[str, float] | None]) -> dict[str, float] | None:
    if lane_id in cache:
        return cache[lane_id]
    try:
        lane_state = {
            "halting_number": float(traci.lane.getLastStepHaltingNumber(lane_id)),
            "occupancy": float(traci.lane.getLastStepOccupancy(lane_id)),
            "mean_speed": float(traci.lane.getLastStepMeanSpeed(lane_id)),
        }
    except Exception:
        cache[lane_id] = None
        return None
    cache[lane_id] = lane_state
    return lane_state


def _vehicle_state_snapshot(
    vehicle_id: str,
    cache: dict[str, dict[str, float | str] | None],
) -> dict[str, float | str] | None:
    if vehicle_id in cache:
        return cache[vehicle_id]
    try:
        vehicle_state = {
            "road_id": str(traci.vehicle.getRoadID(vehicle_id)),
            "lane_id": str(traci.vehicle.getLaneID(vehicle_id)),
            "speed": float(traci.vehicle.getSpeed(vehicle_id)),
            "accumulated_wait": float(traci.vehicle.getAccumulatedWaitingTime(vehicle_id)),
        }
    except Exception:
        cache[vehicle_id] = None
        return None
    cache[vehicle_id] = vehicle_state
    return vehicle_state


def _lane_sample_due(metric_sample_interval_s: float, rel_t: float, next_sample_t: float) -> bool:
    if metric_sample_interval_s <= 0:
        return True
    return rel_t + 1e-9 >= next_sample_t


def free_traci_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])

def sumo_binary() -> str:
    path = shutil.which("sumo")
    if not path:
        raise RuntimeError("sumo 실행 파일을 찾지 못했습니다. SUMO 설치와 PATH를 확인하세요.")
    return path


def traci_trafficlight_ids() -> set[str]:
    try:
        return set(traci.trafficlight.getIDList())
    except Exception:
        return set()


def _json_safe(value: Any) -> Any:
    """Recursively convert non-JSON-serializable types for diagnostic/logging output only."""
    if isinstance(value, set):
        return sorted(_json_safe(v) for v in value)
    if isinstance(value, tuple):
        return [_json_safe(v) for v in value]
    if isinstance(value, list):
        return [_json_safe(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, Path):
        return str(value)
    return value


def _runtime_log_append(path: str | Path | None, payload: dict[str, Any]) -> None:
    if path is None:
        return
    log_path = Path(path)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(_json_safe(payload), ensure_ascii=False, sort_keys=True) + "\n")


def _runtime_failure_write(path: str | Path | None, payload: dict[str, Any]) -> None:
    if path is None:
        return
    failure_path = Path(path)
    failure_path.parent.mkdir(parents=True, exist_ok=True)
    failure_path.write_text(json.dumps(_json_safe(payload), ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")


def _runtime_progress_append(path: str | Path | None, payload: dict[str, Any]) -> None:
    if path is None:
        return
    progress_path = Path(path)
    progress_path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "timestamp",
        "scenario",
        "seed",
        "simulation_time",
        "wall_time_sec",
        "vehicle_count",
        "person_count",
        "min_expected_number",
    ]
    write_header = not progress_path.exists()
    with progress_path.open("a", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        if write_header:
            writer.writeheader()
        writer.writerow({key: payload.get(key, "") for key in fieldnames})


def _active_sumo_return_code(traci_label: str | None) -> int | None:
    if not traci_label:
        return None
    try:
        conn = traci.getConnection(traci_label)
    except Exception:
        return None
    proc = getattr(conn, "_process", None)
    try:
        return None if proc is None else proc.poll()
    except Exception:
        return None


def is_known_trafficlight(tl_id: str | None, known_tls_ids: set[str] | None = None) -> bool:
    if not tl_id:
        return False
    ids = known_tls_ids if known_tls_ids is not None else traci_trafficlight_ids()
    return tl_id in ids


def snapshot_traci_trafficlight_ids(
    sumocfg: str | Path,
    seed: int = 42,
    step_length: float = 0.1,
) -> set[str]:
    if traci is None:
        raise RuntimeError("traci가 설치되어 있지 않습니다.")
    port = free_traci_port()
    label = f"tls_probe_{seed}_{random.randint(0, 1_000_000)}"
    traci.start(
        [
            sumo_binary(),
            "-c",
            str(sumocfg),
            "--seed",
            str(seed),
            "--step-length",
            str(step_length),
            "--no-warnings",
            "--no-step-log",
        ],
        port=port,
        label=label,
    )
    try:
        traci.switch(label)
        try:
            traci.simulationStep()
        except Exception:
            pass
        return traci_trafficlight_ids()
    finally:
        try:
            traci.switch(label)
            traci.close(False)
        except Exception:
            pass


def compass_direction(origin: tuple[float, float], target: tuple[float, float]) -> str:
    dx = float(target[0]) - float(origin[0])
    dy = float(target[1]) - float(origin[1])
    if abs(dx) >= abs(dy):
        return "E" if dx >= 0 else "W"
    return "N" if dy >= 0 else "S"


def build_surrounding_scope(
    net_file: str | Path,
    metadata: dict[str, Any],
    radius_m: float,
) -> dict[str, Any]:
    net = read_net(net_file)
    crossing_edge = net.getEdge(metadata["crossing_edge"])
    crossing_xy = edge_center(crossing_edge)

    lane_ids: list[str] = []
    boundary_lane_ids: list[str] = []
    direction_lanes: dict[str, list[str]] = {direction: [] for direction in COMPASS_DIRECTIONS}
    lane_capacities: dict[str, int] = {}
    lane_to_edge: dict[str, str] = {}

    for edge in net.getEdges():
        if edge_function(edge) != "normal" or not edge_allows(edge, "passenger"):
            continue
        distance_m = distance_to_edge_shape(edge, crossing_xy)
        if distance_m > radius_m:
            continue
        direction = compass_direction(crossing_xy, edge_center(edge))
        for lane in edge.getLanes():
            if not lane_allows(lane, "passenger"):
                continue
            lane_id = lane.getID()
            lane_ids.append(lane_id)
            direction_lanes[direction].append(lane_id)
            lane_to_edge[lane_id] = edge.getID()
            try:
                length_m = float(lane.getLength())
            except Exception:
                length_m = 7.5
            lane_capacities[lane_id] = max(1, int(length_m / 7.5))
            if distance_m >= radius_m * 0.8:
                boundary_lane_ids.append(lane_id)

    if not lane_ids:
        lane_ids = list(metadata.get("approach_lanes", []))
        direction_lanes = {direction: [] for direction in COMPASS_DIRECTIONS}
        direction_lanes["N"] = lane_ids.copy()
        lane_capacities = {lane_id: 1 for lane_id in lane_ids}
        lane_to_edge = {lane_id: lane_id.rsplit("_", 1)[0] for lane_id in lane_ids}

    return {
        "radius_m": float(radius_m),
        "crossing_xy": crossing_xy,
        "monitored_lanes": sorted(set(lane_ids)),
        "boundary_lanes": sorted(set(boundary_lane_ids or lane_ids)),
        "direction_lanes": {
            direction: sorted(set(lanes)) for direction, lanes in direction_lanes.items()
        },
        "lane_capacities": lane_capacities,
        "lane_to_edge": lane_to_edge,
    }


def adjacent_tls_lanes(target_tl_id: str | None, monitored_lanes: set[str]) -> tuple[set[str], int]:
    if not target_tl_id:
        target_tl_id = ""
    lanes: set[str] = set()
    tls_count = 0
    for tls_id in traci.trafficlight.getIDList():
        if tls_id == target_tl_id:
            continue
        controlled = set(traci.trafficlight.getControlledLanes(tls_id))
        scoped = controlled.intersection(monitored_lanes)
        if scoped:
            tls_count += 1
            lanes.update(scoped)
    return lanes, tls_count


def mean_value(values: list[float] | list[int]) -> float:
    arr = np.asarray(values, dtype=float)
    return float(np.nanmean(arr)) if arr.size else 0.0


def max_value(values: list[float] | list[int]) -> float:
    arr = np.asarray(values, dtype=float)
    return float(np.nanmax(arr)) if arr.size else 0.0


def clip_capacity_multiplier(value: float) -> float:
    return float(min(1.0, max(0.01, value)))


def safe_get_lane_state(lane_id: str) -> LaneState:
    max_speed = float(traci.lane.getMaxSpeed(lane_id))
    allowed = None
    disallowed = None
    try:
        allowed = tuple(traci.lane.getAllowed(lane_id))
    except Exception:
        allowed = None
    try:
        disallowed = tuple(traci.lane.getDisallowed(lane_id))
    except Exception:
        disallowed = None
    return LaneState(max_speed=max_speed, allowed=allowed, disallowed=disallowed)


def apply_lane_block_permissions(lane_id: str) -> None:
    # Permission API support differs by SUMO build, so keep speed fallback too.
    try:
        traci.lane.setDisallowed(lane_id, ["passenger"])
    except Exception:
        pass


def restore_lane_permissions(lane_id: str, state: LaneState) -> None:
    if state.allowed is not None:
        try:
            traci.lane.setAllowed(lane_id, list(state.allowed))
        except Exception:
            pass
    if state.disallowed is not None:
        try:
            traci.lane.setDisallowed(lane_id, list(state.disallowed))
        except Exception:
            pass


def apply_incident(
    event: IncidentEvent,
    lane_original_states: dict[str, LaneState],
) -> None:
    for lane_id in event.affected_lane_ids:
        if lane_id not in lane_original_states:
            try:
                lane_original_states[lane_id] = safe_get_lane_state(lane_id)
            except Exception:
                continue
        state = lane_original_states[lane_id]
        new_speed = max(0.1, float(state.max_speed) * float(event.speed_multiplier))
        try:
            traci.lane.setMaxSpeed(lane_id, new_speed)
        except Exception:
            continue
        if event.event_type == "accident":
            apply_lane_block_permissions(lane_id)


def restore_incident(
    event: IncidentEvent,
    lane_original_states: dict[str, LaneState],
) -> None:
    for lane_id in event.affected_lane_ids:
        state = lane_original_states.get(lane_id)
        if not state:
            continue
        try:
            traci.lane.setMaxSpeed(lane_id, float(state.max_speed))
        except Exception:
            pass
        restore_lane_permissions(lane_id, state)


def incident_event_templates(disruption_scenario: str) -> list[tuple[str, float, float, float]]:
    if disruption_scenario == "best_case":
        return []
    if disruption_scenario == "normal_urban":
        return [
            ("bus_stop", 240.0, 40.0, 0.75),
            ("accident", 900.0, 180.0, 0.45),
        ]
    if disruption_scenario == "congested_urban":
        return [
            ("bus_stop", 210.0, 60.0, 0.75),
            ("accident", 870.0, 240.0, 0.45),
        ]
    if disruption_scenario == "incident_case":
        return [
            ("accident", 360.0, 300.0, 0.45),
            ("bus_stop", 900.0, 180.0, 0.75),
        ]
    raise ValueError(f"Unknown disruption_scenario={disruption_scenario}")


def choose_incident_lanes(
    event_type: str,
    monitored_lanes: list[str],
    approach_lanes: list[str],
) -> list[str]:
    if approach_lanes:
        target = sorted(set(approach_lanes))
    else:
        target = sorted(set(monitored_lanes))
    if not target:
        return []
    if event_type in {"bus_stop", "accident"}:
        return [target[-1]]
    return target[: min(3, len(target))]


def _sample_bus_stop_duration(
    rng: np.random.Generator,
    model_params: dict[str, dict[str, Any]],
) -> float:
    """버스 정차 지속시간을 샘플링한다.

    scipy 사용 가능 시 Inverse Gaussian(μ, λ) (Kwesiga et al. 2026),
    미사용 시 lognormal fallback.
    """
    mu = float(get_parameter_value(model_params, "bus_stop_invgauss_mu", 35.0))
    lam = float(get_parameter_value(model_params, "bus_stop_invgauss_lambda", 50.0))
    cap = float(get_parameter_value(model_params, "bus_stop_duration_cap", 180.0))
    if _SCIPY_AVAILABLE and mu > 0 and lam > 0:
        mu_param = mu / max(lam, 1e-9)
        u = float(rng.uniform(1e-9, 1.0 - 1e-9))
        val = float(invgauss.ppf(u, mu_param, scale=lam))
    else:
        sigma = 0.6
        mean_log = math.log(max(mu, 1.0)) - 0.5 * sigma ** 2
        val = float(rng.lognormal(mean=mean_log, sigma=sigma))
    return float(np.clip(val, 5.0, cap))


def _sample_accident_duration_and_severity(
    rng: np.random.Generator,
    model_params: dict[str, dict[str, Any]],
) -> tuple[float, float, str]:
    """사고 지속시간과 심각도를 strata별로 샘플링한다.

    scipy 사용 가능 시 Weibull(k, θ) per strata (Li et al. 2018),
    미사용 시 numpy weibull fallback.
    반환: (duration_sec, severity_float, severity_label)
    """
    raw_probs = get_parameter_value(model_params, "accident_severity_probs", [0.6, 0.3, 0.1])
    if not isinstance(raw_probs, (list, tuple)) or len(raw_probs) != 3:
        raw_probs = [0.6, 0.3, 0.1]
    probs = np.array([float(p) for p in raw_probs], dtype=float)
    probs = probs / probs.sum()
    cap = float(get_parameter_value(model_params, "accident_duration_cap", 900.0))

    strata_labels = ["minor", "moderate", "severe"]
    default_shapes = [1.5, 1.7, 2.0]
    default_scales = [120.0, 240.0, 420.0]
    severity_map = {"minor": 0.35, "moderate": 0.55, "severe": 0.80}

    strata_idx = int(rng.choice(3, p=probs))
    label = strata_labels[strata_idx]
    k = float(get_parameter_value(
        model_params, f"accident_weibull_shape_{label}", default_shapes[strata_idx]
    ))
    theta = float(get_parameter_value(
        model_params, f"accident_weibull_scale_{label}", default_scales[strata_idx]
    ))

    if _SCIPY_AVAILABLE and k > 0 and theta > 0:
        u = float(rng.uniform(1e-9, 1.0 - 1e-9))
        val = float(weibull_min.ppf(u, k, scale=theta))
    else:
        val = float(rng.weibull(k) * theta)

    duration = float(np.clip(val, 30.0, cap))
    return duration, float(severity_map[label]), label


def _sample_illegal_parking_duration(
    rng: np.random.Generator,
    model_params: dict[str, dict[str, Any]],
) -> float:
    """불법 주정차 지속시간을 샘플링한다.

    scipy 사용 가능 시 Exponential(β) (Gao & Ozbay 2016),
    미사용 시 numpy exponential fallback.
    """
    beta = float(get_parameter_value(model_params, "illegal_parking_exp_beta", 180.0))
    cap = float(get_parameter_value(model_params, "illegal_parking_duration_cap", 600.0))
    if _SCIPY_AVAILABLE and beta > 0:
        u = float(rng.uniform(1e-9, 1.0 - 1e-9))
        val = float(expon.ppf(u, scale=beta))
    else:
        val = float(rng.exponential(beta))
    return float(np.clip(val, 10.0, cap))


def generate_incident_schedule(
    disruption_scenario: str,
    sim_duration: int,
    seed: int,
    metadata: dict[str, Any],
    monitored_lanes: list[str],
    model_params: dict[str, dict[str, Any]],
    enable_random_disruptions: bool,
    bus_stop_rate_per_hour: float,
    illegal_parking_rate_per_hour: float,
    minor_incident_rate_per_hour: float,
    accident_rate_per_hour: float,
) -> list[IncidentEvent]:
    rng = np.random.default_rng(seed + 20000)
    approach_lanes = [str(l) for l in metadata.get("approach_lanes", [])]
    conflict_edges = [str(e) for e in metadata.get("vehicle_conflict_edges", [])]

    cap_map = {
        "accident": float(
            get_parameter_value(
                model_params,
                "capacity_multiplier_accident",
                0.45,
            )
        ),
        "bus_stop": float(
            get_parameter_value(
                model_params,
                "capacity_multiplier_bus_stop",
                0.75,
            )
        ),
        "illegal_parking": float(
            get_parameter_value(
                model_params,
                "capacity_multiplier_illegal_parking",
                0.65,
            )
        ),
    }
    speed_map = {
        "accident": float(
            get_parameter_value(
                model_params,
                "speed_multiplier_accident",
                0.45,
            )
        ),
        "bus_stop": float(
            get_parameter_value(
                model_params,
                "speed_multiplier_bus_stop",
                0.8,
            )
        ),
        "illegal_parking": float(
            get_parameter_value(
                model_params,
                "speed_multiplier_illegal_parking",
                0.75,
            )
        ),
    }

    events: list[IncidentEvent] = []
    idx = 0
    for event_type, start, duration, severity in incident_event_templates(disruption_scenario):
        if start >= float(sim_duration):
            continue
        lanes = choose_incident_lanes(event_type, monitored_lanes, approach_lanes)
        if not lanes:
            continue
        end = min(float(sim_duration), float(start + duration))
        if end <= float(start):
            continue
        event = IncidentEvent(
            incident_id=f"inc_{seed}_{idx}",
            event_type=event_type,
            start_time=float(start),
            end_time=float(end),
            affected_edge_ids=tuple(sorted(set(conflict_edges))),
            affected_lane_ids=tuple(sorted(set(lanes))),
            severity=float(severity),
            capacity_multiplier=clip_capacity_multiplier(float(cap_map.get(event_type, severity))),
            speed_multiplier=clip_capacity_multiplier(float(speed_map.get(event_type, severity))),
            blocked_lanes_count=1 if event_type == "accident" else 0,
            allow_rerouting=event_type == "accident",
        )
        events.append(event)
        idx += 1

    if enable_random_disruptions:
        sim_hours = max(float(sim_duration) / 3600.0, 0.0)

        # bus_stop — Inverse Gaussian duration (Kwesiga et al. 2026 / lognormal fallback)
        bus_count = int(rng.poisson(max(bus_stop_rate_per_hour, 0.0) * sim_hours))
        for _ in range(bus_count):
            duration = _sample_bus_stop_duration(rng, model_params)
            start = float(rng.uniform(0.0, max(float(sim_duration) - duration, 0.0)))
            if start >= float(sim_duration):
                continue
            end = min(float(sim_duration), start + duration)
            if end <= start:
                continue
            lanes = choose_incident_lanes("bus_stop", monitored_lanes, approach_lanes)
            if not lanes:
                continue
            events.append(IncidentEvent(
                incident_id=f"inc_{seed}_{idx}",
                event_type="bus_stop",
                start_time=start,
                end_time=end,
                affected_edge_ids=tuple(sorted(set(conflict_edges))),
                affected_lane_ids=tuple(sorted(set(lanes))),
                severity=0.75,
                capacity_multiplier=clip_capacity_multiplier(float(cap_map["bus_stop"])),
                speed_multiplier=clip_capacity_multiplier(float(speed_map["bus_stop"])),
                blocked_lanes_count=0,
                allow_rerouting=False,
            ))
            idx += 1

        # accident — Weibull duration + severity strata (Li et al. 2018)
        acc_count = int(rng.poisson(max(accident_rate_per_hour, 0.0) * sim_hours))
        for _ in range(acc_count):
            duration, severity_float, _severity_label = _sample_accident_duration_and_severity(
                rng, model_params
            )
            start = float(rng.uniform(0.0, max(float(sim_duration) - duration, 0.0)))
            if start >= float(sim_duration):
                continue
            end = min(float(sim_duration), start + duration)
            if end <= start:
                continue
            lanes = choose_incident_lanes("accident", monitored_lanes, approach_lanes)
            if not lanes:
                continue
            events.append(IncidentEvent(
                incident_id=f"inc_{seed}_{idx}",
                event_type="accident",
                start_time=start,
                end_time=end,
                affected_edge_ids=tuple(sorted(set(conflict_edges))),
                affected_lane_ids=tuple(sorted(set(lanes))),
                severity=severity_float,
                capacity_multiplier=clip_capacity_multiplier(float(cap_map["accident"])),
                speed_multiplier=clip_capacity_multiplier(float(speed_map["accident"])),
                blocked_lanes_count=1,
                allow_rerouting=True,
            ))
            idx += 1

        # illegal_parking — Poisson + Exponential duration (Gao & Ozbay 2016)
        park_count = int(rng.poisson(max(illegal_parking_rate_per_hour, 0.0) * sim_hours))
        for _ in range(park_count):
            duration = _sample_illegal_parking_duration(rng, model_params)
            start = float(rng.uniform(0.0, max(float(sim_duration) - duration, 0.0)))
            if start >= float(sim_duration):
                continue
            end = min(float(sim_duration), start + duration)
            if end <= start:
                continue
            lanes = choose_incident_lanes("illegal_parking", monitored_lanes, approach_lanes)
            if not lanes:
                continue
            events.append(IncidentEvent(
                incident_id=f"inc_{seed}_{idx}",
                event_type="illegal_parking",
                start_time=start,
                end_time=end,
                affected_edge_ids=tuple(sorted(set(conflict_edges))),
                affected_lane_ids=tuple(sorted(set(lanes))),
                severity=0.5,
                capacity_multiplier=clip_capacity_multiplier(float(cap_map["illegal_parking"])),
                speed_multiplier=clip_capacity_multiplier(float(speed_map["illegal_parking"])),
                blocked_lanes_count=0,
                allow_rerouting=False,
            ))
            idx += 1

    return sorted(events, key=lambda e: e.start_time)


def parse_incident_schedule(raw_schedule: list[dict[str, Any]] | None) -> list[IncidentEvent]:
    if not raw_schedule:
        return []
    events = []
    for item in raw_schedule:
        events.append(
            IncidentEvent(
                incident_id=str(item["incident_id"]),
                event_type=str(item["event_type"]),
                start_time=float(item["start_time"]),
                end_time=float(item["end_time"]),
                affected_edge_ids=tuple(item.get("affected_edge_ids", [])),
                affected_lane_ids=tuple(item.get("affected_lane_ids", [])),
                severity=float(item.get("severity", 0.6)),
                capacity_multiplier=float(item.get("capacity_multiplier", 0.8)),
                speed_multiplier=float(item.get("speed_multiplier", 0.8)),
                blocked_lanes_count=int(item.get("blocked_lanes_count", 0)),
                allow_rerouting=bool(item.get("allow_rerouting", False)),
            )
        )
    return sorted(events, key=lambda e: e.start_time)


def serialize_incident_event(
    event: IncidentEvent,
    crosswalk_id: str,
    seed: int,
    scenario: str,
) -> dict[str, Any]:
    return {
        "incident_id": event.incident_id,
        "event_type": event.event_type,
        "start_time": event.start_time,
        "end_time": event.end_time,
        "affected_edge_ids": json.dumps(list(event.affected_edge_ids), ensure_ascii=False),
        "affected_lane_ids": json.dumps(list(event.affected_lane_ids), ensure_ascii=False),
        "severity": event.severity,
        "capacity_multiplier": event.capacity_multiplier,
        "speed_multiplier": event.speed_multiplier,
        "blocked_lanes_count": event.blocked_lanes_count,
        "allow_rerouting": int(event.allow_rerouting),
        "crosswalk_id": crosswalk_id,
        "seed": seed,
        "scenario": scenario,
    }


def active_incidents(events: list[IncidentEvent], relative_time: float) -> list[IncidentEvent]:
    return [e for e in events if e.start_time <= relative_time < e.end_time]


def aggregate_metrics(
    pet_a: list[float],
    pet_b: list[float],
    elderly_inc: int,
    pedestrian_count: int,
    elderly_pedestrian_count: int,
    crossing_times: list[float],
    veh_waits: dict[str, float],
    queues: list[int],
    pedestrian_wait_times: list[float],
    vehicle_model_state: dict[str, float | int],
    measure_steps: int,
    sim_duration: int,
) -> dict[str, float | int]:
    metrics: dict[str, float | int] = {}

    def classify_pet(records: list[float], label: str) -> dict[str, float | int]:
        arr = np.asarray(records, dtype=float)
        if arr.size == 0:
            return {
                f"{label}_count": 0,
                f"{label}_mean": math.nan,
                f"{label}_severe": 0,
                f"{label}_moderate": 0,
                f"{label}_safe": 0,
            }
        return {
            f"{label}_count": int(arr.size),
            f"{label}_mean": float(np.nanmean(arr)),
            f"{label}_severe": int(np.sum(arr < 1.34)),
            f"{label}_moderate": int(np.sum((arr >= 1.34) & (arr < 2.88))),
            f"{label}_safe": int(np.sum(arr >= 2.88)),
        }

    metrics.update(classify_pet(pet_a, "PET_A_proxy"))
    metrics.update(classify_pet(pet_b, "PET_B_surrogate"))

    veh_arr = np.asarray(list(veh_waits.values()), dtype=float)
    queue_arr = np.asarray(queues, dtype=float)
    ped_wait_arr = np.asarray(pedestrian_wait_times, dtype=float)
    crossing_arr = np.asarray(crossing_times, dtype=float)

    metrics["elderly_incomplete_cross"] = int(elderly_inc)
    metrics["pedestrian_count"] = int(pedestrian_count)
    metrics["elderly_pedestrian_count"] = int(elderly_pedestrian_count)
    metrics["veh_avg_delay_sec"] = float(np.nanmean(veh_arr)) if veh_arr.size else 0.0
    metrics["veh_max_delay_sec"] = float(np.nanmax(veh_arr)) if veh_arr.size else 0.0
    metrics["queue_avg"] = float(np.nanmean(queue_arr)) if queue_arr.size else 0.0
    metrics["queue_max"] = int(np.nanmax(queue_arr)) if queue_arr.size else 0
    metrics["average_pedestrian_wait_time"] = float(np.nanmean(ped_wait_arr)) if ped_wait_arr.size else 0.0
    metrics["pedestrian_waiting_time_mean"] = float(np.nanmean(ped_wait_arr)) if ped_wait_arr.size else 0.0
    metrics["pedestrian_waiting_time"] = float(np.nanmean(ped_wait_arr)) if ped_wait_arr.size else 0.0
    metrics["pedestrian_waiting_time_p95"] = float(np.nanpercentile(ped_wait_arr, 95)) if ped_wait_arr.size else 0.0
    metrics["max_pedestrian_wait_time"] = float(np.nanmax(ped_wait_arr)) if ped_wait_arr.size else 0.0
    metrics["pedestrian_waiting_time_max"] = float(np.nanmax(ped_wait_arr)) if ped_wait_arr.size else 0.0
    metrics["crossing_time"] = float(np.nanmean(crossing_arr)) if crossing_arr.size else 0.0
    metrics["pedestrian_delay"] = float(np.nanmean(ped_wait_arr)) if ped_wait_arr.size else 0.0

    vehicle_arrivals = int(vehicle_model_state["vehicle_arrivals"])
    vehicle_departures = int(vehicle_model_state["vehicle_departures"])
    total_vehicle_delay = float(vehicle_model_state["total_vehicle_delay"])
    cumulative_vehicle_queue_length = float(vehicle_model_state["cumulative_vehicle_queue_length"])
    duration_hours = max(float(sim_duration) / 3600.0, 1e-9)

    metrics["vehicle_queue_length"] = int(vehicle_model_state["vehicle_queue_length"])
    metrics["total_vehicle_delay"] = total_vehicle_delay
    metrics["vehicle_arrivals"] = vehicle_arrivals
    metrics["vehicle_departures"] = vehicle_departures
    metrics["max_vehicle_queue_length"] = int(vehicle_model_state["max_vehicle_queue_length"])
    metrics["cumulative_vehicle_queue_length"] = cumulative_vehicle_queue_length
    metrics["pedestrian_green_extension_time"] = float(vehicle_model_state["pedestrian_green_extension_time"])
    metrics["pedestrian_green_extension_count"] = int(vehicle_model_state["pedestrian_green_extension_count"])
    metrics["smart_green_extension_count"] = int(vehicle_model_state["pedestrian_green_extension_count"])
    metrics["cycle_count"] = int(vehicle_model_state["cycle_count"])
    metrics["simulation_time"] = float(sim_duration)
    metrics["average_vehicle_delay"] = total_vehicle_delay / vehicle_arrivals if vehicle_arrivals else 0.0
    metrics["vehicle_delay"] = metrics["average_vehicle_delay"]
    metrics["average_vehicle_queue_length"] = (
        cumulative_vehicle_queue_length / measure_steps if measure_steps else 0.0
    )
    metrics["remaining_vehicle_queue"] = int(vehicle_model_state["vehicle_queue_length"])
    metrics["total_vehicle_arrivals"] = vehicle_arrivals
    metrics["total_vehicle_departures"] = vehicle_departures
    metrics["total_pedestrian_green_extension_time"] = float(
        vehicle_model_state["pedestrian_green_extension_time"]
    )
    metrics["disruption_event_count"] = int(vehicle_model_state["disruption_event_count"])
    metrics["total_disruption_duration"] = float(vehicle_model_state["total_disruption_duration"])
    metrics["disruption_time_ratio"] = float(vehicle_model_state["disruption_time_ratio"])
    metrics["average_capacity_multiplier"] = float(vehicle_model_state["average_capacity_multiplier"])
    metrics["average_effective_capacity"] = float(vehicle_model_state["average_effective_capacity"])
    metrics["lost_capacity_time"] = float(vehicle_model_state["lost_capacity_time"])
    metrics["capacity_loss_due_to_disruptions"] = float(
        vehicle_model_state["capacity_loss_due_to_disruptions"]
    )
    metrics["event_type_counts"] = str(vehicle_model_state["event_type_counts"])
    metrics["event_type_total_durations"] = str(vehicle_model_state["event_type_total_durations"])
    metrics["throughput"] = float(vehicle_departures / duration_hours)
    return metrics


def surrounding_metrics(
    scope: dict[str, Any],
    area_vehicle_waits: dict[str, float],
    area_seen_vehicles: set[str],
    area_entry_count: int,
    area_exit_count: int,
    area_travel_times: list[float],
    area_vehicle_time_sec: float,
    surrounding_queue_totals: list[int],
    direction_queue_totals: dict[str, list[int]],
    adjacent_tls_count: int,
    adjacent_tls_queue_totals: list[int],
    network_arrival_count: int,
    network_travel_times: list[float],
    surrounding_speed_samples: list[float],
    spillback_steps: int,
    measure_steps: int,
    sim_duration: int,
) -> dict[str, float | int]:
    duration_hours = max(float(sim_duration) / 3600.0, 1e-9)
    wait_arr = np.asarray(list(area_vehicle_waits.values()), dtype=float)
    travel_arr = np.asarray(area_travel_times, dtype=float)
    network_travel_arr = np.asarray(network_travel_times, dtype=float)
    speed_arr = np.asarray(surrounding_speed_samples, dtype=float)
    metrics: dict[str, float | int] = {
        "traffic_measure_radius_m": float(scope["radius_m"]),
        "surrounding_lane_count": int(len(scope["monitored_lanes"])),
        "surrounding_boundary_lane_count": int(len(scope["boundary_lanes"])),
        "surrounding_area_unique_vehicles": int(len(area_seen_vehicles)),
        "surrounding_area_entries": int(area_entry_count),
        "surrounding_area_exits": int(area_exit_count),
        "surrounding_throughput_veh_per_hour": float(area_exit_count / duration_hours),
        "surrounding_veh_avg_delay_sec": float(np.nanmean(wait_arr)) if wait_arr.size else 0.0,
        "surrounding_veh_max_delay_sec": float(np.nanmax(wait_arr)) if wait_arr.size else 0.0,
        "surrounding_queue_total_avg": mean_value(surrounding_queue_totals),
        "surrounding_queue_total_max": max_value(surrounding_queue_totals),
        "surrounding_total_travel_time_sec": float(np.nansum(travel_arr)) if travel_arr.size else 0.0,
        "surrounding_avg_travel_time_sec": float(np.nanmean(travel_arr)) if travel_arr.size else 0.0,
        "surrounding_vehicle_time_sec": float(area_vehicle_time_sec),
        "network_arrived_vehicles": int(network_arrival_count),
        "network_throughput_veh_per_hour": float(network_arrival_count / duration_hours),
        "network_avg_travel_time_sec": float(np.nanmean(network_travel_arr)) if network_travel_arr.size else 0.0,
        "surrounding_mean_speed_mps": float(np.nanmean(speed_arr)) if speed_arr.size else 0.0,
        "adjacent_tls_count": int(adjacent_tls_count),
        "adjacent_tls_queue_total_avg": mean_value(adjacent_tls_queue_totals),
        "adjacent_tls_queue_total_max": max_value(adjacent_tls_queue_totals),
        "spillback_observed": int(spillback_steps > 0),
        "spillback_step_count": int(spillback_steps),
        "spillback_rate": float(spillback_steps / measure_steps) if measure_steps else 0.0,
    }
    for direction in COMPASS_DIRECTIONS:
        values = direction_queue_totals.get(direction, [])
        metrics[f"approach_{direction}_queue_avg"] = mean_value(values)
        metrics[f"approach_{direction}_queue_max"] = max_value(values)
    return metrics


def derived_summary_metrics(
    metrics: dict[str, float | int],
    model_params: dict[str, dict[str, Any]],
    sensitivity_config: dict[str, Any] | None = None,
) -> dict[str, float]:
    safety_weight = float(get_parameter_value(model_params, "safety_cost_weight", 2.0))
    accident_expected_cost = float(get_parameter_value(model_params, "accident_expected_cost", 1_000_000.0))
    delay_cost_per_hour = float(
        get_parameter_value(
            model_params,
            "delay_cost_per_hour",
            get_parameter_value(model_params, "value_of_time_vehicle", 15000.0),
        )
    )
    pet_severe = float(metrics.get("PET_B_surrogate_severe", 0.0) or 0.0)
    elderly_incomplete = float(metrics.get("elderly_incomplete_cross", 0.0) or 0.0)
    total_vehicle_delay = float(metrics.get("total_vehicle_delay", 0.0) or 0.0)
    accident_risk_coefficient = float(
        (sensitivity_config or {}).get("accident_risk_coefficient_value", 1.0) or 1.0
    )
    vehicle_speed_risk_multiplier = float(
        (sensitivity_config or {}).get("vehicle_speed_risk_multiplier", 1.0) or 1.0
    )
    safety_risk_score = (
        (pet_severe + elderly_incomplete * safety_weight)
        * accident_risk_coefficient
        * vehicle_speed_risk_multiplier
    )
    return {
        "safety_risk_score": float(safety_risk_score),
        "accident_expected_value": float(safety_risk_score * accident_expected_cost),
        "vehicle_delay_cost": float((total_vehicle_delay / 3600.0) * delay_cost_per_hour),
        "elderly_incomplete_crossings": float(elderly_incomplete),
        "avg_vehicle_delay_sec": float(metrics.get("veh_avg_delay_sec", 0.0) or 0.0),
        "avg_queue_length": float(metrics.get("queue_avg", 0.0) or 0.0),
        "max_queue_length": float(metrics.get("queue_max", 0.0) or 0.0),
        "surrounding_road_delay_sec": float(
            metrics.get("surrounding_veh_avg_delay_sec", 0.0) or 0.0
        ),
        "extension_count": float(metrics.get("pedestrian_green_extension_count", 0.0) or 0.0),
        "total_extension_sec": float(
            metrics.get("total_pedestrian_green_extension_time", 0.0) or 0.0
        ),
        "accident_risk_coefficient": float(accident_risk_coefficient),
        "vehicle_speed_risk_multiplier": float(vehicle_speed_risk_multiplier),
    }


def phase_state(tl_id: str | None, phase_index: int, known_tls_ids: set[str] | None = None) -> str:
    if not is_known_trafficlight(tl_id, known_tls_ids):
        return ""
    try:
        logic = traci.trafficlight.getAllProgramLogics(tl_id)[0]
        return logic.phases[phase_index].state
    except Exception:
        return ""


def tune_phase_duration(
    tl_id: str | None,
    phase_index: int,
    ped_link_indices: list[int],
    signal_timing: dict[str, float],
    known_tls_ids: set[str] | None = None,
) -> None:
    if not is_known_trafficlight(tl_id, known_tls_ids):
        return
    state = phase_state(tl_id, phase_index, known_tls_ids=known_tls_ids)
    if not state:
        return
    if is_ped_green_state(state, ped_link_indices):
        duration = signal_timing["ped_green"]
    elif is_yellow_state(state):
        duration = SIGNAL_PARAMS["yellow_time"]
    elif is_all_red_state(state):
        duration = SIGNAL_PARAMS["all_red_time"]
    elif any(ch in {"g", "G"} for ch in state):
        duration = signal_timing["vehicle_green"]
    else:
        return
    try:
        traci.trafficlight.setPhaseDuration(tl_id, duration)
    except Exception:
        return


def run_simulation(
    net_file: str | Path,
    route_file: str | Path,
    ped_file: str | Path,
    sumocfg: str | Path,
    scenario: str,
    signal_timing: dict[str, float],
    cw_params: dict[str, float],
    metadata: dict[str, Any] | None = None,
    sim_duration: int = 1800,
    warmup: int = 300,
    seed: int = 42,
    traci_step_length: float = 0.1,
    traffic_measure_radius_m: float = 500.0,
    extension_increment: float | None = None,
    max_extensions: int | None = None,
    vehicle_arrival_rate_per_hour: float | None = VEHICLE_MODEL_PARAMS["arrival_rate_per_hour"],
    saturation_flow_rate_per_hour: float = float(VEHICLE_MODEL_PARAMS["saturation_flow_rate_per_hour"]),
    vehicle_num_lanes: int = int(VEHICLE_MODEL_PARAMS["num_lanes"]),
    vehicle_arrival_model: str = str(VEHICLE_MODEL_PARAMS["arrival_model"]),
    disruption_scenario: str = "best_case",
    enable_random_disruptions: bool = False,
    bus_stop_rate_per_hour: float = DEFAULT_RANDOM_DISRUPTION_RATES["bus_stop"],
    illegal_parking_rate_per_hour: float = DEFAULT_RANDOM_DISRUPTION_RATES["illegal_parking"],
    minor_incident_rate_per_hour: float = DEFAULT_RANDOM_DISRUPTION_RATES["minor_incident"],
    accident_rate_per_hour: float = DEFAULT_RANDOM_DISRUPTION_RATES["accident"],
    incident_schedule: list[dict[str, Any]] | None = None,
    crosswalk_id: str | int | None = None,
    model_parameters_path: str | Path | None = None,
    export_fcd: bool = False,
    output_dir: str | Path | None = None,
    vehicle_only: bool = False,
    sensitivity_config: dict[str, Any] | None = None,
    enable_risk_event_collection: bool = False,
    risk_event_sample_interval_s: float = 1.0,
    metric_sample_interval_s: float = 0.0,
    vehicle_sample_interval_s: float = 0.0,
    progress_interval_s: float = 0.0,
) -> tuple[dict[str, float | int], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    if traci is None:
        raise RuntimeError("traci가 설치되어 있지 않습니다.")

    random.seed(seed)
    rng = np.random.default_rng(seed)
    model_params = apply_parameter_value_overrides(
        load_model_parameters(model_parameters_path),
        (sensitivity_config or {}).get("parameter_overrides"),
    )

    signal_params = dict(SIGNAL_PARAMS)
    signal_params["extension_increment"] = float(
        get_parameter_value(model_params, "extension_increment_sec", SIGNAL_PARAMS["extension_increment"])
    )
    signal_params["max_extensions"] = int(
        get_parameter_value(model_params, "max_extensions", SIGNAL_PARAMS["max_extensions"])
    )
    signal_params["trigger_remaining"] = float(
        get_parameter_value(model_params, "trigger_remaining_sec", SIGNAL_PARAMS["trigger_remaining"])
    )
    signal_params["sensor_fn_rate"] = float(
        get_parameter_value(model_params, "sensor_fn_rate", SIGNAL_PARAMS["sensor_fn_rate"])
    )
    if extension_increment is not None:
        signal_params["extension_increment"] = float(extension_increment)
    if max_extensions is not None:
        signal_params["max_extensions"] = int(max_extensions)
    green_extension_policy = (sensitivity_config or {}).get("green_extension_policy_config", {})
    if isinstance(green_extension_policy, dict) and green_extension_policy:
        signal_params["extension_increment"] = float(
            green_extension_policy.get("extension_increment_sec", signal_params["extension_increment"])
        )
        signal_params["max_extensions"] = int(
            green_extension_policy.get("max_extensions", signal_params["max_extensions"])
        )
        signal_params["trigger_remaining"] = float(
            green_extension_policy.get("trigger_remaining_sec", signal_params["trigger_remaining"])
        )

    metadata = metadata or load_metadata(Path(net_file).parent / "metadata.json")
    normalized_home = normalized_sumo_home()
    if normalized_home:
        os.environ["SUMO_HOME"] = normalized_home

    # run_simulation은 runtime_trace_path 파라미터를 받지 않으므로 None으로 고정한다.
    # (run_simulation_integrated는 해당 파라미터를 별도로 수신함)
    runtime_trace_path: str | Path | None = None

    debug_state: dict[str, Any] = {
        "scenario": scenario,
        "seed": int(seed),
        "simulation_time": None,
        "rel_time": None,
        "current_crosswalk_id": "",
        "tls_id": "",
        "crossing_edge": "",
        "last_traci_call": "init",
        "last_object_id": "",
        "sumo_return_code": None,
    }

    def record_runtime_event(event: str, **fields: Any) -> None:
        payload = {**debug_state, **fields, "event": event}
        _runtime_log_append(runtime_trace_path, payload)

    update_runtime_state = debug_state.update

    crossing_edge = metadata["crossing_edge"]
    vehicle_conflict_edges = set(metadata["vehicle_conflict_edges"])
    approach_lanes = list(metadata["approach_lanes"])
    tl_id = metadata.get("tls_id")
    ped_link_indices = [int(idx) for idx in metadata.get("ped_link_indices", [])]
    ped_link_indices_source = str(metadata.get("ped_link_indices_source", "manifest"))

    # Runtime fallback: if manifest has empty ped_link_indices but tls_id and crossing_edge
    # are known, re-derive from net.xml so signalized candidates don't silently no-op.
    if not ped_link_indices and tl_id and crossing_edge:
        try:
            inferred = pedestrian_link_indices(net_file, tl_id, crossing_edge)
            if inferred:
                ped_link_indices = inferred
                ped_link_indices_source = "inferred_from_net_xml"
        except Exception as _pli_exc:
            import logging as _logging
            _logging.getLogger(__name__).warning(
                "[smart_extension] cw=%s: ped_link_indices inference failed: %s",
                crosswalk_id, _pli_exc,
            )

    signal_extension_mapping_status = "ok" if ped_link_indices else "empty_ped_link_indices"
    smart_extension_mapping_warning = (
        ""
        if ped_link_indices
        else (
            f"signalized+runnable candidate has ped_link_indices=[] after manifest and "
            f"net.xml lookup; extension will not trigger "
            f"(tls_id={tl_id}, crossing_edge={crossing_edge})"
        )
    )
    if smart_extension_mapping_warning:
        import logging as _logging
        _logging.getLogger(__name__).warning(
            "[smart_extension] cw=%s: %s", crosswalk_id, smart_extension_mapping_warning
        )

    scope = build_surrounding_scope(net_file, metadata, traffic_measure_radius_m)
    monitored_lanes = set(scope["monitored_lanes"])
    boundary_lanes = set(scope["boundary_lanes"])
    direction_lanes = {direction: set(lanes) for direction, lanes in scope["direction_lanes"].items()}
    lane_capacities = dict(scope["lane_capacities"])

    ped_detector_edges = {
        crossing_edge,
        str(metadata.get("ped_route", {}).get("from_edge", "")),
        str(metadata.get("ped_route", {}).get("to_edge", "")),
    }
    ped_detector_edges |= set(str(edge) for edge in metadata.get("detector_edges", []))
    ped_detector_edges.discard("")

    if incident_schedule:
        incident_events = parse_incident_schedule(incident_schedule)
    else:
        incident_events = generate_incident_schedule(
            disruption_scenario,
            sim_duration,
            seed,
            metadata,
            list(monitored_lanes),
            model_params,
            enable_random_disruptions,
            bus_stop_rate_per_hour,
            illegal_parking_rate_per_hour,
            minor_incident_rate_per_hour,
            accident_rate_per_hour,
        )

    traci_port = free_traci_port()
    traci_label = f"cw_{crosswalk_id}_{scenario}_{seed}_{random.randint(0, 1_000_000)}"
    traci.start(
        [
            sumo_binary(),
            "-c",
            str(sumocfg),
            "--seed",
            str(seed),
            "--step-length",
            str(traci_step_length),
            "--time-to-teleport",
            "300",
            "--no-warnings",
            "--no-step-log",
        ],
        port=traci_port,
        label=traci_label,
    )
    traci.switch(traci_label)

    vehicle_on_conflict_prev: set[str] = set()
    vehicle_exit_times: deque[float] = deque(maxlen=1000)
    ped_entered_crossing: set[str] = set()
    ped_first_seen: dict[str, float] = {}
    ped_wait_recorded: set[str] = set()
    all_seen_pedestrians: set[str] = set()
    elderly_seen_pedestrians: set[str] = set()
    ped_crossing_enter_time: dict[str, float] = {}
    pedestrian_crossing_times: list[float] = []
    pedestrian_wait_times: list[float] = []
    pet_a_records: list[float] = []
    pet_b_records: list[float] = []
    elderly_incomplete = 0
    veh_waits: dict[str, float] = {}
    queue_lengths: list[int] = []
    area_vehicle_waits: dict[str, float] = {}
    area_seen_vehicles: set[str] = set()
    area_active_since: dict[str, float] = {}
    area_entry_count = 0
    area_travel_times: list[float] = []
    area_exit_count = 0
    area_vehicle_time_sec = 0.0
    surrounding_queue_totals: list[int] = []
    direction_queue_totals: dict[str, list[int]] = {direction: [] for direction in COMPASS_DIRECTIONS}
    adjacent_tls_queue_totals: list[int] = []
    adjacent_lanes: set[str] = set()
    adjacent_tls_count = 0
    network_arrival_count = 0
    network_first_seen: dict[str, float] = {}
    network_travel_times: list[float] = []
    spillback_steps = 0
    measure_steps = 0
    lane_measure_steps = 0
    extension_count_in_cycle = 0
    extended_in_cycle = False
    pedestrian_green_extension_time = 0.0
    prev_phase: int | None = None
    prev_phase_was_ped_green = False
    prev_vehicle_green = False
    end_time = warmup + sim_duration

    queue_vehicle_arrival_rate_per_hour = float(vehicle_arrival_rate_per_hour) if vehicle_arrival_rate_per_hour else 0.0
    queue_saturation_flow_rate_per_hour = max(
        0.0,
        float(saturation_flow_rate_per_hour)
        * float((sensitivity_config or {}).get("saturation_flow_multiplier", 1.0) or 1.0),
    )
    queue_num_lanes = max(1, int(vehicle_num_lanes))
    queue_vehicle_length = 0
    queue_total_vehicle_delay = 0.0
    queue_vehicle_arrivals = 0
    queue_vehicle_departures = 0
    queue_max_vehicle_length = 0
    queue_cumulative_length = 0.0
    queue_departure_credit = 0.0
    cycle_count = 0
    total_extension_count = 0
    next_lane_sample_t = 0.0
    step_cache = StepMetricCache({}, {})

    lane_original_states: dict[str, LaneState] = {}
    active_event_ids: set[str] = set()
    disruption_active_time = 0.0
    cumulative_capacity_multiplier = 0.0
    cumulative_effective_capacity = 0.0
    lost_capacity_time = 0.0
    capacity_loss_due_to_disruptions = 0.0
    event_type_counts = Counter(event.event_type for event in incident_events)
    event_type_total_durations = Counter()
    for event in incident_events:
        event_type_total_durations[event.event_type] += float(max(0.0, event.end_time - event.start_time))

    extension_events_log: list[dict[str, Any]] = []
    incident_impacts: list[dict[str, Any]] = []
    incident_events_log = [
        serialize_incident_event(event, str(crosswalk_id), seed, "shared") for event in incident_events
    ]

    ts_queue: deque[tuple[float, float]] = deque(maxlen=10000)
    ts_wait: deque[tuple[float, float]] = deque(maxlen=10000)
    ts_speed: deque[tuple[float, float]] = deque(maxlen=10000)
    surrounding_speed_samples: list[float] = []
    known_tls_ids: set[str] = set()
    tls_refresh_countdown = 0
    next_vehicle_sample_t = 0.0
    next_progress_log_t = 0.0
    progress_output_path = (Path(output_dir) / "runtime_progress.csv") if output_dir else None
    runtime_wall_t0 = time.perf_counter()

    fcd_vehicle_records: list[tuple[float, str, float, float, float, str]] = []
    fcd_person_records: list[tuple[float, str, float, float, float, str]] = []
    lane_data_records: list[tuple[float, str, int, float]] = []
    edge_data_records: list[tuple[float, str, int, float]] = []

    try:
        adjacent_lanes, adjacent_tls_count = adjacent_tls_lanes(tl_id, monitored_lanes)
        while traci.simulation.getTime() < end_time:
            traci.simulationStep()
            t = float(traci.simulation.getTime())
            rel_t = max(0.0, t - warmup)
            collect = t > warmup
            step_cache.reset()
            lane_sample_due = _lane_sample_due(metric_sample_interval_s, rel_t, next_lane_sample_t)
            if lane_sample_due and metric_sample_interval_s > 0:
                next_lane_sample_t += metric_sample_interval_s
            vehicle_sample_due = _lane_sample_due(vehicle_sample_interval_s, rel_t, next_vehicle_sample_t)
            if vehicle_sample_due and vehicle_sample_interval_s > 0:
                next_vehicle_sample_t += vehicle_sample_interval_s
            if tls_refresh_countdown <= 0:
                known_tls_ids = traci_trafficlight_ids()
                tls_refresh_countdown = 10
            else:
                tls_refresh_countdown -= 1

            # Incident state transitions
            currently_active = active_incidents(incident_events, rel_t)
            current_ids = {event.incident_id for event in currently_active}
            for event in currently_active:
                if event.incident_id not in active_event_ids:
                    apply_incident(event, lane_original_states)
            for event in incident_events:
                if event.incident_id in active_event_ids and event.incident_id not in current_ids:
                    restore_incident(event, lane_original_states)
                    before_vals = [v for tt, v in ts_queue if event.start_time - 60 <= tt < event.start_time]
                    after_vals = [v for tt, v in ts_queue if event.end_time <= tt <= event.end_time + 60]
                    before_wait = [v for tt, v in ts_wait if event.start_time - 60 <= tt < event.start_time]
                    after_wait = [v for tt, v in ts_wait if event.end_time <= tt <= event.end_time + 60]
                    before_spd = [v for tt, v in ts_speed if event.start_time - 60 <= tt < event.start_time]
                    after_spd = [v for tt, v in ts_speed if event.end_time <= tt <= event.end_time + 60]
                    incident_impacts.append(
                        {
                            "incident_id": event.incident_id,
                            "crosswalk_id": str(crosswalk_id),
                            "seed": seed,
                            "scenario": scenario,
                            "event_type": event.event_type,
                            "before_queue_avg": float(np.nanmean(before_vals)) if before_vals else 0.0,
                            "after_queue_avg": float(np.nanmean(after_vals)) if after_vals else 0.0,
                            "before_wait_avg_sec": float(np.nanmean(before_wait)) if before_wait else 0.0,
                            "after_wait_avg_sec": float(np.nanmean(after_wait)) if after_wait else 0.0,
                            "before_speed_avg_mps": float(np.nanmean(before_spd)) if before_spd else 0.0,
                            "after_speed_avg_mps": float(np.nanmean(after_spd)) if after_spd else 0.0,
                        }
                    )
            active_event_ids = current_ids

            active_capacity_multiplier = (
                min(event.capacity_multiplier for event in currently_active) if currently_active else 1.0
            )
            tl_id_known = tl_id if is_known_trafficlight(tl_id, known_tls_ids) else None
            try:
                current_phase = traci.trafficlight.getPhase(tl_id_known) if tl_id_known else -1
            except Exception:
                tl_id_known = None
                current_phase = -1
                tls_refresh_countdown = 0
            state = phase_state(tl_id_known, current_phase, known_tls_ids=known_tls_ids) if tl_id_known else ""
            current_phase_is_ped_green = is_ped_green_state(state, ped_link_indices)
            current_phase_is_vehicle_green = is_vehicle_green_state(state, ped_link_indices)
            phase_changed = current_phase != prev_phase

            if collect and phase_changed and prev_phase_was_ped_green and not current_phase_is_ped_green:
                for ped_id in traci.edge.getLastStepPersonIDs(crossing_edge):
                    try:
                        pos = float(traci.person.getLanePosition(ped_id))
                        speed = float(traci.person.getSpeed(ped_id))
                        if speed <= 0:
                            speed = 0.5
                        remaining_dist = max(0.0, float(cw_params["crossing_length_m"]) - pos)
                        time_to_clear = remaining_dist / speed
                        pet_b_records.append(signal_params["all_red_time"] - time_to_clear)
                        if "elderly" in traci.person.getTypeID(ped_id):
                            elderly_incomplete += 1
                    except Exception:
                        continue

            if phase_changed:
                tune_phase_duration(
                    tl_id_known,
                    current_phase,
                    ped_link_indices,
                    signal_timing,
                    known_tls_ids=known_tls_ids,
                )
                if current_phase_is_vehicle_green and not prev_vehicle_green:
                    cycle_count += 1
                    extension_count_in_cycle = 0
                    extended_in_cycle = False
                prev_phase = current_phase

            if scenario == "smart" and tl_id_known and current_phase_is_ped_green:
                try:
                    next_switch = float(traci.trafficlight.getNextSwitch(tl_id_known))
                except Exception:
                    next_switch = t
                    tls_refresh_countdown = 0
                remaining = next_switch - t
                # Fetch peds only when pre-conditions pass (preserves original TraCI call timing).
                ped_on_crossing: set = set()
                detected_peds: set = set()
                sensor_pass = True
                if (
                    remaining <= signal_params["trigger_remaining"]
                    and extension_count_in_cycle < signal_params["max_extensions"]
                    and not extended_in_cycle
                ):
                    ped_on_crossing = set(traci.edge.getLastStepPersonIDs(crossing_edge))
                    ped_on_detectors: set = set()
                    for edge_id in ped_detector_edges:
                        try:
                            ped_on_detectors.update(traci.edge.getLastStepPersonIDs(edge_id))
                        except Exception:
                            continue
                    detected_peds = ped_on_crossing | ped_on_detectors
                    if detected_peds:
                        # random.random() called only when peds detected — same timing as before.
                        sensor_pass = random.random() > signal_params["sensor_fn_rate"]
                decision = evaluate_smart_extension_decision(
                    tls_id=tl_id_known,
                    ped_link_indices=ped_link_indices,
                    tls_state=state,
                    remaining_s=remaining,
                    pedestrians_detected=bool(detected_peds),
                    extension_count_in_cycle=extension_count_in_cycle,
                    extended_in_cycle=extended_in_cycle,
                    signal_params=signal_params,
                    sensor_pass=sensor_pass,
                )
                if decision["should_extend"]:
                    try:
                        traci.trafficlight.setPhaseDuration(
                            tl_id_known,
                            remaining + decision["extension_sec"],
                        )
                    except Exception:
                        tls_refresh_countdown = 0
                        continue
                    extension_count_in_cycle += 1
                    extended_in_cycle = True
                    total_extension_count += 1
                    pedestrian_green_extension_time += decision["extension_sec"]
                    ped_indices_set = set(ped_link_indices)
                    extension_events_log.append(
                        {
                            "sim_time": t,
                            "crosswalk_id": str(crosswalk_id),
                            "tls_id": str(tl_id_known),
                            "phase_index": int(current_phase),
                            "tls_state": str(state),
                            "ped_link_indices": json.dumps(list(ped_link_indices), ensure_ascii=False),
                            "vehicle_green_link_count": int(
                                sum(
                                    1
                                    for idx, ch in enumerate(state)
                                    if idx not in ped_indices_set and ch in {"g", "G"}
                                )
                            ),
                            "is_ped_only_phase": int(
                                is_ped_green_state(state, ped_link_indices)
                                and not any(
                                    ch in {"g", "G"}
                                    for idx, ch in enumerate(state)
                                    if idx not in ped_indices_set
                                )
                            ),
                            "remaining_before_extension": float(remaining),
                            "extension_sec": float(decision["extension_sec"]),
                            "ped_count_on_crossing": int(len(ped_on_crossing)),
                            "seed": int(seed),
                            "scenario": scenario,
                        }
                    )

            if collect:
                measure_steps += 1
                if lane_sample_due:
                    lane_measure_steps += 1
                current_person_ids = set(traci.person.getIDList())
                for ped_id in current_person_ids:
                    ped_first_seen.setdefault(ped_id, t)
                    all_seen_pedestrians.add(ped_id)
                    try:
                        if traci.person.getTypeID(ped_id) == "elderly":
                            elderly_seen_pedestrians.add(ped_id)
                    except Exception:
                        continue
                current_vehicle_ids = set(traci.vehicle.getIDList())
                arrived_ids = set(traci.simulation.getArrivedIDList())
                network_arrival_count += len(arrived_ids)
                for veh_id in arrived_ids:
                    first_seen = network_first_seen.pop(veh_id, None)
                    if first_seen is not None:
                        network_travel_times.append(max(0.0, t - first_seen))
                for veh_id in current_vehicle_ids:
                    network_first_seen.setdefault(veh_id, t)

                if vehicle_sample_due:
                    current_vehicle_conflict = set()
                    current_area_vehicles: set[str] = set()
                    all_vehicle_speeds: list[float] = []
                    for veh_id in current_vehicle_ids:
                        vehicle_state = _vehicle_state_snapshot(veh_id, step_cache.vehicle_state)
                        if vehicle_state is None:
                            continue
                        road_id = str(vehicle_state["road_id"])
                        if road_id in vehicle_conflict_edges:
                            current_vehicle_conflict.add(veh_id)
                        accumulated_wait = float(vehicle_state["accumulated_wait"])
                        veh_waits[veh_id] = accumulated_wait
                        lane_id = str(vehicle_state["lane_id"])
                        all_vehicle_speeds.append(float(vehicle_state["speed"]))
                        if lane_id in monitored_lanes:
                            current_area_vehicles.add(veh_id)
                            area_seen_vehicles.add(veh_id)
                            area_vehicle_waits[veh_id] = accumulated_wait
                            if veh_id not in area_active_since:
                                area_active_since[veh_id] = t
                                area_entry_count += 1

                    for veh_id in list(area_active_since):
                        if veh_id not in current_area_vehicles:
                            area_travel_times.append(max(0.0, t - area_active_since.pop(veh_id)))
                            area_exit_count += 1
                    area_vehicle_time_sec += len(current_area_vehicles) * traci_step_length

                    for veh_id in vehicle_on_conflict_prev - current_vehicle_conflict:
                        vehicle_exit_times.append(t)
                    vehicle_on_conflict_prev = current_vehicle_conflict

                    avg_wait = float(np.nanmean(list(veh_waits.values()))) if veh_waits else 0.0
                    avg_speed = float(np.nanmean(all_vehicle_speeds)) if all_vehicle_speeds else 0.0
                    ts_wait.append((rel_t, avg_wait))
                    ts_speed.append((rel_t, avg_speed))
                    surrounding_speed_samples.append(avg_speed)

                peds_on_crossing = set(traci.edge.getLastStepPersonIDs(crossing_edge))
                for ped_id in peds_on_crossing:
                    if ped_id not in ped_wait_recorded:
                        pedestrian_wait_times.append(max(0.0, t - ped_first_seen.get(ped_id, t)))
                        ped_wait_recorded.add(ped_id)
                    ped_crossing_enter_time.setdefault(ped_id, t)
                for ped_id in list(ped_crossing_enter_time):
                    if ped_id not in peds_on_crossing:
                        pedestrian_crossing_times.append(
                            max(0.0, t - ped_crossing_enter_time.pop(ped_id))
                        )
                for ped_id in peds_on_crossing - ped_entered_crossing:
                    candidates = [vt for vt in recent_values(vehicle_exit_times, t, 30.0) if vt <= t]
                    if candidates:
                        pet_a_records.append(t - max(candidates))
                ped_entered_crossing |= peds_on_crossing

                arrival_lambda = queue_vehicle_arrival_rate_per_hour / 3600.0 * traci_step_length
                if vehicle_arrival_model == "bernoulli":
                    arrivals_this_step = int(rng.random() < min(arrival_lambda, 1.0))
                else:
                    arrivals_this_step = int(rng.poisson(max(arrival_lambda, 0.0)))
                queue_vehicle_length += arrivals_this_step
                queue_vehicle_arrivals += arrivals_this_step

                base_departure_capacity_per_step = (
                    queue_saturation_flow_rate_per_hour / 3600.0 * traci_step_length * queue_num_lanes
                )
                effective_capacity_per_step = base_departure_capacity_per_step * active_capacity_multiplier
                cumulative_capacity_multiplier += active_capacity_multiplier
                cumulative_effective_capacity += effective_capacity_per_step
                capacity_loss_due_to_disruptions += (
                    base_departure_capacity_per_step - effective_capacity_per_step
                )
                lost_capacity_time += (1.0 - active_capacity_multiplier) * traci_step_length
                if currently_active:
                    disruption_active_time += traci_step_length

                if current_phase_is_vehicle_green:
                    queue_departure_credit += effective_capacity_per_step
                    departures_this_step = min(queue_vehicle_length, int(math.floor(queue_departure_credit + 1e-9)))
                    queue_vehicle_length -= departures_this_step
                    queue_vehicle_departures += departures_this_step
                    queue_departure_credit -= departures_this_step

                queue_vehicle_length = max(0, queue_vehicle_length)
                queue_total_vehicle_delay += queue_vehicle_length * traci_step_length
                queue_cumulative_length += queue_vehicle_length
                queue_max_vehicle_length = max(queue_max_vehicle_length, queue_vehicle_length)

                lane_halting: dict[str, int] = {}
                lane_occupancy: dict[str, float] = {}
                lane_speeds: dict[str, float] = {}
                surrounding_queue_total = 0
                if lane_sample_due:
                    for lane_id in monitored_lanes:
                        lane_state = _lane_metric_snapshot(lane_id, step_cache.lane_metrics)
                        if lane_state is None:
                            continue
                        halted = int(lane_state["halting_number"])
                        lane_halting[lane_id] = halted
                        lane_occupancy[lane_id] = float(lane_state["occupancy"])
                        lane_speeds[lane_id] = float(lane_state["mean_speed"])
                        surrounding_queue_total += halted
                        lane_data_records.append((rel_t, lane_id, halted, lane_speeds[lane_id]))

                    # Edge aggregation from lane data
                    edge_queue: dict[str, int] = {}
                    edge_speed_sum: dict[str, float] = {}
                    edge_speed_cnt: dict[str, int] = {}
                    for lane_id, halted in lane_halting.items():
                        edge_id = scope["lane_to_edge"].get(lane_id, lane_id.rsplit("_", 1)[0])
                        edge_queue[edge_id] = edge_queue.get(edge_id, 0) + halted
                        sp = lane_speeds.get(lane_id, 0.0)
                        edge_speed_sum[edge_id] = edge_speed_sum.get(edge_id, 0.0) + sp
                        edge_speed_cnt[edge_id] = edge_speed_cnt.get(edge_id, 0) + 1
                    for edge_id, q in edge_queue.items():
                        mean_sp = edge_speed_sum.get(edge_id, 0.0) / max(1, edge_speed_cnt.get(edge_id, 1))
                        edge_data_records.append((rel_t, edge_id, q, mean_sp))

                    for direction, lanes in direction_lanes.items():
                        direction_queue_totals[direction].append(sum(lane_halting.get(lane_id, 0) for lane_id in lanes))
                    adjacent_tls_queue_totals.append(sum(lane_halting.get(lane_id, 0) for lane_id in adjacent_lanes))
                    spillback_now = False
                    for lane_id in boundary_lanes:
                        halted = lane_halting.get(lane_id, 0)
                        occupancy = lane_occupancy.get(lane_id, 0.0)
                        capacity = max(1, int(lane_capacities.get(lane_id, 1)))
                        if halted >= max(1, int(capacity * 0.8)) or occupancy >= 85.0:
                            spillback_now = True
                            break
                    if spillback_now:
                        spillback_steps += 1

                if lane_sample_due:
                    surrounding_queue_totals.append(surrounding_queue_total)
                    ts_queue.append((rel_t, float(surrounding_queue_total)))

                for lane_id in approach_lanes:
                    try:
                        lane_state = _lane_metric_snapshot(lane_id, step_cache.lane_metrics)
                        if lane_state is None:
                            continue
                        queue_lengths.append(int(lane_state["halting_number"]))
                    except Exception:
                        continue

                if export_fcd:
                    if vehicle_sample_due:
                        for veh_id in current_vehicle_ids:
                            vehicle_state = _vehicle_state_snapshot(veh_id, step_cache.vehicle_state)
                            if vehicle_state is None:
                                continue
                            try:
                                x, y = traci.vehicle.getPosition(veh_id)
                                fcd_vehicle_records.append(
                                    (
                                        rel_t,
                                        veh_id,
                                        float(x),
                                        float(y),
                                        float(vehicle_state["speed"]),
                                        str(vehicle_state["road_id"]),
                                    )
                                )
                            except Exception:
                                continue
                    for person_id in current_person_ids:
                        try:
                            x, y = traci.person.getPosition(person_id)
                            fcd_person_records.append(
                                (
                                    rel_t,
                                    person_id,
                                    float(x),
                                    float(y),
                                    float(traci.person.getSpeed(person_id)),
                                    traci.person.getRoadID(person_id),
                                )
                            )
                        except Exception:
                            continue

                if progress_interval_s > 0 and t + 1e-9 >= next_progress_log_t:
                    while progress_interval_s > 0 and t + 1e-9 >= next_progress_log_t:
                        try:
                            vehicle_count = int(len(current_vehicle_ids))
                        except Exception:
                            vehicle_count = -1
                        try:
                            person_count = int(len(current_person_ids))
                        except Exception:
                            person_count = -1
                        try:
                            min_expected_number = int(traci.simulation.getMinExpectedNumber())
                        except Exception:
                            min_expected_number = -1
                        progress_row = {
                            "timestamp": time.time(),
                            "scenario": scenario,
                            "seed": int(seed),
                            "simulation_time": t,
                            "wall_time_sec": float(time.perf_counter() - runtime_wall_t0),
                            "vehicle_count": vehicle_count,
                            "person_count": person_count,
                            "min_expected_number": min_expected_number,
                        }
                        if progress_output_path is not None:
                            _runtime_progress_append(progress_output_path, progress_row)
                        else:
                            print(
                                "[progress]",
                                f"scenario={scenario}",
                                f"seed={seed}",
                                f"sim_time={t:.1f}",
                                f"wall={progress_row['wall_time_sec']:.1f}s",
                                f"vehicles={vehicle_count}",
                            )
                        next_progress_log_t += progress_interval_s

            prev_phase_was_ped_green = current_phase_is_ped_green
            prev_vehicle_green = current_phase_is_vehicle_green
    finally:
        for event in incident_events:
            if event.incident_id in active_event_ids:
                restore_incident(event, lane_original_states)
        try:
            traci.switch(traci_label)
            traci.close(False)
            debug_state.update(sumo_return_code=_active_sumo_return_code(traci_label))
            record_runtime_event("traci_closed", sumo_return_code=_active_sumo_return_code(traci_label))
        except Exception:
            pass

    for ped_id, enter_time in ped_crossing_enter_time.items():
        pedestrian_crossing_times.append(max(0.0, float(end_time) - float(enter_time)))

    metrics = aggregate_metrics(
        pet_a_records,
        pet_b_records,
        elderly_incomplete,
        len(all_seen_pedestrians),
        len(elderly_seen_pedestrians),
        pedestrian_crossing_times,
        veh_waits,
        queue_lengths,
        pedestrian_wait_times,
        {
            "vehicle_queue_length": queue_vehicle_length,
            "total_vehicle_delay": queue_total_vehicle_delay,
            "vehicle_arrivals": queue_vehicle_arrivals,
            "vehicle_departures": queue_vehicle_departures,
            "max_vehicle_queue_length": queue_max_vehicle_length,
            "cumulative_vehicle_queue_length": queue_cumulative_length,
            "pedestrian_green_extension_time": pedestrian_green_extension_time,
            "pedestrian_green_extension_count": total_extension_count,
            "cycle_count": cycle_count,
            "disruption_event_count": len(incident_events),
            "total_disruption_duration": float(
                sum(max(0.0, e.end_time - e.start_time) for e in incident_events)
            ),
            "disruption_time_ratio": (disruption_active_time / sim_duration if sim_duration else 0.0),
            "average_capacity_multiplier": (
                cumulative_capacity_multiplier / measure_steps if measure_steps else 1.0
            ),
            "average_effective_capacity": (
                cumulative_effective_capacity / measure_steps if measure_steps else 0.0
            ),
            "lost_capacity_time": lost_capacity_time,
            "capacity_loss_due_to_disruptions": capacity_loss_due_to_disruptions,
            "event_type_counts": json.dumps(dict(sorted(event_type_counts.items())), ensure_ascii=False),
            "event_type_total_durations": json.dumps(
                {key: float(value) for key, value in sorted(event_type_total_durations.items())},
                ensure_ascii=False,
            ),
        },
        measure_steps,
        sim_duration,
    )
    metrics["signal_extension_increment_sec"] = float(signal_params["extension_increment"])
    metrics["signal_max_extensions"] = int(signal_params["max_extensions"])
    metrics["vehicle_arrival_rate_per_hour"] = float(queue_vehicle_arrival_rate_per_hour)
    metrics["saturation_flow_rate_per_hour"] = float(queue_saturation_flow_rate_per_hour)
    metrics["vehicle_num_lanes"] = int(queue_num_lanes)
    metrics["vehicle_only_mode"] = int(1 if vehicle_only else 0)
    metrics["vehicle_arrival_model"] = vehicle_arrival_model
    metrics["vehicle_speed_factor"] = float(
        (sensitivity_config or {}).get("vehicle_speed_display_factor", 1.0) or 1.0
    )
    metrics["green_extension_policy"] = str(
        (sensitivity_config or {}).get("green_extension_policy_name", "base_extension")
    )
    metrics["disruption_scenario"] = disruption_scenario
    metrics["enable_random_disruptions"] = int(enable_random_disruptions)
    metrics.update(
        surrounding_metrics(
            scope,
            area_vehicle_waits,
            area_seen_vehicles,
            area_entry_count,
            area_exit_count,
            area_travel_times,
            area_vehicle_time_sec,
            surrounding_queue_totals,
            direction_queue_totals,
            adjacent_tls_count,
            adjacent_tls_queue_totals,
            network_arrival_count,
            network_travel_times,
            surrounding_speed_samples,
            spillback_steps,
            lane_measure_steps,
            sim_duration,
        )
    )
    metrics.update(derived_summary_metrics(metrics, model_params, sensitivity_config))

    file_exports: dict[str, Any] = {}
    if output_dir:
        out_dir = Path(output_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        # These paths are intentionally stable for downstream report scripts.
        edge_data_path = out_dir / f"edge_data_{scenario}_seed{seed}.xml"
        lane_data_path = out_dir / f"lane_data_{scenario}_seed{seed}.xml"
        file_exports["edge_data_path"] = str(edge_data_path)
        file_exports["lane_data_path"] = str(lane_data_path)

        edge_root = ET.Element("edgeData")
        for t, edge_id, q, sp in edge_data_records:
            ET.SubElement(
                edge_root,
                "edge",
                {
                    "time": f"{t:.2f}",
                    "id": edge_id,
                    "queue": str(q),
                    "meanSpeed": f"{sp:.4f}",
                },
            )
        ET.indent(edge_root, space="  ")
        ET.ElementTree(edge_root).write(edge_data_path, encoding="utf-8", xml_declaration=True)

        lane_root = ET.Element("laneData")
        for t, lane_id, q, sp in lane_data_records:
            ET.SubElement(
                lane_root,
                "lane",
                {
                    "time": f"{t:.2f}",
                    "id": lane_id,
                    "queue": str(q),
                    "meanSpeed": f"{sp:.4f}",
                },
            )
        ET.indent(lane_root, space="  ")
        ET.ElementTree(lane_root).write(lane_data_path, encoding="utf-8", xml_declaration=True)

        if export_fcd:
            fcd_vehicle_path = out_dir / f"fcd_vehicle_{scenario}_seed{seed}.xml"
            fcd_person_path = out_dir / f"fcd_person_{scenario}_seed{seed}.xml"
            file_exports["fcd_vehicle_path"] = str(fcd_vehicle_path)
            file_exports["fcd_person_path"] = str(fcd_person_path)

            veh_root = ET.Element("fcd-export")
            for t, veh_id, x, y, speed, road in fcd_vehicle_records:
                timestep = ET.SubElement(veh_root, "timestep", {"time": f"{t:.2f}"})
                ET.SubElement(
                    timestep,
                    "vehicle",
                    {
                        "id": veh_id,
                        "x": f"{x:.3f}",
                        "y": f"{y:.3f}",
                        "speed": f"{speed:.3f}",
                        "edge": road,
                    },
                )
            ET.indent(veh_root, space="  ")
            ET.ElementTree(veh_root).write(fcd_vehicle_path, encoding="utf-8", xml_declaration=True)

            person_root = ET.Element("fcd-export")
            for t, person_id, x, y, speed, road in fcd_person_records:
                timestep = ET.SubElement(person_root, "timestep", {"time": f"{t:.2f}"})
                ET.SubElement(
                    timestep,
                    "person",
                    {
                        "id": person_id,
                        "x": f"{x:.3f}",
                        "y": f"{y:.3f}",
                        "speed": f"{speed:.3f}",
                        "edge": road,
                    },
                )
            ET.indent(person_root, space="  ")
            ET.ElementTree(person_root).write(fcd_person_path, encoding="utf-8", xml_declaration=True)

    # per-candidate 모드: 이번 Phase에서는 루프 연동 없이 flag 필드만 기록
    metrics["risk_event_collection_enabled"] = bool(enable_risk_event_collection)
    metrics["risk_event_count"] = 0
    metrics["senior_risk_event_count"] = 0
    metrics["risk_events_path"] = ""
    metrics["pedestrian_frame_count"] = 0
    metrics["vehicle_frame_count"] = 0
    metrics["ped_link_indices_count"] = len(ped_link_indices)
    metrics["ped_link_indices_source"] = ped_link_indices_source
    metrics["signal_extension_mapping_status"] = signal_extension_mapping_status
    metrics["smart_extension_mapping_warning"] = smart_extension_mapping_warning

    return metrics, extension_events_log, incident_events_log, incident_impacts, file_exports


def run_simulation_integrated(
    net_file: str | Path,
    route_file: str | Path,
    ped_file: str | Path,
    sumocfg: str | Path,
    scenario: str,
    crosswalk_contexts: list[dict[str, Any]],
    smart_target_ids: set[str],
    sim_duration: int = 1800,
    warmup: int = 300,
    seed: int = 42,
    traci_step_length: float = 0.1,
    traffic_measure_radius_m: float = 500.0,
    extension_increment: float | None = None,
    max_extensions: int | None = None,
    vehicle_arrival_rate_per_hour: float | None = VEHICLE_MODEL_PARAMS["arrival_rate_per_hour"],
    saturation_flow_rate_per_hour: float = float(VEHICLE_MODEL_PARAMS["saturation_flow_rate_per_hour"]),
    vehicle_num_lanes: int = int(VEHICLE_MODEL_PARAMS["num_lanes"]),
    vehicle_arrival_model: str = str(VEHICLE_MODEL_PARAMS["arrival_model"]),
    disruption_scenario: str = "best_case",
    enable_random_disruptions: bool = False,
    bus_stop_rate_per_hour: float = DEFAULT_RANDOM_DISRUPTION_RATES["bus_stop"],
    illegal_parking_rate_per_hour: float = DEFAULT_RANDOM_DISRUPTION_RATES["illegal_parking"],
    minor_incident_rate_per_hour: float = DEFAULT_RANDOM_DISRUPTION_RATES["minor_incident"],
    accident_rate_per_hour: float = DEFAULT_RANDOM_DISRUPTION_RATES["accident"],
    incident_schedule: list[dict[str, Any]] | None = None,
    model_parameters_path: str | Path | None = None,
    export_fcd: bool = False,
    output_dir: str | Path | None = None,
    runtime_trace_path: str | Path | None = None,
    failure_context_path: str | Path | None = None,
    vehicle_only: bool = False,
    sensitivity_config: dict[str, Any] | None = None,
    enable_risk_event_collection: bool = False,
    risk_event_sample_interval_s: float = 1.0,
    metric_sample_interval_s: float = 0.0,
    vehicle_sample_interval_s: float = 0.0,
    progress_interval_s: float = 0.0,
) -> tuple[
    list[dict[str, Any]],
    dict[str, Any],
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
    dict[str, Any],
]:
    if traci is None:
        raise RuntimeError("traci가 설치되어 있지 않습니다.")
    if not crosswalk_contexts:
        raise ValueError("crosswalk_contexts가 비어 있습니다.")

    random.seed(seed)
    rng = np.random.default_rng(seed)
    model_params = apply_parameter_value_overrides(
        load_model_parameters(model_parameters_path),
        (sensitivity_config or {}).get("parameter_overrides"),
    )
    signal_params = dict(SIGNAL_PARAMS)
    signal_params["extension_increment"] = float(
        get_parameter_value(model_params, "extension_increment_sec", SIGNAL_PARAMS["extension_increment"])
    )
    signal_params["max_extensions"] = int(
        get_parameter_value(model_params, "max_extensions", SIGNAL_PARAMS["max_extensions"])
    )
    signal_params["trigger_remaining"] = float(
        get_parameter_value(model_params, "trigger_remaining_sec", SIGNAL_PARAMS["trigger_remaining"])
    )
    signal_params["sensor_fn_rate"] = float(
        get_parameter_value(model_params, "sensor_fn_rate", SIGNAL_PARAMS["sensor_fn_rate"])
    )
    if extension_increment is not None:
        signal_params["extension_increment"] = float(extension_increment)
    if max_extensions is not None:
        signal_params["max_extensions"] = int(max_extensions)
    green_extension_policy = (sensitivity_config or {}).get("green_extension_policy_config", {})
    if isinstance(green_extension_policy, dict) and green_extension_policy:
        signal_params["extension_increment"] = float(
            green_extension_policy.get("extension_increment_sec", signal_params["extension_increment"])
        )
        signal_params["max_extensions"] = int(
            green_extension_policy.get("max_extensions", signal_params["max_extensions"])
        )
        signal_params["trigger_remaining"] = float(
            green_extension_policy.get("trigger_remaining_sec", signal_params["trigger_remaining"])
        )

    normalized_home = normalized_sumo_home()
    if normalized_home:
        os.environ["SUMO_HOME"] = normalized_home

    debug_state: dict[str, Any] = {
        "scenario": scenario,
        "seed": int(seed),
        "simulation_time": None,
        "current_crosswalk_id": "",
        "tls_id": "",
        "crossing_edge": "",
        "last_traci_call": "init",
        "last_object_id": "",
        "sumo_return_code": None,
    }

    def record_runtime_event(event: str, **fields: Any) -> None:
        payload = {**debug_state, **fields, "event": event}
        _runtime_log_append(runtime_trace_path, payload)

    update_runtime_state = debug_state.update

    net = read_net(net_file)
    context_states: list[dict[str, Any]] = []
    group_defs: dict[str, dict[str, Any]] = {}
    union_monitored_lanes: set[str] = set()
    union_conflict_edges: set[str] = set()

    for context in crosswalk_contexts:
        crosswalk_id = str(context["crosswalk_id"])
        signal_timing = compute_signal_timing(context)
        scope = build_surrounding_scope(net_file, context, traffic_measure_radius_m)
        monitored_lanes = set(scope["monitored_lanes"])
        union_monitored_lanes.update(monitored_lanes)
        union_conflict_edges.update(str(edge_id) for edge_id in context.get("vehicle_conflict_edges", []))
        ped_detector_edges = {
            str(context["crossing_edge"]),
            str(context.get("ped_route", {}).get("from_edge", "")),
            str(context.get("ped_route", {}).get("to_edge", "")),
        }
        ped_detector_edges |= set(str(edge) for edge in context.get("detector_edges", []))
        ped_detector_edges.discard("")
        tls_id = str(context.get("tls_id") or "")
        group = group_defs.setdefault(
            tls_id,
            {
                "tl_id": tls_id,
                "contexts": [],
                "ped_link_indices_union": set(),
                "signal_timing": signal_timing,
                "prev_phase": None,
                "prev_phase_was_ped_green": False,
                "prev_vehicle_green": False,
                "cycle_count": 0,
                "extension_count_in_cycle": 0,
                "extended_in_cycle": False,
                "total_extension_count": 0,
                "pedestrian_green_extension_time": 0.0,
                "target_context_ids": set(),
            },
        )
        group["contexts"].append(crosswalk_id)
        ctx_pli = [int(idx) for idx in context.get("ped_link_indices", [])]
        # Per-context fallback: if ped_link_indices is missing in manifest, re-derive from net.xml
        # before accumulating into the TLS-level union.
        if not ctx_pli:
            ctx_tls_id = str(context.get("tls_id") or "")
            ctx_crossing_edge = str(context.get("crossing_edge") or "")
            _ctx_net = Path(context.get("net_file") or str(net_file))
            # network_with_signal.net.xml 이 같은 디렉터리에 있으면 우선 사용 (구 manifest 하위호환)
            _signal_net = _ctx_net.parent / "network_with_signal.net.xml"
            ctx_net_file = _signal_net if _signal_net.exists() else _ctx_net
            if ctx_tls_id and ctx_crossing_edge:
                try:
                    ctx_pli = pedestrian_link_indices(ctx_net_file, ctx_tls_id, ctx_crossing_edge)
                except Exception:
                    ctx_pli = []
        group["ped_link_indices_union"].update(ctx_pli)
        if float(signal_timing["ped_green"]) > float(group["signal_timing"]["ped_green"]):
            group["signal_timing"] = signal_timing
        if crosswalk_id in smart_target_ids:
            group["target_context_ids"].add(crosswalk_id)
        adjacent_lanes: set[str] = set()
        adjacent_tls_count = 0
        context_states.append(
            {
                "crosswalk_id": crosswalk_id,
                "metadata": context,
                "scope": scope,
                "signal_timing": signal_timing,
                "ped_detector_edges": ped_detector_edges,
                "context_person_first_seen": {},
                "ped_wait_recorded": set(),
                "all_seen_pedestrians": set(),
                "elderly_seen_pedestrians": set(),
                "ped_crossing_enter_time": {},
                "pedestrian_crossing_times": [],
                "pedestrian_wait_times": [],
                "pet_a_records": [],
                "pet_b_records": [],
                "elderly_incomplete": 0,
                "vehicle_on_conflict_prev": set(),
                "vehicle_exit_times": deque(maxlen=1000),
                "queue_lengths": [],
                "area_vehicle_waits": {},
                "area_seen_vehicles": set(),
                "area_active_since": {},
                "area_entry_count": 0,
                "area_exit_count": 0,
                "area_travel_times": [],
                "area_vehicle_time_sec": 0.0,
                "total_vehicle_wait_exposure": 0.0,
                "surrounding_queue_totals": [],
                "direction_queue_totals": {direction: [] for direction in COMPASS_DIRECTIONS},
                "adjacent_lanes": adjacent_lanes,
                "adjacent_tls_count": adjacent_tls_count,
                "adjacent_tls_queue_totals": [],
                "surrounding_speed_samples": [],
                "spillback_steps": 0,
                "measure_steps": 0,
                "local_extension_count": 0,
                "local_extension_time": 0.0,
            }
        )

    if incident_schedule:
        incident_events = parse_incident_schedule(incident_schedule)
    else:
        incident_events = generate_incident_schedule(
            disruption_scenario,
            sim_duration,
            seed,
            {
                "approach_lanes": sorted(union_monitored_lanes),
                "vehicle_conflict_edges": sorted(union_conflict_edges),
            },
            sorted(union_monitored_lanes),
            model_params,
            enable_random_disruptions,
            bus_stop_rate_per_hour,
            illegal_parking_rate_per_hour,
            minor_incident_rate_per_hour,
            accident_rate_per_hour,
        )

    traci_port = free_traci_port()
    traci_label = f"integrated_{scenario}_{seed}_{random.randint(0, 1_000_000)}"
    out_dir = Path(output_dir) if output_dir is not None else None
    traci_trace_file = None
    sumo_stdout_file = out_dir / f"sumo_stdout_{scenario}_seed{seed}.log" if out_dir else None
    sumo_stderr_file = out_dir / f"sumo_stderr_{scenario}_seed{seed}.log" if out_dir else None
    sumo_stdout_handle = None
    sumo_cmd = [
        sumo_binary(),
        "-c",
        str(sumocfg),
        "--seed",
        str(seed),
        "--step-length",
        str(traci_step_length),
        "--time-to-teleport",
        "300",
        "--no-warnings",
        "--no-step-log",
    ]
    if sumo_stderr_file is not None:
        sumo_cmd.extend(["--error-log", str(sumo_stderr_file)])
    start_ts = time.time()
    record_runtime_event(
        "traci_start_begin",
        timestamp=start_ts,
        scenario=scenario,
        seed=int(seed),
        port=int(traci_port),
        traci_label=traci_label,
        sumocfg=str(sumocfg),
        sumo_command=" ".join(sumo_cmd),
        traci_trace_file=str(traci_trace_file) if traci_trace_file else "",
        sumo_stdout_file=str(sumo_stdout_file) if sumo_stdout_file else "",
        sumo_stderr_file=str(sumo_stderr_file) if sumo_stderr_file else "",
    )
    try:
        if sumo_stdout_file is not None:
            sumo_stdout_handle = open(sumo_stdout_file, "w", encoding="utf-8")
        traci.start(
            sumo_cmd,
            port=traci_port,
            numRetries=180,
            label=traci_label,
            verbose=False,
            traceFile=None,
            stdout=sumo_stdout_handle,
        )
        traci.switch(traci_label)
    except Exception as exc:
        debug_state.update(
            exception_type=type(exc).__name__,
            exception_message=str(exc),
            sumo_return_code=_active_sumo_return_code(traci_label),
        )
        failure_payload = {
            **debug_state,
            "exception_type": type(exc).__name__,
            "exception_message": str(exc),
        }
        _runtime_failure_write(failure_context_path, failure_payload)
        record_runtime_event(
            "traci_start_exception",
            exception_type=type(exc).__name__,
            exception_message=str(exc),
            elapsed_seconds=float(time.time() - start_ts),
        )
        raise
    finally:
        if sumo_stdout_handle is not None:
            sumo_stdout_handle.close()
    debug_state.update(sumo_return_code=_active_sumo_return_code(traci_label))
    record_runtime_event(
        "traci_started",
        sumocfg=str(sumocfg),
        traci_label=traci_label,
        elapsed_seconds=float(time.time() - start_ts),
        sumo_return_code=_active_sumo_return_code(traci_label),
        tls_count=int(len(traci.trafficlight.getIDList())),
        simulation_time=float(traci.simulation.getTime()),
    )

    lane_original_states: dict[str, LaneState] = {}
    active_event_ids: set[str] = set()
    disruption_active_time = 0.0
    cumulative_capacity_multiplier = 0.0
    cumulative_effective_capacity = 0.0
    lost_capacity_time = 0.0
    capacity_loss_due_to_disruptions = 0.0
    event_type_counts = Counter(event.event_type for event in incident_events)
    event_type_total_durations = Counter()
    for event in incident_events:
        event_type_total_durations[event.event_type] += float(max(0.0, event.end_time - event.start_time))

    extension_events_log: list[dict[str, Any]] = []
    incident_impacts: list[dict[str, Any]] = []
    diag: dict[str, int] = {
        "smart_extension_eval_count": 0,
        "smart_extension_ped_green_count": 0,
        "smart_extension_remaining_trigger_window_count": 0,
        "smart_extension_detected_peds_count": 0,
        "smart_extension_sensor_pass_count": 0,
        "smart_extension_decision_true_count": 0,
        "smart_extension_block_missing_tls_id_count": 0,
        "smart_extension_block_missing_ped_link_indices_count": 0,
        "smart_extension_block_ped_signal_not_green_count": 0,
        "smart_extension_block_remaining_time_sufficient_count": 0,
        "smart_extension_block_max_extension_reached_count": 0,
        "smart_extension_block_already_extended_in_cycle_count": 0,
        "smart_extension_block_no_pedestrians_detected_count": 0,
        "smart_extension_block_sensor_false_negative_count": 0,
    }
    diag_rows: list[dict[str, Any]] = []
    incident_events_log = [
        serialize_incident_event(event, "integrated_selected", seed, "shared")
        for event in incident_events
    ]
    ts_queue: deque[tuple[float, float]] = deque(maxlen=10000)
    ts_wait: deque[tuple[float, float]] = deque(maxlen=10000)
    ts_speed: deque[tuple[float, float]] = deque(maxlen=10000)

    network_arrival_count = 0
    network_first_seen: dict[str, float] = {}
    network_travel_times: list[float] = []
    network_delay_step_means: list[float] = []
    network_queue_counts: list[int] = []
    network_speed_means: list[float] = []
    network_spillback_steps = 0
    network_measure_steps = 0
    lane_measure_steps = 0
    teleported_vehicle_count = 0
    known_tls_ids: set[str] = set()
    tls_refresh_countdown = 0

    fcd_vehicle_records: list[tuple[float, str, float, float, float, str]] = []
    fcd_person_records: list[tuple[float, str, float, float, float, str]] = []
    lane_data_records: list[tuple[float, str, int, float]] = []
    edge_data_records: list[tuple[float, str, int, float]] = []
    active_target_count = int(sum(1 for state in context_states if state["crosswalk_id"] in smart_target_ids))
    next_lane_sample_t = 0.0
    next_vehicle_sample_t = 0.0
    next_progress_log_t = 0.0
    progress_output_path = (Path(output_dir) / "runtime_progress.csv") if output_dir else None
    runtime_wall_t0 = time.perf_counter()
    step_cache = StepMetricCache({}, {})

    # feature-flagged risk event collection buffers
    _risk_ped_frames: list[dict[str, Any]] = []
    _risk_veh_frames: list[dict[str, Any]] = []
    _risk_next_sample_t: float = float(warmup)
    _risk_sample_interval = max(float(risk_event_sample_interval_s), float(traci_step_length))

    try:
        for state in context_states:
            adjacent_lanes, adjacent_tls_count = adjacent_tls_lanes(
                state["metadata"].get("tls_id"),
                set(state["scope"]["monitored_lanes"]),
            )
            state["adjacent_lanes"] = adjacent_lanes
            state["adjacent_tls_count"] = adjacent_tls_count

        end_time = warmup + sim_duration
        progress_log_interval_s = 25.0 if scenario == "smart_selected" else 100.0
        next_progress_log = 0.0
        while traci.simulation.getTime() < end_time:
            debug_state.update(last_traci_call="traci.simulationStep", last_object_id="", current_crosswalk_id="", tls_id="", crossing_edge="")
            try:
                traci.simulationStep()
            except Exception as exc:
                debug_state.update(
                    exception_type=type(exc).__name__,
                    exception_message=str(exc),
                    sumo_return_code=_active_sumo_return_code(traci_label),
                )
                failure_payload = {
                    **debug_state,
                    "exception_type": type(exc).__name__,
                    "exception_message": str(exc),
                }
                _runtime_failure_write(failure_context_path, failure_payload)
                raise
            t = float(traci.simulation.getTime())
            rel_t = max(0.0, t - warmup)
            debug_state.update(simulation_time=t, rel_time=rel_t)
            step_cache.reset()
            lane_sample_due = _lane_sample_due(metric_sample_interval_s, rel_t, next_lane_sample_t)
            if lane_sample_due and metric_sample_interval_s > 0:
                next_lane_sample_t += metric_sample_interval_s
            vehicle_sample_due = _lane_sample_due(vehicle_sample_interval_s, rel_t, next_vehicle_sample_t)
            if vehicle_sample_due and vehicle_sample_interval_s > 0:
                next_vehicle_sample_t += vehicle_sample_interval_s
            loop_timing_ms: dict[str, float] = {
                "incident_loop_ms": 0.0,
                "vehicle_metrics_loop_ms": 0.0,
                "lane_edge_loop_ms": 0.0,
                "trafficlight_loop_ms": 0.0,
                "trafficlight_extension_loop_ms": 0.0,
                "per_crosswalk_context_loop_ms": 0.0,
            }
            collect = t > warmup
            if tls_refresh_countdown <= 0:
                known_tls_ids = traci_trafficlight_ids()
                tls_refresh_countdown = 10
            else:
                tls_refresh_countdown -= 1

            incident_loop_t0 = time.perf_counter()
            currently_active = active_incidents(incident_events, rel_t)
            current_ids = {event.incident_id for event in currently_active}
            for event in currently_active:
                if event.incident_id not in active_event_ids:
                    apply_incident(event, lane_original_states)
            for event in incident_events:
                if event.incident_id in active_event_ids and event.incident_id not in current_ids:
                    restore_incident(event, lane_original_states)
                    before_vals = [v for tt, v in ts_queue if event.start_time - 60 <= tt < event.start_time]
                    after_vals = [v for tt, v in ts_queue if event.end_time <= tt <= event.end_time + 60]
                    before_wait = [v for tt, v in ts_wait if event.start_time - 60 <= tt < event.start_time]
                    after_wait = [v for tt, v in ts_wait if event.end_time <= tt <= event.end_time + 60]
                    before_spd = [v for tt, v in ts_speed if event.start_time - 60 <= tt < event.start_time]
                    after_spd = [v for tt, v in ts_speed if event.end_time <= tt <= event.end_time + 60]
                    incident_impacts.append(
                        {
                            "incident_id": event.incident_id,
                            "crosswalk_id": "integrated_selected",
                            "seed": seed,
                            "scenario": scenario,
                            "event_type": event.event_type,
                            "before_queue_avg": float(np.nanmean(before_vals)) if before_vals else 0.0,
                            "after_queue_avg": float(np.nanmean(after_vals)) if after_vals else 0.0,
                            "before_wait_avg_sec": float(np.nanmean(before_wait)) if before_wait else 0.0,
                            "after_wait_avg_sec": float(np.nanmean(after_wait)) if after_wait else 0.0,
                            "before_speed_avg_mps": float(np.nanmean(before_spd)) if before_spd else 0.0,
                            "after_speed_avg_mps": float(np.nanmean(after_spd)) if after_spd else 0.0,
                        }
                    )
            active_event_ids = current_ids
            loop_timing_ms["incident_loop_ms"] = float((time.perf_counter() - incident_loop_t0) * 1000.0)

            active_capacity_multiplier = (
                min(event.capacity_multiplier for event in currently_active) if currently_active else 1.0
            )
            if currently_active:
                disruption_active_time += traci_step_length
            cumulative_capacity_multiplier += active_capacity_multiplier
            cumulative_effective_capacity += active_capacity_multiplier
            lost_capacity_time += (1.0 - active_capacity_multiplier) * traci_step_length
            capacity_loss_due_to_disruptions += (1.0 - active_capacity_multiplier)

            current_vehicle_ids = set(traci.vehicle.getIDList())
            arrived_ids = set(traci.simulation.getArrivedIDList())
            try:
                teleported_vehicle_count += len(set(traci.simulation.getStartingTeleportIDList()))
            except Exception:
                pass
            if collect:
                network_measure_steps += 1
                network_arrival_count += len(arrived_ids)
            for veh_id in arrived_ids:
                first_seen = network_first_seen.pop(veh_id, None)
                if first_seen is not None:
                    network_travel_times.append(max(0.0, t - first_seen))
            for veh_id in current_vehicle_ids:
                network_first_seen.setdefault(veh_id, t)

            vehicle_metrics_t0 = time.perf_counter()
            if vehicle_sample_due:
                vehicle_state_cache: dict[str, dict[str, float | str] | None] = {}
                all_vehicle_speeds: list[float] = []
                for veh_id in current_vehicle_ids:
                    vehicle_state = _vehicle_state_snapshot(veh_id, vehicle_state_cache)
                    if vehicle_state is None:
                        continue
                    all_vehicle_speeds.append(float(vehicle_state["speed"]))

                if collect:
                    avg_wait = float(
                        np.nanmean([float(info["accumulated_wait"]) for info in vehicle_state_cache.values() if info is not None])
                    ) if vehicle_state_cache else 0.0
                    network_delay_step_means.append(avg_wait)
                    network_queue_counts.append(
                        int(sum(1 for info in vehicle_state_cache.values() if info is not None and float(info["speed"]) < 0.1))
                    )
                    avg_speed = float(np.nanmean(all_vehicle_speeds)) if all_vehicle_speeds else 0.0
                    network_speed_means.append(avg_speed)
                    ts_wait.append((rel_t, avg_wait))
                    ts_speed.append((rel_t, avg_speed))
                    ts_queue.append((rel_t, float(network_queue_counts[-1])))
            loop_timing_ms["vehicle_metrics_loop_ms"] = float((time.perf_counter() - vehicle_metrics_t0) * 1000.0)

            union_lane_ids = sorted(
                {
                    lane_id
                    for state in context_states
                    for lane_id in state["scope"]["monitored_lanes"]
                }
            )
            lane_halting: dict[str, int] = {}
            lane_occupancy: dict[str, float] = {}
            lane_speeds: dict[str, float] = {}
            lane_edge_t0 = time.perf_counter()
            if collect and lane_sample_due:
                lane_measure_steps += 1
                for lane_id in union_lane_ids:
                    try:
                        lane_state = _lane_metric_snapshot(lane_id, step_cache.lane_metrics)
                        if lane_state is None:
                            continue
                        halted = int(lane_state["halting_number"])
                        lane_halting[lane_id] = halted
                        lane_occupancy[lane_id] = float(lane_state["occupancy"])
                        lane_speeds[lane_id] = float(lane_state["mean_speed"])
                        lane_data_records.append((rel_t, lane_id, halted, lane_speeds[lane_id]))
                    except Exception:
                        continue
                edge_queue: dict[str, int] = {}
                edge_speed_sum: dict[str, float] = {}
                edge_speed_cnt: dict[str, int] = {}
                for state in context_states:
                    for lane_id in state["scope"]["monitored_lanes"]:
                        if lane_id not in lane_halting:
                            continue
                        edge_id = state["scope"]["lane_to_edge"].get(lane_id, lane_id.rsplit("_", 1)[0])
                        edge_queue[edge_id] = edge_queue.get(edge_id, 0) + lane_halting[lane_id]
                        edge_speed_sum[edge_id] = edge_speed_sum.get(edge_id, 0.0) + lane_speeds.get(lane_id, 0.0)
                        edge_speed_cnt[edge_id] = edge_speed_cnt.get(edge_id, 0) + 1
                for edge_id, queue in edge_queue.items():
                    mean_sp = edge_speed_sum.get(edge_id, 0.0) / max(1, edge_speed_cnt.get(edge_id, 1))
                    edge_data_records.append((rel_t, edge_id, queue, mean_sp))
            loop_timing_ms["lane_edge_loop_ms"] = float((time.perf_counter() - lane_edge_t0) * 1000.0)

            trafficlight_t0 = time.perf_counter()
            trafficlight_extension_elapsed_ms = 0.0
            for group in group_defs.values():
                raw_tl_id = group["tl_id"] or None
                tl_id = raw_tl_id if is_known_trafficlight(raw_tl_id, known_tls_ids) else None
                debug_state.update(
                    current_crosswalk_id=str(group["contexts"][0]) if group["contexts"] else "",
                    tls_id=str(tl_id or raw_tl_id or ""),
                    crossing_edge="",
                )
                try:
                    debug_state.update(last_traci_call="traci.trafficlight.getPhase", last_object_id=str(tl_id or ""))
                    current_phase = traci.trafficlight.getPhase(tl_id) if tl_id else -1
                except Exception:
                    tl_id = None
                    current_phase = -1
                    tls_refresh_countdown = 0
                state_str = phase_state(tl_id, current_phase, known_tls_ids=known_tls_ids) if tl_id else ""
                ped_link_indices_union = sorted(group["ped_link_indices_union"])
                current_phase_is_ped_green = is_ped_green_state(state_str, ped_link_indices_union)
                current_phase_is_vehicle_green = is_vehicle_green_state(state_str, ped_link_indices_union)
                phase_changed = current_phase != group["prev_phase"]

                if collect and phase_changed and group["prev_phase_was_ped_green"] and not current_phase_is_ped_green:
                    for context_state in context_states:
                        if context_state["crosswalk_id"] not in group["contexts"]:
                            continue
                        crossing_edge = str(context_state["metadata"]["crossing_edge"])
                        debug_state.update(
                            current_crosswalk_id=str(context_state["crosswalk_id"]),
                            tls_id=str(tl_id or raw_tl_id or ""),
                            crossing_edge=crossing_edge,
                            last_traci_call="traci.edge.getLastStepPersonIDs",
                            last_object_id=crossing_edge,
                        )
                        for ped_id in traci.edge.getLastStepPersonIDs(crossing_edge):
                            try:
                                debug_state.update(
                                    current_crosswalk_id=str(context_state["crosswalk_id"]),
                                    tls_id=str(tl_id or raw_tl_id or ""),
                                    crossing_edge=crossing_edge,
                                    last_traci_call="traci.person.getLanePosition",
                                    last_object_id=str(ped_id),
                                )
                                pos = float(traci.person.getLanePosition(ped_id))
                                debug_state.update(last_traci_call="traci.person.getSpeed", last_object_id=str(ped_id))
                                speed = float(traci.person.getSpeed(ped_id))
                                if speed <= 0:
                                    speed = 0.5
                                remaining_dist = max(
                                    0.0,
                                    float(context_state["metadata"]["crossing_length_m"]) - pos,
                                )
                                time_to_clear = remaining_dist / speed
                                context_state["pet_b_records"].append(
                                    signal_params["all_red_time"] - time_to_clear
                                )
                                if "elderly" in traci.person.getTypeID(ped_id):
                                    context_state["elderly_incomplete"] += 1
                            except Exception:
                                continue

                if phase_changed:
                    tune_phase_duration(
                        tl_id,
                        current_phase,
                        ped_link_indices_union,
                        group["signal_timing"],
                        known_tls_ids=known_tls_ids,
                    )
                    if current_phase_is_vehicle_green and not group["prev_vehicle_green"]:
                        group["cycle_count"] += 1
                        group["extension_count_in_cycle"] = 0
                        group["extended_in_cycle"] = False
                    group["prev_phase"] = current_phase

                if (
                    scenario == "smart_selected"
                    and tl_id
                    and ped_link_indices_union
                    and group["target_context_ids"]
                    and not current_phase_is_ped_green
                ):
                    diag["smart_extension_block_ped_signal_not_green_count"] += 1

                if (
                    scenario == "smart_selected"
                    and tl_id
                    and current_phase_is_ped_green
                    and group["target_context_ids"]
                ):
                    diag["smart_extension_eval_count"] += 1
                    diag["smart_extension_ped_green_count"] += 1
                    extension_t0 = time.perf_counter()
                    try:
                        debug_state.update(
                            last_traci_call="traci.trafficlight.getNextSwitch",
                            last_object_id=str(tl_id or ""),
                        )
                        next_switch = float(traci.trafficlight.getNextSwitch(tl_id))
                    except Exception:
                        tls_refresh_countdown = 0
                        continue
                    remaining = next_switch - t
                    # Fetch peds only when pre-conditions pass (preserves original TraCI call timing).
                    triggered_context_ids: list[str] = []
                    ped_on_crossing_total = 0
                    _ctx_ped_counts: dict[str, tuple[int, int]] = {}
                    if (
                        remaining <= signal_params["trigger_remaining"]
                        and group["extension_count_in_cycle"] < signal_params["max_extensions"]
                        and not group["extended_in_cycle"]
                    ):
                        diag["smart_extension_remaining_trigger_window_count"] += 1
                        for context_state in context_states:
                            crosswalk_id = context_state["crosswalk_id"]
                            if crosswalk_id not in group["target_context_ids"]:
                                continue
                            if crosswalk_id not in smart_target_ids:
                                continue
                            ped_link_indices = [int(idx) for idx in context_state["metadata"].get("ped_link_indices", [])]
                            if not is_ped_green_state(state_str, ped_link_indices):
                                continue
                            crossing_edge = str(context_state["metadata"]["crossing_edge"])
                            ped_on_crossing = set(traci.edge.getLastStepPersonIDs(crossing_edge))
                            ped_on_detectors = set()
                            for edge_id in context_state["ped_detector_edges"]:
                                try:
                                    ped_on_detectors.update(traci.edge.getLastStepPersonIDs(edge_id))
                                except Exception:
                                    continue
                            detected_peds = ped_on_crossing | ped_on_detectors
                            _ctx_ped_counts[crosswalk_id] = (len(ped_on_crossing), len(ped_on_detectors))
                            if detected_peds:
                                triggered_context_ids.append(crosswalk_id)
                                ped_on_crossing_total += len(ped_on_crossing)
                    # random.random() called only when triggered_context_ids non-empty — same timing as before.
                    sensor_pass = random.random() > signal_params["sensor_fn_rate"] if triggered_context_ids else False
                    if triggered_context_ids:
                        diag["smart_extension_detected_peds_count"] += 1
                    if triggered_context_ids and sensor_pass:
                        diag["smart_extension_sensor_pass_count"] += 1
                    decision = evaluate_smart_extension_decision(
                        tls_id=tl_id,
                        ped_link_indices=ped_link_indices_union,
                        tls_state=state_str,
                        remaining_s=remaining,
                        pedestrians_detected=bool(triggered_context_ids),
                        extension_count_in_cycle=group["extension_count_in_cycle"],
                        extended_in_cycle=group["extended_in_cycle"],
                        signal_params=signal_params,
                        sensor_pass=sensor_pass,
                    )
                    _reason_key = f"smart_extension_block_{decision['reason']}_count"
                    if _reason_key in diag:
                        diag[_reason_key] += 1
                    if decision["should_extend"]:
                        diag["smart_extension_decision_true_count"] += 1
                    _should_log_diag = (
                        current_phase_is_ped_green
                        or remaining <= signal_params["trigger_remaining"]
                        or bool(triggered_context_ids)
                        or decision["reason"] != "ped_signal_not_green"
                    )
                    if _should_log_diag:
                        for _ctx in context_states:
                            if _ctx["crosswalk_id"] not in group["target_context_ids"]:
                                continue
                            _cid = _ctx["crosswalk_id"]
                            _pl = [int(i) for i in _ctx["metadata"].get("ped_link_indices", [])]
                            _cross_cnt, _det_cnt = _ctx_ped_counts.get(_cid, (0, 0))
                            diag_rows.append({
                                "time_s": t,
                                "scenario": scenario,
                                "seed": seed,
                                "crosswalk_id": _cid,
                                "tls_id": tl_id,
                                "phase_index": int(current_phase),
                                "tls_state": state_str,
                                "ped_link_indices": json.dumps(_pl),
                                "is_ped_green": is_ped_green_state(state_str, _pl),
                                "remaining_s": remaining,
                                "trigger_remaining": signal_params["trigger_remaining"],
                                "crossing_edge": str(_ctx["metadata"].get("crossing_edge", "")),
                                "ped_detector_edges": json.dumps(_json_safe(_ctx.get("ped_detector_edges", []))),
                                "ped_on_crossing_count": _cross_cnt,
                                "ped_on_detector_count": _det_cnt,
                                "detected_peds_count": _cross_cnt + _det_cnt,
                                "context_triggered": int(_cid in triggered_context_ids),
                                "sensor_pass": sensor_pass,
                                "decision_should_extend": decision["should_extend"],
                                "decision_reason": decision["reason"],
                                "extension_sec": decision["extension_sec"],
                            })
                    if decision["should_extend"]:
                        try:
                            debug_state.update(
                                last_traci_call="traci.trafficlight.setPhaseDuration",
                                last_object_id=str(tl_id or ""),
                            )
                            traci.trafficlight.setPhaseDuration(
                                tl_id,
                                remaining + decision["extension_sec"],
                            )
                        except Exception:
                            tls_refresh_countdown = 0
                            continue
                        group["extension_count_in_cycle"] += 1
                        group["extended_in_cycle"] = True
                        group["total_extension_count"] += 1
                        group["pedestrian_green_extension_time"] += decision["extension_sec"]
                        ped_indices_union_set = set(ped_link_indices_union)
                        extension_events_log.append(
                            {
                                "sim_time": t,
                                "crosswalk_id": "|".join(triggered_context_ids),
                                "crosswalk_ids": json.dumps(triggered_context_ids, ensure_ascii=False),
                                "tls_id": str(tl_id),
                                "phase_index": int(current_phase),
                                "tls_state": str(state_str),
                                "ped_link_indices": json.dumps(list(ped_link_indices_union), ensure_ascii=False),
                                "vehicle_green_link_count": int(
                                    sum(
                                        1
                                        for idx, ch in enumerate(state_str)
                                        if idx not in ped_indices_union_set and ch in {"g", "G"}
                                    )
                                ),
                                "is_ped_only_phase": int(
                                    is_ped_green_state(state_str, ped_link_indices_union)
                                    and not any(
                                        ch in {"g", "G"}
                                        for idx, ch in enumerate(state_str)
                                        if idx not in ped_indices_union_set
                                    )
                                ),
                                "remaining_before_extension": float(remaining),
                                "extension_sec": float(decision["extension_sec"]),
                                "ped_count_on_crossing": int(ped_on_crossing_total),
                                "seed": int(seed),
                                "scenario": scenario,
                            }
                        )
                        for context_state in context_states:
                            if context_state["crosswalk_id"] in triggered_context_ids:
                                context_state["local_extension_count"] += 1
                                context_state["local_extension_time"] += decision["extension_sec"]
                    trafficlight_extension_elapsed_ms += float((time.perf_counter() - extension_t0) * 1000.0)

                group["prev_phase_was_ped_green"] = current_phase_is_ped_green
                group["prev_vehicle_green"] = current_phase_is_vehicle_green
            loop_timing_ms["trafficlight_loop_ms"] = float((time.perf_counter() - trafficlight_t0) * 1000.0)
            loop_timing_ms["trafficlight_extension_loop_ms"] = float(trafficlight_extension_elapsed_ms)

            per_crosswalk_t0 = time.perf_counter()
            if collect:
                any_spillback_now = False
                current_person_ids = set(traci.person.getIDList())
                if export_fcd:
                    if vehicle_sample_due:
                        for veh_id, info in vehicle_state_cache.items():
                            if info is None:
                                continue
                            try:
                                x, y = traci.vehicle.getPosition(veh_id)
                                fcd_vehicle_records.append(
                                    (rel_t, veh_id, float(x), float(y), float(info["speed"]), str(info["road_id"]))
                                )
                            except Exception:
                                continue
                    for person_id in current_person_ids:
                        try:
                            x, y = traci.person.getPosition(person_id)
                            fcd_person_records.append(
                                (
                                    rel_t,
                                    person_id,
                                    float(x),
                                    float(y),
                                    float(traci.person.getSpeed(person_id)),
                                    traci.person.getRoadID(person_id),
                                )
                            )
                        except Exception:
                            continue

                for context_state in context_states:
                    context_state["measure_steps"] += 1
                    metadata = context_state["metadata"]
                    crossing_edge = str(metadata["crossing_edge"])
                    debug_state.update(
                        current_crosswalk_id=str(context_state["crosswalk_id"]),
                        tls_id=str(metadata.get("tls_id") or ""),
                        crossing_edge=crossing_edge,
                    )
                    if vehicle_sample_due:
                        current_vehicle_conflict = {
                            veh_id
                            for veh_id, info in vehicle_state_cache.items()
                            if info is not None
                            and str(info["road_id"]) in set(str(edge) for edge in metadata.get("vehicle_conflict_edges", []))
                        }
                        for veh_id in context_state["vehicle_on_conflict_prev"] - current_vehicle_conflict:
                            context_state["vehicle_exit_times"].append(t)
                        context_state["vehicle_on_conflict_prev"] = current_vehicle_conflict

                    debug_state.update(
                        last_traci_call="traci.edge.getLastStepPersonIDs",
                        last_object_id=crossing_edge,
                    )
                    peds_on_crossing = set(traci.edge.getLastStepPersonIDs(crossing_edge))
                    detector_peds = set()
                    for edge_id in context_state["ped_detector_edges"]:
                        try:
                            debug_state.update(
                                last_traci_call="traci.edge.getLastStepPersonIDs",
                                last_object_id=str(edge_id),
                            )
                            detector_peds.update(traci.edge.getLastStepPersonIDs(edge_id))
                        except Exception:
                            continue
                    observed_peds = peds_on_crossing | detector_peds
                    for ped_id in observed_peds:
                        context_state["context_person_first_seen"].setdefault(ped_id, t)
                        context_state["all_seen_pedestrians"].add(ped_id)
                        try:
                            debug_state.update(
                                last_traci_call="traci.person.getTypeID",
                                last_object_id=str(ped_id),
                            )
                            if traci.person.getTypeID(ped_id) == "elderly":
                                context_state["elderly_seen_pedestrians"].add(ped_id)
                        except Exception:
                            continue
                    for ped_id in peds_on_crossing:
                        if ped_id not in context_state["ped_wait_recorded"]:
                            context_state["pedestrian_wait_times"].append(
                                max(0.0, t - context_state["context_person_first_seen"].get(ped_id, t))
                            )
                            context_state["ped_wait_recorded"].add(ped_id)
                        context_state["ped_crossing_enter_time"].setdefault(ped_id, t)
                    for ped_id in list(context_state["ped_crossing_enter_time"]):
                        if ped_id not in peds_on_crossing:
                            context_state["pedestrian_crossing_times"].append(
                                max(0.0, t - context_state["ped_crossing_enter_time"].pop(ped_id))
                            )
                    prev_entered = set(context_state.setdefault("ped_entered_crossing", set()))
                    for ped_id in peds_on_crossing - prev_entered:
                        candidates = [
                            vt
                            for vt in recent_values(context_state["vehicle_exit_times"], t, 30.0)
                            if vt <= t
                        ]
                        if candidates:
                            context_state["pet_a_records"].append(t - max(candidates))
                    context_state["ped_entered_crossing"] = prev_entered | peds_on_crossing

                    if vehicle_sample_due:
                        current_area_vehicles = set()
                        area_wait_sum = 0.0
                        monitored_lanes_set = set(context_state["scope"]["monitored_lanes"])
                        for veh_id, info in vehicle_state_cache.items():
                            if info is None:
                                continue
                            lane_id = str(info["lane_id"])
                            if lane_id in monitored_lanes_set:
                                current_area_vehicles.add(veh_id)
                                context_state["area_seen_vehicles"].add(veh_id)
                                context_state["area_vehicle_waits"][veh_id] = float(info["accumulated_wait"])
                                area_wait_sum += float(info["accumulated_wait"])
                                if veh_id not in context_state["area_active_since"]:
                                    context_state["area_active_since"][veh_id] = t
                                    context_state["area_entry_count"] += 1
                        for veh_id in list(context_state["area_active_since"]):
                            if veh_id not in current_area_vehicles:
                                context_state["area_travel_times"].append(
                                    max(0.0, t - context_state["area_active_since"].pop(veh_id))
                                )
                                context_state["area_exit_count"] += 1
                        context_state["area_vehicle_time_sec"] += len(current_area_vehicles) * traci_step_length
                        context_state["total_vehicle_wait_exposure"] += area_wait_sum * traci_step_length

                    for lane_id in metadata.get("approach_lanes", []):
                        lane_state = _lane_metric_snapshot(str(lane_id), step_cache.lane_metrics)
                        if lane_state is None:
                            continue
                        context_state["queue_lengths"].append(int(lane_state["halting_number"]))
                    if lane_sample_due:
                        surrounding_queue_total = sum(
                            lane_halting.get(lane_id, 0)
                            for lane_id in context_state["scope"]["monitored_lanes"]
                        )
                        context_state["surrounding_queue_totals"].append(surrounding_queue_total)
                        if vehicle_sample_due:
                            context_state["surrounding_speed_samples"].append(
                                float(
                                    np.nanmean(
                                        [
                                            lane_speeds.get(lane_id, np.nan)
                                            for lane_id in context_state["scope"]["monitored_lanes"]
                                            if lane_id in lane_speeds
                                        ]
                                    )
                                )
                                if context_state["scope"]["monitored_lanes"]
                                else 0.0
                            )
                        for direction, lanes in context_state["scope"]["direction_lanes"].items():
                            context_state["direction_queue_totals"][direction].append(
                                sum(lane_halting.get(lane_id, 0) for lane_id in lanes)
                            )
                        context_state["adjacent_tls_queue_totals"].append(
                            sum(lane_halting.get(lane_id, 0) for lane_id in context_state["adjacent_lanes"])
                        )
                        spillback_now = False
                        for lane_id in context_state["scope"]["boundary_lanes"]:
                            halted = lane_halting.get(lane_id, 0)
                            occupancy = lane_occupancy.get(lane_id, 0.0)
                            capacity = max(1, int(context_state["scope"]["lane_capacities"].get(lane_id, 1)))
                            if halted >= max(1, int(capacity * 0.8)) or occupancy >= 85.0:
                                spillback_now = True
                                break
                        if spillback_now:
                            context_state["spillback_steps"] += 1
                            any_spillback_now = True
                if any_spillback_now:
                    network_spillback_steps += 1
            loop_timing_ms["per_crosswalk_context_loop_ms"] = float((time.perf_counter() - per_crosswalk_t0) * 1000.0)

            # feature-flagged: risk event frame collection (enable_risk_event_collection=True 시만 실행)
            if enable_risk_event_collection and collect and t >= _risk_next_sample_t:
                _risk_next_sample_t = t + _risk_sample_interval
                # vehicle frames
                if vehicle_sample_due:
                    for veh_id, vinfo in vehicle_state_cache.items():
                        if vinfo is None:
                            continue
                        try:
                            vx, vy = traci.vehicle.getPosition(veh_id)
                            _risk_veh_frames.append({
                                "time_s": t,
                                "vehicle_id": veh_id,
                                "edge_id": str(vinfo.get("road_id", "")),
                                "lane_id": str(vinfo.get("lane_id", "")),
                                "x": float(vx),
                                "y": float(vy),
                                "speed_mps": float(vinfo.get("speed", 0.0)),
                            })
                        except Exception:
                            continue
                # pedestrian frames: 시뮬레이션 내 모든 person 수집
                # (crossing_edge 메타데이터 유무에 관계없이 동작)
                try:
                    _all_person_ids = set(traci.person.getIDList())
                except Exception:
                    _all_person_ids = set()
                _ctx0_cw_id = str(context_states[0].get("crosswalk_id", "")) if context_states else ""
                for ped_id in _all_person_ids:
                    try:
                        px, py = traci.person.getPosition(ped_id)
                    except Exception:
                        continue
                    try:
                        _is_snr = "elderly" in str(traci.person.getTypeID(ped_id)).lower()
                    except Exception:
                        _is_snr = False
                    # crosswalk_id: road ID가 context crossing_edge와 일치하면 해당 id, 아니면 첫 번째 context
                    _ped_cw_id = _ctx0_cw_id
                    try:
                        _ped_road = traci.person.getRoadID(ped_id)
                        for _ctx in context_states:
                            _ctx_meta = _ctx.get("metadata") or {}
                            _ctx_cross = str(_ctx_meta.get("crossing_edge") or "")
                            if _ctx_cross and _ctx_cross == _ped_road:
                                _ped_cw_id = str(_ctx.get("crosswalk_id", ""))
                                break
                    except Exception:
                        pass
                    _risk_ped_frames.append({
                        "time_s": t,
                        "pedestrian_id": ped_id,
                        "crosswalk_id": _ped_cw_id,
                        "x": float(px),
                        "y": float(py),
                        "is_senior": _is_snr,
                        "signal_state": "",
                        "ped_remaining_crossing_time_s": float("nan"),
                    })

            if progress_interval_s > 0 and t + 1e-9 >= next_progress_log_t:
                while progress_interval_s > 0 and t + 1e-9 >= next_progress_log_t:
                    try:
                        vehicle_count = int(len(current_vehicle_ids))
                    except Exception:
                        vehicle_count = -1
                    try:
                        person_count = int(len(traci.person.getIDList()))
                    except Exception:
                        person_count = -1
                    try:
                        min_expected_number = int(traci.simulation.getMinExpectedNumber())
                    except Exception:
                        min_expected_number = -1
                    progress_row = {
                        "timestamp": time.time(),
                        "scenario": scenario,
                        "seed": int(seed),
                        "simulation_time": t,
                        "wall_time_sec": float(time.perf_counter() - runtime_wall_t0),
                        "vehicle_count": vehicle_count,
                        "person_count": person_count,
                        "min_expected_number": min_expected_number,
                    }
                    if progress_output_path is not None:
                        _runtime_progress_append(progress_output_path, progress_row)
                    else:
                        print(
                            "[progress]",
                            f"scenario={scenario}",
                            f"seed={seed}",
                            f"sim_time={t:.1f}",
                            f"wall={progress_row['wall_time_sec']:.1f}s",
                            f"vehicles={vehicle_count}",
                        )
                    next_progress_log_t += progress_interval_s

            if rel_t >= next_progress_log:
                try:
                    vehicle_count = int(len(traci.vehicle.getIDList()))
                except Exception:
                    vehicle_count = -1
                try:
                    person_count = int(len(traci.person.getIDList()))
                except Exception:
                    person_count = -1
                try:
                    min_expected_number = int(traci.simulation.getMinExpectedNumber())
                except Exception:
                    min_expected_number = -1
                record_runtime_event(
                    "runtime_loop_progress",
                    simulation_time=t,
                    rel_time=rel_t,
                    vehicle_count=vehicle_count,
                    person_count=person_count,
                    min_expected_number=min_expected_number,
                    active_target_count=active_target_count,
                    **loop_timing_ms,
                )
                next_progress_log += progress_log_interval_s
        record_runtime_event(
            "runtime_loop_complete",
            simulation_time=float(traci.simulation.getTime()),
            sumo_return_code=_active_sumo_return_code(traci_label),
        )
    except Exception as exc:
        debug_state.update(
            exception_type=type(exc).__name__,
            exception_message=str(exc),
            sumo_return_code=_active_sumo_return_code(traci_label),
        )
        failure_payload = {
            **debug_state,
            "exception_type": type(exc).__name__,
            "exception_message": str(exc),
        }
        _runtime_failure_write(failure_context_path, failure_payload)
        record_runtime_event("runtime_exception", exception_type=type(exc).__name__, exception_message=str(exc))
        raise
    finally:
        for event in incident_events:
            if event.incident_id in active_event_ids:
                restore_incident(event, lane_original_states)
        try:
            traci.switch(traci_label)
            traci.close(False)
        except Exception:
            pass

    per_crosswalk_metrics: list[dict[str, Any]] = []
    total_extension_count = 0
    total_extension_sec = 0.0
    for context_state in context_states:
        for enter_time in context_state["ped_crossing_enter_time"].values():
            context_state["pedestrian_crossing_times"].append(max(0.0, float(warmup + sim_duration) - float(enter_time)))
        vehicle_queue_length = int(context_state["queue_lengths"][-1]) if context_state["queue_lengths"] else 0
        vehicle_departures = int(context_state["area_exit_count"])
        vehicle_arrivals = int(context_state["area_entry_count"])
        cycle_count = 0
        metadata = context_state["metadata"]
        tl_id = str(metadata.get("tls_id") or "")
        if tl_id in group_defs:
            cycle_count = int(group_defs[tl_id]["cycle_count"])
        metrics = aggregate_metrics(
            context_state["pet_a_records"],
            context_state["pet_b_records"],
            int(context_state["elderly_incomplete"]),
            len(context_state["all_seen_pedestrians"]),
            len(context_state["elderly_seen_pedestrians"]),
            context_state["pedestrian_crossing_times"],
            context_state["area_vehicle_waits"],
            context_state["queue_lengths"],
            context_state["pedestrian_wait_times"],
            {
                "vehicle_queue_length": vehicle_queue_length,
                "total_vehicle_delay": float(context_state["total_vehicle_wait_exposure"]),
                "vehicle_arrivals": vehicle_arrivals,
                "vehicle_departures": vehicle_departures,
                "max_vehicle_queue_length": max(context_state["queue_lengths"]) if context_state["queue_lengths"] else 0,
                "cumulative_vehicle_queue_length": float(sum(context_state["queue_lengths"])),
                "pedestrian_green_extension_time": float(context_state["local_extension_time"]),
                "pedestrian_green_extension_count": int(context_state["local_extension_count"]),
                "cycle_count": cycle_count,
                "disruption_event_count": len(incident_events),
                "total_disruption_duration": float(sum(max(0.0, e.end_time - e.start_time) for e in incident_events)),
                "disruption_time_ratio": (disruption_active_time / sim_duration if sim_duration else 0.0),
                "average_capacity_multiplier": (
                    cumulative_capacity_multiplier / max(network_measure_steps, 1)
                ),
                "average_effective_capacity": (
                    cumulative_effective_capacity / max(network_measure_steps, 1)
                ),
                "lost_capacity_time": lost_capacity_time,
                "capacity_loss_due_to_disruptions": capacity_loss_due_to_disruptions,
                "event_type_counts": json.dumps(dict(sorted(event_type_counts.items())), ensure_ascii=False),
                "event_type_total_durations": json.dumps(
                    {key: float(value) for key, value in sorted(event_type_total_durations.items())},
                    ensure_ascii=False,
                ),
            },
            context_state["measure_steps"],
            sim_duration,
        )
        metrics.update(
            surrounding_metrics(
                context_state["scope"],
                context_state["area_vehicle_waits"],
                context_state["area_seen_vehicles"],
                context_state["area_entry_count"],
                context_state["area_exit_count"],
                context_state["area_travel_times"],
                context_state["area_vehicle_time_sec"],
                context_state["surrounding_queue_totals"],
                context_state["direction_queue_totals"],
                context_state["adjacent_tls_count"],
                context_state["adjacent_tls_queue_totals"],
                network_arrival_count,
                network_travel_times,
                context_state["surrounding_speed_samples"],
                context_state["spillback_steps"],
                lane_measure_steps,
                sim_duration,
            )
        )
        metrics.update(derived_summary_metrics(metrics, model_params, sensitivity_config))
        metrics["signal_extension_increment_sec"] = float(signal_params["extension_increment"])
        metrics["signal_max_extensions"] = int(signal_params["max_extensions"])
        metrics["vehicle_arrival_model"] = vehicle_arrival_model
        metrics["disruption_scenario"] = disruption_scenario
        per_crosswalk_metrics.append({"crosswalk_id": context_state["crosswalk_id"], **metrics})
        total_extension_count += int(context_state["local_extension_count"])
        total_extension_sec += float(context_state["local_extension_time"])

    total_safety_risk = float(np.nansum([row.get("safety_risk_score", 0.0) for row in per_crosswalk_metrics]))
    total_elderly_incomplete = float(np.nansum([row.get("elderly_incomplete_crossings", 0.0) for row in per_crosswalk_metrics]))
    network_metrics = {
        "scenario_group": "integrated_network",
        "scenario": scenario,
        "seed": int(seed),
        "safety_risk_score": total_safety_risk,
        "accident_expected_value": float(np.nansum([row.get("accident_expected_value", 0.0) for row in per_crosswalk_metrics])),
        "elderly_incomplete_crossings": total_elderly_incomplete,
        "avg_vehicle_delay_sec": float(np.nanmean(network_delay_step_means)) if network_delay_step_means else 0.0,
        "avg_queue_length": float(np.nanmean(network_queue_counts)) if network_queue_counts else 0.0,
        "max_queue_length": float(np.nanmax(network_queue_counts)) if network_queue_counts else 0.0,
        "surrounding_road_delay_sec": float(np.nanmean(network_delay_step_means)) if network_delay_step_means else 0.0,
        "vehicle_delay_cost": float(
            (float(np.nansum(network_delay_step_means)) / 3600.0)
            * float(get_parameter_value(model_params, "delay_cost_per_hour", 15000.0))
        ),
        "extension_count": float(total_extension_count),
        "total_extension_sec": float(total_extension_sec),
        "network_arrived_vehicles": int(network_arrival_count),
        "ped_link_indices_count": sum(
            len(list(g["ped_link_indices_union"])) for g in group_defs.values()
        ),
        "signal_extension_mapping_status": (
            "ok"
            if any(g["ped_link_indices_union"] for g in group_defs.values())
            else "empty_ped_link_indices"
        ),
        "network_avg_travel_time_sec": float(np.nanmean(network_travel_times)) if network_travel_times else 0.0,
        "network_avg_speed_mps": float(np.nanmean(network_speed_means)) if network_speed_means else 0.0,
        "network_teleported_vehicles": int(teleported_vehicle_count),
        "network_spillback_rate": float(network_spillback_steps / max(lane_measure_steps, 1)),
        "network_spillback_step_count": int(network_spillback_steps),
        "selected_crosswalk_count": int(len(crosswalk_contexts)),
        **diag,
    }

    file_exports: dict[str, Any] = {}
    if output_dir:
        out_dir = Path(output_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        if diag_rows:
            import pandas as _pd_diag
            _pd_diag.DataFrame(diag_rows).to_csv(
                out_dir / f"smart_extension_diagnostics_{scenario}_seed{seed}.csv",
                index=False,
                encoding="utf-8-sig",
            )
        edge_data_path = out_dir / f"edge_data_{scenario}_seed{seed}.xml"
        lane_data_path = out_dir / f"lane_data_{scenario}_seed{seed}.xml"
        file_exports["edge_data_path"] = str(edge_data_path)
        file_exports["lane_data_path"] = str(lane_data_path)

        edge_root = ET.Element("edgeData")
        for t, edge_id, queue, speed in edge_data_records:
            ET.SubElement(
                edge_root,
                "edge",
                {
                    "time": f"{t:.2f}",
                    "id": edge_id,
                    "queue": str(queue),
                    "meanSpeed": f"{speed:.4f}",
                },
            )
        ET.indent(edge_root, space="  ")
        ET.ElementTree(edge_root).write(edge_data_path, encoding="utf-8", xml_declaration=True)

        lane_root = ET.Element("laneData")
        for t, lane_id, queue, speed in lane_data_records:
            ET.SubElement(
                lane_root,
                "lane",
                {
                    "time": f"{t:.2f}",
                    "id": lane_id,
                    "queue": str(queue),
                    "meanSpeed": f"{speed:.4f}",
                },
            )
        ET.indent(lane_root, space="  ")
        ET.ElementTree(lane_root).write(lane_data_path, encoding="utf-8", xml_declaration=True)

        if export_fcd:
            fcd_vehicle_path = out_dir / f"fcd_vehicle_{scenario}_seed{seed}.xml"
            fcd_person_path = out_dir / f"fcd_person_{scenario}_seed{seed}.xml"
            file_exports["fcd_vehicle_path"] = str(fcd_vehicle_path)
            file_exports["fcd_person_path"] = str(fcd_person_path)

            veh_root = ET.Element("fcd-export")
            for t, veh_id, x, y, speed, road in fcd_vehicle_records:
                timestep = ET.SubElement(veh_root, "timestep", {"time": f"{t:.2f}"})
                ET.SubElement(
                    timestep,
                    "vehicle",
                    {
                        "id": veh_id,
                        "x": f"{x:.3f}",
                        "y": f"{y:.3f}",
                        "speed": f"{speed:.3f}",
                        "edge": road,
                    },
                )
            ET.indent(veh_root, space="  ")
            ET.ElementTree(veh_root).write(fcd_vehicle_path, encoding="utf-8", xml_declaration=True)

            person_root = ET.Element("fcd-export")
            for t, person_id, x, y, speed, road in fcd_person_records:
                timestep = ET.SubElement(person_root, "timestep", {"time": f"{t:.2f}"})
                ET.SubElement(
                    timestep,
                    "person",
                    {
                        "id": person_id,
                        "x": f"{x:.3f}",
                        "y": f"{y:.3f}",
                        "speed": f"{speed:.3f}",
                        "edge": road,
                    },
                )
            ET.indent(person_root, space="  ")
            ET.ElementTree(person_root).write(fcd_person_path, encoding="utf-8", xml_declaration=True)

    # feature-flagged: risk event 탐지 및 저장
    _risk_result: dict[str, Any] = {
        "risk_event_collection_enabled": bool(enable_risk_event_collection),
        "risk_event_count": 0,
        "senior_risk_event_count": 0,
        "risk_events_path": "",
        "pedestrian_frame_count": len(_risk_ped_frames),
        "vehicle_frame_count": len(_risk_veh_frames),
        "risk_event_collection_error": "",
    }
    if enable_risk_event_collection:
        try:
            try:
                from .safety.risk_event_detector import (  # noqa: PLC0415
                    RiskEventDetectorParams,
                    compute_pair_diagnostics,
                    detect_risk_events_from_frames,
                    write_risk_events_csv,
                )
            except ImportError:
                from smart_crosswalk_sumo.safety.risk_event_detector import (  # noqa: PLC0415
                    RiskEventDetectorParams,
                    compute_pair_diagnostics,
                    detect_risk_events_from_frames,
                    write_risk_events_csv,
                )
            import pandas as _pd  # noqa: PLC0415
            _risk_ped_df = _pd.DataFrame(_risk_ped_frames) if _risk_ped_frames else _pd.DataFrame()
            _risk_veh_df = _pd.DataFrame(_risk_veh_frames) if _risk_veh_frames else _pd.DataFrame()
            _pair_diag = compute_pair_diagnostics(_risk_ped_df, _risk_veh_df)
            _risk_result.update({
                "pair_diag_n_pairs_total": _pair_diag.get("n_pairs_total", 0),
                "pair_diag_n_within_15m": _pair_diag.get("n_pairs_within_15m", 0),
                "pair_diag_n_within_30m": _pair_diag.get("n_pairs_within_30m", 0),
                "pair_diag_min_distance_m": _pair_diag.get("min_distance_m"),
                "pair_diag_mean_distance_m": _pair_diag.get("mean_distance_m"),
                "pair_diag_n_time_steps": _pair_diag.get("n_time_steps_matched", 0),
            })
            _risk_events_df = detect_risk_events_from_frames(
                _risk_ped_df, _risk_veh_df, RiskEventDetectorParams()
            )
            _risk_result["risk_event_count"] = int(len(_risk_events_df))
            if "is_senior" in _risk_events_df.columns:
                _risk_result["senior_risk_event_count"] = int(_risk_events_df["is_senior"].sum())
            if output_dir:
                _risk_csv_path = Path(output_dir) / f"risk_events_{scenario}_seed{seed}.csv"
                write_risk_events_csv(_risk_events_df, _risk_csv_path)
                _risk_result["risk_events_path"] = str(_risk_csv_path)
                file_exports["risk_events_path"] = str(_risk_csv_path)
        except Exception as _exc:
            _risk_result["risk_event_collection_error"] = str(_exc)
    network_metrics.update(_risk_result)

    return (
        per_crosswalk_metrics,
        network_metrics,
        extension_events_log,
        incident_events_log,
        incident_impacts,
        file_exports,
    )


def compute_signal_timing(row: Any) -> dict[str, float]:
    ped_green = float(row["ped_green_base"] if isinstance(row, dict) else row["ped_green_base"])
    vehicle_green = (
        SIGNAL_PARAMS["cycle_time"]
        - SIGNAL_PARAMS["yellow_time"]
        - SIGNAL_PARAMS["all_red_time"] * 2
        - ped_green
    )
    return {
        "ped_green": float(ped_green),
        "vehicle_green": float(max(vehicle_green, 20.0)),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--net_file", required=True)
    parser.add_argument("--route_file", required=True)
    parser.add_argument("--ped_file", required=True)
    parser.add_argument("--sumocfg", required=True)
    parser.add_argument("--metadata", required=True)
    parser.add_argument("--scenario", choices=["baseline", "smart"], required=True)
    parser.add_argument("--crossing_length_m", type=float, required=True)
    parser.add_argument("--ped_green", type=float, required=True)
    parser.add_argument("--vehicle_green", type=float, required=True)
    parser.add_argument("--sim_duration", type=int, default=1800)
    parser.add_argument("--warmup", type=int, default=300)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--traci_step_length", type=float, default=0.1)
    parser.add_argument("--traffic_measure_radius_m", type=float, default=500.0)
    parser.add_argument("--extension_increment", type=float, default=SIGNAL_PARAMS["extension_increment"])
    parser.add_argument("--max_extensions", type=int, default=int(SIGNAL_PARAMS["max_extensions"]))
    parser.add_argument("--vehicle_arrival_rate_per_hour", type=float, default=None)
    parser.add_argument(
        "--saturation_flow_rate_per_hour",
        type=float,
        default=float(VEHICLE_MODEL_PARAMS["saturation_flow_rate_per_hour"]),
    )
    parser.add_argument("--vehicle_num_lanes", type=int, default=int(VEHICLE_MODEL_PARAMS["num_lanes"]))
    parser.add_argument("--vehicle_arrival_model", choices=["poisson", "bernoulli"], default=str(VEHICLE_MODEL_PARAMS["arrival_model"]))
    parser.add_argument(
        "--disruption_scenario",
        choices=["best_case", "normal_urban", "congested_urban", "incident_case"],
        default="best_case",
    )
    parser.add_argument("--enable_random_disruptions", action="store_true")
    parser.add_argument("--bus_stop_rate_per_hour", type=float, default=0.0)
    parser.add_argument("--illegal_parking_rate_per_hour", type=float, default=0.0)
    parser.add_argument("--minor_incident_rate_per_hour", type=float, default=0.0)
    parser.add_argument("--accident_rate_per_hour", type=float, default=0.0)
    parser.add_argument("--model_parameters", default=None)
    parser.add_argument("--output_dir", default=None)
    parser.add_argument("--export_fcd", action="store_true")
    parser.add_argument("--vehicle_only", action="store_true")
    parser.add_argument("--metric-sample-interval", type=float, default=0.0)
    parser.add_argument("--vehicle-sample-interval", type=float, default=0.0)
    parser.add_argument("--progress-interval", type=float, default=0.0)
    parser.add_argument("--sensitivity_scenarios", default=None)
    parser.add_argument("--sensitivity_case", default=None)
    args = parser.parse_args()

    sensitivity_config = None
    if args.sensitivity_case:
        sensitivity_definitions = load_sensitivity_scenarios(args.sensitivity_scenarios)
        sensitivity_config = resolve_sensitivity_case(sensitivity_definitions, args.sensitivity_case)

    metrics, _, _, _, _ = run_simulation(
        args.net_file,
        args.route_file,
        args.ped_file,
        args.sumocfg,
        args.scenario,
        {"ped_green": args.ped_green, "vehicle_green": args.vehicle_green},
        {"crossing_length_m": args.crossing_length_m},
        load_metadata(args.metadata),
        args.sim_duration,
        args.warmup,
        args.seed,
        args.traci_step_length,
        args.traffic_measure_radius_m,
        args.extension_increment,
        args.max_extensions,
        args.vehicle_arrival_rate_per_hour,
        args.saturation_flow_rate_per_hour,
        args.vehicle_num_lanes,
        args.vehicle_arrival_model,
        args.disruption_scenario,
        args.enable_random_disruptions,
        args.bus_stop_rate_per_hour,
        args.illegal_parking_rate_per_hour,
        args.minor_incident_rate_per_hour,
        args.accident_rate_per_hour,
        None,
        str(load_metadata(args.metadata).get("cw_id", "")),
        args.model_parameters,
        args.export_fcd,
        args.output_dir,
        args.vehicle_only,
        sensitivity_config,
        metric_sample_interval_s=args.metric_sample_interval,
        vehicle_sample_interval_s=args.vehicle_sample_interval,
        progress_interval_s=args.progress_interval,
    )
    for key, value in metrics.items():
        print(f"{key}={value}")


if __name__ == "__main__":
    main()
