#!/usr/bin/env python3
"""
run_enhanced_smoke_group.py  —  Enhanced smoke pipeline.

Changes vs run_sampled10_group:
  • --ped-count N          : pedestrian trips per crosswalk (default 20, not hardcoded 5)
  • --elderly-ratio R      : fraction of elderly peds (default 0.2, walk 0.8 m/s)
  • two-phase extension    : cycle-0 guaranteed (phase-aligned), cycle-1+ natural only
  • avg/max queue length   : measured per step, populated in output
  • global network flow    : travel time + time loss via traci (no tripinfo file needed)
  • --seed -1              : pick a random seed
  • 4-sheet Excel output   : comparison / validation / crosswalk-info / raw-measurements

Usage — random-1:
  python -m smart_crosswalk_sumo.run_enhanced_smoke_group \\
      --candidate-csv result/.../manifests/current_main_12_candidates.csv \\
      --net-file result/.../nets/current_main_12.net.xml \\
      --seed -1 --output-dir outputs/enhanced/quick

Usage — full:
  python -m smart_crosswalk_sumo.run_enhanced_smoke_group \\
      --candidate-csv ... --net-file ... \\
      --seeds 1 2 3 --output-dir outputs/enhanced/full
"""

from __future__ import annotations

import argparse
import json
import random
import socket
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

try:
    import traci  # type: ignore
except ImportError:
    traci = None  # type: ignore

from smart_crosswalk_sumo.run_phase6_recovery_smoke import (
    _build_local_scope,
    _candidate_crossing_roads,
    _network_passenger_edge_ids,
    _passenger_lane_count_for_route,
    _phase_aligned_depart_plan,
    _route_match_details,
    _sumo_binary,
    _summarize_local_watcher_pet,
    _vehicle_route_diversity_metrics,
    _write_routes,
    _write_sumocfg,
    read_net,
)
from smart_crosswalk_sumo.generate_demand import (
    _enforce_exact_vehicle_count,
    random_trips_script,
)
from smart_crosswalk_sumo.network_utils import sumo_env
from smart_crosswalk_sumo.run_sampled10_group import (
    _ensure_global_vehicle_routes,
)

# ── constants ─────────────────────────────────────────────────────────────────
ELDERLY_MAX_SPEED   = 0.8    # m/s
NORMAL_MAX_SPEED    = 1.2    # m/s
EXTENSION_WINDOW    = 12.0   # seconds before phase end → extension eligible
SIM_DURATION        = 120    # smoke-level seconds
EXTENSION_INCREMENT = 5.0    # seconds added per extension
VTYPE_ELDERLY       = "enh_ped_elderly"
VTYPE_NORMAL        = "enh_ped_normal"

# ── lane-scaled vehicle demand ─────────────────────────────────────────────────
# population mean lanes across crosswalk_stepwise_result_50m.csv (n=1089)
_REFERENCE_LANES    = 2.105
# base vehicles for 120 s at main_realistic_stress policy (3480 / 600 * 120)
_BASE_VEH_120S      = 696
_STEPWISE_CSV       = Path(__file__).resolve().parent.parent / "crosswalk_stepwise_result_50m.csv"

# person_id 형식: ped_{idx}_{rep_idx}_{crosswalk_id}
import re as _re
_PED_ID_RE = _re.compile(r'^ped_\d+_\d+_(.+)$')

# ─────────────────────────────────────────────────────────────────────────────
# 0. 보행자 수요 스케일링 (pedestrian.csv source → demand_scenarios.py 매핑)
# ─────────────────────────────────────────────────────────────────────────────
def _compute_scaled_ped_counts(
    candidate_df: "pd.DataFrame",
    base_ped_count: int,
    mode: str = "none",
    min_target: int = 30,
    max_cap: int = 200,
    source_path: "str | None" = None,
) -> "tuple[dict[str, int], float]":
    """crosswalk별 scaled ped count + multiplier 반환.

    Returns ({crosswalk_id: scaled_count}, multiplier).
    mode='none' → flat base_ped_count, multiplier=1.0.
    mode='source_min_to_target' → pedestrian.csv(demand_scenarios.py) 기반 비례 스케일.
    """
    cids = candidate_df["crosswalk_id"].astype(str).tolist()
    if mode == "none":
        return {cid: base_ped_count for cid in cids}, 1.0

    from smart_crosswalk_sumo.demand_scenarios import (
        MAIN_REALISTIC_STRESS_PEDESTRIAN_COUNT_600S_BY_ADMIN_DONG as _PED_MAP,
        MAIN_REALISTIC_STRESS_ELDERLY_RATIO_BY_ADMIN_DONG as _ER_MAP,
    )
    ratio_to_dong = {round(v, 4): k for k, v in _ER_MAP.items()}

    raw: dict[str, float] = {}
    for row in candidate_df.itertuples(index=False):
        cid = str(row.crosswalk_id)
        ratio = round(float(
            getattr(row, "crosswalk_elderly_ratio", None)
            or getattr(row, "elderly_ratio", 0.0)
            or 0.0
        ), 4)
        dong = ratio_to_dong.get(ratio)
        raw[cid] = float(_PED_MAP.get(dong, base_ped_count)) if dong else float(base_ped_count)

    min_val = min(raw.values()) if raw else float(base_ped_count)
    multiplier = max(1.0, float(min_target) / min_val) if min_val > 0 else 1.0
    scaled = {
        cid: min(int(round(v * multiplier)), max_cap)
        for cid, v in raw.items()
    }
    print(
        f"[enhanced smoke] ped demand scale: multiplier={multiplier:.4f} "
        f"(min_target={min_target}/min_ped_600s={min_val}), "
        f"source={source_path or 'demand_scenarios.py'}",
        flush=True,
    )
    return scaled, multiplier


# ─────────────────────────────────────────────────────────────────────────────
# 1. Elderly pedestrian injection (per-crosswalk ratio)
# ─────────────────────────────────────────────────────────────────────────────
def _inject_elderly_types(
    ped_xml: Path,
    elderly_ratio: "float | dict[str, float]",
    seed: int,
) -> None:
    """Post-process ped route XML: assign normal/elderly vTypes per crosswalk ratio.

    elderly_ratio can be a float (uniform) or dict {crosswalk_id: ratio}.
    """
    if not ped_xml.exists():
        return
    tree = ET.parse(ped_xml)
    root = tree.getroot()

    for vt in list(root.findall("vType")):
        root.remove(vt)

    vt_elderly = ET.Element("vType", {
        "id": VTYPE_ELDERLY, "vClass": "pedestrian",
        "maxSpeed": str(ELDERLY_MAX_SPEED), "accel": "0.3", "decel": "0.3",
    })
    vt_normal = ET.Element("vType", {
        "id": VTYPE_NORMAL, "vClass": "pedestrian",
        "maxSpeed": str(NORMAL_MAX_SPEED), "accel": "0.5", "decel": "0.5",
    })
    root.insert(0, vt_elderly)
    root.insert(0, vt_normal)

    persons = root.findall("person")
    is_uniform = isinstance(elderly_ratio, float)

    if is_uniform:
        rng = random.Random(seed + 9999)
        n_elderly = max(0, round(len(persons) * elderly_ratio))
        elderly_idx = set(rng.sample(range(len(persons)), min(n_elderly, len(persons))))
        for i, p in enumerate(persons):
            p.set("type", VTYPE_ELDERLY if i in elderly_idx else VTYPE_NORMAL)
    else:
        # per-crosswalk: group persons by crosswalk_id, apply ratio independently
        from collections import defaultdict
        cw_persons: dict[str, list[int]] = defaultdict(list)
        for i, p in enumerate(persons):
            pid = p.get("id", "")
            m = _PED_ID_RE.match(pid)
            cid = m.group(1) if m else ""
            cw_persons[cid].append(i)

        elderly_set: set[int] = set()
        for cid, idxs in cw_persons.items():
            ratio = elderly_ratio.get(cid, 0.15)
            if ratio <= 0:
                continue
            rng = random.Random(seed + hash(cid) % 100_000)
            n = max(0, round(len(idxs) * ratio))
            elderly_set.update(rng.sample(idxs, min(n, len(idxs))))

        for i, p in enumerate(persons):
            p.set("type", VTYPE_ELDERLY if i in elderly_set else VTYPE_NORMAL)

    tree.write(str(ped_xml), encoding="unicode", xml_declaration=True)


