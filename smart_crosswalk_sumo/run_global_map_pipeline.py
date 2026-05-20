#!/usr/bin/env python3
"""
run_global_map_pipeline.py — Global map measurement pipeline (10-min, 7 crosswalks)

run_enhanced_smoke_group 로직 기반. 주요 차이:
  • sim_duration 기본값 = 600s (10분)
  • 전역 맵: 전체 passenger edge 별 speed/flow/halting 주기 샘플링
  • --max-crosswalks N (기본 7): candidate CSV에서 top N 선택 (risk_score 또는 순서)
  • 추가 출력: global_edge_metrics.csv, global_network_timeseries.csv,
              global_edge_comparison.csv (baseline vs smart delta)

Usage:
  python -m smart_crosswalk_sumo.run_global_map_pipeline \\
      --candidate-csv result/.../manifests/current_main_12_candidates.csv \\
      --net-file result/.../nets/current_main_12.net.xml \\
      --output-dir outputs/global_map/run1 \\
      --seeds 1 2 3 \\
      --max-crosswalks 7
"""

from __future__ import annotations

import argparse
import json
import random
import shutil
import socket
import tempfile
import time
import xml.etree.ElementTree as ET
from collections import defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean as _mean
from typing import Any

import pandas as pd

try:
    import traci  # type: ignore
except ImportError:
    traci = None  # type: ignore

# ── enhanced smoke에서 재사용 ────────────────────────────────────────────────
from smart_crosswalk_sumo.run_enhanced_smoke_group import (
    ELDERLY_MAX_SPEED,
    EXTENSION_INCREMENT,
    EXTENSION_WINDOW,
    NORMAL_MAX_SPEED,
    VTYPE_ELDERLY,
    VTYPE_NORMAL,
    _LANE_COUNT_CAP,
    _PED_ID_RE,
    _REFERENCE_LANES,
    _TIGHT_CONFLICT_RADIUS_M,
    _TRACE_KEYS,
    _as_float,
    _as_int,
    _build_validation_rows,
    _compute_pet_pair_summary,
    _compute_scaled_ped_counts,
    _exact_weighted_counts,
    _file_sha1_short,
    _file_sha256,
    _inject_elderly_types,
    _load_candidate,
    _should_extend,
    _simple_weighted_vehicle_routes,
    _trace_append,
    _trace_mark_extension,
    _vehicle_count_for_seconds,
    _vehicle_edge_allocation_table,
    _vehicle_route_diversity_metrics,
    _write_minimal_sumocfg,
)
from smart_crosswalk_sumo.run_phase6_recovery_smoke import (
    _build_local_scope,
    _candidate_crossing_roads,
    _compute_pet_event_audit_rows,
    _network_passenger_edge_ids,
    _normalize_road_alias,
    _passenger_lane_count_for_route,
    _phase_aligned_depart_plan,
    _route_match_details,
    _sumo_binary,
    _summarize_local_watcher_pet,
    _vehicle_route_path,
    _write_routes,
    read_net,
)
from smart_crosswalk_sumo.network_utils import lane_allows

# ── constants ────────────────────────────────────────────────────────────────
GLOBAL_SIM_DURATION = 600          # 10분
GLOBAL_MAP_INTERVAL = 10.0         # edge 샘플링 간격 (초)
MAX_CROSSWALKS_DEFAULT = 7
EXTENSION_SEC = EXTENSION_INCREMENT


# ─────────────────────────────────────────────────────────────────────────────
# GlobalEdgeCollector
# ─────────────────────────────────────────────────────────────────────────────
class _GlobalEdgeCollector:
    """전체 passenger edge 대상 주기적 traci 샘플링 누적기."""

    def __init__(self, passenger_edge_ids: list[str]) -> None:
        self.edge_ids = passenger_edge_ids
        # edge_id → {"speed": [], "veh": [], "halt": [], "occ": []}
        self._samples: dict[str, dict[str, list[float]]] = {
            eid: {"speed": [], "veh": [], "halt": [], "occ": []}
            for eid in passenger_edge_ids
        }
        self._timeseries: list[dict[str, Any]] = []
        self._last_sample_t: float = float("-inf")

    def maybe_sample(self, t: float, interval: float, scenario: str, warmup: float) -> None:
        if t - self._last_sample_t < interval - 1e-6:
            return
        self._last_sample_t = t
        total_veh = 0
        total_halt = 0
        speed_vals: list[float] = []
        for eid in self.edge_ids:
            try:
                spd = float(traci.edge.getLastStepMeanSpeed(eid))
                veh = int(traci.edge.getLastStepVehicleNumber(eid))
                hlt = int(traci.edge.getLastStepHaltingNumber(eid))
                occ = float(traci.edge.getLastStepOccupancy(eid))
            except Exception:
                spd = float("nan")
                veh = hlt = 0
                occ = float("nan")
            s = self._samples[eid]
            s["speed"].append(spd)
            s["veh"].append(float(veh))
            s["halt"].append(float(hlt))
            s["occ"].append(occ)
            total_veh += veh
            total_halt += hlt
            if spd == spd:  # not nan
                speed_vals.append(spd)
        self._timeseries.append({
            "t": round(t, 1),
            "policy_t": round(t - warmup, 1),
            "scenario": scenario,
            "total_active_vehicles": total_veh,
            "total_halting_vehicles": total_halt,
            "network_mean_speed": round(_mean(speed_vals), 4) if speed_vals else None,
            "sampled_edge_count": len(self.edge_ids),
        })

    def to_edge_df(self, scenario: str, seed: int) -> pd.DataFrame:
        rows = []
        for eid in self.edge_ids:
            s = self._samples[eid]
            speeds = [v for v in s["speed"] if v == v]  # drop nan
            occs = [v for v in s["occ"] if v == v]
            rows.append({
                "edge_id": eid,
                "scenario": scenario,
                "seed": seed,
                "sample_count": len(s["speed"]),
                "mean_speed": round(_mean(speeds), 4) if speeds else None,
                "min_speed": round(min(speeds), 4) if speeds else None,
                "max_speed": round(max(speeds), 4) if speeds else None,
                "mean_flow": round(_mean(s["veh"]), 4) if s["veh"] else None,
                "max_flow": int(max(s["veh"])) if s["veh"] else None,
                "mean_halting": round(_mean(s["halt"]), 4) if s["halt"] else None,
                "max_halting": int(max(s["halt"])) if s["halt"] else None,
                "mean_occupancy": round(_mean(occs), 4) if occs else None,
            })
        return pd.DataFrame(rows)

    def to_timeseries_df(self) -> pd.DataFrame:
        return pd.DataFrame(self._timeseries)


