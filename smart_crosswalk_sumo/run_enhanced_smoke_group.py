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
import hashlib
import json
import random
import shutil
import socket
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from collections import deque
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
    _compute_pet_event_audit_rows,
    _compute_pet_pair_summary,
    _network_passenger_edge_ids,
    _normalize_road_alias,
    _passenger_lane_count_for_route,
    _phase_aligned_depart_plan,
    _route_match_details,
    _sumo_binary,
    _summarize_local_watcher_pet,
    _vehicle_route_path,
    _vehicle_route_diversity_metrics,
    _write_routes,
    _write_sumocfg,
    read_net,
)
from smart_crosswalk_sumo.network_utils import lane_allows
from smart_crosswalk_sumo.vehicle_demand_policy import (
    VEHICLE_POLICY_TOTAL_COUNT_600S,
    VEHICLE_POLICY_TOTAL_FLOW_VPH,
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
_LANE_COUNT_CAP     = 6
_STEPWISE_CSV       = Path(__file__).resolve().parent.parent / "crosswalk_stepwise_result_50m.csv"
_TRACE_KEYS         = [
    "tls_trace_rows",
    "lane_trace_rows",
    "controlled_lane_trace_rows",
    "controlled_lane_step_rows",
    "ped_trace_rows",
]
_DIAGNOSTIC_PET_INTERVAL_COLUMNS = [
    "diagnostic_pet_interval_matched_count",
    "diagnostic_vehicle_exit_delta_mean",
    "diagnostic_vehicle_exit_delta_p50",
    "diagnostic_vehicle_exit_delta_min",
    "diagnostic_vehicle_exit_delta_max",
    "diagnostic_vehicle_exit_delayed_count",
    "diagnostic_vehicle_exit_delayed_ge_1s_count",
    "diagnostic_vehicle_exit_delayed_ge_5s_count",
    "diagnostic_vehicle_exit_delta_status",
]
PET_OBSERVABILITY_GLOBAL_ONLY = "global_only"
PET_OBSERVABILITY_TARGETED    = "targeted_controlled"
PET_OBSERVABILITY_RANDOMIZED   = "randomized_controlled"
_TARGETED_DIAGNOSTIC_VEHICLES_PER_CROSSWALK = 6
_TARGETED_DIAGNOSTIC_DEPART_OFFSETS = [-9.0, -6.0, -3.0, 0.0, 3.0, 6.0]
_RANDOMIZED_DIAGNOSTIC_MAX_VEHICLES_PER_CROSSWALK = 20
_RANDOMIZED_DIAGNOSTIC_DEFAULT_VEHICLES_PER_CROSSWALK = 18
_RANDOMIZED_DIAGNOSTIC_DEFAULT_JITTER_SEC = 25.0
_TIGHT_CONFLICT_RADIUS_M: float = 8.0  # position 기반 tight conflict zone 반경

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


def _file_sha1_short(path: Path) -> str:
    h = hashlib.sha1()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()[:16]


def _file_sha256(path: Path | str | None) -> str:
    if path is None:
        return ""
    p = Path(path)
    if not p.exists():
        return ""
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _trace_append(
    watch_row: dict[str, Any],
    key: str,
    row: dict[str, Any],
    t: float,
    trace_mode: str,
) -> None:
    if trace_mode == "off":
        return
    if trace_mode == "full":
        watch_row[key].append(row)
        return
    buf = watch_row.get(f"{key}_buffer")
    if buf is not None:
        buf.append(row)
    if t <= float(watch_row.get("trace_capture_until", -1.0)):
        watch_row[key].append(row)


def _trace_mark_extension(watch_row: dict[str, Any], t: float, post_window_sec: float) -> None:
    watch_row["trace_capture_until"] = max(
        float(watch_row.get("trace_capture_until", -1.0)),
        float(t) + float(post_window_sec),
    )
    for key in _TRACE_KEYS:
        buf = watch_row.get(f"{key}_buffer")
        if buf:
            watch_row[key].extend(list(buf))


def _write_trace_csv(path: Path, rows: list[dict[str, Any]], trace_mode: str) -> None:
    if trace_mode == "off":
        return
    df = pd.DataFrame(rows)
    if trace_mode == "minimal" and not df.empty:
        df = df.drop_duplicates()
    df.to_csv(path, index=False)


def _run_enhanced_scenario(
    candidate_df: pd.DataFrame,
    net_file: Path,
    scenario: str,
    seed: int,
    duration: int,
    step_length: float,
    out_dir: Path,          # csv/ (결과) + log/ (메타데이터) 하위 폴더 생성됨
    sim_tmp_dir: Path,      # sumocfg + demand XMLs written here (no persistence)
    include_vehicles: bool,
    extension_sec: float,
    elderly_ratio: "float | dict[str, float]",
    global_veh_file: "Path | None",
    group_name: str = "",
    warmup_sec: int = 0,
    run_profile: str = "custom",
    trace_mode: str = "full",
    trace_pre_window_sec: float = 10.0,
    trace_post_window_sec: float = 10.0,
    traffic_watch_sample_interval_sec: float = 0.1,
    vehicle_route_cache_meta: "dict[str, Any] | None" = None,
    pet_observability_mode: str = PET_OBSERVABILITY_GLOBAL_ONLY,
    forced_extension_policy_t: "float | None" = None,
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
    ped_route_artifact = out_dir / "demand_pedestrian.rou.xml"
    if ped_file and ped_file.exists():
        shutil.copy2(ped_file, ped_route_artifact)

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
            # topology
            "crossing_xy": (float(scope["crossing_xy"][0]), float(scope["crossing_xy"][1])),
            # PET interval tracking (local_watcher 방식, SSM 없이)
            "active_ped_entries":    {},  # pid → enter_time
            "ped_intervals":         [],  # {enter_time, exit_time}
            "ped_intervals_with_id": [],  # {pedestrian_id, enter_time, exit_time} — audit용
            "active_veh_entries": {},     # vid → interval metadata (35m conflict zone)
            "veh_intervals":      [],     # {vehicle_id, enter_time, exit_time, ...}
            # tight conflict zone (position 기반, 8m radius)
            "active_tight_veh_entries": {},  # vid → {vehicle_id, enter_time}
            "tight_veh_intervals":      [],  # {vehicle_id, enter_time, exit_time}
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
            "controlled_lane_trace_rows": [],
            "controlled_lane_step_rows": [],
            "ped_trace_rows":  [],
            "ped_crossing_audit_rows": [],
            "trace_capture_until": -1.0,
            "controlled_lane_ids": set(),
            "controlled_lane_link_indices": {},
            "_last_ped_link_state": "",
            "_forced_ext_fired": False,
        }
        if trace_mode == "minimal":
            maxlen = max(1, int(round(float(trace_pre_window_sec) / max(float(step_length), 0.001))))
            for key in _TRACE_KEYS:
                watch[cid][f"{key}_buffer"] = deque(maxlen=maxlen)

    # TLS-controlled incoming lanes, keyed by lane_id -> controlled linkIndex list.
    # The pedestrian link itself is excluded so phase-extension vehicle effects are isolated.
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
            lane_id: sorted(link_indices)
            for lane_id, link_indices in lane_to_links.items()
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
    last_traffic_sample_t: float | None = None

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
            sample_traffic = (
                last_traffic_sample_t is None
                or (t - last_traffic_sample_t) >= max(float(traffic_watch_sample_interval_sec), float(step_length)) - 1e-9
            )
            if sample_traffic:
                last_traffic_sample_t = t

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
            step_tight_conflict_vehs: dict[str, set] = {cid: set() for cid in watch}
            step_local_speeds:  dict[str, list] = {cid: [] for cid in watch}
            step_local_waiting: dict[str, list] = {cid: [] for cid in watch}
            step_local_veh_cnt: dict[str, int]  = {cid: 0  for cid in watch}
            step_controlled_speeds: dict[str, list] = {cid: [] for cid in watch}
            step_controlled_waiting: dict[str, list] = {cid: [] for cid in watch}
            step_controlled_positions: dict[str, list] = {cid: [] for cid in watch}
            step_controlled_veh_ids: dict[str, set] = {cid: set() for cid in watch}
            step_controlled_halting: dict[str, int] = {cid: 0 for cid in watch}

            for vid in all_veh_ids:
                try:
                    road_id = str(traci.vehicle.getRoadID(vid))
                    lane_id = str(traci.vehicle.getLaneID(vid))
                except Exception:
                    continue
                _veh_aliases = _normalize_road_alias(road_id) | _normalize_road_alias(lane_id)
                veh_status: list[tuple[str, dict[str, Any], bool, bool, bool, bool, list[int]]] = []
                needs_motion = bool(sample_traffic)
                for cid, w in watch.items():
                    in_local = sample_traffic and (lane_id in w["local_lane_ids"] or road_id in w["local_edge_ids"])
                    in_conflict = bool(_veh_aliases & w["conflict_edge_aliases"])
                    controlled_link_indices = w["controlled_lane_link_indices"].get(lane_id, [])
                    controlled_signal_affected = bool(controlled_link_indices)
                    in_controlled_lane = sample_traffic and controlled_signal_affected
                    if in_conflict:
                        needs_motion = True
                    veh_status.append((cid, w, in_local, in_conflict, in_controlled_lane, controlled_signal_affected, controlled_link_indices))
                if needs_motion:
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
                        _step_wt = float(traci.vehicle.getWaitingTime(vid))
                    except Exception:
                        _step_wt = float("nan")
                    try:
                        _tl = float(traci.vehicle.getTimeLoss(vid))
                    except Exception:
                        _tl = float("nan")
                    try:
                        _lane_pos = float(traci.vehicle.getLanePosition(vid))
                    except Exception:
                        _lane_pos = float("nan")
                else:
                    speed = _wt = _step_wt = _tl = _lane_pos = float("nan")
                for cid, w, in_local, in_conflict, in_controlled_lane, controlled_signal_affected, controlled_link_indices in veh_status:
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
                    if in_controlled_lane:
                        step_controlled_veh_ids[cid].add(vid)
                        if not (speed != speed):
                            step_controlled_speeds[cid].append(speed)
                            if speed <= 0.1:
                                step_controlled_halting[cid] += 1
                        if not (_step_wt != _step_wt):
                            step_controlled_waiting[cid].append(_step_wt)
                        if not (_lane_pos != _lane_pos):
                            step_controlled_positions[cid].append(_lane_pos)
                        _trace_append(w, "controlled_lane_trace_rows", {
                            "t": round(t, 1),
                            "policy_t": round(t - warmup_sec, 1),
                            "scenario": scenario,
                            "crosswalk_id": cid,
                            "tls_id": w["tls_id"],
                            "vehicle_id": vid,
                            "road_id": road_id,
                            "lane_id": lane_id,
                            "controlled_link_indices": "|".join(str(i) for i in controlled_link_indices),
                            "speed": round(speed, 4) if not (speed != speed) else None,
                            "waiting_time": round(_step_wt, 4) if not (_step_wt != _step_wt) else None,
                            "accumulated_waiting_time": round(_wt, 4) if not (_wt != _wt) else None,
                            "time_loss": round(_tl, 4) if not (_tl != _tl) else None,
                            "lane_position": round(_lane_pos, 4) if not (_lane_pos != _lane_pos) else None,
                            "in_conflict_edge": bool(in_conflict),
                        }, t, trace_mode)
                    if in_conflict:
                        step_conflict_vehs[cid].add(vid)
                        if vid not in w["active_veh_entries"]:
                            w["active_veh_entries"][vid] = {
                                "vehicle_id": vid,
                                "enter_time": float(t),
                                "enter_road_id": road_id,
                                "enter_lane_id": lane_id,
                                "enter_controlled_link_indices": "|".join(str(i) for i in controlled_link_indices),
                                "controlled_signal_affected": bool(controlled_signal_affected),
                            }
                        entry = w["active_veh_entries"][vid]
                        entry["exit_road_id"] = road_id
                        entry["exit_lane_id"] = lane_id
                        entry["exit_speed"] = round(speed, 4) if not (speed != speed) else None
                        entry["exit_lane_position"] = round(_lane_pos, 4) if not (_lane_pos != _lane_pos) else None
                        entry["controlled_signal_affected"] = bool(
                            entry.get("controlled_signal_affected") or controlled_signal_affected
                        )
                        # ── tight conflict zone (position 기반 8m) ──────────
                        try:
                            pos = traci.vehicle.getPosition(vid)
                            cx, cy = w["crossing_xy"]
                            dist = ((pos[0] - cx) ** 2 + (pos[1] - cy) ** 2) ** 0.5
                            if dist <= _TIGHT_CONFLICT_RADIUS_M:
                                step_tight_conflict_vehs[cid].add(vid)
                                if vid not in w["active_tight_veh_entries"]:
                                    w["active_tight_veh_entries"][vid] = {
                                        "vehicle_id": vid,
                                        "enter_time": float(t),
                                    }
                        except Exception:
                            pass

            # flush vehicles that left conflict zone
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
                tls_id        = w["tls_id"]
                ped_link_index = w["ped_link_index"]
                local_lane_ids = w["local_lane_ids"]
                crossing_edges = w["crossing_edges"]

                # ── queue length ──────────────────────────────────────────────
                if sample_traffic:
                    q = 0
                    for lid in local_lane_ids:
                        try:
                            q += traci.lane.getLastStepHaltingNumber(lid)
                        except Exception:
                            pass
                    w["queue_samples"].append(float(q))
                    _spds = step_local_speeds[cid]
                    _wtgs = step_local_waiting[cid]
                    _trace_append(w, "lane_trace_rows", {
                        "t":                 round(t, 1),
                        "policy_t":          round(t - warmup_sec, 1),
                        "scenario":          scenario,
                        "crosswalk_id":      cid,
                        "veh_count":         step_local_veh_cnt[cid],
                        "halting_count":     int(q),
                        "mean_speed":        round(sum(_spds) / len(_spds), 4) if _spds else None,
                        "mean_waiting_time": round(sum(_wtgs) / len(_wtgs), 4) if _wtgs else None,
                    }, t, trace_mode)
                    _cspds = step_controlled_speeds[cid]
                    _cwtgs = step_controlled_waiting[cid]
                    _cpos = step_controlled_positions[cid]
                    _trace_append(w, "controlled_lane_step_rows", {
                        "t": round(t, 1),
                        "policy_t": round(t - warmup_sec, 1),
                        "scenario": scenario,
                        "crosswalk_id": cid,
                        "tls_id": tls_id,
                        "controlled_lane_count": len(w["controlled_lane_ids"]),
                        "vehicle_count": len(step_controlled_veh_ids[cid]),
                        "halting_count": int(step_controlled_halting[cid]),
                        "mean_speed": round(sum(_cspds) / len(_cspds), 4) if _cspds else None,
                        "mean_waiting_time": round(sum(_cwtgs) / len(_cwtgs), 4) if _cwtgs else None,
                        "min_lane_position": round(min(_cpos), 4) if _cpos else None,
                        "vehicle_ids": "|".join(sorted(step_controlled_veh_ids[cid])),
                    }, t, trace_mode)

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
                    _trace_append(w, "ped_trace_rows", {
                        "t":             round(t, 1),
                        "policy_t":      round(t - warmup_sec, 1),
                        "scenario":      scenario,
                        "crosswalk_id":  cid,
                        "person_id":     pid,
                        "road_id":       road,
                        "lane_id":       lane,
                        "waiting_time":  round(_pid_wt, 3),
                        "on_crossing":   bool(hit),
                    }, t, trace_mode)
                    if hit:
                        ped_near += 1
                        step_crossing_pids.add(pid)
                        w["ped_seen"].add(pid)
                        if pid not in w["active_ped_entries"]:
                            w["active_ped_entries"][pid] = t
                            w["ped_crossing_audit_rows"].append({
                                "t": round(t, 1),
                                "policy_t": round(t - warmup_sec, 1),
                                "scenario": scenario,
                                "crosswalk_id": cid,
                                "person_id": pid,
                                "event": "enter",
                                "ped_link_state": w.get("_last_ped_link_state", ""),
                                "dwell_sec": None,
                            })
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
                        w["ped_intervals_with_id"].append({"pedestrian_id": pid, "enter_time": float(enter), "exit_time": t})
                        w["ped_crossing_audit_rows"].append({
                            "t": round(t, 1),
                            "policy_t": round(t - warmup_sec, 1),
                            "scenario": scenario,
                            "crosswalk_id": cid,
                            "person_id": pid,
                            "event": "exit",
                            "ped_link_state": w.get("_last_ped_link_state", ""),
                            "dwell_sec": round(t - float(enter), 1),
                        })

                # ── elderly incomplete detection ──────────────────────────────
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

                # ── TLS cycle detection ───────────────────────────────────────
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
                    last_ph   = tls_last_phase.get(tls_id, phase)
                    if phase < last_ph:  # phase index wrapped → new signal cycle
                        tls_cycle[tls_id] = tls_cycle.get(tls_id, 0) + 1
                    tls_last_phase[tls_id] = phase
                    cycle = tls_cycle.get(tls_id, 0)

                    ped_link_state = state[ped_link_index] if ped_link_index < len(state) else ""
                    w["_last_ped_link_state"] = ped_link_state
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
                    # ── forced extension: 자연 발동 억제, 지정 시간 이후 첫 eligible step에 발동 ──
                    if forced_extension_policy_t is not None and scenario == "smart" and extension_sec > 0:
                        forced_sumo_t = float(warmup_sec) + float(forced_extension_policy_t)
                        if (not w["_forced_ext_fired"]
                                and t >= forced_sumo_t
                                and tls_id
                                and ped_link_state in {"G", "g"}):
                            skip = ""  # forced eligible
                        else:
                            skip = "forced_ext_pending_or_done"  # 자연 발동 억제
                    if skip == "" and extension_sec > 0:
                        traci.trafficlight.setPhaseDuration(tls_id, remaining + extension_sec)
                        extended_this_cycle.add((tls_id, phase, cid))
                        if forced_extension_policy_t is not None and scenario == "smart":
                            w["_forced_ext_fired"] = True
                        if trace_mode == "minimal":
                            _trace_mark_extension(w, t, trace_post_window_sec)
                        try:
                            _remaining_after = float(traci.trafficlight.getNextSwitch(tls_id) - t)
                        except Exception:
                            _remaining_after = remaining + extension_sec
                        tls_step_cache[tls_id] = (phase, state, _remaining_after)
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
                    _trace_append(w, "tls_trace_rows", {
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
                    }, t, trace_mode)
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
            w["ped_intervals_with_id"].append({"pedestrian_id": pid, "enter_time": float(enter), "exit_time": float(t)})
        w["active_ped_entries"].clear()
        for vid, enter in list(w["active_veh_entries"].items()):
            entry = dict(enter)
            entry["exit_time"] = float(t)
            w["veh_intervals"].append(entry)
        w["active_veh_entries"].clear()
        for vid, entry in list(w["active_tight_veh_entries"].items()):
            e = dict(entry)
            e["exit_time"] = float(t)
            w["tight_veh_intervals"].append(e)
        w["active_tight_veh_entries"].clear()

    # ── 출력 디렉토리: csv/ (결과), log/ (메타데이터) ─────────────────────────
    csv_dir = out_dir / "csv"
    log_dir = out_dir / "log"
    csv_dir.mkdir(parents=True, exist_ok=True)
    log_dir.mkdir(parents=True, exist_ok=True)

    # ── observations.csv: 이벤트 상세 4종 통합 ────────────────────────────────
    _obs_frames: list[pd.DataFrame] = []

    _ext_df = pd.DataFrame(extension_events)
    if not _ext_df.empty:
        _ext_df.insert(0, "source", "extension_trace")
    _obs_frames.append(_ext_df)

    _ped_sig_df = pd.DataFrame(
        [r for w in watch.values() for r in w["ped_crossing_audit_rows"]]
    )
    if not _ped_sig_df.empty:
        _ped_sig_df.insert(0, "source", "ped_crossing_signal_audit")
    _obs_frames.append(_ped_sig_df)

    _audit_rows: list[dict] = []
    for cid, w in watch.items():
        _audit_rows.extend(
            _compute_pet_event_audit_rows(
                crosswalk_id=cid,
                scenario=scenario,
                ped_intervals=w["ped_intervals_with_id"],
                veh_intervals=w["veh_intervals"],
            )
        )
    if _audit_rows:
        _pet_audit_df = pd.DataFrame(_audit_rows)
        _pet_audit_df.insert(0, "source", "local_watcher_pet_event_audit")
        _obs_frames.append(_pet_audit_df)

    _veh_int_df = pd.DataFrame(
        [r for w in watch.values() for r in w["veh_intervals"]]
    )
    if not _veh_int_df.empty:
        _veh_int_df.insert(0, "source", "pet_vehicle_intervals")
    _obs_frames.append(_veh_int_df)

    _obs_all = [f for f in _obs_frames if not f.empty]
    (pd.concat(_obs_all, ignore_index=True) if _obs_all else pd.DataFrame()).to_csv(
        csv_dir / "observations.csv", index=False
    )

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
        # PET 계산 (local_watcher 방식, SSM 없이) — 기존 유지
        pet = _summarize_local_watcher_pet(
            ped_intervals=w["ped_intervals"],
            veh_intervals=w["veh_intervals"],
            pedestrian_crossing_count=len(w["ped_seen"]),
            conflict_edge_count=len(w["conflict_edge_ids"]),
        )
        # conflict-zone PET (position 기반 tight zone, 8m radius)
        cz_pet = _summarize_local_watcher_pet(
            ped_intervals=w["ped_intervals"],
            veh_intervals=w["tight_veh_intervals"],
            pedestrian_crossing_count=len(w["ped_seen"]),
            conflict_edge_count=len(w["conflict_edge_ids"]),
        )
        # pair-level diagnostic (per-ped min 포화 우회용 보조 지표)
        pair_diag = _compute_pet_pair_summary(
            ped_intervals=w["ped_intervals"],
            veh_intervals=w["veh_intervals"],
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
            "pet_observability_mode":  pet_observability_mode,
            "diagnostic_only":         bool((vehicle_route_cache_meta or {}).get("diagnostic_only", False)),
            "diagnostic_random_seed":  int((vehicle_route_cache_meta or {}).get("diagnostic_random_seed", 0) or 0),
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
            "ped_repeat_spacing_sec":      float(getattr(row, "ped_repeat_spacing_sec", 2.0)),
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
            "vehicle_route_sha256":       _file_sha256(global_veh_file),
            "diagnostic_targeted_vehicle_count": int((vehicle_route_cache_meta or {}).get("targeted_vehicle_count", 0)),
            "diagnostic_randomized_vehicle_count": int((vehicle_route_cache_meta or {}).get("diagnostic_randomized_vehicle_count", 0)),
            "diagnostic_vehicle_count": int((vehicle_route_cache_meta or {}).get("diagnostic_vehicle_count",
                max(
                    int((vehicle_route_cache_meta or {}).get("targeted_vehicle_count", 0) or 0),
                    int((vehicle_route_cache_meta or {}).get("diagnostic_randomized_vehicle_count", 0) or 0),
                ))),
            "background_vehicle_route_file": str((vehicle_route_cache_meta or {}).get("background_route_file", "")),
            "background_vehicle_route_sha256": str((vehicle_route_cache_meta or {}).get("background_route_sha256", "")),
            "generated_pedestrian_route_file": str(ped_route_artifact) if ped_route_artifact.exists() else None,
            "pedestrian_route_sha256":    _file_sha256(ped_route_artifact),
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
            # ── conflict-zone PET (position 기반 tight zone, 8m radius) ─────────
            "cz_pet_event_count":                  cz_pet.get("pet_event_count"),
            "cz_pet_min":                          cz_pet.get("pet_min"),
            "cz_pet_p10":                          cz_pet.get("pet_p10"),
            "cz_pet_mean":                         cz_pet.get("pet_mean"),
            "cz_low_pet_event_count":              cz_pet.get("low_pet_event_count"),
            "cz_very_risky_count":                 cz_pet.get("very_risky_crossing_count"),
            # ── pair-level diagnostic (보조, PET 대체 아님) ──────────────────
            "pair_total_count":          pair_diag.get("pair_total_count"),
            "pair_overlap_count":        pair_diag.get("pair_overlap_count"),
            "pair_ped_before_veh_count": pair_diag.get("pair_ped_before_veh_count"),
            "pair_veh_before_ped_count": pair_diag.get("pair_veh_before_ped_count"),
            "pair_overlap_rate":         pair_diag.get("pair_overlap_rate"),
            "pair_ped_before_veh_rate":  pair_diag.get("pair_ped_before_veh_rate"),
            "pair_veh_before_ped_rate":  pair_diag.get("pair_veh_before_ped_rate"),
            "pair_pet_mean":             pair_diag.get("pair_pet_mean"),
            "pair_pet_median":           pair_diag.get("pair_pet_median"),
            "pair_nonzero_pet_mean":     pair_diag.get("pair_nonzero_pet_mean"),
            "pair_nonzero_pet_median":   pair_diag.get("pair_nonzero_pet_median"),
            "safety_risk_score":                   round(safety_risk, 4),
            "accident_expected_value":             round(accident_ev, 2),
            # ── run context ──────────────────────────────────────────────────
            "run_group":  group_name,
        })

    elapsed = round(time.time() - t_start, 2)
    result_df = pd.DataFrame(rows)

    # save per-run simulation_result.csv → csv/
    result_df.to_csv(csv_dir / "simulation_result.csv", index=False, encoding="utf-8-sig")

    # run_info.csv: 횡단보도/시뮬레이션 기본 정보 → csv/
    _info_rows = []
    for row in candidate_df.itertuples(index=False):
        cid = str(row.crosswalk_id)
        _cw_elderly = (elderly_ratio.get(cid, 0.15) if isinstance(elderly_ratio, dict)
                       else float(elderly_ratio))
        _info_rows.append({
            "crosswalk_id":            cid,
            "tls_id_used":             getattr(row, "tls_id_used", ""),
            "crossing_id":             getattr(row, "crossing_id", ""),
            "nearest_junction_id":     getattr(row, "nearest_junction_id", ""),
            "ped_link_index":          getattr(row, "ped_link_index", ""),
            "crossing_edge_id":        getattr(row, "crossing_edge_id", ""),
            "route_from_edge":         getattr(row, "route_from_edge", ""),
            "route_to_edge":           getattr(row, "route_to_edge", ""),
            "controlled_links_count":  getattr(row, "controlled_links_count", ""),
            "final_verdict":           getattr(row, "final_verdict", ""),
            "scenario":                scenario,
            "seed":                    seed,
            "sim_duration":            duration,
            "warmup_sec":              warmup_sec,
            "step_length":             step_length,
            "extension_sec":           extension_sec,
            "ped_count":               int(getattr(row, "ped_repeat_count", len(ped_records))),
            "ped_repeat_spacing_sec":  float(getattr(row, "ped_repeat_spacing_sec", 2.0)),
            "elderly_ratio":           _cw_elderly,
            "run_profile":             run_profile,
            "trace_mode":              trace_mode,
            "pet_observability_mode":  pet_observability_mode,
            "group_name":              group_name,
            "net_file":                str(net_file),
        })
    pd.DataFrame(_info_rows).to_csv(csv_dir / "run_info.csv", index=False, encoding="utf-8-sig")

    # run_metadata.json → log/
    meta = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "runner": "run_enhanced_smoke_group",
        "run_profile": run_profile,
        "pet_observability_mode": pet_observability_mode,
        "diagnostic_only": bool((vehicle_route_cache_meta or {}).get("diagnostic_only", False)),
        "diagnostic_random_seed": int((vehicle_route_cache_meta or {}).get("diagnostic_random_seed", 0) or 0),
        "scenario": scenario,
        "seed": seed,
        "sim_duration": duration,
        "step_length": step_length,
        "ped_count": len(ped_records),
        "ped_repeat_spacing_sec": float(candidate_df["ped_repeat_spacing_sec"].iloc[0]) if not candidate_df.empty else 2.0,
        "elderly_ratio": elderly_ratio,
        "include_vehicles": include_vehicles,
        "global_veh_file": str(global_veh_file) if global_veh_file else None,
        "vehicle_route_sha256": _file_sha256(global_veh_file),
        "pedestrian_route_file": str(ped_route_artifact) if ped_route_artifact.exists() else "",
        "pedestrian_route_sha256": _file_sha256(ped_route_artifact),
        "vehicle_route_cache_hit": bool((vehicle_route_cache_meta or {}).get("hit", False)),
        "vehicle_route_cache_file": str((vehicle_route_cache_meta or {}).get("file", global_veh_file or "")),
        "vehicle_route_cache_key": str((vehicle_route_cache_meta or {}).get("key", "")),
        "background_vehicle_route_file": str((vehicle_route_cache_meta or {}).get("background_route_file", "")),
        "background_vehicle_route_sha256": str((vehicle_route_cache_meta or {}).get("background_route_sha256", "")),
        "diagnostic_targeted_vehicle_count": int((vehicle_route_cache_meta or {}).get("targeted_vehicle_count", 0)),
        "diagnostic_randomized_vehicle_count": int((vehicle_route_cache_meta or {}).get("diagnostic_randomized_vehicle_count", 0)),
        "diagnostic_vehicle_count": int((vehicle_route_cache_meta or {}).get("diagnostic_vehicle_count", 0)),
        "extension_sec": extension_sec,
        "trace_mode": trace_mode,
        "trace_pre_window_sec": trace_pre_window_sec,
        "trace_post_window_sec": trace_post_window_sec,
        "traffic_watch_sample_interval_sec": traffic_watch_sample_interval_sec,
        "crosswalk_count": len(rows),
        "elapsed_sec": elapsed,
        "group_name": group_name,
        "net_file": str(net_file),
    }
    (log_dir / "run_metadata.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    return result_df


# ─────────────────────────────────────────────────────────────────────────────
# 4. Candidate CSV loader + ped-count override
# ─────────────────────────────────────────────────────────────────────────────
def _load_candidate(csv_path: Path, ped_count: int, ped_repeat_spacing_sec: float = 2.0) -> pd.DataFrame:
    df = pd.read_csv(csv_path, encoding="utf-8-sig")
    df["ped_repeat_count"]       = ped_count
    df["ped_repeat_spacing_sec"] = ped_repeat_spacing_sec
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


def _edge_length_m(edge: Any) -> float:
    try:
        length = float(edge.getLength())
        if length > 0:
            return length
    except Exception:
        pass
    lengths: list[float] = []
    try:
        for lane in edge.getLanes():
            lane_len = float(lane.getLength())
            if lane_len > 0:
                lengths.append(lane_len)
    except Exception:
        pass
    return float(sum(lengths) / len(lengths)) if lengths else 0.0


def _passenger_lane_count(edge: Any) -> int:
    try:
        lanes = list(edge.getLanes())
    except Exception:
        lanes = []
    return int(sum(1 for lane in lanes if lane_allows(lane, "passenger")))


def _vehicle_count_for_seconds(seconds: float) -> int:
    return max(0, int(round(float(VEHICLE_POLICY_TOTAL_FLOW_VPH) * float(seconds) / 3600.0)))


def _exact_weighted_counts(weights: list[float], total: int) -> list[int]:
    if total <= 0 or not weights or sum(weights) <= 0:
        return [0 for _ in weights]
    exact = [float(w) / float(sum(weights)) * int(total) for w in weights]
    counts = [int(v) for v in exact]
    remainder = int(total) - sum(counts)
    order = sorted(range(len(weights)), key=lambda i: exact[i] - counts[i], reverse=True)
    for idx in order[:remainder]:
        counts[idx] += 1
    return counts


def _vehicle_edge_allocation_table(
    net: Any,
    *,
    run_window_sec: float,
    policy_window_sec: float,
) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for edge in net.getEdges():
        edge_id = str(edge.getID())
        if edge_id.startswith(":"):
            continue
        try:
            allows_passenger = bool(edge.allows("passenger"))
        except Exception:
            allows_passenger = False
        lane_count = _passenger_lane_count(edge) if allows_passenger else 0
        if not allows_passenger or lane_count <= 0:
            continue
        length_m = _edge_length_m(edge)
        lane_count_capped = min(int(lane_count), _LANE_COUNT_CAP)
        edge_weight = float(length_m * lane_count_capped) if length_m > 0 else float(lane_count_capped)
        if edge_weight <= 0:
            continue
        rows.append({
            "edge_id": edge_id,
            "lane_count": int(lane_count),
            "lane_count_capped": int(lane_count_capped),
            "edge_length_m": round(length_m, 3) if length_m > 0 else None,
            "edge_weight": float(edge_weight),
            "allows_passenger": allows_passenger,
            "lane_cap_policy": f"min_lane_count_{_LANE_COUNT_CAP}",
            "lane_weight_alternative": "sqrt_lane_count_not_used",
        })
    df = pd.DataFrame(rows)
    if df.empty:
        return pd.DataFrame(columns=[
            "edge_id", "lane_count", "lane_count_capped", "edge_length_m", "edge_weight",
            "allocated_vehicle_count_run_window", "allocated_vehicle_count_policy_window",
            "allocated_vehicle_count_600s", "generated_vehicle_count", "allows_passenger",
            "lane_cap_policy", "lane_weight_alternative",
        ])
    weights = [float(v) for v in df["edge_weight"].tolist()]
    df["allocated_vehicle_count_run_window"] = _exact_weighted_counts(
        weights, _vehicle_count_for_seconds(run_window_sec)
    )
    df["allocated_vehicle_count_policy_window"] = _exact_weighted_counts(
        weights, _vehicle_count_for_seconds(policy_window_sec)
    )
    df["allocated_vehicle_count_600s"] = _exact_weighted_counts(
        weights, int(VEHICLE_POLICY_TOTAL_COUNT_600S)
    )
    df["generated_vehicle_count"] = 0
    return df


def _route_file_vehicle_count(route_file: Path) -> int:
    try:
        root = ET.parse(route_file).getroot()
    except Exception:
        return 0
    return int(sum(1 for _ in root.iter("vehicle")))


def _route_file_passenger_only(route_file: Path) -> bool:
    try:
        root = ET.parse(route_file).getroot()
    except Exception:
        return False
    type_by_id = {
        str(vtype.attrib.get("id", "")): str(vtype.attrib.get("vClass", ""))
        for vtype in root.findall("vType")
    }
    for veh in root.findall("vehicle"):
        vtype = str(veh.attrib.get("type", "passenger"))
        if vtype in {"taxi", "bus", "truck"}:
            return False
        if type_by_id.get(vtype, "passenger") not in {"", "passenger"}:
            return False
    return True


def _parse_vehicle_route_file(route_file: Path) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    if not route_file.exists():
        return pd.DataFrame(columns=["vehicle_id", "depart_sec", "depart_edge", "arrival_edge", "route_edges"])
    try:
        root = ET.parse(route_file).getroot()
    except Exception:
        return pd.DataFrame(columns=["vehicle_id", "depart_sec", "depart_edge", "arrival_edge", "route_edges"])
    route_defs = {
        str(route.attrib.get("id", "")): str(route.attrib.get("edges", "")).split()
        for route in root.findall("route")
    }
    for veh in root.findall("vehicle"):
        route_elem = veh.find("route")
        edges = (
            str(route_elem.attrib.get("edges", "")).split()
            if route_elem is not None
            else route_defs.get(str(veh.attrib.get("route", "")), [])
        )
        if not edges:
            continue
        rows.append({
            "vehicle_id": str(veh.attrib.get("id", "")),
            "depart_sec": float(veh.attrib.get("depart", 0.0) or 0.0),
            "depart_edge": edges[0],
            "arrival_edge": edges[-1],
            "route_edges": edges,
        })
    return pd.DataFrame(rows)


def _write_vehicle_audits(
    route_file: Path,
    allocation_df: pd.DataFrame,
    out_dir: Path,
    meta: dict[str, Any],
) -> None:
    route_df = _parse_vehicle_route_file(route_file)
    generated_counts = (
        route_df["depart_edge"].value_counts().to_dict()
        if not route_df.empty else {}
    )
    audit = allocation_df.copy()
    audit["generated_vehicle_count"] = audit["edge_id"].map(generated_counts).fillna(0).astype(int)
    audit.to_csv(out_dir / "vehicle_edge_allocation_audit.csv", index=False)

    route_edges: set[str] = set()
    for edges in route_df["route_edges"].tolist() if not route_df.empty else []:
        route_edges.update(str(edge_id) for edge_id in edges)
    top10_share = 0.0
    if not route_df.empty:
        top10_share = float(route_df["depart_edge"].value_counts().head(10).sum() / max(len(route_df), 1))
    summary = pd.DataFrame([{
        "generated_vehicle_count": int(len(route_df)),
        "unique_depart_edges": int(route_df["depart_edge"].nunique()) if not route_df.empty else 0,
        "unique_arrival_edges": int(route_df["arrival_edge"].nunique()) if not route_df.empty else 0,
        "unique_route_edges": int(len(route_edges)),
        "total_valid_passenger_edges": int(len(allocation_df)),
        "network_edge_coverage_ratio": round(len(route_edges) / max(len(allocation_df), 1), 6),
        "top_10_depart_edges_share": round(top10_share, 6),
        "lane_cap_policy": f"min_lane_count_{_LANE_COUNT_CAP}",
        "lane_weight_alternative": "sqrt_lane_count_not_used",
        "source_total_vehicle_flow_vph": float(VEHICLE_POLICY_TOTAL_FLOW_VPH),
        "source_total_vehicle_count_600s": int(VEHICLE_POLICY_TOTAL_COUNT_600S),
        "allocated_vehicle_count_run_window": int(meta.get("vehicle_count_run_window", 0)),
        "allocated_vehicle_count_policy_window": int(meta.get("vehicle_count_policy_window", 0)),
        "allocated_vehicle_count_600s": int(VEHICLE_POLICY_TOTAL_COUNT_600S),
    }])
    summary.to_csv(out_dir / "vehicle_edge_coverage_summary.csv", index=False)


def _simple_weighted_vehicle_routes(
    net_file: Path,
    sim_duration: int,
    seed: int,
    out_dir: Path,
    warmup_sec: int = 0,
    vehicle_route_cache_dir: "Path | None" = None,
) -> "tuple[Path | None, dict[str, Any]]":
    """Passenger-only background traffic over all valid passenger edges.

    Default lane cap is min(lane_count, 6). sqrt(lane_count) is only documented
    as an alternative in audit metadata and is not used.
    """
    total_duration = float(sim_duration + warmup_sec)
    vehicle_count = _vehicle_count_for_seconds(total_duration)
    policy_window_count = _vehicle_count_for_seconds(float(sim_duration))
    net = read_net(str(net_file))
    allocation_df = _vehicle_edge_allocation_table(
        net,
        run_window_sec=total_duration,
        policy_window_sec=float(sim_duration),
    )
    if allocation_df.empty or vehicle_count <= 0:
        return None, {
            "hit": False,
            "error": "no_valid_passenger_edges_or_zero_vehicle_count",
            "vehicle_count_run_window": vehicle_count,
            "vehicle_count_policy_window": policy_window_count,
        }

    try:
        net_hash = _file_sha1_short(net_file)
    except Exception:
        net_hash = "unknown-net"
    cache_key = (
        f"simple_weighted_v1_net{net_hash}_seed{seed:05d}_"
        f"w{warmup_sec}_dur{sim_duration}_veh{vehicle_count}_cap{_LANE_COUNT_CAP}"
    )
    global_dir = (
        Path(vehicle_route_cache_dir) / cache_key
        if vehicle_route_cache_dir is not None
        else out_dir / "global_vehicle_routes"
    )
    global_dir.mkdir(parents=True, exist_ok=True)
    route_file = global_dir / f"demand_vehicle_simple_w{warmup_sec}.rou.xml"
    trip_file = global_dir / f"demand_vehicle_simple_w{warmup_sec}.trips.xml"
    meta: dict[str, Any] = {
        "hit": False,
        "file": str(route_file),
        "key": cache_key,
        "route_dir": str(global_dir),
        "vehicle_count": int(vehicle_count),
        "vehicle_count_run_window": int(vehicle_count),
        "vehicle_count_policy_window": int(policy_window_count),
        "vehicle_count_600s": int(VEHICLE_POLICY_TOTAL_COUNT_600S),
        "vehicle_flow_vph": float(VEHICLE_POLICY_TOTAL_FLOW_VPH),
        "lane_cap_policy": f"min_lane_count_{_LANE_COUNT_CAP}",
        "lane_weight_alternative": "sqrt_lane_count_not_used",
        "cache_dir": str(vehicle_route_cache_dir) if vehicle_route_cache_dir else "",
    }

    if route_file.exists() and _route_file_vehicle_count(route_file) == vehicle_count and _route_file_passenger_only(route_file):
        meta["hit"] = True
        meta["actual_vehicle_count"] = int(vehicle_count)
        _write_vehicle_audits(route_file, allocation_df, global_dir, meta)
        return route_file, meta

    rng = random.Random(seed)
    edge_ids = allocation_df["edge_id"].astype(str).tolist()
    weights = [float(v) for v in allocation_df["edge_weight"].tolist()]
    depart_times = sorted(rng.uniform(0.0, total_duration) for _ in range(vehicle_count))
    records: list[dict[str, Any]] = []
    for idx, depart in enumerate(depart_times, start=1):
        route_edges: list[str] = []
        depart_edge = ""
        arrival_edge = ""
        for _attempt in range(80):
            depart_edge = rng.choices(edge_ids, weights=weights, k=1)[0]
            arrival_edge = rng.choices(edge_ids, weights=weights, k=1)[0]
            if arrival_edge == depart_edge and len(edge_ids) > 1:
                continue
            route_edges = _vehicle_route_path(net, depart_edge, arrival_edge)
            if route_edges:
                break
        if not route_edges:
            depart_edge = rng.choices(edge_ids, weights=weights, k=1)[0]
            arrival_edge = depart_edge
            route_edges = _vehicle_route_path(net, depart_edge, depart_edge) or [depart_edge]
        records.append({
            "vehicle_id": f"veh_{idx:06d}",
            "depart": max(0.0, float(depart)),
            "depart_edge": depart_edge,
            "arrival_edge": arrival_edge,
            "route_edges": route_edges,
        })

    root = ET.Element("routes")
    ET.SubElement(root, "vType", {
        "id": "passenger",
        "vClass": "passenger",
        "maxSpeed": "15.0",
        "accel": "2.6",
        "decel": "4.5",
    })
    for rec in records:
        veh = ET.SubElement(root, "vehicle", {
            "id": str(rec["vehicle_id"]),
            "type": "passenger",
            "depart": f"{float(rec['depart']):.3f}",
        })
        ET.SubElement(veh, "route", {"edges": " ".join(rec["route_edges"])})
    ET.indent(root, space="  ")
    ET.ElementTree(root).write(route_file, encoding="utf-8", xml_declaration=True)

    trips_root = ET.Element("routes")
    for rec in records:
        ET.SubElement(trips_root, "trip", {
            "id": str(rec["vehicle_id"]),
            "type": "passenger",
            "depart": f"{float(rec['depart']):.3f}",
            "from": str(rec["depart_edge"]),
            "to": str(rec["arrival_edge"]),
        })
    ET.indent(trips_root, space="  ")
    ET.ElementTree(trips_root).write(trip_file, encoding="utf-8", xml_declaration=True)

    meta["actual_vehicle_count"] = int(len(records))
    _write_vehicle_audits(route_file, allocation_df, global_dir, meta)
    print(
        f"[enhanced smoke] simple weighted passenger routes: {len(records)} veh "
        f"(vph={VEHICLE_POLICY_TOTAL_FLOW_VPH:.0f}, total_window={total_duration:.0f}s, "
        f"policy_window={sim_duration}s, seed={seed}, cap={_LANE_COUNT_CAP})",
        flush=True,
    )
    return route_file, meta


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or str(value) in {"", "nan", "None"}:
            return default
        return float(value)
    except Exception:
        return default


def _as_int(value: Any, default: int = 0) -> int:
    try:
        if value is None or str(value) in {"", "nan", "None"}:
            return default
        return int(float(value))
    except Exception:
        return default


def _expected_targeted_extension_time(row: Any, warmup_sec: int, duration: int) -> float:
    """Diagnostic estimate only; real extension time still audited from trace."""
    ped_depart = _as_float(getattr(row, "ped_depart_time", 0.0), float(warmup_sec))
    cycle_duration = _as_float(getattr(row, "cycle_duration", 0.0), 0.0)
    if cycle_duration > 0:
        expected = ped_depart + max(0.0, cycle_duration - 2.0)
    else:
        expected = ped_depart + 60.0
    lo = float(warmup_sec) + 1.0
    hi = float(warmup_sec + duration) - 1.0
    return max(lo, min(expected, hi))


def _controlled_vehicle_connection_candidates(
    net_file: Path,
    net: Any,
    *,
    tls_id: str,
    ped_link_index: int,
    conflict_edge_ids: set[str],
    route_edge_ids: set[str],
) -> list[dict[str, Any]]:
    if not tls_id:
        return []
    try:
        root = ET.parse(net_file).getroot()
    except Exception:
        return []

    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, str, int]] = set()
    for conn in root.findall(".//connection"):
        if str(conn.attrib.get("tl", "")) != str(tls_id):
            continue
        link_index = _as_int(conn.attrib.get("linkIndex"), -1)
        if link_index < 0 or link_index == int(ped_link_index):
            continue
        from_edge_id = str(conn.attrib.get("from", "") or "")
        to_edge_id = str(conn.attrib.get("to", "") or "")
        if not from_edge_id or from_edge_id.startswith(":") or not to_edge_id:
            continue
        from_lane_index = _as_int(conn.attrib.get("fromLane"), 0)
        try:
            edge = net.getEdge(from_edge_id)
        except Exception:
            continue
        try:
            if not edge.allows("passenger"):
                continue
        except Exception:
            continue
        try:
            lanes = list(edge.getLanes())
        except Exception:
            lanes = []
        if from_lane_index < 0 or from_lane_index >= len(lanes):
            continue
        lane = lanes[from_lane_index]
        if not lane_allows(lane, "passenger"):
            continue
        lane_id = str(getattr(lane, "getID", lambda: f"{from_edge_id}_{from_lane_index}")())
        key = (lane_id, to_edge_id, link_index)
        if key in seen:
            continue
        seen.add(key)
        try:
            lane_length = float(lane.getLength())
        except Exception:
            lane_length = _edge_length_m(edge)
        rows.append({
            "tls_id": str(tls_id),
            "link_index": int(link_index),
            "from_edge": from_edge_id,
            "from_lane_index": int(from_lane_index),
            "from_lane_id": lane_id,
            "to_edge": to_edge_id,
            "lane_length_m": max(0.0, float(lane_length or 0.0)),
            "in_conflict_edge": from_edge_id in conflict_edge_ids,
            "in_generated_route": from_edge_id in route_edge_ids,
        })

    rows.sort(key=lambda r: (
        0 if r["in_conflict_edge"] else 1,
        0 if r["in_generated_route"] else 1,
        int(r["link_index"]),
        str(r["from_lane_id"]),
    ))
    return rows