# ─────────────────────────────────────────────────────────────────────────────
# 2. Two-phase extension policy
# ─────────────────────────────────────────────────────────────────────────────
def _should_extend(
    scenario: str,
    tls_id: str,
    state: str,
    ped_link_index: int,
    is_ped_only_phase: bool,
    remaining: float,
    ped_near: int,
    extended_this_cycle: set[tuple],  # (tls_id, phase, cid)
    phase: int,
    cid: str,
) -> str:
    """Return '' if extension should fire, else reason string.

    Policy (기존 smoke와 동일):
      보행자 신호 초록 + 보행자 전용 단계 + 12초 이내 + 보행자 존재 + 사이클당 1회
    """
    if scenario == "baseline":
        return "scenario_baseline"
    if not tls_id:
        return "missing_tls"
    if ped_link_index < 0 or ped_link_index >= len(state):
        return "missing_ped_link_index"
    ped_link_state = state[ped_link_index]
    if ped_link_state not in {"G", "g"}:
        return "ped_link_not_green"
    if not is_ped_only_phase:
        return "not_pedestrian_only_phase"
    if remaining > EXTENSION_WINDOW:
        return "outside_extension_window"
    if ped_near <= 0:
        return "no_ped_near"
    cycle_key = (tls_id, phase, cid)
    if cycle_key in extended_this_cycle:
        return "already_extended_this_phase"
    return ""


# ─────────────────────────────────────────────────────────────────────────────
# 3. Enhanced scenario runner (modified step loop)
# ─────────────────────────────────────────────────────────────────────────────
def _write_minimal_sumocfg(
    cfg_path: Path,
    net_file: Path,
    ped_file: Path,
    veh_file: "Path | None",
    duration: int,
    step_length: float,
    warmup_sec: int = 0,
) -> Path:
    """Minimal sumocfg — no XML output files (tripinfo/statistics/collisions)."""
    # resolve to absolute so SUMO finds files regardless of sumocfg location
    net_abs = str(Path(net_file).resolve())
    route_files = [str(Path(ped_file).resolve())]
    if veh_file is not None:
        route_files.append(str(Path(veh_file).resolve()))
    content = f"""<?xml version="1.0" encoding="UTF-8"?>
<configuration>
  <input>
    <net-file value="{net_abs}"/>
    <route-files value="{','.join(route_files)}"/>
  </input>
  <time>
    <begin value="0"/>
    <end value="{duration + warmup_sec}"/>
    <step-length value="{step_length}"/>
  </time>
  <processing>
    <collision.action value="warn"/>
    <intermodal-collision.action value="warn"/>
    <time-to-teleport value="-1"/>
  </processing>
  <report>
    <no-step-log value="true"/>
    <no-warnings value="false"/>
  </report>
</configuration>
"""
    cfg_path.write_text(content, encoding="utf-8")
    return cfg_path


