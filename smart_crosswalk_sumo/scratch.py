import sys
from pathlib import Path
sys.path.append(str(Path.cwd()))
from network_utils import *
import math

def attempt_route_repair(net, crossing_edge_id):
    crossing_edge = net.getEdge(crossing_edge_id)
    if not crossing_edge:
        return None
        
    def edge_allows(e, vclass):
        try:
            return bool(e.allows(vclass))
        except:
            return False

    def edge_function(e):
        return e.getFunction() or "normal"
        
    candidates = set()
    node = crossing_edge.getFromNode()
    
    # Add from incoming/outgoing walkingareas
    for wa in crossing_edge.getIncoming().keys():
        if edge_function(wa) == "walkingarea":
            for e in wa.getIncoming().keys():
                if edge_function(e) == "normal" and edge_allows(e, "pedestrian"):
                    candidates.add(e)
    for wa in crossing_edge.getOutgoing().keys():
        if edge_function(wa) == "walkingarea":
            for e in wa.getOutgoing().keys():
                if edge_function(e) == "normal" and edge_allows(e, "pedestrian"):
                    candidates.add(e)
                    
    # Also add normal edges at the node
    for e in list(node.getIncoming()) + list(node.getOutgoing()):
        if edge_function(e) == "normal" and edge_allows(e, "pedestrian"):
            candidates.add(e)
            
    # Also look at neighbors of vehicle conflict edges
    for veh_e in node.getIncoming():
        if edge_function(veh_e) == "normal" and edge_allows(veh_e, "passenger"):
            veh_node = veh_e.getFromNode()
            for ped_e in list(veh_node.getIncoming()) + list(veh_node.getOutgoing()):
                if edge_function(ped_e) == "normal" and edge_allows(ped_e, "pedestrian"):
                    candidates.add(ped_e)
    for veh_e in node.getOutgoing():
        if edge_function(veh_e) == "normal" and edge_allows(veh_e, "passenger"):
            veh_node = veh_e.getToNode()
            for ped_e in list(veh_node.getIncoming()) + list(veh_node.getOutgoing()):
                if edge_function(ped_e) == "normal" and edge_allows(ped_e, "pedestrian"):
                    candidates.add(ped_e)
                    
    best_cost = float('inf')
    best_path = None
    best_pair = None
    
    for f in candidates:
        for t in candidates:
            if f.getID() == t.getID():
                continue
            try:
                path_edges, path_cost = net.getShortestPath(f, t, vClass="pedestrian", withInternal=True)
                if path_edges:
                    path_ids = [e.getID() for e in path_edges]
                    if crossing_edge_id in path_ids:
                        # Ensure not excessively long
                        if path_cost < best_cost and len(path_ids) <= 15:
                            best_cost = path_cost
                            best_path = path_ids
                            best_pair = (f.getID(), t.getID())
            except:
                pass
                
    if best_pair:
        return {
            "from_edge": best_pair[0],
            "to_edge": best_pair[1],
            "path_cost": best_cost,
            "path_edge_count": len(best_path),
            "path_edge_sequence": "|".join(best_path)
        }
    return None

