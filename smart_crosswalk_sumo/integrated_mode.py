from __future__ import annotations

import hashlib
import json
import math
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

try:
    from .build_networks import build_network
    from .generate_demand import (
        generate_vehicle_routes,
        get_demand_params,
        load_traffic_counts,
        observed_vehicle_rate_for_candidate,
        write_sumocfg,
    )
    from .model_config import load_model_parameters
    from .mpl_runtime import configure_matplotlib, ensure_matplotlib_env
    from .network_utils import (
        attempt_route_repair,
        discover_network_metadata,
        discover_network_metadata_from_net,
        distance_to_edge_shape,
        pedestrian_link_indices,
        read_net,
        validate_pedestrian_connectivity,
    )
    from .output_schema import write_csv_utf8_sig
    from .preprocess import load_crosswalk_features
    from .run_simulations import (
        compute_signal_timing,
        generate_incident_schedule,
        run_simulation_integrated,
        _runtime_log_append,
        snapshot_traci_trafficlight_ids,
        serialize_incident_event,
    )
except ImportError:
    from build_networks import build_network
    from generate_demand import (
        generate_vehicle_routes,
        get_demand_params,
        load_traffic_counts,
        observed_vehicle_rate_for_candidate,
        write_sumocfg,
    )
    from model_config import load_model_parameters
    from mpl_runtime import configure_matplotlib, ensure_matplotlib_env
    from network_utils import (
    attempt_route_repair,
        discover_network_metadata,
        discover_network_metadata_from_net,
        distance_to_edge_shape,
        read_net,
        validate_pedestrian_connectivity,
    )
    from output_schema import write_csv_utf8_sig
    from preprocess import load_crosswalk_features
    from run_simulations import (
        compute_signal_timing,
        generate_incident_schedule,
        run_simulation_integrated,
        _runtime_log_append,
        snapshot_traci_trafficlight_ids,
        serialize_incident_event,
    )


INTEGRATED_DIRNAME = "integrated_selected"
MANIFEST_FILENAME = "smart_crosswalk_manifest.json"
MATCH_DISTANCE_THRESHOLD_M = 50.0
REGISTRY_RUNNABLE_STATUSES = {"A", "recovered"}


def _sha256_text(payload: str) -> str:
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _sha256_file(path: str | Path) -> str:
    hasher = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            if not chunk:
                break
            hasher.update(chunk)
    return hasher.hexdigest()


def _canonical_network_version(
    network_mode: str,
    buffer_m: float,
    network_net_sha256: str,
    netconvert_options_sha256: str,
) -> str:
    buffer_token = f"{float(buffer_m):g}".replace(".", "p")
    mode_token = str(network_mode or "expanded").strip().lower()
    return (
        f"junggu_{mode_token}_b{buffer_token}"
        f"_net{str(network_net_sha256)[:12]}_opt{str(netconvert_options_sha256)[:12]}"
    )


def _to_int_list(value: Any) -> list[int]:
    if isinstance(value, list):
        out: list[int] = []
        for item in value:
            try:
                out.append(int(float(item)))
            except Exception:
                continue
        return out
    text = str(value or "").strip()
    if not text:
        return []
    if text.startswith("[") and text.endswith("]"):
        try:
            parsed = json.loads(text)
            return _to_int_list(parsed)
        except Exception:
            pass
    out: list[int] = []
    for token in text.split("|"):
        token = token.strip()
        if not token:
            continue
        try:
            out.append(int(float(token)))
        except Exception:
            continue
    return out


def _to_str_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value if str(item).strip()]
    text = str(value or "").strip()
    if not text:
        return []
    if text.startswith("[") and text.endswith("]"):
        try:
            parsed = json.loads(text)
            return _to_str_list(parsed)
        except Exception:
            pass
    return [tok.strip() for tok in text.split("|") if tok.strip()]