def _run_enhanced_scenario(
    candidate_df: pd.DataFrame,
    net_file: Path,
    scenario: str,
    seed: int,
    duration: int,
    step_length: float,
    out_dir: Path,          # simulation_result.csv + run_metadata.json saved here
    sim_tmp_dir: Path,      # sumocfg + demand XMLs written here (no persistence)
    include_vehicles: bool,
    extension_sec: float,
    elderly_ratio: "float | dict[str, float]",
    global_veh_file: "Path | None",
    group_name: str = "",
    warmup_sec: int = 0,
    run_profile: str = "custom",
) -> pd.DataFrame:
    if traci is None:
        raise RuntimeError("traci not importable — install SUMO and set SUMO_HOME")

    out_dir.mkdir(parents=True, exist_ok=True)
    sim_tmp_dir.mkdir(parents=True, exist_ok=True)
    t_start = time.time()
    run_start_time = datetime.now(timezone.utc).isoformat()

    # write demand XMLs to tmp dir only
    ped_file, veh_file, _, ped_records, _ = _write_routes(
        candidate_df, sim_tmp_dir, seed, duration, include_vehicles,
        global_vehicle_file=global_veh_file,
        write_debug=False,
    )
    if ped_file and ped_file.exists():
        _inject_elderly_types(ped_file, elderly_ratio, seed)

    # count generated vehicles
    generated_vehicle_count = 0
    if veh_file and veh_file.exists():
        try:
            vr = ET.parse(veh_file).getroot()
            generated_vehicle_count = sum(1 for _ in vr.iter("vehicle"))
        except Exception:
            pass

    cfg_path = _write_minimal_sumocfg(
        sim_tmp_dir / f"enhanced_{scenario}.sumocfg",
        net_file, ped_file, veh_file, duration, step_length,
        warmup_sec=warmup_sec,
    )

    cmd = [_sumo_binary(), "-c", str(cfg_path), "--no-step-log",
           "--collision.action", "warn", "--time-to-teleport", "-1"]
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()

    net = read_net(net_file)
    all_net_edges = _network_passenger_edge_ids(net)

    try:
        traci.start(cmd, port=port)
    except Exception as _traci_err:
        import subprocess as _sp
        _probe = _sp.run(cmd, capture_output=True, text=True, timeout=15)
        raise RuntimeError(
            f"SUMO failed to start (TraCI: {_traci_err}).\n"
            f"SUMO stdout: {_probe.stdout.strip()}\n"
            f"SUMO stderr: {_probe.stderr.strip()}"
        ) from _traci_err

    # valid lane set for queue measurement (vehicle lanes; internal junctions excluded from getIDList)
    _valid_lanes = set(traci.lane.getIDList())
    step_count = 0

    cids = candidate_df["crosswalk_id"].astype(str).tolist()
    # _person_routes_crossing NOT called — enhanced smoke uses watch dict directly

    # per-crosswalk watch structures
    watch: dict[str, dict[str, Any]] = {}
    for row in candidate_df.itertuples(index=False):
        cid = str(row.crosswalk_id)
        scope = _build_local_scope(net, str(row.crossing_edge_id), 500.0)
        watch[cid] = {
            "tls_id":          str(getattr(row, "tls_id", "") or getattr(row, "tls_id_used", "")),
            "ped_link_index":  int(getattr(row, "ped_link_index", 0) or 0),
            "crossing_edge_id": str(row.crossing_edge_id),
            "route_from_edge":  str(getattr(row, "route_from_edge", "")),
            "route_to_edge":    str(getattr(row, "route_to_edge", "")),
            # crossing_edges: NOT filtered by getIDList — internal junction edges are queryable
            # but not listed; try/except in step loop handles invalid ones
            "crossing_edges":       _candidate_crossing_roads(str(row.nearest_junction_id), str(row.crossing_edge_id)),
            "route_path":           set(str(getattr(row, "generated_route_edges", "") or "").split("|")) - {""},
            "local_lane_ids":       set(scope["local_lane_ids"]) & _valid_lanes,
            "local_edge_ids":       set(scope["local_edge_ids"]),
            "conflict_edge_ids":    set(scope["conflict_edge_ids"]),
            "conflict_edge_aliases": set(scope["conflict_edge_aliases"]),
            # pedestrian accumulators
            "ped_wait_samples":   [],
            "ped_presence_steps": 0,
            "ped_seen":           set(),
            "elderly_incomplete": 0,
            # PET interval tracking (local_watcher 방식, SSM 없이)
            "active_ped_entries": {},   # pid → enter_time
            "ped_intervals":      [],   # {enter_time, exit_time}
            "active_veh_entries": {},   # vid → enter_time (conflict zone)
            "veh_intervals":      [],   # {enter_time, exit_time}
            # queue
            "queue_samples":      [],
            # local 500m vehicle accumulators (matching existing smoke)
            "local_speed_samples":     [],
            "local_time_loss_samples": [],
            "local_delay_samples":     [],
            "local_seen_veh_ids":      set(),
            "local_stop_count":        0,
            "prev_veh_stop_state":     {},
            "veh_delay_samples":       [],
            # per-step trace (전 step 기록, post-sim 필터링)
            "tls_trace_rows":  [],
            "lane_trace_rows": [],
            "ped_trace_rows":  [],
        }

    # ── run-level state ───────────────────────────────────────────────────────
    extension_events: list[dict] = []
    extended_this_cycle: set[tuple] = set()   # (tls_id, phase, cid) — 사이클당 1회
    tls_cycle:      dict[str, int] = {}
    tls_last_phase: dict[str, int] = {}

    # global flow
    veh_depart_t:    dict[str, float] = {}
    veh_travel_times: list[float] = []
    veh_time_losses:  list[float] = []
    arrived_ids:  set[str] = set()
    departed_ids: set[str] = set()
    seen_edges:   set[str] = set()
    teleport_count = 0
    collision_count = 0
    # cache: last known accumulated waiting time per vehicle (populated in active loop)
    last_veh_waiting: dict[str, float] = {}

    # surrounding lane count (topology, computed once)
    surrounding_lane_count = 0
    try:
        route_based = int(sum(
            _passenger_lane_count_for_route(net, str(getattr(r, "vehicle_route_edges", "") or ""))
            for r in candidate_df.itertuples(index=False)
            if str(getattr(r, "vehicle_generation_action", "") or "") == "generate_vehicle"
        ))
        if route_based > 0:
            surrounding_lane_count = route_based
        else:
            # vehicle_route_edges 없을 때 topology fallback — unique lane 합산
            surrounding_lane_count = len(set().union(*(w["local_lane_ids"] for w in watch.values())))
    except Exception:
        surrounding_lane_count = len(set().union(*(w["local_lane_ids"] for w in watch.values()))) if watch else 0

    vehicle_route_diversity = _vehicle_route_diversity_metrics(veh_file if include_vehicles else None)

    # ── step loop ─────────────────────────────────────────────────────────────
    try:
        while True:
            traci.simulationStep()
            t = float(traci.simulation.getTime())
            if t >= float(duration + warmup_sec):  # SUMO end = sim_duration + warmup_sec
                break
            if t < warmup_sec:  # warmup 구간: 차량 pre-loading만, 지표 수집 skip
                continue

            step_count += 1

            # all person IDs this step — matches existing smoke approach
            # (avoids traci.edge.getLastStepPersonIDs on internal junction edges)
            try:
                all_ped_ids = list(traci.person.getIDList())
            except Exception:
                all_ped_ids = []

            # global vehicle tracking
            for vid in traci.simulation.getDepartedIDList():
                veh_depart_t[vid] = t
                departed_ids.add(vid)
                try:
                    seen_edges.update(traci.vehicle.getRoute(vid))
                except Exception:
                    pass
            for vid in traci.simulation.getArrivedIDList():
                arrived_ids.add(vid)
                if vid in veh_depart_t:
                    veh_travel_times.append(t - veh_depart_t[vid])
                # use cached value — vehicle already removed, traci call would raise
                if vid in last_veh_waiting:
                    veh_time_losses.append(last_veh_waiting.pop(vid))
            try:
                teleport_count += len(traci.simulation.getStartingTeleportIDList())
            except Exception:
                pass
            try:
                collision_count += traci.simulation.getCollidingVehiclesNumber()
            except Exception:
                pass

            # ── local 500m vehicle tracking + conflict zone for PET ─────────
            try:
                all_veh_ids = list(traci.vehicle.getIDList())
            except Exception:
                all_veh_ids = []

            step_conflict_vehs: dict[str, set] = {cid: set() for cid in watch}
            step_local_speeds:  dict[str, list] = {cid: [] for cid in watch}
            step_local_waiting: dict[str, list] = {cid: [] for cid in watch}
            step_local_veh_cnt: dict[str, int]  = {cid: 0  for cid in watch}

            for vid in all_veh_ids:
                try:
                    road_id = str(traci.vehicle.getRoadID(vid))
                    lane_id = str(traci.vehicle.getLaneID(vid))
                except Exception:
                    continue
                try:
                    speed = float(traci.vehicle.getSpeed(vid))
                except Exception:
                    speed = float("nan")
                # cache time_loss and waiting_time while vehicle is active
                try:
                    _wt = float(traci.vehicle.getAccumulatedWaitingTime(vid))
                    last_veh_waiting[vid] = _wt
                except Exception:
                    _wt = last_veh_waiting.get(vid, float("nan"))
                try:
                    _tl = float(traci.vehicle.getTimeLoss(vid))
                except Exception:
                    _tl = float("nan")
                for cid, w in watch.items():
                    in_local = lane_id in w["local_lane_ids"] or road_id in w["local_edge_ids"]
                    in_conflict = road_id in w["conflict_edge_ids"] or road_id in w["conflict_edge_aliases"]
                    if in_local:
                        w["local_seen_veh_ids"].add(vid)
                        if not (speed != speed):  # not nan
                            w["local_speed_samples"].append(speed)
                            stop_now = speed <= 0.1
                            if stop_now and not w["prev_veh_stop_state"].get(vid, False):
                                w["local_stop_count"] += 1
                            w["prev_veh_stop_state"][vid] = stop_now
                        if not (_tl != _tl):  # not nan
                            w["local_time_loss_samples"].append(_tl)
                        if not (_wt != _wt):  # not nan
                            w["local_delay_samples"].append(_wt)
                        step_local_speeds[cid].append(speed if not (speed != speed) else 0.0)
                        step_local_waiting[cid].append(_wt if not (_wt != _wt) else 0.0)
                        step_local_veh_cnt[cid] += 1
                    if in_conflict:
                        step_conflict_vehs[cid].add(vid)
                        if vid not in w["active_veh_entries"]:
                            w["active_veh_entries"][vid] = t

            # flush vehicles that left conflict zone
            for cid, w in watch.items():
                for vid in list(w["active_veh_entries"].keys()):
                    if vid not in step_conflict_vehs[cid]:
                        enter = w["active_veh_entries"].pop(vid)
                        w["veh_intervals"].append({"enter_time": float(enter), "exit_time": t})

            # per-crosswalk step work
            for cid, w in watch.items():
                tls_id        = w["tls_id"]
                ped_link_index = w["ped_link_index"]
                local_lane_ids = w["local_lane_ids"]
                crossing_edges = w["crossing_edges"]

                # ── queue length ──────────────────────────────────────────────
                q = 0
                for lid in local_lane_ids:
                    try:
                        q += traci.lane.getLastStepHaltingNumber(lid)
                    except Exception:
                        pass
                w["queue_samples"].append(float(q))
                _spds = step_local_speeds[cid]
                _wtgs = step_local_waiting[cid]
                w["lane_trace_rows"].append({
                    "t":                 round(t, 1),
                    "policy_t":          round(t - warmup_sec, 1),
                    "scenario":          scenario,
                    "crosswalk_id":      cid,
                    "veh_count":         step_local_veh_cnt[cid],
                    "halting_count":     int(q),
                    "mean_speed":        round(sum(_spds) / len(_spds), 4) if _spds else None,
                    "mean_waiting_time": round(sum(_wtgs) / len(_wtgs), 4) if _wtgs else None,
                })

                # ── pedestrian presence + wait time + PET interval tracking ──
                # 기존 smoke 방식: all_ped_ids에서 getRoadID → crossing_edge_set 비교
                ped_near = 0
                step_crossing_pids: set[str] = set()
                for pid in all_ped_ids:
                    try:
                        road = traci.person.getRoadID(pid)
                        lane = traci.person.getLaneID(pid)
                    except Exception:
                        continue
                    hit, _ = _route_match_details(road, lane, crossing_edges, w["route_path"], tls_id)
                    try:
                        _pid_wt = float(traci.person.getWaitingTime(pid))
                    except Exception:
                        _pid_wt = 0.0
                    w["ped_trace_rows"].append({
                        "t":             round(t, 1),
                        "policy_t":      round(t - warmup_sec, 1),
                        "scenario":      scenario,
                        "crosswalk_id":  cid,
                        "person_id":     pid,
                        "road_id":       road,
                        "lane_id":       lane,
                        "waiting_time":  round(_pid_wt, 3),
                        "on_crossing":   bool(hit),
                    })
                    if hit:
                        ped_near += 1
                        step_crossing_pids.add(pid)
                        w["ped_seen"].add(pid)
                        if pid not in w["active_ped_entries"]:
                            w["active_ped_entries"][pid] = t
                        try:
                            wt = traci.person.getWaitingTime(pid)
                            if wt > 0:
                                w["ped_wait_samples"].append(wt)
                        except Exception:
                            pass
                if ped_near > 0:
                    w["ped_presence_steps"] += 1
                # flush peds that left crossing
                for pid in list(w["active_ped_entries"].keys()):
                    if pid not in step_crossing_pids:
                        enter = w["active_ped_entries"].pop(pid)
                        w["ped_intervals"].append({"enter_time": float(enter), "exit_time": t})

                # ── elderly incomplete detection ──────────────────────────────
                if extension_sec > 0 and tls_id:
                    try:
                        rem_chk = float(traci.trafficlight.getNextSwitch(tls_id) - t)
                        st_chk  = traci.trafficlight.getRedYellowGreenState(tls_id)
                        if 0 < rem_chk <= 3.0 and ped_link_index < len(st_chk):
                            if st_chk[ped_link_index] in {"G", "g"}:
                                for pid in all_ped_ids:
                                    try:
                                        road = traci.person.getRoadID(pid)
                                        lane = traci.person.getLaneID(pid)
                                    except Exception:
                                        continue
                                    hit, _ = _route_match_details(road, lane, crossing_edges, w["route_path"], tls_id)
                                    if hit:
                                        try:
                                            if traci.person.getTypeID(pid) == VTYPE_ELDERLY:
                                                w["elderly_incomplete"] += 1
                                        except Exception:
                                            pass
                    except Exception:
                        pass

                # ── TLS cycle detection ───────────────────────────────────────
                if not tls_id:
                    continue
                try:
                    phase     = traci.trafficlight.getPhase(tls_id)
                    last_ph   = tls_last_phase.get(tls_id, phase)
                    if phase < last_ph:  # phase index wrapped → new signal cycle
                        tls_cycle[tls_id] = tls_cycle.get(tls_id, 0) + 1
                    tls_last_phase[tls_id] = phase
                    cycle = tls_cycle.get(tls_id, 0)

                    state     = traci.trafficlight.getRedYellowGreenState(tls_id)
                    remaining = float(traci.trafficlight.getNextSwitch(tls_id) - t)

                    ped_link_state = state[ped_link_index] if ped_link_index < len(state) else ""
                    # pedestrian-only phase: ped link is green AND no vehicle-green links
                    non_ped_green = sum(
                        1 for i, s in enumerate(state)
                        if i != ped_link_index and s in {"G", "g", "s"}
                    )
                    is_ped_only = ped_link_state in {"G", "g"} and non_ped_green == 0

                    skip = _should_extend(
                        scenario, tls_id, state, ped_link_index, is_ped_only,
                        remaining, ped_near,
                        extended_this_cycle, phase, cid,
                    )
                    if skip == "" and extension_sec > 0:
                        traci.trafficlight.setPhaseDuration(tls_id, remaining + extension_sec)
                        extended_this_cycle.add((tls_id, phase, cid))
                        try:
                            _remaining_after = float(traci.trafficlight.getNextSwitch(tls_id) - t)
                        except Exception:
                            _remaining_after = remaining + extension_sec
                        extension_events.append({
                            "crosswalk_id":              cid,
                            "time":                      round(t, 1),
                            "policy_time":               round(t - warmup_sec, 1),
                            "tls_id":                    tls_id,
                            "phase":                     phase,
                            "cycle":                     cycle,
                            "state":                     state,
                            "ped_link_state":            ped_link_state,
                            "remaining_before":          round(remaining, 1),
                            "next_switch_before":        round(t + remaining, 1),
                            "extension_sec":             extension_sec,
                            "remaining_after_observed":  round(_remaining_after, 1),
                            "next_switch_after_observed": round(t + _remaining_after, 1),
                            "ped_near":                  ped_near,
                        })
                    w["tls_trace_rows"].append({
                        "t":               round(t, 1),
                        "policy_t":        round(t - warmup_sec, 1),
                        "scenario":        scenario,
                        "crosswalk_id":    cid,
                        "tls_id":          tls_id,
                        "phase":           int(phase),
                        "state":           state,
                        "remaining":       round(remaining, 1),
                        "ped_link_state":  ped_link_state,
                        "is_ped_green":    ped_link_state in {"G", "g"},
                        "is_ped_only":     is_ped_only,
                        "ped_near":        ped_near,
                        "extension_fired": (skip == "" and extension_sec > 0 and scenario == "smart"),
                    })
                except Exception:
                    pass

    finally:
        try:
            traci.close()
        except Exception:
            pass

    # flush remaining active entries (peds/vehicles still active at sim end)
    for cid, w in watch.items():
        for pid, enter in list(w["active_ped_entries"].items()):
            w["ped_intervals"].append({"enter_time": float(enter), "exit_time": float(t)})
        w["active_ped_entries"].clear()
        for vid, enter in list(w["active_veh_entries"].items()):
            w["veh_intervals"].append({"enter_time": float(enter), "exit_time": float(t)})
        w["active_veh_entries"].clear()

    # ── trace CSV 출력 (baseline/smart 모두 전 step 기록) ─────────────────────
    pd.DataFrame(extension_events).to_csv(out_dir / "extension_trace.csv", index=False)
    pd.DataFrame([r for w in watch.values() for r in w["tls_trace_rows"]]).to_csv(
        out_dir / "tls_state_trace.csv", index=False)
    pd.DataFrame([r for w in watch.values() for r in w["lane_trace_rows"]]).to_csv(
        out_dir / "local_lane_effect_trace.csv", index=False)
    pd.DataFrame([r for w in watch.values() for r in w["ped_trace_rows"]]).to_csv(
        out_dir / "pedestrian_effect_trace.csv", index=False)

    # ── compile per-crosswalk result rows ─────────────────────────────────────
    net_tt   = (sum(veh_travel_times) / len(veh_travel_times)) if veh_travel_times else None
    net_loss = (sum(veh_time_losses)  / len(veh_time_losses))  if veh_time_losses  else None
    elapsed = round(time.time() - t_start, 2)
    run_end_time = datetime.now(timezone.utc).isoformat()
    net_edge_total = len(all_net_edges) if all_net_edges else 1

    rows: list[dict] = []
    for row in candidate_df.itertuples(index=False):
        cid  = str(row.crosswalk_id)
        w    = watch[cid]
        exts = [e for e in extension_events if e["crosswalk_id"] == cid]
        qs   = w["queue_samples"]
        pws  = w["ped_wait_samples"]
        cw_elderly = (elderly_ratio.get(cid, 0.15) if isinstance(elderly_ratio, dict)
                      else float(elderly_ratio))
        # PET 계산 (local_watcher 방식, SSM 없이)
        pet = _summarize_local_watcher_pet(
            ped_intervals=w["ped_intervals"],
            veh_intervals=w["veh_intervals"],
            pedestrian_crossing_count=len(w["ped_seen"]),
            conflict_edge_count=len(w["conflict_edge_ids"]),
        )
        pet_severe = float(pet.get("very_risky_crossing_count", 0) or 0)
        cw_eld_inc = float(w["elderly_incomplete"])
        safety_risk = (pet_severe + cw_eld_inc * 2.0)
        accident_ev = safety_risk * 1_000_000.0

        # local 500m 집계
        ls_sp  = w["local_speed_samples"]
        ls_tl  = w["local_time_loss_samples"]
        ls_dl  = w["local_delay_samples"]
        l500_speed  = round(sum(ls_sp) / len(ls_sp), 4) if ls_sp else None
        l500_tl     = round(sum(ls_tl) / len(ls_tl), 4) if ls_tl else None
        l500_delay  = round(sum(ls_dl) / len(ls_dl), 4) if ls_dl else None
        l500_veh    = len(w["local_seen_veh_ids"])

        ped_wt_mean = round(sum(pws) / len(pws), 3) if pws else 0.0
        ped_wt_max  = round(max(pws), 3) if pws else 0.0
        l_delay = l500_delay if l500_delay is not None else (round(net_loss, 2) if net_loss is not None else None)

        rows.append({
            # ── identity (기존 smoke 호환) ────────────────────────────────────
            "run_name":               f"enhanced_{scenario}_seed{seed}",
            "run_profile":            run_profile,
            "crosswalk_id":           cid,
            "scenario":               scenario,
            "scenario_name":          "main_realistic_stress",
            "demand_profile":         str(getattr(row, "demand_profile", "main_realistic_stress")),
            "original_ped_count":     int(getattr(row, "original_ped_count", 0)),
            "scaled_ped_count":       int(getattr(row, "scaled_ped_count", getattr(row, "ped_repeat_count", 0))),
            "pedestrian_demand_multiplier": float(getattr(row, "pedestrian_demand_multiplier", 1.0)),
            "pedestrian_demand_source":     str(getattr(row, "pedestrian_demand_source", "")),
            "seed":                   seed,
            "output_dir":             str(out_dir),
            "run_start_time":         run_start_time,
            "run_end_time":           run_end_time,
            "elapsed_sec":            elapsed,
            "completed":              True,
            "sim_duration":           duration,
            "warmup":                 warmup_sec,
            "step_length":            step_length,
            "step_count":             step_count,
            "tls_id_used":            w["tls_id"],
            "ped_link_index":         w["ped_link_index"],
            "crossing_edge_id":       w["crossing_edge_id"],
            "route_from_edge":        w["route_from_edge"],
            "route_to_edge":          w["route_to_edge"],
            "batch_network_file":     str(getattr(row, "batch_network_file", "")),
            "route_reason":           str(getattr(row, "route_reason", "")),
            # ── pedestrian ───────────────────────────────────────────────────
            "ped_crossing_person_count":   len(w["ped_seen"]),
            "pedestrian_crossing_count":   len(w["ped_seen"]),
            "expected_ped_repeat_count":   int(getattr(row, "ped_repeat_count", 0)),
            "ped_repeat_count_match":      len(w["ped_seen"]) >= int(getattr(row, "ped_repeat_count", 0)),
            "ped_crossing_presence_steps": w["ped_presence_steps"],
            "ped_wait_time_mean":              ped_wt_mean,
            "ped_wait_time_max":               ped_wt_max,
            "average_pedestrian_wait_time":    ped_wt_mean,
            "pedestrian_waiting_time_mean":    ped_wt_mean,
            "pedestrian_waiting_time":         ped_wt_mean,
            "pedestrian_waiting_time_max":     ped_wt_max,
            "max_pedestrian_wait_time":        ped_wt_max,
            "elderly_incomplete_crossings":    w["elderly_incomplete"],
            "crosswalk_elderly_ratio":         cw_elderly,
            # ── extension (enhanced smoke 신규) ──────────────────────────────
            "extension_count":                 len(exts),
            "pedestrian_green_extension_count": len(exts),
            "total_extension_sec":             len(exts) * extension_sec,
            "extension_natural_count":         len(exts),  # 모든 연장이 ped_near>0 조건 통과
            # ── queue / local vehicle ────────────────────────────────────────
            "avg_queue_length":         round(sum(qs) / len(qs), 3) if qs else 0.0,
            "max_queue_length":         max(qs) if qs else 0.0,
            "local_500m_vehicle_count": l500_veh,
            "local_500m_mean_speed":    l500_speed,
            "local_500m_mean_time_loss": l500_tl,
            "local_500m_avg_delay_sec": l500_delay,
            "local_500m_queue_proxy":   round(sum(qs) / len(qs), 3) if qs else 0.0,
            "local_500m_stop_count":    w["local_stop_count"],
            "surrounding_road_delay_sec": l_delay,
            "veh_delay_mean":     l_delay,
            "veh_delay_max":      round(max(ls_dl), 3) if ls_dl else 0.0,
            "veh_avg_delay_sec":  l_delay,
            "avg_vehicle_delay_sec": l_delay,
            "vehicle_delay_cost": None,
            # ── global network flow (기존 smoke 호환) ─────────────────────────
            "network_mean_travel_time":    round(net_tt,   2) if net_tt   is not None else None,
            "network_mean_time_loss":      round(net_loss, 2) if net_loss is not None else None,
            "network_avg_delay_sec":       round(net_loss, 2) if net_loss is not None else None,
            "network_arrived_vehicles":    len(arrived_ids),
            "network_departed_vehicles":   len(departed_ids),
            "total_vehicle_arrivals":      len(arrived_ids),
            "network_teleport_count":      teleport_count,
            "network_collision_count":     collision_count,
            "network_vehicle_edge_coverage_ratio":
                round(len(seen_edges) / net_edge_total, 4) if net_edge_total else 0.0,
            # ── vehicle routes ───────────────────────────────────────────────
            "generated_vehicle_count":    generated_vehicle_count,
            "generated_vehicle_route_file": str(global_veh_file) if global_veh_file else None,
            "vehicle_route_count":        int(vehicle_route_diversity.get("vehicle_route_count", 0)),
            "unique_vehicle_route_count": int(vehicle_route_diversity.get("unique_vehicle_route_count", 0)),
            "duplicate_factor":           float(vehicle_route_diversity.get("duplicate_factor", 1.0)),
            "unique_vehicle_route_ratio": float(vehicle_route_diversity.get("unique_vehicle_route_ratio", 1.0)),
            "vehicle_bbox_coverage":      None,
            # ── infrastructure ───────────────────────────────────────────────
            "used_vehicle_edges":      int(vehicle_route_diversity.get("used_vehicle_edges", 0)),
            "surrounding_lane_count":  surrounding_lane_count,
            "road_lanes":              float(getattr(row, "road_lanes", _REFERENCE_LANES)),
            # ── PET / risk (local_watcher 방식, SSM 없이 수동 추정) ──────────
            "pet_source":                          pet.get("pet_source", "local_watcher"),
            "pet_available":                       pet.get("pet_available", False),
            "pet_unavailable_reason":              pet.get("pet_unavailable_reason", ""),
            "pet_event_count":                     pet.get("pet_event_count"),
            "very_risky_crossing_count":           pet.get("very_risky_crossing_count"),
            "risky_crossing_count":                pet.get("risky_crossing_count"),
            "safe_crossing_count":                 pet.get("safe_crossing_count"),
            "low_pet_event_count":                 pet.get("low_pet_event_count"),
            "low_pet_per_100_crossings":           pet.get("low_pet_per_100_crossings"),
            "low_pet_per_100_conflict_candidates": pet.get("low_pet_per_100_conflict_candidates"),
            "pet_min":                             pet.get("pet_min"),
            "pet_p10":                             pet.get("pet_p10"),
            "pet_mean":                            pet.get("pet_mean"),
            "accident_risk_estimate":              pet.get("accident_risk_estimate"),
            "safety_risk_score":                   round(safety_risk, 4),
            "accident_expected_value":             round(accident_ev, 2),
            # ── run context ──────────────────────────────────────────────────
            "run_group":  group_name,
        })

    elapsed = round(time.time() - t_start, 2)
    result_df = pd.DataFrame(rows)

    # save per-run CSV
    csv_path = out_dir / "simulation_result.csv"
    result_df.to_csv(csv_path, index=False, encoding="utf-8-sig")

    # save run_metadata.json
    meta = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "runner": "run_enhanced_smoke_group",
        "run_profile": run_profile,
        "scenario": scenario,
        "seed": seed,
        "sim_duration": duration,
        "step_length": step_length,
        "ped_count": len(ped_records),
        "elderly_ratio": elderly_ratio,
        "include_vehicles": include_vehicles,
        "global_veh_file": str(global_veh_file) if global_veh_file else None,
        "extension_sec": extension_sec,
        "crosswalk_count": len(rows),
        "elapsed_sec": elapsed,
        "group_name": group_name,
        "net_file": str(net_file),
    }
    (out_dir / "run_metadata.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    return result_df


# ─────────────────────────────────────────────────────────────────────────────
# 4. Candidate CSV loader + ped-count override
# ─────────────────────────────────────────────────────────────────────────────
def _load_candidate(csv_path: Path, ped_count: int) -> pd.DataFrame:
    df = pd.read_csv(csv_path, encoding="utf-8-sig")
    df["ped_repeat_count"]       = ped_count
    df["ped_repeat_spacing_sec"] = 2.0
    # _person_routes_crossing requires these columns; fill if absent in CSV
    if "route_reason" not in df.columns:
        df["route_reason"] = ""
    if "batch_network_file" not in df.columns:
        df["batch_network_file"] = str(csv_path.parent.parent / "nets" / (csv_path.stem.replace("_candidates", "") + ".net.xml"))
    # join road_lanes + crosswalk_elderly_ratio from stepwise source CSV
    try:
        src = pd.read_csv(_STEPWISE_CSV, encoding="cp949", usecols=["crosswalk_id", "lanes", "elderly_ratio"])
        if "road_lanes" not in df.columns:
            df = df.merge(src.rename(columns={"lanes": "road_lanes"}), on="crosswalk_id", how="left")
            df["road_lanes"] = df["road_lanes"].fillna(src["lanes"].median()).fillna(2.0)
        if "crosswalk_elderly_ratio" not in df.columns:
            df = df.merge(src[["crosswalk_id", "elderly_ratio"]].rename(
                columns={"elderly_ratio": "crosswalk_elderly_ratio"}
            ), on="crosswalk_id", how="left")
            df["crosswalk_elderly_ratio"] = df["crosswalk_elderly_ratio"].fillna(
                src["elderly_ratio"].median()
            ).fillna(0.19)
    except Exception as e:
        print(f"[enhanced smoke][WARN] stepwise join failed ({e})", flush=True)
        if "road_lanes" not in df.columns:
            df["road_lanes"] = 2.0
        if "crosswalk_elderly_ratio" not in df.columns:
            df["crosswalk_elderly_ratio"] = 0.19
    return df


def _lane_scaled_vehicle_routes(
    net_file: Path,
    sim_duration: int,
    seed: int,
    out_dir: Path,
    avg_lanes: float,
    warmup_sec: int = 0,
) -> "Path | None":
    """Like _ensure_global_vehicle_routes but scales vehicle count by avg_lanes."""
    total_duration = sim_duration + warmup_sec
    vehicle_count = max(1, round(_BASE_VEH_120S * avg_lanes / _REFERENCE_LANES * total_duration / 120))
    vph = round(vehicle_count / total_duration * 3600)
    period = max(0.1, 3600.0 / max(vph, 1.0))

    global_dir = out_dir / "global_vehicle_routes"
    global_dir.mkdir(parents=True, exist_ok=True)
    # warmup_sec를 파일명에 포함해 캐시 충돌 방지
    route_file = global_dir / f"demand_vehicle_w{warmup_sec}.rou.xml"
    trip_file  = global_dir / f"demand_vehicle_w{warmup_sec}.trips.xml"

    if route_file.exists():
        try:
            root = ET.parse(route_file).getroot()
            actual = sum(1 for _ in root.iter("vehicle"))
            if actual >= int(vehicle_count * 0.9):
                print(f"[enhanced smoke] reuse existing vehicle routes ({actual} veh, avg_lanes={avg_lanes:.2f}, warmup={warmup_sec}s)", flush=True)
                return route_file
        except ET.ParseError:
            pass
        route_file.unlink(missing_ok=True)
        trip_file.unlink(missing_ok=True)

    try:
        rt_script = random_trips_script()
        log_file  = global_dir / f"randomtrips_w{warmup_sec}.log"
        with log_file.open("w", encoding="utf-8") as lf:
            subprocess.run(
                [
                    sys.executable, rt_script,
                    "-n", str(net_file),
                    "-o", str(trip_file),
                    "-r", str(route_file),
                    "--period", str(period),
                    "--seed", str(seed),
                    "--begin", "0",
                    "--end", str(sim_duration + warmup_sec),
                    "--vehicle-class", "passenger",
                    "--validate",
                ],
                check=True, env=sumo_env(),
                stdout=lf, stderr=subprocess.STDOUT,
            )
        _enforce_exact_vehicle_count(route_file, trip_file, vehicle_count, total_duration)
        print(
            f"[enhanced smoke] lane-scaled vehicle routes: {vehicle_count} veh "
            f"(avg_lanes={avg_lanes:.2f}, ref={_REFERENCE_LANES}, seed={seed}, warmup={warmup_sec}s)",
            flush=True,
        )
        return route_file
    except Exception as exc:
        print(f"[enhanced smoke][WARN] lane-scaled vehicle route gen failed: {exc}", flush=True)
        return None


# ─────────────────────────────────────────────────────────────────────────────
# 5. One group run (baseline + smart for all crosswalks, one seed)
# ─────────────────────────────────────────────────────────────────────────────
def _run_group(
    candidate_csv: Path,
    net_file: Path,
    seed: int,
    out_dir: Path,          # group-level dir: {output_dir}
    ped_count: int,
    elderly_ratio: float,
    duration: int,
    step_length: float,
    crosswalk_id: str | None = None,
    warmup_sec: int = 0,
    run_profile: str = "custom",
    ped_demand_scale_mode: str = "none",
    ped_demand_min_target: int = 30,
    ped_demand_max_cap: int = 200,
    ped_demand_source: "str | None" = None,
    scenario_mode: str = "both",
) -> pd.DataFrame:
    import shutil, tempfile

    group_name = candidate_csv.stem.replace("_candidates", "")
    candidate_df_raw = _load_candidate(candidate_csv, ped_count)
    if crosswalk_id:
        candidate_df_raw = candidate_df_raw[
            candidate_df_raw["crosswalk_id"].astype(str) == crosswalk_id
        ].reset_index(drop=True)

    # phase-aligned depart: 원본 candidate_df에 depart_time 컬럼만 merge
    # (_phase_aligned_depart_plan은 새 DataFrame 반환 → 전체 교체 금지)
    depart_plan = _phase_aligned_depart_plan(
        net_file, candidate_df_raw,
        ped_repeat_count=ped_count,
        ped_repeat_spacing_sec=2.0,
    )
    # ── 보행자 depart 시간: policy_t → SUMO_t 변환 ────────────────────────────
    # policy_t = sumo_t - warmup_sec  (policy_t=0이 실질 시뮬 시작)
    # ped_depart_time은 phase_aligned_plan의 policy 시간축 기준값
    # SUMO route에는 sumo_depart = policy_depart + warmup_sec 으로 기록
    _plan_cols = ["crosswalk_id", "depart_time"]
    if "cycle_duration" in depart_plan.columns:
        _plan_cols = ["crosswalk_id", "depart_time", "cycle_duration"]
    _plan_merge = depart_plan[_plan_cols].rename(columns={"depart_time": "ped_depart_time"})
    candidate_df = candidate_df_raw.merge(_plan_merge, on="crosswalk_id", how="left")

    if "ped_depart_time" in candidate_df.columns:
        _cycle_dur = (
            candidate_df["cycle_duration"].astype(float)
            if "cycle_duration" in candidate_df.columns
            else pd.Series(0.0, index=candidate_df.index)
        )
        # 이전 사이클 타게팅: diagnostic_fast에서만 적용
        # policy_safe / custom은 phase-aligned depart 원본 유지
        if run_profile == "diagnostic_fast" and warmup_sec > 0:
            _policy_prev = candidate_df["ped_depart_time"] - _cycle_dur
            _apply = (_cycle_dur > 0) & (_policy_prev + warmup_sec >= 0)
            candidate_df.loc[_apply, "ped_depart_time"] = _policy_prev[_apply]
            if not _apply.any():
                print(
                    f"[enhanced smoke][WARN] prev-cycle shift skipped "
                    f"(warmup_sec={warmup_sec} 부족 — cycle_duration > warmup_sec인 경우 "
                    f"--warmup-sec를 늘리세요)",
                    flush=True,
                )
            else:
                print(
                    f"[enhanced smoke] prev-cycle shift (diagnostic_fast): {_apply.sum()}/{len(candidate_df)} rows",
                    flush=True,
                )
        # policy_t → SUMO_t: depart + warmup_sec
        candidate_df["ped_depart_time"] = (
            candidate_df["ped_depart_time"] + warmup_sec
        ).clip(lower=0.0)
        print(
            f"[enhanced smoke] ped_depart_time (SUMO axis, warmup={warmup_sec}s): "
            f"{candidate_df['ped_depart_time'].min():.1f}~"
            f"{candidate_df['ped_depart_time'].max():.1f}s",
            flush=True,
        )

    # per-crosswalk elderly ratio dict (from crosswalk_stepwise_result_50m.csv join)
    if "crosswalk_elderly_ratio" in candidate_df_raw.columns:
        cw_elderly_ratios: dict[str, float] = dict(
            zip(candidate_df_raw["crosswalk_id"].astype(str),
                candidate_df_raw["crosswalk_elderly_ratio"].astype(float))
        )
    else:
        cw_elderly_ratios = {}

    # 보행자 수요 스케일링 (pedestrian.csv source → demand_scenarios.py)
    _scaled_counts, _ped_multiplier = _compute_scaled_ped_counts(
        candidate_df,
        base_ped_count=ped_count,
        mode=ped_demand_scale_mode,
        min_target=ped_demand_min_target,
        max_cap=ped_demand_max_cap,
        source_path=ped_demand_source,
    )
    # candidate_df에 수요 메타 컬럼 추가 (baseline/smart 공유)
    candidate_df = candidate_df.copy()
    candidate_df["original_ped_count"]           = ped_count
    candidate_df["scaled_ped_count"]             = candidate_df["crosswalk_id"].astype(str).map(_scaled_counts)
    candidate_df["pedestrian_demand_multiplier"] = round(_ped_multiplier, 4)
    candidate_df["pedestrian_demand_source"]     = ped_demand_source or "demand_scenarios.py"
    candidate_df["demand_profile"] = (
        "peak_pedestrian_scaled" if ped_demand_scale_mode != "none" else "main_realistic_stress"
    )
    # ped_repeat_count를 scaled 값으로 교체
    candidate_df["ped_repeat_count"] = candidate_df["scaled_ped_count"].fillna(ped_count).astype(int)

    # vehicle routes: keep in named subdir (reused across baseline/smart)
    avg_lanes = float(candidate_df_raw["road_lanes"].mean()) if "road_lanes" in candidate_df_raw.columns else _REFERENCE_LANES
    veh_route_dir = out_dir / "_vehicle_routes" / f"seed{seed:05d}"
    global_veh_file = _lane_scaled_vehicle_routes(
        net_file=net_file,
        sim_duration=duration,
        seed=seed,
        out_dir=veh_route_dir,
        avg_lanes=avg_lanes,
        warmup_sec=warmup_sec,
    )

    _all_pairs = [("baseline", 0.0), ("smart", EXTENSION_INCREMENT)]
    _run_pairs = {
        "both":          _all_pairs,
        "baseline_only": [("baseline", 0.0)],
        "smart_only":    [("smart",    EXTENSION_INCREMENT)],
    }.get(scenario_mode, _all_pairs)

    scenario_dfs: dict[str, pd.DataFrame] = {}
    for scenario, ext_sec in _run_pairs:
        # target: {out_dir}/{scenario}/seed{NN}/
        result_dir = out_dir / scenario / f"seed{seed:05d}"
        result_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=f"enh_{scenario}_{seed}_") as tmp:
            sim_tmp = Path(tmp)
            df = _run_enhanced_scenario(
                candidate_df, net_file, scenario, seed, duration, step_length,
                out_dir=result_dir,
                sim_tmp_dir=sim_tmp,
                include_vehicles=True,
                extension_sec=ext_sec,
                elderly_ratio=cw_elderly_ratios if cw_elderly_ratios else 0.19,
                global_veh_file=global_veh_file,
                group_name=group_name,
                warmup_sec=warmup_sec,
                run_profile=run_profile,
            )
        scenario_dfs[scenario] = df
        print(f"  [{scenario}] seed{seed:05d} done — {len(df)} rows → {result_dir / 'simulation_result.csv'}")

    # simulation_result_with_baseline.csv — both 모드일 때만 생성
    baseline_df = scenario_dfs.get("baseline", pd.DataFrame())
    smart_df    = scenario_dfs.get("smart",    pd.DataFrame())
    if scenario_mode == "both" and not baseline_df.empty and not smart_df.empty:
        metric_cols = [c for c in smart_df.columns
                       if c not in {"run_name", "crosswalk_id", "scenario", "seed",
                                    "output_dir", "run_start_time", "run_end_time",
                                    "elapsed_sec", "completed", "run_group",
                                    "sim_duration", "warmup", "step_length"}]
        # delta_<metric> = smart - baseline (numeric only; bool/str skipped)
        # added only to smart rows; baseline rows get NaN automatically via concat
        smart_df_out = smart_df.copy()
        base_indexed = baseline_df.set_index("crosswalk_id")
        for mc in metric_cols:
            if smart_df_out[mc].dtype == bool:
                continue
            s_num = pd.to_numeric(smart_df_out[mc], errors="coerce")
            b_vals = (base_indexed[mc].reindex(smart_df_out["crosswalk_id"].values).values
                      if mc in base_indexed.columns else None)
            if b_vals is None:
                continue
            b_num = pd.to_numeric(b_vals, errors="coerce")
            if s_num.notna().any() and pd.Series(b_num).notna().any():
                smart_df_out[f"delta_{mc}"] = s_num.values - b_num
        # concat: baseline first, smart second
        combined = pd.concat([baseline_df, smart_df_out], ignore_index=True)
        combined = combined.sort_values(
            ["crosswalk_id", "scenario"],
            key=lambda col: col.map({"baseline": 0, "smart": 1})
                            if col.name == "scenario" else col,
        ).reset_index(drop=True)
        # validation gate: each crosswalk must have both rows
        for cid in smart_df["crosswalk_id"].unique():
            sub_scenarios = combined.loc[combined["crosswalk_id"] == cid, "scenario"].tolist()
            if "baseline" not in sub_scenarios or "smart" not in sub_scenarios:
                raise RuntimeError(
                    f"simulation_result_with_baseline: {cid} missing row — got {sub_scenarios}"
                )
        smart_result_dir = out_dir / "smart" / f"seed{seed:05d}"
        combined.to_csv(smart_result_dir / "simulation_result_with_baseline.csv",
                        index=False, encoding="utf-8-sig")

    all_df = pd.concat(list(scenario_dfs.values()), ignore_index=True)
    return all_df


