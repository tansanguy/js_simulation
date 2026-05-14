#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import random
import shutil
import subprocess
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd

try:
    import traci  # type: ignore
except Exception:  # pragma: no cover
    traci = None

BASE_DIR = Path(__file__).resolve().parents[1]
RESULT_DIR = BASE_DIR / "result"
FACTORY_TABLE = RESULT_DIR / "phase_next_real_csv_crossing_factory_20260514_103928" / "real_csv_crossing_factory_table.csv"
CROSSING_INVENTORY = RESULT_DIR / "phase_next_pedestrian_augmented_network_20260514_120939" / "generated_crossing_edge_inventory.csv"


@dataclass
class Candidate:
    crosswalk_id: str
    nearest_junction_id: str
    tls_id_used: str
    crossing_id: str
    ped_link_index: int
    ped_depart_offset_sec: float
    crossing_edge_id: str
    route_from_edge: str
    route_to_edge: str
    ped_repeat_count: int
    ped_repeat_spacing_sec: float
    source_file: str
    batch_network_file: str


def _parse_edges(value: Any) -> list[str]:
    if value is None:
        return []
    text = str(value).strip()
    if not text or text in {"nan", "None"}:
        return []
    if text.startswith("[") and text.endswith("]"):
        try:
            parsed = json.loads(text.replace("'", '"'))
            if isinstance(parsed, list):
                return [str(x).strip() for x in parsed if str(x).strip()]
        except Exception:
            pass
        text = text.strip("[]")
    text = text.replace(",", " ").replace("|", " ")
    return [tok.strip().strip("'").strip('"') for tok in text.split() if tok.strip()]


def _load_csv(path: Path) -> pd.DataFrame:
    if not path.exists():
        raise FileNotFoundError(path)
    return pd.read_csv(path)


def _resolve_batch_network(source_file: str, override: str | None) -> str:
    if override:
        return str(Path(override).expanduser().resolve())
    if "batch_03_run/batch_promoted.csv" in source_file:
        return str((RESULT_DIR / "phase_next_recovery_command_plan_20260514_200908" / "batch_03_run" / "recovery_tls_batch_network_v1.net.xml").resolve())
    if "batch_02_run/batch_promoted.csv" in source_file:
        return str((RESULT_DIR / "phase_next_recovery_command_plan_20260514_200908" / "batch_02_run" / "recovery_tls_batch_network_v1.net.xml").resolve())
    if "batch_01_run/batch_promoted.csv" in source_file:
        return str((RESULT_DIR / "phase_next_recovery_command_plan_20260514_200908" / "batch_01_run" / "recovery_tls_batch_network_v1.net.xml").resolve())
    return str((RESULT_DIR / "phase_next_recovery_command_plan_20260514_200908" / "batch_03_run" / "recovery_tls_batch_network_v1.net.xml").resolve())


def _choose_route_pair(incident_edges: list[str], nearest_road_edge: str) -> tuple[str, str]:
    if not incident_edges:
        raise ValueError("incident road edges missing")
    from_edge = nearest_road_edge if nearest_road_edge else incident_edges[0]
    to_edge = None
    from_core = from_edge.lstrip("-")
    for edge in incident_edges:
        if edge == from_edge:
            continue
        if edge.lstrip("-") == from_core:
            continue
        if edge.startswith("-") != from_edge.startswith("-"):
            to_edge = edge
            break
    if to_edge is None:
        for edge in incident_edges:
            if edge != from_edge:
                to_edge = edge
                break
    if to_edge is None:
        to_edge = from_edge
    return from_edge, to_edge


def _find_crossing_edge(node_id: str, route_from_edge: str, inventory: pd.DataFrame) -> str:
    node_rows = inventory[inventory["node_id"].astype(str) == str(node_id)].copy()
    if node_rows.empty:
        return ""
    core = route_from_edge.lstrip("-")
    canon = node_rows["crossingEdges_canonical"].astype(str).str.replace("-", "", regex=False)
    match = node_rows[canon == core]
    if not match.empty:
        return str(match.iloc[0]["crossing_edge_id"])
    return str(node_rows.iloc[0]["crossing_edge_id"])