# ─────────────────────────────────────────────────────────────────────────────
# 핵심 시뮬레이션 실행자 (enhanced smoke 로직 + 전역 맵)
# ─────────────────────────────────────────────────────────────────────────────
def _run_global_scenario(
    candidate_df: pd.DataFrame,
    net_file: Path,
    scenario: str,
    seed: int,
    duration: int,
    step_length: float,
    out_dir: Path,
    sim_tmp_dir: Path,
    include_vehicles: bool,
    extension_sec: float,
    elderly_ratio: "float | dict[str, float]",
    global_veh_file: "Path | None",
    group_name: str = "",
    warmup_sec: int = 0,
    run_profile: str = "custom",
    global_map_interval_sec: float = GLOBAL_MAP_INTERVAL,
    traffic_watch_sample_interval_sec: float = 1.0,
    vehicle_route_cache_meta: "dict[str, Any] | None" = None,
) -> "tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]":
    """Enhanced smoke 로직 + 전역 맵 측정.

    Returns:
        (result_df, global_edge_df, global_timeseries_df)
    """
    if traci is None:
        raise RuntimeError("traci not importable — install SUMO and set SUMO_HOME")

    out_dir.mkdir(parents=True, exist_ok=True)
    sim_tmp_dir.mkdir(parents=True, exist_ok=True)
    t_start = time.time()
    run_start_time = datetime.now(timezone.utc).isoformat()

    ped_file, veh_file, _, ped_records, _ = _write_routes(
        candidate_df, sim_tmp_dir, seed, duration, include_vehicles,
        global_vehicle_file=global_veh_file,
        write_debug=False,
    )
    if ped_file and ped_file.exists():
        _inject_elderly_types(ped_file, elderly_ratio, seed)
    ped_route_artifact = out_dir / "demand_pedestrian.rou.xml"
    if ped_file and ped_file.exists():
        shutil.copy2(ped_file, ped_route_artifact)

    generated_vehicle_count = 0
    if veh_file and veh_file.exists():
        try:
            vr = ET.parse(veh_file).getroot()
            generated_vehicle_count = sum(1 for _ in vr.iter("vehicle"))
        except Exception:
            pass

    cfg_path = _write_minimal_sumocfg(
        sim_tmp_dir / f"global_{scenario}.sumocfg",
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

    # 전역 맵 collector 초기화
    passenger_edge_ids = list(all_net_edges)
    global_collector = _GlobalEdgeCollector(passenger_edge_ids)

    try:
        traci.start(cmd, port=port)
    except Exception as _err:
        import subprocess as _sp
        _probe = _sp.run(cmd, capture_output=True, text=True, timeout=15)
        raise RuntimeError(
            f"SUMO failed (TraCI: {_err}).\n"
            f"stdout: {_probe.stdout.strip()}\nstderr: {_probe.stderr.strip()}"
        ) from _err

    _valid_lanes = set(traci.lane.getIDList())
    step_count = 0
    cids = candidate_df["crosswalk_id"].astype(str).tolist()

    # per-crosswalk watch (enhanced smoke와 동일 구조)
    watch: dict[str, dict[str, Any]] = {}
    for row in candidate_df.itertuples(index=False):
        cid = str(row.crosswalk_id)
        scope = _build_local_scope(net, str(row.crossing_edge_id), 500.0)
        watch[cid] = {
            "tls_id":           str(getattr(row, "tls_id", "") or getattr(row, "tls_id_used", "")),
            "ped_link_index":   int(getattr(row, "ped_link_index", 0) or 0),
            "crossing_edge_id": str(row.crossing_edge_id),
            "route_from_edge":  str(getattr(row, "route_from_edge", "")),
            "route_to_edge":    str(getattr(row, "route_to_edge", "")),
            "crossing_edges":   _candidate_crossing_roads(
                str(row.nearest_junction_id), str(row.crossing_edge_id)
            ),
            "route_path":       set(str(getattr(row, "generated_route_edges", "") or "").split("|")) - {""},
            "local_lane_ids":   set(scope["local_lane_ids"]) & _valid_lanes,
            "local_edge_ids":   set(scope["local_edge_ids"]),
            "conflict_edge_ids": set(scope["conflict_edge_ids"]),
            "conflict_edge_aliases": set(scope["conflict_edge_aliases"]),
            "ped_wait_samples":   [],
            "ped_presence_steps": 0,
            "ped_seen":           set(),
            "elderly_incomplete": 0,
            "crossing_xy": (float(scope["crossing_xy"][0]), float(scope["crossing_xy"][1])),
            "active_ped_entries":    {},
            "ped_intervals":         [],
            "ped_intervals_with_id": [],
            "active_veh_entries":  {},
            "veh_intervals":       [],
            "active_tight_veh_entries": {},
            "tight_veh_intervals":      [],
            "queue_samples":      [],
            "local_speed_samples":     [],
            "local_time_loss_samples": [],
            "local_delay_samples":     [],
            "local_seen_veh_ids":      set(),
            "local_stop_count":        0,
            "prev_veh_stop_state":     {},
            "ped_crossing_audit_rows": [],
            "controlled_lane_ids":     set(),
            "controlled_lane_link_indices": {},
            "_last_ped_link_state":    "",
            # global pipeline에서는 trace CSV 비활성 (성능/용량)
        }

    # TLS controlled lanes 파악
    for cid, w in watch.items():
        tls_id = w["tls_id"]
        ped_link_index = w["ped_link_index"]
        lane_to_links: dict[str, set[int]] = {}
        if tls_id:
            try:
                controlled_links = traci.trafficlight.getControlledLinks(tls_id)
            except Exception:
                controlled_links = []
            for link_index, link_group in enumerate(controlled_links):
                if link_index == ped_link_index:
                    continue
                for link in link_group:
                    if not link:
                        continue
                    lane_id = str(link[0] or "")
                    if lane_id and lane_id in _valid_lanes:
                        lane_to_links.setdefault(lane_id, set()).add(int(link_index))
        w["controlled_lane_ids"] = set(lane_to_links)
        w["controlled_lane_link_indices"] = {
            lid: sorted(ls) for lid, ls in lane_to_links.items()
        }

    # run-level state
    extension_events: list[dict] = []
    extended_this_cycle: set[tuple] = set()
    tls_cycle:      dict[str, int] = {}
    tls_last_phase: dict[str, int] = {}

    veh_depart_t:    dict[str, float] = {}
    veh_travel_times: list[float] = []
    veh_time_losses:  list[float] = []
    arrived_ids:  set[str] = set()
    departed_ids: set[str] = set()
    seen_edges:   set[str] = set()
    teleport_count = 0
    collision_count = 0
    last_veh_waiting: dict[str, float] = {}

    surrounding_lane_count = 0
    try:
        route_based = int(sum(
            _passenger_lane_count_for_route(net, str(getattr(r, "vehicle_route_edges", "") or ""))
            for r in candidate_df.itertuples(index=False)
            if str(getattr(r, "vehicle_generation_action", "") or "") == "generate_vehicle"
        ))
        surrounding_lane_count = route_based if route_based > 0 else len(
            set().union(*(w["local_lane_ids"] for w in watch.values()))
        )
    except Exception:
        surrounding_lane_count = len(
            set().union(*(w["local_lane_ids"] for w in watch.values()))
        ) if watch else 0

    vehicle_route_diversity = _vehicle_route_diversity_metrics(
        veh_file if include_vehicles else None
    )
    last_traffic_sample_t: float | None = None

    # ── step loop ─────────────────────────────────────────────────────────────
    try:
        while True:
            traci.simulationStep()
            t = float(traci.simulation.getTime())
            if t >= float(duration + warmup_sec):
                break
            if t < warmup_sec:
                continue

            step_count += 1

            # 전역 맵 샘플링
            global_collector.maybe_sample(t, global_map_interval_sec, scenario, warmup_sec)

            sample_traffic = (
                last_traffic_sample_t is None
                or (t - last_traffic_sample_t) >= max(
                    float(traffic_watch_sample_interval_sec), float(step_length)
                ) - 1e-9
            )
            if sample_traffic:
                last_traffic_sample_t = t

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

            try:
                all_veh_ids = list(traci.vehicle.getIDList())
            except Exception:
                all_veh_ids = []

            step_conflict_vehs: dict[str, set] = {cid: set() for cid in watch}
            step_tight_conflict_vehs: dict[str, set] = {cid: set() for cid in watch}
            step_local_speeds:  dict[str, list] = {cid: [] for cid in watch}
            step_local_waiting: dict[str, list] = {cid: [] for cid in watch}
            step_local_veh_cnt: dict[str, int]  = {cid: 0 for cid in watch}

            for vid in all_veh_ids:
                try:
                    road_id = str(traci.vehicle.getRoadID(vid))
                    lane_id = str(traci.vehicle.getLaneID(vid))
                except Exception:
                    continue
                _veh_aliases = _normalize_road_alias(road_id) | _normalize_road_alias(lane_id)

                needs_motion = False
                for cid, w in watch.items():
                    in_local = sample_traffic and (
                        lane_id in w["local_lane_ids"] or road_id in w["local_edge_ids"]
                    )
                    in_conflict = bool(_veh_aliases & w["conflict_edge_aliases"])
                    if in_local or in_conflict:
                        needs_motion = True
                        break

                if needs_motion:
                    try:
                        speed = float(traci.vehicle.getSpeed(vid))
                    except Exception:
                        speed = float("nan")
                    try:
                        _wt = float(traci.vehicle.getAccumulatedWaitingTime(vid))
                        last_veh_waiting[vid] = _wt
                    except Exception:
                        _wt = last_veh_waiting.get(vid, float("nan"))
                    try:
                        _tl = float(traci.vehicle.getTimeLoss(vid))
                    except Exception:
                        _tl = float("nan")
                else:
                    speed = _wt = _tl = float("nan")

                for cid, w in watch.items():
                    in_local = sample_traffic and (
                        lane_id in w["local_lane_ids"] or road_id in w["local_edge_ids"]
                    )
                    in_conflict = bool(_veh_aliases & w["conflict_edge_aliases"])
                    controlled_link_indices = w["controlled_lane_link_indices"].get(lane_id, [])

                    if in_local:
                        w["local_seen_veh_ids"].add(vid)
                        if not (speed != speed):
                            w["local_speed_samples"].append(speed)
                            stop_now = speed <= 0.1
                            if stop_now and not w["prev_veh_stop_state"].get(vid, False):
                                w["local_stop_count"] += 1
                            w["prev_veh_stop_state"][vid] = stop_now
                        if not (_tl != _tl):
                            w["local_time_loss_samples"].append(_tl)
                        if not (_wt != _wt):
                            w["local_delay_samples"].append(_wt)
                        step_local_speeds[cid].append(speed if not (speed != speed) else 0.0)
                        step_local_waiting[cid].append(_wt if not (_wt != _wt) else 0.0)
                        step_local_veh_cnt[cid] += 1

                    if in_conflict:
                        step_conflict_vehs[cid].add(vid)
                        if vid not in w["active_veh_entries"]:
                            w["active_veh_entries"][vid] = {
                                "vehicle_id": vid,
                                "enter_time": float(t),
                                "enter_road_id": road_id,
                                "enter_lane_id": lane_id,
                                "controlled_signal_affected": bool(controlled_link_indices),
                            }
                        entry = w["active_veh_entries"][vid]
                        entry["exit_road_id"] = road_id
                        entry["exit_speed"] = round(speed, 4) if not (speed != speed) else None
                        # tight conflict zone
                        try:
                            pos = traci.vehicle.getPosition(vid)
                            cx, cy = w["crossing_xy"]
                            if ((pos[0] - cx) ** 2 + (pos[1] - cy) ** 2) ** 0.5 <= _TIGHT_CONFLICT_RADIUS_M:
                                step_tight_conflict_vehs[cid].add(vid)
                                if vid not in w["active_tight_veh_entries"]:
                                    w["active_tight_veh_entries"][vid] = {
                                        "vehicle_id": vid,
                                        "enter_time": float(t),
                                    }
                        except Exception:
                            pass

            # flush vehicles that left conflict/tight zone
            for cid, w in watch.items():
                for vid in list(w["active_veh_entries"].keys()):
                    if vid not in step_conflict_vehs[cid]:
                        entry = dict(w["active_veh_entries"].pop(vid))
                        entry["exit_time"] = float(t)
                        w["veh_intervals"].append(entry)
                for vid in list(w["active_tight_veh_entries"].keys()):
                    if vid not in step_tight_conflict_vehs[cid]:
                        entry = dict(w["active_tight_veh_entries"].pop(vid))
                        entry["exit_time"] = float(t)
                        w["tight_veh_intervals"].append(entry)

            tls_step_cache: dict[str, tuple[int, str, float]] = {}

            # per-crosswalk step work
            for cid, w in watch.items():
                tls_id         = w["tls_id"]
                ped_link_index = w["ped_link_index"]
                crossing_edges = w["crossing_edges"]

                # queue
                if sample_traffic:
                    q = 0
                    for lid in w["local_lane_ids"]:
                        try:
                            q += traci.lane.getLastStepHaltingNumber(lid)
                        except Exception:
                            pass
                    w["queue_samples"].append(float(q))

                # pedestrian presence
                ped_near = 0
                step_crossing_pids: set[str] = set()
                for pid in all_ped_ids:
                    try:
                        road = traci.person.getRoadID(pid)
                        lane = traci.person.getLaneID(pid)
                    except Exception:
                        continue
                    hit, _ = _route_match_details(road, lane, crossing_edges, w["route_path"], tls_id)
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
                # flush peds
                for pid in list(w["active_ped_entries"].keys()):
                    if pid not in step_crossing_pids:
                        enter = w["active_ped_entries"].pop(pid)
                        w["ped_intervals"].append({"enter_time": float(enter), "exit_time": t})
                        w["ped_intervals_with_id"].append({
                            "pedestrian_id": pid,
                            "enter_time": float(enter),
                            "exit_time": t,
                        })

                # elderly incomplete
                if extension_sec > 0 and tls_id:
                    try:
                        if tls_id not in tls_step_cache:
                            tls_step_cache[tls_id] = (
                                int(traci.trafficlight.getPhase(tls_id)),
                                traci.trafficlight.getRedYellowGreenState(tls_id),
                                float(traci.trafficlight.getNextSwitch(tls_id) - t),
                            )
                        _, st_chk, rem_chk = tls_step_cache[tls_id]
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

                if not tls_id:
                    continue
                try:
                    if tls_id not in tls_step_cache:
                        tls_step_cache[tls_id] = (
                            int(traci.trafficlight.getPhase(tls_id)),
                            traci.trafficlight.getRedYellowGreenState(tls_id),
                            float(traci.trafficlight.getNextSwitch(tls_id) - t),
                        )
                    phase, state, remaining = tls_step_cache[tls_id]
                    last_ph = tls_last_phase.get(tls_id, phase)
                    if phase < last_ph:
                        tls_cycle[tls_id] = tls_cycle.get(tls_id, 0) + 1
                    tls_last_phase[tls_id] = phase
                    cycle = tls_cycle.get(tls_id, 0)

                    ped_link_state = state[ped_link_index] if ped_link_index < len(state) else ""
                    w["_last_ped_link_state"] = ped_link_state
                    non_ped_green = sum(
                        1 for i, s in enumerate(state)
                        if i != ped_link_index and s in {"G", "g", "s"}
                    )
                    is_ped_only = ped_link_state in {"G", "g"} and non_ped_green == 0

                    skip = _should_extend(
                        scenario, tls_id, state, ped_link_index, is_ped_only,
                        remaining, ped_near, extended_this_cycle, phase, cid,
                    )
                    if skip == "" and extension_sec > 0:
                        traci.trafficlight.setPhaseDuration(tls_id, remaining + extension_sec)
                        extended_this_cycle.add((tls_id, phase, cid))
                        try:
                            _remaining_after = float(traci.trafficlight.getNextSwitch(tls_id) - t)
                        except Exception:
                            _remaining_after = remaining + extension_sec
                        tls_step_cache[tls_id] = (phase, state, _remaining_after)
                        extension_events.append({
                            "crosswalk_id":    cid,
                            "time":            round(t, 1),
                            "policy_time":     round(t - warmup_sec, 1),
                            "tls_id":          tls_id,
                            "phase":           phase,
                            "cycle":           cycle,
                            "state":           state,
                            "remaining_before": round(remaining, 1),
                            "extension_sec":   extension_sec,
                            "ped_near":        ped_near,
                        })
                except Exception:
                    pass

    finally:
        try:
            traci.close()
        except Exception:
            pass

    # flush remaining
    for cid, w in watch.items():
        for pid, enter in list(w["active_ped_entries"].items()):
            w["ped_intervals"].append({"enter_time": float(enter), "exit_time": float(t)})
            w["ped_intervals_with_id"].append({
                "pedestrian_id": pid,
                "enter_time": float(enter),
                "exit_time": float(t),
            })
        w["active_ped_entries"].clear()
        for vid, entry in list(w["active_veh_entries"].items()):
            e = dict(entry)
            e["exit_time"] = float(t)
            w["veh_intervals"].append(e)
        w["active_veh_entries"].clear()
        for vid, entry in list(w["active_tight_veh_entries"].items()):
            e = dict(entry)
            e["exit_time"] = float(t)
            w["tight_veh_intervals"].append(e)
        w["active_tight_veh_entries"].clear()

    # ── 출력 디렉토리 ─────────────────────────────────────────────────────────
    csv_dir = out_dir / "csv"
    log_dir = out_dir / "log"
    csv_dir.mkdir(parents=True, exist_ok=True)
    log_dir.mkdir(parents=True, exist_ok=True)

    net_tt   = (_mean(veh_travel_times)) if veh_travel_times else None
    net_loss = (_mean(veh_time_losses))  if veh_time_losses  else None
    elapsed  = round(time.time() - t_start, 2)
    run_end_time = datetime.now(timezone.utc).isoformat()
    net_edge_total = len(all_net_edges) if all_net_edges else 1

    rows: list[dict] = []
    for row in candidate_df.itertuples(index=False):
        cid = str(row.crosswalk_id)
        w   = watch[cid]
        exts = [e for e in extension_events if e["crosswalk_id"] == cid]
        qs   = w["queue_samples"]
        pws  = w["ped_wait_samples"]
        cw_elderly = (elderly_ratio.get(cid, 0.15) if isinstance(elderly_ratio, dict)
                      else float(elderly_ratio))

        pet = _summarize_local_watcher_pet(
            ped_intervals=w["ped_intervals"],
            veh_intervals=w["veh_intervals"],
            pedestrian_crossing_count=len(w["ped_seen"]),
            conflict_edge_count=len(w["conflict_edge_ids"]),
        )
        cz_pet = _summarize_local_watcher_pet(
            ped_intervals=w["ped_intervals"],
            veh_intervals=w["tight_veh_intervals"],
            pedestrian_crossing_count=len(w["ped_seen"]),
            conflict_edge_count=len(w["conflict_edge_ids"]),
        )
        pair_diag = _compute_pet_pair_summary(
            ped_intervals=w["ped_intervals"],
            veh_intervals=w["veh_intervals"],
        )
        pet_severe = float(pet.get("very_risky_crossing_count", 0) or 0)
        cw_eld_inc = float(w["elderly_incomplete"])
        safety_risk = pet_severe + cw_eld_inc * 2.0

        ls_sp = w["local_speed_samples"]
        ls_tl = w["local_time_loss_samples"]
        ls_dl = w["local_delay_samples"]
        l500_speed = round(_mean(ls_sp), 4) if ls_sp else None
        l500_tl    = round(_mean(ls_tl), 4) if ls_tl else None
        l500_delay = round(_mean(ls_dl), 4) if ls_dl else None

        ped_wt_mean = round(_mean(pws), 3) if pws else 0.0
        ped_wt_max  = round(max(pws), 3) if pws else 0.0
        l_delay = l500_delay if l500_delay is not None else (
            round(net_loss, 2) if net_loss is not None else None
        )

        rows.append({
            "run_name":           f"global_{scenario}_seed{seed}",
            "run_profile":        run_profile,
            "crosswalk_id":       cid,
            "scenario":           scenario,
            "seed":               seed,
            "output_dir":         str(out_dir),
            "run_start_time":     run_start_time,
            "run_end_time":       run_end_time,
            "elapsed_sec":        elapsed,
            "completed":          True,
            "sim_duration":       duration,
            "warmup":             warmup_sec,
            "step_length":        step_length,
            "step_count":         step_count,
            "tls_id_used":        w["tls_id"],
            "ped_link_index":     w["ped_link_index"],
            "crossing_edge_id":   w["crossing_edge_id"],
            # pedestrian
            "ped_crossing_person_count":   len(w["ped_seen"]),
            "pedestrian_crossing_count":   len(w["ped_seen"]),
            "ped_crossing_presence_steps": w["ped_presence_steps"],
            "ped_wait_time_mean":          ped_wt_mean,
            "ped_wait_time_max":           ped_wt_max,
            "average_pedestrian_wait_time": ped_wt_mean,
            "elderly_incomplete_crossings": w["elderly_incomplete"],
            "crosswalk_elderly_ratio":      cw_elderly,
            # extension
            "extension_count":             len(exts),
            "pedestrian_green_extension_count": len(exts),
            "total_extension_sec":         len(exts) * extension_sec,
            # queue / local
            "avg_queue_length":    round(_mean(qs), 3) if qs else 0.0,
            "max_queue_length":    max(qs) if qs else 0.0,
            "local_500m_vehicle_count": len(w["local_seen_veh_ids"]),
            "local_500m_mean_speed":    l500_speed,
            "local_500m_mean_time_loss": l500_tl,
            "local_500m_avg_delay_sec": l500_delay,
            "local_500m_stop_count":    w["local_stop_count"],
            "surrounding_road_delay_sec": l_delay,
            "veh_delay_mean":     l_delay,
            "veh_avg_delay_sec":  l_delay,
            # global network
            "network_mean_travel_time": round(net_tt,   2) if net_tt   is not None else None,
            "network_mean_time_loss":   round(net_loss, 2) if net_loss is not None else None,
            "network_arrived_vehicles": len(arrived_ids),
            "network_departed_vehicles": len(departed_ids),
            "network_teleport_count":   teleport_count,
            "network_collision_count":  collision_count,
            "network_vehicle_edge_coverage_ratio":
                round(len(seen_edges) / net_edge_total, 4) if net_edge_total else 0.0,
            "network_passenger_edge_count": len(passenger_edge_ids),
            "global_map_interval_sec": global_map_interval_sec,
            # vehicles
            "generated_vehicle_count": generated_vehicle_count,
            "vehicle_route_sha256": _file_sha256(global_veh_file),
            "vehicle_route_count": int(vehicle_route_diversity.get("vehicle_route_count", 0)),
            "unique_vehicle_route_count": int(vehicle_route_diversity.get("unique_vehicle_route_count", 0)),
            "surrounding_lane_count": surrounding_lane_count,
            "road_lanes": float(getattr(row, "road_lanes", _REFERENCE_LANES)),
            # PET
            "pet_source":               pet.get("pet_source", "local_watcher"),
            "pet_available":            pet.get("pet_available", False),
            "pet_event_count":          pet.get("pet_event_count"),
            "very_risky_crossing_count": pet.get("very_risky_crossing_count"),
            "risky_crossing_count":     pet.get("risky_crossing_count"),
            "pet_min":                  pet.get("pet_min"),
            "pet_p10":                  pet.get("pet_p10"),
            "pet_mean":                 pet.get("pet_mean"),
            "cz_pet_event_count":       cz_pet.get("pet_event_count"),
            "cz_pet_min":               cz_pet.get("pet_min"),
            "cz_pet_mean":              cz_pet.get("pet_mean"),
            "pair_overlap_count":       pair_diag.get("pair_overlap_count"),
            "pair_pet_mean":            pair_diag.get("pair_pet_mean"),
            "safety_risk_score":        round(safety_risk, 4),
            "run_group":                group_name,
        })

    result_df = pd.DataFrame(rows)
    result_df.to_csv(csv_dir / "simulation_result.csv", index=False, encoding="utf-8-sig")

    # 전역 맵 출력
    global_edge_df = global_collector.to_edge_df(scenario, seed)
    global_edge_df.to_csv(csv_dir / "global_edge_metrics.csv", index=False, encoding="utf-8-sig")

    global_ts_df = global_collector.to_timeseries_df()
    global_ts_df.to_csv(csv_dir / "global_network_timeseries.csv", index=False, encoding="utf-8-sig")

    # run_metadata.json
    meta = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "runner": "run_global_map_pipeline",
        "run_profile": run_profile,
        "scenario": scenario,
        "seed": seed,
        "sim_duration": duration,
        "warmup_sec": warmup_sec,
        "step_length": step_length,
        "global_map_interval_sec": global_map_interval_sec,
        "passenger_edge_count": len(passenger_edge_ids),
        "crosswalk_count": len(rows),
        "elapsed_sec": elapsed,
        "group_name": group_name,
        "net_file": str(net_file),
    }
    (log_dir / "run_metadata.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    return result_df, global_edge_df, global_ts_df


# ─────────────────────────────────────────────────────────────────────────────
# 전역 맵 edge 비교 (baseline vs smart)
# ─────────────────────────────────────────────────────────────────────────────
def _compare_global_edge_metrics(
    baseline_edge_df: pd.DataFrame,
    smart_edge_df: pd.DataFrame,
    out_path: Path,
) -> pd.DataFrame:
    if baseline_edge_df.empty or smart_edge_df.empty:
        return pd.DataFrame()
    b = baseline_edge_df.rename(columns={
        c: f"{c}_baseline" for c in baseline_edge_df.columns if c != "edge_id"
    })
    s = smart_edge_df.rename(columns={
        c: f"{c}_smart" for c in smart_edge_df.columns if c != "edge_id"
    })
    comp = b.merge(s, on="edge_id", how="outer")
    for metric in ["mean_speed", "mean_flow", "mean_halting", "mean_occupancy"]:
        b_col = f"{metric}_baseline"
        s_col = f"{metric}_smart"
        if b_col in comp.columns and s_col in comp.columns:
            b_num = pd.to_numeric(comp[b_col], errors="coerce")
            s_num = pd.to_numeric(comp[s_col], errors="coerce")
            comp[f"delta_{metric}"] = (s_num - b_num).round(4)
            comp[f"pct_delta_{metric}"] = (
                ((s_num - b_num) / b_num.replace(0, float("nan"))) * 100
            ).round(2)
    comp.to_csv(out_path, index=False, encoding="utf-8-sig")
    return comp


# ─────────────────────────────────────────────────────────────────────────────
# 그룹 실행 (baseline + smart, 전역 맵 포함)
# ─────────────────────────────────────────────────────────────────────────────
def _run_global_group(
    candidate_csv: Path,
    net_file: Path,
    seed: int,
    out_dir: Path,
    ped_count: int,
    elderly_ratio: float,
    duration: int,
    step_length: float,
    max_crosswalks: int = MAX_CROSSWALKS_DEFAULT,
    crosswalk_id: "str | None" = None,
    warmup_sec: int = 0,
    run_profile: str = "custom",
    ped_demand_scale_mode: str = "none",
    ped_demand_min_target: int = 30,
    ped_demand_max_cap: int = 200,
    ped_demand_source: "str | None" = None,
    scenario_mode: str = "both",
    ped_repeat_spacing_sec: float = 2.0,
    vehicle_route_cache_dir: "Path | None" = None,
    global_map_interval_sec: float = GLOBAL_MAP_INTERVAL,
    traffic_watch_sample_interval_sec: float = 1.0,
) -> pd.DataFrame:
    group_name = candidate_csv.stem.replace("_candidates", "")
    candidate_df_raw = _load_candidate(candidate_csv, ped_count, ped_repeat_spacing_sec=ped_repeat_spacing_sec)

    # 단일 횡단보도 필터
    if crosswalk_id:
        candidate_df_raw = candidate_df_raw[
            candidate_df_raw["crosswalk_id"].astype(str) == crosswalk_id
        ].reset_index(drop=True)
    else:
        # max_crosswalks 제한: risk_score 또는 순서 기준
        if len(candidate_df_raw) > max_crosswalks:
            if "risk_score" in candidate_df_raw.columns:
                candidate_df_raw = candidate_df_raw.nlargest(max_crosswalks, "risk_score").reset_index(drop=True)
            else:
                candidate_df_raw = candidate_df_raw.head(max_crosswalks).reset_index(drop=True)
            print(
                f"[global map] max_crosswalks={max_crosswalks} → {len(candidate_df_raw)} 후보 선택",
                flush=True,
            )

    # phase-aligned depart
    depart_plan = _phase_aligned_depart_plan(
        net_file, candidate_df_raw,
        ped_repeat_count=ped_count,
        ped_repeat_spacing_sec=ped_repeat_spacing_sec,
    )
    _plan_cols = ["crosswalk_id", "depart_time"]
    if "cycle_duration" in depart_plan.columns:
        _plan_cols = ["crosswalk_id", "depart_time", "cycle_duration"]
    candidate_df = candidate_df_raw.merge(
        depart_plan[_plan_cols].rename(columns={"depart_time": "ped_depart_time"}),
        on="crosswalk_id", how="left",
    )
    if "ped_depart_time" in candidate_df.columns:
        candidate_df["ped_depart_time"] = (
            candidate_df["ped_depart_time"] + warmup_sec
        ).clip(lower=0.0)

    # per-crosswalk elderly ratio
    cw_elderly_ratios: dict[str, float] = {}
    if "crosswalk_elderly_ratio" in candidate_df_raw.columns:
        cw_elderly_ratios = dict(
            zip(
                candidate_df_raw["crosswalk_id"].astype(str),
                candidate_df_raw["crosswalk_elderly_ratio"].astype(float),
            )
        )

    # 보행자 수요 스케일링
    _scaled_counts, _ped_multiplier = _compute_scaled_ped_counts(
        candidate_df,
        base_ped_count=ped_count,
        mode=ped_demand_scale_mode,
        min_target=ped_demand_min_target,
        max_cap=ped_demand_max_cap,
        source_path=ped_demand_source,
    )
    candidate_df = candidate_df.copy()
    candidate_df["original_ped_count"]           = ped_count
    candidate_df["scaled_ped_count"]             = candidate_df["crosswalk_id"].astype(str).map(_scaled_counts)
    candidate_df["pedestrian_demand_multiplier"] = round(_ped_multiplier, 4)
    candidate_df["demand_profile"] = (
        "peak_pedestrian_scaled" if ped_demand_scale_mode != "none" else "main_realistic_stress"
    )
    candidate_df["ped_repeat_count"] = candidate_df["scaled_ped_count"].fillna(ped_count).astype(int)

    # vehicle routes (seed 단위 공유)
    veh_route_dir = out_dir / "_vehicle_routes" / f"seed{seed:05d}"
    global_veh_file, vehicle_route_cache_meta = _simple_weighted_vehicle_routes(
        net_file=net_file,
        sim_duration=duration,
        seed=seed,
        out_dir=veh_route_dir,
        warmup_sec=warmup_sec,
        vehicle_route_cache_dir=vehicle_route_cache_dir,
    )

    _all_pairs = [("baseline", 0.0), ("smart", EXTENSION_SEC)]
    _run_pairs = {
        "both":          _all_pairs,
        "baseline_only": [("baseline", 0.0)],
        "smart_only":    [("smart", EXTENSION_SEC)],
    }.get(scenario_mode, _all_pairs)

    scenario_dfs: dict[str, pd.DataFrame] = {}
    scenario_edge_dfs: dict[str, pd.DataFrame] = {}

    for scenario, ext_sec in _run_pairs:
        result_dir = out_dir / scenario / f"seed{seed:05d}"
        result_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=f"global_{scenario}_{seed}_") as tmp:
            sim_tmp = Path(tmp)
            df, edge_df, ts_df = _run_global_scenario(
                candidate_df, net_file, scenario, seed, duration, step_length,
                out_dir=result_dir,
                sim_tmp_dir=sim_tmp,
                include_vehicles=True,
                extension_sec=ext_sec,
                elderly_ratio=cw_elderly_ratios if cw_elderly_ratios else elderly_ratio,
                global_veh_file=global_veh_file,
                group_name=group_name,
                warmup_sec=warmup_sec,
                run_profile=run_profile,
                global_map_interval_sec=global_map_interval_sec,
                traffic_watch_sample_interval_sec=traffic_watch_sample_interval_sec,
                vehicle_route_cache_meta=vehicle_route_cache_meta,
            )
        scenario_dfs[scenario] = df
        scenario_edge_dfs[scenario] = edge_df
        n_edge_samples = len(edge_df)
        print(
            f"  [{scenario}] seed{seed:05d} done — "
            f"{len(df)} crosswalk rows, {n_edge_samples} edge rows "
            f"→ {result_dir / 'csv' / 'simulation_result.csv'}",
            flush=True,
        )

    # 전역 맵 edge-level 비교 (baseline vs smart 둘 다 있을 때)
    if scenario_mode == "both":
        b_edge = scenario_edge_dfs.get("baseline", pd.DataFrame())
        s_edge = scenario_edge_dfs.get("smart", pd.DataFrame())
        comp_path = out_dir / "global_edge_comparison" / f"seed{seed:05d}_global_edge_comparison.csv"
        comp_path.parent.mkdir(parents=True, exist_ok=True)
        _compare_global_edge_metrics(b_edge, s_edge, comp_path)
        print(f"  [global map] edge comparison → {comp_path}", flush=True)

    all_df = pd.concat(list(scenario_dfs.values()), ignore_index=True)
    return all_df