def _load_integrated_network_fingerprint(integrated_dir: Path) -> dict[str, Any]:
    base_net = integrated_dir / "network.net.xml"
    if not base_net.exists():
        raise FileNotFoundError(f"통합 네트워크 파일이 없습니다: {base_net}")
    # SHA256 은 registry 버전 정합성을 위해 항상 network.net.xml 기준으로 유지한다.
    # runtime net_file 은 network_with_signal.net.xml 이 있으면 그것을 우선한다.
    signal_net = integrated_dir / "network_with_signal.net.xml"
    net_file = signal_net if signal_net.exists() else base_net
    provenance_path = integrated_dir / "network_build_provenance.json"
    if provenance_path.exists():
        provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
    else:
        provenance = {}
    filtered_options = list(provenance.get("filtered_netconvert_options") or [])
    skipped_options = list(provenance.get("skipped_netconvert_options") or [])
    netconvert_version = str(provenance.get("netconvert_version") or "")
    option_fingerprint = _sha256_text(
        json.dumps(
            {
                "filtered_options": filtered_options,
                "skipped_options": skipped_options,
                "netconvert_version": netconvert_version,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
    )
    return {
        "net_file": net_file,
        "network_net_sha256": str(provenance.get("network_net_sha256") or _sha256_file(base_net)),
        "netconvert_options_sha256": str(
            provenance.get("netconvert_option_fingerprint_sha256")
            or provenance.get("netconvert_options_sha256")
            or option_fingerprint
        ),
    }


def _build_runtime_registry(
    integrated_dir: Path,
    global_registry_path: Path,
    network_mode: str,
    buffer_m: float,
    output_path: Path,
) -> str:
    """Build a run-local registry for the current runtime network.

    If the global registry already has rows for canonical_current, copies them
    to output_path.  Otherwise upserts: replaces only the three network-hash
    fields (canonical_network_version, network_net_sha256,
    netconvert_options_sha256) while preserving every other column
    (crosswalk geometry, tls_id, ped_link_indices, runnable flags,
    installation_assumption, registry_status, recovery fields, c1 provenance
    etc.).  A topological spot-check warns when runnable tls_ids are absent
    from the runtime net.xml but does not abort.

    Returns canonical_current (the version string for this runtime network).
    """
    if not global_registry_path.exists():
        raise FileNotFoundError(f"글로벌 레지스트리가 없습니다: {global_registry_path}")

    fingerprint = _load_integrated_network_fingerprint(integrated_dir)
    canonical_current = _canonical_network_version(
        network_mode=network_mode,
        buffer_m=buffer_m,
        network_net_sha256=fingerprint["network_net_sha256"],
        netconvert_options_sha256=fingerprint["netconvert_options_sha256"],
    )

    global_df = pd.read_csv(global_registry_path)
    global_df["canonical_network_version"] = global_df["canonical_network_version"].astype(str)

    # Direct match — use as-is
    scoped_df = global_df[global_df["canonical_network_version"] == canonical_current].copy()
    if not scoped_df.empty:
        print(
            f"[registry] canonical_network_version={canonical_current} "
            f"matched global registry ({len(scoped_df)} rows)."
        )
        output_path.parent.mkdir(parents=True, exist_ok=True)
        write_csv_utf8_sig(scoped_df, output_path)
        return canonical_current

    # Upsert path — global registry has a different network version.
    # CRITICAL: select exactly ONE source canonical_network_version block.
    # The global registry may contain multiple version blocks (e.g. 1340 rows =
    # 2 versions × 670 rows).  Copying all rows would create duplicate
    # crosswalk_ids in the runtime registry — unsafe for manifest building.
    existing_versions = global_df["canonical_network_version"].unique().tolist()
    print(
        f"[registry] Runtime net version {canonical_current} not in global registry. "
        f"Existing versions: {existing_versions}."
    )

    # Build per-version groups preserving CSV order (sort=False).
    version_group_sizes: dict[str, int] = {}
    version_group_dfs: dict[str, pd.DataFrame] = {}
    for _ver, _grp in global_df.groupby("canonical_network_version", sort=False):
        version_group_sizes[str(_ver)] = len(_grp)
        version_group_dfs[str(_ver)] = _grp

    # Prefer the largest (most complete) block — typically 670 rows.
    max_size = max(version_group_sizes.values())
    full_version_candidates = [v for v, sz in version_group_sizes.items() if sz == max_size]

    if len(full_version_candidates) == 1:
        source_version = full_version_candidates[0]
    elif "validated_at" in global_df.columns:
        # Multiple equal-size blocks: pick the one with the latest validated_at.
        best_ver: str | None = None
        best_ts: str | None = None
        for _v in full_version_candidates:
            _ts_series = version_group_dfs[_v]["validated_at"].dropna()
            if not _ts_series.empty:
                _latest = str(_ts_series.max())
                if best_ts is None or _latest > best_ts:
                    best_ts = _latest
                    best_ver = _v
        source_version = best_ver if best_ver else full_version_candidates[-1]
    else:
        # Fall back to the last full-size version in CSV order (most recently appended).
        _csv_order = list(dict.fromkeys(global_df["canonical_network_version"].tolist()))
        source_version = next(
            (v for v in reversed(_csv_order) if v in full_version_candidates),
            full_version_candidates[-1],
        )

    source_df = version_group_dfs[source_version].copy()
    print(
        f"[registry] Upsert source: version={source_version} ({len(source_df)} rows). "
        f"Replacing hash fields only — all crosswalk data preserved."
    )

    upserted_df = source_df.copy()
    upserted_df["canonical_network_version"] = canonical_current
    upserted_df["network_net_sha256"] = fingerprint["network_net_sha256"]
    upserted_df["netconvert_options_sha256"] = fingerprint["netconvert_options_sha256"]

    # Safety assertion: upserted registry must have no duplicate crosswalk_ids.
    upserted_df["crosswalk_id"] = upserted_df["crosswalk_id"].astype(str)
    _unique_cw = upserted_df["crosswalk_id"].nunique()
    _total_rows = len(upserted_df)
    if _unique_cw != _total_rows:
        raise ValueError(
            f"런타임 레지스트리에 중복 crosswalk_id가 감지되었습니다: "
            f"unique={_unique_cw}, total={_total_rows}. "
            f"Source version={source_version}. 소스 블록이 이미 중복 행을 포함합니다."
        )
    print(f"[registry] Safety check OK: unique crosswalk_id={_unique_cw}, total rows={_total_rows}.")

    # Topological spot-check: warn when runnable tls_ids are absent from runtime net.xml.
    base_net = integrated_dir / "network.net.xml"
    if base_net.exists():
        try:
            runtime_root = ET.parse(str(base_net)).getroot()
            runtime_tls_ids = {tl.get("id") for tl in runtime_root.findall("tlLogic")}
            sig_ext_mask = (
                upserted_df["runnable_for_signal_extension"]
                .astype(str).str.lower().isin(["true", "1", "yes"])
            )
            sig_ext_rows = upserted_df[sig_ext_mask]
            missing_tls_rows = sig_ext_rows[
                sig_ext_rows["tls_id"].astype(str).str.strip().apply(
                    lambda t: bool(t) and t not in runtime_tls_ids
                )
            ]
            if not missing_tls_rows.empty:
                print(
                    f"[registry] WARNING: {len(missing_tls_rows)} runnable crosswalk(s) have "
                    f"tls_id not present in runtime net.xml tlLogic. Network topology may differ "
                    f"from registry build. First affected: "
                    f"{missing_tls_rows['tls_id'].unique().tolist()[:5]}"
                )
            else:
                print(
                    "[registry] Topology spot-check OK: all runnable tls_ids found in runtime net.xml."
                )
        except Exception as _exc:
            print(f"[registry] Topology spot-check skipped: {_exc}")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    write_csv_utf8_sig(upserted_df, output_path)
    print(
        f"[registry] Run-local registry written: {output_path} "
        f"({len(upserted_df)} rows, version={canonical_current})"
    )
    return canonical_current


def _inject_selected_synthetic_tls(
    manifest_rows: list[dict[str, Any]],
    base_net: Path,
    signal_net: Path,
) -> tuple[Path, list[dict[str, Any]]]:
    """Inject synthetic TLS only for selected crosswalks that require it.

    Reads installation_assumption from each manifest row.  Only rows with
    installation_assumption == "synthetic_pedestrian_signal" trigger injection.
    If no selected crosswalk needs synthetic TLS (e.g. 119055-only run where
    the existing TLS joinedS_11252413259_11252413260 is used), returns base_net
    unchanged and network_with_signal.net.xml is NOT created for this run.

    Returns (net_file_to_use, updated_manifest_rows).
    """
    try:
        from .synthetic_tls_injector import inject_synthetic_tls
    except ImportError:
        from synthetic_tls_injector import inject_synthetic_tls  # type: ignore[no-redef]

    selected_tier2_ces: list[str] = []
    for row in manifest_rows:
        _raw_assumption = row.get("installation_assumption")
        # pandas CSV round-trip may produce float('nan') for empty cells
        assumption = (
            ""
            if _raw_assumption is None or pd.isna(_raw_assumption)  # type: ignore[arg-type]
            else str(_raw_assumption).strip()
        )
        if assumption == "synthetic_pedestrian_signal":
            ce = str(row.get("crossing_edge") or "").strip()
            if ce and ce not in selected_tier2_ces:
                selected_tier2_ces.append(ce)

    if not selected_tier2_ces:
        print(
            "[net] Selected crosswalks require no synthetic TLS injection. "
            "Using network.net.xml as runtime net."
        )
        return base_net, list(manifest_rows)

    print(
        f"[net] Injecting synthetic TLS for {len(selected_tier2_ces)} selected "
        f"crossing edge(s): {selected_tier2_ces}"
    )
    injection_result: dict[str, dict[str, Any]] = inject_synthetic_tls(
        net_xml_path=base_net,
        tier2_crossing_edges=selected_tier2_ces,
        output_path=signal_net,
    )

    # Patch manifest rows with the injected tls_id / ped_link_indices
    updated_rows: list[dict[str, Any]] = []
    for row in manifest_rows:
        row = dict(row)
        ce = str(row.get("crossing_edge") or "").strip()
        if ce in injection_result:
            synth = injection_result[ce]
            if "tls_id" in synth:
                row["tls_id"] = synth["tls_id"]
            if "ped_link_indices" in synth:
                row["ped_link_indices"] = synth["ped_link_indices"]
        updated_rows.append(row)

    return signal_net, updated_rows


def _validate_no_sumo_manifest(
    integrated_dir: Path,
    runtime_registry_path: Path | None,
    manifest_path: Path,
    selected_rows: list[dict[str, Any]],
) -> None:
    """No-SUMO pre-flight validation.

    Checks registry row counts, crosswalk field values, net.xml TLS coverage,
    and prints which synthetic TLS IDs are present in network_with_signal.net.xml.
    Warns on inconsistencies but does not abort (callers can add assertions).
    """
    print("\n" + "=" * 60)
    print("[no-SUMO validate] Manifest / registry consistency check")
    print("=" * 60)

    # 1. Registry checks
    if runtime_registry_path and runtime_registry_path.exists():
        try:
            reg_df = pd.read_csv(runtime_registry_path)
            reg_df = _prepare_crosswalk_id_columns(reg_df)
            print(f"  registry total rows : {len(reg_df)}")

            sig_ext = (
                reg_df["runnable_for_signal_extension"]
                .astype(str).str.lower().isin(["true", "1", "yes"])
            )
            print(f"  runnable_for_signal_extension=True : {sig_ext.sum()}")

            missing_tls = reg_df[
                sig_ext & (reg_df["tls_id"].astype(str).str.strip() == "")
            ]
            print(f"  missing tls_id (signal-ext runnable) : {len(missing_tls)}")

            missing_pli = reg_df[
                sig_ext &
                reg_df["ped_link_indices"].astype(str).str.strip().isin(["", "nan"])
            ]
            print(f"  missing ped_link_indices (signal-ext runnable) : {len(missing_pli)}")

            for row in selected_rows:
                requested_id = str(row.get("requested_crosswalk_id") or row.get("crosswalk_id") or "")
                source_id = str(row.get("source_crosswalk_id") or row.get("crosswalk_id") or "")
                canonical_id = _normalize_crosswalk_id(
                    row.get("canonical_crosswalk_id") or row.get("crosswalk_id") or ""
                )
                rows = reg_df[reg_df["canonical_crosswalk_id"] == canonical_id]
                if rows.empty:
                    print(
                        f"  [WARN] requested={requested_id} source={source_id} canonical={canonical_id} "
                        "not found in run-local registry"
                    )
                else:
                    r = rows.iloc[0]
                    print(
                        f"  [requested={requested_id} source={source_id} canonical={canonical_id}] "
                        f"registry_crosswalk_id={r.get('registry_crosswalk_id', r.get('crosswalk_id', ''))}, "
                        f"crossing_edge={r.get('crossing_edge')}, "
                        f"tls_id={r.get('tls_id')}, "
                        f"ped_link_indices={r.get('ped_link_indices')}, "
                        f"runnable_for_signal_extension={r.get('runnable_for_signal_extension')}, "
                        f"installation_assumption={r.get('installation_assumption','')!r}"
                    )
        except Exception as _exc:
            print(f"  [WARN] registry read failed: {_exc}")
    else:
        print("  [INFO] run-local registry not available for validation.")

    # 2. net.xml TLS coverage check
    base_net = integrated_dir / "network.net.xml"
    signal_net = integrated_dir / "network_with_signal.net.xml"

    if base_net.exists():
        try:
            base_root = ET.parse(str(base_net)).getroot()
            base_tls_ids: set[str] = {
                tl.get("id", "") for tl in base_root.findall("tlLogic")
            }

            if signal_net.exists():
                signal_root = ET.parse(str(signal_net)).getroot()
                signal_tls_ids: set[str] = {
                    tl.get("id", "") for tl in signal_root.findall("tlLogic")
                }
                injected = sorted(signal_tls_ids - base_tls_ids)
                print(
                    f"  synthetic TLS injected into network_with_signal.net.xml: "
                    f"{injected if injected else '(none)'}"
                )
                if "1945254658" in signal_tls_ids:
                    known_1945 = {"125786", "125787", "74154", "8166"}
                    selected_canonical_ids = {
                        _normalize_crosswalk_id(row.get("canonical_crosswalk_id") or row.get("crosswalk_id") or "")
                        for row in selected_rows
                    }
                    if not known_1945.intersection(selected_canonical_ids):
                        print(
                            "  [WARN] tlLogic id=1945254658 present in network_with_signal.net.xml "
                            "but no selected crosswalk is known to require it. "
                            "Check _inject_selected_synthetic_tls() filtering."
                        )
            else:
                print("  network_with_signal.net.xml : not created (no synthetic TLS needed).")
        except Exception as _exc:
            print(f"  [WARN] net.xml TLS check failed: {_exc}")

    # 3. Manifest JSON summary
    if manifest_path.exists():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            print(f"  manifest net_file       : {manifest.get('net_file', '')}")
            print(f"  manifest crosswalks     : {len(manifest.get('crosswalks', []))}")
            print(f"  manifest excluded       : {len(manifest.get('excluded_crosswalks', []))}")
        except Exception as _exc:
            print(f"  [WARN] manifest JSON read failed: {_exc}")

    print("=" * 60 + "\n")


def _geojson_polygons(geojson_path: str | Path) -> list[list[list[tuple[float, float]]]]:
    data = json.loads(Path(geojson_path).read_text(encoding="utf-8"))
    features = data.get("features", [])
    polygons: list[list[list[tuple[float, float]]]] = []
    for feature in features:
        geometry = feature.get("geometry", {})
        gtype = geometry.get("type")
        coords = geometry.get("coordinates", [])
        if gtype == "Polygon":
            polygons.append(
                [[(float(lon), float(lat)) for lon, lat in ring] for ring in coords]
            )
        elif gtype == "MultiPolygon":
            for polygon in coords:
                polygons.append(
                    [[(float(lon), float(lat)) for lon, lat in ring] for ring in polygon]
                )
    if not polygons:
        raise ValueError(f"유효한 Polygon/MultiPolygon이 없습니다: {geojson_path}")
    return polygons


def _point_in_ring(lon: float, lat: float, ring: list[tuple[float, float]]) -> bool:
    inside = False
    if len(ring) < 3:
        return False
    x1, y1 = ring[-1]
    for x2, y2 in ring:
        denom = y2 - y1
        if abs(denom) > 1e-12 and ((y1 > lat) != (y2 > lat)):
            intersect_x = (x2 - x1) * (lat - y1) / denom + x1
            if lon < intersect_x:
                inside = not inside
        x1, y1 = x2, y2
    return inside


def point_in_geojson(lon: float, lat: float, geojson_path: str | Path) -> bool:
    for polygon in _geojson_polygons(geojson_path):
        if not polygon:
            continue
        outer = polygon[0]
        holes = polygon[1:]
        if _point_in_ring(lon, lat, outer) and not any(
            _point_in_ring(lon, lat, hole) for hole in holes
        ):
            return True
    return False


def _numeric_metric_keys(rows: list[dict[str, Any]], excluded: set[str]) -> list[str]:
    if not rows:
        return []
    keys: list[str] = []
    for key in rows[0]:
        if key in excluded:
            continue
        series = pd.Series([row.get(key) for row in rows])
        if pd.api.types.is_numeric_dtype(series):
            keys.append(key)
    return keys


def _mean_or_nan(values: list[float]) -> float:
    arr = np.asarray(values, dtype=float)
    if arr.size == 0 or np.all(np.isnan(arr)):
        return float("nan")
    return float(np.nanmean(arr))


def _df_to_markdown(frame: pd.DataFrame) -> str:
    try:
        return frame.to_markdown(index=False)
    except Exception:
        return "```text\n" + frame.to_csv(index=False) + "```"


def _net_tllogic_ids(net_file: str | Path) -> set[str]:
    root = ET.parse(net_file).getroot()
    return {
        str(node.attrib.get("id"))
        for node in root.findall("tlLogic")
        if str(node.attrib.get("id") or "").strip()
    }


def _net_connection_tl_ids(net_file: str | Path) -> set[str]:
    root = ET.parse(net_file).getroot()
    return {
        str(node.attrib.get("tl"))
        for node in root.findall("connection")
        if str(node.attrib.get("tl") or "").strip()
    }


def _probe_traci_tls_ids_for_net(net_file: str | Path, seed: int = 42, step_length: float = 0.1) -> set[str]:
    net_file = Path(net_file)
    probe_sumocfg = net_file.parent / f"_precheck_tls_probe_seed{seed}.sumocfg"
    probe_sumocfg.write_text(
        "\n".join(
            [
                "<configuration>",
                "    <input>",
                f'        <net-file value="{net_file.name}"/>',
                "    </input>",
                "    <time>",
                "        <begin value=\"0\"/>",
                "        <end value=\"1\"/>",
                f'        <step-length value="{step_length}"/>',
                "    </time>",
                "</configuration>",
                "",
            ]
        ),
        encoding="utf-8",
    )
    try:
        return snapshot_traci_trafficlight_ids(probe_sumocfg, seed=seed, step_length=step_length)
    except Exception:
        return set()
    finally:
        try:
            probe_sumocfg.unlink(missing_ok=True)
        except Exception:
            pass


def _to_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    return text in {"1", "true", "yes", "y"}


def _normalize_crosswalk_id(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        numeric = float(text)
    except Exception:
        numeric = None
    if numeric is not None and numeric.is_integer():
        return str(int(numeric))
    match = re.search(r"(\d+)(?:\.0+)?$", text)
    if match:
        return str(int(match.group(1)))
    return text


def _prepare_crosswalk_id_columns(df: pd.DataFrame, id_column: str = "crosswalk_id") -> pd.DataFrame:
    prepared = df.copy()
    if id_column in prepared.columns:
        prepared[id_column] = prepared[id_column].astype(str).str.strip()
        prepared["source_crosswalk_id"] = prepared[id_column]
        prepared["canonical_crosswalk_id"] = prepared[id_column].map(_normalize_crosswalk_id)
    return prepared


def _stable_crosswalk_seed_offset(value: Any) -> int:
    canonical = _normalize_crosswalk_id(value)
    seed_text = canonical if canonical else str(value or "").strip()
    if not seed_text:
        seed_text = "unknown_crosswalk"
    try:
        return int(seed_text)
    except Exception:
        digest = hashlib.sha1(seed_text.encode("utf-8")).hexdigest()
        return int(digest[:12], 16)


def _candidate_recovery_dirs(output_dir: Path) -> list[Path]:
    candidates: list[Path] = [output_dir]
    result_root = output_dir.parent.parent if output_dir.parent.parent.exists() else output_dir.parent
    fallback = result_root / "integrated_implementation_audit" / "outputs"
    if fallback not in candidates:
        candidates.append(fallback)
    uniq: list[Path] = []
    for path in candidates:
        if path not in uniq:
            uniq.append(path)
    return uniq


def load_recovered_candidates(output_dir: str | Path) -> tuple[dict[str, dict[str, Any]], str]:
    out_dir = Path(output_dir)
    for candidate_dir in _candidate_recovery_dirs(out_dir):
        recovered_path = candidate_dir / "recovered_simulation_target_list.csv"
        attempts_path = candidate_dir / "c_recovery_attempts.csv"
        tls_remap_path = candidate_dir / "tls_remap_candidates.csv"
        if not recovered_path.exists() or not attempts_path.exists():
            continue
        recovered_df = pd.read_csv(recovered_path)
        attempts_df = pd.read_csv(attempts_path)
        tls_remap_df = pd.read_csv(tls_remap_path) if tls_remap_path.exists() else pd.DataFrame()
        recovered_df["smart_crosswalk_id"] = recovered_df["smart_crosswalk_id"].astype(str)
        attempts_df["smart_crosswalk_id"] = attempts_df["smart_crosswalk_id"].astype(str)
        if not tls_remap_df.empty:
            tls_remap_df["smart_crosswalk_id"] = tls_remap_df["smart_crosswalk_id"].astype(str)
        merged = recovered_df.merge(
            attempts_df[
                [
                    "smart_crosswalk_id",
                    "path_uses_crossing",
                    "path_uses_walkingarea",
                    "matched_crossing_edge",
                    "resolved_tllogic_id",
                    "recovery_distance_m",
                    "matched_radius_m",
                    "recovery_status",
                    "simulation_usable",
                ]
            ],
            on="smart_crosswalk_id",
            how="left",
            suffixes=("", "_attempt"),
        )
        if not tls_remap_df.empty:
            merged = merged.merge(
                tls_remap_df[
                    [
                        "smart_crosswalk_id",
                        "resolved_exists_in_traci",
                    ]
                ],
                on="smart_crosswalk_id",
                how="left",
            )
        rows = {
            str(row.get("smart_crosswalk_id")): dict(row)
            for row in merged.to_dict(orient="records")
        }
        return rows, str(recovered_path)
    return {}, ""


def _build_manifest_rows_from_registry(
    selected_df: pd.DataFrame,
    registry_path: str | Path,
    integrated_dir: Path,
    network_mode: str,
    buffer_m: float,
    target_network_version: str | None = None,
    output_dir: Path | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], str, list[dict[str, Any]]]:
    registry_path = Path(registry_path)
    if not registry_path.exists():
        raise FileNotFoundError(f"registry 파일이 없습니다: {registry_path}")

    fingerprint = _load_integrated_network_fingerprint(integrated_dir)
    canonical_current = _canonical_network_version(
        network_mode=network_mode,
        buffer_m=buffer_m,
        network_net_sha256=fingerprint["network_net_sha256"],
        netconvert_options_sha256=fingerprint["netconvert_options_sha256"],
    )
    target_version = target_network_version or canonical_current

    registry_df = pd.read_csv(registry_path)
    if registry_df.empty:
        raise ValueError(f"registry가 비어 있습니다: {registry_path}")
    registry_df = _prepare_crosswalk_id_columns(registry_df)
    registry_df["canonical_network_version"] = registry_df["canonical_network_version"].astype(str)
    scoped_df = registry_df[registry_df["canonical_network_version"] == str(target_version)].copy()
    if scoped_df.empty:
        raise ValueError(
            f"registry에 canonical_network_version={target_version} 데이터가 없습니다. "
            "registry build/validate를 먼저 실행하세요."
        )

    hashes = scoped_df[["network_net_sha256", "netconvert_options_sha256"]].drop_duplicates()
    if len(hashes) != 1:
        raise ValueError(
            f"registry 버전 {target_version} 에 해시가 복수 존재합니다. registry 정합성 확인이 필요합니다."
        )
    row_hash = hashes.iloc[0].to_dict()
    row_net_sha = str(row_hash.get("network_net_sha256") or "")
    row_opt_sha = str(row_hash.get("netconvert_options_sha256") or "")
    if row_net_sha != fingerprint["network_net_sha256"] or row_opt_sha != fingerprint["netconvert_options_sha256"]:
        raise ValueError(
            "registry 네트워크 해시가 현재 integrated network와 다릅니다. "
            "registry validate/build를 다시 실행하세요."
        )

    row_map = {
        str(row.get("canonical_crosswalk_id")): row
        for row in scoped_df.to_dict(orient="records")
    }
    manifest_rows: list[dict[str, Any]] = []
    excluded_rows: list[dict[str, Any]] = []
    mapping_rows: list[dict[str, Any]] = []
    for row in selected_df.itertuples(index=False):
        requested_cw_id = str(getattr(row, "requested_crosswalk_id", getattr(row, "crosswalk_id")))
        source_cw_id = str(getattr(row, "source_crosswalk_id", getattr(row, "crosswalk_id")))
        canonical_cw_id = _normalize_crosswalk_id(
            getattr(row, "canonical_crosswalk_id", getattr(row, "crosswalk_id"))
        )
        reg = row_map.get(canonical_cw_id)
        registry_cw_id = str(reg.get("registry_crosswalk_id") or reg.get("crosswalk_id") or "") if reg else ""
        runnable_flag = _to_bool(reg.get("runnable_for_signal_extension", False)) if reg else False
        if reg is None:
            mapping_status = "registry_missing_crosswalk_id"
            mapping_reason = "registry_missing_crosswalk_id"
            mapping_method = "canonical_crosswalk_id"
        else:
            registry_status = str(reg.get("registry_status") or "")
            if registry_status not in REGISTRY_RUNNABLE_STATUSES:
                mapping_status = f"registry_status_not_runnable:{registry_status or 'empty'}"
                mapping_reason = f"registry_status_not_runnable:{registry_status or 'empty'}"
                mapping_method = "canonical_crosswalk_id"
            elif not runnable_flag:
                mapping_status = "registry_not_runnable_for_signal_extension"
                mapping_reason = "runnable_for_signal_extension_false"
                mapping_method = "canonical_crosswalk_id"
            else:
                mapping_status = "matched"
                mapping_reason = ""
                mapping_method = "canonical_crosswalk_id"
        mapping_rows.append(
            {
                "requested_crosswalk_id": requested_cw_id,
                "source_crosswalk_id": source_cw_id,
                "canonical_crosswalk_id": canonical_cw_id,
                "registry_crosswalk_id": registry_cw_id,
                "mapping_status": mapping_status,
                "mapping_method": mapping_method,
                "runnable_for_signal_extension": runnable_flag,
                "mapping_reason": mapping_reason,
            }
        )
        if reg is None:
            excluded_rows.append(
                {
                    "smart_crosswalk_id": source_cw_id,
                    "requested_crosswalk_id": requested_cw_id,
                    "canonical_crosswalk_id": canonical_cw_id,
                    "original_tls_id": "",
                    "rejected_reason": "registry_missing_crosswalk_id",
                    "tlLogic_exists_in_net_xml": False,
                    "exists_in_traci_getIDList": False,
                    "action_taken": "excluded_from_manifest_precheck",
                    "implementation_status": "",
                    "failure_reason": "registry_missing_crosswalk_id",
                    "recovery_status": "",
                    "recovery_source": "canonical_registry",
                    "recovery_distance_m": None,
                    "matched_radius_m": None,
                    "original_failure_reason": "",
                    "override_reason": "",
                }
            )
            continue
        registry_status = str(reg.get("registry_status") or "")
        if registry_status not in REGISTRY_RUNNABLE_STATUSES:
            tls_id = str(reg.get("tls_id") or "")
            excluded_rows.append(
                {
                    "smart_crosswalk_id": source_cw_id,
                    "requested_crosswalk_id": requested_cw_id,
                    "canonical_crosswalk_id": canonical_cw_id,
                    "original_tls_id": tls_id,
                    "rejected_reason": f"registry_status_not_runnable:{registry_status or 'empty'}",
                    "tlLogic_exists_in_net_xml": _to_bool(reg.get("tlLogic_exists_in_net_xml", False)),
                    "exists_in_traci_getIDList": False,
                    "action_taken": "excluded_from_manifest_precheck",
                    "implementation_status": str(reg.get("implementation_status") or ""),
                    "failure_reason": str(reg.get("notes") or ""),
                    "recovery_status": str(reg.get("recovery_status") or ""),
                    "recovery_source": "canonical_registry",
                    "recovery_distance_m": reg.get("match_distance_m"),
                    "matched_radius_m": reg.get("matched_radius_m"),
                    "original_failure_reason": str(reg.get("notes") or ""),
                    "override_reason": "registry_status_gate",
                }
            )
            continue

        ped_route = {
            "from_edge": str(reg.get("from_edge") or ""),
            "to_edge": str(reg.get("to_edge") or ""),
        }
        cw_metadata = {
            "cw_id": source_cw_id,
            "net_file": str(fingerprint["net_file"]),
            "crossing_edge": str(reg.get("crossing_edge") or ""),
            "crossing_lon": float(reg.get("crossing_lon")) if str(reg.get("crossing_lon") or "").strip() else None,
            "crossing_lat": float(reg.get("crossing_lat")) if str(reg.get("crossing_lat") or "").strip() else None,
            "tls_id": str(reg.get("tls_id") or ""),
            "ped_link_indices": _to_int_list(reg.get("ped_link_indices")),
            "ped_route": ped_route,
            "vehicle_conflict_edges": _to_str_list(reg.get("vehicle_conflict_edges")),
            "approach_lanes": _to_str_list(reg.get("approach_lanes")),
        }

        manifest_rows.append(
            {
                "crosswalk_id": source_cw_id,
                "requested_crosswalk_id": requested_cw_id,
                "source_crosswalk_id": source_cw_id,
                "canonical_crosswalk_id": canonical_cw_id,
                "registry_crosswalk_id": registry_cw_id,
                "admin_dong": getattr(row, "admin_dong"),
                "dong_name": getattr(row, "dong_name"),
                "longitude": float(getattr(row, "longitude")),
                "latitude": float(getattr(row, "latitude")),
                "lane_count": float(getattr(row, "lane_count")),
                "max_speed_kph": float(getattr(row, "max_speed_kph")),
                "elderly_ratio": float(getattr(row, "elderly_ratio")),
                "accident_count": float(getattr(row, "accident_count")),
                "estimated_aadt": getattr(row, "estimated_aadt", 0.0),
                "crossing_length_m": getattr(row, "crossing_length_m", getattr(row, "crosswalk_length", 0.0)),
                "ped_green_base": getattr(row, "ped_green_base", 0.0),
                "ped_green_elderly": getattr(row, "ped_green_elderly", 0.0),
                "risk_score": getattr(row, "risk_score", getattr(row, "priority_score", 0.0)),
                "inside_junggu_boundary": True,
                "inside_analysis_area": True,
                "inside_smart_target_area": True,
                "match_distance_m": reg.get("match_distance_m"),
                "implementation_status": str(reg.get("implementation_status") or "ready_for_simulation"),
                "repair_attempted": False,
                "repair_success": bool(registry_status == "recovered"),
                "reconstruction_needed": False,
                "final_usable_for_simulation": True,
                "original_from_edge": ped_route["from_edge"],
                "original_to_edge": ped_route["to_edge"],
                "repaired_from_edge": ped_route["from_edge"] if registry_status == "recovered" else "",
                "repaired_to_edge": ped_route["to_edge"] if registry_status == "recovered" else "",
                "path_uses_crossing": _to_bool(reg.get("path_uses_crossing", True)),
                "validation_status": "valid" if _to_bool(reg.get("path_uses_crossing", True)) else "registry_invalid",
                "recovery_status": str(reg.get("recovery_status") or ""),
                "recovery_source": "canonical_registry",
                "recovery_distance_m": reg.get("match_distance_m"),
                "matched_radius_m": reg.get("matched_radius_m"),
                "original_failure_reason": str(reg.get("notes") or ""),
                "override_reason": "registry_promoted",
                **cw_metadata,
            }
        )

    mapping_df = pd.DataFrame(mapping_rows)
    if output_dir is not None:
        write_csv_utf8_sig(mapping_df, output_dir / "crosswalk_id_mapping.csv")
    return manifest_rows, excluded_rows, str(registry_path), mapping_rows


def apply_recovered_candidate_override(
    assessment: dict[str, Any],
    recovered_row: dict[str, Any] | None,
    net: Any,
    tl_logic_ids: set[str],
    connection_tl_ids: set[str],
) -> tuple[dict[str, Any], bool, str]:
    if not recovered_row:
        return assessment, False, "recovered_override_not_applicable"

    original_failure_reason = str(assessment.get("failure_reason") or "")
    status = str(assessment.get("implementation_status") or "")
    if status not in {"crossing_reconstruction_required", "route_repair_failed"}:
        return assessment, False, "recovered_override_not_applicable"
    if "distance_exceeded" not in original_failure_reason and "missing_tls" not in original_failure_reason:
        return assessment, False, "recovered_override_not_applicable"

    recovery_status = str(recovered_row.get("recovery_status") or "")
    simulation_usable = _to_bool(recovered_row.get("simulation_usable", False))
    matched_crossing_edge = str(recovered_row.get("matched_crossing_edge") or "")
    resolved_tllogic_id = str(recovered_row.get("resolved_tllogic_id") or "")
    path_uses_crossing = _to_bool(recovered_row.get("path_uses_crossing", False))
    path_uses_walkingarea = _to_bool(recovered_row.get("path_uses_walkingarea", False))
    recovery_distance_m = float(recovered_row.get("recovery_distance_m"))
    matched_radius_m = int(float(recovered_row.get("matched_radius_m")))

    if recovery_status != "auto_recovered_distance_rematch":
        return assessment, False, "recovered_override_failed_invalid_recovery_status"
    if not simulation_usable:
        return assessment, False, "recovered_override_failed_simulation_usable_false"
    if not matched_crossing_edge:
        return assessment, False, "recovered_override_failed_missing_crossing_edge"
    if not path_uses_crossing:
        return assessment, False, "recovered_override_failed_route_not_using_crossing"
    if not path_uses_walkingarea:
        return assessment, False, "recovered_override_failed_route_not_using_walkingarea"
    if not resolved_tllogic_id:
        return assessment, False, "recovered_override_failed_missing_resolved_tllogic"
    if resolved_tllogic_id not in tl_logic_ids:
        return assessment, False, "recovered_override_failed_missing_tllogic"
    if resolved_tllogic_id not in connection_tl_ids:
        return assessment, False, "recovered_override_failed_missing_connection_tl"
    if not _to_bool(recovered_row.get("resolved_exists_in_traci", False)):
        return assessment, False, "recovered_override_failed_not_in_traci"
    if recovery_distance_m > 100.0:
        return assessment, False, "recovered_override_failed_distance_over_100m"
    if matched_radius_m not in {75, 100}:
        return assessment, False, "recovered_override_failed_invalid_radius"

    cw_metadata = dict(assessment.get("cw_metadata", {}))
    ped_val = dict(assessment.get("ped_val", {}))
    cw_metadata["crossing_edge"] = matched_crossing_edge
    cw_metadata["tls_id"] = resolved_tllogic_id
    try:
        edge = net.getEdge(matched_crossing_edge)
        shape = edge.getShape()
        if shape:
            mid = shape[len(shape) // 2]
            crossing_lon, crossing_lat = net.convertXY2LonLat(mid[0], mid[1])
            cw_metadata["crossing_lon"] = float(crossing_lon)
            cw_metadata["crossing_lat"] = float(crossing_lat)
    except Exception:
        pass
    # Recompute ped_link_indices with the corrected crossing_edge and tls_id.
    # The original assessment failed with tls_id=None so ped_link_indices was [],
    # and that empty value is otherwise silently inherited after recovery.
    net_file_path = cw_metadata.get("net_file")
    if net_file_path:
        recomputed = pedestrian_link_indices(net_file_path, resolved_tllogic_id, matched_crossing_edge)
        cw_metadata["ped_link_indices"] = recomputed
        cw_metadata["ped_link_indices_source"] = "recomputed_after_recovery"
    ped_val["path_uses_crossing"] = True
    ped_val["path_uses_walkingarea"] = True
    ped_val["path_exists"] = True
    ped_val["validation_status"] = "valid"

    patched = dict(assessment)
    patched.update(
        {
            "implementation_status": "auto_recovered_distance_rematch",
            "final_usable_for_simulation": True,
            "failure_reason": "",
            "repair_suggestion": "",
            "tls_id": resolved_tllogic_id,
            "crossing_edge": matched_crossing_edge,
            "initial_match_distance_m": recovery_distance_m,
            "repaired_match_distance_m": recovery_distance_m,
            "cw_metadata": cw_metadata,
            "ped_val": ped_val,
            "recovery_status": recovery_status,
            "recovery_source": "recovered_simulation_target_list.csv",
            "recovery_distance_m": recovery_distance_m,
            "matched_radius_m": matched_radius_m,
            "original_failure_reason": original_failure_reason,
            "override_reason": "verified_recovered_candidate",
        }
    )
    return patched, True, ""


def _validate_integrated_tls_candidates(
    crosswalk_contexts: list[dict[str, Any]],
    net_file: str | Path,
    traci_tls_ids: set[str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    tl_logic_ids = _net_tllogic_ids(net_file)
    valid_contexts: list[dict[str, Any]] = []
    excluded_rows: list[dict[str, Any]] = []
    for context in crosswalk_contexts:
        cw_id = str(context.get("crosswalk_id") or "")
        original_tls_id = str(context.get("tls_id") or "")
        tl_logic_exists = bool(original_tls_id and original_tls_id in tl_logic_ids)
        exists_in_traci = bool(original_tls_id and original_tls_id in traci_tls_ids)
        if tl_logic_exists and exists_in_traci:
            valid_contexts.append(context)
            continue
        if not original_tls_id:
            reason = "missing_tls_id"
        elif not tl_logic_exists and not exists_in_traci:
            reason = "ghost_tls_no_tlLogic_and_tls_not_known_by_traci"
        elif not tl_logic_exists:
            reason = "ghost_tls_no_tlLogic"
        else:
            reason = "tls_not_known_by_traci"
        excluded_rows.append(
            {
                "smart_crosswalk_id": cw_id,
                "original_tls_id": original_tls_id,
                "rejected_reason": reason,
                "tlLogic_exists_in_net_xml": tl_logic_exists,
                "exists_in_traci_getIDList": exists_in_traci,
                "action_taken": "excluded_from_integrated_run",
            }
        )
    return valid_contexts, excluded_rows


def _write_integrated_methodology(
    output_dir: Path,
    selected_df: pd.DataFrame,
    per_crosswalk_delta: pd.DataFrame,
    network_delta: pd.DataFrame,
    model_parameters_path: str | Path | None,
) -> None:
    model_params = load_model_parameters(model_parameters_path)
    params_preview = pd.DataFrame(
        [
            {
                "parameter": name,
                "value": json.dumps(meta.get("value"), ensure_ascii=False)
                if isinstance(meta.get("value"), (dict, list))
                else meta.get("value"),
                "source_type": meta.get("source_type"),
            }
            for name, meta in model_params.items()
        ]
    )
    lines = [
        "# 통합 중구망 다중 스마트 횡단보도 시뮬레이션",
        "",
        f"- 선택 횡단보도 수: {len(selected_df)}",
        f"- 선택 ID: {', '.join(selected_df['crosswalk_id'].astype(str).tolist())}",
        "- 비교 시나리오: baseline / smart_selected",
        "- **분석 대상**: 중구 내부 횡단보도만 허용",
        "- **네트워크 범위**: 교통류 단절 방지 및 boundary effect 완화를 위해 중구 행정경계 + buffer 1000m 사용",
        "- **Buffer 영역 처리**: 교통 유입/유출용으로만 사용하며, 스마트 횡단보도 설치 후보 선정 및 주요 성과 평가에서는 완전히 제외됨",
        "- **SUMO Crossing 매칭**: 원본 T2 좌표와 SUMO 네트워크 내 crossing의 매칭 임계값은 50m로 제한",
        "  - 50m를 초과하는 후보는 잘못된 교차로/횡단보도에 매칭될 가능성(위치 정합성 문제)이 높아 실패/제외 처리",
        "  - 300m 등 임계값을 완화할 경우 인접 교차로 신호기를 엉뚱하게 제어하는 심각한 논리적 오류가 발생하므로 완화 불가",
        "  - `--crossings.guess` 옵션을 사용하더라도 T2 좌표에 정확히 crossing이 생성된다는 보장이 없으므로 50m 이내 강제 매칭을 우회할 수 없음",
        "",
        "## 파라미터",
        "",
        _df_to_markdown(params_preview) if not params_preview.empty else "없음",
        "",
        "## 선택 횡단보도 delta",
        "",
        _df_to_markdown(per_crosswalk_delta.head(20))
        if not per_crosswalk_delta.empty
        else "비교 가능한 선택 횡단보도 결과가 없습니다.",
        "",
        "## 네트워크 delta",
        "",
        _df_to_markdown(network_delta)
        if not network_delta.empty
        else "비교 가능한 네트워크 결과가 없습니다.",
        "",
    ]
    (output_dir / "report_4_methodology.md").write_text("\n".join(lines), encoding="utf-8")


def _write_delta_figure(delta_df: pd.DataFrame, figures_dir: Path) -> None:
    figures_dir.mkdir(parents=True, exist_ok=True)
    try:
        ensure_matplotlib_env()
        import matplotlib

        matplotlib.use("Agg")
        configure_matplotlib(matplotlib)
        import matplotlib.pyplot as plt
    except ImportError:
        return

    plt.figure(figsize=(8, 6))
    if delta_df.empty:
        plt.text(0.5, 0.5, "no comparable baseline/smart_selected result", ha="center", va="center")
        plt.xlim(0, 1)
        plt.ylim(0, 1)
        plt.xticks([])
        plt.yticks([])
    else:
        x_vals = pd.to_numeric(delta_df["avg_vehicle_delay_delta_sec"], errors="coerce")
        y_vals = pd.to_numeric(delta_df["safety_risk_delta"], errors="coerce")
        plt.scatter(x_vals, y_vals, color="#1f5aa6", s=70, alpha=0.85)
        for row in delta_df.itertuples(index=False):
            plt.annotate(
                str(row.crosswalk_id),
                (row.avg_vehicle_delay_delta_sec, row.safety_risk_delta),
                xytext=(4, 4),
                textcoords="offset points",
                fontsize=8,
            )
        plt.axhline(0, color="gray", linewidth=1)
        plt.axvline(0, color="gray", linewidth=1)
        plt.xlabel("avg_vehicle_delay_delta_sec")
        plt.ylabel("safety_risk_delta")
    plt.title("통합망 스마트 횡단보도 안전-지체 Trade-off")
    plt.tight_layout()
    plt.savefig(figures_dir / "tradeoff_summary.png", dpi=160)
    plt.close()


def _build_baseline_delta(
    summary_df: pd.DataFrame,
    key_column: str,
    smart_scenario: str = "smart_selected",
) -> pd.DataFrame:
    if summary_df.empty:
        return pd.DataFrame()
    baseline = summary_df[summary_df["scenario"] == "baseline"].set_index(key_column)
    smart = summary_df[summary_df["scenario"] == smart_scenario].set_index(key_column)
    common = baseline.index.intersection(smart.index)
    if common.empty:
        return pd.DataFrame()
    return pd.DataFrame(
        {
            key_column: common,
            "safety_risk_delta": smart.loc[common, "safety_risk_score"].to_numpy()
            - baseline.loc[common, "safety_risk_score"].to_numpy(),
            "accident_expected_delta": smart.loc[common, "accident_expected_value"].to_numpy()
            - baseline.loc[common, "accident_expected_value"].to_numpy(),
            "elderly_incomplete_crossings_delta": smart.loc[common, "elderly_incomplete_crossings"].to_numpy()
            - baseline.loc[common, "elderly_incomplete_crossings"].to_numpy(),
            "avg_vehicle_delay_delta_sec": smart.loc[common, "avg_vehicle_delay_sec"].to_numpy()
            - baseline.loc[common, "avg_vehicle_delay_sec"].to_numpy(),
            "avg_queue_length_delta": smart.loc[common, "avg_queue_length"].to_numpy()
            - baseline.loc[common, "avg_queue_length"].to_numpy(),
            "max_queue_length_delta": smart.loc[common, "max_queue_length"].to_numpy()
            - baseline.loc[common, "max_queue_length"].to_numpy(),
            "surrounding_road_delay_delta_sec": smart.loc[common, "surrounding_road_delay_sec"].to_numpy()
            - baseline.loc[common, "surrounding_road_delay_sec"].to_numpy(),
            "vehicle_delay_cost_delta": smart.loc[common, "vehicle_delay_cost"].to_numpy()
            - baseline.loc[common, "vehicle_delay_cost"].to_numpy(),
            "extension_count_smart": smart.loc[common, "extension_count"].to_numpy(),
            "total_extension_sec_smart": smart.loc[common, "total_extension_sec"].to_numpy(),
        }
    ).sort_values(key_column)


def select_smart_crosswalks(
    t2_path: str | Path,
    smart_crosswalk_ids: list[str] | tuple[str, ...],
    output_dir: str | Path,
    admin_polygon_path: str | Path,
) -> pd.DataFrame:
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    all_features = _prepare_crosswalk_id_columns(load_crosswalk_features(t2_path))
    requested_ids = [str(crosswalk_id).strip() for crosswalk_id in smart_crosswalk_ids]
    canonical_requested_ids = [_normalize_crosswalk_id(crosswalk_id) for crosswalk_id in requested_ids]
    all_features["requested_crosswalk_id"] = ""
    matched = all_features[all_features["canonical_crosswalk_id"].isin(canonical_requested_ids)].copy()
    found = set(matched["canonical_crosswalk_id"].astype(str))
    missing = [
        requested_id
        for requested_id, canonical_id in zip(requested_ids, canonical_requested_ids)
        if canonical_id not in found
    ]
    if missing:
        available_preview = (
            all_features[["source_crosswalk_id", "canonical_crosswalk_id"]]
            .drop_duplicates()
            .head(20)
            .to_dict(orient="records")
        )
        raise ValueError(
            "smart_crosswalk_ids를 찾지 못했습니다: "
            f"requested={missing}, canonical={[_normalize_crosswalk_id(x) for x in missing]}, "
            f"available_preview={available_preview}"
        )
    matched["inside_junggu_boundary"] = matched.apply(
        lambda row: point_in_geojson(float(row["longitude"]), float(row["latitude"]), admin_polygon_path),
        axis=1,
    )
    selected_rows: list[pd.Series] = []
    outside: list[str] = []
    duplicate_inside: list[str] = []
    for requested_id, canonical_id in zip(requested_ids, canonical_requested_ids):
        rows = matched[matched["canonical_crosswalk_id"].astype(str) == canonical_id].copy()
        inside_rows = rows[rows["inside_junggu_boundary"]].copy()
        if inside_rows.empty:
            outside.append(requested_id)
            continue
        if len(inside_rows) > 1:
            duplicate_inside.append(requested_id)
            continue
        selected = inside_rows.iloc[0].copy()
        selected["requested_crosswalk_id"] = requested_id
        selected["canonical_requested_crosswalk_id"] = canonical_id
        selected_rows.append(selected)
    if outside:
        raise ValueError(f"중구 경계 밖 횡단보도는 integrated_selected 모드에서 사용할 수 없습니다: {outside}")
    if duplicate_inside:
        raise ValueError(f"중구 내부에 중복 행이 있어 선택할 수 없는 crosswalk_id가 있습니다: {duplicate_inside}")
    filtered = pd.DataFrame(selected_rows)
    filtered["_order"] = filtered["requested_crosswalk_id"].astype(str).map(
        {cw_id: idx for idx, cw_id in enumerate(requested_ids)}
    )
    filtered = filtered.sort_values("_order").drop(columns="_order")
    filtered["source_crosswalk_id"] = filtered["source_crosswalk_id"].astype(str)
    filtered["crosswalk_id"] = filtered["source_crosswalk_id"]
    filtered["canonical_crosswalk_id"] = filtered["canonical_crosswalk_id"].astype(str)
    filtered["requested_crosswalk_id"] = filtered["requested_crosswalk_id"].astype(str)
    write_csv_utf8_sig(filtered, output_dir / "candidates.csv")
    write_csv_utf8_sig(filtered, output_dir / "preprocessed_crosswalks.csv")
    return filtered.reset_index(drop=True)



def assess_candidate_implementation(net: Any, net_file: str | Path, row: Any, xml_root: ET.Element, max_match_distance_m: float, require_tls: bool) -> dict[str, Any]:
    cw_id = str(getattr(row, "crosswalk_id"))
    lon = float(getattr(row, "longitude"))
    lat = float(getattr(row, "latitude"))
    
    try:
        cw_metadata = discover_network_metadata_from_net(
            net,
            net_file,
            lon=lon,
            lat=lat,
            cw_id=cw_id,
            xml_root=xml_root,
        )
        
        target_xy = net.convertLonLat2XY(lon, lat)
        crossing_edge = net.getEdge(cw_metadata["crossing_edge"])
        match_distance_m = float(distance_to_edge_shape(crossing_edge, target_xy))
        
        tls_id = cw_metadata.get("tls_id")
        
        ped_val = validate_pedestrian_connectivity(net_file, cw_metadata, cw_id=cw_id, net=net)
        
        if match_distance_m > max_match_distance_m or (require_tls and not tls_id):
            return {
                "implementation_status": "crossing_reconstruction_required",
                "initial_match_distance_m": match_distance_m,
                "repaired_match_distance_m": None,
                "tls_id": tls_id,
                "crossing_edge": cw_metadata["crossing_edge"],
                "validation_status": ped_val.get("validation_status", ""),
                "repair_attempted": False,
                "repair_success": False,
                "reconstruction_needed": True,
                "final_usable_for_simulation": False,
                "failure_reason": "distance_exceeded" if match_distance_m > max_match_distance_m else "missing_tls",
                "repair_suggestion": "netedit/plain XML/additional crossing reconstruction required",
                "cw_metadata": cw_metadata,
                "ped_val": ped_val,
                "original_from_edge": ped_val.get("from_edge", ""),
                "original_to_edge": ped_val.get("to_edge", ""),
                "repaired_from_edge": "",
                "repaired_to_edge": "",
            }
        
        if ped_val["path_exists"] and ped_val["path_uses_crossing"]:
            return {
                "implementation_status": "ready_for_simulation",
                "initial_match_distance_m": match_distance_m,
                "repaired_match_distance_m": match_distance_m,
                "tls_id": tls_id,
                "crossing_edge": cw_metadata["crossing_edge"],
                "validation_status": ped_val.get("validation_status", ""),
                "repair_attempted": False,
                "repair_success": False,
                "reconstruction_needed": False,
                "final_usable_for_simulation": True,
                "failure_reason": "",
                "repair_suggestion": "",
                "cw_metadata": cw_metadata,
                "ped_val": ped_val,
                "original_from_edge": ped_val.get("from_edge", ""),
                "original_to_edge": ped_val.get("to_edge", ""),
                "repaired_from_edge": "",
                "repaired_to_edge": "",
            }
            
        repair_result = attempt_route_repair(net, cw_metadata["crossing_edge"])
        if repair_result:
            cw_metadata["ped_route"]["from_edge"] = repair_result["from_edge"]
            cw_metadata["ped_route"]["to_edge"] = repair_result["to_edge"]
            
            new_ped_val = validate_pedestrian_connectivity(net_file, cw_metadata, cw_id=cw_id, net=net)
            return {
                "implementation_status": "route_repair_success",
                "initial_match_distance_m": match_distance_m,
                "repaired_match_distance_m": match_distance_m,
                "tls_id": tls_id,
                "crossing_edge": cw_metadata["crossing_edge"],
                "validation_status": new_ped_val.get("validation_status", ""),
                "repair_attempted": True,
                "repair_success": True,
                "reconstruction_needed": False,
                "final_usable_for_simulation": True,
                "failure_reason": "",
                "repair_suggestion": "",
                "cw_metadata": cw_metadata,
                "ped_val": new_ped_val,
                "original_from_edge": ped_val.get("from_edge", ""),
                "original_to_edge": ped_val.get("to_edge", ""),
                "repaired_from_edge": repair_result["from_edge"],
                "repaired_to_edge": repair_result["to_edge"],
            }
        else:
            return {
                "implementation_status": "route_repair_failed",
                "initial_match_distance_m": match_distance_m,
                "repaired_match_distance_m": None,
                "tls_id": tls_id,
                "crossing_edge": cw_metadata["crossing_edge"],
                "validation_status": ped_val.get("validation_status", ""),
                "repair_attempted": True,
                "repair_success": False,
                "reconstruction_needed": False,
                "final_usable_for_simulation": False,
                "failure_reason": ped_val.get("invalid_reason", "path_missing_crossing_edge"),
                "repair_suggestion": "from/to edge selection or walkingarea connectivity repair required",
                "cw_metadata": cw_metadata,
                "ped_val": ped_val,
                "original_from_edge": ped_val.get("from_edge", ""),
                "original_to_edge": ped_val.get("to_edge", ""),
                "repaired_from_edge": "",
                "repaired_to_edge": "",
            }
    except Exception as e:
        return {
            "implementation_status": "crossing_reconstruction_required",
            "initial_match_distance_m": None,
            "repaired_match_distance_m": None,
            "tls_id": None,
            "crossing_edge": None,
            "validation_status": "error",
            "repair_attempted": False,
            "repair_success": False,
            "reconstruction_needed": True,
            "final_usable_for_simulation": False,
            "failure_reason": str(e),
            "repair_suggestion": "netedit/plain XML/additional crossing reconstruction required",
            "cw_metadata": {},
            "ped_val": {},
            "original_from_edge": "",
            "original_to_edge": "",
            "repaired_from_edge": "",
            "repaired_to_edge": "",
        }


def build_integrated_network_manifest(
    selected_df: pd.DataFrame,
    nets_dir: str | Path,
    output_dir: str | Path,
    force: bool,
    admin_polygon_path: str | Path,
    buffer_m: float,
    corridor_whitelist: list[str] | None = None,
    network_mode: str = "expanded",
    match_distance_threshold_m: float = MATCH_DISTANCE_THRESHOLD_M,
    from_registry: bool = False,
    registry_path: str | Path | None = None,
    registry_mode: str = "required",
    registry_network_version: str | None = None,
    t2_path: str | Path | None = None,
) -> tuple[Path, Path, pd.DataFrame]:
    nets_dir = Path(nets_dir)
    output_dir = Path(output_dir)
    selected_df = _prepare_crosswalk_id_columns(selected_df)
    integrated_dir = nets_dir / INTEGRATED_DIRNAME
    integrated_dir.mkdir(parents=True, exist_ok=True)
    mean_lat = float(selected_df["latitude"].mean())
    mean_lon = float(selected_df["longitude"].mean())
    metadata, warnings = build_network(
        INTEGRATED_DIRNAME,
        mean_lat,
        mean_lon,
        integrated_dir,
        force=force,
        network_mode=network_mode,
        admin_polygon_path=str(admin_polygon_path),
        buffer_m=buffer_m,
        corridor_whitelist=corridor_whitelist,
    )
    if warnings:
        write_csv_utf8_sig(pd.DataFrame(warnings), output_dir / "network_mode_warnings.csv")

    # Base network (SHA256 anchor for canonical_network_version — always network.net.xml).
    # Runtime net_file may become network_with_signal.net.xml later if selected
    # crosswalks require synthetic TLS injection.
    base_net = integrated_dir / "network.net.xml"
    signal_net = integrated_dir / "network_with_signal.net.xml"

    manifest_rows: list[dict[str, Any]] = []
    excluded_rows: list[dict[str, Any]] = []
    recovered_source_path = ""
    registry_enabled = from_registry and registry_mode != "off" and registry_path

    # Build a run-local registry whose canonical_network_version matches the
    # just-generated runtime network.  This resolves the mismatch caused by
    # netconvert non-determinism producing a different net hash each run.
    runtime_registry_path: Path | None = None
    if registry_enabled:
        runtime_registry_path = output_dir / "runtime_registry.csv"
        try:
            _build_runtime_registry(
                integrated_dir=integrated_dir,
                global_registry_path=Path(registry_path),  # type: ignore[arg-type]
                network_mode=network_mode,
                buffer_m=buffer_m,
                output_path=runtime_registry_path,
            )
        except Exception as exc:
            if registry_mode == "required":
                raise
            print(f"[registry] run-local registry build failed: {exc}")
            runtime_registry_path = None

    if registry_enabled and runtime_registry_path and runtime_registry_path.exists():
        try:
            manifest_rows, excluded_rows, recovered_source_path, mapping_rows = _build_manifest_rows_from_registry(
                selected_df=selected_df,
                registry_path=runtime_registry_path,
                integrated_dir=integrated_dir,
                network_mode=network_mode,
                buffer_m=buffer_m,
                target_network_version=registry_network_version,
                output_dir=output_dir,
            )
        except Exception as exc:
            if registry_mode == "required":
                raise
            if registry_mode == "prefer":
                print(f"[registry] fallback to dynamic rematch: {exc}")

    # For registry path: inject synthetic TLS only for the selected crosswalks
    # that require it (installation_assumption == "synthetic_pedestrian_signal").
    # For a 119055-only run the existing TLS joinedS_11252413259_11252413260
    # is used, so selected_tier2_ces will be empty and network_with_signal.net.xml
    # is NOT created / overwritten for this run.
    if manifest_rows and registry_enabled:
        net_file, manifest_rows = _inject_selected_synthetic_tls(
            manifest_rows=manifest_rows,
            base_net=base_net,
            signal_net=signal_net,
        )
    else:
        # Dynamic fallback: preserve legacy behaviour — use pre-existing global
        # network_with_signal.net.xml if present, otherwise base network.
        net_file = signal_net if signal_net.exists() else base_net

    allow_dynamic_fallback = (not registry_enabled) or (registry_mode == "prefer")
    if not manifest_rows and allow_dynamic_fallback:
        net = read_net(net_file)
        xml_root = ET.parse(net_file).getroot()
        tl_logic_ids = _net_tllogic_ids(net_file)
        connection_tl_ids = _net_connection_tl_ids(net_file)
        recovered_candidates, recovered_source_path = load_recovered_candidates(output_dir)

        for row in selected_df.itertuples(index=False):
            cw_id = str(getattr(row, "crosswalk_id"))

            assessment = assess_candidate_implementation(net, net_file, row, xml_root, match_distance_threshold_m, True)
            recovered_row = recovered_candidates.get(cw_id)
            assessment, override_applied, override_failure_reason = apply_recovered_candidate_override(
                assessment=assessment,
                recovered_row=recovered_row,
                net=net,
                tl_logic_ids=tl_logic_ids,
                connection_tl_ids=connection_tl_ids,
            )

            status = str(assessment.get("implementation_status") or "")
            if not bool(assessment.get("final_usable_for_simulation", False)):
                original_tls_id = str(assessment.get("tls_id") or assessment.get("cw_metadata", {}).get("tls_id") or "")
                if recovered_row and override_failure_reason.startswith("recovered_override_failed_"):
                    rejected_reason = override_failure_reason
                elif status == "crossing_reconstruction_required":
                    if original_tls_id and original_tls_id not in tl_logic_ids:
                        rejected_reason = "ghost_tls_no_tlLogic"
                    elif not original_tls_id:
                        rejected_reason = "missing_tls_id_or_controlled_tl"
                    else:
                        rejected_reason = str(assessment.get("failure_reason") or "crossing_reconstruction_required")
                elif status == "route_repair_failed":
                    rejected_reason = "route_repair_failed"
                else:
                    rejected_reason = str(assessment.get("failure_reason") or status or "excluded_precheck")
                excluded_rows.append(
                    {
                        "smart_crosswalk_id": cw_id,
                        "requested_crosswalk_id": str(getattr(row, "requested_crosswalk_id", cw_id)),
                        "canonical_crosswalk_id": str(getattr(row, "canonical_crosswalk_id", _normalize_crosswalk_id(cw_id))),
                        "original_tls_id": original_tls_id,
                        "rejected_reason": rejected_reason,
                        "tlLogic_exists_in_net_xml": bool(original_tls_id and original_tls_id in tl_logic_ids),
                        "exists_in_traci_getIDList": _to_bool(recovered_row.get("resolved_exists_in_traci", False)) if recovered_row else False,
                        "action_taken": "excluded_from_manifest_precheck",
                        "implementation_status": status,
                        "failure_reason": str(assessment.get("failure_reason") or ""),
                        "recovery_status": str(assessment.get("recovery_status") or ""),
                        "recovery_source": str(assessment.get("recovery_source") or ""),
                        "recovery_distance_m": assessment.get("recovery_distance_m"),
                        "matched_radius_m": assessment.get("matched_radius_m"),
                        "original_failure_reason": str(assessment.get("original_failure_reason") or assessment.get("failure_reason") or ""),
                        "override_reason": str(assessment.get("override_reason") or ""),
                    }
                )
                continue

            cw_metadata = assessment["cw_metadata"]
            manifest_rows.append(
                {
                    "crosswalk_id": cw_id,
                    "requested_crosswalk_id": str(getattr(row, "requested_crosswalk_id", cw_id)),
                    "source_crosswalk_id": str(getattr(row, "source_crosswalk_id", cw_id)),
                    "canonical_crosswalk_id": str(getattr(row, "canonical_crosswalk_id", _normalize_crosswalk_id(cw_id))),
                    "registry_crosswalk_id": str(getattr(row, "registry_crosswalk_id", cw_id)),
                    "admin_dong": getattr(row, "admin_dong"),
                    "dong_name": getattr(row, "dong_name"),
                    "longitude": float(getattr(row, "longitude")),
                    "latitude": float(getattr(row, "latitude")),
                    "lane_count": float(getattr(row, "lane_count")),
                    "max_speed_kph": float(getattr(row, "max_speed_kph")),
                    "elderly_ratio": float(getattr(row, "elderly_ratio")),
                    "accident_count": float(getattr(row, "accident_count")),
                    "estimated_aadt": getattr(row, "estimated_aadt", 0.0),
                    "crossing_length_m": getattr(row, "crossing_length_m", getattr(row, "crosswalk_length", 0.0)),
                    "ped_green_base": getattr(row, "ped_green_base", 0.0),
                    "ped_green_elderly": getattr(row, "ped_green_elderly", 0.0),
                    "risk_score": getattr(row, "risk_score", getattr(row, "priority_score", 0.0)),
                    "inside_junggu_boundary": True,
                    "inside_analysis_area": True,
                    "inside_smart_target_area": True,
                    "match_distance_m": assessment["initial_match_distance_m"],
                    "implementation_status": assessment["implementation_status"],
                    "repair_attempted": assessment["repair_attempted"],
                    "repair_success": assessment["repair_success"],
                    "reconstruction_needed": assessment["reconstruction_needed"],
                    "final_usable_for_simulation": assessment["final_usable_for_simulation"],
                    "original_from_edge": assessment["original_from_edge"],
                    "original_to_edge": assessment["original_to_edge"],
                    "repaired_from_edge": assessment["repaired_from_edge"],
                    "repaired_to_edge": assessment["repaired_to_edge"],
                    "path_uses_crossing": assessment["ped_val"].get("path_uses_crossing", False),
                    "validation_status": assessment["validation_status"],
                    "recovery_status": assessment.get("recovery_status", ""),
                    "recovery_source": assessment.get("recovery_source", ""),
                    "recovery_distance_m": assessment.get("recovery_distance_m"),
                    "matched_radius_m": assessment.get("matched_radius_m"),
                    "original_failure_reason": assessment.get("original_failure_reason", ""),
                    "override_reason": assessment.get("override_reason", ""),
                    **cw_metadata,
                }
            )

    if not manifest_rows:
        mapping_preview = []
        if "mapping_rows" in locals() and mapping_rows:
            mapping_preview = mapping_rows[:5]
        raise ValueError(
            "실행 가능한 integrated_selected 후보가 없습니다.\n"
            f"- mapping_preview={mapping_preview}\n"
            "- requested/source/canonical/registry 매핑과 runnable_for_signal_extension 값을 확인하세요.\n"
            "- excluded_integrated_candidates.csv 및 crosswalk_id_mapping.csv 를 확인하세요."
        )

    manifest = {
        "mode": "integrated_selected",
        "net_file": str(net_file),
        "network_metadata": metadata,
        "smart_crosswalk_ids": [str(row["crosswalk_id"]) for row in manifest_rows],
        "crosswalks": manifest_rows,
        "excluded_crosswalks": excluded_rows,
        "recovered_source_path": recovered_source_path,
    }
    manifest_path = integrated_dir / MANIFEST_FILENAME
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    write_csv_utf8_sig(pd.DataFrame(manifest_rows), output_dir / "smart_crosswalk_manifest.csv")
    write_csv_utf8_sig(pd.DataFrame(excluded_rows), output_dir / "excluded_integrated_candidates_precheck.csv")

    # No-SUMO pre-flight: verify registry/net.xml/manifest consistency before simulation.
    _validate_no_sumo_manifest(
        integrated_dir=integrated_dir,
        runtime_registry_path=runtime_registry_path,
        manifest_path=manifest_path,
        selected_rows=manifest_rows,
    )

    return manifest_path, integrated_dir, pd.DataFrame(manifest_rows)

def check_valid_smart_crosswalks(
    t2_path: str | Path,
    output_dir: str | Path,
    admin_polygon_path: str | Path,
    nets_dir: str | Path,
    max_match_distance_m: float = 50.0,
    require_tls: bool = True,
    num_valid_crosswalks: int | None = None,
    buffer_m: float = 1000.0,
    corridor_whitelist: list[str] | None = None,
    network_mode: str = "expanded",
) -> None:
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    nets_dir = Path(nets_dir)
    
    all_features = load_crosswalk_features(t2_path)
    
    all_features["inside_junggu_boundary"] = all_features.apply(
        lambda row: point_in_geojson(float(row["longitude"]), float(row["latitude"]), admin_polygon_path),
        axis=1,
    )
    
    if "risk_score" in all_features.columns:
        all_features["priority_score"] = all_features["risk_score"]
    else:
        # compute simple priority_score if risk_score not present
        all_features["priority_score"] = all_features.get("accident_count", 0).astype(float) * 2 + \
                                         all_features.get("elderly_ratio", 0).astype(float) * 10
    
    # 1. all_junggu_crosswalk_candidates.csv
    junggu_candidates = all_features[all_features["inside_junggu_boundary"]].copy()
    write_csv_utf8_sig(junggu_candidates, output_dir / "all_junggu_crosswalk_candidates.csv")
    
    # 2. candidate_priority.csv
    priority_df = junggu_candidates.sort_values("priority_score", ascending=False).copy()
    write_csv_utf8_sig(priority_df, output_dir / "candidate_priority.csv")
    
    integrated_dir = nets_dir / INTEGRATED_DIRNAME
    integrated_dir.mkdir(parents=True, exist_ok=True)
    mean_lat = float(junggu_candidates["latitude"].mean())
    mean_lon = float(junggu_candidates["longitude"].mean())
    metadata, warnings = build_network(
        INTEGRATED_DIRNAME,
        mean_lat,
        mean_lon,
        integrated_dir,
        force=False,
        network_mode=network_mode,
        admin_polygon_path=str(admin_polygon_path),
        buffer_m=buffer_m,
        corridor_whitelist=corridor_whitelist,
    )
    
    _signal_net_file2 = integrated_dir / "network_with_signal.net.xml"
    net_file = _signal_net_file2 if _signal_net_file2.exists() else integrated_dir / "network.net.xml"
    if not net_file.exists():
        raise FileNotFoundError(f"네트워크 파일이 없습니다: {net_file}")

    net = read_net(net_file)
    xml_root = ET.parse(net_file).getroot()

    audit_results = []
    
    for row in junggu_candidates.itertuples(index=False):
        assessment = assess_candidate_implementation(net, net_file, row, xml_root, max_match_distance_m, require_tls)
        ped_val = assessment.get("ped_val", {})
        
        audit_results.append({
            "crosswalk_id": str(getattr(row, "crosswalk_id")),
            "lon": float(getattr(row, "longitude")),
            "lat": float(getattr(row, "latitude")),
            "dong_name": getattr(row, "dong_name"),
            "admin_dong": getattr(row, "admin_dong"),
            "priority_score": getattr(row, "priority_score", 0),
            "inside_junggu_boundary": getattr(row, "inside_junggu_boundary", True),
            "implementation_status": assessment["implementation_status"],
            "initial_match_distance_m": assessment["initial_match_distance_m"],
            "repaired_match_distance_m": assessment["repaired_match_distance_m"],
            "crossing_edge": assessment["crossing_edge"],
            "crossing_lon": assessment["cw_metadata"].get("crossing_lon"),
            "crossing_lat": assessment["cw_metadata"].get("crossing_lat"),
            "tls_id": assessment["tls_id"],
            "ped_link_indices": str(assessment["cw_metadata"].get("ped_link_indices", [])),
            "from_edge": ped_val.get("from_edge", ""),
            "to_edge": ped_val.get("to_edge", ""),
            "crossing_edge_exists": ped_val.get("crossing_edge_exists", False),
            "from_edge_exists": ped_val.get("from_edge_exists", False),
            "to_edge_exists": ped_val.get("to_edge_exists", False),
            "from_edge_allows_pedestrian": ped_val.get("from_edge_allows_pedestrian", False),
            "to_edge_allows_pedestrian": ped_val.get("to_edge_allows_pedestrian", False),
            "incoming_walkingarea_count": ped_val.get("incoming_walkingarea_count", 0),
            "outgoing_walkingarea_count": ped_val.get("outgoing_walkingarea_count", 0),
            "path_exists": ped_val.get("path_exists", False),
            "path_uses_crossing": ped_val.get("path_uses_crossing", False),
            "path_uses_walkingarea": ped_val.get("path_uses_walkingarea", False),
            "path_cost": ped_val.get("path_cost", math.nan),
            "path_edge_count": ped_val.get("path_edge_count", 0),
            "validation_status": assessment["validation_status"],
            "repair_attempted": assessment["repair_attempted"],
            "repair_success": assessment["repair_success"],
            "reconstruction_needed": assessment["reconstruction_needed"],
            "final_usable_for_simulation": assessment["final_usable_for_simulation"],
            "failure_reason": assessment["failure_reason"],
            "repair_suggestion": assessment["repair_suggestion"],
        })
            
    audit_df = pd.DataFrame(audit_results)
    
    # 3. crosswalk_sumo_implementation_audit.csv
    write_csv_utf8_sig(audit_df, output_dir / "crosswalk_sumo_implementation_audit.csv")
    
    # 4. valid_integrated_smart_crosswalks.csv
    valid_df = audit_df[audit_df["final_usable_for_simulation"] == True].copy()
    write_csv_utf8_sig(valid_df, output_dir / "valid_integrated_smart_crosswalks.csv")
    
    # 5. repair_needed_crosswalks.csv
    repair_df = audit_df[audit_df["implementation_status"].isin(["route_repair_failed", "crossing_reconstruction_required"])].copy()
    write_csv_utf8_sig(repair_df, output_dir / "repair_needed_crosswalks.csv")
    
    if num_valid_crosswalks:
        print(f"Top {num_valid_crosswalks} valid candidates:")
        print(valid_df[["crosswalk_id", "implementation_status", "initial_match_distance_m", "tls_id"]].head(num_valid_crosswalks))

def _ped_vtypes(parent: ET.Element, normal_speed: float, elderly_speed: float, elderly_startup_delay: float) -> None:
    ET.SubElement(
        parent,
        "vType",
        {
            "id": "adult",
            "vClass": "pedestrian",
            "minGap": "0.25",
            "width": "0.5",
            "length": "0.25",
            "maxSpeed": f"{normal_speed:.3f}",
            "speedDev": "0.1",
        },
    )
    ET.SubElement(
        parent,
        "vType",
        {
            "id": "elderly",
            "vClass": "pedestrian",
            "minGap": "0.25",
            "width": "0.5",
            "length": "0.25",
            "maxSpeed": f"{elderly_speed:.3f}",
            "speedDev": "0.15",
            "startupDelay": f"{elderly_startup_delay:.2f}",
            "color": "255,0,0",
        },
    )


def generate_integrated_pedestrian_demand(
    selected_rows: list[dict[str, Any]],
    manifest_rows: list[dict[str, Any]],
    output_file: str | Path,
    sim_duration: int,
    seed: int,
) -> dict[str, int]:
    manifest_by_id = {str(row["crosswalk_id"]): row for row in manifest_rows}
    root = ET.Element("routes")
    if selected_rows:
        first = selected_rows[0]["params"]
        _ped_vtypes(
            root,
            float(first["normal_ped_speed_mps"]),
            float(first["elderly_ped_speed_mps"]),
            float(first["elderly_startup_delay_sec"]),
        )
    count_by_crosswalk: dict[str, int] = {}
    for selected in selected_rows:
        cw_id = str(selected["crosswalk_id"])
        source_cw_id = str(selected.get("source_crosswalk_id") or cw_id)
        canonical_cw_id = _normalize_crosswalk_id(selected.get("canonical_crosswalk_id") or cw_id)
        registry_cw_id = str(selected.get("registry_crosswalk_id") or "")
        seed_offset = _stable_crosswalk_seed_offset(
            canonical_cw_id or registry_cw_id or source_cw_id or cw_id
        )
        params = selected["params"]
        manifest_row = manifest_by_id[cw_id]
        route = manifest_row["ped_route"]
        rng = np.random.default_rng(seed + 1000 + seed_offset)
        elderly_ratio = float(params["elderly_ratio"])
        mean_gap = float(params["ped_mean_gap_sec"])
        t = float(rng.exponential(mean_gap))
        ped_idx = 0
        while t < sim_duration:
            person_type = "elderly" if rng.random() < elderly_ratio else "adult"
            person = ET.SubElement(
                root,
                "person",
                {
                    "id": f"ped_{cw_id}_{ped_idx}",
                    "depart": f"{t:.2f}",
                    "type": person_type,
                },
            )
            ET.SubElement(
                person,
                "walk",
                {
                    "from": str(route["from_edge"]),
                    "to": str(route["to_edge"]),
                },
            )
            t += float(rng.exponential(mean_gap))
            ped_idx += 1
        count_by_crosswalk[cw_id] = ped_idx
        selected["source_crosswalk_id"] = source_cw_id
        selected["canonical_crosswalk_id"] = canonical_cw_id
        selected["registry_crosswalk_id"] = registry_cw_id
    ET.indent(root, space="  ")
    ET.ElementTree(root).write(output_file, encoding="utf-8", xml_declaration=True)
    return count_by_crosswalk


def build_shared_calibrator_additional(
    manifest_rows: list[dict[str, Any]],
    counts_df: pd.DataFrame,
    output_file: Path,
) -> tuple[Path | None, list[dict[str, Any]]]:
    if counts_df.empty:
        return None, []
    target_edges: set[str] = set()
    for row in manifest_rows:
        target_edges.update(str(edge_id) for edge_id in row.get("vehicle_conflict_edges", []))
        target_edges.update(
            lane_id.rsplit("_", 1)[0]
            for lane_id in row.get("approach_lanes", [])
            if isinstance(lane_id, str) and "_" in lane_id
        )
    if not target_edges:
        return None, []
    filtered = counts_df[counts_df["edge_id"].astype(str).isin(target_edges)].copy()
    if filtered.empty:
        return None, []

    root = ET.Element("additional")
    audit_rows: list[dict[str, Any]] = []
    for idx, row in enumerate(filtered.itertuples(index=False), start=1):
        begin = max(0.0, float(row.start_time_sec))
        end = max(begin + 1.0, float(row.end_time_sec))
        veh_per_hour = float(row.volume) * (3600.0 / max(end - begin, 1.0))
        ET.SubElement(
            root,
            "calibrator",
            {
                "id": f"cal_{idx}",
                "edge": str(row.edge_id),
                "pos": "5",
                "begin": f"{begin:.2f}",
                "end": f"{end:.2f}",
                "vehsPerHour": f"{veh_per_hour:.3f}",
                "type": "passenger",
            },
        )
        audit_rows.append(
            {
                "edge_id": str(row.edge_id),
                "demand_source": "observed_15min_shared",
                "volume": float(row.volume),
                "confidence_level": "high",
            }
        )
    ET.indent(root, space="  ")
    ET.ElementTree(root).write(output_file, encoding="utf-8", xml_declaration=True)
    return output_file, audit_rows


def generate_integrated_demand(
    selected_df: pd.DataFrame,
    manifest_path: str | Path,
    output_dir: str | Path,
    seeds: tuple[int, ...],
    sim_duration: int,
    warmup: int,
    step_length: float,
    demand_profile: str,
    traffic_counts_csv: str | Path | None,
    representative_day_id: str | None,
    model_parameters_path: str | Path | None,
    vehicle_only: bool = False,
    sensitivity_config: dict[str, Any] | None = None,
) -> pd.DataFrame:
    output_dir = Path(output_dir)
    manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    manifest_rows = manifest.get("crosswalks", [])
    counts_df = load_traffic_counts(traffic_counts_csv, representative_day_id)
    model_params = load_model_parameters(model_parameters_path)
    integrated_dir = Path(manifest_path).parent
    net_file = Path(manifest["net_file"])
    print(f"[generate_demand] runtime net: {net_file}")
    demand_rows: list[dict[str, Any]] = []
    audit_rows: list[dict[str, Any]] = []
    invalid_ped_rows: list[dict[str, Any]] = []
    ped_connectivity_rows: list[dict[str, Any]] = []

    manifest_by_id = {str(row["crosswalk_id"]): row for row in manifest_rows}
    selected_rows = selected_df.to_dict(orient="records")

    per_crosswalk_observed: dict[str, float | None] = {}
    for row in selected_rows:
        cw_id = str(row["crosswalk_id"])
        observed_rate, matched_audits = observed_vehicle_rate_for_candidate(
            manifest_by_id[cw_id],
            counts_df,
            sim_duration,
        )
        per_crosswalk_observed[cw_id] = observed_rate
        audit_rows.extend(matched_audits)
        ped_validation = validate_pedestrian_connectivity(net_file, manifest_by_id[cw_id], cw_id=cw_id)
        ped_connectivity_rows.append({"stage": "pre_route_generation", "seed": "", **ped_validation})
        if ped_validation["validation_status"] != "valid" and not vehicle_only:
            invalid_ped_rows.append(
                {
                    "crosswalk_id": cw_id,
                    "seed": "",
                    "reason": ped_validation["invalid_reason"] or "invalid_pedestrian_candidate",
                    "detail": "pre_route_generation connectivity audit failed",
                }
            )

    shared_calibrator_path, shared_audits = build_shared_calibrator_additional(
        manifest_rows,
        counts_df,
        integrated_dir / "calibrator_shared.add.xml",
    )
    audit_rows.extend(shared_audits)

    for seed in seeds:
        selected_with_params: list[dict[str, Any]] = []
        shared_vehicle_rates: list[float] = []
        for row in selected_rows:
            cw_id = str(row["crosswalk_id"])
            params = get_demand_params(
                pd.Series(row),
                seed,
                demand_profile,
                model_params,
                per_crosswalk_observed[cw_id],
                sensitivity_config,
            )
            selected_with_params.append(
                {
                    "crosswalk_id": cw_id,
                    "source_crosswalk_id": str(row.get("source_crosswalk_id") or cw_id),
                    "canonical_crosswalk_id": _normalize_crosswalk_id(row.get("canonical_crosswalk_id") or cw_id),
                    "registry_crosswalk_id": str(row.get("registry_crosswalk_id") or ""),
                    "params": params,
                }
            )
            shared_vehicle_rates.append(float(params["veh_per_hour"]))
        shared_veh_per_hour = float(np.nanmean(shared_vehicle_rates)) if shared_vehicle_rates else 1.0
        vehicle_file = integrated_dir / f"routes_seed{seed}.rou.xml"
        pedestrian_file = integrated_dir / f"peds_seed{seed}.rou.xml"
        generate_vehicle_routes(
            {"veh_per_hour": shared_veh_per_hour},
            net_file,
            vehicle_file,
            sim_duration,
            seed,
        )
        if vehicle_only:
            ET.ElementTree(ET.Element("routes")).write(
                pedestrian_file,
                encoding="utf-8",
                xml_declaration=True,
            )
            ped_counts: dict[str, int] = {str(row["crosswalk_id"]): 0 for row in selected_rows}
        else:
            ped_counts = generate_integrated_pedestrian_demand(
                selected_with_params,
                manifest_rows,
                pedestrian_file,
                sim_duration,
                seed,
            )
        additional_files = [str(shared_calibrator_path.resolve())] if shared_calibrator_path else None
        for scenario in ("baseline", "smart_selected"):
            write_sumocfg(
                net_file,
                vehicle_file,
                pedestrian_file,
                integrated_dir / f"{scenario}_seed{seed}.sumocfg",
                sim_duration,
                warmup,
                step_length,
                additional_files,
                vehicle_only=vehicle_only,
            )
        for row in selected_rows:
            cw_id = str(row["crosswalk_id"])
            params = next(item["params"] for item in selected_with_params if item["crosswalk_id"] == cw_id)
            ped_validation = validate_pedestrian_connectivity(net_file, manifest_by_id[cw_id], cw_id=cw_id)
            ped_connectivity_rows.append(
                {
                    "stage": "post_route_generation",
                    "seed": seed,
                    "generated_pedestrian_count": ped_counts.get(cw_id, 0),
                    **ped_validation,
                }
            )
            demand_rows.append(
                {
                    "crosswalk_id": cw_id,
                    "source_crosswalk_id": str(row.get("source_crosswalk_id") or cw_id),
                    "canonical_crosswalk_id": _normalize_crosswalk_id(row.get("canonical_crosswalk_id") or cw_id),
                    "registry_crosswalk_id": str(row.get("registry_crosswalk_id") or ""),
                    "seed": seed,
                    "demand_profile": params["demand_profile"],
                    "veh_per_hour": params["veh_per_hour"],
                    "veh_source": params["veh_source"],
                    "ped_lambda": params["ped_lambda"],
                    "elderly_ratio": params["elderly_ratio"],
                    "ped_count": ped_counts.get(cw_id, 0),
                    "pedestrian_arrival_rate_multiplier": params["pedestrian_arrival_rate_multiplier"],
                    "vehicle_volume_multiplier": params["vehicle_volume_multiplier"],
                    "walking_speed_profile": params["walking_speed_profile"],
                    "shared_vehicle_route_rate_per_hour": shared_veh_per_hour,
                }
            )

    demand_df = pd.DataFrame(demand_rows)
    write_csv_utf8_sig(demand_df, output_dir / "demand_params.csv")
    write_csv_utf8_sig(pd.DataFrame(audit_rows).drop_duplicates(), output_dir / "demand_source_audit.csv")
    write_csv_utf8_sig(pd.DataFrame(ped_connectivity_rows), output_dir / "pedestrian_connectivity_audit.csv")
    write_csv_utf8_sig(
        pd.DataFrame(invalid_ped_rows).drop_duplicates(),
        output_dir / "invalid_pedestrian_candidates.csv",
    )
    write_csv_utf8_sig(
        pd.DataFrame(invalid_ped_rows).drop_duplicates(),
        output_dir / "invalid_pedestrian_routes.csv",
    )
    return demand_df


def collect_integrated_metrics(
    selected_df: pd.DataFrame,
    manifest_path: str | Path,
    output_dir: str | Path,
    sim_duration: int,
    warmup: int,
    seeds: tuple[int, ...],
    traci_step_length: float,
    traffic_measure_radius_m: float,
    extension_increment: float | None,
    max_extensions: int | None,
    vehicle_arrival_rate_per_hour: float | None,
    saturation_flow_rate_per_hour: float,
    vehicle_arrival_model: str,
    disruption_scenario: str,
    enable_random_disruptions: bool,
    bus_stop_rate_per_hour: float,
    illegal_parking_rate_per_hour: float,
    minor_incident_rate_per_hour: float,
    accident_rate_per_hour: float,
    model_parameters_path: str | Path | None,
    export_fcd: bool = False,
    vehicle_only: bool = False,
    sensitivity_config: dict[str, Any] | None = None,
    enable_risk_event_collection: bool = False,
    risk_event_sample_interval_s: float = 1.0,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    output_dir = Path(output_dir)
    run_name = output_dir.parent.name if output_dir.parent.name else output_dir.name
    seed_for_trace = int(seeds[0]) if seeds else 42
    runtime_trace_path = output_dir / f"runtime_trace_seed{seed_for_trace}.log"
    failure_context_path = output_dir / f"traci_failure_context_seed{seed_for_trace}.json"
    manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    integrated_dir = Path(manifest_path).parent
    net_file = Path(manifest["net_file"])
    print(f"[collect_integrated_metrics] runtime net: {net_file}")
    crosswalk_contexts = manifest.get("crosswalks", [])
    pre_excluded_rows = list(manifest.get("excluded_crosswalks", []))
    smart_target_ids = {str(crosswalk_id) for crosswalk_id in manifest.get("smart_crosswalk_ids", [])}
    validation_seed = int(seeds[0]) if seeds else 42
    validation_sumocfg = integrated_dir / f"baseline_seed{validation_seed}.sumocfg"
    if not validation_sumocfg.exists():
        fallback_sumocfg = next(iter(sorted(integrated_dir.glob("baseline_seed*.sumocfg"))), None)
        if fallback_sumocfg is None:
            raise FileNotFoundError(
                f"TLS 검증용 sumocfg를 찾지 못했습니다: {validation_sumocfg}"
            )
        validation_sumocfg = fallback_sumocfg
    traci_tls_ids = snapshot_traci_trafficlight_ids(
        validation_sumocfg,
        seed=validation_seed,
        step_length=traci_step_length,
    )
    crosswalk_contexts, runtime_excluded_rows = _validate_integrated_tls_candidates(
        crosswalk_contexts,
        net_file,
        traci_tls_ids,
    )
    excluded_rows: list[dict[str, Any]] = []
    for row in pre_excluded_rows:
        original_tls_id = str(row.get("original_tls_id") or "")
        enriched = {
            "smart_crosswalk_id": str(row.get("smart_crosswalk_id") or ""),
            "original_tls_id": original_tls_id,
            "rejected_reason": str(row.get("rejected_reason") or "excluded_precheck"),
            "tlLogic_exists_in_net_xml": bool(row.get("tlLogic_exists_in_net_xml", False)),
            "exists_in_traci_getIDList": bool(original_tls_id and original_tls_id in traci_tls_ids),
            "action_taken": str(row.get("action_taken") or "excluded_from_manifest_precheck"),
        }
        excluded_rows.append(enriched)
    excluded_rows.extend(runtime_excluded_rows)
    excluded_df = pd.DataFrame(
        excluded_rows,
        columns=[
            "smart_crosswalk_id",
            "original_tls_id",
            "rejected_reason",
            "tlLogic_exists_in_net_xml",
            "exists_in_traci_getIDList",
            "action_taken",
        ],
    )
    write_csv_utf8_sig(excluded_df, output_dir / "excluded_integrated_candidates.csv")
    valid_crosswalk_ids = {str(ctx.get("crosswalk_id")) for ctx in crosswalk_contexts}
    smart_target_ids = {cw_id for cw_id in smart_target_ids if cw_id in valid_crosswalk_ids}
    if not crosswalk_contexts:
        raise ValueError(
            "실행 가능한 integrated_selected 후보가 없습니다. excluded_integrated_candidates.csv를 확인하세요."
        )

    selected_map = {
        str(row["crosswalk_id"]): row
        for row in selected_df.to_dict(orient="records")
        if str(row["crosswalk_id"]) in valid_crosswalk_ids
    }
    union_metadata = {
        "approach_lanes": sorted(
            {
                str(lane_id)
                for ctx in crosswalk_contexts
                for lane_id in ctx.get("approach_lanes", [])
            }
        ),
        "vehicle_conflict_edges": sorted(
            {
                str(edge_id)
                for ctx in crosswalk_contexts
                for edge_id in ctx.get("vehicle_conflict_edges", [])
            }
        ),
    }

    model_params = load_model_parameters(model_parameters_path)
    per_seed_rows: list[dict[str, Any]] = []
    per_avg_rows: list[dict[str, Any]] = []
    network_seed_rows: list[dict[str, Any]] = []
    network_avg_rows: list[dict[str, Any]] = []
    extension_rows: list[dict[str, Any]] = []
    incident_event_rows: list[dict[str, Any]] = []
    incident_impact_rows: list[dict[str, Any]] = []

    def trace(event: str, **fields: Any) -> None:
        _runtime_log_append(runtime_trace_path, {"event": event, "run_name": run_name, **fields})

    trace("collect_integrated_metrics_enter", seed=seed_for_trace, run_name=run_name)

    for seed in seeds:
        trace("seed_begin", seed=int(seed), run_name=run_name)
        schedule_events = generate_incident_schedule(
            disruption_scenario,
            sim_duration,
            seed,
            union_metadata,
            union_metadata["approach_lanes"],
            model_params,
            enable_random_disruptions,
            bus_stop_rate_per_hour,
            illegal_parking_rate_per_hour,
            minor_incident_rate_per_hour,
            accident_rate_per_hour,
        )
        for event in schedule_events:
            incident_event_rows.append(
                serialize_incident_event(event, "integrated_selected", int(seed), "shared")
            )
        schedule_payload = [
            {
                "incident_id": event.incident_id,
                "event_type": event.event_type,
                "start_time": event.start_time,
                "end_time": event.end_time,
                "affected_edge_ids": list(event.affected_edge_ids),
                "affected_lane_ids": list(event.affected_lane_ids),
                "severity": event.severity,
                "capacity_multiplier": event.capacity_multiplier,
                "speed_multiplier": event.speed_multiplier,
                "blocked_lanes_count": event.blocked_lanes_count,
                "allow_rerouting": event.allow_rerouting,
            }
            for event in schedule_events
        ]
        for scenario in ("baseline", "smart_selected"):
            route_file = integrated_dir / f"routes_seed{seed}.rou.xml"
            ped_file = integrated_dir / f"peds_seed{seed}.rou.xml"
            sumocfg = integrated_dir / f"{scenario}_seed{seed}.sumocfg"
            trace("scenario_begin", scenario=scenario, seed=int(seed), run_name=run_name, sumocfg=str(sumocfg))
            try:
                per_metrics, network_metrics, ext_events, _, incident_impacts, _ = run_simulation_integrated(
                    manifest["net_file"],
                    route_file,
                    ped_file,
                    sumocfg,
                    scenario,
                    crosswalk_contexts,
                    smart_target_ids,
                    sim_duration,
                    warmup,
                    seed,
                    traci_step_length,
                    traffic_measure_radius_m,
                    extension_increment,
                    max_extensions,
                    vehicle_arrival_rate_per_hour,
                    saturation_flow_rate_per_hour,
                    max(1, int(np.nanmean(selected_df["lane_count"]))),
                    vehicle_arrival_model,
                    disruption_scenario,
                    enable_random_disruptions,
                    bus_stop_rate_per_hour,
                    illegal_parking_rate_per_hour,
                    minor_incident_rate_per_hour,
                    accident_rate_per_hour,
                    schedule_payload,
                    model_parameters_path,
                    export_fcd,
                    output_dir,
                    runtime_trace_path,
                    failure_context_path,
                    vehicle_only,
                    sensitivity_config,
                    enable_risk_event_collection=enable_risk_event_collection,
                    risk_event_sample_interval_s=risk_event_sample_interval_s,
                )
            except Exception as exc:
                trace(
                    "scenario_exception",
                    scenario=scenario,
                    seed=int(seed),
                    run_name=run_name,
                    exception_type=type(exc).__name__,
                    exception_message=str(exc),
                )
                raise
            for row in per_metrics:
                cw_id = str(row["crosswalk_id"])
                selected_row = selected_map[cw_id]
                signal_timing = compute_signal_timing(selected_row)
                per_seed_rows.append(
                    {
                        "crosswalk_id": cw_id,
                        "seed": seed,
                        "admin_dong": selected_row["admin_dong"],
                        "dong_name": selected_row["dong_name"],
                        "scenario": scenario,
                        "raw_accident_count": selected_row["accident_count"],
                        "elderly_ratio": selected_row["elderly_ratio"],
                        "lane_count": selected_row["lane_count"],
                        "max_speed_kph": selected_row["max_speed_kph"],
                        "crossing_length_m": selected_row["crossing_length_m"],
                        "ped_green_base_sec": signal_timing["ped_green"],
                        "risk_score": selected_row["risk_score"],
                        **row,
                    }
                )
            network_seed_rows.append(network_metrics)
            extension_rows.extend(ext_events)
            incident_impact_rows.extend(incident_impacts)
            trace("scenario_end", scenario=scenario, seed=int(seed), run_name=run_name)

    for cw_id, cw_rows in pd.DataFrame(per_seed_rows).groupby("crosswalk_id"):
        for scenario, scenario_rows in cw_rows.groupby("scenario"):
            records = scenario_rows.to_dict(orient="records")
            metric_keys = _numeric_metric_keys(
                records,
                {"crosswalk_id", "seed", "admin_dong", "dong_name", "scenario"},
            )
            avg_metrics = {
                key: _mean_or_nan([float(record.get(key, np.nan)) for record in records])
                for key in metric_keys
            }
            first = records[0]
            per_avg_rows.append(
                {
                    "crosswalk_id": cw_id,
                    "admin_dong": first["admin_dong"],
                    "dong_name": first["dong_name"],
                    "scenario": scenario,
                    **avg_metrics,
                }
            )

    network_seed_df = pd.DataFrame(network_seed_rows)
    if not network_seed_df.empty:
        for scenario, scenario_rows in network_seed_df.groupby("scenario"):
            records = scenario_rows.to_dict(orient="records")
            metric_keys = _numeric_metric_keys(records, {"scenario", "seed"})
            avg_metrics = {
                key: _mean_or_nan([float(record.get(key, np.nan)) for record in records])
                for key in metric_keys
            }
            network_avg_rows.append({"scenario": scenario, **avg_metrics})

    per_seed_df = pd.DataFrame(per_seed_rows)
    per_avg_df = pd.DataFrame(per_avg_rows)
    network_avg_df = pd.DataFrame(network_avg_rows)

    write_csv_utf8_sig(per_seed_df, output_dir / "per_crosswalk_simulation_results_seed.csv")
    write_csv_utf8_sig(per_avg_df, output_dir / "per_crosswalk_simulation_results.csv")
    write_csv_utf8_sig(network_seed_df, output_dir / "network_simulation_summary_seed.csv")
    write_csv_utf8_sig(network_avg_df, output_dir / "network_simulation_summary.csv")
    write_csv_utf8_sig(pd.DataFrame(extension_rows), output_dir / "extension_events_seed.csv")
    write_csv_utf8_sig(
        pd.DataFrame(incident_event_rows).drop_duplicates(), output_dir / "incident_events_seed.csv"
    )
    write_csv_utf8_sig(pd.DataFrame(incident_impact_rows), output_dir / "incident_impact_seed.csv")
    trace("collect_integrated_metrics_exit", seed=seed_for_trace, run_name=run_name)
    return per_seed_df, per_avg_df, network_seed_df, network_avg_df


def generate_integrated_reports(
    selected_df: pd.DataFrame,
    output_dir: str | Path,
    figures_dir: str | Path,
    model_parameters_path: str | Path | None,
) -> dict[str, pd.DataFrame]:
    output_dir = Path(output_dir)
    figures_dir = Path(figures_dir)
    per_crosswalk_summary = pd.read_csv(output_dir / "per_crosswalk_simulation_results.csv")
    network_summary = pd.read_csv(output_dir / "network_simulation_summary.csv")
    per_crosswalk_delta = _build_baseline_delta(per_crosswalk_summary, "crosswalk_id")
    if not network_summary.empty and "scenario_group" not in network_summary.columns:
        network_summary = network_summary.copy()
        network_summary["scenario_group"] = "integrated_network"
    network_delta = _build_baseline_delta(network_summary, "scenario_group")

    write_csv_utf8_sig(per_crosswalk_delta, output_dir / "baseline_vs_smart_summary.csv")
    write_csv_utf8_sig(network_delta, output_dir / "network_baseline_vs_smart_summary.csv")
    _write_delta_figure(per_crosswalk_delta, figures_dir)
    _write_integrated_methodology(
        output_dir,
        selected_df,
        per_crosswalk_delta,
        network_delta,
        model_parameters_path,
    )
    return {
        "per_crosswalk_summary": per_crosswalk_summary,
        "baseline_vs_smart_summary": per_crosswalk_delta,
        "network_simulation_summary": network_summary,
        "network_baseline_vs_smart_summary": network_delta,
    }
