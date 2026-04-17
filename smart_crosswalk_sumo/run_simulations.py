from __future__ import annotations

import argparse
import math
import os
import random
import shutil
from collections import deque
from pathlib import Path
from typing import Any

import numpy as np

try:
    import traci  # type: ignore
except ImportError:  # pragma: no cover - handled at runtime
    traci = None

try:
    from .network_utils import load_metadata, normalized_sumo_home, recent_values
except ImportError:
    from network_utils import load_metadata, normalized_sumo_home, recent_values


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


def sumo_binary() -> str:
    path = shutil.which("sumo")
    if not path:
        raise RuntimeError("sumo 실행 파일을 찾지 못했습니다. SUMO 설치와 PATH를 확인하세요.")
    return path


def compute_signal_timing(row: Any) -> dict[str, float]:
    ped_green = float(row["ped_green_base"] if isinstance(row, dict) else row["ped_green_base"])
    vehicle_green = SIGNAL_PARAMS["cycle_time"] - SIGNAL_PARAMS["yellow_time"] - SIGNAL_PARAMS["all_red_time"] * 2 - ped_green
    return {
        "ped_green": float(ped_green),
        "vehicle_green": float(max(vehicle_green, 20.0)),
    }


def phase_state(tl_id: str | None, phase_index: int) -> str:
    if not tl_id:
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


def tune_phase_duration(tl_id: str | None, phase_index: int, ped_link_indices: list[int], signal_timing: dict[str, float]) -> None:
    if not tl_id:
        return
    state = phase_state(tl_id, phase_index)
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
    except traci.TraCIException:
        return


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


def aggregate_metrics(
    pet_a: list[float],
    pet_b: list[float],
    elderly_inc: int,
    veh_waits: dict[str, float],
    queues: list[int],
) -> dict[str, float | int]:
    metrics: dict[str, float | int] = {}
    metrics.update(classify_pet(pet_a, "PET_A_proxy"))
    metrics.update(classify_pet(pet_b, "PET_B_surrogate"))

    veh_arr = np.asarray(list(veh_waits.values()), dtype=float)
    queue_arr = np.asarray(queues, dtype=float)
    metrics["elderly_incomplete_cross"] = int(elderly_inc)
    metrics["veh_avg_delay_sec"] = float(np.nanmean(veh_arr)) if veh_arr.size else 0.0
    metrics["veh_max_delay_sec"] = float(np.nanmax(veh_arr)) if veh_arr.size else 0.0
    metrics["queue_avg"] = float(np.nanmean(queue_arr)) if queue_arr.size else 0.0
    metrics["queue_max"] = int(np.nanmax(queue_arr)) if queue_arr.size else 0
    return metrics


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
) -> dict[str, float | int]:
    if traci is None:
        raise RuntimeError("traci가 설치되어 있지 않습니다.")

    random.seed(seed)
    metadata = metadata or load_metadata(Path(net_file).parent / "metadata.json")
    normalized_home = normalized_sumo_home()
    if normalized_home:
        os.environ["SUMO_HOME"] = normalized_home

    crossing_edge = metadata["crossing_edge"]
    vehicle_conflict_edges = set(metadata["vehicle_conflict_edges"])
    approach_lanes = list(metadata["approach_lanes"])
    tl_id = metadata.get("tls_id")
    ped_link_indices = [int(idx) for idx in metadata.get("ped_link_indices", [])]

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
        ]
    )

    vehicle_on_conflict_prev: set[str] = set()
    vehicle_exit_times: deque[float] = deque(maxlen=1000)
    ped_entered_crossing: set[str] = set()
    pet_a_records: list[float] = []
    pet_b_records: list[float] = []
    elderly_incomplete = 0
    veh_waits: dict[str, float] = {}
    queue_lengths: list[int] = []
    extension_count = 0
    prev_phase: int | None = None
    prev_phase_was_ped_green = False
    end_time = warmup + sim_duration

    try:
        while traci.simulation.getTime() < end_time and traci.simulation.getMinExpectedNumber() > 0:
            traci.simulationStep()
            t = float(traci.simulation.getTime())
            collect = t > warmup

            current_phase = traci.trafficlight.getPhase(tl_id) if tl_id else -1
            state = phase_state(tl_id, current_phase) if tl_id else ""
            current_phase_is_ped_green = is_ped_green_state(state, ped_link_indices)
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
                        pet_b_records.append(SIGNAL_PARAMS["all_red_time"] - time_to_clear)
                        if "elderly" in traci.person.getTypeID(ped_id):
                            elderly_incomplete += 1
                    except traci.TraCIException:
                        continue

            if phase_changed:
                tune_phase_duration(tl_id, current_phase, ped_link_indices, signal_timing)
                if current_phase_is_ped_green:
                    extension_count = 0
                prev_phase = current_phase

            if scenario == "smart" and tl_id and current_phase_is_ped_green:
                next_switch = float(traci.trafficlight.getNextSwitch(tl_id))
                remaining = next_switch - t
                if (
                    remaining <= SIGNAL_PARAMS["trigger_remaining"]
                    and extension_count < SIGNAL_PARAMS["max_extensions"]
                ):
                    peds_on = traci.edge.getLastStepPersonIDs(crossing_edge)
                    if peds_on and random.random() > SIGNAL_PARAMS["sensor_fn_rate"]:
                        traci.trafficlight.setPhaseDuration(
                            tl_id,
                            remaining + SIGNAL_PARAMS["extension_increment"],
                        )
                        extension_count += 1

            if collect:
                current_vehicle_conflict = set()
                for veh_id in traci.vehicle.getIDList():
                    try:
                        if traci.vehicle.getRoadID(veh_id) in vehicle_conflict_edges:
                            current_vehicle_conflict.add(veh_id)
                        veh_waits[veh_id] = float(traci.vehicle.getAccumulatedWaitingTime(veh_id))
                    except traci.TraCIException:
                        continue

                for veh_id in vehicle_on_conflict_prev - current_vehicle_conflict:
                    vehicle_exit_times.append(t)
                vehicle_on_conflict_prev = current_vehicle_conflict

                peds_on_crossing = set(traci.edge.getLastStepPersonIDs(crossing_edge))
                for ped_id in peds_on_crossing - ped_entered_crossing:
                    candidates = [vt for vt in recent_values(vehicle_exit_times, t, 30.0) if vt <= t]
                    if candidates:
                        pet_a_records.append(t - max(candidates))
                ped_entered_crossing |= peds_on_crossing

                for lane_id in approach_lanes:
                    try:
                        queue_lengths.append(int(traci.lane.getLastStepHaltingNumber(lane_id)))
                    except traci.TraCIException:
                        continue

            prev_phase_was_ped_green = current_phase_is_ped_green
    finally:
        traci.close(False)

    return aggregate_metrics(pet_a_records, pet_b_records, elderly_incomplete, veh_waits, queue_lengths)


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
    args = parser.parse_args()
    metrics = run_simulation(
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
    )
    for key, value in metrics.items():
        print(f"{key}={value}")


if __name__ == "__main__":
    main()