def _candidate_crossing_roads(node_id: str, route_from_edge: str, crossing_edge_id: str, inventory: pd.DataFrame) -> set[str]:
    roads = {str(crossing_edge_id)} if crossing_edge_id else set()
    node_rows = inventory[inventory["node_id"].astype(str) == str(node_id)].copy()
    if not node_rows.empty:
        roads.update(node_rows["crossing_edge_id"].astype(str).tolist())
        roads.update(node_rows["crossingEdges"].astype(str).tolist())
        roads.update(node_rows["crossingEdges_canonical"].astype(str).tolist())
    roads.add(f":{node_id}_c0")
    roads.add(f":{node_id}_c1")
    roads.add(f":{node_id}_c2")
    roads.add(f":{node_id}_w0")
    roads.add(f":{node_id}_w1")
    roads.add(f":{node_id}_w2")
    roads.discard("")
    roads.discard("nan")
    roads.discard("None")
    return {road for road in roads if road}


def _safe_get(row: Any, col: str, default: Any = None) -> Any:
    if row is None:
        return default
    try:
        value = row[col]
    except Exception:
        return default
    if pd.isna(value):
        return default
    return value


def _write_compare_warning(compare_dir: Path, scenario: str, reason: str, details: dict[str, Any]) -> None:
    compare_dir.mkdir(parents=True, exist_ok=True)
    warning = {
        "scenario": scenario,
        "reason": reason,
        "details": details,
    }
    (compare_dir / "phase6_smoke_compare_warning.json").write_text(json.dumps(warning, ensure_ascii=False, indent=2), encoding="utf-8")
    (compare_dir / "phase6_smoke_compare_warning.md").write_text(
        "\n".join(
            [
                "# phase 6 smoke compare warning",
                "",
                f"- scenario: {scenario}",
                f"- reason: {reason}",
                "",
                "## details",
                "",
                *[f"- {k}: {v}" for k, v in details.items()],
            ]
        )
        + "\n",
        encoding="utf-8",
    )


def _build_candidate_table(candidate_csv: Path, net_file: Path | None) -> pd.DataFrame:
    raw = _load_csv(candidate_csv)
    factory = _load_csv(FACTORY_TABLE)
    inventory = _load_csv(CROSSING_INVENTORY)

    needed = [
        "crosswalk_id",
        "nearest_junction_id",
        "tls_id_used",
        "ped_link_indices",
        "source_file",
    ]
    for col in needed:
        if col not in raw.columns:
            raise ValueError(f"candidate csv missing column: {col}")

    rows: list[dict[str, Any]] = []
    for _, r in raw.iterrows():
        cid = str(r["crosswalk_id"])
        jid = str(r["nearest_junction_id"])
        tls_id = str(r["tls_id_used"])
        crossing_id = str(r.get("crossing_id", "") or "")
        ped_indices = _parse_edges(r.get("ped_link_indices"))
        if not ped_indices:
            raise ValueError(f"ped_link_indices missing for {cid}")
        ped_link_index = int(ped_indices[0])
        ped_depart_offset_sec = float(r.get("ped_depart_offset_sec", 0) or 0)

        route_from_edge = str(r.get("route_from_edge", "") or "")
        route_to_edge = str(r.get("route_to_edge", "") or "")
        crossing_edge_id = str(r.get("crossing_edge_id", "") or "")
        ped_repeat_count = int(r.get("ped_repeat_count", 1) or 1)
        ped_repeat_spacing_sec = float(r.get("ped_repeat_spacing_sec", 1.5) or 1.5)

        frow = factory[factory["crosswalk_id"].astype(str) == cid]
        incident_edges: list[str] = []
        nearest_road_edge = ""
        if not frow.empty:
            fr = frow.iloc[0]
            incident_edges = _parse_edges(fr.get("incident_road_edges"))
            nearest_road_edge = str(fr.get("nearest_road_edge_id") or "")
            if not route_from_edge or not route_to_edge:
                route_from_edge, route_to_edge = _choose_route_pair(incident_edges, nearest_road_edge)

        if not route_from_edge:
            raise ValueError(f"route_from_edge missing for {cid}")
        if not route_to_edge:
            raise ValueError(f"route_to_edge missing for {cid}")
        if not crossing_edge_id:
            crossing_edge_id = crossing_id or _find_crossing_edge(jid, route_from_edge, inventory)
        rows.append(
            {
                "crosswalk_id": cid,
                "nearest_junction_id": jid,
                "tls_id_used": tls_id,
                "crossing_id": crossing_id,
                "ped_link_index": ped_link_index,
                "ped_depart_offset_sec": ped_depart_offset_sec,
                "crossing_edge_id": crossing_edge_id,
                "route_from_edge": route_from_edge,
                "route_to_edge": route_to_edge,
                "ped_repeat_count": ped_repeat_count,
                "ped_repeat_spacing_sec": ped_repeat_spacing_sec,
                "route_reason": "from_nearest_road_edge_to_other_incident_edge",
                "incident_road_edges": "|".join(incident_edges),
                "nearest_road_edge_id": nearest_road_edge,
                "source_file": str(r.get("source_file", "")),
                "batch_network_file": _resolve_batch_network(str(r.get("source_file", "")), str(net_file) if net_file else None),
                "final_verdict": str(r.get("final_verdict", "")),
                "step_test_ok": str(r.get("step_test_ok", "")),
                "controlled_links_count": str(r.get("controlled_links_count", "")),
            }
        )

    out = pd.DataFrame(rows)
    if out["batch_network_file"].nunique() != 1:
        raise ValueError("smoke candidates span multiple net files; split run required")
    return out


