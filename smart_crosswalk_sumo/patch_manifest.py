import re
from pathlib import Path

content = Path("integrated_mode.py").read_text()

old_manifest = re.search(r"def build_integrated_network_manifest\(.*?\)\s*->\s*tuple\[Path, Path, pd\.DataFrame\]:.*?(?=def check_valid_smart_crosswalks)", content, re.DOTALL)
if old_manifest:
    new_manifest = """def build_integrated_network_manifest(
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
    manifest_rows: list[dict[str, Any]] = []
    
    for row in selected_df.itertuples(index=False):
        cw_id = str(getattr(row, "crosswalk_id"))
        
        assessment = assess_candidate_implementation(net, net_file, row, xml_root, match_distance_threshold_m, True)
        
        if assessment["implementation_status"] == "crossing_reconstruction_required":
            match_dist = assessment.get('initial_match_distance_m')
            raise ValueError(
                f"crosswalk_id={cw_id}은 T2 기준 실제 중구 횡단보도이지만, 통합 SUMO 네트워크에서 {match_distance_threshold_m}m 이내 제어 가능한 crossing/TLS를 찾지 못했습니다. "
                f"{match_dist if match_dist is not None else 'N/A'}m 떨어진 crossing에 강제 매칭하면 다른 교차로를 제어하게 되므로 허용하지 않습니다. "
                f"이 후보는 crossing_reconstruction_required 상태로 기록되며, 향후 netedit/plain XML 기반 crossing 보강이 필요합니다."
            )
            
        if assessment["implementation_status"] == "route_repair_failed":
            raise ValueError(
                f"crosswalk_id={cw_id}은 T2 기준 실제 중구 횡단보도이며 {match_distance_threshold_m}m 이내 SUMO crossing/TLS에도 매칭되었습니다. "
                f"그러나 pedestrian route가 해당 crossing_edge를 통과하지 않아 route repair가 필요합니다. "
                f"자동 repair를 시도했으나 실패했으므로 현 버전에서는 시뮬레이션 대상에 포함할 수 없습니다. "
                f"이 후보는 삭제된 것이 아니라 repair_needed_crosswalks.csv에 보정 필요 후보로 기록됩니다."
            )
            
        if assessment["implementation_status"] == "out_of_scope":
            raise ValueError(f"crosswalk_id={cw_id}은 T2 중구 후보군에 없음 또는 중구 밖이므로 제외합니다.")

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

    manifest = {
        "mode": "integrated_selected",
        "net_file": str(net_file),
        "network_metadata": metadata,
        "smart_crosswalk_ids": [str(crosswalk_id) for crosswalk_id in selected_df["crosswalk_id"].astype(str)],
        "crosswalks": manifest_rows,
    }
    manifest_path = integrated_dir / MANIFEST_FILENAME
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    write_csv_utf8_sig(pd.DataFrame(manifest_rows), output_dir / "smart_crosswalk_manifest.csv")
    return manifest_path, integrated_dir, pd.DataFrame(manifest_rows)
\n"""
    content = content[:old_manifest.start()] + new_manifest + content[old_manifest.end():]
    Path("integrated_mode.py").write_text(content)
