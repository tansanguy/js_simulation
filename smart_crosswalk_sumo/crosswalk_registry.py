from __future__ import annotations

import argparse
import hashlib
import json
import math
from datetime import datetime
from pathlib import Path
from typing import Any
import xml.etree.ElementTree as ET

import pandas as pd

try:
    from .build_networks import DEFAULT_ADMIN_POLYGON_PATH, build_network
    from .integrated_mode import (
        INTEGRATED_DIRNAME,
        MATCH_DISTANCE_THRESHOLD_M,
        assess_candidate_implementation,
        apply_recovered_candidate_override,
        load_recovered_candidates,
        point_in_geojson,
    )
    from .network_utils import (
        approach_lanes_at_crossing,
        distance_to_edge_shape,
        find_tls_id,
        pedestrian_link_indices,
        pedestrian_route_from_crossing,
        read_net,
        validate_pedestrian_connectivity,
        vehicle_edges_at_crossing,
    )
    from .output_schema import write_csv_utf8_sig
    from .preprocess import load_crosswalk_features
except ImportError:
    from build_networks import DEFAULT_ADMIN_POLYGON_PATH, build_network
    from integrated_mode import (
        INTEGRATED_DIRNAME,
        MATCH_DISTANCE_THRESHOLD_M,
        assess_candidate_implementation,
        apply_recovered_candidate_override,
        load_recovered_candidates,
        point_in_geojson,
    )
    from network_utils import (
        approach_lanes_at_crossing,
        distance_to_edge_shape,
        find_tls_id,
        pedestrian_link_indices,
        pedestrian_route_from_crossing,
        read_net,
        validate_pedestrian_connectivity,
        vehicle_edges_at_crossing,
    )
    from output_schema import write_csv_utf8_sig
    from preprocess import load_crosswalk_features


BASE_DIR = Path(__file__).resolve().parent
DEFAULT_REGISTRY_PATH = BASE_DIR / "registry" / "junggu_crosswalk_sumo_registry.csv"
DEFAULT_NETWORK_VERSIONS_PATH = BASE_DIR / "registry" / "junggu_network_versions.csv"

REGISTRY_COLUMNS = [
    "canonical_network_version",
    "crosswalk_id",
    "crossing_edge",
    "tls_id",
    "ped_link_indices",
    "from_edge",
    "to_edge",
    "vehicle_conflict_edges",
    "approach_lanes",
    "match_distance_m",
    "path_uses_crossing",
    "path_uses_walkingarea",
    "tlLogic_exists_in_net_xml",
    "exists_in_connection_tl",
    "is_netconvert_ghost_tls",
    "registry_status",
    "implementation_status",
    "c_subtype",
    "recovery_status",
    "recovery_source",
    "network_net_sha256",
    "netconvert_options_sha256",
    "sumo_version",
    "netconvert_version",
    "validated_at",
    "validated_by",
    "crossing_lon",
    "crossing_lat",
    "matched_radius_m",
    "distance_bucket",
    "mapping_confidence",
    "control_mode",
    "runnable_for_signal_extension",
    "runnable_for_unsignalized_sim",
    "requires_patch",
    "interpretation_note",
    "original_failure_reason",
    "override_reason",
    "notes",
]


def _as_str(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and math.isnan(value):
        return ""
    return str(value).strip()


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    text = _as_str(value).lower()
    return text in {"1", "true", "yes", "y"}


def _to_float(value: Any) -> float | None:
    try:
        if value is None:
            return None
        num = float(value)
        if math.isnan(num):
            return None
        return num
    except Exception:
        return None


def _sha256_text(payload: str) -> str:
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _sha256_file(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            if not chunk:
                break
            hasher.update(chunk)
    return hasher.hexdigest()


def _sha256_json(payload: Any) -> str:
    return _sha256_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))


def _canonical_network_version(
    network_mode: str,
    buffer_m: float,
    network_net_sha256: str,
    netconvert_options_sha256: str,
) -> str:
    buffer_token = f"{float(buffer_m):g}".replace(".", "p")
    mode_token = _as_str(network_mode).lower() or "expanded"
    return (
        f"junggu_{mode_token}_b{buffer_token}"
        f"_net{_as_str(network_net_sha256)[:12]}_opt{_as_str(netconvert_options_sha256)[:12]}"
    )


def _collect_ghost_tls(netconvert_log: Path) -> set[str]:
    if not netconvert_log.exists():
        return set()
    marker = "does not control any links; it will not be build"
    out: set[str] = set()
    for line in netconvert_log.read_text(encoding="utf-8", errors="ignore").splitlines():
        if marker not in line:
            continue
        parts = line.split("'")
        if len(parts) >= 2:
            tl = _as_str(parts[1])
            if tl:
                out.add(tl)
    return out


