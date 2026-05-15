#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import traceback
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

from smart_crosswalk_sumo.network_utils import (
    apply_sumo_environment,
    distance_to_edge_shape,
    edge_allows,
    edge_center,
    edge_function,
    read_net,
)
from smart_crosswalk_sumo.run_phase6_recovery_smoke import (
    RESULT_DIR,
    _parse_edges,
    _safe_get,
    _write_compare_warning,
)


BASE_DIR = Path(__file__).resolve().parents[1]
COMPARE_DIR = RESULT_DIR / "phase6_transition_after_recovery_20260514_220549"


@dataclass
class PedRecord:
    person_id: str
    crosswalk_id: str
    depart: float
    route_from_edge: str
    route_to_edge: str
    candidate_index: int
    original_candidate_index: int
    repeat_index: int


@dataclass
class VehicleRecord:
    vehicle_id: str
    route_id: str
    crosswalk_id: str
    candidate_index: int
    original_candidate_index: int
    vehicle_index: int
    depart: float
    route_from_edge: str
    route_to_edge: str
    route_edge_sequence: list[str]
    route_edge_count: int
    route_cost: float | None


def _sumo_binary() -> str:
    path = shutil.which("sumo")
    if not path:
        raise RuntimeError("sumo binary not found in PATH")
    return path


def _load_csv(path: Path) -> pd.DataFrame:
    if not path.exists():
        raise FileNotFoundError(path)
    return pd.read_csv(path)


def _normalize_limit_ids(values: list[str] | None) -> list[str] | None:
    if not values:
        return None
    out: list[str] = []
    for value in values:
        for token in str(value).replace(",", " ").split():
            token = token.strip()
            if token:
                out.append(token)
    return out or None


def _resolved_candidate_df(
    candidate_csv: Path,
    net_file: Path,
    limit_crosswalk_ids: list[str] | None,
    ped_repeat_count_override: int | None,
) -> pd.DataFrame:
    raw = _load_csv(candidate_csv)
    required = [
        "crosswalk_id",
        "nearest_junction_id",
        "tls_id_used",
        "ped_link_indices",
        "route_from_edge",
        "route_to_edge",
        "crossing_edge_id",
    ]
    missing = [col for col in required if col not in raw.columns]
    if missing:
        raise ValueError(f"candidate csv missing columns: {missing}")

    rows: list[dict[str, Any]] = []
    coord_cols = [
        "target_x",
        "target_y",
        "crossing_x",
        "crossing_y",
        "crossing_lon",
        "crossing_lat",
        "original_csv_lon",
        "original_csv_lat",
    ]
    for raw_index, row in raw.iterrows():
        ped_indices = _parse_edges(row.get("ped_link_indices"))
        if not ped_indices:
            raise ValueError(f"ped_link_indices missing for {row.get('crosswalk_id')}")
        item: dict[str, Any] = {
            "original_candidate_index": int(raw_index) + 1,
            "crosswalk_id": str(row["crosswalk_id"]),
            "nearest_junction_id": str(row["nearest_junction_id"]),
            "tls_id_used": str(row["tls_id_used"]),
            "ped_link_indices": [int(v) for v in ped_indices],
            "ped_link_index": int(ped_indices[0]),
            "ped_depart_offset_sec": float(row.get("ped_depart_offset_sec", 0.0) or 0.0),
            "ped_repeat_count": int(row.get("ped_repeat_count", 1) or 1),
            "ped_repeat_spacing_sec": float(row.get("ped_repeat_spacing_sec", 1.5) or 1.5),
            "route_from_edge": str(row["route_from_edge"]),
            "route_to_edge": str(row["route_to_edge"]),
            "crossing_edge_id": str(row["crossing_edge_id"]),
            "batch_network_file": str(row.get("batch_network_file", net_file)),
            "source_file": str(row.get("source_file", "")),
        }
        for col in coord_cols:
            if col in raw.columns:
                item[col] = row.get(col)
        rows.append(item)
    candidate_df = pd.DataFrame(rows)
    if limit_crosswalk_ids:
        candidate_df = candidate_df[candidate_df["crosswalk_id"].astype(str).isin(limit_crosswalk_ids)].copy()
    if candidate_df.empty:
        raise ValueError("no candidates after limit_crosswalk_ids filter")
    if ped_repeat_count_override is not None:
        candidate_df = candidate_df.copy()
        candidate_df["ped_repeat_count"] = int(ped_repeat_count_override)
    candidate_df = candidate_df.reset_index(drop=True)
    candidate_df.insert(0, "candidate_index", range(1, len(candidate_df) + 1))
    return candidate_df


def _candidate_crosswalk_ids(candidate_df: pd.DataFrame) -> list[str]:
    return candidate_df["crosswalk_id"].astype(str).tolist()


def _candidate_crossing_roads_simple(row: Any) -> set[str]:
    roads = {
        str(getattr(row, "crossing_edge_id", "") or ""),
        f":{getattr(row, 'tls_id_used', '')}_c0",
        f":{getattr(row, 'tls_id_used', '')}_c1",
        f":{getattr(row, 'tls_id_used', '')}_c2",
        f":{getattr(row, 'tls_id_used', '')}_w0",
        f":{getattr(row, 'tls_id_used', '')}_w1",
        f":{getattr(row, 'tls_id_used', '')}_w2",
    }
    return {road for road in roads if road and road not in {"nan", "None"}}


def _parse_int_list(value: Any) -> list[int]:
    if value is None:
        return []
    if isinstance(value, list):
        out: list[int] = []
        for item in value:
            try:
                out.append(int(item))
            except Exception:
                continue
        return out
    text = str(value).strip()
    if not text or text in {"nan", "None"}:
        return []
    if text.startswith("[") and text.endswith("]"):
        text = text.strip("[]")
    text = text.replace(",", " ").replace("|", " ")
    out: list[int] = []
    for token in text.split():
        try:
            out.append(int(float(token)))
        except Exception:
            continue
    return out


def _phase_link_audit(state: str, ped_link_indices: list[int]) -> dict[str, Any]:
    green_indices_in_phase = [idx for idx, ch in enumerate(str(state)) if ch in {"G", "g"}]
    ped_set = set(int(v) for v in ped_link_indices)
    non_ped_green_indices_in_phase = [idx for idx in green_indices_in_phase if idx not in ped_set]
    ped_green_ok = bool(ped_set) and all(idx in green_indices_in_phase for idx in ped_set)
    if not ped_green_ok:
        extension_phase_type = "not_ped_green"
    elif non_ped_green_indices_in_phase:
        extension_phase_type = "mixed"
    else:
        extension_phase_type = "pedestrian_only"
    return {
        "ped_link_indices": "|".join(str(v) for v in sorted(ped_set)),
        "green_indices_in_phase": "|".join(str(v) for v in green_indices_in_phase),
        "non_ped_green_indices_in_phase": "|".join(str(v) for v in non_ped_green_indices_in_phase),
        "non_ped_green_count": int(len(non_ped_green_indices_in_phase)),
        "extension_phase_type": extension_phase_type,
        "phase_extension_affects_non_ped_green": bool(non_ped_green_indices_in_phase),
        "ped_green_ok": ped_green_ok,
    }


def _parse_float_list(raw_value: str | None) -> list[float]:
    if raw_value is None:
        return []
    values: list[float] = []
    for token in str(raw_value).replace(";", ",").split(","):
        token = token.strip()
        if not token:
            continue
        try:
            values.append(float(token))
        except ValueError:
            continue
    return values


def _edge_length(edge: Any) -> float:
    try:
        return float(edge.getLength())
    except Exception:
        try:
            lane = edge.getLanes()[0]
            return float(lane.getLength())
        except Exception:
            return 0.0


def _net_bounds(net: Any) -> tuple[float, float, float, float] | None:
    try:
        (xmin, ymin), (xmax, ymax) = net.getBBoxXY()
        return float(xmin), float(ymin), float(xmax), float(ymax)
    except Exception:
        return None


def _point_within_bounds(xy: tuple[float, float] | None, bounds: tuple[float, float, float, float] | None) -> bool:
    if xy is None or bounds is None:
        return False
    x, y = float(xy[0]), float(xy[1])
    xmin, ymin, xmax, ymax = bounds
    return xmin <= x <= xmax and ymin <= y <= ymax


def _edge_midpoint_xy(edge: Any) -> tuple[float, float] | None:
    try:
        shape = [(float(x), float(y)) for x, y in edge.getShape()]
    except Exception:
        shape = []
    if shape:
        return edge_center(edge)
    try:
        return edge_center(edge)
    except Exception:
        return None


def _edge_distance_to_anchor(edge: Any, anchor_xy: tuple[float, float]) -> float:
    try:
        shape = list(edge.getShape())
    except Exception:
        shape = []
    if shape:
        try:
            return float(distance_to_edge_shape(edge, anchor_xy))
        except Exception:
            pass
    midpoint = _edge_midpoint_xy(edge)
    if midpoint is None:
        return float("inf")
    return float(((float(anchor_xy[0]) - float(midpoint[0])) ** 2 + (float(anchor_xy[1]) - float(midpoint[1])) ** 2) ** 0.5)


