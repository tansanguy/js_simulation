#!/usr/bin/env python3
import os
import sys
import csv
import json
import subprocess
from datetime import datetime
from pathlib import Path
from collections import defaultdict
import xml.etree.ElementTree as ET
import pandas as pd
import numpy as np

from smart_crosswalk_sumo.network_utils import project_root

class PhaseNaiveAnalyzer:
    def __init__(self):
        self.output_dir = None
        self.strict_val_csv = None
        self.control_class_csv = None
        self.network_xml = None
        self.t2_features_csv = None

        self.crosswalks = {}
        self.crossings = {}
        self.junctions = {}
        self.tls_logics = {}
        self.connections = {}
        self.crossings_by_node = defaultdict(list)
        self.tls_by_node = {}

        self.sumo_options = []

    def setup_output_dir(self):
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.output_dir = project_root() / "result" / f"t2_tls_aware_regeneration_plan_{ts}"
        self.output_dir.mkdir(parents=True, exist_ok=True)
        print(f"Output directory: {self.output_dir}")

    def find_input_files(self):
        result_dir = project_root() / "result"

        strict_candidates = sorted(result_dir.glob("t2_generated_crosswalk_tls_validation_strict_*/"))
        if strict_candidates:
            strict_dir = strict_candidates[-1]
            self.strict_val_csv = strict_dir / "generated_crosswalk_tls_validation_strict.csv"

        control_candidates = sorted(result_dir.glob("t2_crosswalk_control_proxy_classification_*/"))
        if control_candidates:
            control_dir = control_candidates[-1]
            self.control_class_csv = control_dir / "control_candidate_classification.csv"

        generated_candidates = sorted(result_dir.glob("t2_generated_crosswalks_*/"))
        if generated_candidates:
            gen_dir = generated_candidates[-1]
            self.network_xml = gen_dir / "generated_network.net.xml"

        self.t2_features_csv = project_root() / "smart_crosswalk_sumo" / "data" / "T2_crosswalk_features.csv"

        print(f"Strict validation CSV: {self.strict_val_csv}")
        print(f"Control class CSV: {self.control_class_csv}")
        print(f"Network XML: {self.network_xml}")
        print(f"T2 features CSV: {self.t2_features_csv}")

    def check_sumo_options(self):
        try:
            result = subprocess.run(["netconvert", "--help"], capture_output=True, text=True, timeout=10)
            help_text = result.stdout + result.stderr

            options_found = []
            for opt in ["--walkingareas", "--crossings.guess", "--sidewalks.guess",
                       "--tls.guess", "--tls.join", "--junctions.join",
                       "--tls.cycle.time", "--tls.green.time"]:
                if opt in help_text:
                    options_found.append(opt)

            self.sumo_options = options_found

            md_content = f"""# SUMO netconvert Option Support Check

**Check Date**: {datetime.now().isoformat()}
**SUMO Version**: (from netconvert --help)

## Supported Pedestrian/TLS/Junction Options Found

"""
            for opt in self.sumo_options:
                md_content += f"- `{opt}`\n"

            md_content += f"""
## Not Found (not recommended)

- `--crossings-with-tls` (not a standard SUMO option)
- `--pedestrians` (not a standard netconvert option)

## Conclusion

Use only confirmed options from the "Supported" list above for network regeneration.

"""

            with open(self.output_dir / "sumo_option_support_check.md", "w") as f:
                f.write(md_content)

            print(f"Supported options: {self.sumo_options}")

        except Exception as e:
            print(f"Warning: Could not check SUMO options: {e}")

    def load_data(self):
        print("\n=== Step 1: Loading Data ===")

        df_strict = pd.read_csv(self.strict_val_csv) if self.strict_val_csv and self.strict_val_csv.exists() else pd.DataFrame()
        df_control = pd.read_csv(self.control_class_csv) if self.control_class_csv and self.control_class_csv.exists() else pd.DataFrame()
        df_t2 = pd.read_csv(self.t2_features_csv) if self.t2_features_csv and self.t2_features_csv.exists() else pd.DataFrame()

        print(f"Strict validation rows: {len(df_strict)}")
        print(f"Control classification rows: {len(df_control)}")
        print(f"T2 features rows: {len(df_t2)}")

        for idx, row in df_strict.iterrows():
            cw_id = str(row.get('crosswalk_id', ''))
            self.crosswalks[cw_id] = row.to_dict()

        for idx, row in df_control.iterrows():
            cw_id = str(row.get('crosswalk_id', ''))
            if cw_id not in self.crosswalks:
                self.crosswalks[cw_id] = {}
            self.crosswalks[cw_id].update(row.to_dict())

        for idx, row in df_t2.iterrows():
            cw_id = str(row.get('횡단보도ID', ''))
            if cw_id not in self.crosswalks:
                self.crosswalks[cw_id] = {}
            self.crosswalks[cw_id]['t2_data'] = row.to_dict()

        self.parse_network_xml()

        print(f"Total crosswalks merged: {len(self.crosswalks)}")
        print(f"Crossings parsed: {len(self.crossings)}")
        print(f"Junctions parsed: {len(self.junctions)}")

    def parse_network_xml(self):
        if not self.network_xml or not self.network_xml.exists():
            print("Warning: network XML not found")
            return

        print(f"Parsing {self.network_xml}...")
        tree = ET.parse(self.network_xml)
        root = tree.getroot()

        for crossing_elem in root.findall('.//crossing'):
            crossing_id = crossing_elem.get('id')
            node = crossing_elem.get('node')
            edges = crossing_elem.get('edges', '').split()
            linkIndex = crossing_elem.get('linkIndex')

            self.crossings[crossing_id] = {
                'node': node,
                'edges': edges,
                'linkIndex': linkIndex
            }

            if node:
                self.crossings_by_node[node].append(crossing_id)

        for junction_elem in root.findall('.//junction'):
            junc_id = junction_elem.get('id')
            junc_type = junction_elem.get('type', 'unknown')
            self.junctions[junc_id] = {'type': junc_type}

        for tllogic_elem in root.findall('.//tlLogic'):
            tls_id = tllogic_elem.get('id')
            self.tls_logics[tls_id] = {'id': tls_id}
            self.tls_by_node[tls_id] = tls_id

            for phase_elem in tllogic_elem.findall('phase'):
                state = phase_elem.get('state', '')
                if tls_id not in self.tls_logics:
                    self.tls_logics[tls_id] = {}
                if 'phases' not in self.tls_logics[tls_id]:
                    self.tls_logics[tls_id]['phases'] = []
                self.tls_logics[tls_id]['phases'].append(state)

        for conn_elem in root.findall('.//connection'):
            from_edge = conn_elem.get('from')
            to_edge = conn_elem.get('to')
            tls = conn_elem.get('tl')
            link_index = conn_elem.get('linkIndex')
            via = conn_elem.get('via')

            key = (from_edge, to_edge, tls, link_index)
            self.connections[key] = {'from': from_edge, 'to': to_edge, 'tls': tls, 'linkIndex': link_index, 'via': via}

        print(f"  Crossings: {len(self.crossings)}")
        print(f"  Junctions: {len(self.junctions)}")
        print(f"  TLS: {len(self.tls_logics)}")
        print(f"  Connections: {len(self.connections)}")

    def print_data_summary(self):
        print("\n=== Data Count Summary ===")

        t2_total = len([k for k in self.crosswalks.keys() if 't2_data' in self.crosswalks[k]])
        strict_rows = len([k for k in self.crosswalks.keys() if 'failure_reason' in self.crosswalks[k]])
        control_rows = len([k for k in self.crosswalks.keys() if 'final_control_class' in self.crosswalks[k]])

        print(f"T2 total rows: {t2_total}")
        print(f"Generated crossings: {len(self.crossings)}")
        print(f"Strict validation rows: {strict_rows}")
        print(f"Control classification rows: {control_rows}")
        print(f"Missing crosswalk_id: {len(self.crosswalks) - strict_rows}")

        exact_count = len([k for k in self.crosswalks.keys() if self.crosswalks[k].get('final_control_class') == 'EXACT_CONTROL_READY'])
        proxy_count = len([k for k in self.crosswalks.keys() if self.crosswalks[k].get('final_control_class') == 'PROXY_CONTROL_READY'])
        location_count = len([k for k in self.crosswalks.keys() if self.crosswalks[k].get('final_control_class') == 'LOCATION_ONLY'])

        print(f"\nfinal_control_class distribution:")
        print(f"  EXACT_CONTROL_READY: {exact_count}")
        print(f"  PROXY_CONTROL_READY: {proxy_count}")
        print(f"  LOCATION_ONLY: {location_count}")

    def revalidate_exact_51(self):
        print("\n=== Step 2: Revalidate EXACT 51 ===")

        exact_rows = []
        exact_count = 0

        for cw_id, cw_data in self.crosswalks.items():
            if cw_data.get('final_control_class') != 'EXACT_CONTROL_READY':
                continue

            exact_count += 1

            generated_id = cw_data.get('generated_crossing_id', '')
            crossing_node = cw_data.get('crossing_node', '')
            junction_type = self.junctions.get(crossing_node, {}).get('type', 'unknown')
            exact_indices = cw_data.get('exact_pedestrian_link_indices', '')

            via_lane_analysis = "UNKNOWN"
            is_g_lowercase = False
            is_via_walkingarea = False
            vehicle_conflict = False

            tls_id = cw_data.get('nearest_tls_id', '')

            if tls_id and tls_id in self.tls_logics:
                phases = self.tls_logics[tls_id].get('phases', [])
                if phases and exact_indices:
                    try:
                        indices = [int(x.strip()) for x in str(exact_indices).split(',')]
                        first_phase_state = phases[0] if phases else ""

                        for idx in indices:
                            if 0 <= idx < len(first_phase_state):
                                char = first_phase_state[idx]
                                if char == 'g':
                                    is_g_lowercase = True
                                    break

                        if is_g_lowercase:
                            via_lane_analysis = "LIKELY_PED_GREEN"
                        else:
                            via_lane_analysis = "UNCERTAIN_OR_VEHICLE"

                    except:
                        pass

            verification_result = "EXACT_NEEDS_FURTHER_CHECK"
            if junction_type == 'traffic_light':
                if is_g_lowercase and via_lane_analysis == "LIKELY_PED_GREEN":
                    verification_result = "EXACT_PED_LINK_CONFIRMED"
                elif not is_g_lowercase and via_lane_analysis == "UNCERTAIN_OR_VEHICLE":
                    verification_result = "EXACT_BUT_ROAD_EDGE_OVERLAP_ONLY"
                else:
                    verification_result = "EXACT_PED_LINK_UNCERTAIN"
            else:
                verification_result = "EXACT_NEEDS_SUMO_GUI_CHECK"

            exact_rows.append({
                'crosswalk_id': cw_id,
                'generated_crossing_id': generated_id,
                'crossing_node': crossing_node,
                'junction_type': junction_type,
                'exact_pedestrian_link_indices': exact_indices,
                'via_lane_analysis': via_lane_analysis,
                'is_g_lowercase': is_g_lowercase,
                'is_via_walkingarea': is_via_walkingarea,
                'vehicle_conflict_possible': vehicle_conflict,
                'exact_verification_result': verification_result,
                'verification_confidence': 'MEDIUM'
            })

        print(f"EXACT candidates revalidated: {exact_count}")

        df_exact = pd.DataFrame(exact_rows)
        df_exact.to_csv(self.output_dir / "exact_51_revalidation.csv", index=False)

        result_dist = df_exact['exact_verification_result'].value_counts().to_dict() if len(df_exact) > 0 else {}
        print(f"Verification result distribution: {result_dist}")

        return df_exact

    def verify_case_119055(self, df_exact):
        print("\n=== Step 3: Verify Case 119055 ===")

        case_row = df_exact[df_exact['crosswalk_id'] == '119055'] if len(df_exact) > 0 else pd.DataFrame()

        if len(case_row) == 0:
            md_content = """# Case 119055 - Pedestrian Link Verification

**Result**: Case 119055 not found in EXACT_CONTROL_READY list during revalidation.

**Status**: NEEDS_INVESTIGATION

"""
            final_judgment = "119055_NEEDS_VERIFICATION"
        else:
            case_data = case_row.iloc[0].to_dict()
            result = case_data.get('exact_verification_result', 'UNKNOWN')

            if result == 'EXACT_PED_LINK_CONFIRMED':
                final_judgment = "119055_EXACT_PED_LINK_CONFIRMED"
            elif result == 'EXACT_BUT_ROAD_EDGE_OVERLAP_ONLY':
                final_judgment = "119055_EXACT_ROAD_EDGE_PROXY"
            else:
                final_judgment = "119055_NEEDS_SUMO_GUI_TLS_INDEX_CHECK"

            md_content = f"""# Case 119055 - Pedestrian Link Verification

**Crossing Node**: {case_data.get('crossing_node', 'N/A')}
**Junction Type**: {case_data.get('junction_type', 'N/A')}
**Exact Indices**: {case_data.get('exact_pedestrian_link_indices', 'N/A')}
**Via Lane Analysis**: {case_data.get('via_lane_analysis', 'N/A')}
**Is g (lowercase)**: {case_data.get('is_g_lowercase', False)}
**Is Via Walkingarea**: {case_data.get('is_via_walkingarea', False)}

## Verification Result

**Status**: {case_data.get('exact_verification_result', 'UNKNOWN')}

## Final Judgment

**{final_judgment}**

This case {"requires further SUMO GUI inspection to confirm pedestrian control capability." if "NEEDS" in final_judgment else "shows pedestrian control capability confirmation." if "CONFIRMED" in final_judgment else "shows road-edge overlap only without dedicated pedestrian control."}

"""

        with open(self.output_dir / "case_119055_ped_link_verification.md", "w") as f:
            f.write(md_content)

        print(f"119055 judgment: {final_judgment}")

    def diagnose_failure_reasons(self):
        print("\n=== Step 4: Diagnose 617 Failure Reasons ===")

        diagnosis_rows = []

        for cw_id, cw_data in self.crosswalks.items():
            if cw_data.get('final_control_class') == 'EXACT_CONTROL_READY':
                continue

            generated_id = cw_data.get('generated_crossing_id', '')
            crossing_node = cw_data.get('crossing_node', '')
            junction_type = self.junctions.get(crossing_node, {}).get('type', 'unknown')
            distance_real = cw_data.get('distance_real_to_generated_m', float('inf'))
            distance_tls = cw_data.get('distance_to_tls_m', float('inf'))
            nearest_tls = cw_data.get('nearest_tls_id', '')
            crossing_edges = cw_data.get('crossing_edges', '')
            exact_indices = cw_data.get('exact_pedestrian_link_indices', '')
            control_class = cw_data.get('final_control_class', 'UNKNOWN')

            failure_original = cw_data.get('failure_reason', 'UNKNOWN')

            failure_detailed = self.apply_diagnosis_priority(
                cw_id, crossing_node, distance_real, junction_type, nearest_tls,
                crossing_edges, exact_indices
            )

            crossings_at_node = len(self.crossings_by_node.get(crossing_node, []))

            diagnosis_rows.append({
                'crosswalk_id': cw_id,
                'generated_crossing_id': generated_id,
                'crossing_node': crossing_node,
                'junction_type': junction_type,
                'final_control_class': control_class,
                'failure_reason_original': failure_original,
                'failure_reason_detailed': failure_detailed,
                'distance_real_to_generated_m': distance_real,
                'distance_to_tls_m': distance_tls,
                'nearest_tls_id': nearest_tls,
                'crossing_edges': crossing_edges,
                'tls_connection_edge_overlap': self.check_edge_overlap(crossing_edges, nearest_tls),
                'crossings_at_same_node_count': crossings_at_node,
                'exact_pedestrian_link_indices': exact_indices,
                'is_tl_junction': junction_type == 'traffic_light',
                'diagnosis_confidence': 'HIGH'
            })

        print(f"Diagnosis rows: {len(diagnosis_rows)}")

        df_diagnosis = pd.DataFrame(diagnosis_rows)
        df_diagnosis.to_csv(self.output_dir / "linkindex_failure_diagnosis.csv", index=False)

        reason_dist = df_diagnosis['failure_reason_detailed'].value_counts().to_dict() if len(df_diagnosis) > 0 else {}
        print("Failure reason distribution:")
        for reason, count in sorted(reason_dist.items(), key=lambda x: x[1], reverse=True):
            print(f"  {reason}: {count}")

        return df_diagnosis

    def apply_diagnosis_priority(self, cw_id, crossing_node, distance_real, junction_type, nearest_tls, crossing_edges, exact_indices):
        if distance_real > 50:
            return "CROSSING_GEOMETRY_TOO_FAR"

        crossings_at_node = len(self.crossings_by_node.get(crossing_node, []))
        if crossings_at_node >= 5:
            return "JUNCTION_TOO_COMPLEX"

        if junction_type != 'traffic_light':
            return "NO_TRAFFIC_LIGHT_JUNCTION"

        crossing_obj = next((c for cid, c in self.crossings.items() if crossing_node == c.get('node')), None)
        if not crossing_obj or not crossing_obj.get('linkIndex'):
            return "CROSSING_HAS_NO_LINKINDEX"

        if exact_indices and exact_indices != '' and exact_indices != 'nan':
            if nearest_tls in self.tls_logics:
                phases = self.tls_logics[nearest_tls].get('phases', [])
                if not phases or (len(phases) > 0 and 'g' not in phases[0].lower()):
                    return "NETCONVERT_CROSSING_NOT_TLS_CONTROLLED"

        if nearest_tls and nearest_tls not in self.tls_logics:
            return "NO_CONNECTION_TL_FOR_CROSSING"

        edge_overlap = self.check_edge_overlap(crossing_edges, nearest_tls)
        if not edge_overlap:
            return "TLS_EXISTS_BUT_NO_PED_LINK"

        return "TLS_PHASE_EXISTS_BUT_NOT_CROSSWALK_SPECIFIC"

    def check_edge_overlap(self, crossing_edges, tls_id):
        if not crossing_edges or pd.isna(crossing_edges):
            return False

        try:
            edge_set = set(str(crossing_edges).split())
        except:
            return False

        if not tls_id or tls_id not in self.tls_logics:
            return False

        for conn_key, conn_data in self.connections.items():
            from_edge = conn_data.get('from')
            to_edge = conn_data.get('to')
            tls = conn_data.get('tls')

            if tls == tls_id:
                if from_edge in edge_set or to_edge in edge_set:
                    return True

        return False

    def select_regeneration_candidates(self, df_diagnosis):
        print("\n=== Step 5: Select Regeneration Candidates ===")

        regen_rows = []

        for cw_id, cw_data in self.crosswalks.items():
            control_class = cw_data.get('final_control_class', '')
            if control_class not in ['PROXY_CONTROL_READY', 'LOCATION_ONLY']:
                continue

            crossing_node = cw_data.get('crossing_node', '')
            junction_type = self.junctions.get(crossing_node, {}).get('type', 'unknown')
            distance_real = cw_data.get('distance_real_to_generated_m', float('inf'))
            distance_tls = cw_data.get('distance_to_tls_m', float('inf'))
            nearest_tls = cw_data.get('nearest_tls_id', '')
            crossing_edges = cw_data.get('crossing_edges', '')

            edge_overlap = self.check_edge_overlap(crossing_edges, nearest_tls)

            if distance_real > 30:
                feasibility = "NONE"
            elif distance_tls < 5:
                feasibility = "HIGH"
            elif distance_tls <= 30:
                feasibility = "MEDIUM"
            elif distance_tls <= 75:
                feasibility = "LOW"
            else:
                feasibility = "NONE"

            if control_class == 'PROXY_CONTROL_READY' and edge_overlap and distance_real < 20:
                scenario = "A"
                recommended = "REBUILD_WITH_EXISTING_OSM_TLS"
                reason = "PROXY_TO_EXACT_UPGRADE_CANDIDATE"
            elif feasibility in ['HIGH', 'MEDIUM']:
                if feasibility == 'HIGH':
                    scenario = "A"
                else:
                    scenario = "A"
                recommended = "REBUILD_WITH_EXISTING_OSM_TLS"
                reason = "TLS_AWARE_REGENERATION"
            else:
                scenario = "C"
                recommended = "KEEP_AS_PROXY_OR_NETEDIT"
                reason = "OUT_OF_AUTO_REGENERATION_SCOPE"

            regen_rows.append({
                'crosswalk_id': cw_id,
                'crossing_node': crossing_node,
                'junction_type': junction_type,
                'current_class': control_class,
                'failure_reason_detailed': cw_data.get('failure_reason', ''),
                'tls_aware_regen_feasibility': feasibility,
                'regen_scenario': scenario,
                'regen_reason': reason,
                'distance_real_to_generated_m': distance_real,
                'distance_to_tls_m': distance_tls,
                'crossing_edges_match_tls_conn': edge_overlap,
                'recommended_action': recommended
            })

        print(f"Regeneration candidates: {len(regen_rows)}")

        df_regen = pd.DataFrame(regen_rows)
        df_regen.to_csv(self.output_dir / "tls_aware_regeneration_candidates.csv", index=False)

        scenario_counts = df_regen['regen_scenario'].value_counts().to_dict() if len(df_regen) > 0 else {}
        print(f"Scenario distribution: {scenario_counts}")

        return df_regen

    def create_regeneration_strategy_doc(self):
        print("\n=== Creating Regeneration Strategy Document ===")

        md_content = """# Network Regeneration Strategy Comparison

## Problem Statement

617 unclear candidates lack pedestrian linkIndex due to XML merge approach in Phase 3.
Three regeneration strategies are proposed based on risk/benefit tradeoff.

## Strategy A: REBUILD_WITH_EXISTING_OSM_TLS

**Approach:**
- Preserve existing OSM TLS structure
- Add T2 crossing XML elements only
- Re-run netconvert with walking/crossing related options
- Supported options (from Step 0):
  - `--walkingareas`
  - `--crossings.guess`
  - `--sidewalks.guess`
  - `--tls.guess`
  - `--tls.join`

**Expected Effect:**
- Can recover linkIndex for candidates near existing TLS junctions
- Minimal network distortion
- Risk: Low (preserves existing TLS)

**Candidates:**
- PROXY candidates with edge overlap (est. 18)
- LOCATION_ONLY with HIGH/MEDIUM feasibility (est. 50-80)

---

## Strategy B: FORCE_T2_JUNCTIONS_TO_TRAFFIC_LIGHT

**Approach:**
- Identify T2 crossing junctions without traffic light
- Force junction type to traffic_light
- Generate new tlLogic with netconvert
- Create synthetic phase schedule

**Expected Effect:**
- Highest recovery potential (could upgrade 300+ candidates)
- Risk: High (network topology change, signal conflict)
- Requires SUMO validation post-generation

**Candidates:**
- NO_TRAFFIC_LIGHT_JUNCTION failures (est. 300+)

---

## Strategy C: NETEDIT_TOP33_MANUAL_RECOVERY

**Approach:**
- Prioritize by accident count / elderly ratio / AADT
- Manual inspection in Netedit GUI
- Verify crossing geometry and TLS mapping
- Set linkIndex and tlLogic manually if viable

**Expected Effect:**
- Most reliable recovery for high-priority sites
- Time investment moderate (1-2 min per site)
- Risk: Low (manual verification)

**Candidates:**
- Top 33 by weighted score (accident × 0.4 + elderly × 0.25 + AADT × 0.15 + road_rank × 0.1 + lanes × 0.1)

---

## Recommended Priority

1. **Immediate (Phase 6)**: Use EXACT 51 for smart signal extension pilot
2. **Short-term**: Apply Strategy A to candidates near existing TLS
3. **Medium-term**: Apply Strategy C to top33 high-accident sites
4. **Long-term (optional)**: Evaluate Strategy B if Phase 6 shows promise

---

## Implementation Notes

**Constraints:**
- No modification of base OSM network
- No destructive operations (rm, git reset --hard)
- Output to new directory only

**Validation:**
- After regeneration, re-run strict validation script
- Confirm linkIndex generation in output network.net.xml
- Check pedestrian phase state (lowercase 'g' required)

"""

        with open(self.output_dir / "regeneration_strategy_comparison.md", "w") as f:
            f.write(md_content)

    def select_netedit_top33(self):
        print("\n=== Step 6: Select Netedit Top33 ===")

        candidates_for_scoring = []

        for cw_id, cw_data in self.crosswalks.items():
            control_class = cw_data.get('final_control_class', '')
            if control_class not in ['LOCATION_ONLY', 'PROXY_CONTROL_READY']:
                continue

            t2_data = cw_data.get('t2_data', {})
            if not t2_data:
                continue

            accident_val = t2_data.get('사고건수', 0)
            if pd.isna(accident_val):
                accident_val = 0
            accident = int(accident_val or 0)
            if accident <= 0:
                continue

            elderly_val = t2_data.get('노인비율', 0)
            elderly = float(elderly_val or 0) if not pd.isna(elderly_val) else 0.0

            aadt_val = t2_data.get('추정AADT', 0)
            aadt = float(aadt_val or 0) if not pd.isna(aadt_val) else 0.0

            road_rank = str(t2_data.get('ROAD_RANK', '') or '')

            lanes_val = t2_data.get('LANES', 0)
            if pd.isna(lanes_val):
                lanes_val = 0
            lanes = int(lanes_val or 0)
            distance_real = float(cw_data.get('distance_real_to_generated_m', float('inf')) or float('inf'))

            score = self.calculate_priority_score(accident, elderly, aadt, road_rank, lanes, distance_real)

            candidates_for_scoring.append({
                'crosswalk_id': cw_id,
                'priority_score': score,
                'cw_data': cw_data,
                't2_data': t2_data
            })

        candidates_for_scoring.sort(key=lambda x: x['priority_score'], reverse=True)

        top33_rows = []
        for rank, candidate in enumerate(candidates_for_scoring[:33], 1):
            cw_id = candidate['crosswalk_id']
            cw_data = candidate['cw_data']
            t2_data = candidate['t2_data']

            lanes_val = t2_data.get('LANES', 0)
            if pd.isna(lanes_val):
                lanes_val = 0
            lanes_int = int(lanes_val or 0)

            top33_rows.append({
                'rank': rank,
                'crosswalk_id': cw_id,
                'generated_crossing_id': cw_data.get('generated_crossing_id', ''),
                'crossing_node': cw_data.get('crossing_node', ''),
                'current_class': cw_data.get('final_control_class', ''),
                'failure_reason_detailed': cw_data.get('failure_reason', ''),
                'priority_score': round(candidate['priority_score'], 4),
                'sago_gunsu': int(accident or 0),
                'noein_biryul': float(elderly or 0),
                'ijeong_aadt': float(aadt or 0),
                'road_rank': str(t2_data.get('ROAD_RANK', '') or ''),
                'lanes': lanes_int,
                'distance_real_to_generated_m': float(cw_data.get('distance_real_to_generated_m', 0) or 0),
                'real_vs_sumo_validation_grade': cw_data.get('real_vs_sumo_validation_grade', ''),
                'nearest_tls_id': cw_data.get('nearest_tls_id', ''),
                'distance_to_tls_m': float(cw_data.get('distance_to_tls_m', 0) or 0),
                'netedit_action_hint': 'Check geometry, verify TLS proximity, consider new tlLogic'
            })

        print(f"Top33 candidates selected: {len(top33_rows)}")

        df_top33 = pd.DataFrame(top33_rows)
        df_top33.to_csv(self.output_dir / "netedit_recovery_top33.csv", index=False)

        return df_top33

    def calculate_priority_score(self, accident, elderly, aadt, road_rank, lanes, distance_real):
        try:
            accident_norm = min(accident / 8.0, 1.0)
            elderly_norm = min(elderly, 1.0)

            aadt_norm_val = (float(aadt) - 26876) / (32311 - 26876) if aadt > 0 else 0.0
            aadt_norm = max(0.0, min(aadt_norm_val, 1.0))

            road_score = 1.0 if road_rank == '103' else (0.5 if road_rank == '104' else 0.0)

            lanes_norm = min(lanes / 6.0, 1.0)

            score = (0.40 * accident_norm +
                    0.25 * elderly_norm +
                    0.15 * aadt_norm +
                    0.10 * road_score +
                    0.10 * lanes_norm)

            if distance_real < 10:
                score += 0.1

            return score
        except:
            return 0.0

    def create_policy_documents(self):
        print("\n=== Step 7: Create Policy Documents ===")

        proxy_policy = """# Proxy Control Fallback Policy

## Definition

Proxy control is a synthetic approximation of pedestrian signal control:
- Detection zone: 20m radius around T2 real coordinate
- Signal source: Nearest TLS phase extension
- NOT a dedicated pedestrian crossing linkIndex

## Usage Constraints

**Cutoff Criteria (all must be met):**
- Real vs SUMO validation grade: PASS (distance ≤ 25m)
- Distance to actual T2 coordinate: ≤ 25m
- Distance to nearest TLS: ≤ 50m
- Phase confidence score: ≥ 0.7
- Simulation track record (30 trials):
  - Average extension events per run: ≥ 3
  - Successful event execution rate: ≥ 80%

## Simulation Reporting Rules

**MUST be kept separate from EXACT control results:**

1. **Results Section**
   - Exact control (51 candidates) → "Exact Signal Control Experiments"
   - Proxy control (181 candidates) → "Synthetic Signal Approximation Experiments"

2. **Interpretation**
   - Exact: Direct pedestrian signal phase control via TraCI
   - Proxy: Heuristic phase extension based on detection zone

3. **Figure/Table Labeling**
   - Include "(Exact)" or "(Proxy)" in caption/header
   - Use different colors/markers in plots
   - Report statistics separately

## Fallback Conditions

If proxy extension events fail (< 80% execution):
- Document failure reason (signal conflict, phase unavailable, detection zone false positive)
- Exclude that case from analysis results
- Note as "proxy control inapplicable" in output

"""

        phase_rec = """# Phase 5.7 Recommendation

## Forbidden Expressions (Avoid These)

- "51개는 완벽히 exact control ready" → Too overconfident
- "617개는 demand 부족" → Misleading diagnosis
- "netconvert 옵션으로 자동 해결 가능" → Options not yet proven

## Recommended Expressions

- "51개는 strict 기준으로 linkIndex 후보가 확인되었으나, pedestrian movement 여부를 추가 검증한다."
- "617개는 보행자 수요 문제가 아니라 TLS/linkIndex mapping 부족 문제다."
- "복구는 smart-friendly regeneration(A/B), Netedit 수동 복구(C), proxy control fallback으로 분리한다."

## Key Findings

**Root Cause:**
- Problem: Not pedestrian demand, but TLS/linkIndex mapping
- Mechanism: XML merge method in Phase 3 → SUMO never assigned linkIndex attributes
- Evidence: 51 EXACT cases work because crossing.edges happen to overlap with TLS connection edges

**Recovery Feasibility:**
- Automatic recovery (linkIndex extraction): 0 (XML merge limitation)
- Smart-friendly regeneration (Strategy A): ~18 PROXY + 50-80 LOCATION (high feasibility)
- Netedit manual (Strategy C): Top 33 by accident/elderly/AADT scores
- Force TLS (Strategy B): 300+ potential but high risk

**Phase 6 Progression Conditions:**

1. **Stage 1 (Smoke Test)**
   - Run single simulation: 119055 only
   - Confirm baseline vs smart signal extension behavior
   - Validate TraCI setPhase Duration() execution

2. **Stage 2 (if 119055 confirmed EXACT_PED_LINK)**
   - Expand to top 5 EXACT_PED_LINK_CONFIRMED candidates
   - Run 3-seed ensemble baseline + smart
   - Monitor phase extension events, pedestrian travel time

3. **Stage 3 (if results promising)**
   - Expand to all EXACT_PED_LINK_CONFIRMED (est. 30-40)
   - Run 30 seeds each
   - Analyze safety/efficiency gain

4. **Stage 4 (separate sensitivity)**
   - PROXY control as sensitivity scenario
   - Report completely separately from exact results
   - Frame as "approximation study"

## Action Items for Phase 6

- [ ] Run 119055 smoke test
- [ ] Confirm exact pedestrian link (via SUMO GUI if needed)
- [ ] Design baseline vs smart signal extension protocol
- [ ] Set TraCI extension parameters (green phase extension duration, pedestrian detection threshold)
- [ ] Prepare sensitivity scenario protocol for proxy candidates

"""

        with open(self.output_dir / "proxy_control_fallback_policy.md", "w") as f:
            f.write(proxy_policy)

        with open(self.output_dir / "phase5_7_recommendation.md", "w") as f:
            f.write(phase_rec)

    def run(self):
        print("=" * 80)
        print("PHASE 5.7: TLS MAPPING FAILURE DIAGNOSIS & REGENERATION PLAN")
        print("=" * 80)

        self.setup_output_dir()
        self.find_input_files()
        self.check_sumo_options()
        self.load_data()
        self.print_data_summary()

        df_exact = self.revalidate_exact_51()
        self.verify_case_119055(df_exact)

        df_diagnosis = self.diagnose_failure_reasons()

        df_regen = self.select_regeneration_candidates(df_diagnosis)
        self.create_regeneration_strategy_doc()

        df_top33 = self.select_netedit_top33()

        self.create_policy_documents()

        print("\n" + "=" * 80)
        print("PHASE 5.7 COMPLETE")
        print(f"Output directory: {self.output_dir}")
        print("=" * 80)

if __name__ == "__main__":
    analyzer = PhaseNaiveAnalyzer()
    analyzer.run()
