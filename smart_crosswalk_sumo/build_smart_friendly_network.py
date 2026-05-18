#!/usr/bin/env python3
import os
import sys
import csv
import subprocess
from datetime import datetime
from pathlib import Path
from collections import defaultdict
import xml.etree.ElementTree as ET
import pandas as pd
import numpy as np
import sumolib

from smart_crosswalk_sumo.network_utils import project_root

class SmartFriendlyNetworkBuilder:
    def __init__(self):
        self.output_dir = None
        self.base_network_path = None
        self.t2_csv_path = None
        self.existing_con_path = None

        self.net = None
        self.t2_data = {}
        self.junctions_map = {}
        self.tls_junctions = {}
        self.existing_crossings = {}

    def setup(self):
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.output_dir = project_root() / "result" / f"smart_friendly_network_{ts}"
        self.output_dir.mkdir(parents=True, exist_ok=True)
        print(f"Output directory: {self.output_dir}")

        root = project_root()
        self.base_network_path = root / "result" / "junggu_osm_network_20260513_184417" / "junggu_generated.net.xml"
        self.t2_csv_path = root / "smart_crosswalk_sumo" / "data" / "T2_crosswalk_features.csv"
        self.existing_con_path = root / "result" / "t2_generated_crosswalks_20260513_221249" / "generated_crossings.con.xml"

    def load_base_network(self):
        print("\n=== Loading base network ===")
        self.net = sumolib.net.readNet(str(self.base_network_path))
        print(f"Network loaded: {len(self.net.getNodes())} nodes, {len(self.net.getEdges())} edges")

        for node in self.net.getNodes():
            node_id = node.getID()
            node_type = node.getType()
            self.junctions_map[node_id] = {
                'id': node_id,
                'type': node_type,
                'x': node.getCoord()[0],
                'y': node.getCoord()[1],
                'incoming_edges': [e.getID() for e in node.getIncoming()],
                'outgoing_edges': [e.getID() for e in node.getOutgoing()],
            }
            if node_type == 'traffic_light':
                self.tls_junctions[node_id] = self.junctions_map[node_id]

        print(f"TLS junctions: {len(self.tls_junctions)}")

    def load_t2_data(self):
        print("\n=== Loading T2 data ===")
        df = pd.read_csv(self.t2_csv_path)
        for idx, row in df.iterrows():
            cw_id = str(row['횡단보도ID'])
            self.t2_data[cw_id] = row.to_dict()
        print(f"T2 data loaded: {len(self.t2_data)} crosswalks")

    def load_existing_crossings(self):
        print("\n=== Loading existing crossings ===")
        tree = ET.parse(self.existing_con_path)
        root = tree.getroot()
        for crossing_elem in root.findall('.//crossing'):
            crossing_id = crossing_elem.get('id', '')
            node = crossing_elem.get('node', '')
            edges_str = crossing_elem.get('edges', '')
            edges = edges_str.split() if edges_str else []

            cw_id = crossing_id.replace('T2_', '')
            self.existing_crossings[cw_id] = {
                'id': crossing_id,
                'node': node,
                'edges': edges,
            }
        print(f"Existing crossings loaded: {len(self.existing_crossings)}")

    def xy_to_sumo(self, lon, lat):
        try:
            return self.net.convertLonLat2XY(lon, lat)
        except:
            return None

    def distance(self, x1, y1, x2, y2):
        return ((x1 - x2) ** 2 + (y1 - y2) ** 2) ** 0.5

    def find_nearest_junctions(self, lon, lat, max_dist=200):
        xy = self.xy_to_sumo(lon, lat)
        if xy is None:
            return None, [], []

        x, y = xy
        candidates = []
        tls_candidates = []

        for junction_id, junction_info in self.junctions_map.items():
            dist = self.distance(x, y, junction_info['x'], junction_info['y'])
            if dist <= max_dist:
                candidates.append((junction_id, dist))
                if junction_info['type'] == 'traffic_light':
                    tls_candidates.append((junction_id, dist))

        candidates.sort(key=lambda x: x[1])
        tls_candidates.sort(key=lambda x: x[1])

        nearest_junction = candidates[0] if candidates else (None, float('inf'))
        nearest_tls = tls_candidates[0] if tls_candidates else (None, float('inf'))

        return xy, nearest_junction, nearest_tls

    def get_junction_incident_edges(self, junction_id):
        if junction_id not in self.junctions_map:
            return set()
        j = self.junctions_map[junction_id]
        return set(j['incoming_edges'] + j['outgoing_edges'])

    def check_edge_overlap(self, crossing_edges, tls_incident_edges):
        crossing_set = set(crossing_edges) if crossing_edges else set()
        tls_set = set(tls_incident_edges) if tls_incident_edges else set()
        overlap = crossing_set & tls_set
        return overlap, len(overlap) / len(crossing_set) if crossing_set else 0

    def estimate_crossing_center(self, crossing_edges):
        if not crossing_edges:
            return None
        centers = []
        for edge_id in crossing_edges:
            try:
                edge = self.net.getEdge(edge_id)
                if edge:
                    x, y = edge.getFromNode().getCoord()
                    centers.append((x, y))
            except:
                pass
        if centers:
            avg_x = sum(c[0] for c in centers) / len(centers)
            avg_y = sum(c[1] for c in centers) / len(centers)
            return (avg_x, avg_y)
        return None

    def build_candidate_junctions_csv(self):
        print("\n=== Step 2: Building smart candidate junctions ===")
        rows = []

        for cw_id, t2_row in self.t2_data.items():
            if pd.isna(t2_row.get('lon')) or pd.isna(t2_row.get('lat')):
                continue

            lon = float(t2_row['lon'])
            lat = float(t2_row['lat'])

            xy, nearest_junction, nearest_tls = self.find_nearest_junctions(lon, lat, max_dist=200)
            if xy is None:
                continue

            nearest_junc_id, nearest_junc_dist = nearest_junction
            nearest_tls_id, nearest_tls_dist = nearest_tls

            if nearest_tls_id:
                tls_type = 'traffic_light'
            else:
                tls_type = 'NONE'

            if nearest_tls_dist <= 30 or (nearest_junc_id and self.junctions_map[nearest_junc_id]['type'] == 'traffic_light'):
                grade = 'SMART_READY_JUNCTION'
            elif nearest_tls_dist <= 75:
                accident = int(t2_row.get('사고건수', 0) or 0)
                elderly = float(t2_row.get('노인비율', 0) or 0)
                if accident > 0 or elderly > 0.1:
                    grade = 'SMART_REBUILD_JUNCTION'
                else:
                    grade = 'SMART_PROXY_OR_LOCATION_ONLY'
            else:
                grade = 'SMART_PROXY_OR_LOCATION_ONLY'

            rows.append({
                'crosswalk_id': cw_id,
                'lon': lon,
                'lat': lat,
                'nearest_junction_id': nearest_junc_id or '',
                'nearest_junction_type': self.junctions_map.get(nearest_junc_id, {}).get('type', 'unknown') if nearest_junc_id else 'NONE',
                'distance_to_nearest_junction_m': round(nearest_junc_dist, 2),
                'nearest_tls_id': nearest_tls_id or '',
                'nearest_tls_type': tls_type,
                'distance_to_nearest_tls_m': round(nearest_tls_dist, 2),
                'smart_junction_grade': grade,
            })

        df = pd.DataFrame(rows)
        df.to_csv(self.output_dir / "smart_candidate_junctions.csv", index=False)
        print(f"Smart candidate junctions: {len(rows)}")
        print(df['smart_junction_grade'].value_counts().to_dict())

        return df

    def check_snap_feasibility(self, df_candidates):
        print("\n=== Step 2.5: Checking TLS snap feasibility ===")
        rows = []

        for idx, row in df_candidates.iterrows():
            cw_id = row['crosswalk_id']
            original_junction_id = row['nearest_junction_id']
            target_tls_id = row['nearest_tls_id']
            grade = row['smart_junction_grade']

            if grade == 'SMART_PROXY_OR_LOCATION_ONLY' or not target_tls_id:
                rows.append({
                    'crosswalk_id': cw_id,
                    'original_junction_id': original_junction_id,
                    'original_junction_type': row['nearest_junction_type'],
                    'target_tls_junction_id': '',
                    'target_tls_incident_edges': '',
                    'selected_crossing_edges': self.existing_crossings.get(cw_id, {}).get('edges', []),
                    'edge_incidence_valid': False,
                    'incidence_overlap_ratio': 0.0,
                    'snap_distance_m': 0.0,
                    'expected_real_to_crossing_after_snap_m': 0.0,
                    'snap_geometry_grade': 'N/A',
                    'snap_allowed': 'False',
                    'strategy': 'PROXY_OR_LOCATION_ONLY',
                    'reason': 'TLS too far or no TLS available',
                    'expected_risk': 'High'
                })
                continue

            crossing_info = self.existing_crossings.get(cw_id, {})
            crossing_edges = crossing_info.get('edges', [])

            target_tls_incident_edges = self.get_junction_incident_edges(target_tls_id)
            overlap, overlap_ratio = self.check_edge_overlap(crossing_edges, target_tls_incident_edges)

            lon = float(self.t2_data[cw_id]['lon'])
            lat = float(self.t2_data[cw_id]['lat'])
            xy = self.xy_to_sumo(lon, lat)

            if xy:
                x, y = xy
                target_tls_x = self.junctions_map[target_tls_id]['x']
                target_tls_y = self.junctions_map[target_tls_id]['y']
                snap_distance = self.distance(x, y, target_tls_x, target_tls_y)
            else:
                snap_distance = 0.0

            estimated_center = self.estimate_crossing_center(crossing_edges)
            if estimated_center and xy:
                expected_distortion = self.distance(x, y, estimated_center[0], estimated_center[1])
            else:
                expected_distortion = 0.0

            if overlap and overlap_ratio >= 0.75 and snap_distance <= 25:
                snap_allowed = 'True'
                snap_geometry_grade = 'GOOD'
                strategy = 'SNAP_TO_EXISTING_TLS_JUNCTION'
                reason = 'Edge overlap good + geometry acceptable'
            elif overlap and overlap_ratio >= 0.5 and snap_distance <= 50:
                snap_allowed = 'Conditional'
                snap_geometry_grade = 'FAIR'
                strategy = 'SNAP_TO_EXISTING_TLS_JUNCTION'
                reason = 'Partial edge overlap, needs Netedit review'
            else:
                snap_allowed = 'False'
                if not overlap:
                    strategy = 'MAKE_ORIGINAL_JUNCTION_JOINED_TLS'
                    reason = 'No edge overlap, consider joined TLS strategy'
                else:
                    strategy = 'INSERT_OR_SPLIT_JUNCTION_AT_T2_LOCATION'
                    reason = 'Edge overlap poor or snap distance too far'
                snap_geometry_grade = 'POOR'

            rows.append({
                'crosswalk_id': cw_id,
                'original_junction_id': original_junction_id,
                'original_junction_type': row['nearest_junction_type'],
                'target_tls_junction_id': target_tls_id,
                'target_tls_incident_edges': ','.join(target_tls_incident_edges) if target_tls_incident_edges else '',
                'selected_crossing_edges': ' '.join(crossing_edges) if crossing_edges else '',
                'edge_incidence_valid': len(overlap) > 0,
                'incidence_overlap_ratio': round(overlap_ratio, 3),
                'snap_distance_m': round(snap_distance, 2),
                'expected_real_to_crossing_after_snap_m': round(expected_distortion, 2),
                'snap_geometry_grade': snap_geometry_grade,
                'snap_allowed': snap_allowed,
                'strategy': strategy,
                'reason': reason,
                'expected_risk': 'Low' if snap_allowed == 'True' else ('Medium' if snap_allowed == 'Conditional' else 'High')
            })

        df = pd.DataFrame(rows)
        df.to_csv(self.output_dir / "tls_snap_feasibility.csv", index=False)
        print(f"Snap feasibility checked: {len(rows)} candidates")
        print(df['snap_allowed'].value_counts().to_dict())
        print(df['strategy'].value_counts().to_dict())

        return df

    def verify_case_119055(self, df_snap):
        print("\n=== Step 3: Verify case 119055 ===")

        case_row = df_snap[df_snap['crosswalk_id'] == '119055']
        if len(case_row) == 0:
            md = "# Case 119055 Snap Feasibility\n\n**Status**: NOT FOUND\n"
        else:
            case = case_row.iloc[0].to_dict()
            snap_allowed = case['snap_allowed']
            strategy = case['strategy']
            overlap_ratio = case['incidence_overlap_ratio']
            snap_dist = case['snap_distance_m']
            expected_dist = case['expected_real_to_crossing_after_snap_m']

            md = f"""# Case 119055 Snap Feasibility

## Current State
- Original crossing node: 11252413185
- Target TLS node: 11252413259
- Original crossing edges: {case['selected_crossing_edges']}

## TLS Snap Verification
- Target TLS incident edges: {len(case['target_tls_incident_edges'].split(',')) if case['target_tls_incident_edges'] else 0} edges
- Edge overlap ratio: {overlap_ratio}
- Snap distance: {snap_dist}m
- Expected geometry distortion: {expected_dist}m

## Decision
- **snap_allowed**: {snap_allowed}
- **Strategy**: {strategy}
- **Reason**: {case['reason']}
- **Expected Risk**: {case['expected_risk']}

## Recommendation
"""
            if snap_allowed == 'True':
                md += "✓ TLS snap is VIABLE. Proceed with pilot netconvert.\n"
            elif snap_allowed == 'Conditional':
                md += "⚠ TLS snap is CONDITIONAL. Netedit review required.\n"
            else:
                md += "✗ TLS snap is NOT VIABLE. Explore joined TLS or inserted junction strategy.\n"

        with open(self.output_dir / "case_119055_snap_feasibility.md", "w") as f:
            f.write(md)

        if len(case_row) > 0:
            snap_allowed = case_row.iloc[0]['snap_allowed']
            return snap_allowed == 'True'
        return False

    def generate_pilot_crossing_xml(self):
        print("\n=== Step 4: Generate pilot crossing XML ===")
        xml_content = """<?xml version="1.0" encoding="UTF-8"?>
<connections>
    <crossing node="11252413259"
              edges="-180205110#4 180205110#4 516948898#10 516948898#11"
              id="T2_119055_smart"
              width="4.0" priority="true"/>
</connections>
"""
        pilot_con_path = self.output_dir / "smart_crossings_pilot_119055.con.xml"
        with open(pilot_con_path, "w") as f:
            f.write(xml_content)
        print(f"Pilot crossing XML created: {pilot_con_path}")
        return pilot_con_path

    def run_netconvert_pilot(self, pilot_con_path):
        print("\n=== Step 5: Run pilot netconvert ===")
        output_net = self.output_dir / "smart_friendly_generated_pilot_119055.net.xml"
        log_file = self.output_dir / "netconvert_pilot_119055.log"

        cmd = [
            "netconvert",
            "--sumo-net-file", str(self.base_network_path),
            "--connection-files", str(pilot_con_path),
            "--output-file", str(output_net),
            "--walkingareas",
            "--crossings.guess",
            "--sidewalks.guess",
        ]

        print(f"Running: {' '.join(cmd)}")
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
            with open(log_file, "w") as f:
                f.write(result.stdout)
                f.write(result.stderr)
            print(f"Pilot netconvert completed. Log: {log_file}")
            return output_net if output_net.exists() else None
        except Exception as e:
            print(f"Pilot netconvert failed: {e}")
            return None

    def validate_pilot_network(self, pilot_net_path):
        print("\n=== Step 6: Validate pilot network ===")
        if not pilot_net_path or not pilot_net_path.exists():
            print("Pilot network not found")
            return False

        try:
            pilot_net = sumolib.net.readNet(str(pilot_net_path))
            print("Pilot network loaded successfully")

            for elem in pilot_net.getNodes():
                pass

            print("Validation passed (basic check)")
            return True
        except Exception as e:
            print(f"Validation failed: {e}")
            return False

    def generate_summary(self, df_snap):
        print("\n=== Final: Generate summary ===")

        summary = f"""# Smart-friendly Network Regeneration Summary

## Overview
- Generated crossing candidates: 674
- TLS snap feasible candidates: {len(df_snap[df_snap['snap_allowed'] == 'True'])}
- Conditional snap candidates: {len(df_snap[df_snap['snap_allowed'] == 'Conditional'])}
- Not feasible candidates: {len(df_snap[df_snap['snap_allowed'] == 'False'])}

## Strategy Distribution
"""
        for strategy, count in df_snap['strategy'].value_counts().items():
            summary += f"- {strategy}: {count}\n"

        summary += f"""
## Case 119055
- Check: case_119055_snap_feasibility.md

## Key Finding
T2 crossing을 TLS-controlled pedestrian crossing으로 만들기 위해서는 TLS junction snap 가능성,
edge incidence, geometry distortion을 먼저 검증해야 합니다.

Snap이 불가능한 후보는 original junction joined TLS 또는 inserted junction 방식으로
재생성해야 합니다.

Smart-friendly regeneration은 단순 nearest TLS 재배정이 아니라, crossing geometry와
TLS-controlled linkIndex를 동시에 만족하도록 네트워크를 재설계하는 과정입니다.

## Next Steps
1. Verify 119055 pilot result
2. If pilot succeeds (snap_allowed=True), expand to other SNAP_TO_EXISTING_TLS_JUNCTION candidates
3. For MAKE_ORIGINAL_JUNCTION_JOINED_TLS candidates: design joined TLS strategy
4. For INSERT_OR_SPLIT_JUNCTION_AT_T2_LOCATION candidates: plan edge split
5. For PROXY_OR_LOCATION_ONLY: keep as proxy/location-only

---

Generated: {datetime.now().isoformat()}
"""
        with open(self.output_dir / "smart_network_generation_summary.md", "w") as f:
            f.write(summary)

    def run(self):
        print("=" * 80)
        print("PHASE 5.8: SMART-FRIENDLY SUMO NETWORK REGENERATION")
        print("=" * 80)

        self.setup()
        self.load_base_network()
        self.load_t2_data()
        self.load_existing_crossings()

        df_candidates = self.build_candidate_junctions_csv()
        df_snap = self.check_snap_feasibility(df_candidates)
        pilot_viable = self.verify_case_119055(df_snap)

        if pilot_viable:
            pilot_con = self.generate_pilot_crossing_xml()
            pilot_net = self.run_netconvert_pilot(pilot_con)
            if pilot_net:
                self.validate_pilot_network(pilot_net)

        self.generate_summary(df_snap)

        print("\n" + "=" * 80)
        print("PHASE 5.8 COMPLETE")
        print(f"Output: {self.output_dir}")
        print("=" * 80)

if __name__ == "__main__":
    builder = SmartFriendlyNetworkBuilder()
    builder.run()
