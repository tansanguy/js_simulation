from __future__ import annotations

import json
import math
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
        read_net,
        validate_pedestrian_connectivity,
    )
    from .output_schema import write_csv_utf8_sig
    from .preprocess import load_crosswalk_features
    from .run_simulations import (
        compute_signal_timing,
        generate_incident_schedule,
        run_simulation_integrated,
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
        snapshot_traci_trafficlight_ids,
        serialize_incident_event,
    )


INTEGRATED_DIRNAME = "integrated_selected"
MANIFEST_FILENAME = "smart_crosswalk_manifest.json"
MATCH_DISTANCE_THRESHOLD_M = 50.0


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
    all_features = load_crosswalk_features(t2_path)
    wanted = [str(crosswalk_id) for crosswalk_id in smart_crosswalk_ids]
    matched = all_features[all_features["crosswalk_id"].astype(str).isin(wanted)].copy()
    found = set(matched["crosswalk_id"].astype(str))
    missing = [crosswalk_id for crosswalk_id in wanted if crosswalk_id not in found]
    if missing:
        raise ValueError(f"smart_crosswalk_ids를 찾지 못했습니다: {missing}")
    matched["inside_junggu_boundary"] = matched.apply(
        lambda row: point_in_geojson(float(row["longitude"]), float(row["latitude"]), admin_polygon_path),
        axis=1,
    )
    selected_rows: list[pd.Series] = []
    outside: list[str] = []
    duplicate_inside: list[str] = []
    for cw_id in wanted:
        rows = matched[matched["crosswalk_id"].astype(str) == cw_id].copy()
        inside_rows = rows[rows["inside_junggu_boundary"]].copy()
        if inside_rows.empty:
            outside.append(cw_id)
            continue
        if len(inside_rows) > 1:
            duplicate_inside.append(cw_id)
            continue
        selected_rows.append(inside_rows.iloc[0])
    if outside:
        raise ValueError(f"중구 경계 밖 횡단보도는 integrated_selected 모드에서 사용할 수 없습니다: {outside}")
    if duplicate_inside:
        raise ValueError(f"중구 내부에 중복 행이 있어 선택할 수 없는 crosswalk_id가 있습니다: {duplicate_inside}")
    filtered = pd.DataFrame(selected_rows)
    filtered["_order"] = filtered["crosswalk_id"].astype(str).map({cw_id: idx for idx, cw_id in enumerate(wanted)})
    filtered = filtered.sort_values("_order").drop(columns="_order")
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
) -> tuple[Path, Path, pd.DataFrame]:
    nets_dir = Path(nets_dir)
    output_dir = Path(output_dir)
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

    net_file = integrated_dir / "network.net.xml"
    net = read_net(net_file)
    xml_root = ET.parse(net_file).getroot()
    tl_logic_ids = _net_tllogic_ids(net_file)
    manifest_rows: list[dict[str, Any]] = []
    excluded_rows: list[dict[str, Any]] = []
    
    for row in selected_df.itertuples(index=False):
        cw_id = str(getattr(row, "crosswalk_id"))
        
        assessment = assess_candidate_implementation(net, net_file, row, xml_root, match_distance_threshold_m, True)
        
        status = str(assessment.get("implementation_status") or "")
        if not bool(assessment.get("final_usable_for_simulation", False)):
            original_tls_id = str(assessment.get("tls_id") or assessment.get("cw_metadata", {}).get("tls_id") or "")
            if status == "crossing_reconstruction_required":
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
                    "original_tls_id": original_tls_id,
                    "rejected_reason": rejected_reason,
                    "tlLogic_exists_in_net_xml": bool(original_tls_id and original_tls_id in tl_logic_ids),
                    "exists_in_traci_getIDList": False,
                    "action_taken": "excluded_from_manifest_precheck",
                    "implementation_status": status,
                    "failure_reason": str(assessment.get("failure_reason") or ""),
                }
            )
            continue

        cw_metadata = assessment["cw_metadata"]
        manifest_rows.append(
            {
                "crosswalk_id": cw_id,
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
                **cw_metadata,
            }
        )

    if not manifest_rows:
        raise ValueError(
            "실행 가능한 integrated_selected 후보가 없습니다. excluded_integrated_candidates.csv를 확인하세요."
        )

    manifest = {
        "mode": "integrated_selected",
        "net_file": str(net_file),
        "network_metadata": metadata,
        "smart_crosswalk_ids": [str(row["crosswalk_id"]) for row in manifest_rows],
        "crosswalks": manifest_rows,
        "excluded_crosswalks": excluded_rows,
    }
    manifest_path = integrated_dir / MANIFEST_FILENAME
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    write_csv_utf8_sig(pd.DataFrame(manifest_rows), output_dir / "smart_crosswalk_manifest.csv")
    write_csv_utf8_sig(pd.DataFrame(excluded_rows), output_dir / "excluded_integrated_candidates_precheck.csv")
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
    
    net_file = integrated_dir / "network.net.xml"
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
        params = selected["params"]
        manifest_row = manifest_by_id[cw_id]
        route = manifest_row["ped_route"]
        rng = np.random.default_rng(seed + 1000 + int(cw_id))
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
            selected_with_params.append({"crosswalk_id": cw_id, "params": params})
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
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    output_dir = Path(output_dir)
    manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    integrated_dir = Path(manifest_path).parent
    net_file = Path(manifest["net_file"])
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

    for seed in seeds:
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
                vehicle_only,
                sensitivity_config,
            )
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
