from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import shutil
import subprocess
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import requests

try:
    from .output_schema import english_output_columns
except ImportError:
    from output_schema import english_output_columns

try:
    from .network_utils import discover_network_metadata, save_metadata, sumo_env
except ImportError:
    from network_utils import discover_network_metadata, save_metadata, sumo_env


DEFAULT_ADMIN_POLYGON_PATH = (
    Path(__file__).resolve().parent / "data" / "junggu_admin_boundary.geojson"
)


def sumo_tool(name: str) -> str:
    path = shutil.which(name)
    if path:
        return path
    raise RuntimeError(f"{name} 실행 파일을 찾지 못했습니다. SUMO 설치와 PATH를 확인하세요.")


UNSUPPORTED_VCLASSES = {"container", "cable_car", "subway", "aircraft", "wheelchair", "scooter", "drone"}
VCLASS_REPLACEMENTS = {
    "wheelchair": "pedestrian",
    "scooter": "bicycle",
}


def netconvert_supported_options() -> set[str]:
    try:
        proc = subprocess.run(
            [sumo_tool("netconvert"), "--help"],
            check=True,
            capture_output=True,
            text=True,
            env=sumo_env(),
        )
        text = proc.stdout + "\n" + proc.stderr
    except Exception:
        return set()
    return set(re.findall(r"(--[a-zA-Z0-9_.-]+)", text))


def filtered_netconvert_options(raw_options: list[str], supported: set[str]) -> tuple[list[str], list[str]]:
    if not supported:
        return raw_options, []
    kept: list[str] = []
    skipped: list[str] = []
    i = 0
    while i < len(raw_options):
        token = raw_options[i]
        if token.startswith("--"):
            if token in supported:
                kept.append(token)
                if i + 1 < len(raw_options) and not raw_options[i + 1].startswith("--"):
                    kept.append(raw_options[i + 1])
                    i += 2
                    continue
            else:
                skipped.append(token)
                if i + 1 < len(raw_options) and not raw_options[i + 1].startswith("--"):
                    i += 2
                    continue
        else:
            kept.append(token)
        i += 1
    return kept, skipped


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


def _tool_version(name: str) -> str:
    try:
        proc = subprocess.run(
            [sumo_tool(name), "--version"],
            check=True,
            capture_output=True,
            text=True,
            env=sumo_env(),
        )
    except Exception:
        return ""
    text = (proc.stdout or "").strip() or (proc.stderr or "").strip()
    return text.splitlines()[0] if text else ""


def sanitize_vclass_string(value: str) -> tuple[str, bool]:
    tokens = [tok for tok in value.split() if tok]
    out: list[str] = []
    changed = False
    for tok in tokens:
        if tok in UNSUPPORTED_VCLASSES:
            changed = True
            repl = VCLASS_REPLACEMENTS.get(tok)
            if repl:
                out.append(repl)
            continue
        out.append(tok)
    deduped = []
    seen = set()
    for tok in out:
        if tok in seen:
            changed = True
            continue
        seen.add(tok)
        deduped.append(tok)
    return " ".join(deduped), changed


def sanitize_network_vclasses(net_file: Path) -> dict[str, Any]:
    tree = ET.parse(net_file)
    root = tree.getroot()
    scanned_attrs = 0
    changed_attrs = 0
    removed_counts = {k: 0 for k in sorted(UNSUPPORTED_VCLASSES)}
    replaced_counts = {k: 0 for k in sorted(VCLASS_REPLACEMENTS)}

    for elem in root.iter():
        for attr in ("allow", "disallow", "vClass", "vclasses"):
            raw = elem.attrib.get(attr)
            if not raw:
                continue
            scanned_attrs += 1
            tokens = [tok for tok in raw.split() if tok]
            for tok in tokens:
                if tok in removed_counts:
                    removed_counts[tok] += 1
                if tok in replaced_counts:
                    replaced_counts[tok] += 1
            sanitized, changed = sanitize_vclass_string(raw)
            if changed:
                changed_attrs += 1
                if sanitized:
                    elem.attrib[attr] = sanitized
                else:
                    elem.attrib.pop(attr, None)

    tree.write(net_file, encoding="utf-8", xml_declaration=True)
    return {
        "net_file": str(net_file),
        "scanned_attrs": int(scanned_attrs),
        "changed_attrs": int(changed_attrs),
        "removed_counts": removed_counts,
        "replaced_counts": replaced_counts,
    }


