from pathlib import Path
content = Path("integrated_mode.py").read_text()

assess_func = """
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
        
        ped_val = validate_pedestrian_connectivity(net_file, cw_metadata, cw_id=cw_id)
        
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
            
            new_ped_val = validate_pedestrian_connectivity(net_file, cw_metadata, cw_id=cw_id)
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

"""

if assess_func not in content:
    content = content.replace("def build_integrated_network_manifest(", assess_func + "\ndef build_integrated_network_manifest(")

Path("integrated_mode.py").write_text(content)