def _pipe_join(values: Any) -> str:
    if isinstance(values, list):
        return "|".join(str(v) for v in values if _as_str(v))
    text = _as_str(values)
    if not text:
        return ""
    if text.startswith("[") and text.endswith("]"):
        try:
            parsed = json.loads(text)
            return _pipe_join(parsed)
        except Exception:
            pass
    return text


def _load_fingerprint(integrated_dir: Path) -> dict[str, Any]:
    net_file = integrated_dir / "network.net.xml"
    if not net_file.exists():
        raise FileNotFoundError(f"통합 네트워크 파일이 없습니다: {net_file}")

    provenance_path = integrated_dir / "network_build_provenance.json"
    provenance = json.loads(provenance_path.read_text(encoding="utf-8")) if provenance_path.exists() else {}
    filtered_options = list(provenance.get("filtered_netconvert_options") or [])
    skipped_options = list(provenance.get("skipped_netconvert_options") or [])
    netconvert_version = _as_str(provenance.get("netconvert_version"))
    options_sha = _as_str(
        provenance.get("netconvert_option_fingerprint_sha256")
        or provenance.get("netconvert_options_sha256")
    ) or _sha256_json(
        {
            "filtered_options": filtered_options,
            "skipped_options": skipped_options,
            "netconvert_version": netconvert_version,
        }
    )

    return {
        "net_file": net_file,
        "network_net_sha256": _as_str(provenance.get("network_net_sha256")) or _sha256_file(net_file),
        "netconvert_options_sha256": options_sha,
        "sumo_version": _as_str(provenance.get("sumo_version")),
        "netconvert_version": netconvert_version,
        "network_bbox": _as_str(provenance.get("network_bbox")),
    }


def _read_tls_topology(net_file: Path) -> tuple[set[str], set[str]]:
    root = ET.parse(net_file).getroot()
    tl_logic_ids = {
        _as_str(node.attrib.get("id"))
        for node in root.findall("tlLogic")
        if _as_str(node.attrib.get("id"))
    }
    connection_tl_ids = {
        _as_str(node.attrib.get("tl"))
        for node in root.findall("connection")
        if _as_str(node.attrib.get("tl"))
    }
    return tl_logic_ids, connection_tl_ids


def _classify_subtype(
    assessment: dict[str, Any],
    tls_id: str,
    is_ghost_tls: bool,
) -> str:
    ped_val = assessment.get("ped_val", {}) or {}
    failure_reason = _as_str(assessment.get("failure_reason"))
    status = _as_str(assessment.get("implementation_status"))
    if not _as_bool(ped_val.get("crossing_edge_exists", True)):
        return "C4_missing_crossing"
    if is_ghost_tls:
        return "C3_ghost_tls"
    if not tls_id and "missing_tls" in failure_reason:
        return "C2_no_tllogic"
    if "distance_exceeded" in failure_reason:
        return "C1_distance_match_failed"
    if status == "route_repair_failed" or not _as_bool(ped_val.get("path_uses_crossing", False)):
        return "C6_ped_route_failed"
    return "C11_manual_netedit_required"


def _registry_status(
    assessment: dict[str, Any],
    c_subtype: str,
    recovery_status: str,
) -> str:
    usable = _as_bool(assessment.get("final_usable_for_simulation", False))
    impl_status = _as_str(assessment.get("implementation_status"))
    if usable and impl_status == "ready_for_simulation":
        return "A"
    if usable and (impl_status == "auto_recovered_distance_rematch" or recovery_status == "auto_recovered_distance_rematch"):
        return "recovered"
    if c_subtype == "C4_missing_crossing":
        return "C4"
    if c_subtype == "C3_ghost_tls":
        return "ghost"
    return "manual_review"


def _upsert_registry_frame(existing: pd.DataFrame, fresh: pd.DataFrame) -> pd.DataFrame:
    if existing.empty:
        base = pd.DataFrame(columns=REGISTRY_COLUMNS)
    else:
        base = existing.copy()
    for col in REGISTRY_COLUMNS:
        if col not in base.columns:
            base[col] = ""
        if col not in fresh.columns:
            fresh[col] = ""

    base["canonical_network_version"] = base["canonical_network_version"].astype(str)
    base["crosswalk_id"] = base["crosswalk_id"].astype(str)
    fresh["canonical_network_version"] = fresh["canonical_network_version"].astype(str)
    fresh["crosswalk_id"] = fresh["crosswalk_id"].astype(str)

    keys = set(zip(fresh["canonical_network_version"], fresh["crosswalk_id"]))
    kept = base[
        ~base.apply(
            lambda row: (str(row["canonical_network_version"]), str(row["crosswalk_id"])) in keys,
            axis=1,
        )
    ]
    merged = pd.concat([kept, fresh], ignore_index=True)
    merged = merged[REGISTRY_COLUMNS].sort_values(
        ["canonical_network_version", "crosswalk_id"],
        kind="stable",
    )
    return merged.reset_index(drop=True)