def _lane_allows_pedestrian(lane_elem: ET.Element) -> bool:
    allow = (lane_elem.attrib.get("allow") or "").strip()
    disallow = (lane_elem.attrib.get("disallow") or "").strip()
    if allow:
        return "pedestrian" in set(allow.split())
    if disallow:
        return "pedestrian" not in set(disallow.split())
    return True


def remove_invalid_walkingarea_connections(net_file: Path) -> dict[str, Any]:
    tree = ET.parse(net_file)
    root = tree.getroot()
    edges = {e.attrib.get("id", ""): e for e in root.findall("edge")}
    lanes = {}
    for edge in edges.values():
        eid = edge.attrib.get("id", "")
        for lane in edge.findall("lane"):
            lid = lane.attrib.get("id", "")
            lanes[(eid, lid)] = lane
    removed_rows: list[dict[str, Any]] = []
    to_remove: list[ET.Element] = []
    for conn in root.findall("connection"):
        from_edge = conn.attrib.get("from", "")
        to_edge = conn.attrib.get("to", "")
        from_lane_idx = conn.attrib.get("fromLane", "0")
        to_lane_idx = conn.attrib.get("toLane", "0")
        from_edge_elem = edges.get(from_edge)
        to_edge_elem = edges.get(to_edge)
        if from_edge_elem is None or to_edge_elem is None:
            continue
        from_func = from_edge_elem.attrib.get("function", "normal")
        to_func = to_edge_elem.attrib.get("function", "normal")
        check_to_walking = to_func == "walkingarea"
        check_from_walking = from_func == "walkingarea"
        if not (check_to_walking or check_from_walking):
            continue
        from_lane_id = f"{from_edge}_{from_lane_idx}"
        to_lane_id = f"{to_edge}_{to_lane_idx}"
        from_lane_elem = lanes.get((from_edge, from_lane_id))
        to_lane_elem = lanes.get((to_edge, to_lane_id))
        from_ped = _lane_allows_pedestrian(from_lane_elem) if from_lane_elem is not None else False
        to_ped = _lane_allows_pedestrian(to_lane_elem) if to_lane_elem is not None else True
        remove_reason = None
        if check_to_walking and not from_ped:
            remove_reason = "non_pedestrian_lane_to_walkingarea"
        elif check_from_walking and not to_ped:
            remove_reason = "walkingarea_to_non_pedestrian_lane"
        if remove_reason:
            removed_rows.append(
                {
                    "from_edge": from_edge,
                    "to_edge": to_edge,
                    "from_lane": from_lane_id,
                    "to_lane": to_lane_id,
                    "from_function": from_func,
                    "to_function": to_func,
                    "from_lane_allows_pedestrian": bool(from_ped),
                    "to_lane_allows_pedestrian": bool(to_ped),
                    "reason": remove_reason,
                }
            )
            to_remove.append(conn)
    if to_remove:
        for conn in to_remove:
            root.remove(conn)
        tree.write(net_file, encoding="utf-8", xml_declaration=True)
    return {
        "net_file": str(net_file),
        "removed_count": len(to_remove),
        "removed_connections": removed_rows,
    }


def remove_invalid_tls_references(net_file: Path) -> dict[str, Any]:
    tree = ET.parse(net_file)
    root = tree.getroot()
    valid_tls_ids = {tl.attrib.get("id", "") for tl in root.findall("tlLogic")}
    removed_rows: list[dict[str, Any]] = []
    changed = 0
    for conn in root.findall("connection"):
        tl_id = conn.attrib.get("tl", "")
        if not tl_id or tl_id in valid_tls_ids:
            continue
        removed_rows.append(
            {
                "from_edge": conn.attrib.get("from", ""),
                "to_edge": conn.attrib.get("to", ""),
                "from_lane": conn.attrib.get("fromLane", ""),
                "to_lane": conn.attrib.get("toLane", ""),
                "invalid_tl_id": tl_id,
                "link_index": conn.attrib.get("linkIndex", ""),
            }
        )
        conn.attrib.pop("tl", None)
        conn.attrib.pop("linkIndex", None)
        changed += 1
    if changed:
        tree.write(net_file, encoding="utf-8", xml_declaration=True)
    return {
        "net_file": str(net_file),
        "removed_count": int(changed),
        "removed_tls_references": removed_rows,
    }