def _candidate_anchor(row: Any, net: Any) -> dict[str, Any]:
    bounds = _net_bounds(net)
    anchor_x: float | None = None
    anchor_y: float | None = None
    anchor_source = "fallback_route_edges"
    converted_from_lonlat = False
    fallback_used = True
    warning = ""

    def _accept(xy: tuple[float, float] | None, source: str) -> bool:
        nonlocal anchor_x, anchor_y, anchor_source, fallback_used
        if xy is None:
            return False
        if not _point_within_bounds(xy, bounds):
            return False
        anchor_x = float(xy[0])
        anchor_y = float(xy[1])
        anchor_source = source
        fallback_used = False
        return True

    crossing_edge_id = str(getattr(row, "crossing_edge_id", "") or getattr(row, "crossing_edge", "") or "")
    if crossing_edge_id and crossing_edge_id not in {"nan", "None"}:
        try:
            crossing_edge = net.getEdge(crossing_edge_id)
            if _accept(_edge_midpoint_xy(crossing_edge), "crossing_edge_midpoint"):
                return {
                    "anchor_source": anchor_source,
                    "anchor_x": anchor_x,
                    "anchor_y": anchor_y,
                    "raw_target_x": getattr(row, "target_x", None),
                    "raw_target_y": getattr(row, "target_y", None),
                    "raw_crossing_lon": getattr(row, "crossing_lon", None),
                    "raw_crossing_lat": getattr(row, "crossing_lat", None),
                    "raw_original_csv_lon": getattr(row, "original_csv_lon", None),
                    "raw_original_csv_lat": getattr(row, "original_csv_lat", None),
                    "net_boundary": bounds,
                    "converted_from_lonlat": converted_from_lonlat,
                    "fallback_used": fallback_used,
                    "warning": warning,
                }
        except Exception:
            pass

    route_from_edge = str(getattr(row, "route_from_edge", "") or "")
    route_to_edge = str(getattr(row, "route_to_edge", "") or "")
    if route_from_edge and route_to_edge and route_from_edge not in {"nan", "None"} and route_to_edge not in {"nan", "None"}:
        try:
            from_edge = net.getEdge(route_from_edge)
            to_edge = net.getEdge(route_to_edge)
            from_xy = _edge_midpoint_xy(from_edge)
            to_xy = _edge_midpoint_xy(to_edge)
            if from_xy is not None and to_xy is not None:
                midpoint_xy = ((float(from_xy[0]) + float(to_xy[0])) / 2.0, (float(from_xy[1]) + float(to_xy[1])) / 2.0)
                if _accept(midpoint_xy, "route_midpoint"):
                    return {
                        "anchor_source": anchor_source,
                        "anchor_x": anchor_x,
                        "anchor_y": anchor_y,
                        "raw_target_x": getattr(row, "target_x", None),
                        "raw_target_y": getattr(row, "target_y", None),
                        "raw_crossing_lon": getattr(row, "crossing_lon", None),
                        "raw_crossing_lat": getattr(row, "crossing_lat", None),
                        "raw_original_csv_lon": getattr(row, "original_csv_lon", None),
                        "raw_original_csv_lat": getattr(row, "original_csv_lat", None),
                        "net_boundary": bounds,
                        "converted_from_lonlat": converted_from_lonlat,
                        "fallback_used": fallback_used,
                        "warning": warning,
                    }
        except Exception:
            pass

    for x_col, y_col in [("target_x", "target_y"), ("crossing_x", "crossing_y")]:
        x_val = getattr(row, x_col, None)
        y_val = getattr(row, y_col, None)
        if x_val is None or y_val is None or pd.isna(x_val) or pd.isna(y_val):
            continue
        try:
            xy = (float(x_val), float(y_val))
        except Exception:
            continue
        if _accept(xy, f"{x_col}_{y_col}"):
            return {
                "anchor_source": anchor_source,
                "anchor_x": anchor_x,
                "anchor_y": anchor_y,
                "raw_target_x": getattr(row, "target_x", None),
                "raw_target_y": getattr(row, "target_y", None),
                "raw_crossing_lon": getattr(row, "crossing_lon", None),
                "raw_crossing_lat": getattr(row, "crossing_lat", None),
                "raw_original_csv_lon": getattr(row, "original_csv_lon", None),
                "raw_original_csv_lat": getattr(row, "original_csv_lat", None),
                "net_boundary": bounds,
                "converted_from_lonlat": converted_from_lonlat,
                "fallback_used": fallback_used,
                "warning": warning,
            }

    for lon_col, lat_col in [("crossing_lon", "crossing_lat"), ("original_csv_lon", "original_csv_lat")]:
        lon_val = getattr(row, lon_col, None)
        lat_val = getattr(row, lat_col, None)
        if lon_val is None or lat_val is None or pd.isna(lon_val) or pd.isna(lat_val):
            continue
        try:
            xy = net.convertLonLat2XY(float(lon_val), float(lat_val))
        except Exception:
            continue
        if _accept(xy, f"{lon_col}_{lat_col}"):
            converted_from_lonlat = True
            return {
                "anchor_source": anchor_source,
                "anchor_x": anchor_x,
                "anchor_y": anchor_y,
                "raw_target_x": getattr(row, "target_x", None),
                "raw_target_y": getattr(row, "target_y", None),
                "raw_crossing_lon": getattr(row, "crossing_lon", None),
                "raw_crossing_lat": getattr(row, "crossing_lat", None),
                "raw_original_csv_lon": getattr(row, "original_csv_lon", None),
                "raw_original_csv_lat": getattr(row, "original_csv_lat", None),
                "net_boundary": bounds,
                "converted_from_lonlat": converted_from_lonlat,
                "fallback_used": fallback_used,
                "warning": warning,
            }

    warning = "fallback_route_edges_used"
    return {
        "anchor_source": anchor_source,
        "anchor_x": anchor_x,
        "anchor_y": anchor_y,
        "raw_target_x": getattr(row, "target_x", None),
        "raw_target_y": getattr(row, "target_y", None),
        "raw_crossing_lon": getattr(row, "crossing_lon", None),
        "raw_crossing_lat": getattr(row, "crossing_lat", None),
        "raw_original_csv_lon": getattr(row, "original_csv_lon", None),
        "raw_original_csv_lat": getattr(row, "original_csv_lat", None),
        "net_boundary": bounds,
        "converted_from_lonlat": converted_from_lonlat,
        "fallback_used": fallback_used,
        "warning": warning,
    }


def _normal_passenger_edges(net: Any) -> list[Any]:
    out: list[Any] = []
    for edge in net.getEdges():
        if edge.getID().startswith(":"):
            continue
        if edge_function(edge) != "normal":
            continue
        if not edge_allows(edge, "passenger"):
            continue
        out.append(edge)
    return out


def _fallback_scope_edges(net: Any, row: Any) -> set[str]:
    seeds = {
        str(getattr(row, "route_from_edge", "") or ""),
        str(getattr(row, "route_to_edge", "") or ""),
        str(getattr(row, "crossing_edge_id", "") or ""),
    }
    seeds = {edge_id for edge_id in seeds if edge_id and edge_id not in {"nan", "None"}}
    chosen: set[str] = set()
    frontier = list(seeds)
    seen = set(frontier)
    for _ in range(2):
        next_frontier: list[str] = []
        for edge_id in frontier:
            try:
                edge = net.getEdge(edge_id)
            except Exception:
                continue
            if edge.getID().startswith(":"):
                continue
            if edge_function(edge) == "normal" and edge_allows(edge, "passenger"):
                chosen.add(edge.getID())
            linked = list(edge.getIncoming().keys()) + list(edge.getOutgoing().keys())
            for linked_edge in linked:
                linked_id = linked_edge.getID()
                if not linked_id or linked_id in seen or linked_id.startswith(":"):
                    continue
                if edge_function(linked_edge) != "normal" or not edge_allows(linked_edge, "passenger"):
                    continue
                seen.add(linked_id)
                next_frontier.append(linked_id)
        frontier = next_frontier
    if not chosen and seeds:
        for edge_id in seeds:
            try:
                edge = net.getEdge(edge_id)
            except Exception:
                continue
            if edge.getID().startswith(":"):
                continue
            if edge_function(edge) == "normal" and edge_allows(edge, "passenger"):
                chosen.add(edge.getID())
    return chosen


def _impact_scope_names(impact_radii_m: list[float], include_global_scope: bool) -> list[tuple[str, float | None]]:
    scopes = [(f"{int(radius)}m" if float(radius).is_integer() else f"{radius:g}m", float(radius)) for radius in impact_radii_m]
    if include_global_scope:
        scopes.append(("global", None))
    return scopes