def _write_routes(
    candidate_df: pd.DataFrame,
    out_dir: Path,
    seed: int,
    duration: int,
    include_vehicles: bool,
) -> tuple[Path, Path | None, Path]:
    rng = random.Random(seed)
    ped_path = out_dir / "demand_pedestrian.rou.xml"
    ped_lines = ['<?xml version="1.0" encoding="UTF-8"?>', '<routes xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">', '    <vType id="pedestrian_type" vClass="pedestrian"/>']
    veh_path = out_dir / "demand_vehicle.rou.xml" if include_vehicles else None
    veh_lines = ['<?xml version="1.0" encoding="UTF-8"?>', '<routes xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">', '    <vType id="car" vClass="passenger" maxSpeed="15.0" accel="2.6" decel="4.5"/>'] if include_vehicles else []
    ped_records: list[dict[str, Any]] = []

    for idx, row in enumerate(candidate_df.itertuples(index=False), start=1):
        repeat_count = max(1, int(getattr(row, "ped_repeat_count", 1) or 1))
        repeat_spacing = float(getattr(row, "ped_repeat_spacing_sec", 1.5) or 1.5)
        for rep_idx in range(repeat_count):
            ped_depart = round(
                10.0
                + (idx - 1) * 40.0
                + float(getattr(row, "ped_depart_offset_sec", 0.0))
                + (rep_idx * repeat_spacing)
                + rng.uniform(-1.0, 1.0),
                1,
            )
            ped_records.append(
                {
                    "person_id": f"ped_{idx}_{rep_idx}_{row.crosswalk_id}",
                    "crosswalk_id": str(row.crosswalk_id),
                    "depart": max(1.0, ped_depart),
                    "route_from_edge": str(row.route_from_edge),
                    "route_to_edge": str(row.route_to_edge),
                }
            )
        if include_vehicles:
            veh_depart = round(5.0 + (idx - 1) * 40.0 + rng.uniform(-2.0, 2.0), 1)
            veh_lines.append(
                f'    <route id="veh_route_{idx}" edges="{row.route_from_edge} {row.route_to_edge}"/>'
            )
            veh_lines.append(
                f'    <vehicle id="veh_{idx}_{row.crosswalk_id}" type="car" route="veh_route_{idx}" depart="{max(1.0, veh_depart)}"/>'
            )

    ped_records.sort(key=lambda rec: (float(rec["depart"]), str(rec["person_id"])))
    for rec in ped_records:
        ped_lines.append(
            f'    <person id="{rec["person_id"]}" type="pedestrian_type" depart="{rec["depart"]}">'
        )
        ped_lines.append(
            f'        <walk from="{rec["route_from_edge"]}" to="{rec["route_to_edge"]}"/>'
        )
        ped_lines.append("    </person>")
    ped_lines.append("</routes>")
    ped_path.write_text("\n".join(ped_lines) + "\n", encoding="utf-8")
    if include_vehicles and veh_path is not None:
        veh_lines.append("</routes>")
        veh_path.write_text("\n".join(veh_lines) + "\n", encoding="utf-8")
    ped_summary_path = out_dir / "pedestrian_route_order_debug.csv"
    ped_summary_df = pd.DataFrame(
        [
            {
                "sorted_index": idx + 1,
                "person_id": rec["person_id"],
                "crosswalk_id": rec["crosswalk_id"],
                "depart": rec["depart"],
                "route_from_edge": rec["route_from_edge"],
                "route_to_edge": rec["route_to_edge"],
            }
            for idx, rec in enumerate(ped_records)
        ]
    )
    ped_summary_df["depart_sorted_ok"] = ped_summary_df["depart"].is_monotonic_increasing
    ped_summary_df.to_csv(ped_summary_path, index=False)
    return ped_path, veh_path, ped_summary_path