# ─────────────────────────────────────────────────────────────────────────────
# 6. Excel output — 4 sheets
# ─────────────────────────────────────────────────────────────────────────────
_METRIC_COLS = [
    "pedestrian_crossing_count", "ped_crossing_presence_steps",
    "elderly_incomplete_crossings",
    "extension_count", "pedestrian_green_extension_count",
    "total_extension_sec", "extension_natural_count",
    "avg_queue_length", "max_queue_length",
    "network_mean_travel_time", "network_mean_time_loss",
    "network_arrived_vehicles", "network_departed_vehicles",
    "network_teleport_count", "network_collision_count",
    "used_vehicle_edges", "surrounding_lane_count",
]

def _style_header(ws: Any, color: str = "1F3864") -> None:
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    thin = Side(style="thin", color="CCCCCC")
    bdr  = Border(left=thin, right=thin, top=thin, bottom=thin)
    for cell in ws[1]:
        cell.font      = Font(name="맑은 고딕", bold=True, color="FFFFFF", size=10)
        cell.fill      = PatternFill("solid", fgColor=color)
        cell.alignment = Alignment(horizontal="center", wrap_text=True)
        cell.border    = bdr
    ws.row_dimensions[1].height = 38


def _auto_width(ws: Any, cols: list[str]) -> None:
    from openpyxl.utils import get_column_letter
    for i, col in enumerate(cols, start=1):
        ws.column_dimensions[get_column_letter(i)].width = max(13, min(32, len(col) + 3))


