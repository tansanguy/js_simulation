import re
from pathlib import Path

content = Path("integrated_mode.py").read_text()

old_check = re.search(r"def check_valid_smart_crosswalks\(.*?\)\s*->\s*None:.*?(?=def _ped_vtypes)", content, re.DOTALL)
if old_check:
    new_check = """def check_valid_smart_crosswalks(
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
        all_features["priority_score"] = all_features.get("accident_count", 0).astype(float) * 2 + \\
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
\n"""
    content = content[:old_check.start()] + new_check + content[old_check.end():]
    Path("integrated_mode.py").write_text(content)