def _sort_route_file_vehicles(root: ET.Element) -> None:
    vehicles = list(root.findall("vehicle"))
    if not vehicles:
        return
    for veh in vehicles:
        root.remove(veh)
    vehicles.sort(key=lambda elem: (
        _as_float(elem.attrib.get("depart"), 0.0),
        str(elem.attrib.get("id", "")),
    ))
    for veh in vehicles:
        root.append(veh)


def _diag_seed_int(*parts: Any) -> int:
    h = hashlib.sha1()
    for part in parts:
        h.update(str(part).encode("utf-8"))
        h.update(b"\0")
    return int.from_bytes(h.digest()[:8], "big", signed=False)


def _route_target_choices(row: Any, conn: dict[str, Any]) -> list[str]:
    choices = [
        str(conn.get("to_edge", "")),
        str(getattr(row, "route_to_edge", "") or ""),
        str(getattr(row, "route_from_edge", "") or ""),
        str(getattr(row, "crossing_edge_id", "") or ""),
    ]
    return [edge for edge in dict.fromkeys(choices) if edge]


def _append_targeted_controlled_vehicle_routes(
    *,
    background_route_file: Path,
    net_file: Path,
    candidate_df: pd.DataFrame,
    seed: int,
    out_dir: Path,
    warmup_sec: int,
    duration: int,
    vehicle_route_cache_meta: dict[str, Any] | None,
) -> tuple[Path, dict[str, Any]]:
    """Diagnostic-only route augmentation.

    Background route remains global. This copies it to a seed-local file, then
    adds a small number of passenger vehicles on TLS-controlled incoming lanes.
    Baseline and smart still share this single augmented route file.
    """
    target_dir = out_dir / "_vehicle_routes" / f"seed{seed:05d}" / "targeted_controlled"
    target_dir.mkdir(parents=True, exist_ok=True)
    target_route_file = target_dir / f"demand_vehicle_targeted_w{warmup_sec}.rou.xml"
    shutil.copy2(background_route_file, target_route_file)

    net = read_net(str(net_file))
    try:
        tree = ET.parse(target_route_file)
        root = tree.getroot()
    except Exception as exc:
        raise RuntimeError(f"failed to load targeted vehicle route file: {target_route_file}") from exc

    if not any(str(vt.attrib.get("id", "")) == "passenger" for vt in root.findall("vType")):
        root.insert(0, ET.Element("vType", {
            "id": "passenger",
            "vClass": "passenger",
            "maxSpeed": "15.0",
            "accel": "2.6",
            "decel": "4.5",
        }))

    audit_rows: list[dict[str, Any]] = []
    used_ids: set[str] = {str(v.attrib.get("id", "")) for v in root.findall("vehicle")}
    total_added = 0
    for row in candidate_df.itertuples(index=False):
        cid = str(getattr(row, "crosswalk_id", ""))
        tls_id = str(getattr(row, "tls_id", "") or getattr(row, "tls_id_used", ""))
        ped_link_index = _as_int(getattr(row, "ped_link_index", 0), 0)
        route_edge_ids = set(str(getattr(row, "generated_route_edges", "") or "").split("|")) - {""}
        try:
            scope = _build_local_scope(net, str(getattr(row, "crossing_edge_id", "")), 500.0)
            conflict_edge_ids = set(scope.get("conflict_edge_ids", set()))
        except Exception:
            conflict_edge_ids = set()

        candidates = _controlled_vehicle_connection_candidates(
            net_file,
            net,
            tls_id=tls_id,
            ped_link_index=ped_link_index,
            conflict_edge_ids=conflict_edge_ids,
            route_edge_ids=route_edge_ids,
        )
        expected_t = _expected_targeted_extension_time(row, warmup_sec, duration)
        added_for_cw = 0
        if not candidates:
            audit_rows.append({
                "seed": int(seed),
                "crosswalk_id": cid,
                "diagnostic_only": True,
                "targeted_vehicle_id": "",
                "expected_extension_time": round(expected_t, 1),
                "depart_time": None,
                "from_edge": "",
                "from_lane_id": "",
                "depart_lane_index": None,
                "depart_pos_m": None,
                "to_edge": "",
                "link_index": None,
                "route_edges": "",
                "target_reason": "no_controlled_vehicle_connection",
            })
            continue

        for i, offset in enumerate(_TARGETED_DIAGNOSTIC_DEPART_OFFSETS):
            if added_for_cw >= _TARGETED_DIAGNOSTIC_VEHICLES_PER_CROSSWALK:
                break
            conn = candidates[i % len(candidates)]
            route_edges: list[str] = []
            for to_edge in [
                str(conn["to_edge"]),
                str(getattr(row, "route_to_edge", "") or ""),
                str(getattr(row, "route_from_edge", "") or ""),
            ]:
                if not to_edge:
                    continue
                route_edges = _vehicle_route_path(net, str(conn["from_edge"]), to_edge)
                if route_edges:
                    break
            if not route_edges:
                audit_rows.append({
                    "seed": int(seed),
                    "crosswalk_id": cid,
                    "diagnostic_only": True,
                    "targeted_vehicle_id": "",
                    "expected_extension_time": round(expected_t, 1),
                    "depart_time": None,
                    "from_edge": str(conn["from_edge"]),
                    "from_lane_id": str(conn["from_lane_id"]),
                    "depart_lane_index": int(conn["from_lane_index"]),
                    "depart_pos_m": None,
                    "to_edge": str(conn["to_edge"]),
                    "link_index": int(conn["link_index"]),
                    "route_edges": "",
                    "target_reason": "no_passenger_route_from_controlled_edge",
                })
                continue

            safe_cid = _re.sub(r"[^A-Za-z0-9_.:-]", "_", cid)
            vehicle_id = f"diag_petobs_seed{seed:05d}_{safe_cid}_{added_for_cw + 1:03d}"
            while vehicle_id in used_ids:
                added_for_cw += 1
                vehicle_id = f"diag_petobs_seed{seed:05d}_{safe_cid}_{added_for_cw + 1:03d}"
            used_ids.add(vehicle_id)
            depart = max(0.0, min(float(warmup_sec + duration) - 0.1, expected_t + float(offset)))
            lane_length = float(conn.get("lane_length_m", 0.0) or 0.0)
            depart_pos = max(0.0, min(lane_length - 7.5, lane_length - 25.0)) if lane_length > 10.0 else 0.0
            veh = ET.SubElement(root, "vehicle", {
                "id": vehicle_id,
                "type": "passenger",
                "depart": f"{depart:.3f}",
                "departLane": str(int(conn["from_lane_index"])),
                "departPos": f"{depart_pos:.3f}",
                "departSpeed": "0",
            })
            ET.SubElement(veh, "route", {"edges": " ".join(route_edges)})
            audit_rows.append({
                "seed": int(seed),
                "crosswalk_id": cid,
                "diagnostic_only": True,
                "targeted_vehicle_id": vehicle_id,
                "expected_extension_time": round(expected_t, 1),
                "depart_time": round(depart, 3),
                "from_edge": str(conn["from_edge"]),
                "from_lane_id": str(conn["from_lane_id"]),
                "depart_lane_index": int(conn["from_lane_index"]),
                "depart_pos_m": round(depart_pos, 3),
                "to_edge": str(conn["to_edge"]),
                "link_index": int(conn["link_index"]),
                "route_edges": " ".join(route_edges),
                "target_reason": "diagnostic_controlled_lane_ext_window",
            })
            added_for_cw += 1
            total_added += 1

    _sort_route_file_vehicles(root)
    ET.indent(root, space="  ")
    tree.write(target_route_file, encoding="utf-8", xml_declaration=True)

    audit_columns = [
        "seed", "crosswalk_id", "diagnostic_only", "targeted_vehicle_id",
        "expected_extension_time", "depart_time", "from_edge", "from_lane_id",
        "depart_lane_index", "depart_pos_m", "to_edge", "link_index",
        "route_edges", "target_reason",
    ]
    pd.DataFrame(audit_rows, columns=audit_columns).to_csv(
        target_dir / "diagnostic_targeted_vehicle_audit.csv",
        index=False,
    )

    total_duration = float(duration + warmup_sec)
    allocation_df = _vehicle_edge_allocation_table(
        net,
        run_window_sec=total_duration,
        policy_window_sec=float(duration),
    )
    target_meta = dict(vehicle_route_cache_meta or {})
    target_meta.update({
        "file": str(target_route_file),
        "route_dir": str(target_dir),
        "diagnostic_only": True,
        "pet_observability_mode": PET_OBSERVABILITY_TARGETED,
        "diagnostic_random_seed": 0,
        "background_route_file": str(background_route_file),
        "background_route_sha256": _file_sha256(background_route_file),
        "targeted_vehicle_count": int(total_added),
        "diagnostic_targeted_vehicle_count": int(total_added),
        "diagnostic_randomized_vehicle_count": 0,
        "diagnostic_vehicle_count": int(total_added),
        "actual_vehicle_count": int(_route_file_vehicle_count(target_route_file)),
    })
    _write_vehicle_audits(target_route_file, allocation_df, target_dir, target_meta)
    print(
        f"[enhanced smoke] targeted PET diagnostic vehicles: {total_added} "
        f"(seed={seed}, route={target_route_file})",
        flush=True,
    )
    return target_route_file, target_meta