def _build_excel(
    result_df: pd.DataFrame,   # all baseline+smart rows
    candidate_df: pd.DataFrame,  # crosswalk metadata from candidate CSV
    out_path: Path,
) -> None:
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    from openpyxl.utils import get_column_letter

    metric_set = set(_METRIC_COLS)
    thin = Side(style="thin", color="CCCCCC")
    bdr  = Border(left=thin, right=thin, top=thin, bottom=thin)
    data_font = Font(name="맑은 고딕", size=9)

    # ── Sheet 1: 비교결과 (Smart vs Baseline) ────────────────────────────────
    wb = Workbook()
    ws1 = wb.active
    ws1.title = "비교결과(Smart vs Baseline)"

    all_cols = list(result_df.columns)
    ws1.append(all_cols)
    _style_header(ws1)

    base_rows = {
        r["crosswalk_id"]: r
        for r in result_df[result_df["scenario"] == "baseline"].to_dict("records")
    }
    smart_rows = {
        r["crosswalk_id"]: r
        for r in result_df[result_df["scenario"] == "smart"].to_dict("records")
    }
    cids = list(dict.fromkeys(result_df["crosswalk_id"].tolist()))

    cur_row = 2
    FILL_BASE  = "D6E4F0"
    FILL_SMART = "E8F5E9"
    FILL_DIFF  = "FFF2CC"
    for cid in cids:
        for scenario, fill_col, row_dict in [
            ("baseline", FILL_BASE,  base_rows.get(cid, {})),
            ("smart",    FILL_SMART, smart_rows.get(cid, {})),
        ]:
            if not row_dict:
                continue
            ws1.append([row_dict.get(c, "") for c in all_cols])
            brow = base_rows.get(cid, {})
            srow = smart_rows.get(cid, {})
            for idx, col in enumerate(all_cols, start=1):
                cell = ws1.cell(row=cur_row, column=idx)
                is_diff = col in metric_set and str(brow.get(col, "")) != str(srow.get(col, ""))
                cell.fill      = PatternFill("solid", fgColor=FILL_DIFF if is_diff else fill_col)
                cell.font      = data_font
                cell.alignment = Alignment(horizontal="center")
                cell.border    = bdr
            cur_row += 1

    _auto_width(ws1, all_cols)
    ws1.freeze_panes = "C2"

    # ── Sheet 2: 검증결과 ────────────────────────────────────────────────────
    ws2 = wb.create_sheet("검증결과")
    val_rows = _build_validation_rows(result_df, candidate_df)
    if val_rows:
        val_cols = list(val_rows[0].keys())
        ws2.append(val_cols)
        _style_header(ws2)
        for r in val_rows:
            ws2.append([r.get(c, "") for c in val_cols])
        _auto_width(ws2, val_cols)
    ws2.freeze_panes = "A2"

    # ── Sheet 3: 횡단보도기본정보 ─────────────────────────────────────────────
    ws3 = wb.create_sheet("횡단보도기본정보")
    if not candidate_df.empty:
        c3_cols = list(candidate_df.columns)
        ws3.append(c3_cols)
        _style_header(ws3, color="2E75B6")
        for r in candidate_df.to_dict("records"):
            ws3.append([r.get(c, "") for c in c3_cols])
        _auto_width(ws3, c3_cols)
    ws3.freeze_panes = "A2"

    # ── Sheet 4: 측정값모음 (기본 정보 + 실측값) ──────────────────────────────
    ws4 = wb.create_sheet("측정값모음")
    info_cols = ["crosswalk_id", "dong_name", "admin_dong", "net_group", "run_group",
                 "risk_rank", "risk_score", "phase6_status_label"]
    avail_info = [c for c in info_cols if c in candidate_df.columns]
    meas_cols  = _METRIC_COLS

    c4_header = avail_info + ["scenario", "seed", "sim_duration"] + meas_cols
    ws4.append(c4_header)
    _style_header(ws4, color="375623")

    info_lookup = candidate_df.set_index("crosswalk_id").to_dict("index") if "crosswalk_id" in candidate_df.columns else {}
    for r in result_df.to_dict("records"):
        cid  = str(r.get("crosswalk_id", ""))
        info = info_lookup.get(cid, {})
        row4 = [info.get(c, "") for c in avail_info] + \
               [r.get("scenario", ""), r.get("seed", ""), r.get("sim_duration", "")] + \
               [r.get(c, "") for c in meas_cols]
        ws4.append(row4)

    for cell in ws4[1]:
        cell.font      = Font(name="맑은 고딕", bold=True, color="FFFFFF", size=10)
        cell.alignment = Alignment(horizontal="center", wrap_text=True)
        cell.border    = bdr
    _auto_width(ws4, c4_header)
    ws4.freeze_panes = "A2"

    out_path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(str(out_path))
    print(f"Excel saved → {out_path}")


