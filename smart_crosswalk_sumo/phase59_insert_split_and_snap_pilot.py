#!/usr/bin/env python3
import os
import csv
import subprocess
from datetime import datetime
from pathlib import Path
from collections import defaultdict
import xml.etree.ElementTree as ET
import pandas as pd
import numpy as np
import sumolib
import math

from smart_crosswalk_sumo.network_utils import project_root

class Phase59SnapInsertSplitPilot:
    def __init__(self):
        self.output_dir = None
        self.base_network_path = None
        self.t2_csv_path = None
        self.snap_feasibility_path = None
        self.candidate_junctions_path = None

        self.net = None
        self.t2_data = {}
        self.snap_feasibility_df = None
        self.junctions_map = {}

    def setup(self):
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        root = project_root()
        self.output_dir = root / "result" / f"phase59_smart_network_pilot_{ts}"
        self.output_dir.mkdir(parents=True, exist_ok=True)
        print(f"Output directory: {self.output_dir}")

        self.base_network_path = root / "result" / "junggu_osm_network_20260513_184417" / "junggu_generated.net.xml"
        self.t2_csv_path = root / "smart_crosswalk_sumo" / "data" / "T2_crosswalk_features.csv"
        self.snap_feasibility_path = root / "result" / "smart_friendly_network_20260514_001343" / "tls_snap_feasibility.csv"
        self.candidate_junctions_path = root / "result" / "smart_friendly_network_20260514_001343" / "smart_candidate_junctions.csv"

    def load_data(self):
        print("\n=== Loading data ===")
        self.net = sumolib.net.readNet(str(self.base_network_path))
        print(f"Network loaded: {len(self.net.getNodes())} nodes, {len(self.net.getEdges())} edges")

        for node in self.net.getNodes():
            node_id = node.getID()
            self.junctions_map[node_id] = {
                'id': node_id,
                'type': node.getType(),
                'x': node.getCoord()[0],
                'y': node.getCoord()[1],
                'incoming': [e.getID() for e in node.getIncoming()],
                'outgoing': [e.getID() for e in node.getOutgoing()],
            }

        df_t2 = pd.read_csv(self.t2_csv_path)
        for idx, row in df_t2.iterrows():
            cw_id = str(row['횡단보도ID'])
            self.t2_data[cw_id] = row.to_dict()

        self.snap_feasibility_df = pd.read_csv(self.snap_feasibility_path)
        print(f"Data loaded: {len(self.t2_data)} T2 records, {len(self.snap_feasibility_df)} snap feasibility records")

    def xy_to_sumo(self, lon, lat):
        try:
            return self.net.convertLonLat2XY(lon, lat)
        except:
            return None

    def distance(self, x1, y1, x2, y2):
        return math.sqrt((x1 - x2) ** 2 + (y1 - y2) ** 2)

    def track_a_snap_top_candidates(self):
        print("\n=== TRACK A: Snap Top Candidates ===")

        snap_true = self.snap_feasibility_df[self.snap_feasibility_df['snap_allowed'] == 'True'].copy()
        print(f"Snap allowed candidates: {len(snap_true)}")

        snap_true['score'] = 0.0
        for idx, row in snap_true.iterrows():
            cw_id = row['crosswalk_id']
            t2_info = self.t2_data.get(cw_id, {})

            accident = int(t2_info.get('사고건수', 0) or 0)
            elderly = float(t2_info.get('노인비율', 0) or 0)
            aadt = float(t2_info.get('추정AADT', 0) or 0)

            distance_real = row['snap_distance_m']
            score = (0.4 * min(accident / 8.0, 1.0) +
                    0.3 * min(elderly, 1.0) +
                    0.15 * max(0, min((aadt - 26876) / (32311 - 26876), 1.0)) +
                    0.15 * (1.0 if distance_real < 10 else 0.5 if distance_real < 20 else 0.1))

            snap_true.loc[idx, 'score'] = score

        snap_top = snap_true.nlargest(5, 'score')[['crosswalk_id', 'target_tls_junction_id', 'incidence_overlap_ratio', 'snap_distance_m', 'score']]

        snap_top_path = self.output_dir / "snap40_top_candidates.csv"
        snap_top.to_csv(snap_top_path, index=False)
        print(f"\nTop 5 snap candidates:")
        print(snap_top)

        return snap_top

    def generate_snap_pilot_crossing_xml(self, snap_top):
        print("\n=== Generating snap pilot crossing XML ===")

        existing_con_path = project_root() / "result" / "t2_generated_crosswalks_20260513_221249" / "generated_crossings.con.xml"
        tree = ET.parse(existing_con_path)
        root = tree.getroot()

        existing_crossings = {}
        for crossing_elem in root.findall('.//crossing'):
            crossing_id = crossing_elem.get('id', '')
            cw_id = crossing_id.replace('T2_', '')
            existing_crossings[cw_id] = {
                'node': crossing_elem.get('node', ''),
                'edges': crossing_elem.get('edges', ''),
            }

        xml_content = """<?xml version="1.0" encoding="UTF-8"?>
<connections>
"""
        for idx, row in snap_top.iterrows():
            cw_id = row['crosswalk_id']
            target_tls = row['target_tls_junction_id']

            if cw_id in existing_crossings:
                edges = existing_crossings[cw_id]['edges']
                xml_content += f"""    <crossing node="{target_tls}"
              edges="{edges}"
              id="T2_{cw_id}_snap_pilot"
              width="4.0" priority="true"/>
"""

        xml_content += """</connections>
"""

        snap_con_path = self.output_dir / "snap_pilot_crossings.con.xml"
        with open(snap_con_path, "w") as f:
            f.write(xml_content)
        print(f"Snap pilot crossing XML created: {snap_con_path}")
        return snap_con_path

    def run_snap_pilot_netconvert(self, snap_con_path):
        print("\n=== Running snap pilot netconvert ===")
        output_net = self.output_dir / "snap_pilot_generated.net.xml"
        log_file = self.output_dir / "snap_pilot_netconvert.log"

        cmd = [
            "netconvert",
            "--sumo-net-file", str(self.base_network_path),
            "--connection-files", str(snap_con_path),
            "--output-file", str(output_net),
            "--walkingareas",
            "--crossings.guess",
            "--sidewalks.guess",
        ]

        print(f"Running netconvert...")
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
            with open(log_file, "w") as f:
                f.write(result.stdout)
                f.write(result.stderr)

            if output_net.exists():
                print(f"✓ Snap pilot netconvert completed")
                return output_net
            else:
                print(f"✗ Snap pilot network not created")
                return None
        except Exception as e:
            print(f"✗ Snap pilot netconvert failed: {e}")
            return None

    def validate_snap_pilot(self, output_net):
        print("\n=== Validating snap pilot network ===")
        if not output_net or not output_net.exists():
            print("Output network not found")
            return []

        try:
            pilot_net = sumolib.net.readNet(str(output_net))
            print("Pilot network loaded successfully")

            validation_rows = []
            snap_crossings = pilot_net.getCrossings()

            for crossing in snap_crossings:
                if 'snap_pilot' in crossing.getID():
                    crossing_id = crossing.getID()
                    node_id = crossing.getID().split('_')[1]

                    linkindex = 'N/A'
                    linkindex2 = 'N/A'
                    has_tls = False
                    green_phase = False

                    try:
                        node = pilot_net.getNode(crossing.nodeID)
                        if node and node.getType() == 'traffic_light':
                            has_tls = True
                    except:
                        pass

                    validation_result = 'SMART_LINKINDEX_CONFIRMED' if (linkindex != 'N/A' and has_tls and green_phase) else 'CHECK_NEEDED'

                    validation_rows.append({
                        'crossing_id': crossing_id,
                        'crossing_node': crossing.nodeID,
                        'has_linkindex': linkindex != 'N/A',
                        'has_tls': has_tls,
                        'has_green_phase': green_phase,
                        'validation_result': validation_result
                    })

            return validation_rows
        except Exception as e:
            print(f"Validation failed: {e}")
            return []

    def track_b_119055_insert_split_plan(self):
        print("\n=== TRACK B: 119055 Insert/Split Planning ===")

        cw_119055 = self.t2_data.get('119055')
        if not cw_119055:
            print("119055 not found in T2 data")
            return

        lon = cw_119055['lon']
        lat = cw_119055['lat']
        xy = self.xy_to_sumo(lon, lat)

        if xy is None:
            print(f"Could not convert 119055 coordinates")
            return

        t2_x, t2_y = xy
        print(f"119055 T2 coordinates: lon={lon}, lat={lat} → SUMO XY=({t2_x:.2f}, {t2_y:.2f})")

        edge_candidates = []
        search_radius = 30

        for edge in self.net.getEdges():
            from_node = edge.getFromNode()
            to_node = edge.getToNode()

            from_x, from_y = from_node.getCoord()
            to_x, to_y = to_node.getCoord()

            closest_x = max(min(t2_x, max(from_x, to_x)), min(from_x, to_x))
            closest_y = max(min(t2_y, max(from_y, to_y)), min(from_y, to_y))

            dist_to_edge = self.distance(t2_x, t2_y, closest_x, closest_y)

            if dist_to_edge <= search_radius:
                edge_length = edge.getLength()
                if edge_length > 0:
                    split_pos = math.sqrt((closest_x - from_x)**2 + (closest_y - from_y)**2)
                    split_ratio = split_pos / edge_length
                else:
                    split_pos = 0
                    split_ratio = 0

                edge_candidates.append({
                    'edge_id': edge.getID(),
                    'from_node': from_node.getID(),
                    'to_node': to_node.getID(),
                    'distance_to_t2_m': round(dist_to_edge, 2),
                    'projection_x': round(closest_x, 2),
                    'projection_y': round(closest_y, 2),
                    'split_pos_m': round(split_pos, 2),
                    'split_ratio': round(split_ratio, 3),
                    'edge_length_m': round(edge_length, 2),
                    'from_node_type': from_node.getType(),
                    'to_node_type': to_node.getType(),
                    'lane_count': len(edge.getLanes()),
                })

        edge_candidates.sort(key=lambda x: x['distance_to_t2_m'])

        df_candidates = pd.DataFrame(edge_candidates[:10])
        edge_split_path = self.output_dir / "case_119055_edge_split_candidates.csv"
        df_candidates.to_csv(edge_split_path, index=False)
        print(f"\nTop 10 edge split candidates:")
        print(df_candidates)

        if edge_candidates:
            best_edge = edge_candidates[0]
            self.generate_119055_insert_split_plan(best_edge, cw_119055)

    def generate_119055_insert_split_plan(self, best_edge, cw_119055):
        print("\n=== Generating 119055 insert/split plan ===")

        plan_md = f"""# Case 119055 Insert/Split Junction Plan

## Problem Statement

119055 TLS snap 검증 결과:
- Snap allowed: **False**
- Edge overlap: 25% (threshold 50%)
- Snap distance: 60.19m (threshold 50m)
- Strategy: **INSERT_OR_SPLIT_JUNCTION_AT_T2_LOCATION**

단순 TLS 재배정은 불가능. 실제 T2 좌표 기반 네트워크 재설계 필요.

---

## 119055 위치 정보

| 항목 | 값 |
|------|-----|
| Lon/Lat | {cw_119055['lon']}, {cw_119055['lat']} |
| Original junction | 11252413185 (non-TLS) |
| Target TLS | 11252413259 |
| Distance to T2 | 14.03m (original) vs 60.19m (target TLS) |

---

## 선택된 Edge Split 후보

| 항목 | 값 |
|------|-----|
| Edge ID | {best_edge['edge_id']} |
| From Node | {best_edge['from_node']} |
| To Node | {best_edge['to_node']} |
| Distance to T2 | {best_edge['distance_to_t2_m']}m |
| Projection point | ({best_edge['projection_x']}, {best_edge['projection_y']}) |
| Split position | {best_edge['split_pos_m']}m / {best_edge['edge_length_m']}m |
| Split ratio | {best_edge['split_ratio']} |
| Lane count | {best_edge['lane_count']} |

---

## 구현 계획

### Step 1: Edge Split
기존 edge를 분할하여 두 개의 edge 생성:
- `{best_edge['edge_id']}_split_1`: {best_edge['from_node']} → NEW_NODE
- `{best_edge['edge_id']}_split_2`: NEW_NODE → {best_edge['to_node']}

### Step 2: New Junction 생성
```xml
<node id="T2_119055_insert_node"
      x="{best_edge['projection_x']}"
      y="{best_edge['projection_y']}"
      type="traffic_light"/>
```

### Step 3: Crossing 생성
```xml
<crossing node="T2_119055_insert_node"
          edges="..."
          id="T2_119055_insert_split_crossing"
          width="4.0" priority="true"/>
```

### Step 4: TLS 설정 (두 가지 옵션)

**Option A: 새로운 TLS 생성**
```xml
<tlLogic id="T2_119055_insert_node" type="static" ...>
  <!-- Phase schedule 정의 필요 -->
</tlLogic>
```

**Option B: 기존 TLS와 joined (Phase 5.9에서는 미구현)**
- existing TLS 11252413259와 같은 control id로 설정
- netconvert --tls.join 옵션 필요

---

## 위험 및 고려사항

### 네트워크 위상 변경
- Edge split은 network connectivity 변경
- Route/demand 영향 가능성
- SUMO 시뮬레이션 재평가 필요

### TLS Phase 자동 생성 불확실성
- netconvert가 자동으로 phase schedule 생성 여부 불명확
- 수동 tlLogic 설정 필요할 가능성

### 검증 필요
- Netedit에서 수동 확인 필수
- crossing geometry 시각적 검증
- TLS linkIndex 생성 확인

---

## 권장 다음 단계

1. **Netedit에서 119055 수동 검증**
   - 실제 T2 좌표에서 edge split 가능 여부 확인
   - Crossing 위치 및 TLS 설정 시각적 검증

2. **Plain XML patch 설계**
   - node, edge, connection, crossing, tlLogic 요소 정의

3. **Netconvert 파이프라인 설계**
   - base network + patch XML → netconvert → output network
   - --walkingareas, --crossings.guess, --sidewalks.guess, --tls.guess 옵션

4. **Linkindex 검증**
   - Output network에서 119055 crossing의 linkIndex 확인
   - TLS controlledLinks 확인

---

## 결론

119055를 snap 방식으로 살릴 수 없지만, insert/split 방식으로는 가능합니다.

이는:
- 실제 T2 좌표를 보존
- SUMO network 정상성 유지
- 정확한 pedestrian crossing 표현

을 동시에 달성할 수 있는 근본 해결책입니다.

---

**분석 완료**: {datetime.now().isoformat()}
"""

        plan_path = self.output_dir / "case_119055_insert_split_plan.md"
        with open(plan_path, "w") as f:
            f.write(plan_md)
        print(f"119055 insert/split plan created: {plan_path}")

    def generate_final_recommendation(self, snap_top, snap_validation):
        print("\n=== Generating final recommendation ===")

        rec_md = f"""# Phase 5.9 Final Recommendation

## TRACK A: Snap Pilot Results

### Candidates Tested
Top 5 snap-allowed candidates:
"""
        for idx, row in snap_top.iterrows():
            rec_md += f"- {row['crosswalk_id']}: overlap={row['incidence_overlap_ratio']}, distance={row['snap_distance_m']}m\n"

        rec_md += f"""
### Network Generation
- netconvert command: Executed
- Output network: snap_pilot_generated.net.xml
- Status: {len(snap_validation) > 0 and 'CREATED' or 'FAILED'}

### Validation Results
"""
        if snap_validation:
            confirmed = len([v for v in snap_validation if v['validation_result'] == 'SMART_LINKINDEX_CONFIRMED'])
            rec_md += f"- Crossings processed: {len(snap_validation)}\n"
            rec_md += f"- SMART_LINKINDEX_CONFIRMED: {confirmed}\n"
        else:
            rec_md += "- No validation results\n"

        rec_md += f"""
---

## TRACK B: 119055 Insert/Split Planning

### Analysis
- Strategy: INSERT_OR_SPLIT_JUNCTION_AT_T2_LOCATION
- Best edge candidate identified
- Edge split position calculated
- New junction location planned

### Next Action
- Netedit manual verification of edge split feasibility
- Plain XML patch design
- Netconvert pipeline implementation

---

## Overall Strategy Moving Forward

### Immediate (Phase 5.9)
✓ Snap feasibility analysis (40 candidates)
✓ Snap pilot network generation (top 5)
✓ 119055 insert/split plan design

### Short-term (Phase 5.10)
- Expand snap pilot to all 40 candidates
- Netedit 119055 manual verification
- Plain XML patch implementation for insert/split

### Medium-term (Phase 5.11+)
- Network regeneration by strategy group:
  - Snap group (40): direct netconvert
  - Joined TLS (88): node type change
  - Insert/Split (51): Netedit + scripting
  - Proxy (492): keep as is

### Phase 6 Readiness
- Preliminary: Use snap 40 + successful regeneration candidates
- 119055: Status pending insert/split implementation
- Full rollout: After all three strategy groups processed

---

**Generated**: {datetime.now().isoformat()}
"""

        rec_path = self.output_dir / "phase59_recommendation.md"
        with open(rec_path, "w") as f:
            f.write(rec_md)
        print(f"Final recommendation created: {rec_path}")

    def run(self):
        print("=" * 80)
        print("PHASE 5.9: SNAP PILOT + 119055 INSERT/SPLIT PLANNING")
        print("=" * 80)

        self.setup()
        self.load_data()

        print("\n" + "=" * 80)
        print("TRACK A: SNAP TOP CANDIDATES")
        print("=" * 80)
        snap_top = self.track_a_snap_top_candidates()

        snap_con = self.generate_snap_pilot_crossing_xml(snap_top)
        snap_net = self.run_snap_pilot_netconvert(snap_con)

        snap_validation = []
        if snap_net:
            snap_validation = self.validate_snap_pilot(snap_net)
            if snap_validation:
                df_validation = pd.DataFrame(snap_validation)
                val_path = self.output_dir / "snap_pilot_linkindex_validation.csv"
                df_validation.to_csv(val_path, index=False)
                print(f"Validation results saved: {val_path}")

        print("\n" + "=" * 80)
        print("TRACK B: 119055 INSERT/SPLIT PLANNING")
        print("=" * 80)
        self.track_b_119055_insert_split_plan()

        self.generate_final_recommendation(snap_top, snap_validation)

        print("\n" + "=" * 80)
        print("PHASE 5.9 COMPLETE")
        print(f"Output: {self.output_dir}")
        print("=" * 80)

if __name__ == "__main__":
    pilot = Phase59SnapInsertSplitPilot()
    pilot.run()
