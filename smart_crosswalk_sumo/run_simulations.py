from __future__ import annotations

import argparse
import json
import math
import os
import random
import shutil
import socket
import xml.etree.ElementTree as ET
from collections import Counter, deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

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
        read_net,
        recent_values,
    )
    from sensitivity import apply_parameter_value_overrides, load_sensitivity_scenarios, resolve_sensitivity_case


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
        rate_specs = [
            ("bus_stop", bus_stop_rate_per_hour),
            ("accident", accident_rate_per_hour),
        ]
        duration_by_type = {
            "bus_stop": 35.0,
            "accident": 240.0,
        }
        sim_hours = max(float(sim_duration) / 3600.0, 0.0)
        for event_type, rate in rate_specs:
            count = int(rng.poisson(max(rate, 0.0) * sim_hours))
            for _ in range(count):
                duration = duration_by_type[event_type]
                start = float(rng.uniform(0.0, max(float(sim_duration) - duration, 0.0)))
                if start >= float(sim_duration):
                    continue
                end = min(float(sim_duration), start + duration)
                if end <= start:
                    continue
                lanes = choose_incident_lanes(event_type, monitored_lanes, approach_lanes)
                if not lanes:
                    continue
                event = IncidentEvent(
                    incident_id=f"inc_{seed}_{idx}",
                    event_type=event_type,
                    start_time=start,
                    end_time=end,
                    affected_edge_ids=tuple(sorted(set(conflict_edges))),
                    affected_lane_ids=tuple(sorted(set(lanes))),
                    severity=0.6,
                    capacity_multiplier=clip_capacity_multiplier(float(cap_map.get(event_type, 0.6))),
                    speed_multiplier=clip_capacity_multiplier(float(speed_map.get(event_type, 0.7))),
                    blocked_lanes_count=1 if event_type == "accident" else 0,
                    allow_rerouting=event_type == "accident",
                )
                events.append(event)
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

    crossing_edge = metadata["crossing_edge"]
    vehicle_conflict_edges = set(metadata["vehicle_conflict_edges"])
    approach_lanes = list(metadata["approach_lanes"])
    tl_id = metadata.get("tls_id")
    ped_link_indices = [int(idx) for idx in metadata.get("ped_link_indices", [])]

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
                if (
                    remaining <= signal_params["trigger_remaining"]
                    and extension_count_in_cycle < signal_params["max_extensions"]
                    and not extended_in_cycle
                ):
                    ped_on_crossing = set(traci.edge.getLastStepPersonIDs(crossing_edge))
                    ped_on_detectors = set()
                    for edge_id in ped_detector_edges:
                        try:
                            ped_on_detectors.update(traci.edge.getLastStepPersonIDs(edge_id))
                        except Exception:
                            continue
                    detected_peds = ped_on_crossing | ped_on_detectors
                    if detected_peds and random.random() > signal_params["sensor_fn_rate"]:
                        try:
                            traci.trafficlight.setPhaseDuration(
                                tl_id_known,
                                remaining + signal_params["extension_increment"],
                            )
                        except Exception:
                            tls_refresh_countdown = 0
                            continue
                        extension_count_in_cycle += 1
                        extended_in_cycle = True
                        total_extension_count += 1
                        pedestrian_green_extension_time += signal_params["extension_increment"]
                        extension_events_log.append(
                            {
                                "sim_time": t,
                                "crosswalk_id": str(crosswalk_id),
                                "tls_id": str(tl_id_known),
                                "phase_index": int(current_phase),
                                "remaining_before_extension": float(remaining),
                                "extension_sec": float(signal_params["extension_increment"]),
                                "ped_count_on_crossing": int(len(ped_on_crossing)),
                                "seed": int(seed),
                                "scenario": scenario,
                            }
                        )

            if collect:
                measure_steps += 1
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

                current_vehicle_conflict = set()
                current_area_vehicles: set[str] = set()
                all_vehicle_speeds: list[float] = []
                for veh_id in current_vehicle_ids:
                    try:
                        road_id = traci.vehicle.getRoadID(veh_id)
                        if road_id in vehicle_conflict_edges:
                            current_vehicle_conflict.add(veh_id)
                        accumulated_wait = float(traci.vehicle.getAccumulatedWaitingTime(veh_id))
                        veh_waits[veh_id] = accumulated_wait
                        lane_id = traci.vehicle.getLaneID(veh_id)
                        all_vehicle_speeds.append(float(traci.vehicle.getSpeed(veh_id)))
                        if lane_id in monitored_lanes:
                            current_area_vehicles.add(veh_id)
                            area_seen_vehicles.add(veh_id)
                            area_vehicle_waits[veh_id] = accumulated_wait
                            if veh_id not in area_active_since:
                                area_active_since[veh_id] = t
                                area_entry_count += 1
                    except Exception:
                        continue

                for veh_id in list(area_active_since):
                    if veh_id not in current_area_vehicles:
                        area_travel_times.append(max(0.0, t - area_active_since.pop(veh_id)))
                        area_exit_count += 1
                area_vehicle_time_sec += len(current_area_vehicles) * traci_step_length

                for veh_id in vehicle_on_conflict_prev - current_vehicle_conflict:
                    vehicle_exit_times.append(t)
                vehicle_on_conflict_prev = current_vehicle_conflict

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
                for lane_id in monitored_lanes:
                    try:
                        halted = int(traci.lane.getLastStepHaltingNumber(lane_id))
                        lane_halting[lane_id] = halted
                        lane_occupancy[lane_id] = float(traci.lane.getLastStepOccupancy(lane_id))
                        lane_speeds[lane_id] = float(traci.lane.getLastStepMeanSpeed(lane_id))
                        surrounding_queue_total += halted
                        lane_data_records.append((rel_t, lane_id, halted, lane_speeds[lane_id]))
                    except Exception:
                        continue

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

                surrounding_queue_totals.append(surrounding_queue_total)
                avg_wait = float(np.nanmean(list(veh_waits.values()))) if veh_waits else 0.0
                avg_speed = float(np.nanmean(all_vehicle_speeds)) if all_vehicle_speeds else 0.0
                ts_queue.append((rel_t, float(surrounding_queue_total)))
                ts_wait.append((rel_t, avg_wait))
                ts_speed.append((rel_t, avg_speed))
                surrounding_speed_samples.append(avg_speed)

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

                for lane_id in approach_lanes:
                    try:
                        queue_lengths.append(int(traci.lane.getLastStepHaltingNumber(lane_id)))
                    except Exception:
                        continue

                if export_fcd:
                    for veh_id in current_vehicle_ids:
                        try:
                            x, y = traci.vehicle.getPosition(veh_id)
                            fcd_vehicle_records.append(
                                (
                                    rel_t,
                                    veh_id,
                                    float(x),
                                    float(y),
                                    float(traci.vehicle.getSpeed(veh_id)),
                                    traci.vehicle.getRoadID(veh_id),
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

            prev_phase_was_ped_green = current_phase_is_ped_green
            prev_vehicle_green = current_phase_is_vehicle_green
    finally:
        for event in incident_events:
            if event.incident_id in active_event_ids:
                restore_incident(event, lane_original_states)
        try:
            traci.switch(traci_label)
            traci.close(False)
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
            measure_steps,
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
    vehicle_only: bool = False,
    sensitivity_config: dict[str, Any] | None = None,
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
        group["ped_link_indices_union"].update(int(idx) for idx in context.get("ped_link_indices", []))
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
    teleported_vehicle_count = 0
    known_tls_ids: set[str] = set()
    tls_refresh_countdown = 0

    fcd_vehicle_records: list[tuple[float, str, float, float, float, str]] = []
    fcd_person_records: list[tuple[float, str, float, float, float, str]] = []
    lane_data_records: list[tuple[float, str, int, float]] = []
    edge_data_records: list[tuple[float, str, int, float]] = []

    try:
        for state in context_states:
            adjacent_lanes, adjacent_tls_count = adjacent_tls_lanes(
                state["metadata"].get("tls_id"),
                set(state["scope"]["monitored_lanes"]),
            )
            state["adjacent_lanes"] = adjacent_lanes
            state["adjacent_tls_count"] = adjacent_tls_count

        end_time = warmup + sim_duration
        while traci.simulation.getTime() < end_time:
            traci.simulationStep()
            t = float(traci.simulation.getTime())
            rel_t = max(0.0, t - warmup)
            collect = t > warmup
            if tls_refresh_countdown <= 0:
                known_tls_ids = traci_trafficlight_ids()
                tls_refresh_countdown = 10
            else:
                tls_refresh_countdown -= 1

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

            vehicle_info: dict[str, dict[str, Any]] = {}
            all_vehicle_speeds: list[float] = []
            for veh_id in current_vehicle_ids:
                try:
                    lane_id = traci.vehicle.getLaneID(veh_id)
                    road_id = traci.vehicle.getRoadID(veh_id)
                    speed = float(traci.vehicle.getSpeed(veh_id))
                    accumulated_wait = float(traci.vehicle.getAccumulatedWaitingTime(veh_id))
                    vehicle_info[veh_id] = {
                        "lane_id": lane_id,
                        "road_id": road_id,
                        "speed": speed,
                        "accumulated_wait": accumulated_wait,
                    }
                    all_vehicle_speeds.append(speed)
                except Exception:
                    continue

            if collect:
                avg_wait = float(
                    np.nanmean([info["accumulated_wait"] for info in vehicle_info.values()])
                ) if vehicle_info else 0.0
                network_delay_step_means.append(avg_wait)
                network_queue_counts.append(
                    int(sum(1 for info in vehicle_info.values() if float(info["speed"]) < 0.1))
                )
                avg_speed = float(np.nanmean(all_vehicle_speeds)) if all_vehicle_speeds else 0.0
                network_speed_means.append(avg_speed)
                ts_queue.append((rel_t, float(network_queue_counts[-1])))
                ts_wait.append((rel_t, avg_wait))
                ts_speed.append((rel_t, avg_speed))

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
            if collect:
                for lane_id in union_lane_ids:
                    try:
                        halted = int(traci.lane.getLastStepHaltingNumber(lane_id))
                        lane_halting[lane_id] = halted
                        lane_occupancy[lane_id] = float(traci.lane.getLastStepOccupancy(lane_id))
                        lane_speeds[lane_id] = float(traci.lane.getLastStepMeanSpeed(lane_id))
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

            for group in group_defs.values():
                raw_tl_id = group["tl_id"] or None
                tl_id = raw_tl_id if is_known_trafficlight(raw_tl_id, known_tls_ids) else None
                try:
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
                        for ped_id in traci.edge.getLastStepPersonIDs(crossing_edge):
                            try:
                                pos = float(traci.person.getLanePosition(ped_id))
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
                    and current_phase_is_ped_green
                    and group["target_context_ids"]
                ):
                    try:
                        next_switch = float(traci.trafficlight.getNextSwitch(tl_id))
                    except Exception:
                        tls_refresh_countdown = 0
                        continue
                    remaining = next_switch - t
                    if (
                        remaining <= signal_params["trigger_remaining"]
                        and group["extension_count_in_cycle"] < signal_params["max_extensions"]
                        and not group["extended_in_cycle"]
                    ):
                        triggered_context_ids: list[str] = []
                        ped_on_crossing_total = 0
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
                            if detected_peds:
                                triggered_context_ids.append(crosswalk_id)
                                ped_on_crossing_total += len(ped_on_crossing)
                        if triggered_context_ids and random.random() > signal_params["sensor_fn_rate"]:
                            try:
                                traci.trafficlight.setPhaseDuration(
                                    tl_id,
                                    remaining + signal_params["extension_increment"],
                                )
                            except Exception:
                                tls_refresh_countdown = 0
                                continue
                            group["extension_count_in_cycle"] += 1
                            group["extended_in_cycle"] = True
                            group["total_extension_count"] += 1
                            group["pedestrian_green_extension_time"] += signal_params["extension_increment"]
                            extension_events_log.append(
                                {
                                    "sim_time": t,
                                    "crosswalk_id": "|".join(triggered_context_ids),
                                    "crosswalk_ids": json.dumps(triggered_context_ids, ensure_ascii=False),
                                    "tls_id": str(tl_id),
                                    "phase_index": int(current_phase),
                                    "remaining_before_extension": float(remaining),
                                    "extension_sec": float(signal_params["extension_increment"]),
                                    "ped_count_on_crossing": int(ped_on_crossing_total),
                                    "seed": int(seed),
                                    "scenario": scenario,
                                }
                            )
                            for context_state in context_states:
                                if context_state["crosswalk_id"] in triggered_context_ids:
                                    context_state["local_extension_count"] += 1
                                    context_state["local_extension_time"] += signal_params["extension_increment"]

                group["prev_phase_was_ped_green"] = current_phase_is_ped_green
                group["prev_vehicle_green"] = current_phase_is_vehicle_green

            if collect:
                any_spillback_now = False
                current_person_ids = set(traci.person.getIDList())
                if export_fcd:
                    for veh_id, info in vehicle_info.items():
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
                    current_vehicle_conflict = {
                        veh_id
                        for veh_id, info in vehicle_info.items()
                        if str(info["road_id"]) in set(str(edge) for edge in metadata.get("vehicle_conflict_edges", []))
                    }
                    for veh_id in context_state["vehicle_on_conflict_prev"] - current_vehicle_conflict:
                        context_state["vehicle_exit_times"].append(t)
                    context_state["vehicle_on_conflict_prev"] = current_vehicle_conflict

                    peds_on_crossing = set(traci.edge.getLastStepPersonIDs(crossing_edge))
                    detector_peds = set()
                    for edge_id in context_state["ped_detector_edges"]:
                        try:
                            detector_peds.update(traci.edge.getLastStepPersonIDs(edge_id))
                        except Exception:
                            continue
                    observed_peds = peds_on_crossing | detector_peds
                    for ped_id in observed_peds:
                        context_state["context_person_first_seen"].setdefault(ped_id, t)
                        context_state["all_seen_pedestrians"].add(ped_id)
                        try:
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

                    current_area_vehicles = set()
                    area_wait_sum = 0.0
                    for veh_id, info in vehicle_info.items():
                        lane_id = str(info["lane_id"])
                        if lane_id in set(context_state["scope"]["monitored_lanes"]):
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

                    surrounding_queue_total = sum(
                        lane_halting.get(lane_id, 0)
                        for lane_id in context_state["scope"]["monitored_lanes"]
                    )
                    context_state["surrounding_queue_totals"].append(surrounding_queue_total)
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
                    for lane_id in metadata.get("approach_lanes", []):
                        context_state["queue_lengths"].append(lane_halting.get(str(lane_id), 0))
                if any_spillback_now:
                    network_spillback_steps += 1
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
                context_state["measure_steps"],
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
        "network_avg_travel_time_sec": float(np.nanmean(network_travel_times)) if network_travel_times else 0.0,
        "network_avg_speed_mps": float(np.nanmean(network_speed_means)) if network_speed_means else 0.0,
        "network_teleported_vehicles": int(teleported_vehicle_count),
        "network_spillback_rate": float(network_spillback_steps / max(network_measure_steps, 1)),
        "network_spillback_step_count": int(network_spillback_steps),
        "selected_crosswalk_count": int(len(crosswalk_contexts)),
    }

    file_exports: dict[str, Any] = {}
    if output_dir:
        out_dir = Path(output_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
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
    )
    for key, value in metrics.items():
        print(f"{key}={value}")


if __name__ == "__main__":
    main()