def _build_validation_rows(result_df: pd.DataFrame, candidate_df: pd.DataFrame) -> list[dict]:
    """Simple sanity checks per crosswalk."""
    rows = []
    cids = result_df["crosswalk_id"].unique().tolist()
    for cid in cids:
        sub  = result_df[result_df["crosswalk_id"] == cid]
        base = sub[sub["scenario"] == "baseline"]
        smart = sub[sub["scenario"] == "smart"]
        base_ext  = int(base["extension_count"].sum())  if not base.empty  else -1
        smart_ext = int(smart["extension_count"].sum()) if not smart.empty else -1
        rows.append({
            "crosswalk_id":        cid,
            "baseline_ext_count":  base_ext,
            "smart_ext_count":     smart_ext,
            "baseline_ext_ok":     base_ext == 0,
            "smart_ext_gte1":      smart_ext >= 1,
            "smart_natural_ext":   int(smart["extension_natural_count"].sum()) if not smart.empty else -1,
            "avg_queue_baseline":  round(float(base["avg_queue_length"].mean()), 3)  if not base.empty  else None,
            "avg_queue_smart":     round(float(smart["avg_queue_length"].mean()), 3) if not smart.empty else None,
            "net_tt_baseline":     base["network_mean_travel_time"].iloc[0]   if not base.empty  else None,
            "net_tt_smart":        smart["network_mean_travel_time"].iloc[0]  if not smart.empty else None,
            "elderly_incomplete_smart": int(smart["elderly_incomplete_crossings"].sum()) if not smart.empty else 0,
            "status":              "ok" if (base_ext == 0 and smart_ext >= 1) else "warn",
        })
    return rows