def _append_randomized_controlled_vehicle_routes(
    *,
    background_route_file: Path,
    net_file: Path,
    candidate_df: pd.DataFrame,
    seed: int,
    out_dir: Path,
    warmup_sec: int,
    duration: int,
    vehicle_route_cache_meta: dict[str, Any] | None,
    diagnostic_random_seed: int,
    vehicles_per_crosswalk: int,
    jitter_sec: float,
    forced_extension_policy_t: "float | None" = None,
) -> tuple[Path, dict[str, Any]]:
    """Diagnostic-only route augmentation with randomized controlled-lane placement."""
    target_dir = out_dir / "_vehicle_routes" / f"seed{seed:05d}" / "randomized_controlled"
    target_dir.mkdir(parents=True, exist_ok=True)
    target_route_file = target_dir / f"demand_vehicle_randomized_w{warmup_sec}.rou.xml"
    shutil.copy2(background_route_file, target_route_file)

    net = read_net(str(net_file))
    try:
        tree = ET.parse(target_route_file)
        root = tree.getroot()
    except Exception as exc:
        raise RuntimeError(f"failed to load randomized vehicle route file: {target_route_file}") from exc

    if not any(str(vt.attrib.get("id", "")) == "passenger" for vt in root.findall("vType")):
        root.insert(0, ET.Element("vType", {
            "id": "passenger",
            "vClass": "passenger",
            "maxSpeed": "15.0",
            "accel": "2.6",
            "decel": "4.5",
        }))

    cap = max(0, min(int(vehicles_per_crosswalk), _RANDOMIZED_DIAGNOSTIC_MAX_VEHICLES_PER_CROSSWALK))
    audit_rows: list[dict[str, Any]] = []
    used_ids: set[str] = {str(v.attrib.get("id", "")) for v in root.findall("vehicle")}
    total_added = 0

    for row in candidate_df.itertuples(index=False):
        cid = str(getattr(row, "crosswalk_id", ""))
        tls_id = str(getattr(row, "tls_id", "") or getattr(row, "tls_id_used", ""))
        ped_link_index = _as_int(getattr(row, "ped_link_index", 0), 0)
        route_edge_ids = set(str(getattr(row, "generated_route_edges", "") or "").split("|")) - {""}
        try:
            scope = _build_local_scope(net, str(getattr(row, "crossing_edge_id", "")), 500.0)
            conflict_edge_ids = set(scope.get("conflict_edge_ids", set()))
        except Exception:
            conflict_edge_ids = set()

        candidates = _controlled_vehicle_connection_candidates(
            net_file,
            net,
            tls_id=tls_id,
            ped_link_index=ped_link_index,
            conflict_edge_ids=conflict_edge_ids,
            route_edge_ids=route_edge_ids,
        )
        if forced_extension_policy_t is not None:
            expected_t = float(warmup_sec) + float(forced_extension_policy_t)
        else:
            expected_t = _expected_targeted_extension_time(row, warmup_sec, duration)
        if not candidates:
            audit_rows.append({
                "seed": int(seed),
                "diagnostic_random_seed": int(diagnostic_random_seed),
                "crosswalk_id": cid,
                "diagnostic_only": True,
                "pet_observability_mode": PET_OBSERVABILITY_RANDOMIZED,
                "randomized_vehicle_id": "",
                "expected_extension_time": round(expected_t, 1),
                "depart_time": None,
                "depart_offset_sec": None,
                "from_edge": "",
                "from_lane_id": "",
                "depart_lane_index": None,
                "depart_pos_m": None,
                "to_edge": "",
                "link_index": None,
                "route_edges": "",
                "target_reason": "no_controlled_vehicle_connection",
            })
            continue

        rng = random.Random(_diag_seed_int("randomized_controlled", seed, diagnostic_random_seed, cid))
        base_depart_low = float(warmup_sec)
        base_depart_high = float(warmup_sec + duration) - 0.1
        vehicle_seq = 0
        for _ in range(cap):
            shuffled_candidates = candidates[:]
            rng.shuffle(shuffled_candidates)
            chosen_conn: dict[str, Any] | None = None
            chosen_route_edges: list[str] = []
            chosen_to_edge = ""
            for conn in shuffled_candidates:
                target_choices = _route_target_choices(row, conn)
                rng.shuffle(target_choices)
                for to_edge in target_choices:
                    route_edges = _vehicle_route_path(net, str(conn["from_edge"]), str(to_edge))
                    if route_edges:
                        chosen_conn = conn
                        chosen_route_edges = route_edges
                        chosen_to_edge = str(to_edge)
                        break
                if chosen_conn is not None:
                    break

            depart_offset = rng.uniform(-float(jitter_sec), float(jitter_sec))
            depart = max(base_depart_low, min(base_depart_high, expected_t + depart_offset))

            if chosen_conn is None or not chosen_route_edges:
                audit_rows.append({
                    "seed": int(seed),
                    "diagnostic_random_seed": int(diagnostic_random_seed),
                    "crosswalk_id": cid,
                    "diagnostic_only": True,
                    "pet_observability_mode": PET_OBSERVABILITY_RANDOMIZED,
                    "randomized_vehicle_id": "",
                    "expected_extension_time": round(expected_t, 1),
                    "depart_time": round(depart, 3),
                    "depart_offset_sec": round(depart_offset, 3),
                    "from_edge": "",
                    "from_lane_id": "",
                    "depart_lane_index": None,
                    "depart_pos_m": None,
                    "to_edge": "",
                    "link_index": None,
                    "route_edges": "",
                    "target_reason": "no_passenger_route_from_controlled_edge",
                })
                continue

            vehicle_seq += 1
            lane_length = float(chosen_conn.get("lane_length_m", 0.0) or 0.0)
            if lane_length > 10.0:
                low_pos = max(0.0, lane_length - 90.0)
                high_pos = max(low_pos, lane_length - 5.0)
            else:
                low_pos = 0.0
                high_pos = max(0.0, lane_length)
            depart_pos = rng.uniform(low_pos, high_pos) if high_pos >= low_pos else 0.0
            safe_cid = _re.sub(r"[^A-Za-z0-9_.:-]", "_", cid)
            vehicle_id = f"diag_rand_petobs_seed{seed:05d}_{safe_cid}_{vehicle_seq:03d}"
            while vehicle_id in used_ids:
                vehicle_seq += 1
                vehicle_id = f"diag_rand_petobs_seed{seed:05d}_{safe_cid}_{vehicle_seq:03d}"
            used_ids.add(vehicle_id)
            veh = ET.SubElement(root, "vehicle", {
                "id": vehicle_id,
                "type": "passenger",
                "depart": f"{depart:.3f}",
                "departLane": str(int(chosen_conn["from_lane_index"])),
                "departPos": f"{depart_pos:.3f}",
                "departSpeed": "0",
            })
            ET.SubElement(veh, "route", {"edges": " ".join(chosen_route_edges)})
            audit_rows.append({
                "seed": int(seed),
                "diagnostic_random_seed": int(diagnostic_random_seed),
                "crosswalk_id": cid,
                "diagnostic_only": True,
                "pet_observability_mode": PET_OBSERVABILITY_RANDOMIZED,
                "randomized_vehicle_id": vehicle_id,
                "expected_extension_time": round(expected_t, 1),
                "depart_time": round(depart, 3),
                "depart_offset_sec": round(depart_offset, 3),
                "from_edge": str(chosen_conn["from_edge"]),
                "from_lane_id": str(chosen_conn["from_lane_id"]),
                "depart_lane_index": int(chosen_conn["from_lane_index"]),
                "depart_pos_m": round(depart_pos, 3),
                "to_edge": chosen_to_edge,
                "link_index": int(chosen_conn["link_index"]),
                "route_edges": " ".join(chosen_route_edges),
                "target_reason": "randomized_controlled_lane_ext_window",
            })
            total_added += 1

    _sort_route_file_vehicles(root)
    ET.indent(root, space="  ")
    tree.write(target_route_file, encoding="utf-8", xml_declaration=True)

    audit_columns = [
        "seed", "diagnostic_random_seed", "crosswalk_id", "diagnostic_only",
        "pet_observability_mode", "randomized_vehicle_id", "expected_extension_time",
        "depart_time", "depart_offset_sec", "from_edge", "from_lane_id",
        "depart_lane_index", "depart_pos_m", "to_edge", "link_index",
        "route_edges", "target_reason",
    ]
    pd.DataFrame(audit_rows, columns=audit_columns).to_csv(
        target_dir / "diagnostic_random_vehicle_audit.csv",
        index=False,
    )

    total_duration = float(duration + warmup_sec)
    allocation_df = _vehicle_edge_allocation_table(
        net,
        run_window_sec=total_duration,
        policy_window_sec=float(duration),
    )
    target_meta = dict(vehicle_route_cache_meta or {})
    target_meta.update({
        "file": str(target_route_file),
        "route_dir": str(target_dir),
        "diagnostic_only": True,
        "pet_observability_mode": PET_OBSERVABILITY_RANDOMIZED,
        "diagnostic_random_seed": int(diagnostic_random_seed),
        "background_route_file": str(background_route_file),
        "background_route_sha256": _file_sha256(background_route_file),
        "diagnostic_targeted_vehicle_count": 0,
        "diagnostic_randomized_vehicle_count": int(total_added),
        "diagnostic_vehicle_count": int(total_added),
        "actual_vehicle_count": int(_route_file_vehicle_count(target_route_file)),
    })
    _write_vehicle_audits(target_route_file, allocation_df, target_dir, target_meta)
    print(
        f"[enhanced smoke] randomized PET diagnostic vehicles: {total_added} "
        f"(seed={seed}, diag_seed={diagnostic_random_seed}, route={target_route_file})",
        flush=True,
    )
    return target_route_file, target_meta