def _build_impact_scope_cache(
    candidate_df: pd.DataFrame,
    net_file: Path,
    impact_radii_m: list[float],
    include_global_scope: bool,
) -> tuple[dict[str, dict[str, Any]], pd.DataFrame, pd.DataFrame]:
    net = read_net(net_file)
    rows: list[dict[str, Any]] = []
    anchor_rows: list[dict[str, Any]] = []
    cache: dict[str, dict[str, Any]] = {}
    scope_names = _impact_scope_names(impact_radii_m, include_global_scope)
    all_edges = _normal_passenger_edges(net)

    for row in candidate_df.itertuples(index=False):
        cid = str(row.crosswalk_id)
        anchor = _candidate_anchor(row, net)
        anchor_xy = None
        if anchor["anchor_x"] is not None and anchor["anchor_y"] is not None:
            anchor_xy = (float(anchor["anchor_x"]), float(anchor["anchor_y"]))
        scope_map: dict[str, dict[str, Any]] = {}

        for scope_name, radius_m in scope_names:
            scoped_edges: list[tuple[str, float, str, float | None, str]] = []
            if scope_name == "global":
                for edge in all_edges:
                    edge_id = edge.getID()
                    scoped_edges.append((edge_id, _edge_length(edge), "global_network", None, "global_network"))
            elif anchor_xy is not None and not bool(anchor["fallback_used"]):
                for edge in all_edges:
                    edge_id = edge.getID()
                    try:
                        distance_m = float(_edge_distance_to_anchor(edge, anchor_xy))
                    except Exception:
                        continue
                    if distance_m <= float(radius_m):
                        scoped_edges.append((edge_id, _edge_length(edge), "radius_geometry", distance_m, "radius_geometry"))
            else:
                fallback_edges = _fallback_scope_edges(net, row)
                for edge_id in sorted(fallback_edges):
                    try:
                        edge = net.getEdge(edge_id)
                    except Exception:
                        continue
                    scoped_edges.append((edge_id, _edge_length(edge), "fallback_route_edges", None, "fallback_route_edges"))

            scoped_edges = list(dict.fromkeys(scoped_edges))
            scope_rows = [
                {
                    "crosswalk_id": cid,
                    "scope": scope_name,
                    "radius_m": float(radius_m) if radius_m is not None else None,
                    "edge_id": edge_id,
                    "edge_length": float(edge_length),
                    "edge_source": edge_source,
                    "distance_m": distance_m,
                    "impact_edge_source": impact_edge_source,
                    "anchor_source": anchor["anchor_source"],
                    "anchor_x": anchor["anchor_x"],
                    "anchor_y": anchor["anchor_y"],
                }
                for edge_id, edge_length, edge_source, distance_m, impact_edge_source in scoped_edges
            ]
            rows.extend(scope_rows)
            scope_map[scope_name] = {
                "edge_ids": {edge_id for edge_id, *_ in scoped_edges},
                "edge_count": len(scoped_edges),
                "edge_source": scope_rows[0]["edge_source"] if scope_rows else ("global_network" if scope_name == "global" else anchor["anchor_source"]),
                "impact_edge_source": scope_rows[0]["impact_edge_source"] if scope_rows else ("global_network" if scope_name == "global" else anchor["anchor_source"]),
                "radius_m": float(radius_m) if radius_m is not None else None,
            }
        unique_counts = {scope_name: len(scope_map[scope_name]["edge_ids"]) for scope_name, _ in scope_names}
        counts = list(unique_counts.values())
        warning = str(anchor["warning"] or "")
        if len(counts) > 1 and len(set(counts)) == 1:
            warning = f"{warning};all_radius_edge_counts_same" if warning else "all_radius_edge_counts_same"
        anchor_rows.append(
            {
                "crosswalk_id": cid,
                "anchor_source": anchor["anchor_source"],
                "anchor_x": anchor["anchor_x"],
                "anchor_y": anchor["anchor_y"],
                "raw_target_x": anchor["raw_target_x"],
                "raw_target_y": anchor["raw_target_y"],
                "raw_crossing_lon": anchor["raw_crossing_lon"],
                "raw_crossing_lat": anchor["raw_crossing_lat"],
                "raw_original_csv_lon": anchor["raw_original_csv_lon"],
                "raw_original_csv_lat": anchor["raw_original_csv_lat"],
                "net_boundary": json.dumps(anchor["net_boundary"]) if anchor["net_boundary"] is not None else "",
                "converted_from_lonlat": bool(anchor["converted_from_lonlat"]),
                "fallback_used": bool(anchor["fallback_used"]),
                "warning": warning,
            }
        )
        cache[cid] = {
            "scope_map": scope_map,
            "reference_xy": anchor_xy,
            "reference_xy_source": anchor["anchor_source"],
        }

    impact_edges_df = pd.DataFrame(
        rows,
        columns=[
            "crosswalk_id",
            "scope",
            "radius_m",
            "edge_id",
            "edge_length",
            "edge_source",
            "distance_m",
            "impact_edge_source",
            "anchor_source",
            "anchor_x",
            "anchor_y",
        ],
    )
    anchor_debug_df = pd.DataFrame(
        anchor_rows,
        columns=[
            "crosswalk_id",
            "anchor_source",
            "anchor_x",
            "anchor_y",
            "raw_target_x",
            "raw_target_y",
            "raw_crossing_lon",
            "raw_crossing_lat",
            "raw_original_csv_lon",
            "raw_original_csv_lat",
            "net_boundary",
            "converted_from_lonlat",
            "fallback_used",
            "warning",
        ],
    )
    return cache, impact_edges_df, anchor_debug_df


def _build_ped_records(
    candidate_df: pd.DataFrame,
    seed: int,
    sim_duration: int,
) -> list[PedRecord]:
    rng = random.Random(seed)
    records: list[PedRecord] = []
    for row in candidate_df.itertuples(index=False):
        original_candidate_index = int(getattr(row, "original_candidate_index", row.candidate_index))
        repeat_count = max(1, int(getattr(row, "ped_repeat_count", 1) or 1))
        repeat_spacing = float(getattr(row, "ped_repeat_spacing_sec", 1.5) or 1.5)
        for repeat_index in range(repeat_count):
            depart = round(
                10.0
                + (original_candidate_index - 1) * 40.0
                + float(getattr(row, "ped_depart_offset_sec", 0.0))
                + repeat_index * repeat_spacing
                + rng.uniform(-1.0, 1.0),
                1,
            )
            if depart > float(sim_duration):
                continue
            records.append(
                PedRecord(
                    person_id=f"ped_{original_candidate_index}_{repeat_index + 1}_{row.crosswalk_id}",
                    crosswalk_id=str(row.crosswalk_id),
                    depart=max(1.0, depart),
                    route_from_edge=str(row.route_from_edge),
                    route_to_edge=str(row.route_to_edge),
                    candidate_index=int(row.candidate_index),
                    original_candidate_index=original_candidate_index,
                    repeat_index=repeat_index + 1,
                )
            )
    records.sort(key=lambda rec: (rec.depart, rec.crosswalk_id, rec.person_id))
    return records


def _vehicle_count_for_candidate(
    demand_mode: str,
    sim_duration: int,
    vehicle_count_per_candidate: int,
    vehicle_hourly_volume: float,
    vehicle_lane_scale: float,
) -> int:
    if demand_mode in {"smoke", "fixed"}:
        return max(0, int(vehicle_count_per_candidate))
    if demand_mode == "hourly":
        count = round(float(vehicle_hourly_volume) * float(sim_duration) / 3600.0 * float(vehicle_lane_scale))
        return max(0, int(count))
    raise ValueError(f"unknown vehicle demand mode: {demand_mode}")


def _vehicle_depart_times(
    demand_mode: str,
    arrival_process: str,
    vehicle_count: int,
    sim_duration: int,
    vehicle_start_time: float,
    vehicle_depart_spacing_sec: float,
    seed: int,
) -> list[float]:
    if vehicle_count <= 0:
        return []
    start = max(0.0, float(vehicle_start_time))
    if demand_mode in {"smoke", "fixed"}:
        return [round(start + idx * float(vehicle_depart_spacing_sec), 1) for idx in range(vehicle_count)]
    if demand_mode != "hourly":
        raise ValueError(f"unknown vehicle demand mode: {demand_mode}")

    if arrival_process == "deterministic":
        if vehicle_count == 1:
            return [round(start, 1)]
        span = max(float(sim_duration) - start - 1e-6, 0.0)
        step = span / float(vehicle_count)
        return [round(start + idx * step, 1) for idx in range(vehicle_count)]

    if arrival_process != "poisson":
        raise ValueError(f"unknown vehicle arrival process: {arrival_process}")

    rng = random.Random(seed)
    duration = max(float(sim_duration) - start, 1.0)
    rate = float(vehicle_count) / duration if duration > 0 else 0.0
    if rate <= 0.0:
        return [round(start, 1)] * vehicle_count
    times: list[float] = []
    current = start
    for _ in range(vehicle_count):
        current += rng.expovariate(rate)
        if current > float(sim_duration):
            break
        times.append(round(current, 1))
    return times