def _upsert_network_versions(path: Path, row: dict[str, Any]) -> None:
    columns = [
        "canonical_network_version",
        "network_mode",
        "buffer_m",
        "network_net_sha256",
        "netconvert_options_sha256",
        "sumo_version",
        "netconvert_version",
        "admin_polygon_path",
        "admin_polygon_sha256",
        "network_bbox",
        "created_at",
    ]
    if path.exists():
        existing = pd.read_csv(path)
    else:
        existing = pd.DataFrame(columns=columns)
    for col in columns:
        if col not in existing.columns:
            existing[col] = ""

    key = _as_str(row.get("canonical_network_version"))
    existing = existing[existing["canonical_network_version"].astype(str) != key]
    out = pd.concat([existing, pd.DataFrame([row])], ignore_index=True)
    out = out[columns].sort_values("canonical_network_version", kind="stable")
    write_csv_utf8_sig(out, path)


def _radius_steps(start_m: float, max_m: float, step_m: float = 5.0) -> list[float]:
    if max_m <= start_m or step_m <= 0:
        return []
    out: list[float] = []
    cur = start_m + step_m
    while cur <= max_m + 1e-9:
        out.append(round(cur, 6))
        cur += step_m
    return out


def _distance_bucket_for_radius(radius_m: float) -> str:
    upper = int(round(float(radius_m)))
    lower = upper - 5
    if lower < 50:
        lower = 50
    return f"{lower}_{upper}"


def _mapping_confidence(radius_m: float) -> str:
    if radius_m <= 60.0:
        return "high"
    if radius_m <= 80.0:
        return "medium"
    return "low"


def _candidate_crossings_within(net: Any, lon: float, lat: float, radius_m: float) -> list[tuple[Any, float]]:
    x, y = net.convertLonLat2XY(float(lon), float(lat))
    nearby = net.getNeighboringEdges(x, y, float(radius_m), includeJunctions=False)
    by_id: dict[str, tuple[Any, float]] = {}
    for edge, dist in nearby:
        if edge.getFunction() != "crossing":
            continue
        eid = _as_str(edge.getID())
        if not eid:
            continue
        prev = by_id.get(eid)
        if prev is None or float(dist) < prev[1]:
            by_id[eid] = (edge, float(dist))
    return sorted(by_id.values(), key=lambda item: item[1])