def _first_path_from_df(df: pd.DataFrame, column: str) -> Path | None:
    if df.empty or column not in df.columns:
        return None
    vals = [str(v) for v in df[column].dropna().tolist() if str(v)]
    return Path(vals[0]) if vals else None


def _first_numeric(df: pd.DataFrame, column: str, default: float = float("nan")) -> float:
    if df.empty or column not in df.columns:
        return default
    series = pd.to_numeric(df[column], errors="coerce").dropna()
    return float(series.iloc[0]) if not series.empty else default


def _write_route_hash_audit(
    out_dir: Path,
    seed: int,
    scenario_dfs: dict[str, pd.DataFrame],
) -> None:
    audit_dir = out_dir / "audits" / f"seed{seed:05d}"
    audit_dir.mkdir(parents=True, exist_ok=True)
    baseline_df = scenario_dfs.get("baseline", pd.DataFrame())
    smart_df = scenario_dfs.get("smart", pd.DataFrame())
    base_vehicle = _first_path_from_df(baseline_df, "generated_vehicle_route_file")
    smart_vehicle = _first_path_from_df(smart_df, "generated_vehicle_route_file")
    base_ped = _first_path_from_df(baseline_df, "generated_pedestrian_route_file")
    smart_ped = _first_path_from_df(smart_df, "generated_pedestrian_route_file")
    row = {
        "seed": int(seed),
        "baseline_vehicle_route_file": str(base_vehicle or ""),
        "smart_vehicle_route_file": str(smart_vehicle or ""),
        "same_vehicle_route_hash": bool(_file_sha256(base_vehicle) and _file_sha256(base_vehicle) == _file_sha256(smart_vehicle)),
        "baseline_ped_route_file": str(base_ped or ""),
        "smart_ped_route_file": str(smart_ped or ""),
        "same_ped_route_hash": bool(_file_sha256(base_ped) and _file_sha256(base_ped) == _file_sha256(smart_ped)),
    }
    pd.DataFrame([row]).to_csv(audit_dir / "baseline_smart_route_hash_audit.csv", index=False)