# ─────────────────────────────────────────────────────────────────────────────
# 집계: seed별 global edge comparison 합산
# ─────────────────────────────────────────────────────────────────────────────
def _aggregate_global_comparisons(output_dir: Path) -> None:
    frames: list[pd.DataFrame] = []
    for path in sorted((output_dir / "global_edge_comparison").glob("seed*_global_edge_comparison.csv")):
        try:
            frames.append(pd.read_csv(path))
        except Exception:
            continue
    if not frames:
        return
    agg = pd.concat(frames, ignore_index=True)
    # edge별 평균 delta
    numeric_cols = agg.select_dtypes("number").columns.tolist()
    if "edge_id" in agg.columns and numeric_cols:
        summary = agg.groupby("edge_id")[numeric_cols].mean().round(4).reset_index()
        summary.to_csv(output_dir / "global_edge_comparison_agg.csv", index=False, encoding="utf-8-sig")
    agg.to_csv(output_dir / "global_edge_comparison_all_seeds.csv", index=False, encoding="utf-8-sig")
    print(f"[global map] aggregated edge comparison → {output_dir / 'global_edge_comparison_agg.csv'}", flush=True)


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────
def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--candidate-csv", required=True, type=Path)
    parser.add_argument("--net-file",      required=True, type=Path)
    parser.add_argument("--output-dir",    required=True, type=Path)
    # seed
    parser.add_argument("--seed",  type=int, default=-1,
                        help="-1 = random seed (단일 실행)")
    parser.add_argument("--seeds", type=int, nargs="*", default=None,
                        help="명시 seed 목록. 예: --seeds 1 2 3")
    # demand
    parser.add_argument("--ped-count",     type=int,   default=20)
    parser.add_argument("--elderly-ratio", type=float, default=0.2)
    # simulation
    parser.add_argument("--sim-duration",  type=int,   default=GLOBAL_SIM_DURATION,
                        help=f"시뮬레이션 시간(초). 기본 {GLOBAL_SIM_DURATION}s (10분)")
    parser.add_argument("--step-length",   type=float, default=0.1)
    parser.add_argument("--warmup-sec",    type=int,   default=0)
    # crosswalk 수 제한
    parser.add_argument("--max-crosswalks", type=int, default=MAX_CROSSWALKS_DEFAULT,
                        help=f"최대 횡단보도 수 (기본 {MAX_CROSSWALKS_DEFAULT}). "
                             "risk_score 상위 N개 선택. 없으면 순서 기준.")
    parser.add_argument("--crosswalk-id", type=str, default=None,
                        help="단일 횡단보도 검증 모드")
    # global map
    parser.add_argument("--global-map-interval", type=float, default=GLOBAL_MAP_INTERVAL,
                        help=f"전역 edge 샘플링 간격(초). 기본 {GLOBAL_MAP_INTERVAL}s")
    parser.add_argument("--traffic-watch-interval", type=float, default=1.0,
                        help="로컬 traffic watch 샘플링 간격(초). 기본 1.0s")
    # scenario
    parser.add_argument("--scenario-mode",
                        choices=["both", "baseline_only", "smart_only"],
                        default="both")
    parser.add_argument("--ped-repeat-spacing-sec", type=float, default=2.0)
    # demand scaling
    parser.add_argument("--ped-demand-source",     type=str, default=None)
    parser.add_argument("--ped-demand-scale-mode", type=str, default="none",
                        choices=["none", "source_min_to_target"])
    parser.add_argument("--min-ped-count-target",  type=int, default=30)
    parser.add_argument("--max-ped-count-cap",     type=int, default=200)
    parser.add_argument("--vehicle-route-cache-dir", type=Path, default=None)

    args = parser.parse_args()

    # seed 해석
    if args.seeds is not None:
        seeds = args.seeds
    elif args.seed == -1:
        seeds = [random.randint(1, 9999)]
        print(f"Random seed: {seeds[0]}", flush=True)
    else:
        seeds = [args.seed]

    candidate_df_meta = pd.read_csv(args.candidate_csv, encoding="utf-8-sig")
    print(
        f"[global map] candidate: {len(candidate_df_meta)} 행, "
        f"max_crosswalks={args.max_crosswalks}, sim={args.sim_duration}s, "
        f"seeds={seeds}",
        flush=True,
    )

    all_frames: list[pd.DataFrame] = []
    for seed in seeds:
        print(f"\n=== seed {seed} ===", flush=True)
        df = _run_global_group(
            candidate_csv=args.candidate_csv,
            net_file=args.net_file,
            seed=seed,
            out_dir=args.output_dir,
            ped_count=args.ped_count,
            elderly_ratio=args.elderly_ratio,
            duration=args.sim_duration,
            step_length=args.step_length,
            max_crosswalks=args.max_crosswalks,
            crosswalk_id=args.crosswalk_id,
            warmup_sec=args.warmup_sec,
            run_profile="custom",
            ped_demand_scale_mode=args.ped_demand_scale_mode,
            ped_demand_min_target=args.min_ped_count_target,
            ped_demand_max_cap=args.max_ped_count_cap,
            ped_demand_source=args.ped_demand_source,
            scenario_mode=args.scenario_mode,
            ped_repeat_spacing_sec=args.ped_repeat_spacing_sec,
            vehicle_route_cache_dir=args.vehicle_route_cache_dir,
            global_map_interval_sec=args.global_map_interval,
            traffic_watch_sample_interval_sec=args.traffic_watch_interval,
        )
        all_frames.append(df)

    _aggregate_global_comparisons(args.output_dir)
    result_df = pd.concat(all_frames, ignore_index=True)

    if not args.crosswalk_id:
        csv_out = args.output_dir / "global_simulation_result.csv"
        result_df.to_csv(csv_out, index=False, encoding="utf-8-sig")
        print(f"\nCSV → {csv_out}  ({len(result_df)} rows)", flush=True)
        print(f"전역 맵 비교 → {args.output_dir / 'global_edge_comparison_agg.csv'}", flush=True)
    else:
        print(f"\nSingle-crosswalk mode 완료 → {args.output_dir}", flush=True)


if __name__ == "__main__":
    main()