def download_osm_bbox(bbox: str, osm_file: Path) -> None:
    endpoints = [
        "https://overpass-api.de/api/map?bbox={bbox}",
        "https://lz4.overpass-api.de/api/map?bbox={bbox}",
        "https://z.overpass-api.de/api/map?bbox={bbox}",
        "https://overpass.kumi.systems/api/map?bbox={bbox}",
    ]
    last_error = None
    for attempt in range(3):
        for endpoint in endpoints:
            try:
                response = requests.get(endpoint.format(bbox=bbox), timeout=90)
                response.raise_for_status()
                if b"<osm" not in response.content[:200]:
                    raise RuntimeError("Overpass 응답이 OSM XML이 아닙니다.")
                osm_file.write_bytes(response.content)
                return
            except Exception as exc:
                last_error = exc
        time.sleep(2 + attempt)
    raise RuntimeError(f"OSM 다운로드 실패: {last_error}")


def bbox_from_radius(lon: float, lat: float, radius_m: float) -> str:
    lat_margin = float(radius_m) / 111_320.0
    lon_margin = float(radius_m) / (111_320.0 * max(math.cos(math.radians(float(lat))), 0.01))
    return f"{lon - lon_margin},{lat - lat_margin},{lon + lon_margin},{lat + lat_margin}"