def _build_candidate_metadata(
    net: Any,
    net_file: Path,
    xml_root: ET.Element,
    crossing_edge: Any,
    cw_id: str,
) -> dict[str, Any] | None:
    crossing_id = _as_str(crossing_edge.getID())
    if not crossing_id:
        return None
    try:
        crossing_shape = crossing_edge.getShape()
        crossing_lon = None
        crossing_lat = None
        if crossing_shape:
            mid = crossing_shape[len(crossing_shape) // 2]
            crossing_lon, crossing_lat = net.convertXY2LonLat(mid[0], mid[1])
        tls_id = find_tls_id(net, crossing_edge, xml_root=xml_root)
        ped_route = pedestrian_route_from_crossing(crossing_edge)
        vehicle_edges = vehicle_edges_at_crossing(crossing_edge)
        approach_lanes = approach_lanes_at_crossing(crossing_edge)
        return {
            "cw_id": cw_id,
            "net_file": str(net_file),
            "crossing_edge": crossing_id,
            "crossing_lon": float(crossing_lon) if crossing_lon is not None else None,
            "crossing_lat": float(crossing_lat) if crossing_lat is not None else None,
            "tls_id": tls_id,
            "ped_link_indices": pedestrian_link_indices(net_file, tls_id, crossing_id, xml_root=xml_root),
            "ped_route": ped_route,
            "vehicle_conflict_edges": vehicle_edges,
            "approach_lanes": approach_lanes,
        }
    except Exception:
        return None


def _attempt_c1_distance_remap(
    *,
    net: Any,
    net_file: Path,
    xml_root: ET.Element,
    feature_row: Any,
    cw_id: str,
    threshold_m: float,
    max_radius_m: float,
    tl_logic_ids: set[str],
    connection_tl_ids: set[str],
) -> dict[str, Any] | None:
    lon = float(getattr(feature_row, "longitude"))
    lat = float(getattr(feature_row, "latitude"))
    steps = _radius_steps(float(threshold_m), float(max_radius_m), 5.0)
    if not steps:
        return None
    target_xy = net.convertLonLat2XY(lon, lat)
    for radius in steps:
        candidates = _candidate_crossings_within(net, lon, lat, radius)
        for crossing_edge, _ in candidates:
            metadata = _build_candidate_metadata(net, net_file, xml_root, crossing_edge, cw_id)
            if not metadata:
                continue
            ped_val = validate_pedestrian_connectivity(
                net_file,
                metadata,
                cw_id=cw_id,
                net=net,
            )
            if not bool(ped_val.get("crossing_edge_exists", False)):
                continue
            if not bool(ped_val.get("from_edge_exists", False)):
                continue
            if not bool(ped_val.get("to_edge_exists", False)):
                continue
            if not bool(ped_val.get("path_uses_crossing", False)):
                continue
            if not bool(ped_val.get("path_uses_walkingarea", False)):
                continue
            has_vehicle_context = bool(metadata.get("vehicle_conflict_edges")) or bool(metadata.get("approach_lanes"))
            if not has_vehicle_context:
                continue
            dist_m = float(distance_to_edge_shape(crossing_edge, target_xy))
            tls_id = _as_str(metadata.get("tls_id"))
            tls_valid = bool(tls_id and tls_id in tl_logic_ids and tls_id in connection_tl_ids)
            control_mode = "proxy_signalized" if tls_valid else "proxy_unsignalized"
            interpretation_note = (
                "distance-remapped signalized proxy crosswalk"
                if tls_valid
                else "distance-remapped unsignalized proxy crosswalk"
            )
            return {
                "registry_status": "distance_mapped",
                "implementation_status": "auto_distance_radius_remap",
                "recovery_status": "distance_remapped",
                "recovery_source": "registry_distance_remap",
                "cw_metadata": metadata,
                "ped_val": ped_val,
                "tls_id": tls_id,
                "match_distance_m": dist_m,
                "matched_radius_m": float(radius),
                "distance_bucket": _distance_bucket_for_radius(radius),
                "mapping_confidence": _mapping_confidence(radius),
                "control_mode": control_mode,
                "runnable_for_signal_extension": tls_valid,
                "runnable_for_unsignalized_sim": True,
                "requires_patch": False,
                "interpretation_note": interpretation_note,
                "original_failure_reason": "distance_exceeded",
                "override_reason": "distance_radius_remap",
                "notes": "distance_exceeded",
            }
    return None


def _candidate_registry_recovery_dirs(output_dir_arg: str | Path | None) -> list[Path]:
    explicit = _as_str(output_dir_arg)
    candidates: list[Path] = []
    if explicit:
        candidates.append(Path(explicit))
    default_rel = Path("result") / "integrated_implementation_audit" / "outputs"
    for path in [
        BASE_DIR.parent / default_rel,
        Path.cwd() / default_rel,
        BASE_DIR / default_rel,
    ]:
        if path not in candidates:
            candidates.append(path)
    return candidates


def _load_registry_recovered_candidates(output_dir_arg: str | Path | None) -> tuple[dict[str, dict[str, Any]], str, str]:
    attempted: list[str] = []
    for candidate in _candidate_registry_recovery_dirs(output_dir_arg):
        attempted.append(str(candidate))
        rows, source_path = load_recovered_candidates(candidate)
        if rows:
            return rows, source_path, str(candidate)
    return {}, "", " | ".join(attempted)


def build_registry(args: argparse.Namespace) -> dict[str, Path]:
    nets_dir = Path(args.nets_dir)
    integrated_dir = nets_dir / INTEGRATED_DIRNAME
    integrated_dir.mkdir(parents=True, exist_ok=True)

    features = load_crosswalk_features(args.t2)
    features["inside_junggu_boundary"] = features.apply(
        lambda row: point_in_geojson(float(row["longitude"]), float(row["latitude"]), args.admin_polygon_path),
        axis=1,
    )
    junggu = features[features["inside_junggu_boundary"]].copy().reset_index(drop=True)
    if junggu.empty:
        raise ValueError("중구 내부 후보가 없어 registry를 생성할 수 없습니다.")

    mean_lat = float(junggu["latitude"].mean())
    mean_lon = float(junggu["longitude"].mean())
    build_network(
        INTEGRATED_DIRNAME,
        mean_lat,
        mean_lon,
        integrated_dir,
        force=bool(args.force_networks),
        network_mode=args.network_mode,
        admin_polygon_path=str(args.admin_polygon_path),
        buffer_m=float(args.buffer_m),
        corridor_whitelist=args.corridor_road_whitelist,
    )

    fingerprint = _load_fingerprint(integrated_dir)
    canonical = _canonical_network_version(
        network_mode=args.network_mode,
        buffer_m=float(args.buffer_m),
        network_net_sha256=fingerprint["network_net_sha256"],
        netconvert_options_sha256=fingerprint["netconvert_options_sha256"],
    )

    net_file = fingerprint["net_file"]
    net = read_net(net_file)
    xml_root = ET.parse(net_file).getroot()
    tl_logic_ids, connection_tl_ids = _read_tls_topology(net_file)
    ghost_tls_ids = _collect_ghost_tls(integrated_dir / "netconvert.log")

    recovered_candidates, recovered_source_path, recovered_probe = _load_registry_recovered_candidates(args.output_dir)
    if recovered_source_path:
        print(f"[registry] recovered candidates loaded: {len(recovered_candidates)} from {recovered_source_path}")
    else:
        print(f"[registry] recovered candidates not found. attempted: {recovered_probe}")

    rows: list[dict[str, Any]] = []
    validated_at = datetime.now().isoformat(timespec="seconds")
    for feature_row in junggu.itertuples(index=False):
        cw_id = _as_str(getattr(feature_row, "crosswalk_id"))
        assessment = assess_candidate_implementation(
            net,
            net_file,
            feature_row,
            xml_root,
            float(args.max_match_distance_m),
            True,
        )

        recovered_row = recovered_candidates.get(cw_id)
        if recovered_row:
            assessment, _, _ = apply_recovered_candidate_override(
                assessment=assessment,
                recovered_row=recovered_row,
                net=net,
                tl_logic_ids=tl_logic_ids,
                connection_tl_ids=connection_tl_ids,
            )

        cw_metadata = dict(assessment.get("cw_metadata") or {})
        ped_val = dict(assessment.get("ped_val") or {})
        crossing_edge = _as_str(cw_metadata.get("crossing_edge") or assessment.get("crossing_edge"))
        tls_id = _as_str(assessment.get("tls_id") or cw_metadata.get("tls_id"))
        recovery_status = _as_str(assessment.get("recovery_status"))
        is_ghost_tls = bool(tls_id and tls_id in ghost_tls_ids)
        c_subtype = _classify_subtype(assessment, tls_id, is_ghost_tls)
        registry_status = _registry_status(assessment, c_subtype, recovery_status)

        ped_route = cw_metadata.get("ped_route") or {}
        from_edge = _as_str(ped_route.get("from_edge") or ped_val.get("from_edge"))
        to_edge = _as_str(ped_route.get("to_edge") or ped_val.get("to_edge"))
        implementation_status = _as_str(assessment.get("implementation_status"))
        recovery_source = _as_str(assessment.get("recovery_source"))
        match_distance_m = _to_float(assessment.get("initial_match_distance_m"))
        matched_radius_m = _to_float(assessment.get("matched_radius_m"))

        distance_bucket = ""
        mapping_confidence = ""
        control_mode = ""
        runnable_for_signal_extension = False
        runnable_for_unsignalized_sim = False
        requires_patch = False
        interpretation_note = ""

        failure_reason = _as_str(assessment.get("failure_reason"))
        original_failure_reason = _as_str(assessment.get("original_failure_reason") or failure_reason)
        override_reason = _as_str(assessment.get("override_reason"))

        if registry_status in {"A", "recovered"}:
            control_mode = "signalized"
            runnable_for_signal_extension = True
            runnable_for_unsignalized_sim = True
            requires_patch = False
            mapping_confidence = "verified"

        if c_subtype == "C1_distance_match_failed" and registry_status == "manual_review":
            c1_remap = _attempt_c1_distance_remap(
                net=net,
                net_file=net_file,
                xml_root=xml_root,
                feature_row=feature_row,
                cw_id=cw_id,
                threshold_m=float(args.max_match_distance_m),
                max_radius_m=float(args.c1_max_radius_m),
                tl_logic_ids=tl_logic_ids,
                connection_tl_ids=connection_tl_ids,
            )
            if c1_remap:
                cw_metadata = dict(c1_remap.get("cw_metadata") or {})
                ped_val = dict(c1_remap.get("ped_val") or {})
                crossing_edge = _as_str(cw_metadata.get("crossing_edge"))
                tls_id = _as_str(c1_remap.get("tls_id") or cw_metadata.get("tls_id"))
                is_ghost_tls = bool(tls_id and tls_id in ghost_tls_ids)
                ped_route = cw_metadata.get("ped_route") or {}
                from_edge = _as_str(ped_route.get("from_edge") or ped_val.get("from_edge"))
                to_edge = _as_str(ped_route.get("to_edge") or ped_val.get("to_edge"))
                registry_status = _as_str(c1_remap.get("registry_status"))
                implementation_status = _as_str(c1_remap.get("implementation_status"))
                recovery_status = _as_str(c1_remap.get("recovery_status"))
                recovery_source = _as_str(c1_remap.get("recovery_source"))
                match_distance_m = _to_float(c1_remap.get("match_distance_m"))
                matched_radius_m = _to_float(c1_remap.get("matched_radius_m"))
                distance_bucket = _as_str(c1_remap.get("distance_bucket"))
                mapping_confidence = _as_str(c1_remap.get("mapping_confidence"))
                control_mode = _as_str(c1_remap.get("control_mode"))
                runnable_for_signal_extension = _as_bool(c1_remap.get("runnable_for_signal_extension"))
                runnable_for_unsignalized_sim = _as_bool(c1_remap.get("runnable_for_unsignalized_sim"))
                requires_patch = _as_bool(c1_remap.get("requires_patch"))
                interpretation_note = _as_str(c1_remap.get("interpretation_note"))
                original_failure_reason = _as_str(c1_remap.get("original_failure_reason")) or original_failure_reason
                override_reason = _as_str(c1_remap.get("override_reason"))
                failure_reason = _as_str(c1_remap.get("notes")) or failure_reason

        if c_subtype == "C2_no_tllogic" and registry_status == "manual_review":
            c2_valid = bool(crossing_edge and from_edge and to_edge and ped_val.get("path_uses_crossing", False))
            if c2_valid:
                registry_status = "unsignalized"
                implementation_status = "unsignalized_assumed_no_tllogic"
                control_mode = "unsignalized"
                runnable_for_signal_extension = False
                runnable_for_unsignalized_sim = True
                requires_patch = False
                interpretation_note = "No controllable tlLogic; modeled as unsignalized crosswalk"
                mapping_confidence = mapping_confidence or "assumed"

        if c_subtype == "C3_ghost_tls" and registry_status in {"manual_review", "ghost"}:
            registry_status = "unsignalized_or_ghost"
            implementation_status = "unsignalized_assumed_ghost_tls"
            control_mode = "unsignalized"
            runnable_for_signal_extension = False
            runnable_for_unsignalized_sim = True
            requires_patch = False
            interpretation_note = "Ghost or non-controllable TLS; modeled as unsignalized crosswalk"
            mapping_confidence = mapping_confidence or "assumed"

        if c_subtype == "C6_ped_route_failed":
            registry_status = "route_patch_required"
            implementation_status = "route_patch_required"
            control_mode = "patch_required"
            runnable_for_signal_extension = False
            runnable_for_unsignalized_sim = False
            requires_patch = True
            interpretation_note = ""
            mapping_confidence = mapping_confidence or "blocked"

        if original_failure_reason:
            notes = original_failure_reason
        elif override_reason:
            notes = override_reason
        else:
            notes = failure_reason

        rows.append(
            {
                "canonical_network_version": canonical,
                "crosswalk_id": cw_id,
                "crossing_edge": crossing_edge,
                "tls_id": tls_id,
                "ped_link_indices": _pipe_join(cw_metadata.get("ped_link_indices") or []),
                "from_edge": from_edge,
                "to_edge": to_edge,
                "vehicle_conflict_edges": _pipe_join(cw_metadata.get("vehicle_conflict_edges") or []),
                "approach_lanes": _pipe_join(cw_metadata.get("approach_lanes") or []),
                "match_distance_m": match_distance_m,
                "path_uses_crossing": bool(ped_val.get("path_uses_crossing", False)),
                "path_uses_walkingarea": bool(ped_val.get("path_uses_walkingarea", False)),
                "tlLogic_exists_in_net_xml": bool(tls_id and tls_id in tl_logic_ids),
                "exists_in_connection_tl": bool(tls_id and tls_id in connection_tl_ids),
                "is_netconvert_ghost_tls": is_ghost_tls,
                "registry_status": registry_status,
                "implementation_status": implementation_status,
                "c_subtype": c_subtype,
                "recovery_status": recovery_status,
                "recovery_source": recovery_source,
                "network_net_sha256": fingerprint["network_net_sha256"],
                "netconvert_options_sha256": fingerprint["netconvert_options_sha256"],
                "sumo_version": fingerprint["sumo_version"],
                "netconvert_version": fingerprint["netconvert_version"],
                "validated_at": validated_at,
                "validated_by": _as_str(args.validated_by) or "registry_build_cli",
                "crossing_lon": _to_float(cw_metadata.get("crossing_lon")),
                "crossing_lat": _to_float(cw_metadata.get("crossing_lat")),
                "matched_radius_m": matched_radius_m,
                "distance_bucket": distance_bucket,
                "mapping_confidence": mapping_confidence,
                "control_mode": control_mode,
                "runnable_for_signal_extension": bool(runnable_for_signal_extension),
                "runnable_for_unsignalized_sim": bool(runnable_for_unsignalized_sim),
                "requires_patch": bool(requires_patch),
                "interpretation_note": interpretation_note,
                "original_failure_reason": original_failure_reason,
                "override_reason": override_reason,
                "notes": notes,
            }
        )

    fresh_df = pd.DataFrame(rows)
    registry_path = Path(args.registry_path)
    registry_path.parent.mkdir(parents=True, exist_ok=True)
    existing_df = pd.read_csv(registry_path) if registry_path.exists() else pd.DataFrame(columns=REGISTRY_COLUMNS)
    merged = _upsert_registry_frame(existing_df, fresh_df)
    write_csv_utf8_sig(merged, registry_path)

    admin_polygon_path = Path(args.admin_polygon_path)
    admin_polygon_sha = _sha256_file(admin_polygon_path) if admin_polygon_path.exists() else ""
    network_versions_path = Path(args.network_versions_path)
    network_versions_path.parent.mkdir(parents=True, exist_ok=True)
    _upsert_network_versions(
        network_versions_path,
        {
            "canonical_network_version": canonical,
            "network_mode": args.network_mode,
            "buffer_m": float(args.buffer_m),
            "network_net_sha256": fingerprint["network_net_sha256"],
            "netconvert_options_sha256": fingerprint["netconvert_options_sha256"],
            "sumo_version": fingerprint["sumo_version"],
            "netconvert_version": fingerprint["netconvert_version"],
            "admin_polygon_path": str(admin_polygon_path),
            "admin_polygon_sha256": admin_polygon_sha,
            "network_bbox": fingerprint.get("network_bbox", ""),
            "created_at": validated_at,
        },
    )

    print(f"registry rows upserted: {len(fresh_df)}")
    print(f"canonical_network_version: {canonical}")
    print(f"registry_path: {registry_path}")
    print(f"network_versions_path: {network_versions_path}")

    return {
        "registry_path": registry_path,
        "network_versions_path": network_versions_path,
    }


def _safe_get_edge(net: Any, edge_id: str) -> Any | None:
    try:
        return net.getEdge(edge_id)
    except Exception:
        return None


def validate_registry(args: argparse.Namespace) -> int:
    registry_path = Path(args.registry_path)
    if not registry_path.exists():
        raise FileNotFoundError(f"registry 파일이 없습니다: {registry_path}")
    registry_df = pd.read_csv(registry_path)
    if registry_df.empty:
        raise ValueError(f"registry가 비어 있습니다: {registry_path}")

    integrated_dir = Path(args.nets_dir) / INTEGRATED_DIRNAME
    fingerprint = _load_fingerprint(integrated_dir)
    canonical_current = _canonical_network_version(
        network_mode=args.network_mode,
        buffer_m=float(args.buffer_m),
        network_net_sha256=fingerprint["network_net_sha256"],
        netconvert_options_sha256=fingerprint["netconvert_options_sha256"],
    )
    target_version = _as_str(args.canonical_network_version) or canonical_current

    registry_df["canonical_network_version"] = registry_df["canonical_network_version"].astype(str)
    scoped = registry_df[registry_df["canonical_network_version"] == target_version].copy()
    if scoped.empty:
        raise ValueError(f"검증 대상 canonical_network_version이 없습니다: {target_version}")

    net_file = fingerprint["net_file"]
    net = read_net(net_file)
    tl_logic_ids, connection_tl_ids = _read_tls_topology(net_file)
    ghost_tls_ids = _collect_ghost_tls(integrated_dir / "netconvert.log")

    report_rows: list[dict[str, Any]] = []
    failures = 0

    for row in scoped.to_dict(orient="records"):
        cw_id = _as_str(row.get("crosswalk_id"))
        registry_status = _as_str(row.get("registry_status"))
        tls_id = _as_str(row.get("tls_id"))
        crossing_edge = _as_str(row.get("crossing_edge"))
        from_edge = _as_str(row.get("from_edge"))
        to_edge = _as_str(row.get("to_edge"))

        net_sha_ok = _as_str(row.get("network_net_sha256")) == fingerprint["network_net_sha256"]
        opt_sha_ok = _as_str(row.get("netconvert_options_sha256")) == fingerprint["netconvert_options_sha256"]

        edge_obj = _safe_get_edge(net, crossing_edge) if crossing_edge else None
        crossing_exists = edge_obj is not None
        crossing_is_crossing = bool(crossing_exists and edge_obj.getFunction() == "crossing")

        tl_logic_exists = bool(tls_id and tls_id in tl_logic_ids)
        conn_tl_exists = bool(tls_id and tls_id in connection_tl_ids)
        is_ghost = bool(tls_id and tls_id in ghost_tls_ids)

        ped_val = validate_pedestrian_connectivity(
            net_file,
            {
                "crossing_edge": crossing_edge,
                "ped_route": {
                    "from_edge": from_edge,
                    "to_edge": to_edge,
                },
            },
            cw_id=cw_id,
            net=net,
        )
        path_uses_crossing = _as_bool(ped_val.get("path_uses_crossing", False))
        path_uses_walkingarea = _as_bool(ped_val.get("path_uses_walkingarea", False))

        base_checks = [net_sha_ok, opt_sha_ok]
        runnable_signal_ext = _as_bool(row.get("runnable_for_signal_extension"))
        if registry_status in {"A", "recovered"}:
            required_checks = base_checks + [
                crossing_exists,
                crossing_is_crossing,
                path_uses_crossing,
                path_uses_walkingarea,
                tl_logic_exists,
                conn_tl_exists,
                not is_ghost,
            ]
        elif registry_status == "distance_mapped":
            required_checks = base_checks + [
                crossing_exists,
                crossing_is_crossing,
                path_uses_crossing,
                path_uses_walkingarea,
            ]
            if runnable_signal_ext:
                required_checks += [tl_logic_exists, conn_tl_exists, not is_ghost]
        elif registry_status in {"unsignalized", "unsignalized_or_ghost"}:
            required_checks = base_checks + [
                crossing_exists,
                crossing_is_crossing,
                path_uses_crossing,
            ]
        elif registry_status == "route_patch_required":
            required_checks = base_checks
        elif registry_status == "C4":
            required_checks = base_checks
        elif registry_status == "ghost":
            required_checks = base_checks + [is_ghost or not tl_logic_exists]
        else:
            required_checks = base_checks

        passed = all(required_checks)
        if not passed:
            failures += 1

        report_rows.append(
            {
                "canonical_network_version": target_version,
                "crosswalk_id": cw_id,
                "registry_status": registry_status,
                "net_sha_match": net_sha_ok,
                "option_sha_match": opt_sha_ok,
                "crossing_edge_exists": crossing_exists,
                "crossing_edge_is_crossing": crossing_is_crossing,
                "tlLogic_exists_in_net_xml": tl_logic_exists,
                "exists_in_connection_tl": conn_tl_exists,
                "is_netconvert_ghost_tls": is_ghost,
                "path_uses_crossing": path_uses_crossing,
                "path_uses_walkingarea": path_uses_walkingarea,
                "validation_status": _as_str(ped_val.get("validation_status")),
                "invalid_reason": _as_str(ped_val.get("invalid_reason")),
                "validated_at": datetime.now().isoformat(timespec="seconds"),
                "passed": passed,
            }
        )

    report_df = pd.DataFrame(report_rows)
    report_path = Path(args.report_path) if _as_str(args.report_path) else registry_path.parent / "registry_validation_report.csv"
    write_csv_utf8_sig(report_df, report_path)
    print(f"validation report: {report_path}")
    print(f"rows: {len(report_df)} failed: {failures}")
    if failures:
        print("registry validation failed. `python -m smart_crosswalk_sumo.crosswalk_registry build` or `validate`를 재실행하세요.")
    return 1 if failures else 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Canonical crosswalk registry build/validate CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    p_build = sub.add_parser("build", help="build or upsert canonical crosswalk registry")
    p_build.add_argument("--t2", default=str(BASE_DIR / "data" / "T2_crosswalk_features.csv"))
    p_build.add_argument("--admin_polygon_path", default=str(DEFAULT_ADMIN_POLYGON_PATH))
    p_build.add_argument("--nets_dir", default=str(BASE_DIR / "sumo_nets"))
    p_build.add_argument("--network_mode", choices=["local", "expanded", "local_radius", "jungu_admin_buffer", "corridor_network"], default="expanded")
    p_build.add_argument("--buffer_m", type=float, default=1000.0)
    p_build.add_argument("--corridor_road_whitelist", nargs="*", default=None)
    p_build.add_argument("--registry_path", default=str(DEFAULT_REGISTRY_PATH))
    p_build.add_argument("--network_versions_path", default=str(DEFAULT_NETWORK_VERSIONS_PATH))
    p_build.add_argument("--max_match_distance_m", type=float, default=MATCH_DISTANCE_THRESHOLD_M)
    p_build.add_argument("--c1_max_radius_m", type=float, default=100.0)
    p_build.add_argument("--validated_by", default="registry_build_cli")
    p_build.add_argument("--output_dir", default="")
    p_build.add_argument("--force_networks", action="store_true")

    p_validate = sub.add_parser("validate", help="validate canonical crosswalk registry against current integrated network")
    p_validate.add_argument("--registry_path", default=str(DEFAULT_REGISTRY_PATH))
    p_validate.add_argument("--nets_dir", default=str(BASE_DIR / "sumo_nets"))
    p_validate.add_argument("--canonical_network_version", default=None)
    p_validate.add_argument("--network_mode", choices=["local", "expanded", "local_radius", "jungu_admin_buffer", "corridor_network"], default="expanded")
    p_validate.add_argument("--buffer_m", type=float, default=1000.0)
    p_validate.add_argument("--report_path", default=None)

    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.command == "build":
        build_registry(args)
        return
    if args.command == "validate":
        raise SystemExit(validate_registry(args))


if __name__ == "__main__":
    main()