def _copy_vehicle_route_audits(
    out_dir: Path,
    seed: int,
    vehicle_route_cache_meta: dict[str, Any] | None,
) -> None:
    route_dir = Path(str((vehicle_route_cache_meta or {}).get("route_dir", "")))
    if not route_dir.exists():
        return
    audit_dir = out_dir / "audits" / f"seed{seed:05d}"
    audit_dir.mkdir(parents=True, exist_ok=True)
    for name in (
        "vehicle_edge_allocation_audit.csv",
        "vehicle_edge_coverage_summary.csv",
        "diagnostic_targeted_vehicle_audit.csv",
        "diagnostic_random_vehicle_audit.csv",
    ):
        dst = audit_dir / name
        if dst.exists():
            dst.unlink()
        src = route_dir / name
        if src.exists():
            shutil.copy2(src, dst)


def _interval_overlaps(row: pd.Series, start: float, end: float) -> bool:
    try:
        enter = float(row.get("enter_time", 0.0))
        exit_time = float(row.get("exit_time", enter))
    except Exception:
        return False
    return enter <= end and exit_time >= start


def _read_csv_or_empty(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame()
    try:
        return pd.read_csv(path)
    except Exception:
        return pd.DataFrame()


def _blank_diagnostic_pet_interval_summary(diagnostic_only: bool = False) -> dict[str, Any]:
    status = "not_diagnostic_only" if not diagnostic_only else "no_diagnostic_pet_interval_match"
    return {
        "diagnostic_pet_interval_matched_count": 0,
        "diagnostic_vehicle_exit_delta_mean": None,
        "diagnostic_vehicle_exit_delta_p50": None,
        "diagnostic_vehicle_exit_delta_min": None,
        "diagnostic_vehicle_exit_delta_max": None,
        "diagnostic_vehicle_exit_delayed_count": 0,
        "diagnostic_vehicle_exit_delayed_ge_1s_count": 0,
        "diagnostic_vehicle_exit_delayed_ge_5s_count": 0,
        "diagnostic_vehicle_exit_delta_status": status,
    }


def _diagnostic_vehicle_crosswalk_lookup(audit_dir: Path) -> dict[str, str]:
    random_audit = _read_csv_or_empty(audit_dir / "diagnostic_random_vehicle_audit.csv")
    if random_audit.empty or "randomized_vehicle_id" not in random_audit.columns or "crosswalk_id" not in random_audit.columns:
        return {}
    valid = random_audit[
        random_audit["randomized_vehicle_id"].fillna("").astype(str).str.startswith("diag_rand_petobs_")
    ]
    return {
        str(row.randomized_vehicle_id): str(row.crosswalk_id)
        for row in valid.itertuples(index=False)
    }


def _diagnostic_interval_side(df: pd.DataFrame, scenario: str) -> pd.DataFrame:
    columns = [
        "vehicle_id", "occurrence_index",
        f"enter_time_{scenario}", f"exit_time_{scenario}",
        f"controlled_signal_affected_{scenario}",
    ]
    if df.empty or "vehicle_id" not in df.columns:
        return pd.DataFrame(columns=columns)
    out = df[df["vehicle_id"].fillna("").astype(str).str.startswith("diag_rand_petobs_")].copy()
    if out.empty:
        return pd.DataFrame(columns=columns)
    out["_row_order"] = range(len(out))
    out["enter_time"] = pd.to_numeric(out.get("enter_time"), errors="coerce")
    out["exit_time"] = pd.to_numeric(out.get("exit_time"), errors="coerce")
    out = out.sort_values(["vehicle_id", "enter_time", "exit_time", "_row_order"], na_position="last")
    out["occurrence_index"] = out.groupby("vehicle_id").cumcount()
    affected = out.get("controlled_signal_affected", False)
    if not isinstance(affected, pd.Series):
        affected = pd.Series([False] * len(out), index=out.index)
    out[f"controlled_signal_affected_{scenario}"] = affected.astype(str).str.lower().isin({"true", "1", "yes"})
    out = out.rename(columns={
        "enter_time": f"enter_time_{scenario}",
        "exit_time": f"exit_time_{scenario}",
    })
    return out[columns]


def _summarize_diagnostic_interval_delta(delta_df: pd.DataFrame, diagnostic_only: bool) -> dict[str, Any]:
    if not diagnostic_only:
        return _blank_diagnostic_pet_interval_summary(False)
    if delta_df.empty or "exit_delta" not in delta_df.columns:
        return _blank_diagnostic_pet_interval_summary(True)
    deltas = pd.to_numeric(delta_df["exit_delta"], errors="coerce").dropna()
    if deltas.empty:
        return _blank_diagnostic_pet_interval_summary(True)
    mean = float(deltas.mean())
    status = "diagnostic_vehicle_delay_observed" if mean > 0 else "diagnostic_vehicle_no_exit_delay"
    return {
        "diagnostic_pet_interval_matched_count": int(len(deltas)),
        "diagnostic_vehicle_exit_delta_mean": round(mean, 6),
        "diagnostic_vehicle_exit_delta_p50": round(float(deltas.quantile(0.5)), 6),
        "diagnostic_vehicle_exit_delta_min": round(float(deltas.min()), 6),
        "diagnostic_vehicle_exit_delta_max": round(float(deltas.max()), 6),
        "diagnostic_vehicle_exit_delayed_count": int((deltas > 0).sum()),
        "diagnostic_vehicle_exit_delayed_ge_1s_count": int((deltas >= 1.0).sum()),
        "diagnostic_vehicle_exit_delayed_ge_5s_count": int((deltas >= 5.0).sum()),
        "diagnostic_vehicle_exit_delta_status": status,
    }


def _patch_simulation_result_with_diagnostic_delta(
    path: Path,
    summaries_by_cid: dict[str, dict[str, Any]],
    diagnostic_only: bool,
) -> pd.DataFrame:
    df = _read_csv_or_empty(path)
    if df.empty or "crosswalk_id" not in df.columns:
        return df
    for col in _DIAGNOSTIC_PET_INTERVAL_COLUMNS:
        if col not in df.columns:
            df[col] = None
    for idx, row in df.iterrows():
        cid = str(row.get("crosswalk_id", ""))
        summary = summaries_by_cid.get(cid, _blank_diagnostic_pet_interval_summary(diagnostic_only))
        for col, value in summary.items():
            df.at[idx, col] = value
    df.to_csv(path, index=False, encoding="utf-8-sig")
    return df


def _postprocess_diagnostic_pet_interval_deltas(
    out_dir: Path,
    seed: int,
    pet_observability_mode: str = PET_OBSERVABILITY_RANDOMIZED,
) -> dict[str, dict[str, Any]]:
    """Patch existing diagnostic outputs with randomized PET vehicle exit deltas."""
    seed_dir = f"seed{seed:05d}"
    baseline_dir = out_dir / "baseline" / seed_dir
    smart_dir = out_dir / "smart" / seed_dir
    audit_dir = out_dir / "audits" / seed_dir
    audit_dir.mkdir(parents=True, exist_ok=True)
    diagnostic_only = pet_observability_mode == PET_OBSERVABILITY_RANDOMIZED

    def _pet_intervals_from_obs(seed_dir_path: Path) -> pd.DataFrame:
        obs = _read_csv_or_empty(seed_dir_path / "csv" / "observations.csv")
        if obs.empty or "source" not in obs.columns:
            return pd.DataFrame()
        sub = obs[obs["source"] == "pet_vehicle_intervals"].drop(columns=["source"]).reset_index(drop=True)
        return sub

    base = _diagnostic_interval_side(_pet_intervals_from_obs(baseline_dir), "baseline")
    smart = _diagnostic_interval_side(_pet_intervals_from_obs(smart_dir), "smart")
    delta = base.merge(smart, on=["vehicle_id", "occurrence_index"], how="inner")
    if not delta.empty:
        delta["exit_delta"] = (
            pd.to_numeric(delta["exit_time_smart"], errors="coerce")
            - pd.to_numeric(delta["exit_time_baseline"], errors="coerce")
        )
    audit_columns = [
        "vehicle_id", "occurrence_index",
        "enter_time_baseline", "enter_time_smart",
        "exit_time_baseline", "exit_time_smart", "exit_delta",
        "controlled_signal_affected_baseline", "controlled_signal_affected_smart",
    ]
    for col in audit_columns:
        if col not in delta.columns:
            delta[col] = None
    delta[audit_columns].to_csv(audit_dir / "diagnostic_pet_interval_delta_audit.csv", index=False)

    vid_to_cid = _diagnostic_vehicle_crosswalk_lookup(audit_dir)
    if delta.empty:
        summaries_by_cid: dict[str, dict[str, Any]] = {}
    else:
        delta["_crosswalk_id"] = delta["vehicle_id"].astype(str).map(vid_to_cid).fillna("")
        if not vid_to_cid:
            delta["_crosswalk_id"] = ""
        summaries_by_cid = {
            str(cid): _summarize_diagnostic_interval_delta(sub, diagnostic_only)
            for cid, sub in delta.groupby("_crosswalk_id")
            if str(cid)
        }
        if not summaries_by_cid:
            summaries_by_cid = {"": _summarize_diagnostic_interval_delta(delta, diagnostic_only)}

    for result_path in (
        baseline_dir / "csv" / "simulation_result.csv",
        smart_dir / "csv" / "simulation_result.csv",
        smart_dir / "csv" / "simulation_result_with_baseline.csv",
    ):
        df = _read_csv_or_empty(result_path)
        if df.empty:
            continue
        if "" in summaries_by_cid and "crosswalk_id" in df.columns:
            summaries = {str(cid): summaries_by_cid[""] for cid in df["crosswalk_id"].dropna().astype(str).unique()}
        else:
            summaries = summaries_by_cid
        _patch_simulation_result_with_diagnostic_delta(result_path, summaries, diagnostic_only)

    pet_audit_path = audit_dir / "pet_observability_audit.csv"
    pet_audit = _read_csv_or_empty(pet_audit_path)
    if not pet_audit.empty and "crosswalk_id" in pet_audit.columns:
        for col in _DIAGNOSTIC_PET_INTERVAL_COLUMNS:
            if col not in pet_audit.columns:
                pet_audit[col] = None
        for idx, row in pet_audit.iterrows():
            cid = str(row.get("crosswalk_id", ""))
            summary = summaries_by_cid.get(cid, summaries_by_cid.get("", _blank_diagnostic_pet_interval_summary(diagnostic_only)))
            for col, value in summary.items():
                pet_audit.at[idx, col] = value
            if (
                diagnostic_only
                and summary.get("diagnostic_vehicle_exit_delta_status") == "diagnostic_vehicle_delay_observed"
                and str(row.get("baseline_smart_pet_diff_exists", False)).lower() not in {"true", "1", "yes"}
            ):
                pet_audit.at[idx, "observability_status"] = "diagnostic_vehicle_delay_observed"
        pet_audit.to_csv(pet_audit_path, index=False, encoding="utf-8-sig")

    legacy_audit_path = out_dir / "pet_observability_audit.csv"
    if legacy_audit_path.exists():
        pet_audit = _read_csv_or_empty(pet_audit_path)
        if not pet_audit.empty:
            pet_audit.to_csv(legacy_audit_path, index=False, encoding="utf-8-sig")

    return summaries_by_cid


def _write_pet_observability_audit(
    out_dir: Path,
    seed: int,
    scenario_dfs: dict[str, pd.DataFrame],
    pet_observability_mode: str = PET_OBSERVABILITY_GLOBAL_ONLY,
    vehicle_route_cache_meta: dict[str, Any] | None = None,
) -> None:
    smart_dir = out_dir / "smart" / f"seed{seed:05d}"
    audit_dir = out_dir / "audits" / f"seed{seed:05d}"
    audit_dir.mkdir(parents=True, exist_ok=True)
    columns = [
        "seed", "crosswalk_id", "extension_time",
        "diagnostic_only", "pet_observability_mode", "diagnostic_random_seed",
        "targeted_vehicle_count", "targeted_vehicle_in_ext_window_count",
        "randomized_vehicle_count", "randomized_vehicle_in_ext_window_count",
        "controlled_vehicle_count_ext_pm10s", "controlled_halting_count_ext_pm10s",
        "controlled_signal_affected_pet_vehicle_count", "pet_event_count",
        "low_pet_event_count", "pet_min", "pet_p10", "pet_mean",
        "pet_delta_nonzero", "pet_delta_reason",
        "baseline_smart_pet_diff_exists", "observability_status",
        *_DIAGNOSTIC_PET_INTERVAL_COLUMNS,
    ]
    ext_path = smart_dir / "extension_trace.csv"
    if not ext_path.exists():
        pd.DataFrame(columns=columns).to_csv(audit_dir / "pet_observability_audit.csv", index=False)
        return
    ext_df = _read_csv_or_empty(ext_path)
    controlled_trace = _read_csv_or_empty(smart_dir / "controlled_vehicle_trace.csv")
    controlled_steps = _read_csv_or_empty(smart_dir / "controlled_lane_effect_trace.csv")
    pet_intervals = _read_csv_or_empty(smart_dir / "pet_vehicle_intervals.csv")
    targeted_audit = _read_csv_or_empty(audit_dir / "diagnostic_targeted_vehicle_audit.csv")
    random_audit = _read_csv_or_empty(audit_dir / "diagnostic_random_vehicle_audit.csv")
    diagnostic_only = pet_observability_mode in {PET_OBSERVABILITY_TARGETED, PET_OBSERVABILITY_RANDOMIZED}
    targeted_ids_by_cid: dict[str, set[str]] = {}
    randomized_ids_by_cid: dict[str, set[str]] = {}
    if not targeted_audit.empty and "targeted_vehicle_id" in targeted_audit.columns and "crosswalk_id" in targeted_audit.columns:
        valid_targeted = targeted_audit[
            targeted_audit["targeted_vehicle_id"].fillna("").astype(str).str.len() > 0
        ]
        for cid, sub in valid_targeted.groupby(valid_targeted["crosswalk_id"].astype(str)):
            targeted_ids_by_cid[str(cid)] = set(sub["targeted_vehicle_id"].astype(str).tolist())
    if not random_audit.empty and "randomized_vehicle_id" in random_audit.columns and "crosswalk_id" in random_audit.columns:
        valid_random = random_audit[
            random_audit["randomized_vehicle_id"].fillna("").astype(str).str.len() > 0
        ]
        for cid, sub in valid_random.groupby(valid_random["crosswalk_id"].astype(str)):
            randomized_ids_by_cid[str(cid)] = set(sub["randomized_vehicle_id"].astype(str).tolist())
    diagnostic_random_seed = _as_int((vehicle_route_cache_meta or {}).get("diagnostic_random_seed", 0), 0)
    if diagnostic_random_seed <= 0 and not random_audit.empty and "diagnostic_random_seed" in random_audit.columns:
        diagnostic_random_seed = _as_int(random_audit["diagnostic_random_seed"].dropna().iloc[0] if not random_audit["diagnostic_random_seed"].dropna().empty else 0, 0)
    baseline_df = scenario_dfs.get("baseline", pd.DataFrame())
    smart_df = scenario_dfs.get("smart", pd.DataFrame())
    rows: list[dict[str, Any]] = []

    def _delta_metrics(brow: pd.DataFrame, srow: pd.DataFrame) -> tuple[bool, str]:
        metric_names = ["pet_event_count", "low_pet_event_count", "pet_min", "pet_p10", "pet_mean"]
        diffs: list[str] = []
        if brow.empty or srow.empty:
            return False, "missing_baseline_or_smart_row"
        for name in metric_names:
            b_val = pd.to_numeric(brow.get(name), errors="coerce") if name in brow.columns else pd.Series(dtype=float)
            s_val = pd.to_numeric(srow.get(name), errors="coerce") if name in srow.columns else pd.Series(dtype=float)
            b_num = float(b_val.dropna().iloc[0]) if not b_val.dropna().empty else None
            s_num = float(s_val.dropna().iloc[0]) if not s_val.dropna().empty else None
            if b_num is None or s_num is None:
                continue
            if abs(b_num - s_num) > 1e-9:
                diffs.append(name)
        return bool(diffs), "|".join(diffs) if diffs else "no_difference"

    for _, ext in ext_df.iterrows():
        cid = str(ext.get("crosswalk_id", ""))
        t0 = float(ext.get("time", 0.0))
        start = t0 - 10.0
        end = t0 + 10.0
        ct_win = controlled_trace
        if not ct_win.empty:
            ct_win = ct_win[
                (ct_win["crosswalk_id"].astype(str) == cid)
                & pd.to_numeric(ct_win["t"], errors="coerce").between(start, end)
            ]
        step_win = controlled_steps
        if not step_win.empty:
            step_win = step_win[
                (step_win["crosswalk_id"].astype(str) == cid)
                & pd.to_numeric(step_win["t"], errors="coerce").between(start, end)
            ]
        pet_win = pet_intervals
        if not pet_win.empty:
            affected = pet_win.get("controlled_signal_affected", False)
            if not isinstance(affected, pd.Series):
                affected = pd.Series([False] * len(pet_win), index=pet_win.index)
            affected_bool = affected.astype(str).str.lower().isin({"true", "1", "yes"})
            overlap_bool = pet_win.apply(lambda row: _interval_overlaps(row, start, end), axis=1)
            pet_win = pet_win[affected_bool & overlap_bool]
        srow = smart_df[smart_df["crosswalk_id"].astype(str) == cid]
        brow = baseline_df[baseline_df["crosswalk_id"].astype(str) == cid]
        smart_pet_event = _first_numeric(srow, "pet_event_count", 0.0)
        smart_low_pet_event = _first_numeric(srow, "low_pet_event_count", 0.0)
        smart_pet_min = _first_numeric(srow, "pet_min")
        smart_pet_p10 = _first_numeric(srow, "pet_p10")
        smart_pet_mean = _first_numeric(srow, "pet_mean")
        pet_diff, pet_reason = _delta_metrics(brow, srow)
        controlled_vehicle_count = int(ct_win["vehicle_id"].nunique()) if not ct_win.empty and "vehicle_id" in ct_win.columns else 0
        targeted_ids = targeted_ids_by_cid.get(cid, set())
        randomized_ids = randomized_ids_by_cid.get(cid, set())
        targeted_count = int(len(targeted_ids))
        randomized_count = int(len(randomized_ids))
        targeted_in_window = 0
        randomized_in_window = 0
        if not ct_win.empty and "vehicle_id" in ct_win.columns:
            veh_ids = ct_win["vehicle_id"].astype(str)
            if targeted_ids:
                targeted_in_window = int(ct_win[veh_ids.isin(targeted_ids)]["vehicle_id"].nunique())
            if randomized_ids:
                randomized_in_window = int(ct_win[veh_ids.isin(randomized_ids)]["vehicle_id"].nunique())
        controlled_halting = (
            int(pd.to_numeric(step_win["halting_count"], errors="coerce").fillna(0).max())
            if not step_win.empty and "halting_count" in step_win.columns else 0
        )
        affected_pet_vehicle_count = int(pet_win["vehicle_id"].nunique()) if not pet_win.empty and "vehicle_id" in pet_win.columns else 0
        if pet_observability_mode == PET_OBSERVABILITY_RANDOMIZED and randomized_count > 0 and randomized_in_window <= 0:
            status = "randomized_vehicle_missed_extension_window"
        elif pet_observability_mode == PET_OBSERVABILITY_TARGETED and targeted_count > 0 and targeted_in_window <= 0:
            status = "targeted_vehicle_missed_extension_window"
        elif controlled_vehicle_count <= 0:
            status = "no_controlled_vehicle_in_extension_window"
        elif affected_pet_vehicle_count <= 0:
            status = "no_controlled_signal_affected_pet_vehicle"
        elif not smart_pet_event or float(smart_pet_event) <= 0:
            status = "no_pet_events"
        elif pet_diff:
            status = "observable_pet_delta"
        else:
            status = "observable_no_pet_delta" if diagnostic_only else "observable_no_pet_diff"
        rows.append({
            "seed": int(seed),
            "crosswalk_id": cid,
            "extension_time": round(t0, 1),
            "diagnostic_only": bool(diagnostic_only),
            "pet_observability_mode": str(pet_observability_mode),
            "diagnostic_random_seed": int(diagnostic_random_seed),
            "targeted_vehicle_count": targeted_count,
            "targeted_vehicle_in_ext_window_count": targeted_in_window,
            "randomized_vehicle_count": randomized_count,
            "randomized_vehicle_in_ext_window_count": randomized_in_window,
            "controlled_vehicle_count_ext_pm10s": controlled_vehicle_count,
            "controlled_halting_count_ext_pm10s": controlled_halting,
            "controlled_signal_affected_pet_vehicle_count": affected_pet_vehicle_count,
            "pet_event_count": smart_pet_event,
            "low_pet_event_count": smart_low_pet_event,
            "pet_min": smart_pet_min,
            "pet_p10": smart_pet_p10,
            "pet_mean": smart_pet_mean,
            "pet_delta_nonzero": pet_diff,
            "pet_delta_reason": pet_reason,
            "baseline_smart_pet_diff_exists": pet_diff,
            "observability_status": status,
        })
    pd.DataFrame(rows, columns=columns).to_csv(audit_dir / "pet_observability_audit.csv", index=False)


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
    ped_repeat_spacing_sec: float = 2.0,
    trace_mode: str = "full",
    trace_pre_window_sec: float = 10.0,
    trace_post_window_sec: float = 10.0,
    traffic_watch_sample_interval_sec: float = 0.1,
    vehicle_route_cache_dir: "Path | None" = None,
    pet_observability_mode: str = PET_OBSERVABILITY_GLOBAL_ONLY,
    pet_diagnostic_random_seed: int = 0,
    pet_diagnostic_vehicles_per_crosswalk: int = _RANDOMIZED_DIAGNOSTIC_DEFAULT_VEHICLES_PER_CROSSWALK,
    pet_diagnostic_jitter_sec: float = _RANDOMIZED_DIAGNOSTIC_DEFAULT_JITTER_SEC,
    forced_extension_policy_t: "float | None" = None,
) -> pd.DataFrame:
    import shutil, tempfile

    group_name = candidate_csv.stem.replace("_candidates", "")
    candidate_df_raw = _load_candidate(candidate_csv, ped_count, ped_repeat_spacing_sec=ped_repeat_spacing_sec)
    if crosswalk_id:
        candidate_df_raw = candidate_df_raw[
            candidate_df_raw["crosswalk_id"].astype(str) == crosswalk_id
        ].reset_index(drop=True)

    # phase-aligned depart: 원본 candidate_df에 depart_time 컬럼만 merge
    # (_phase_aligned_depart_plan은 새 DataFrame 반환 → 전체 교체 금지)
    depart_plan = _phase_aligned_depart_plan(
        net_file, candidate_df_raw,
        ped_repeat_count=ped_count,
        ped_repeat_spacing_sec=ped_repeat_spacing_sec,
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

    # vehicle routes: one seed-level file reused across baseline/smart.
    veh_route_dir = out_dir / "_vehicle_routes" / f"seed{seed:05d}"
    global_veh_file, vehicle_route_cache_meta = _simple_weighted_vehicle_routes(
        net_file=net_file,
        sim_duration=duration,
        seed=seed,
        out_dir=veh_route_dir,
        warmup_sec=warmup_sec,
        vehicle_route_cache_dir=vehicle_route_cache_dir,
    )
    if pet_observability_mode == PET_OBSERVABILITY_TARGETED:
        if global_veh_file is None:
            raise RuntimeError("targeted_controlled diagnostic requires a background vehicle route file")
        global_veh_file, vehicle_route_cache_meta = _append_targeted_controlled_vehicle_routes(
            background_route_file=global_veh_file,
            net_file=net_file,
            candidate_df=candidate_df,
            seed=seed,
            out_dir=out_dir,
            warmup_sec=warmup_sec,
            duration=duration,
            vehicle_route_cache_meta=vehicle_route_cache_meta,
        )
    elif pet_observability_mode == PET_OBSERVABILITY_RANDOMIZED:
        if global_veh_file is None:
            raise RuntimeError("randomized_controlled diagnostic requires a background vehicle route file")
        global_veh_file, vehicle_route_cache_meta = _append_randomized_controlled_vehicle_routes(
            background_route_file=global_veh_file,
            net_file=net_file,
            candidate_df=candidate_df,
            seed=seed,
            out_dir=out_dir,
            warmup_sec=warmup_sec,
            duration=duration,
            vehicle_route_cache_meta=vehicle_route_cache_meta,
            diagnostic_random_seed=int(pet_diagnostic_random_seed),
            vehicles_per_crosswalk=int(pet_diagnostic_vehicles_per_crosswalk),
            jitter_sec=float(pet_diagnostic_jitter_sec),
            forced_extension_policy_t=forced_extension_policy_t,
        )
    _copy_vehicle_route_audits(out_dir, seed, vehicle_route_cache_meta)

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
                trace_mode=trace_mode,
                trace_pre_window_sec=trace_pre_window_sec,
                trace_post_window_sec=trace_post_window_sec,
                traffic_watch_sample_interval_sec=traffic_watch_sample_interval_sec,
                vehicle_route_cache_meta=vehicle_route_cache_meta,
                pet_observability_mode=pet_observability_mode,
                forced_extension_policy_t=forced_extension_policy_t,
            )
        scenario_dfs[scenario] = df
        print(f"  [{scenario}] seed{seed:05d} done — {len(df)} rows → {result_dir / 'csv' / 'simulation_result.csv'}")

    _write_route_hash_audit(out_dir, seed, scenario_dfs)
    _write_pet_observability_audit(
        out_dir,
        seed,
        scenario_dfs,
        pet_observability_mode,
        vehicle_route_cache_meta,
    )
    if pet_observability_mode == PET_OBSERVABILITY_RANDOMIZED:
        _postprocess_diagnostic_pet_interval_deltas(out_dir, seed, pet_observability_mode)
        for scenario in list(scenario_dfs):
            patched = _read_csv_or_empty(out_dir / scenario / f"seed{seed:05d}" / "csv" / "simulation_result.csv")
            if not patched.empty:
                scenario_dfs[scenario] = patched

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
        combined.to_csv(smart_result_dir / "csv" / "simulation_result_with_baseline.csv",
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


def _aggregate_seed_audits(output_dir: Path) -> None:
    audit_names = [
        "vehicle_edge_allocation_audit.csv",
        "vehicle_edge_coverage_summary.csv",
        "diagnostic_targeted_vehicle_audit.csv",
        "diagnostic_random_vehicle_audit.csv",
        "baseline_smart_route_hash_audit.csv",
        "pet_observability_audit.csv",
    ]
    for name in audit_names:
        frames: list[pd.DataFrame] = []
        for path in sorted((output_dir / "audits").glob(f"seed*/{name}")):
            try:
                frames.append(pd.read_csv(path))
            except Exception:
                continue
        if frames:
            pd.concat(frames, ignore_index=True).to_csv(output_dir / name, index=False)


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
    parser.add_argument("--ped-repeat-spacing-sec", type=float, default=2.0,
                        help="보행자 반복 출발 간격 (초, 기본 2.0). 크게 하면 crossing 시간 분산.")
    parser.add_argument("--trace-mode", choices=["off", "minimal", "full"], default=None,
                        help="Trace CSV output mode. default: diagnostic_fast=minimal, otherwise=full.")
    parser.add_argument("--trace-pre-window-sec", type=float, default=10.0,
                        help="minimal trace ring-buffer window before an extension (default 10s).")
    parser.add_argument("--trace-post-window-sec", type=float, default=10.0,
                        help="minimal trace capture window after an extension (default 10s).")
    parser.add_argument("--traffic-watch-sample-interval-sec", type=float, default=None,
                        help="Traffic aggregate watcher interval. default: diagnostic_fast=1.0, otherwise=0.1.")
    parser.add_argument("--vehicle-route-cache-dir", type=Path, default=None,
                        help="Shared vehicle route cache directory, independent from output-dir.")
    parser.add_argument(
        "--pet-observability-mode",
        choices=[PET_OBSERVABILITY_GLOBAL_ONLY, PET_OBSERVABILITY_TARGETED, PET_OBSERVABILITY_RANDOMIZED],
        default=PET_OBSERVABILITY_GLOBAL_ONLY,
        help=(
            "global_only(기본): 전역 배경교통만 사용. "
            "targeted_controlled: PET 관측성 진단 전용 controlled-lane 차량 추가. "
            "randomized_controlled: controlled 배치 랜덤화 진단."
        ),
    )
    parser.add_argument(
        "--pet-diagnostic-random-seed",
        type=int,
        default=0,
        help="randomized_controlled 전용 보조 seed. run seed 와 함께 재현성 유지.",
    )
    parser.add_argument(
        "--pet-diagnostic-vehicles-per-crosswalk",
        type=int,
        default=_RANDOMIZED_DIAGNOSTIC_DEFAULT_VEHICLES_PER_CROSSWALK,
        help="randomized_controlled 에서 crosswalk 당 diagnostic 차량 수 (기본 18, 최대 20).",
    )
    parser.add_argument(
        "--pet-diagnostic-jitter-sec",
        type=float,
        default=_RANDOMIZED_DIAGNOSTIC_DEFAULT_JITTER_SEC,
        help="randomized_controlled 출발시각 jitter 범위 초 단위 절대값 (기본 25.0).",
    )
    parser.add_argument(
        "--diagnostic-force-extension-policy-time",
        type=float, default=None,
        dest="diagnostic_force_extension_policy_time",
        help=(
            "diagnostic_fast + scenario-mode both + randomized_controlled 전용. "
            "smart extension을 warmup_sec + 이 값(policy time)에 강제 발동하고 자연 발동을 억제. "
            "예: --diagnostic-force-extension-policy-time 5.0 → warmup=30이면 SUMO t=35.0에 발동. "
            "forced time 이후 첫 보행자 녹색 step에 발동."
        ),
    )
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
    trace_mode = args.trace_mode or ("minimal" if run_profile == "diagnostic_fast" else "full")
    traffic_watch_sample_interval_sec = (
        args.traffic_watch_sample_interval_sec
        if args.traffic_watch_sample_interval_sec is not None
        else (1.0 if run_profile == "diagnostic_fast" else 0.1)
    )
    if args.pet_observability_mode in {PET_OBSERVABILITY_TARGETED, PET_OBSERVABILITY_RANDOMIZED}:
        if run_profile != "diagnostic_fast":
            raise SystemExit("--pet-observability-mode targeted_controlled/randomized_controlled is diagnostic_fast only")
        if args.scenario_mode != "both":
            raise SystemExit("--pet-observability-mode targeted_controlled/randomized_controlled requires --scenario-mode both")
    if args.diagnostic_force_extension_policy_time is not None:
        if run_profile != "diagnostic_fast":
            raise SystemExit("--diagnostic-force-extension-policy-time requires --profile diagnostic_fast")
        if args.scenario_mode != "both":
            raise SystemExit("--diagnostic-force-extension-policy-time requires --scenario-mode both")
        if args.pet_observability_mode != PET_OBSERVABILITY_RANDOMIZED:
            raise SystemExit("--diagnostic-force-extension-policy-time requires --pet-observability-mode randomized_controlled")
    if args.pet_diagnostic_vehicles_per_crosswalk > _RANDOMIZED_DIAGNOSTIC_MAX_VEHICLES_PER_CROSSWALK:
        print(
            f"[enhanced smoke][WARN] pet-diagnostic-vehicles-per-crosswalk capped at "
            f"{_RANDOMIZED_DIAGNOSTIC_MAX_VEHICLES_PER_CROSSWALK}",
            flush=True,
        )
        args.pet_diagnostic_vehicles_per_crosswalk = _RANDOMIZED_DIAGNOSTIC_MAX_VEHICLES_PER_CROSSWALK

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
            ped_repeat_spacing_sec=args.ped_repeat_spacing_sec,
            trace_mode=trace_mode,
            trace_pre_window_sec=args.trace_pre_window_sec,
            trace_post_window_sec=args.trace_post_window_sec,
            traffic_watch_sample_interval_sec=traffic_watch_sample_interval_sec,
            vehicle_route_cache_dir=args.vehicle_route_cache_dir,
            pet_observability_mode=args.pet_observability_mode,
            pet_diagnostic_random_seed=args.pet_diagnostic_random_seed,
            pet_diagnostic_vehicles_per_crosswalk=args.pet_diagnostic_vehicles_per_crosswalk,
            pet_diagnostic_jitter_sec=args.pet_diagnostic_jitter_sec,
            forced_extension_policy_t=args.diagnostic_force_extension_policy_time,
        )
        all_frames.append(df)

    _aggregate_seed_audits(args.output_dir)
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
        print(f"Results → {args.output_dir}/baseline/seed{seeds[0]:05d}/csv/simulation_result.csv")

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
