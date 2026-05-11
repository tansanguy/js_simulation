
import sys
import os
from pathlib import Path
import math

# Add project dir to path
sys.path.append(str(Path.cwd()))

from smart_crosswalk_sumo.network_utils import read_net, edge_function, edge_allows, distance_to_edge_shape

net_file = "result/integrated_implementation_audit/sumo_nets/integrated_selected/network.net.xml"
net = read_net(net_file)

# 8068 coordinates
lon, lat = 127.0094009, 37.56809667
target_xy = net.convertLonLat2XY(lon, lat)

crossings = [edge for edge in net.getEdges() if edge_function(edge) == "crossing"]

print(f"Crossings within 50m of 8068 ({lon}, {lat}):")
for c in crossings:
    dist = distance_to_edge_shape(c, target_xy)
    if dist < 50:
        print(f"  {c.getID()}, dist: {dist:.2f}m")
        # Check connectivity
        wa_in = [e for e in c.getIncoming().keys() if edge_function(e) == "walkingarea"]
        wa_out = [e for e in c.getOutgoing().keys() if edge_function(e) == "walkingarea"]
        print(f"    WA in: {[w.getID() for w in wa_in]}")
        for w in wa_in:
            print(f"      WA {w.getID()} incoming: {[e.getID() for e in w.getIncoming().keys() if edge_function(e) == 'normal']}")
        print(f"    WA out: {[w.getID() for w in wa_out]}")
        for w in wa_out:
            print(f"      WA {w.getID()} outgoing: {[e.getID() for e in w.getOutgoing().keys() if edge_function(e) == 'normal']}")