def _write_sumocfg(cfg_path: Path, net_file: Path, ped_file: Path, veh_file: Path | None, duration: int, step_length: float) -> Path:
    route_files = [str(ped_file)]
    if veh_file is not None:
        route_files.append(str(veh_file))
    content = f'''<?xml version="1.0" encoding="UTF-8"?>
<configuration>
  <input>
    <net-file value="{net_file}"/>
    <route-files value="{','.join(route_files)}"/>
  </input>
  <time>
    <begin value="0"/>
    <end value="{duration}"/>
    <step-length value="{step_length}"/>
  </time>
  <processing>
    <collision.action value="warn"/>
    <time-to-teleport value="-1"/>
  </processing>
  <report>
    <no-step-log value="true"/>
    <no-warnings value="false"/>
  </report>
</configuration>
'''
    cfg_path.write_text(content, encoding="utf-8")
    return cfg_path


def _sumo_binary() -> str:
    path = shutil.which("sumo")
    if not path:
        raise RuntimeError("sumo binary not found in PATH")
    return path


def _person_routes_crossing(candidate_df: pd.DataFrame) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for row in candidate_df.itertuples(index=False):
        tls_id = str(row.tls_id_used)
        out[str(row.crosswalk_id)] = {
            "crossing_id": str(getattr(row, "crossing_id", "")),
            "crossing_edge_id": str(row.crossing_edge_id),
            "route_from_edge": str(row.route_from_edge),
            "route_to_edge": str(row.route_to_edge),
            "tls_id": tls_id,
            "ped_link_index": int(row.ped_link_index),
            "route_reason": str(row.route_reason),
            "batch_network_file": str(row.batch_network_file),
        }
    return out