def _shortest_vehicle_route(net: Any, from_edge_id: str, to_edge_id: str) -> tuple[list[str], float | None, str | None]:
    try:
        from_edge = net.getEdge(from_edge_id)
    except Exception:
        return [], None, "route_from_edge_missing_in_net"
    try:
        to_edge = net.getEdge(to_edge_id)
    except Exception:
        return [], None, "route_to_edge_missing_in_net"
    try:
        path_edges, path_cost = net.getShortestPath(from_edge, to_edge, vClass="passenger")
    except Exception as exc:
        return [], None, f"shortest_path_exception:{exc}"
    if not path_edges:
        return [], None, "no_shortest_path"
    edge_ids = [edge.getID() for edge in path_edges if edge is not None]
    edge_ids = [edge_id for edge_id in edge_ids if edge_id and not edge_id.startswith(":")]
    if not edge_ids:
        return [], None, "shortest_path_empty_after_filter"
    return edge_ids, float(path_cost) if path_cost is not None else None, None


def _build_vehicle_records(
    candidate_df: pd.DataFrame,
    net_file: Path,
    demand_mode: str,
    arrival_process: str,
    sim_duration: int,
    vehicle_count_per_candidate: int,
    vehicle_hourly_volume: float,
    vehicle_lane_scale: float,
    vehicle_start_time: float,
    vehicle_depart_spacing_sec: float,
    seed: int,
) -> tuple[list[VehicleRecord], list[dict[str, Any]]]:
    net = read_net(net_file)
    records: list[VehicleRecord] = []
    validation_rows: list[dict[str, Any]] = []

    for row in candidate_df.itertuples(index=False):
        original_candidate_index = int(getattr(row, "original_candidate_index", row.candidate_index))
        requested_count = _vehicle_count_for_candidate(
            demand_mode,
            sim_duration,
            vehicle_count_per_candidate,
            vehicle_hourly_volume,
            vehicle_lane_scale,
        )
        depart_times = _vehicle_depart_times(
            demand_mode,
            arrival_process,
            requested_count,
            sim_duration,
            vehicle_start_time,
            vehicle_depart_spacing_sec,
            seed + original_candidate_index,
        )
        for vehicle_index, depart in enumerate(depart_times):
            vehicle_id = f"veh_{row.candidate_index}_{vehicle_index + 1}_{row.crosswalk_id}"
            route_id = f"veh_route_{row.candidate_index}_{vehicle_index + 1}"
            route_edge_sequence, route_cost, invalid_reason = _shortest_vehicle_route(
                net,
                str(row.route_from_edge),
                str(row.route_to_edge),
            )
            valid = invalid_reason is None
            validation_rows.append(
                {
                    "candidate_index": int(row.candidate_index),
                    "original_candidate_index": original_candidate_index,
                    "crosswalk_id": str(row.crosswalk_id),
                    "vehicle_id": vehicle_id,
                    "route_from_edge": str(row.route_from_edge),
                    "route_to_edge": str(row.route_to_edge),
                    "depart": depart,
                    "route_edge_sequence": "|".join(route_edge_sequence),
                    "route_edge_count": len(route_edge_sequence),
                    "route_cost": route_cost,
                    "valid": valid,
                    "invalid_reason": invalid_reason or "",
                    "net_file": str(net_file),
                }
            )
            if not valid:
                continue
            records.append(
                VehicleRecord(
                    vehicle_id=vehicle_id,
                    route_id=route_id,
                    crosswalk_id=str(row.crosswalk_id),
                    candidate_index=int(row.candidate_index),
                    original_candidate_index=original_candidate_index,
                    vehicle_index=vehicle_index + 1,
                    depart=depart,
                    route_from_edge=str(row.route_from_edge),
                    route_to_edge=str(row.route_to_edge),
                    route_edge_sequence=route_edge_sequence,
                    route_edge_count=len(route_edge_sequence),
                    route_cost=route_cost,
                )
            )

    records.sort(key=lambda rec: (rec.depart, rec.candidate_index, rec.vehicle_index, rec.vehicle_id))
    validation_rows.sort(key=lambda row: (float(row["depart"]), int(row["candidate_index"]), row["vehicle_id"]))
    return records, validation_rows


def _write_routes(
    candidate_df: pd.DataFrame,
    out_dir: Path,
    seed: int,
    sim_duration: int,
    demand_mode: str,
    arrival_process: str,
    vehicle_count_per_candidate: int,
    vehicle_hourly_volume: float,
    vehicle_lane_scale: float,
    vehicle_start_time: float,
    vehicle_depart_spacing_sec: float,
) -> tuple[Path, Path, Path, Path, pd.DataFrame, pd.DataFrame]:
    ped_records = _build_ped_records(candidate_df, seed, sim_duration)
    vehicle_records, vehicle_validation_rows = _build_vehicle_records(
        candidate_df,
        Path(candidate_df["batch_network_file"].iloc[0]),
        demand_mode,
        arrival_process,
        sim_duration,
        vehicle_count_per_candidate,
        vehicle_hourly_volume,
        vehicle_lane_scale,
        vehicle_start_time,
        vehicle_depart_spacing_sec,
        seed,
    )

    ped_path = out_dir / "demand_pedestrian.rou.xml"
    veh_path = out_dir / "demand_vehicle.rou.xml"
    all_path = out_dir / "demand_all.rou.xml"

    ped_lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        "<routes xmlns:xsi=\"http://www.w3.org/2001/XMLSchema-instance\">",
        '    <vType id="pedestrian_type" vClass="pedestrian"/>',
    ]
    veh_lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        "<routes xmlns:xsi=\"http://www.w3.org/2001/XMLSchema-instance\">",
        '    <vType id="car" vClass="passenger" maxSpeed="15.0" accel="2.6" decel="4.5"/>',
    ]
    all_lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        "<routes xmlns:xsi=\"http://www.w3.org/2001/XMLSchema-instance\">",
        '    <vType id="pedestrian_type" vClass="pedestrian"/>',
        '    <vType id="car" vClass="passenger" maxSpeed="15.0" accel="2.6" decel="4.5"/>',
    ]

    for record in vehicle_records:
        route_edges = " ".join(record.route_edge_sequence)
        route_line = f'    <route id="{record.route_id}" edges="{route_edges}"/>'
        vehicle_line = (
            f'    <vehicle id="{record.vehicle_id}" type="car" route="{record.route_id}" depart="{record.depart}"/>'
        )
        veh_lines.append(route_line)
        veh_lines.append(vehicle_line)

    for ped in ped_records:
        person_line = f'    <person id="{ped.person_id}" type="pedestrian_type" depart="{ped.depart}">'
        walk_line = f'        <walk from="{ped.route_from_edge}" to="{ped.route_to_edge}"/>'
        end_line = "    </person>"
        ped_lines.extend([person_line, walk_line, end_line])

    combined_events: list[tuple[float, str, str, str]] = []
    for record in vehicle_records:
        combined_events.append((record.depart, "vehicle", record.vehicle_id, record.route_id))
    for ped in ped_records:
        combined_events.append((ped.depart, "person", ped.person_id, ""))
    combined_events.sort(key=lambda item: (item[0], 0 if item[1] == "person" else 1, item[2]))

    for _, kind, item_id, route_id in combined_events:
        if kind == "vehicle":
            record = next(rec for rec in vehicle_records if rec.vehicle_id == item_id)
            route_edges = " ".join(record.route_edge_sequence)
            all_lines.append(f'    <route id="{route_id}" edges="{route_edges}"/>')
            all_lines.append(f'    <vehicle id="{item_id}" type="car" route="{route_id}" depart="{record.depart}"/>')
        else:
            ped = next(rec for rec in ped_records if rec.person_id == item_id)
            all_lines.extend(
                [
                    f'    <person id="{ped.person_id}" type="pedestrian_type" depart="{ped.depart}">',
                    f'        <walk from="{ped.route_from_edge}" to="{ped.route_to_edge}"/>',
                    "    </person>",
                ]
            )

    ped_lines.append("</routes>")
    veh_lines.append("</routes>")
    all_lines.append("</routes>")
    ped_path.write_text("\n".join(ped_lines) + "\n", encoding="utf-8")
    veh_path.write_text("\n".join(veh_lines) + "\n", encoding="utf-8")
    all_path.write_text("\n".join(all_lines) + "\n", encoding="utf-8")

    ped_order_df = pd.DataFrame(
        [
            {
                "sorted_index": idx + 1,
                "person_id": rec.person_id,
                "crosswalk_id": rec.crosswalk_id,
                "depart": rec.depart,
                "route_from_edge": rec.route_from_edge,
                "route_to_edge": rec.route_to_edge,
                "candidate_index": rec.candidate_index,
                "original_candidate_index": rec.original_candidate_index,
                "repeat_index": rec.repeat_index,
            }
            for idx, rec in enumerate(ped_records)
        ]
    )
    ped_order_df["depart_sorted_ok"] = ped_order_df["depart"].is_monotonic_increasing if not ped_order_df.empty else True
    ped_order_path = out_dir / "pedestrian_route_order_debug.csv"
    ped_order_df.to_csv(ped_order_path, index=False)

    vehicle_validation_df = pd.DataFrame(vehicle_validation_rows)
    return ped_path, veh_path, all_path, ped_order_path, ped_order_df, vehicle_validation_df


