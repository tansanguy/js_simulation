
import sys
import os
from pathlib import Path

# Add project dir to path
sys.path.append(str(Path.cwd()))

from smart_crosswalk_sumo.network_utils import read_net, edge_function, edge_allows

net_file = "result/integrated_implementation_audit/sumo_nets/integrated_selected/network.net.xml"
net = read_net(net_file)

crossing_id = ":8209566041_c0"
crossing_edge = net.getEdge(crossing_id)
node = crossing_edge.getFromNode()

print(f"Crossing: {crossing_id}")
print(f"Node: {node.getID()}, type: {node.getType()}")

print("\nIncoming to Crossing:")
for e in crossing_edge.getIncoming().keys():
    print(f"  {e.getID()} ({edge_function(e)})")
    if edge_function(e) == "walkingarea":
        print(f"    Walkingarea {e.getID()} incoming edges:")
        for ee in e.getIncoming().keys():
            print(f"      {ee.getID()} ({edge_function(ee)}) allows ped: {edge_allows(ee, 'pedestrian')}")

print("\nOutgoing from Crossing:")
for e in crossing_edge.getOutgoing().keys():
    print(f"  {e.getID()} ({edge_function(e)})")
    if edge_function(e) == "walkingarea":
        print(f"    Walkingarea {e.getID()} outgoing edges:")
        for ee in e.getOutgoing().keys():
            print(f"      {ee.getID()} ({edge_function(ee)}) allows ped: {edge_allows(ee, 'pedestrian')}")

print("\nNormal edges at Node:")
for e in list(node.getIncoming()) + list(node.getOutgoing()):
    if edge_function(e) == "normal":
        print(f"  {e.getID()} allows ped: {edge_allows(e, 'pedestrian')}")
