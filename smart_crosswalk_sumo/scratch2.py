import sys
from pathlib import Path
sys.path.append(str(Path.cwd()))
from network_utils import attempt_route_repair, validate_pedestrian_connectivity, discover_network_metadata_from_net, distance_to_edge_shape

def assess_candidate_implementation(net, net_file, row, xml_root, max_match_distance_m, require_tls):
    cw_id = str(getattr(row, "crosswalk_id"))
    lon = float(getattr(row, "longitude"))
    lat = float(getattr(row, "latitude"))
    
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
    
    # Assess Tier
    # First, validate connectivity
    ped_val = validate_pedestrian_connectivity(net_file, cw_metadata, cw_id=cw_id)
    
    # Check conditions
    if match_distance_m > max_match_distance_m or (require_tls and not tls_id):
        # C-tier: crossing reconstruction required
        return {
            "implementation_status": "crossing_reconstruction_required",
            "match_distance_m": match_distance_m,
            "repaired_match_distance_m": None,
            "tls_id": tls_id,
            "crossing_edge": cw_metadata["crossing_edge"],
            "validation_status": ped_val.get("validation_status", ""),
            "repair_attempted": False,
            "repair_success": False,
            "reconstruction_needed": True,
            "final_usable_for_simulation": False,
            "failure_reason": "Distance exceeded or missing TLS",
            "repair_suggestion": "netedit/plain XML/additional crossing reconstruction required",
            "cw_metadata": cw_metadata,
            "ped_val": ped_val,
        }
    
    if ped_val["path_exists"] and ped_val["path_uses_crossing"]:
        # A-tier: ready for simulation
        return {
            "implementation_status": "ready_for_simulation",
            "match_distance_m": match_distance_m,
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
        }
        
    # B-tier: route repair required
    repair_result = attempt_route_repair(net, cw_metadata["crossing_edge"])
    if repair_result:
        cw_metadata["ped_route"]["from_edge"] = repair_result["from_edge"]
        cw_metadata["ped_route"]["to_edge"] = repair_result["to_edge"]
        
        # re-validate
        new_ped_val = validate_pedestrian_connectivity(net_file, cw_metadata, cw_id=cw_id)
        return {
            "implementation_status": "route_repair_success",
            "match_distance_m": match_distance_m,
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
            "original_from_edge": ped_val["from_edge"],
            "original_to_edge": ped_val["to_edge"],
            "repaired_from_edge": repair_result["from_edge"],
            "repaired_to_edge": repair_result["to_edge"],
        }
    else:
        return {
            "implementation_status": "route_repair_failed",
            "match_distance_m": match_distance_m,
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
        }