# ─────────────────────────────────────────────────────────────────────────────
# 7. Main entry point
# ─────────────────────────────────────────────────────────────────────────────
def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--candidate-csv", required=True, type=Path)
    parser.add_argument("--net-file",      required=True, type=Path)
    parser.add_argument("--output-dir",    required=True, type=Path)
    # seed control
    parser.add_argument("--seed", type=int, default=-1,
                        help="-1 = random seed (single run). Use with --seeds for full mode.")
    parser.add_argument("--seeds", type=int, nargs="*", default=None,
                        help="Explicit seed list for full mode. e.g. --seeds 1 2 3")
    # demand
    parser.add_argument("--ped-count",    type=int,   default=20,
                        help="Pedestrian trips per crosswalk (default 20)")
    parser.add_argument("--elderly-ratio", type=float, default=0.2,
                        help="Fraction of pedestrians that are elderly (default 0.2)")
    # simulation
    parser.add_argument("--sim-duration", type=int,   default=SIM_DURATION)
    parser.add_argument("--step-length",  type=float, default=0.1)
    parser.add_argument("--warmup-sec",   type=int,   default=0,
                        help="Warmup seconds before policy t=0 (default 0). Overridden by --profile.")
    parser.add_argument(
        "--profile",
        choices=["diagnostic_fast", "policy_safe"],
        default=None,
        help=(
            "diagnostic_fast: warmup=30, sim=150, 진단 전용 (보고서 사용 금지). "
            "policy_safe: warmup=0, sim=600, reference 방식 (보고서용)."
        ),
    )
    # pedestrian demand scaling
    parser.add_argument("--ped-demand-source",     type=str, default=None,
                        help="pedestrian.csv path (레이블 전용, 실제 데이터는 demand_scenarios.py)")
    parser.add_argument("--ped-demand-scale-mode", type=str, default="none",
                        choices=["none", "source_min_to_target"],
                        help="none(기본): flat ped-count. source_min_to_target: pedestrian.csv 비례 스케일.")
    parser.add_argument("--min-ped-count-target",  type=int, default=30,
                        help="source_min_to_target 모드에서 최저 수요 목표 (기본 30)")
    parser.add_argument("--max-ped-count-cap",     type=int, default=200,
                        help="scaled_ped_count 상한 (기본 200)")
    parser.add_argument("--scenario-mode", choices=["both", "baseline_only", "smart_only"],
                        default="both",
                        help="both(기본): baseline+smart 둘 다. baseline_only: baseline만. smart_only: smart만.")
    # single-crosswalk mode
    parser.add_argument("--crosswalk-id", type=str, default=None,
                        help="Run only this crosswalk (pipeline check / result inspect mode)")
    args = parser.parse_args()

    # profile override (--profile은 --warmup-sec / --sim-duration을 덮어씀)
    if args.profile == "diagnostic_fast":
        args.warmup_sec   = 30
        args.sim_duration = 150
    elif args.profile == "policy_safe":
        args.warmup_sec   = 0
        args.sim_duration = 600
    run_profile = args.profile or "custom"

    # resolve seeds
    if args.seeds is not None:
        seeds = args.seeds
    elif args.seed == -1:
        seeds = [random.randint(1, 9999)]
        print(f"Random seed selected: {seeds[0]}")
    else:
        seeds = [args.seed]

    candidate_df_meta = pd.read_csv(args.candidate_csv, encoding="utf-8-sig")

    # single-crosswalk filter
    if args.crosswalk_id:
        if args.crosswalk_id not in candidate_df_meta["crosswalk_id"].astype(str).values:
            raise SystemExit(f"crosswalk_id '{args.crosswalk_id}' not found in {args.candidate_csv}")
        candidate_df_meta = candidate_df_meta[
            candidate_df_meta["crosswalk_id"].astype(str) == args.crosswalk_id
        ].reset_index(drop=True)
        print(f"Single-crosswalk mode: {args.crosswalk_id}")

    all_frames: list[pd.DataFrame] = []
    for seed in seeds:
        print(f"\n=== seed {seed} ===")
        df = _run_group(
            candidate_csv=args.candidate_csv,
            net_file=args.net_file,
            seed=seed,
            out_dir=args.output_dir,
            ped_count=args.ped_count,
            elderly_ratio=args.elderly_ratio,
            duration=args.sim_duration,
            step_length=args.step_length,
            crosswalk_id=args.crosswalk_id,
            warmup_sec=args.warmup_sec,
            run_profile=run_profile,
            ped_demand_scale_mode=args.ped_demand_scale_mode,
            ped_demand_min_target=args.min_ped_count_target,
            ped_demand_max_cap=args.max_ped_count_cap,
            ped_demand_source=args.ped_demand_source,
            scenario_mode=args.scenario_mode,
        )
        all_frames.append(df)

    result_df = pd.concat(all_frames, ignore_index=True)

    # global CSV + Excel only when running all crosswalks (not single-crosswalk check mode)
    if not args.crosswalk_id:
        csv_out = args.output_dir / "enhanced_simulation_result.csv"
        result_df.to_csv(csv_out, index=False, encoding="utf-8-sig")
        print(f"\nCSV  → {csv_out}  ({len(result_df)} rows)")

        xlsx_out = args.output_dir / "enhanced_results.xlsx"
        _build_excel(result_df, candidate_df_meta, xlsx_out)
    else:
        print(f"\nSingle-crosswalk mode: global CSV/Excel skipped.")
        print(f"Results → {args.output_dir}/baseline/seed{seeds[0]:05d}/simulation_result.csv")

    # ── next-step command recommendations ────────────────────────────────────
    group = args.candidate_csv.stem.replace("_candidates", "")
    shell_cmd = "bash result/active/real_30seed_runs_sampled10/commands/command_to_run_enhanced_smoke.sh"
    print("\n" + "─" * 60)
    print("[next steps]")
    if args.crosswalk_id:
        print(f"  # single crosswalk OK → run all crosswalks (random seed):")
        print(f"  {shell_cmd} --group {group}")
        print(f"  # or specify seeds:")
        print(f"  {shell_cmd} --group {group} --seeds 1 2 3")
    elif len(seeds) == 1:
        print(f"  # full multi-seed run:")
        print(f"  {shell_cmd} --group {group} --seeds 1 2 3 4 5")
        print(f"  # or other group:")
        for g in ["current_main_12", "signal_fix_9", "generated_signal_7", "p1_p4_recovery_6"]:
            if g != group:
                print(f"  {shell_cmd} --group {g} --seeds 1 2 3")
    else:
        print(f"  # all groups, same seeds ({' '.join(str(s) for s in seeds)}):")
        for g in ["current_main_12", "signal_fix_9", "generated_signal_7", "p1_p4_recovery_6"]:
            if g != group:
                print(f"  {shell_cmd} --group {g} --seeds {' '.join(str(s) for s in seeds)}")
    print("─" * 60)


if __name__ == "__main__":
    main()