def load_polygon_bbox_with_buffer(polygon_path: str | Path, buffer_m: float) -> tuple[float, float, float, float]:
    path = Path(polygon_path)
    if not path.exists():
        raise FileNotFoundError(f"행정경계 파일이 없습니다: {path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    # GeoJSON expected: features[0].geometry.coordinates
    features = data.get("features")
    if features is None or len(features) == 0:
        raise ValueError("유효한 GeoJSON features가 없습니다.")
    geometry = features[0].get("geometry", {})
    coords = geometry.get("coordinates", [])
    if geometry.get("type") == "Polygon":
        rings = coords
    elif geometry.get("type") == "MultiPolygon":
        rings = [ring for poly in coords for ring in poly]
    else:
        raise ValueError(f"지원하지 않는 geometry type: {geometry.get('type')}")
    lon_vals: list[float] = []
    lat_vals: list[float] = []
    for ring in rings:
        for lon, lat in ring:
            lon_vals.append(float(lon))
            lat_vals.append(float(lat))
    if not lon_vals or not lat_vals:
        raise ValueError("행정경계 좌표를 읽지 못했습니다.")
    center_lat = (min(lat_vals) + max(lat_vals)) / 2.0
    lat_margin = float(buffer_m) / 111_320.0
    lon_margin = float(buffer_m) / (111_320.0 * max(math.cos(math.radians(center_lat)), 0.01))
    return (
        min(lon_vals) - lon_margin,
        min(lat_vals) - lat_margin,
        max(lon_vals) + lon_margin,
        max(lat_vals) + lat_margin,
    )


def resolve_admin_polygon_path(
    network_mode: str,
    admin_polygon_path: str | Path | None,
) -> str | None:
    normalized_mode = normalize_network_mode(network_mode)
    if normalized_mode != "expanded":
        return str(admin_polygon_path) if admin_polygon_path else None
    if admin_polygon_path:
        return str(admin_polygon_path)
    if DEFAULT_ADMIN_POLYGON_PATH.exists():
        return str(DEFAULT_ADMIN_POLYGON_PATH)
    raise FileNotFoundError(
        "중구 행정경계 GeoJSON이 없습니다. "
        f"--admin_polygon_path를 지정하거나 기본 파일을 생성하세요: {DEFAULT_ADMIN_POLYGON_PATH}"
    )


def normalize_network_mode(network_mode: str) -> str:
    aliases = {
        "local": "local",
        "local_radius": "local",
        "expanded": "expanded",
        "jungu_admin_buffer": "expanded",
        "corridor_network": "expanded",
    }
    if network_mode not in aliases:
        raise ValueError(f"지원하지 않는 network_mode입니다: {network_mode}")
    return aliases[network_mode]


def corridor_boundary_warning(
    osm_file: Path,
    bbox: tuple[float, float, float, float],
    corridor_whitelist: list[str] | None,
) -> list[dict[str, Any]]:
    if not corridor_whitelist:
        return []
    warnings: list[dict[str, Any]] = []
    if not osm_file.exists():
        return warnings
    try:
        root = ET.parse(osm_file).getroot()
    except Exception:
        return warnings

    node_coords: dict[str, tuple[float, float]] = {}
    for node in root.findall("node"):
        node_coords[node.attrib.get("id", "")] = (
            float(node.attrib.get("lon", "0")),
            float(node.attrib.get("lat", "0")),
        )

    xmin, ymin, xmax, ymax = bbox
    tol_lon = (xmax - xmin) * 0.01
    tol_lat = (ymax - ymin) * 0.01

    for way in root.findall("way"):
        tags = {tag.attrib.get("k"): tag.attrib.get("v") for tag in way.findall("tag")}
        name = tags.get("name", "")
        if name not in corridor_whitelist:
            continue
        near_boundary = False
        for nd in way.findall("nd"):
            nid = nd.attrib.get("ref", "")
            if nid not in node_coords:
                continue
            lon, lat = node_coords[nid]
            if (
                abs(lon - xmin) <= tol_lon
                or abs(lon - xmax) <= tol_lon
                or abs(lat - ymin) <= tol_lat
                or abs(lat - ymax) <= tol_lat
            ):
                near_boundary = True
                break
        if near_boundary:
            warnings.append(
                {
                    "warning_type": "corridor_edge_cut",
                    "road_name": name,
                    "message": "주요 corridor가 bbox 경계에서 잘릴 가능성이 있습니다.",
                }
            )
    return warnings


def resolve_network_bbox(
    lon: float,
    lat: float,
    network_mode: str,
    bbox_margin: float,
    network_radius_m: float | None,
    admin_polygon_path: str | None,
    buffer_m: float,
) -> tuple[str, tuple[float, float, float, float]]:
    normalized_mode = normalize_network_mode(network_mode)
    admin_polygon_path = resolve_admin_polygon_path(normalized_mode, admin_polygon_path)
    if normalized_mode == "expanded":
        xmin, ymin, xmax, ymax = load_polygon_bbox_with_buffer(admin_polygon_path, buffer_m)
        return f"{xmin},{ymin},{xmax},{ymax}", (xmin, ymin, xmax, ymax)

    if network_radius_m:
        bbox = bbox_from_radius(lon, lat, network_radius_m)
        xmin, ymin, xmax, ymax = [float(x) for x in bbox.split(",")]
        return bbox, (xmin, ymin, xmax, ymax)

    xmin = lon - bbox_margin
    ymin = lat - bbox_margin
    xmax = lon + bbox_margin
    ymax = lat + bbox_margin
    return f"{xmin},{ymin},{xmax},{ymax}", (xmin, ymin, xmax, ymax)


def build_network(
    cw_id: str | int,
    lat: float,
    lon: float,
    output_dir: str | Path,
    bbox_margin: float = 0.002,
    network_radius_m: float | None = None,
    force: bool = False,
    network_mode: str = "expanded",
    admin_polygon_path: str | None = None,
    buffer_m: float = 1000.0,
    corridor_whitelist: list[str] | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    osm_file = output_dir / "map.osm"
    net_file = output_dir / "network.net.xml"
    metadata_file = output_dir / "metadata.json"
    provenance_file = output_dir / "network_build_provenance.json"

    normalized_mode = normalize_network_mode(network_mode)
    bbox, bbox_tuple = resolve_network_bbox(
        lon,
        lat,
        normalized_mode,
        bbox_margin,
        network_radius_m,
        admin_polygon_path,
        buffer_m,
    )

    warnings = corridor_boundary_warning(osm_file, bbox_tuple, corridor_whitelist)
    if force or not osm_file.exists():
        try:
            download_osm_bbox(bbox, osm_file)
        except Exception:
            if osm_file.exists() and osm_file.stat().st_size > 0:
                warnings.append(
                    {
                        "warning_type": "overpass_cache_fallback",
                        "road_name": "",
                        "message": "OSM 재다운로드 실패로 기존 캐시 map.osm을 재사용합니다.",
                    }
                )
            else:
                raise

    sanitize_report: dict[str, Any] | None = None
    connection_sanitize_report: dict[str, Any] | None = None
    tls_sanitize_report: dict[str, Any] | None = None
    supported = netconvert_supported_options()
    desired_options = [
        "--geometry.remove",
        "--roundabouts.guess",
        "--ramps.guess",
        # "--junctions.join", # 👈 너무 공격적인 병합으로 인해 신호등/횡단보도가 사라지는 것 방지
        "--tls.guess",
        "--tls.guess-signals",
        "--tls.discard-simple",
        "--tls.join",
        "--tls.default-type",
        "actuated",
        "--osm.lane-access",
        "--osm.turn-lanes",
        "--osm.sidewalks",
        "--osm.all-attributes",
        # --crossings.guess는 OSM 데이터 상의 교차로 등에 횡단보도를 유추해서 생성해주는 옵션이지만,
        # 사용자가 원하는 T2 좌표에 정확히 횡단보도를 만들어준다는 보장은 없습니다.
        # 따라서 매칭 단계에서 반드시 50m 이내 매칭 여부를 검증해야 합니다.
        "--crossings.guess",
        "--crossings.guess.all",
        "--crossings.guess.speed-threshold",
        "-1",
        "--walkingareas",
        "--no-turnarounds.except-deadend",
        "--sidewalks.guess",
        "--sidewalks.guess.from-permissions",
        "--sidewalks.guess.max-speed",
        "13.89",
        "--keep-edges.by-vclass",
        "passenger,pedestrian",
        "--verbose",
    ]
    filtered_options, skipped_options = filtered_netconvert_options(desired_options, supported)
    if skipped_options:
        warnings.append(
            {
                "warning_type": "netconvert_option_fallback",
                "road_name": "",
                "message": f"현재 SUMO 버전에서 미지원 옵션 제외: {', '.join(skipped_options)}",
            }
        )

    if force or not net_file.exists():
        with (output_dir / "netconvert.log").open("w", encoding="utf-8") as log_file:
            subprocess.run(
                [
                    sumo_tool("netconvert"),
                    "--osm-files",
                    str(osm_file),
                    "--output-file",
                    str(net_file),
                    *filtered_options,
                ],
                check=True,
                env=sumo_env(),
                stdout=log_file,
                stderr=subprocess.STDOUT,
            )
        if net_file.exists():
            sanitize_report = sanitize_network_vclasses(net_file)
            connection_sanitize_report = remove_invalid_walkingarea_connections(net_file)
            tls_sanitize_report = remove_invalid_tls_references(net_file)
            (output_dir / "network_vclass_sanitize_report.json").write_text(
                json.dumps(sanitize_report, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            (output_dir / "network_vclass_sanitize_report.md").write_text(
                "\n".join(
                    [
                        "# Network vClass Sanitize Report",
                        "",
                        f"- net_file: `{sanitize_report['net_file']}`",
                        f"- scanned_attrs: {sanitize_report['scanned_attrs']}",
                        f"- changed_attrs: {sanitize_report['changed_attrs']}",
                        "",
                        "## Removed Counts",
                        json.dumps(sanitize_report["removed_counts"], ensure_ascii=False, indent=2),
                        "",
                        "## Replaced Counts",
                        json.dumps(sanitize_report["replaced_counts"], ensure_ascii=False, indent=2),
                    ]
                ),
                encoding="utf-8",
            )
            (output_dir / "invalid_walkingarea_connections.json").write_text(
                json.dumps(connection_sanitize_report, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            (output_dir / "invalid_tls_references.json").write_text(
                json.dumps(tls_sanitize_report, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            if connection_sanitize_report and connection_sanitize_report.get("removed_count", 0):
                pd.DataFrame(connection_sanitize_report.get("removed_connections", [])).to_csv(
                    output_dir / "invalid_walkingarea_connections.csv",
                    index=False,
                )
            if tls_sanitize_report and tls_sanitize_report.get("removed_count", 0):
                pd.DataFrame(tls_sanitize_report.get("removed_tls_references", [])).to_csv(
                    output_dir / "invalid_tls_references.csv",
                    index=False,
                )

    metadata = discover_network_metadata(net_file, lon=lon, lat=lat, cw_id=cw_id)
    metadata["network_mode"] = normalized_mode
    metadata["network_bbox"] = bbox
    metadata["buffer_m"] = float(buffer_m)
    save_metadata(metadata_file, metadata)
    net_sha = _sha256_file(net_file)
    filtered_hash = _sha256_text(
        json.dumps(filtered_options, ensure_ascii=False, separators=(",", ":"))
    )
    netconvert_version = _tool_version("netconvert")
    sumo_version = _tool_version("sumo")
    option_fingerprint_payload = {
        "filtered_options": filtered_options,
        "skipped_options": skipped_options,
        "netconvert_version": netconvert_version,
    }
    option_fingerprint_sha = _sha256_text(
        json.dumps(option_fingerprint_payload, ensure_ascii=False, separators=(",", ":"))
    )
    provenance = {
        "cw_id": str(cw_id),
        "network_mode": normalized_mode,
        "buffer_m": float(buffer_m),
        "network_bbox": bbox,
        "network_net_sha256": net_sha,
        "netconvert_options_sha256": filtered_hash,
        "netconvert_option_fingerprint_sha256": option_fingerprint_sha,
        "filtered_netconvert_options": filtered_options,
        "skipped_netconvert_options": skipped_options,
        "netconvert_version": netconvert_version,
        "sumo_version": sumo_version,
    }
    provenance_file.write_text(
        json.dumps(provenance, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return metadata, warnings


def build_all_networks(
    candidates_csv: str | Path,
    nets_dir: str | Path = "sumo_nets",
    output_dir: str | Path = "outputs",
    force: bool = False,
    network_radius_m: float | None = None,
    network_mode: str = "expanded",
    admin_polygon_path: str | None = None,
    buffer_m: float = 1000.0,
    corridor_whitelist: list[str] | None = None,
) -> pd.DataFrame:
    candidates = pd.read_csv(candidates_csv)
    nets_dir = Path(nets_dir)
    nets_dir.mkdir(parents=True, exist_ok=True)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    failures = []
    network_warnings: list[dict[str, Any]] = []
    lane_updates: list[dict[str, Any]] = []
    invalid_walk_conn_rows: list[dict[str, Any]] = []

    for row in candidates.itertuples(index=False):
        cw_id = getattr(row, "crosswalk_id")
        cw_dir = nets_dir / f"cw_{cw_id}"
        try:
            print(f"[net] building cw_{cw_id}", flush=True)
            metadata, warnings = build_network(
                cw_id,
                float(getattr(row, "latitude")),
                float(getattr(row, "longitude")),
                cw_dir,
                network_radius_m=network_radius_m,
                force=force,
                network_mode=network_mode,
                admin_polygon_path=admin_polygon_path,
                buffer_m=buffer_m,
                corridor_whitelist=corridor_whitelist,
            )
            approach_lanes = [str(x) for x in metadata.get("approach_lanes", [])]
            by_edge: dict[str, int] = {}
            for lane_id in approach_lanes:
                if "_" not in lane_id:
                    continue
                edge_id = lane_id.rsplit("_", 1)[0]
                by_edge[edge_id] = by_edge.get(edge_id, 0) + 1
            inferred_lane_count = int(np.median(list(by_edge.values()))) if by_edge else np.nan
            observed_lane_count = pd.to_numeric(pd.Series([getattr(row, "lane_count", np.nan)]), errors="coerce").iloc[0]
            has_imputed_values = bool(getattr(row, "has_imputed_values", False))
            if (not has_imputed_values) and pd.notna(observed_lane_count) and float(observed_lane_count) > 0:
                effective_lane_count = float(observed_lane_count)
                lane_source = "t2_observed"
            elif pd.notna(inferred_lane_count) and int(inferred_lane_count) > 0:
                effective_lane_count = float(int(inferred_lane_count))
                lane_source = "network_inferred"
            else:
                effective_lane_count = 1.0
                lane_source = "fallback_default"
            metadata["lane_count_csv"] = float(effective_lane_count)
            metadata["lane_count_source"] = lane_source
            metadata["lane_count_inferred"] = (
                float(inferred_lane_count) if pd.notna(inferred_lane_count) else None
            )
            save_metadata(cw_dir / "metadata.json", metadata)
            lane_updates.append(
                {
                    "crosswalk_id": str(cw_id),
                    "lane_count_input": float(observed_lane_count) if pd.notna(observed_lane_count) else np.nan,
                    "lane_count_inferred": float(inferred_lane_count) if pd.notna(inferred_lane_count) else np.nan,
                    "lane_count": float(effective_lane_count),
                    "lane_count_source": lane_source,
                }
            )
            for warning in warnings:
                network_warnings.append({"crosswalk_id": cw_id, **warning})
            per_cw_invalid = cw_dir / "invalid_walkingarea_connections.csv"
            if per_cw_invalid.exists():
                cw_invalid_df = pd.read_csv(per_cw_invalid)
                if not cw_invalid_df.empty:
                    cw_invalid_df.insert(0, "crosswalk_id", str(cw_id))
                    invalid_walk_conn_rows.extend(cw_invalid_df.to_dict(orient="records"))
        except Exception as exc:
            failures.append({"crosswalk_id": cw_id, "step": "build_network", "error": str(exc)})

    failure_df = pd.DataFrame(failures)
    if failures:
        failed_path = output_dir / "failed_cases.csv"
        failure_df.to_csv(
            failed_path,
            mode="a",
            header=not failed_path.exists(),
            index=False,
        )
    if network_warnings:
        english_output_columns(pd.DataFrame(network_warnings)).to_csv(
            output_dir / "network_mode_warnings.csv",
            index=False,
        )
    if lane_updates and Path(candidates_csv).exists():
        updates_df = pd.DataFrame(lane_updates)
        cand_df = pd.read_csv(candidates_csv)
        cand_df["_cw_key"] = cand_df["crosswalk_id"].astype(str)
        merged = cand_df.merge(
            updates_df.rename(columns={"crosswalk_id": "_cw_key"}),
            on="_cw_key",
            how="left",
        )
        for col in ("lane_count_input", "lane_count_inferred", "lane_count_source"):
            if col not in merged.columns:
                merged[col] = np.nan
        if "lane_count_y" in merged.columns:
            merged["lane_count"] = merged["lane_count_y"].fillna(merged.get("lane_count_x"))
            merged = merged.drop(columns=[c for c in ("lane_count_x", "lane_count_y") if c in merged.columns])
        merged = merged.drop(columns=["_cw_key"])
        merged.to_csv(candidates_csv, index=False)
        updates_df.to_csv(output_dir / "lane_count_updates.csv", index=False)
    if invalid_walk_conn_rows:
        pd.DataFrame(invalid_walk_conn_rows).to_csv(
            output_dir / "invalid_walkingarea_connections.csv",
            index=False,
        )
    return failure_df


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--candidates", default="outputs/candidates.csv")
    parser.add_argument("--nets_dir", default="sumo_nets")
    parser.add_argument("--output_dir", default="outputs")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--network_radius_m", type=float, default=None)
    parser.add_argument(
        "--network_mode",
        choices=["local", "expanded", "local_radius", "jungu_admin_buffer", "corridor_network"],
        default="expanded",
    )
    parser.add_argument("--admin_polygon_path", default=None)
    parser.add_argument("--buffer_m", type=float, default=1000.0)
    parser.add_argument("--corridor_road_whitelist", nargs="*", default=None)
    args = parser.parse_args()
    build_all_networks(
        args.candidates,
        args.nets_dir,
        args.output_dir,
        args.force,
        args.network_radius_m,
        args.network_mode,
        args.admin_polygon_path,
        args.buffer_m,
        args.corridor_road_whitelist,
    )


if __name__ == "__main__":
    main()