def _run_scenario(
    candidate_df: pd.DataFrame,
    net_file: Path,
    scenario: str,
    seed: int,
    duration: int,
    warmup: int,
    step_length: float,
    out_dir: Path,
    include_vehicles: bool,
    extension_sec: float,
) -> pd.DataFrame:
    if traci is None:
        raise RuntimeError("traci not importable")

    ped_file, veh_file, ped_summary_path = _write_routes(candidate_df, out_dir, seed, duration, include_vehicles)
    cfg_path = _write_sumocfg(out_dir / f"phase6_smoke_{scenario}.sumocfg", net_file, ped_file, veh_file, duration, step_length)

    cmd = [_sumo_binary(), "-c", str(cfg_path), "--no-step-log", "--collision.action", "warn", "--time-to-teleport", "-1"]
    traci.start(cmd)

    candidate_meta = _person_routes_crossing(candidate_df)
    rows: list[dict[str, Any]] = []
    extension_events: list[dict[str, Any]] = []
    debug_rows: list[dict[str, Any]] = []

    # per-candidate tracking
    departure_times: dict[str, float] = {}
    wait_recorded: set[str] = set()
    extended_this_cycle: set[tuple[str, int, str]] = set()
    ped_presence_steps: dict[str, int] = {cid: 0 for cid in candidate_df["crosswalk_id"].astype(str).tolist()}
    ped_people_seen: dict[str, set[str]] = {cid: set() for cid in candidate_df["crosswalk_id"].astype(str).tolist()}
    expected_repeat_counts: dict[str, int] = {
        str(row.crosswalk_id): int(getattr(row, "ped_repeat_count", 1) or 1)
        for row in candidate_df.itertuples(index=False)
    }
    inventory = _load_csv(CROSSING_INVENTORY)
    crossing_roads: dict[str, set[str]] = {}
    for row in candidate_df.itertuples(index=False):
        cid = str(row.crosswalk_id)
        crossing_roads[cid] = _candidate_crossing_roads(
            str(row.nearest_junction_id),
            str(row.route_from_edge),
            str(row.crossing_edge_id),
            inventory,
        )
    veh_delays: dict[str, list[float]] = {cid: [] for cid in candidate_df["crosswalk_id"].astype(str).tolist()}

    step = 0
    try:
        while step < duration:
            traci.simulationStep()
            t = float(traci.simulation.getTime())
            ped_ids = list(traci.person.getIDList())
            veh_ids = list(traci.vehicle.getIDList())
            step_crossing_hit: dict[str, bool] = {cid: False for cid in candidate_meta}

            for pid in traci.simulation.getDepartedPersonIDList():
                departure_times[str(pid)] = t

            for cid, meta in candidate_meta.items():
                tls_id = meta["tls_id"]
                ped_link_index = meta["ped_link_index"]
                crossing_edge_id = meta["crossing_edge_id"]
                crossing_edge_set = crossing_roads.get(cid, set()) | {crossing_edge_id}

                try:
                    state = traci.trafficlight.getRedYellowGreenState(tls_id)
                    phase = traci.trafficlight.getPhase(tls_id)
                    remaining = float(traci.trafficlight.getNextSwitch(tls_id) - t)
                except Exception:
                    continue

                if scenario == "smart" and ped_link_index < len(state) and state[ped_link_index] in {"G", "g"}:
                    ped_near = 0
                    for pid in ped_ids:
                        try:
                            road = traci.person.getRoadID(pid)
                        except Exception:
                            continue
                        if road in crossing_edge_set or road.startswith(f":{tls_id}_") or road.startswith(f":{str(tls_id)}_"):
                            ped_near += 1
                    key = (str(tls_id), int(phase), cid)
                    if ped_near > 0 and remaining <= 12.0 and key not in extended_this_cycle:
                        try:
                            traci.trafficlight.setPhaseDuration(tls_id, remaining + extension_sec)
                            extended_this_cycle.add(key)
                            extension_events.append(
                                {
                                    "time": round(t, 1),
                                    "crosswalk_id": cid,
                                    "tls_id": tls_id,
                                    "linkIndex": ped_link_index,
                                    "phase": phase,
                                    "state": state,
                                    "remaining_before": round(remaining, 1),
                                    "extension_sec": extension_sec,
                                    "ped_near": ped_near,
                                }
                            )
                        except Exception as exc:
                            extension_events.append(
                                {
                                    "time": round(t, 1),
                                    "crosswalk_id": cid,
                                    "tls_id": tls_id,
                                    "linkIndex": ped_link_index,
                                    "phase": phase,
                                    "state": state,
                                    "error": str(exc),
                                }
                            )

                # wait / crossing stats
                for pid in ped_ids:
                    try:
                        road = traci.person.getRoadID(pid)
                    except Exception:
                        continue
                    road_match = road in crossing_edge_set or road.startswith(f":{tls_id}_") or road.startswith(f":{str(tls_id)}_")
                    debug_rows.append(
                        {
                            "time": round(t, 1),
                            "scenario": scenario,
                            "crosswalk_id": cid,
                            "person_id": pid,
                            "road_id": road,
                            "tls_id": tls_id,
                            "phase": phase,
                            "state": state,
                            "remaining_time": round(remaining, 1),
                            "ped_link_index": ped_link_index,
                            "crossing_edge_id": crossing_edge_id,
                            "crossing_match": bool(road_match),
                        }
                    )
                    if road_match:
                        step_crossing_hit[cid] = True
                        ped_people_seen[cid].add(str(pid))
                        if pid not in wait_recorded:
                            depart_t = departure_times.get(str(pid), t)
                            wait_recorded.add(pid)
                            rows.append(
                                {
                                    "crosswalk_id": cid,
                                    "scenario": scenario,
                                    "seed": seed,
                                    "tls_id_used": tls_id,
                                    "ped_link_index": ped_link_index,
                                    "crossing_edge_id": crossing_edge_id,
                                    "route_from_edge": meta["route_from_edge"],
                                    "route_to_edge": meta["route_to_edge"],
                                    "ped_wait_time": round(t - depart_t, 2),
                                    "ped_crossing_count": 1,
                                    "veh_delay_sample": None,
                                }
                            )

            for cid, hit in step_crossing_hit.items():
                if hit:
                    ped_presence_steps[cid] += 1

            if step % 20 == 0:
                for cid in candidate_meta:
                    for vid in veh_ids:
                        try:
                            veh_delays[cid].append(float(traci.vehicle.getAccumulatedWaitingTime(vid)))
                        except Exception:
                            pass

            step += 1
    finally:
        try:
            traci.close(False)
        except Exception:
            pass

    out_rows: list[dict[str, Any]] = []
    for cid, meta in candidate_meta.items():
        waits = [r["ped_wait_time"] for r in rows if r["crosswalk_id"] == cid and r["ped_wait_time"] is not None]
        delays = veh_delays.get(cid, [])
        out_rows.append(
            {
                "crosswalk_id": cid,
                "scenario": scenario,
                "seed": seed,
                "tls_id_used": meta["tls_id"],
                "ped_link_index": meta["ped_link_index"],
                "crossing_edge_id": meta["crossing_edge_id"],
                "route_from_edge": meta["route_from_edge"],
                "route_to_edge": meta["route_to_edge"],
                "completed": True,
                "step_count": step,
                "ped_crossing_presence_steps": ped_presence_steps.get(cid, 0),
                "ped_crossing_person_count": len(ped_people_seen.get(cid, set())),
                "expected_ped_repeat_count": expected_repeat_counts.get(cid, 1),
                "ped_repeat_count_match": len(ped_people_seen.get(cid, set())) == expected_repeat_counts.get(cid, 1),
                "ped_wait_time_mean": round(sum(waits) / len(waits), 2) if waits else None,
                "ped_wait_time_max": round(max(waits), 2) if waits else None,
                "veh_delay_mean": round(sum(delays) / len(delays), 2) if delays else None,
                "veh_delay_max": round(max(delays), 2) if delays else None,
                "extension_count": len([e for e in extension_events if e.get("crosswalk_id") == cid]),
                "batch_network_file": meta["batch_network_file"],
                "route_reason": meta["route_reason"],
            }
        )

    pd.DataFrame(out_rows).to_csv(out_dir / f"phase6_smoke_{scenario}_results.csv", index=False)
    pd.DataFrame(debug_rows).to_csv(out_dir / f"phase6_smoke_{scenario}_debug_trace.csv", index=False)
    (out_dir / f"phase6_smoke_{scenario}_extension_events.json").write_text(json.dumps(extension_events, ensure_ascii=False, indent=2), encoding="utf-8")
    ext_columns = ["time", "crosswalk_id", "tls_id", "linkIndex", "phase", "state", "remaining_before", "extension_sec", "ped_near", "error"]
    pd.DataFrame(extension_events, columns=ext_columns).to_csv(out_dir / f"phase6_smoke_{scenario}_extension_events.csv", index=False)
    (out_dir / f"phase6_smoke_{scenario}_candidate_validation.json").write_text(json.dumps(candidate_meta, ensure_ascii=False, indent=2), encoding="utf-8")
    route_order_ok = False
    try:
        route_order_ok = bool(pd.read_csv(ped_summary_path)["depart_sorted_ok"].iloc[0])
    except Exception:
        route_order_ok = False
    (out_dir / f"phase6_smoke_{scenario}_route_order_summary.json").write_text(
        json.dumps(
            {
                "scenario": scenario,
                "ped_route_file": str(ped_file),
                "ped_route_order_debug_csv": str(ped_summary_path),
                "ped_depart_sorted_ok": route_order_ok,
                "ped_person_rows": int(len(pd.read_csv(ped_summary_path))) if ped_summary_path.exists() else 0,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    other_scenario = "smart" if scenario == "baseline" else "baseline"
    other_dir = out_dir.with_name(f"phase6_recovery_smoke_{other_scenario}_20260514_220549")
    other_results = other_dir / f"phase6_smoke_{other_scenario}_results.csv"
    current_results = out_dir / f"phase6_smoke_{scenario}_results.csv"
    compare_path = RESULT_DIR / "phase6_transition_after_recovery_20260514_220549" / "phase6_smoke_baseline_vs_smart_compare.csv"
    required_cols = [
        "completed",
        "ped_crossing_presence_steps",
        "ped_crossing_person_count",
        "expected_ped_repeat_count",
        "ped_repeat_count_match",
        "extension_count",
    ]
    compare_dir = RESULT_DIR / "phase6_transition_after_recovery_20260514_220549"
    if not current_results.exists():
        _write_compare_warning(compare_dir, scenario, "current_results_missing", {"path": str(current_results)})
        return pd.DataFrame(out_rows)
    if not other_results.exists():
        _write_compare_warning(compare_dir, scenario, "other_results_missing", {"path": str(other_results)})
        return pd.DataFrame(out_rows)

    current_df_raw = pd.read_csv(current_results)
    other_df_raw = pd.read_csv(other_results)
    missing_current = [c for c in required_cols if c not in current_df_raw.columns]
    missing_other = [c for c in required_cols if c not in other_df_raw.columns]
    if missing_current or missing_other:
        _write_compare_warning(
            compare_dir,
            scenario,
            "required_columns_missing",
            {
                "current_missing": missing_current,
                "other_missing": missing_other,
                "current_path": str(current_results),
                "other_path": str(other_results),
            },
        )
        return pd.DataFrame(out_rows)

    current_df = current_df_raw.set_index("crosswalk_id")
    other_df = other_df_raw.set_index("crosswalk_id")
    ordered = candidate_df["crosswalk_id"].astype(str).tolist()
    compare_rows: list[dict[str, Any]] = []
    for cid in ordered:
        cur = current_df.loc[cid] if cid in current_df.index else None
        oth = other_df.loc[cid] if cid in other_df.index else None
        compare_rows.append(
            {
                "crosswalk_id": cid,
                "completed_baseline": bool(_safe_get(cur, "completed")) if scenario == "baseline" else bool(_safe_get(oth, "completed")) if other_scenario == "baseline" else None,
                "completed_smart": bool(_safe_get(cur, "completed")) if scenario == "smart" else bool(_safe_get(oth, "completed")) if other_scenario == "smart" else None,
                "ped_crossing_presence_steps_baseline": int(_safe_get(cur, "ped_crossing_presence_steps")) if scenario == "baseline" else int(_safe_get(oth, "ped_crossing_presence_steps")) if other_scenario == "baseline" else None,
                "ped_crossing_presence_steps_smart": int(_safe_get(cur, "ped_crossing_presence_steps")) if scenario == "smart" else int(_safe_get(oth, "ped_crossing_presence_steps")) if other_scenario == "smart" else None,
                "ped_crossing_person_count_baseline": int(_safe_get(cur, "ped_crossing_person_count")) if scenario == "baseline" else int(_safe_get(oth, "ped_crossing_person_count")) if other_scenario == "baseline" else None,
                "ped_crossing_person_count_smart": int(_safe_get(cur, "ped_crossing_person_count")) if scenario == "smart" else int(_safe_get(oth, "ped_crossing_person_count")) if other_scenario == "smart" else None,
                "expected_ped_repeat_count_baseline": int(_safe_get(cur, "expected_ped_repeat_count")) if scenario == "baseline" else int(_safe_get(oth, "expected_ped_repeat_count")) if other_scenario == "baseline" else None,
                "expected_ped_repeat_count_smart": int(_safe_get(cur, "expected_ped_repeat_count")) if scenario == "smart" else int(_safe_get(oth, "expected_ped_repeat_count")) if other_scenario == "smart" else None,
                "ped_repeat_count_match_baseline": bool(_safe_get(cur, "ped_repeat_count_match")) if scenario == "baseline" else bool(_safe_get(oth, "ped_repeat_count_match")) if other_scenario == "baseline" else None,
                "ped_repeat_count_match_smart": bool(_safe_get(cur, "ped_repeat_count_match")) if scenario == "smart" else bool(_safe_get(oth, "ped_repeat_count_match")) if other_scenario == "smart" else None,
                "extension_count_baseline": int(_safe_get(cur, "extension_count")) if scenario == "baseline" else int(_safe_get(oth, "extension_count")) if other_scenario == "baseline" else None,
                "extension_count_smart": int(_safe_get(cur, "extension_count")) if scenario == "smart" else int(_safe_get(oth, "extension_count")) if other_scenario == "smart" else None,
            }
        )
    pd.DataFrame(compare_rows).to_csv(compare_path, index=False)
    return pd.DataFrame(out_rows)


def main() -> None:
    parser = argparse.ArgumentParser(description="Phase 6 recovery smoke runner using final 237 recovery candidates directly.")
    parser.add_argument("--candidate-csv", required=True, help="Prepared smoke candidate CSV.")
    parser.add_argument("--net-file", required=True, help="Recovery batch network XML for candidates.")
    parser.add_argument("--scenario", choices=["baseline", "smart"], required=True)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--sim-duration", type=int, default=300)
    parser.add_argument("--warmup", type=int, default=0)
    parser.add_argument("--step-length", type=float, default=0.5)
    parser.add_argument("--extension-sec", type=float, default=5.0, help="Smart pedestrian green extension in seconds.")
    parser.add_argument("--include-vehicles", action="store_true", help="Also emit vehicle routes and include them in the SUMO config.")
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args()

    candidate_csv = Path(args.candidate_csv).expanduser().resolve()
    net_file = Path(args.net_file).expanduser().resolve()
    out_dir = Path(args.output_dir).expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    candidate_df = _build_candidate_table(candidate_csv, net_file)
    if not net_file.exists():
        raise FileNotFoundError(net_file)

    summary = _run_scenario(
        candidate_df,
        net_file,
        args.scenario,
        args.seed,
        args.sim_duration,
        args.warmup,
        args.step_length,
        out_dir,
        args.include_vehicles,
        args.extension_sec,
    )

    metadata = {
        "candidate_csv": str(candidate_csv),
        "net_file": str(net_file),
        "scenario": args.scenario,
        "seed": args.seed,
        "sim_duration": args.sim_duration,
        "warmup": args.warmup,
        "step_length": args.step_length,
        "extension_sec": args.extension_sec,
        "include_vehicles": args.include_vehicles,
        "output_dir": str(out_dir),
        "candidate_rows": int(len(candidate_df)),
        "candidate_ids": candidate_df["crosswalk_id"].astype(str).tolist(),
    }
    (out_dir / "run_metadata.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    candidate_df.to_csv(out_dir / "smoke_candidates_resolved.csv", index=False)
    summary.to_csv(out_dir / "phase6_smoke_summary.csv", index=False)
    (out_dir / "phase6_smoke_summary.md").write_text(
        "\n".join(
            [
                "# phase 6 recovery smoke summary",
                "",
                f"- scenario: {args.scenario}",
                f"- candidate_rows: {len(candidate_df)}",
                f"- seed: {args.seed}",
                f"- sim_duration: {args.sim_duration}",
                f"- warmup: {args.warmup}",
                f"- net_file: `{net_file}`",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"output_dir: {out_dir}")
    print(f"candidate_rows: {len(candidate_df)}")
    print(f"scenario: {args.scenario}")


if __name__ == "__main__":
    main()