def _write_sumocfg(cfg_path: Path, net_file: Path, route_file: Path, sim_duration: int, warmup: int) -> Path:
    content = f'''<?xml version="1.0" encoding="UTF-8"?>
<configuration>
  <input>
    <net-file value="{net_file}"/>
    <route-files value="{route_file}"/>
  </input>
  <time>
    <begin value="0"/>
    <end value="{sim_duration}"/>
    <step-length value="1"/>
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


def _person_routes_crossing(candidate_df: pd.DataFrame) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for row in candidate_df.itertuples(index=False):
        out[str(row.crosswalk_id)] = {
            "crossing_id": str(getattr(row, "crossing_id", "")),
            "crossing_edge_id": str(row.crossing_edge_id),
            "route_from_edge": str(row.route_from_edge),
            "route_to_edge": str(row.route_to_edge),
            "tls_id": str(row.tls_id_used),
            "ped_link_indices": _parse_int_list(getattr(row, "ped_link_indices", [])),
            "ped_link_index": int(row.ped_link_index),
            "batch_network_file": str(row.batch_network_file),
            "candidate_index": int(row.candidate_index),
            "original_candidate_index": int(getattr(row, "original_candidate_index", row.candidate_index)),
        }
    return out


def _collect_vehicle_metrics(
    candidate_df: pd.DataFrame,
    net_file: Path,
    scenario: str,
    seed: int,
    sim_duration: int,
    warmup: int,
    step_length: float,
    out_dir: Path,
    include_vehicles: bool,
    extension_sec: float,
    demand_mode: str,
    arrival_process: str,
    vehicle_count_per_candidate: int,
    vehicle_hourly_volume: float,
    vehicle_lane_scale: float,
    vehicle_start_time: float,
    vehicle_depart_spacing_sec: float,
    impact_radii_m: list[float],
    include_global_scope: bool,
    ped_repeat_count_override: int | None,
    require_pedestrian_only_extension: bool,
    write_extension_skip_events: bool,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    if traci is None:
        raise RuntimeError("traci not importable")

    impact_cache, impact_edges_df, impact_anchor_df = _build_impact_scope_cache(
        candidate_df,
        net_file,
        impact_radii_m,
        include_global_scope,
    )

    ped_path, veh_path, all_path, ped_order_path, ped_order_df, vehicle_validation_df = _write_routes(
        candidate_df,
        out_dir,
        seed,
        sim_duration,
        demand_mode,
        arrival_process,
        vehicle_count_per_candidate,
        vehicle_hourly_volume,
        vehicle_lane_scale,
        vehicle_start_time,
        vehicle_depart_spacing_sec,
    )
    vehicle_validation_path = out_dir / f"phase6_vehicle_flow_{scenario}_vehicle_route_validation.csv"
    vehicle_validation_df.to_csv(vehicle_validation_path, index=False)

    route_file = all_path if include_vehicles else ped_path
    cfg_path = _write_sumocfg(out_dir / f"phase6_vehicle_flow_{scenario}.sumocfg", net_file, route_file, sim_duration, warmup)

    cmd = [_sumo_binary(), "-c", str(cfg_path), "--no-step-log", "--collision.action", "warn", "--time-to-teleport", "-1"]

    results: list[dict[str, Any]] = []
    ped_debug_rows: list[dict[str, Any]] = []
    veh_debug_rows: list[dict[str, Any]] = []
    extension_condition_rows: list[dict[str, Any]] = []
    extension_events: list[dict[str, Any]] = []
    extension_skip_events: list[dict[str, Any]] = []
    sim_error: str | None = None
    sim_error_trace: str | None = None
    completed = False
    enforce_pedestrian_only_extension = scenario == "smart" or bool(require_pedestrian_only_extension)

    candidate_meta = _person_routes_crossing(candidate_df)
    expected_repeat_counts = {
        str(row.crosswalk_id): int(getattr(row, "ped_repeat_count", 1) or 1) for row in candidate_df.itertuples(index=False)
    }
    if ped_repeat_count_override is not None:
        expected_repeat_counts = {cid: int(ped_repeat_count_override) for cid in expected_repeat_counts}
    crossing_roads = {
        str(row.crosswalk_id): _candidate_crossing_roads_simple(row)
        for row in candidate_df.itertuples(index=False)
    }

    candidate_crosswalk_ids = _candidate_crosswalk_ids(candidate_df)
    ped_people_seen: dict[str, set[str]] = {cid: set() for cid in candidate_crosswalk_ids}
    ped_presence_steps: dict[str, int] = {cid: 0 for cid in candidate_crosswalk_ids}
    ped_dep_times: dict[str, float] = {}
    vehicle_departed: dict[str, set[str]] = {cid: set() for cid in candidate_crosswalk_ids}
    vehicle_arrived: dict[str, set[str]] = {cid: set() for cid in candidate_crosswalk_ids}
    vehicle_route_valid_count: dict[str, int] = {cid: 0 for cid in candidate_crosswalk_ids}
    vehicle_requested_count: dict[str, int] = {
        str(row.crosswalk_id): _vehicle_count_for_candidate(
            demand_mode,
            sim_duration,
            vehicle_count_per_candidate,
            vehicle_hourly_volume,
            vehicle_lane_scale,
        )
        for row in candidate_df.itertuples(index=False)
    }
    vehicle_wait_samples: dict[str, list[float]] = {cid: [] for cid in candidate_crosswalk_ids}
    vehicle_loss_samples: dict[str, list[float]] = {cid: [] for cid in candidate_crosswalk_ids}
    vehicle_debug_seen: set[tuple[str, str]] = set()

    scope_names = [scope_name for scope_name, _ in _impact_scope_names(impact_radii_m, include_global_scope)]
    scope_stats: dict[str, dict[str, dict[str, Any]]] = {}
    road_to_scope_keys: dict[str, list[tuple[str, str]]] = {}
    for row in candidate_df.itertuples(index=False):
        cid = str(row.crosswalk_id)
        scope_stats[cid] = {}
        scope_info = impact_cache.get(cid, {}).get("scope_map", {})
        for scope_name in scope_names:
            info = scope_info.get(scope_name, {})
            edge_ids = set(info.get("edge_ids", set()) or set())
            scope_stats[cid][scope_name] = {
                "edge_count": int(info.get("edge_count", len(edge_ids))),
                "edge_ids": edge_ids,
                "vehicle_seen_ids": set(),
                "wait_samples": [],
                "loss_samples": [],
                "active_counts": [],
            }
            if scope_name != "global":
                for edge_id in edge_ids:
                    road_to_scope_keys.setdefault(edge_id, []).append((cid, scope_name))

    try:
        traci.start(cmd)
        candidate_vehicle_ids = {
            row.crosswalk_id: {
                f"veh_{row.candidate_index}_{idx + 1}_{row.crosswalk_id}"
                for idx in range(_vehicle_count_for_candidate(
                    demand_mode,
                    sim_duration,
                    vehicle_count_per_candidate,
                    vehicle_hourly_volume,
                    vehicle_lane_scale,
                ))
            }
            for row in candidate_df.itertuples(index=False)
        }
        vehicle_route_valid_count = {
            str(row.crosswalk_id): int(vehicle_validation_df[vehicle_validation_df["crosswalk_id"].astype(str) == str(row.crosswalk_id)]["valid"].sum())
            for row in candidate_df.itertuples(index=False)
        }

        step = 0
        last_phase_by_tls: dict[str, int | None] = {}
        phase_segment_by_tls: dict[str, int] = {}
        extended_segments: set[tuple[str, str, int]] = set()

        while step <= int(sim_duration):
            traci.simulationStep()
            t = float(traci.simulation.getTime())
            active_people = list(traci.person.getIDList())
            active_vehicles = list(traci.vehicle.getIDList())

            for pid in traci.simulation.getDepartedPersonIDList():
                ped_dep_times[str(pid)] = t

            departed_vehicles = {str(v) for v in traci.simulation.getDepartedIDList()}
            arrived_vehicles = {str(v) for v in traci.simulation.getArrivedIDList()}

            active_vehicle_records: list[tuple[str, str, float | None, float | None]] = []
            for vid in active_vehicles:
                try:
                    road = str(traci.vehicle.getRoadID(vid))
                except Exception:
                    road = ""
                wait = None
                loss = None
                try:
                    wait = float(traci.vehicle.getAccumulatedWaitingTime(vid))
                except Exception:
                    pass
                try:
                    loss = float(traci.vehicle.getTimeLoss(vid))
                except Exception:
                    pass
                active_vehicle_records.append((str(vid), road, wait, loss))

            for cid in candidate_crosswalk_ids:
                meta = candidate_meta[cid]
                tls_id = str(meta["tls_id"])
                ped_link_index = int(meta["ped_link_index"])
                ped_link_indices = list(meta.get("ped_link_indices", [ped_link_index]))
                crossing_edge_id = str(meta["crossing_edge_id"])
                route_from_edge = str(meta["route_from_edge"])
                route_to_edge = str(meta["route_to_edge"])
                crossing_edge_set = crossing_roads.get(cid, set()) | {crossing_edge_id}

                try:
                    state = traci.trafficlight.getRedYellowGreenState(tls_id)
                    phase = int(traci.trafficlight.getPhase(tls_id))
                    remaining = float(traci.trafficlight.getNextSwitch(tls_id) - t)
                except Exception:
                    state = ""
                    phase = -1
                    remaining = float("nan")

                prev_phase = last_phase_by_tls.get(tls_id)
                if prev_phase != phase:
                    phase_segment_by_tls[tls_id] = phase_segment_by_tls.get(tls_id, 0) + 1
                    last_phase_by_tls[tls_id] = phase
                phase_segment = phase_segment_by_tls.get(tls_id, 0)

                ped_near = 0
                active_crossing_people: list[str] = []
                for pid in active_people:
                    try:
                        road = traci.person.getRoadID(pid)
                    except Exception:
                        continue
                    if road in crossing_edge_set or road.startswith(f":{tls_id}_") or road.startswith(f":{str(tls_id)}_"):
                        ped_near += 1
                        active_crossing_people.append(str(pid))
                        ped_people_seen[cid].add(str(pid))

                if ped_near > 0:
                    ped_presence_steps[cid] += 1

                extension_triggered = False
                state_at_ped_link = state[ped_link_index] if ped_link_index < len(state) else ""
                already_extended = (tls_id, cid, phase_segment) in extended_segments
                phase_audit = _phase_link_audit(state, ped_link_indices)
                extension_condition_met = (
                    scenario == "smart"
                    and ped_link_index < len(state)
                    and state_at_ped_link in {"G", "g"}
                    and ped_near > 0
                    and remaining <= 12.0
                    and not already_extended
                )
                extension_allowed = (not enforce_pedestrian_only_extension) or (
                    phase_audit["ped_green_ok"] and phase_audit["extension_phase_type"] == "pedestrian_only"
                )
                extension_condition_rows.append(
                    {
                        "time": round(t, 1),
                        "crosswalk_id": cid,
                        "candidate_index": int(meta["candidate_index"]),
                        "original_candidate_index": int(meta["original_candidate_index"]),
                        "tls_id": tls_id,
                        "phase": phase,
                        "phase_segment": phase_segment,
                        "state": state,
                        "ped_link_index": ped_link_index,
                        "state_at_ped_link": state_at_ped_link,
                        "remaining_time": round(remaining, 1) if remaining == remaining else None,
                        "ped_near": ped_near,
                        "already_extended": already_extended,
                        "extension_condition_met": extension_condition_met,
                        "extension_allowed": extension_allowed,
                        "phase_extension_affects_non_ped_green": phase_audit["phase_extension_affects_non_ped_green"],
                        "extension_phase_type": phase_audit["extension_phase_type"],
                        "extension_triggered": False,
                    }
                )
                if extension_condition_met:
                    segment_key = (tls_id, cid, phase_segment)
                    if enforce_pedestrian_only_extension and not extension_allowed:
                        extension_skip_events.append(
                            {
                                "time": round(t, 1),
                                "crosswalk_id": cid,
                                "tls_id": tls_id,
                                "linkIndex": ped_link_index,
                                "phase": phase,
                                "state": state,
                                "ped_link_indices": phase_audit["ped_link_indices"],
                                "green_indices_in_phase": phase_audit["green_indices_in_phase"],
                                "non_ped_green_indices_in_phase": phase_audit["non_ped_green_indices_in_phase"],
                                "skip_reason": "mixed_phase",
                            }
                        )
                    else:
                        try:
                            traci.trafficlight.setPhaseDuration(tls_id, max(0.0, remaining) + float(extension_sec))
                            extension_events.append(
                                {
                                    "time": round(t, 1),
                                    "crosswalk_id": cid,
                                    "candidate_index": int(meta["candidate_index"]),
                                    "original_candidate_index": int(meta["original_candidate_index"]),
                                    "tls_id": tls_id,
                                    "linkIndex": ped_link_index,
                                    "phase": phase,
                                    "phase_segment": phase_segment,
                                    "state": state,
                                    "remaining_before": round(remaining, 1),
                                    "extension_sec": float(extension_sec),
                                    "ped_near": ped_near,
                                    "ped_link_indices": phase_audit["ped_link_indices"],
                                    "green_indices_in_phase": phase_audit["green_indices_in_phase"],
                                    "non_ped_green_indices_in_phase": phase_audit["non_ped_green_indices_in_phase"],
                                    "non_ped_green_count": phase_audit["non_ped_green_count"],
                                    "extension_phase_type": phase_audit["extension_phase_type"],
                                    "phase_extension_affects_non_ped_green": phase_audit["phase_extension_affects_non_ped_green"],
                                }
                            )
                            extended_segments.add(segment_key)
                            extension_triggered = True
                        except Exception as exc:
                            extension_events.append(
                                {
                                    "time": round(t, 1),
                                    "crosswalk_id": cid,
                                    "candidate_index": int(meta["candidate_index"]),
                                    "original_candidate_index": int(meta["original_candidate_index"]),
                                    "tls_id": tls_id,
                                    "linkIndex": ped_link_index,
                                    "phase": phase,
                                    "phase_segment": phase_segment,
                                    "state": state,
                                    "remaining_before": round(remaining, 1),
                                    "extension_sec": float(extension_sec),
                                    "ped_near": ped_near,
                                    "ped_link_indices": phase_audit["ped_link_indices"],
                                    "green_indices_in_phase": phase_audit["green_indices_in_phase"],
                                    "non_ped_green_indices_in_phase": phase_audit["non_ped_green_indices_in_phase"],
                                    "non_ped_green_count": phase_audit["non_ped_green_count"],
                                    "extension_phase_type": phase_audit["extension_phase_type"],
                                    "phase_extension_affects_non_ped_green": phase_audit["phase_extension_affects_non_ped_green"],
                                    "error": str(exc),
                                }
                            )
                            extended_segments.add(segment_key)
                if extension_condition_rows:
                    extension_condition_rows[-1]["extension_triggered"] = extension_triggered

                ped_debug_rows.append(
                    {
                        "time": round(t, 1),
                        "scenario": scenario,
                        "crosswalk_id": cid,
                        "candidate_index": int(meta["candidate_index"]),
                        "original_candidate_index": int(meta["original_candidate_index"]),
                        "tls_id": tls_id,
                        "phase": phase,
                        "phase_segment": phase_segment,
                        "state": state,
                        "remaining_time": round(remaining, 1) if remaining == remaining else None,
                        "ped_near": ped_near,
                        "active_crossing_people": "|".join(sorted(active_crossing_people)),
                        "extension_triggered": extension_triggered,
                    }
                )

                candidate_vehicle_ids_for_cid = candidate_vehicle_ids.get(cid, set())
                active_candidate_vehicles = [vid for vid in active_vehicles if vid in candidate_vehicle_ids_for_cid]
                for vid in departed_vehicles:
                    if vid in candidate_vehicle_ids_for_cid:
                        vehicle_departed[cid].add(vid)
                for vid in arrived_vehicles:
                    if vid in candidate_vehicle_ids_for_cid:
                        vehicle_arrived[cid].add(vid)
                for vid, road, wait, loss in active_vehicle_records:
                    if vid in candidate_vehicle_ids_for_cid:
                        if wait is not None:
                            vehicle_wait_samples[cid].append(wait)
                        if loss is not None:
                            vehicle_loss_samples[cid].append(loss)
                for scope_name, scope_state in scope_stats.get(cid, {}).items():
                    if scope_name == "global":
                        scope_active = active_vehicle_records
                    else:
                        edge_ids = scope_state["edge_ids"]
                        scope_active = [record for record in active_vehicle_records if record[1] in edge_ids]
                    scope_state["active_counts"].append(len(scope_active))
                    for vid, road, wait, loss in scope_active:
                        scope_state["vehicle_seen_ids"].add(vid)
                        if wait is not None:
                            scope_state["wait_samples"].append(wait)
                        if loss is not None:
                            scope_state["loss_samples"].append(loss)

                waits = [wait for vid, road, wait, loss in active_vehicle_records if vid in candidate_vehicle_ids_for_cid and wait is not None]
                losses = [loss for vid, road, wait, loss in active_vehicle_records if vid in candidate_vehicle_ids_for_cid and loss is not None]
                if waits or losses or active_candidate_vehicles:
                    veh_debug_rows.append(
                        {
                            "time": round(t, 1),
                            "scenario": scenario,
                            "crosswalk_id": cid,
                            "candidate_index": int(meta["candidate_index"]),
                            "original_candidate_index": int(meta["original_candidate_index"]),
                            "tls_id": tls_id,
                            "active_vehicle_count": len(active_candidate_vehicles),
                            "departed_count_cum": len(vehicle_departed[cid]),
                            "arrived_count_cum": len(vehicle_arrived[cid]),
                            "veh_waiting_time_mean_step": round(sum(waits) / len(waits), 2) if waits else None,
                            "veh_waiting_time_max_step": round(max(waits), 2) if waits else None,
                            "veh_time_loss_mean_step": round(sum(losses) / len(losses), 2) if losses else None,
                            "veh_time_loss_max_step": round(max(losses), 2) if losses else None,
                        }
                    )
                    vehicle_wait_samples[cid].extend(waits)
                    vehicle_loss_samples[cid].extend(losses)

            if step >= int(sim_duration):
                break
            step += 1

        completed = True
    except Exception as exc:
        sim_error = str(exc)
        sim_error_trace = traceback.format_exc()
        completed = False
    finally:
        if traci is not None:
            try:
                traci.close(False)
            except Exception:
                pass

    result_rows: list[dict[str, Any]] = []
    for row in candidate_df.itertuples(index=False):
        cid = str(row.crosswalk_id)
        wait_samples = vehicle_wait_samples.get(cid, [])
        loss_samples = vehicle_loss_samples.get(cid, [])
        expected_repeat_count = int(expected_repeat_counts.get(cid, 1))
        result_row: dict[str, Any] = {
            "crosswalk_id": cid,
            "scenario": scenario,
            "completed": completed,
            "candidate_index": int(getattr(row, "candidate_index", 0)),
            "original_candidate_index": int(getattr(row, "original_candidate_index", getattr(row, "candidate_index", 0))),
            "expected_ped_repeat_count": expected_repeat_count,
            "ped_crossing_person_count": len(ped_people_seen.get(cid, set())),
            "ped_repeat_count_match": len(ped_people_seen.get(cid, set())) == expected_repeat_count,
            "ped_crossing_presence_steps": ped_presence_steps.get(cid, 0),
            "vehicle_requested_count": vehicle_requested_count.get(cid, 0),
            "vehicle_route_valid_count": vehicle_route_valid_count.get(cid, 0),
            "vehicle_departed_count": len(vehicle_departed.get(cid, set())),
            "vehicle_arrived_count": len(vehicle_arrived.get(cid, set())),
            "vehicle_route_invalid_count": max(0, vehicle_requested_count.get(cid, 0) - vehicle_route_valid_count.get(cid, 0)),
            "veh_waiting_time_mean": round(sum(wait_samples) / len(wait_samples), 3) if wait_samples else None,
            "veh_waiting_time_max": round(max(wait_samples), 3) if wait_samples else None,
            "veh_time_loss_mean": round(sum(loss_samples) / len(loss_samples), 3) if loss_samples else None,
            "veh_time_loss_max": round(max(loss_samples), 3) if loss_samples else None,
            "extension_count": len([e for e in extension_events if str(e.get("crosswalk_id", "")) == cid]),
            "extension_sec": float(extension_sec),
            "simulation_error": sim_error or "",
        }
        scope_info = scope_stats.get(cid, {})
        for scope_name in scope_names:
            stat = scope_info.get(scope_name, {})
            edge_count = int(stat.get("edge_count", 0))
            seen_ids = stat.get("vehicle_seen_ids", set())
            active_counts = list(stat.get("active_counts", []))
            wait_s = list(stat.get("wait_samples", []))
            loss_s = list(stat.get("loss_samples", []))
            prefix = f"impact_{scope_name}_"
            result_row[f"{prefix}edge_count"] = edge_count
            result_row[f"{prefix}vehicle_seen_count"] = int(len(seen_ids))
            result_row[f"{prefix}waiting_time_mean"] = round(sum(wait_s) / len(wait_s), 3) if wait_s else None
            result_row[f"{prefix}waiting_time_max"] = round(max(wait_s), 3) if wait_s else None
            result_row[f"{prefix}time_loss_mean"] = round(sum(loss_s) / len(loss_s), 3) if loss_s else None
            result_row[f"{prefix}time_loss_max"] = round(max(loss_s), 3) if loss_s else None
            result_row[f"{prefix}active_vehicle_mean"] = round(sum(active_counts) / len(active_counts), 3) if active_counts else 0.0
            result_row[f"{prefix}active_vehicle_max"] = int(max(active_counts)) if active_counts else 0
        result_rows.append(result_row)

    results_df = pd.DataFrame(result_rows)
    results_df.to_csv(out_dir / f"phase6_vehicle_flow_{scenario}_results.csv", index=False)
    ext_cols = [
        "time",
        "crosswalk_id",
        "candidate_index",
        "original_candidate_index",
        "tls_id",
        "linkIndex",
        "phase",
        "phase_segment",
        "state",
        "remaining_before",
        "extension_sec",
        "ped_near",
        "ped_link_indices",
        "green_indices_in_phase",
        "non_ped_green_indices_in_phase",
        "non_ped_green_count",
        "extension_phase_type",
        "phase_extension_affects_non_ped_green",
        "error",
    ]
    skip_cols = [
        "time",
        "crosswalk_id",
        "tls_id",
        "linkIndex",
        "phase",
        "state",
        "ped_link_indices",
        "green_indices_in_phase",
        "non_ped_green_indices_in_phase",
        "skip_reason",
    ]
    ext_cond_cols = [
        "time",
        "crosswalk_id",
        "candidate_index",
        "original_candidate_index",
        "tls_id",
        "phase",
        "phase_segment",
        "state",
        "ped_link_index",
        "state_at_ped_link",
        "remaining_time",
        "ped_near",
        "already_extended",
        "extension_condition_met",
        "extension_allowed",
        "phase_extension_affects_non_ped_green",
        "extension_phase_type",
        "extension_triggered",
    ]
    ped_cols = ["time", "scenario", "crosswalk_id", "candidate_index", "original_candidate_index", "tls_id", "phase", "phase_segment", "state", "remaining_time", "ped_near", "active_crossing_people", "extension_triggered"]
    veh_cols = ["time", "scenario", "crosswalk_id", "candidate_index", "original_candidate_index", "tls_id", "active_vehicle_count", "departed_count_cum", "arrived_count_cum", "veh_waiting_time_mean_step", "veh_waiting_time_max_step", "veh_time_loss_mean_step", "veh_time_loss_max_step"]
    pd.DataFrame(extension_events, columns=ext_cols).to_csv(out_dir / f"phase6_vehicle_flow_{scenario}_extension_events.csv", index=False)
    pd.DataFrame(extension_skip_events, columns=skip_cols).to_csv(out_dir / f"phase6_vehicle_flow_{scenario}_extension_skip_events.csv", index=False)
    pd.DataFrame(extension_condition_rows, columns=ext_cond_cols).to_csv(out_dir / f"phase6_vehicle_flow_{scenario}_extension_condition_debug.csv", index=False)
    pd.DataFrame(ped_debug_rows, columns=ped_cols).to_csv(out_dir / f"phase6_vehicle_flow_{scenario}_pedestrian_debug_trace.csv", index=False)
    pd.DataFrame(veh_debug_rows, columns=veh_cols).to_csv(out_dir / f"phase6_vehicle_flow_{scenario}_vehicle_debug_trace.csv", index=False)
    impact_edges_df.to_csv(out_dir / f"phase6_vehicle_flow_{scenario}_impact_edges.csv", index=False)
    impact_anchor_df.to_csv(out_dir / f"phase6_vehicle_flow_{scenario}_impact_anchor_debug.csv", index=False)
    vehicle_validation_df.to_csv(vehicle_validation_path, index=False)

    metadata = {
        "candidate_csv": str(candidate_df.attrs.get("candidate_csv", "")),
        "net_file": str(net_file),
        "scenario": scenario,
        "seed": seed,
        "sim_duration": sim_duration,
        "warmup": warmup,
        "extension_sec": float(extension_sec),
        "vehicle_demand_mode": demand_mode,
        "vehicle_arrival_process": arrival_process,
        "vehicle_count_per_candidate": int(vehicle_count_per_candidate),
        "vehicle_hourly_volume": float(vehicle_hourly_volume),
        "vehicle_lane_scale": float(vehicle_lane_scale),
        "vehicle_depart_spacing_sec": float(vehicle_depart_spacing_sec),
        "vehicle_start_time": float(vehicle_start_time),
        "candidate_rows": int(len(candidate_df)),
        "candidate_ids": candidate_df["crosswalk_id"].astype(str).tolist(),
        "candidate_index_map": [
            {
                "crosswalk_id": str(row.crosswalk_id),
                "candidate_index": int(getattr(row, "candidate_index", 0)),
                "original_candidate_index": int(getattr(row, "original_candidate_index", getattr(row, "candidate_index", 0))),
            }
            for row in candidate_df.itertuples(index=False)
        ],
        "completed": bool(completed),
        "simulation_error": sim_error,
        "simulation_error_trace": sim_error_trace or "",
        "ped_route_file": str(ped_path),
        "vehicle_route_file": str(veh_path),
        "combined_route_file": str(all_path),
        "pedestrian_route_order_debug": str(ped_order_path),
        "vehicle_route_validation": str(vehicle_validation_path),
        "extension_condition_debug": str(out_dir / f"phase6_vehicle_flow_{scenario}_extension_condition_debug.csv"),
        "impact_edges_csv": str(out_dir / f"phase6_vehicle_flow_{scenario}_impact_edges.csv"),
        "impact_anchor_debug_csv": str(out_dir / f"phase6_vehicle_flow_{scenario}_impact_anchor_debug.csv"),
        "impact_radii_m": impact_radii_m,
        "include_global_scope": bool(include_global_scope),
        "sumocfg": str(cfg_path),
    }
    (out_dir / "run_metadata.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")

    return results_df, metadata


def _find_latest_result(root: Path, scenario: str) -> Path | None:
    matches = sorted(root.rglob(f"phase6_vehicle_flow_{scenario}_results.csv"), key=lambda p: p.stat().st_mtime, reverse=True)
    return matches[0] if matches else None


def _write_compare_if_ready(out_dir: Path, scenario: str) -> None:
    other = "smart" if scenario == "baseline" else "baseline"
    root = out_dir.parent
    current_file = _find_latest_result(root, scenario)
    other_file = _find_latest_result(root, other)
    compare_path = COMPARE_DIR / "phase6_vehicle_flow_baseline_vs_smart_BATCH03_SMOKE.csv"
    COMPARE_DIR.mkdir(parents=True, exist_ok=True)

    if not current_file or not other_file:
        _write_compare_warning(
            COMPARE_DIR,
            scenario,
            "compare_inputs_missing",
            {
                "current_file": str(current_file) if current_file else "",
                "other_file": str(other_file) if other_file else "",
                "compare_path": str(compare_path),
            },
        )
        return

    current_df = pd.read_csv(current_file).set_index("crosswalk_id")
    other_df = pd.read_csv(other_file).set_index("crosswalk_id")
    ordered_ids = list(dict.fromkeys(current_df.index.astype(str).tolist() + other_df.index.astype(str).tolist()))
    rows: list[dict[str, Any]] = []
    for cid in ordered_ids:
        cur = current_df.loc[cid] if cid in current_df.index else None
        oth = other_df.loc[cid] if cid in other_df.index else None
        rows.append(
            {
                "crosswalk_id": cid,
                "baseline_completed": bool(_safe_get(cur, "completed")) if scenario == "baseline" else bool(_safe_get(oth, "completed")),
                "smart_completed": bool(_safe_get(cur, "completed")) if scenario == "smart" else bool(_safe_get(oth, "completed")),
                "baseline_vehicle_route_valid_count": int(_safe_get(cur, "vehicle_route_valid_count")) if scenario == "baseline" else int(_safe_get(oth, "vehicle_route_valid_count")),
                "smart_vehicle_route_valid_count": int(_safe_get(cur, "vehicle_route_valid_count")) if scenario == "smart" else int(_safe_get(oth, "vehicle_route_valid_count")),
                "baseline_vehicle_arrived_count": int(_safe_get(cur, "vehicle_arrived_count")) if scenario == "baseline" else int(_safe_get(oth, "vehicle_arrived_count")),
                "smart_vehicle_arrived_count": int(_safe_get(cur, "vehicle_arrived_count")) if scenario == "smart" else int(_safe_get(oth, "vehicle_arrived_count")),
                "baseline_veh_waiting_time_mean": _safe_get(cur, "veh_waiting_time_mean") if scenario == "baseline" else _safe_get(oth, "veh_waiting_time_mean"),
                "smart_veh_waiting_time_mean": _safe_get(cur, "veh_waiting_time_mean") if scenario == "smart" else _safe_get(oth, "veh_waiting_time_mean"),
                "baseline_veh_time_loss_mean": _safe_get(cur, "veh_time_loss_mean") if scenario == "baseline" else _safe_get(oth, "veh_time_loss_mean"),
                "smart_veh_time_loss_mean": _safe_get(cur, "veh_time_loss_mean") if scenario == "smart" else _safe_get(oth, "veh_time_loss_mean"),
                "baseline_extension_count": int(_safe_get(cur, "extension_count")) if scenario == "baseline" else int(_safe_get(oth, "extension_count")),
                "smart_extension_count": int(_safe_get(cur, "extension_count")) if scenario == "smart" else int(_safe_get(oth, "extension_count")),
                "baseline_extension_sec": _safe_get(cur, "extension_sec") if scenario == "baseline" else _safe_get(oth, "extension_sec"),
                "smart_extension_sec": _safe_get(cur, "extension_sec") if scenario == "smart" else _safe_get(oth, "extension_sec"),
            }
        )
    pd.DataFrame(rows).to_csv(compare_path, index=False)


def main() -> None:
    parser = argparse.ArgumentParser(description="Phase 6 vehicle flow runner for batch03 smoke and mini experiments.")
    parser.add_argument("--candidate-csv", required=True)
    parser.add_argument("--net-file", required=True)
    parser.add_argument("--scenario", choices=["baseline", "smart"], required=True)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--sim-duration", type=int, default=600)
    parser.add_argument("--warmup", type=int, default=0)
    parser.add_argument("--extension-sec", type=float, default=5.0)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--vehicle-demand-mode", choices=["smoke", "fixed", "hourly"], default="smoke")
    parser.add_argument("--vehicle-hourly-volume", type=float, default=0.0)
    parser.add_argument("--vehicle-arrival-process", choices=["deterministic", "poisson"], default="deterministic")
    parser.add_argument("--vehicle-lane-scale", type=float, default=1.0)
    parser.add_argument("--vehicle-count-per-candidate", type=int, default=3)
    parser.add_argument("--vehicle-depart-spacing-sec", type=float, default=5.0)
    parser.add_argument("--vehicle-start-time", type=float, default=0.0)
    parser.add_argument("--impact-radii-m", default="100,300,500,1000")
    parser.add_argument("--impact-radius-m", type=float, default=None)
    parser.add_argument("--include-global-scope", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--limit-crosswalk-ids", nargs="*")
    parser.add_argument("--ped-repeat-count-override", type=int)
    parser.add_argument("--require-pedestrian-only-extension", action=argparse.BooleanOptionalAction, default=False)
    parser.add_argument("--write-extension-skip-events", action=argparse.BooleanOptionalAction, default=False)
    args = parser.parse_args()

    apply_sumo_environment()

    candidate_csv = Path(args.candidate_csv).expanduser().resolve()
    net_file = Path(args.net_file).expanduser().resolve()
    out_dir = Path(args.output_dir).expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    limit_crosswalk_ids = _normalize_limit_ids(args.limit_crosswalk_ids)
    candidate_df = _resolved_candidate_df(candidate_csv, net_file, limit_crosswalk_ids, args.ped_repeat_count_override)
    candidate_df = candidate_df.copy()
    candidate_df.attrs["candidate_csv"] = str(candidate_csv)

    impact_radii_m = _parse_float_list(args.impact_radii_m)
    if args.impact_radius_m is not None:
        impact_radii_m.append(float(args.impact_radius_m))
    impact_radii_m = sorted({float(radius) for radius in impact_radii_m if float(radius) > 0.0})

    results_df, metadata = _collect_vehicle_metrics(
        candidate_df,
        net_file,
        args.scenario,
        args.seed,
        args.sim_duration,
        args.warmup,
        1.0,
        out_dir,
        True,
        args.extension_sec,
        args.vehicle_demand_mode,
        args.vehicle_arrival_process,
        args.vehicle_count_per_candidate,
        args.vehicle_hourly_volume,
        args.vehicle_lane_scale,
        args.vehicle_start_time,
        args.vehicle_depart_spacing_sec,
        impact_radii_m,
        bool(args.include_global_scope),
        args.ped_repeat_count_override,
        bool(args.require_pedestrian_only_extension),
        bool(args.write_extension_skip_events),
    )

    results_df.to_csv(out_dir / f"phase6_vehicle_flow_{args.scenario}_results.csv", index=False)
    (out_dir / "run_metadata.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")

    _write_compare_if_ready(out_dir, args.scenario)

    print(f"output_dir: {out_dir}")
    print(f"candidate_rows: {len(candidate_df)}")
    print(f"scenario: {args.scenario}")


if __name__ == "__main__":
    main()
